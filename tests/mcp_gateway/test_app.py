import asyncio
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from mcp_gateway.app import app, get_downstream_client
from mocks.mcp_downstream import app as downstream_app


class _SpyClient:
    """Wraps a real (in-process) downstream client and records every call
    the gateway makes through it, so tests can assert an unauthorized
    tools/call never reached the downstream server at all."""

    def __init__(self, inner: httpx.AsyncClient):
        self._inner = inner
        self.calls: list[Any] = []

    async def post(self, url: str, json: Any = None, **kwargs: Any) -> httpx.Response:
        self.calls.append(json)
        return await self._inner.post(url, json=json, **kwargs)

    async def aclose(self) -> None:
        await self._inner.aclose()


@pytest.fixture
def downstream_spy() -> Iterator[_SpyClient]:
    inner = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=downstream_app), base_url="http://downstream"
    )
    spy = _SpyClient(inner)
    app.dependency_overrides[get_downstream_client] = lambda: spy
    try:
        yield spy
    finally:
        app.dependency_overrides.clear()
        asyncio.run(spy.aclose())


@pytest.fixture
def client(downstream_spy: _SpyClient) -> TestClient:
    return TestClient(app)


def test_health(client: TestClient):
    resp = client.get("/health")
    assert resp.status_code == 200


def test_tools_list_forwards_transparently_without_any_auth_header(
    client: TestClient, downstream_spy: _SpyClient
):
    resp = client.post("/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp.status_code == 200
    names = {tool["name"] for tool in resp.json()["result"]["tools"]}
    assert names == {"echo", "get_time", "admin_reset_key"}
    assert len(downstream_spy.calls) == 1


def test_normal_tool_call_allowed_without_any_auth_header(
    client: TestClient, downstream_spy: _SpyClient
):
    resp = client.post(
        "/",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "echo", "arguments": {"text": "hi"}},
        },
    )
    assert resp.status_code == 200
    assert resp.json()["result"]["content"][0]["text"] == "hi"
    assert len(downstream_spy.calls) == 1


def test_admin_tool_call_without_auth_header_is_blocked_and_never_forwarded(
    client: TestClient, downstream_spy: _SpyClient
):
    resp = client.post(
        "/",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "admin_reset_key", "arguments": {"account_id": "acct-1"}},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["error"]["code"] == -32001
    assert downstream_spy.calls == []


def test_admin_tool_call_with_viewer_role_is_blocked(
    client: TestClient, downstream_spy: _SpyClient
):
    resp = client.post(
        "/",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "admin_reset_key", "arguments": {"account_id": "acct-1"}},
        },
        headers={"Authorization": "Bearer viewer-token"},
    )
    assert resp.json()["error"]["code"] == -32001
    assert downstream_spy.calls == []


def test_admin_tool_call_with_unrecognized_token_is_blocked(
    client: TestClient, downstream_spy: _SpyClient
):
    resp = client.post(
        "/",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "admin_reset_key", "arguments": {"account_id": "acct-1"}},
        },
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert resp.json()["error"]["code"] == -32001
    assert downstream_spy.calls == []


def test_admin_tool_call_with_admin_role_is_forwarded(
    client: TestClient, downstream_spy: _SpyClient
):
    resp = client.post(
        "/",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "admin_reset_key", "arguments": {"account_id": "acct-1"}},
        },
        headers={"Authorization": "Bearer admin-token"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "error" not in body
    assert "acct-1" in body["result"]["content"][0]["text"]
    assert len(downstream_spy.calls) == 1


def test_malformed_json_returns_parse_error(client: TestClient):
    resp = client.post("/", content=b"{not json", headers={"Content-Type": "application/json"})
    assert resp.json()["error"]["code"] == -32700


def test_empty_batch_returns_invalid_request(client: TestClient):
    resp = client.post("/", json=[])
    assert resp.json()["error"]["code"] == -32600


def test_batch_blocks_only_the_unauthorized_admin_call(
    client: TestClient, downstream_spy: _SpyClient
):
    resp = client.post(
        "/",
        json=[
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": {"text": "hi"}},
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "admin_reset_key", "arguments": {"account_id": "acct-1"}},
            },
        ],
    )
    assert resp.status_code == 200
    body = {item["id"]: item for item in resp.json()}
    assert "result" in body[1]
    assert body[2]["error"]["code"] == -32001

    # The unauthorized call must never reach the downstream server, even
    # inside a batch that also contains an authorized call. Only one item
    # survived authorization, so the gateway forwards it bare (not as a
    # single-element batch array).
    assert len(downstream_spy.calls) == 1
    assert downstream_spy.calls[0]["params"]["name"] == "echo"


def test_notification_admin_tool_call_without_auth_gets_no_response(
    client: TestClient, downstream_spy: _SpyClient
):
    resp = client.post(
        "/",
        json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "admin_reset_key", "arguments": {"account_id": "acct-1"}},
        },
    )
    assert resp.status_code == 204
    assert downstream_spy.calls == []
