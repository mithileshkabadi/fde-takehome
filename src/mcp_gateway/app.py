"""Task 2: MCP security gateway — a JSON-RPC reverse proxy in front of a
downstream MCP server, enforcing role-based tool-call authorization.

Rule (from the spec): `tools/list` forwards transparently; a `tools/call`
whose `params.name` starts with `admin_` requires the caller's Bearer-token
role to be `admin`, else the gateway intercepts it and returns JSON-RPC
error `-32001 Unauthorized Tool Call` *without* contacting the downstream
server. Generalized here as: every method is forwarded transparently except
an unauthorized `admin_*` tool call, which is intercepted locally. That's a
deliberate reading beyond the two named methods — a real MCP session opens
with `initialize`/`ping` before ever calling a tool, and blocking those
because they're not `tools/list` would break every client. `tools/list` is
just the spec's example of the (many) methods that always pass through.

Batches are supported (the downstream mock does, so the gateway should too):
each item in a batch is authorized independently, only the authorized subset
is forwarded downstream in one call, and responses are merged back in the
original order — so one unauthorized call in a batch of ten doesn't block
the other nine, and doesn't reach the downstream server either.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response

from mcp_gateway.auth import extract_role

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s mcp_gateway: %(message)s",
)
logger = logging.getLogger("mcp_gateway")

DOWNSTREAM_URL = os.environ.get("MCP_DOWNSTREAM_URL", "http://127.0.0.1:9001/")

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
INTERNAL_ERROR = -32603
UNAUTHORIZED_TOOL_CALL = -32001

app = FastAPI(title="mcp-security-gateway")

_client: httpx.AsyncClient | None = None


def get_downstream_client() -> httpx.AsyncClient:
    """Lazily-created singleton `httpx.AsyncClient` for the process lifetime.

    Tests replace this via `app.dependency_overrides` to point at an
    in-process mock instead of a real network call.
    """
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=DOWNSTREAM_URL, timeout=10.0)
    return _client


def _requires_admin(item: dict[str, Any]) -> bool:
    if item.get("method") != "tools/call":
        return False
    params = item.get("params") or {}
    name = params.get("name")
    return isinstance(name, str) and name.startswith("admin_")


def _unauthorized_error(item: dict[str, Any]) -> dict[str, Any] | None:
    if "id" not in item:
        return None  # notifications never get a response, authorized or not
    return {
        "jsonrpc": "2.0",
        "id": item["id"],
        "error": {"code": UNAUTHORIZED_TOOL_CALL, "message": "Unauthorized Tool Call"},
    }


def _classify(item: Any, role: str | None) -> tuple[str, dict[str, Any] | None]:
    """Decide what to do with one JSON-RPC item: ("forward", None) or
    ("local", response_or_None). `response` is None only for a notification
    that owes no reply."""
    if not isinstance(item, dict):
        return "local", {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": INVALID_REQUEST, "message": "Invalid Request"},
        }

    is_notification = "id" not in item
    if item.get("jsonrpc") != "2.0" or "method" not in item:
        if is_notification:
            return "local", None
        return "local", {
            "jsonrpc": "2.0",
            "id": item.get("id"),
            "error": {"code": INVALID_REQUEST, "message": "Invalid Request"},
        }

    if not _requires_admin(item) or role == "admin":
        return "forward", None

    name = (item.get("params") or {}).get("name")
    logger.info("blocked unauthorized tools/call to %r (role=%r)", name, role)
    return "local", _unauthorized_error(item)


async def _forward(client: httpx.AsyncClient, items: list[dict[str, Any]]) -> Any:
    """POST already-authorized item(s) downstream. Returns whatever the
    downstream returned: a dict, a list, or None (204 / empty body)."""
    payload: Any = items[0] if len(items) == 1 else items
    try:
        resp = await client.post("/", json=payload)
    except httpx.HTTPError as exc:
        logger.error("downstream request failed: %s", exc)
        error = {"code": INTERNAL_ERROR, "message": "Downstream MCP server unreachable"}
        return [
            {"jsonrpc": "2.0", "id": item["id"], "error": error} for item in items if "id" in item
        ]
    if resp.status_code == 204 or not resp.content:
        return None
    return resp.json()


async def _handle_items(
    items: list[Any], *, is_batch: bool, role: str | None, client: httpx.AsyncClient
) -> Response:
    to_forward: list[dict[str, Any]] = []
    local_by_identity: dict[int, dict[str, Any] | None] = {}

    for item in items:
        action, response = _classify(item, role)
        if action == "forward":
            to_forward.append(item)
        else:
            local_by_identity[id(item)] = response

    downstream_by_id: dict[Any, dict[str, Any]] = {}
    if to_forward:
        result = await _forward(client, to_forward)
        if isinstance(result, list):
            downstream_by_id = {r.get("id"): r for r in result}
        elif isinstance(result, dict):
            downstream_by_id = {result.get("id"): result}

    output: list[dict[str, Any]] = []
    for item in items:
        if id(item) in local_by_identity:
            response = local_by_identity[id(item)]
            if response is not None:
                output.append(response)
        elif isinstance(item, dict) and "id" in item:
            response = downstream_by_id.get(item["id"])
            if response is not None:
                output.append(response)

    if not output:
        return Response(status_code=204)
    if not is_batch:
        return JSONResponse(output[0])
    return JSONResponse(output)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/")
async def rpc(
    request: Request, client: httpx.AsyncClient = Depends(get_downstream_client)
) -> Response:
    role = extract_role(request.headers.get("authorization"))

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": PARSE_ERROR, "message": "Parse error"}}
        )

    if isinstance(body, list):
        if not body:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": INVALID_REQUEST, "message": "Empty batch array"},
                }
            )
        return await _handle_items(body, is_batch=True, role=role, client=client)

    return await _handle_items([body], is_batch=False, role=role, client=client)
