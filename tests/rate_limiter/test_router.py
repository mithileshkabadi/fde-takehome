import asyncio

import httpx
import pytest

from mocks.llm_provider import app as upstream_app
from rate_limiter.router import RouterError, complete_with_failover

# Small timeout/delay values so timeout-triggering tests run fast rather
# than actually waiting out anything close to the real 3000ms default.
FAST_TIMEOUT = 0.2
SLOW_DELAY_MS = 2000


@pytest.fixture
def client():
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=upstream_app), base_url="http://mock")
    try:
        yield c
    finally:
        asyncio.run(c.aclose())


def _url(behavior: str, **params: str) -> str:
    query = "&".join([f"behavior={behavior}", *(f"{k}={v}" for k, v in params.items())])
    return f"http://mock/v1/completions?{query}"


def test_primary_success_never_touches_secondary(client):
    result = asyncio.run(
        complete_with_failover(
            client,
            {"prompt": "hi"},
            primary_url=_url("normal"),
            secondary_url=_url("rate_limited"),  # would fail if ever called
            timeout_s=FAST_TIMEOUT,
        )
    )
    assert result.model == "primary"
    assert result.text  # got the mock's normal completion text


def test_primary_429_fails_over_to_secondary(client):
    result = asyncio.run(
        complete_with_failover(
            client,
            {"prompt": "hi"},
            primary_url=_url("rate_limited"),
            secondary_url=_url("normal"),
            timeout_s=FAST_TIMEOUT,
        )
    )
    assert result.model == "secondary"
    assert result.text


def test_primary_timeout_fails_over_to_secondary(client):
    result = asyncio.run(
        complete_with_failover(
            client,
            {"prompt": "hi"},
            primary_url=_url("slow", delay_ms=str(SLOW_DELAY_MS)),
            secondary_url=_url("normal"),
            timeout_s=FAST_TIMEOUT,
        )
    )
    assert result.model == "secondary"
    assert result.text


def test_both_providers_failing_raises_sanitized_router_error(client):
    with pytest.raises(RouterError) as exc_info:
        asyncio.run(
            complete_with_failover(
                client,
                {"prompt": "hi"},
                primary_url=_url("rate_limited"),
                secondary_url=_url("rate_limited"),
                timeout_s=FAST_TIMEOUT,
            )
        )
    message = str(exc_info.value)
    # No upstream internals (status codes, exception class names, mock
    # internals) may leak into the message shown to a caller.
    assert "429" not in message
    assert "rate_limit" not in message.lower()
    assert "traceback" not in message.lower()


def test_both_providers_timing_out_raises_sanitized_router_error(client):
    with pytest.raises(RouterError):
        asyncio.run(
            complete_with_failover(
                client,
                {"prompt": "hi"},
                primary_url=_url("slow", delay_ms=str(SLOW_DELAY_MS)),
                secondary_url=_url("slow", delay_ms=str(SLOW_DELAY_MS)),
                timeout_s=FAST_TIMEOUT,
            )
        )
