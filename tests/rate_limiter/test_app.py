import asyncio
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from mocks.llm_provider import app as upstream_app
from rate_limiter.app import RouterConfig, app, get_http_client, get_limiter, get_router_config
from rate_limiter.limiter import TokenRateLimiter

FAST_TIMEOUT = 0.2

NORMAL_CONFIG = RouterConfig(
    primary_url="http://mock/v1/completions?behavior=normal",
    secondary_url="http://mock/v1/completions?behavior=normal",
    timeout_s=FAST_TIMEOUT,
)


class _SpyClient:
    """Wraps the in-process mock client and records every URL requested, so
    tests can assert a rate-limited request never reaches an upstream model
    provider at all."""

    def __init__(self, inner: httpx.AsyncClient):
        self._inner = inner
        self.requested_urls: list[str] = []

    def stream(self, method: str, url: str, **kwargs: Any):
        self.requested_urls.append(url)
        return self._inner.stream(method, url, **kwargs)

    async def aclose(self) -> None:
        await self._inner.aclose()


@pytest.fixture
def spy_client() -> Iterator[_SpyClient]:
    inner = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=upstream_app), base_url="http://mock"
    )
    spy = _SpyClient(inner)
    app.dependency_overrides[get_http_client] = lambda: spy
    try:
        yield spy
    finally:
        app.dependency_overrides.pop(get_http_client, None)
        asyncio.run(spy.aclose())


@pytest.fixture
def gateway_client(spy_client: _SpyClient, tmp_path) -> Iterator[TestClient]:
    limiter = TokenRateLimiter(str(tmp_path / "rl.db"), limit=1000, window_seconds=60.0)
    app.dependency_overrides[get_limiter] = lambda: limiter
    app.dependency_overrides[get_router_config] = lambda: NORMAL_CONFIG
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_limiter, None)
        app.dependency_overrides.pop(get_router_config, None)


def test_health(gateway_client: TestClient):
    assert gateway_client.get("/health").status_code == 200


def test_successful_completion_within_budget(gateway_client: TestClient):
    resp = gateway_client.post("/v1/completions", json={"prompt": "hi", "max_tokens": 100})
    assert resp.status_code == 200
    body = resp.json()
    assert body["model"] == "primary"
    assert body["tenant_tokens_used"] == 100
    assert body["content"]


def test_missing_max_tokens_returns_400(gateway_client: TestClient):
    resp = gateway_client.post("/v1/completions", json={"prompt": "hi"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_request"


def test_non_positive_max_tokens_returns_400(gateway_client: TestClient):
    resp = gateway_client.post("/v1/completions", json={"prompt": "hi", "max_tokens": 0})
    assert resp.status_code == 400


def test_over_budget_request_returns_429_and_never_calls_upstream(
    gateway_client: TestClient, spy_client: _SpyClient
):
    resp = gateway_client.post(
        "/v1/completions",
        json={"prompt": "hi", "max_tokens": 2000},
        headers={"Authorization": "Bearer tenant-x"},
    )
    assert resp.status_code == 429
    body = resp.json()
    assert body["error"]["code"] == "rate_limit_exceeded"
    assert "Retry-After" in resp.headers
    assert spy_client.requested_urls == []


def test_second_request_pushing_tenant_over_budget_is_rejected(gateway_client: TestClient):
    headers = {"Authorization": "Bearer tenant-y"}
    first = gateway_client.post(
        "/v1/completions", json={"prompt": "hi", "max_tokens": 700}, headers=headers
    )
    assert first.status_code == 200
    second = gateway_client.post(
        "/v1/completions", json={"prompt": "hi", "max_tokens": 400}, headers=headers
    )
    assert second.status_code == 429


def test_different_tenants_have_independent_budgets(gateway_client: TestClient):
    gateway_client.post(
        "/v1/completions",
        json={"prompt": "hi", "max_tokens": 900},
        headers={"Authorization": "Bearer tenant-a"},
    )
    resp = gateway_client.post(
        "/v1/completions",
        json={"prompt": "hi", "max_tokens": 900},
        headers={"Authorization": "Bearer tenant-b"},
    )
    assert resp.status_code == 200


def test_primary_failure_fails_over_and_is_reported_in_response(gateway_client: TestClient):
    app.dependency_overrides[get_router_config] = lambda: RouterConfig(
        primary_url="http://mock/v1/completions?behavior=rate_limited",
        secondary_url="http://mock/v1/completions?behavior=normal",
        timeout_s=FAST_TIMEOUT,
    )
    resp = gateway_client.post("/v1/completions", json={"prompt": "hi", "max_tokens": 100})

    assert resp.status_code == 200
    assert resp.json()["model"] == "secondary"


def test_both_providers_down_returns_sanitized_503(gateway_client: TestClient):
    app.dependency_overrides[get_router_config] = lambda: RouterConfig(
        primary_url="http://mock/v1/completions?behavior=rate_limited",
        secondary_url="http://mock/v1/completions?behavior=rate_limited",
        timeout_s=FAST_TIMEOUT,
    )
    resp = gateway_client.post("/v1/completions", json={"prompt": "hi", "max_tokens": 100})

    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "upstream_unavailable"
    message = body["error"]["message"]
    assert "429" not in message
    assert "traceback" not in message.lower()
    assert "mock" not in message.lower()
