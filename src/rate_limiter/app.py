"""Task 4: rate-limiting & model-fallback router.

`POST /v1/completions`: the caller declares `max_tokens` (mirrors OpenAI's
API) — no tokenizer in scope, so the request states its own cost rather
than the gateway estimating one. The tenant (from `Bearer <token>`) is
charged against a 50,000 tokens/minute sliding window *before* any upstream
call is made; a request that would exceed budget gets a standardized 429
and never reaches a model provider. A request within budget is routed
through `complete_with_failover` (primary, falling back to secondary on a
429 or a >3s timeout); if both providers fail, the client gets a generic,
sanitized 503 — no upstream detail ever reaches the response body.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from rate_limiter.limiter import TokenRateLimiter
from rate_limiter.router import RouterError, complete_with_failover

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s rate_limiter: %(message)s",
)
logger = logging.getLogger("rate_limiter")


@dataclass(frozen=True)
class RouterConfig:
    primary_url: str
    secondary_url: str
    timeout_s: float


DB_PATH = os.environ.get("RATE_LIMITER_DB_PATH", "rate_limiter.db")
TOKEN_LIMIT = int(os.environ.get("RATE_LIMIT_TOKENS_PER_MINUTE", "50000"))

app = FastAPI(title="rate-limiter-router")

_limiter = TokenRateLimiter(DB_PATH, limit=TOKEN_LIMIT, window_seconds=60.0)
_client: httpx.AsyncClient | None = None
_router_config = RouterConfig(
    primary_url=os.environ.get("PRIMARY_MODEL_URL", "http://127.0.0.1:9002/v1/completions"),
    secondary_url=os.environ.get("SECONDARY_MODEL_URL", "http://127.0.0.1:9003/v1/completions"),
    timeout_s=float(os.environ.get("FAILOVER_TIMEOUT_S", "3.0")),
)


def get_limiter() -> TokenRateLimiter:
    return _limiter


def get_router_config() -> RouterConfig:
    return _router_config


def get_http_client() -> httpx.AsyncClient:
    """Lazily-created singleton for the process lifetime; tests override
    all three dependencies to point at an in-process mock, a scratch DB,
    and fast timeouts."""
    global _client
    if _client is None:
        _client = httpx.AsyncClient()
    return _client


def _tenant_key(request: Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and token:
        return token
    return "anonymous"


def _error(code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status_code)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/completions")
async def completions(
    request: Request,
    client: httpx.AsyncClient = Depends(get_http_client),
    rate_limiter: TokenRateLimiter = Depends(get_limiter),
    router_config: RouterConfig = Depends(get_router_config),
) -> JSONResponse:
    body = await request.json()
    tokens = body.get("max_tokens")
    if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens <= 0:
        return _error("invalid_request", "max_tokens must be a positive integer", 400)

    tenant = _tenant_key(request)
    limit_result = await rate_limiter.check_and_record(tenant, tokens)
    if not limit_result.allowed:
        response = _error(
            "rate_limit_exceeded",
            f"Token rate limit exceeded for this tenant ({limit_result.limit} tokens/minute).",
            429,
        )
        if limit_result.retry_after_seconds is not None:
            response.headers["Retry-After"] = str(int(limit_result.retry_after_seconds) + 1)
        return response

    try:
        completion = await complete_with_failover(
            client,
            body,
            primary_url=router_config.primary_url,
            secondary_url=router_config.secondary_url,
            timeout_s=router_config.timeout_s,
        )
    except RouterError as exc:
        logger.error("both model providers failed for tenant=%r", tenant)
        return _error("upstream_unavailable", str(exc), 503)

    return JSONResponse(
        {
            "content": completion.text,
            "model": completion.model,
            "tenant_tokens_used": limit_result.used_tokens,
        }
    )
