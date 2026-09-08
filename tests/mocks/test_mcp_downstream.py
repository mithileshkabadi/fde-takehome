from fastapi.testclient import TestClient

from mocks.mcp_downstream import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    app,
)

client = TestClient(app)


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_tools_list_returns_all_tools_including_admin():
    resp = client.post("/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == 1
    names = {tool["name"] for tool in body["result"]["tools"]}
    assert names == {"echo", "get_time", "admin_reset_key"}
    for tool in body["result"]["tools"]:
        assert "inputSchema" in tool
        assert "description" in tool


def test_tools_call_normal_tool_echo():
    resp = client.post(
        "/",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "echo", "arguments": {"text": "hello"}},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == 2
    assert body["result"]["content"][0]["text"] == "hello"


def test_tools_call_admin_tool_executes_without_auth_check():
    # The mock has no concept of roles; that's the gateway's job (Task 2).
    resp = client.post(
        "/",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "admin_reset_key", "arguments": {"account_id": "acct-1"}},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "acct-1" in body["result"]["content"][0]["text"]


def test_tools_call_unknown_tool_returns_invalid_params_error():
    resp = client.post(
        "/",
        json={"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope"}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["error"]["code"] == INVALID_PARAMS


def test_unknown_method_returns_method_not_found():
    resp = client.post("/", json={"jsonrpc": "2.0", "id": 5, "method": "not/a/method"})
    assert resp.status_code == 200
    assert resp.json()["error"]["code"] == METHOD_NOT_FOUND


def test_malformed_request_missing_jsonrpc_field_returns_invalid_request():
    resp = client.post("/", json={"id": 6, "method": "tools/list"})
    assert resp.status_code == 200
    assert resp.json()["error"]["code"] == INVALID_REQUEST


def test_notification_without_id_gets_no_response_body():
    resp = client.post("/", json={"jsonrpc": "2.0", "method": "tools/list"})
    assert resp.status_code == 204
    assert resp.content == b""


def test_batch_request_returns_array_of_responses():
    resp = client.post(
        "/",
        json=[
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": {"text": "hi"}},
            },
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 2
    assert {r["id"] for r in body} == {1, 2}


def test_batch_of_only_notifications_returns_no_content():
    resp = client.post(
        "/",
        json=[
            {"jsonrpc": "2.0", "method": "tools/list"},
            {"jsonrpc": "2.0", "method": "tools/list"},
        ],
    )
    assert resp.status_code == 204
    assert resp.content == b""


def test_batch_mixing_calls_and_notifications_only_returns_call_responses():
    resp = client.post(
        "/",
        json=[
            {"jsonrpc": "2.0", "method": "tools/list"},  # notification, no response
            {"jsonrpc": "2.0", "id": 9, "method": "tools/list"},
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["id"] == 9


def test_empty_batch_is_invalid_request():
    resp = client.post("/", json=[])
    assert resp.status_code == 200
    assert resp.json()["error"]["code"] == INVALID_REQUEST
