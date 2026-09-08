"""End-to-end stdio test: spawns the real `python -m mcp_server` subprocess
and drives it over actual stdin/stdout pipes.

This is the only test that can genuinely verify the "STDIO isolation" eval
criterion — every line read from stdout must parse as JSON-RPC. A unit test
calling handlers directly can't catch a stray `print()` sitting somewhere in
the transport or a dependency; only a real subprocess test can.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
from collections.abc import Iterator

import pytest

PROTOCOL_VERSION = "2026-07-28"
READ_TIMEOUT = 5.0


def _readline_with_timeout(stream, timeout: float = READ_TIMEOUT) -> str:
    q: queue.Queue[str] = queue.Queue(maxsize=1)

    def _reader() -> None:
        q.put(stream.readline())

    threading.Thread(target=_reader, daemon=True).start()
    try:
        line = q.get(timeout=timeout)
    except queue.Empty:
        raise TimeoutError(f"no line read from subprocess stdout within {timeout}s") from None
    if line == "":
        raise EOFError("subprocess stdout closed unexpectedly")
    return line


class _McpProcess:
    def __init__(self, proc: subprocess.Popen[str]):
        self.proc = proc
        self._next_id = 1

    def send_request(self, method: str, params: dict | None = None) -> dict:
        request_id = self._next_id
        self._next_id += 1
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        response = self._read_json()
        assert response.get("id") == request_id, f"id mismatch: {response}"
        return response

    def send_notification(self, method: str, params: dict | None = None) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._write(message)

    def _write(self, message: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def _read_json(self) -> dict:
        assert self.proc.stdout is not None
        line = _readline_with_timeout(self.proc.stdout)
        # Every line on stdout must be pure JSON-RPC: if isolation is broken
        # (a stray print/log line lands on stdout), this parse fails and the
        # test fails — that's the whole point of driving a real subprocess.
        try:
            return json.loads(line)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"non-JSON content on stdout: {line!r}") from exc


@pytest.fixture
def mcp_process() -> Iterator[_McpProcess]:
    proc = subprocess.Popen(
        [sys.executable, "-m", "mcp_server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    client = _McpProcess(proc)
    try:
        init_response = client.send_request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "smoke-test-client", "version": "0.1.0"},
            },
        )
        assert "result" in init_response, init_response
        client.send_notification("notifications/initialized")
        yield client
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def test_initialize_handshake_succeeds(mcp_process: _McpProcess):
    # The fixture itself performs and asserts the handshake; this test just
    # documents that a working connection is the precondition for the rest.
    pass


def test_tools_list_over_real_stdio(mcp_process: _McpProcess):
    response = mcp_process.send_request("tools/list")
    assert "error" not in response, response
    names = {tool["name"] for tool in response["result"]["tools"]}
    assert names == {"get_customer_record", "trigger_refund"}


def test_valid_tool_call_over_real_stdio(mcp_process: _McpProcess):
    response = mcp_process.send_request(
        "tools/call",
        {"name": "get_customer_record", "arguments": {"customer_id": "CUST-00001"}},
    )
    assert "error" not in response, response
    result = response["result"]
    assert result.get("isError") is not True
    payload = json.loads(result["content"][0]["text"])
    assert payload["customer_id"] == "CUST-00001"


def test_malformed_input_returns_jsonrpc_invalid_params_error(mcp_process: _McpProcess):
    response = mcp_process.send_request(
        "tools/call",
        {"name": "get_customer_record", "arguments": {"customer_id": "not-a-valid-id"}},
    )
    assert "result" not in response, response
    assert response["error"]["code"] == -32602


def test_short_refund_reason_returns_jsonrpc_invalid_params_error(mcp_process: _McpProcess):
    response = mcp_process.send_request(
        "tools/call",
        {
            "name": "trigger_refund",
            "arguments": {"customer_id": "CUST-00001", "amount": 10.0, "reason": "too short"},
        },
    )
    assert response["error"]["code"] == -32602


def test_unknown_but_well_formed_customer_is_in_band_tool_error(mcp_process: _McpProcess):
    response = mcp_process.send_request(
        "tools/call",
        {"name": "get_customer_record", "arguments": {"customer_id": "CUST-99999"}},
    )
    assert "error" not in response, response
    assert response["result"]["isError"] is True
