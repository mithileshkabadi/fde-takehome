"""Primary/secondary model failover.

Races a call to the primary model against `timeout_s`; on a primary 429 or
timeout, retries once against the secondary (itself bounded by the same
timeout, so a hung secondary can't hang the request indefinitely either).
`asyncio.wait_for` handles both the race and the cancellation of the losing
attempt — cancelling it propagates into the `httpx` streaming context
manager, which closes the connection cleanly on unwind.

Only ever raises `RouterError`, whose message is safe to show a client
as-is: upstream status codes, connection errors, and any other internal
detail are logged (server-side, stderr) but never included in the message
that reaches the caller. That's the "standardized error payload, no leaked
internals" requirement — enforced at the one place all failure paths must
pass through, not re-implemented at each call site.

Reuses the SSE wire format from `mocks/llm_provider.py` / `llm_gateway`, but
collects the full completion rather than streaming it back — Task 4's eval
criteria are about rate limiting and failover mechanics, not streaming
delivery, which Task 3 already covers.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger("rate_limiter")


@dataclass(frozen=True)
class CompletionResult:
    text: str
    model: str  # "primary" or "secondary"


class RouterError(Exception):
    """Both primary and secondary failed. `str(self)` is safe to return to a client."""


class _UpstreamRateLimited(Exception):
    pass


class _UpstreamError(Exception):
    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"upstream returned status {status_code}")


def _extract_delta_content(event: dict) -> str:
    choices = event.get("choices") or []
    if not choices:
        return ""
    delta = choices[0].get("delta") or {}
    return delta.get("content") or ""


async def _collect(client: httpx.AsyncClient, url: str, body: dict) -> str:
    async with client.stream("POST", url, json=body) as response:
        if response.status_code == 429:
            raise _UpstreamRateLimited()
        if response.status_code != 200:
            await response.aread()
            raise _UpstreamError(response.status_code)

        parts: list[str] = []
        async for line in response.aiter_lines():
            if not line.startswith("data: "):
                continue
            payload = line[len("data: ") :]
            if payload == "[DONE]":
                break
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue
            parts.append(_extract_delta_content(event))
        return "".join(parts)


async def _attempt(client: httpx.AsyncClient, url: str, body: dict, timeout_s: float) -> str:
    return await asyncio.wait_for(_collect(client, url, body), timeout=timeout_s)


async def complete_with_failover(
    client: httpx.AsyncClient,
    body: dict,
    *,
    primary_url: str,
    secondary_url: str,
    timeout_s: float = 3.0,
) -> CompletionResult:
    try:
        text = await _attempt(client, primary_url, body, timeout_s)
        return CompletionResult(text=text, model="primary")
    except (_UpstreamRateLimited, _UpstreamError, TimeoutError, httpx.HTTPError) as primary_exc:
        logger.warning("primary model failed (%r); failing over to secondary", primary_exc)

    try:
        text = await _attempt(client, secondary_url, body, timeout_s)
        return CompletionResult(text=text, model="secondary")
    except (_UpstreamRateLimited, _UpstreamError, TimeoutError, httpx.HTTPError) as secondary_exc:
        logger.error("secondary model also failed (%r)", secondary_exc)
        raise RouterError(
            "The model provider is temporarily unavailable. Please try again shortly."
        ) from secondary_exc
