"""Mock LLM provider: an SSE-streaming completion endpoint whose behavior is
selected per-request via the `X-Mock-Behavior` header or `behavior` query
param (header wins if both given). Used as the upstream that Task 3 (PII
redaction) and Task 4 (rate limit / failover) gateways proxy to.

Behaviors:
  normal        — streams a short benign completion.
  rate_limited  — returns HTTP 429 immediately, no stream.
  slow          — sleeps `delay_ms` (query param, default 5000) before the
                   first chunk, to simulate exceeding a client timeout.
  pii           — streams synthetic email / SSN / credit-card-number text,
                   with each pattern deliberately split across a chunk
                   boundary so a naive per-chunk regex will miss it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s llm_provider: %(message)s",
)
logger = logging.getLogger("llm_provider")

app = FastAPI(title="mock-llm-provider")

KNOWN_BEHAVIORS = {"normal", "rate_limited", "slow", "pii"}

NORMAL_CHUNKS = [
    "The quick brown ",
    "fox jumps over ",
    "the lazy dog. ",
    "This is a mock ",
    "streaming completion.",
]

# Each PII pattern below is deliberately cut mid-token so it straddles a
# chunk boundary: "john.doe@exam" | "ple.com", "123-45-" | "6789",
# "4111-1111-1111-" | "1111".
PII_CHUNKS = [
    "Contact John at john.doe@exam",
    "ple.com or call regarding SSN 123-45-",
    "6789. His card 4111-1111-1111-",
    "1111 was charged. Thanks!",
]

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _behavior(request: Request) -> str:
    header_val = request.headers.get("x-mock-behavior")
    if header_val:
        return header_val.lower()
    query_val = request.query_params.get("behavior")
    if query_val:
        return query_val.lower()
    return "normal"


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


async def _stream_chunks(chunks: list[str], delay_before: float) -> AsyncIterator[str]:
    if delay_before:
        await asyncio.sleep(delay_before)
    for chunk in chunks:
        yield _sse(
            {
                "id": "mock-completion-0",
                "object": "text_completion.chunk",
                "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}],
            }
        )
        await asyncio.sleep(0.01)
    yield _sse(
        {
            "id": "mock-completion-0",
            "object": "text_completion.chunk",
            "choices": [],
            "finish_reason": "stop",
        }
    )
    yield "data: [DONE]\n\n"


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/completions")
async def completions(request: Request):
    behavior = _behavior(request)

    if behavior == "rate_limited":
        logger.info("returning 429 for rate_limited behavior")
        return JSONResponse(
            {
                "error": {
                    "message": "Rate limit exceeded",
                    "type": "rate_limit_error",
                    "code": "rate_limited",
                }
            },
            status_code=429,
            headers={"Retry-After": "1"},
        )

    if behavior not in KNOWN_BEHAVIORS:
        return JSONResponse(
            {
                "error": {
                    "message": f"Unknown behavior: {behavior!r}",
                    "type": "invalid_request_error",
                }
            },
            status_code=400,
        )

    delay_before = 0.0
    if behavior == "slow":
        delay_ms = int(request.query_params.get("delay_ms", "5000"))
        delay_before = delay_ms / 1000

    chunks = PII_CHUNKS if behavior == "pii" else NORMAL_CHUNKS
    logger.info(
        "streaming behavior=%s chunks=%d delay_before=%.3fs", behavior, len(chunks), delay_before
    )
    return StreamingResponse(
        _stream_chunks(chunks, delay_before),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
