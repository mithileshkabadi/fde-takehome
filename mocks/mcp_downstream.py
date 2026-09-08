"""Mock downstream MCP server: HTTP JSON-RPC 2.0, exposing tools/list and
tools/call. Used as the thing the Task 2 security gateway proxies to.

Tools exposed:
  - echo            (normal)  — echoes the given text back.
  - get_time         (normal)  — returns a fixed fake timestamp.
  - admin_reset_key  (admin)   — pretends to rotate an API key.

The gateway is responsible for authorization; this mock executes whatever
tool it's asked to run and has no concept of roles.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s mcp_downstream: %(message)s",
)
logger = logging.getLogger("mcp_downstream")

app = FastAPI(title="mock-mcp-downstream")

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

TOOLS: dict[str, dict[str, Any]] = {
    "echo": {
        "description": "Echo back the provided text.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    "get_time": {
        "description": "Return a fixed fake server time.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    "admin_reset_key": {
        "description": "Rotate the API key for the given account (admin only).",
        "inputSchema": {
            "type": "object",
            "properties": {"account_id": {"type": "string"}},
            "required": ["account_id"],
        },
    },
}


def _error(id_: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _result(id_: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _text_result(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "echo":
        return _text_result(str(arguments.get("text", "")))
    if name == "get_time":
        return _text_result("2026-09-04T00:00:00Z")
    if name == "admin_reset_key":
        account_id = arguments.get("account_id", "unknown")
        return _text_result(f"API key rotated for account {account_id}")
    raise KeyError(name)


def handle_single(req: Any) -> dict[str, Any] | None:
    """Process one JSON-RPC request object. Returns None for notifications
    (no 'id'), per JSON-RPC 2.0 — the caller must not send a response."""
    if not isinstance(req, dict):
        return _error(None, INVALID_REQUEST, "Request must be a JSON object")

    id_ = req.get("id", None)
    is_notification = "id" not in req

    if req.get("jsonrpc") != "2.0" or "method" not in req:
        err = _error(id_, INVALID_REQUEST, "Invalid Request")
        return None if is_notification else err

    method = req["method"]
    params = req.get("params") or {}

    if method == "tools/list":
        tools = [{"name": name, **spec} for name, spec in TOOLS.items()]
        result = _result(id_, {"tools": tools})
        return None if is_notification else result

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not name or name not in TOOLS:
            err = _error(id_, INVALID_PARAMS, f"Unknown tool: {name!r}")
            return None if is_notification else err
        try:
            result = _call_tool(name, arguments)
        except KeyError:
            err = _error(id_, INVALID_PARAMS, f"Unknown tool: {name!r}")
            return None if is_notification else err
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("tool execution failed")
            err = _error(id_, INTERNAL_ERROR, str(exc))
            return None if is_notification else err
        response = _result(id_, result)
        return None if is_notification else response

    err = _error(id_, METHOD_NOT_FOUND, f"Method not found: {method!r}")
    return None if is_notification else err


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/")
async def rpc(request: Request) -> Response:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(_error(None, PARSE_ERROR, "Parse error"), status_code=200)

    if isinstance(body, list):
        if not body:
            return JSONResponse(_error(None, INVALID_REQUEST, "Empty batch array"), status_code=200)
        responses = [r for r in (handle_single(item) for item in body) if r is not None]
        if not responses:
            return Response(status_code=204)
        return JSONResponse(responses, status_code=200)

    response = handle_single(body)
    if response is None:
        return Response(status_code=204)
    return JSONResponse(response, status_code=200)
