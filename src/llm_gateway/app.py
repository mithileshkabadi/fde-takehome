"""Task 3: LLM gateway streaming guardrail — proxies a completion request to
an upstream LLM provider and streams the response back, redacting PII from
the *content* of the stream in real time.

Redaction operates on the logical text stream (each SSE event's
`choices[0].delta.content`), not on the raw SSE bytes — the wire framing
(JSON envelopes, `data: ` prefixes, blank lines) is reconstructed around
already-redacted text on the way out. Redacting raw bytes instead would risk
a match spanning two separate JSON events and corrupting the JSON structure
itself; operating on the extracted content stream sidesteps that entirely.

A non-200 upstream response (e.g. the mock's `rate_limited` behavior, a
plain 429 JSON body, not a stream) is detected before committing to a
streaming response and passed through as-is. Timeout/retry/failover for a
slow or failing upstream is explicitly out of scope here — that's Task 4.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import AsyncIterator

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import Response, StreamingResponse

from llm_gateway.redactor import PiiStreamRedactor

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s llm_gateway: %(message)s",
)
logger = logging.getLogger("llm_gateway")

UPSTREAM_URL = os.environ.get("LLM_UPSTREAM_URL", "http://127.0.0.1:9002/v1/completions")

app = FastAPI(title="llm-gateway")

_client: httpx.AsyncClient | None = None


def get_upstream_client() -> httpx.AsyncClient:
    """Lazily-created singleton `httpx.AsyncClient` for the process lifetime.

    Tests replace this via `app.dependency_overrides` to point at an
    in-process mock instead of a real network call. The read timeout here is
    a generous safety net, not the task's timeout/failover requirement —
    that's Task 4's job, layered on top of this gateway.
    """
    global _client
    if _client is None:
        timeout = httpx.Timeout(connect=5.0, read=30.0, write=5.0, pool=5.0)
        _client = httpx.AsyncClient(timeout=timeout)
    return _client


def _forwardable_params(request: Request) -> dict[str, str]:
    # Only the mock's own test-control knobs are forwarded; a real gateway
    # would not blindly relay arbitrary client query params upstream.
    allowed = {"behavior", "delay_ms"}
    return {k: v for k, v in request.query_params.items() if k in allowed}


def _forwardable_headers(request: Request) -> dict[str, str]:
    headers: dict[str, str] = {}
    behavior = request.headers.get("x-mock-behavior")
    if behavior:
        headers["X-Mock-Behavior"] = behavior
    return headers


def _extract_delta_content(event: dict) -> str:
    choices = event.get("choices") or []
    if not choices:
        return ""
    delta = choices[0].get("delta") or {}
    return delta.get("content") or ""


def _sse_content_event(text: str) -> bytes:
    payload = {
        "id": "llm-gateway-redacted",
        "object": "text_completion.chunk",
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
    }
    return f"data: {json.dumps(payload)}\n\n".encode()


async def _redact_and_stream(upstream_response: httpx.Response) -> AsyncIterator[bytes]:
    redactor = PiiStreamRedactor()
    done_seen = False
    try:
        async for line in upstream_response.aiter_lines():
            if not line.startswith("data: "):
                continue
            payload = line[len("data: ") :]
            if payload == "[DONE]":
                done_seen = True
                break
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                logger.warning("skipping malformed SSE payload from upstream: %r", payload)
                continue
            content = _extract_delta_content(event)
            if not content:
                continue
            safe_text = redactor.feed(content)
            if safe_text:
                yield _sse_content_event(safe_text)

        tail = redactor.flush()
        if tail:
            yield _sse_content_event(tail)
        if done_seen:
            yield b"data: [DONE]\n\n"
    finally:
        await upstream_response.aclose()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/completions")
async def completions(
    request: Request, client: httpx.AsyncClient = Depends(get_upstream_client)
) -> Response:
    body = await request.json()
    upstream_request = client.build_request(
        "POST",
        UPSTREAM_URL,
        json=body,
        params=_forwardable_params(request),
        headers=_forwardable_headers(request),
    )
    upstream_response = await client.send(upstream_request, stream=True)

    if upstream_response.status_code != 200:
        content = await upstream_response.aread()
        await upstream_response.aclose()
        logger.info(
            "upstream returned %s; passing through unchanged", upstream_response.status_code
        )
        return Response(
            content=content,
            status_code=upstream_response.status_code,
            media_type=upstream_response.headers.get("content-type", "application/json"),
        )

    return StreamingResponse(
        _redact_and_stream(upstream_response),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
