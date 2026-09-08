"""Task 1: MCP server exposing `get_customer_record` and `trigger_refund`.

Built on the low-level `mcp.server.lowlevel.Server`, deliberately not the
high-level `mcp.server.mcpserver.MCPServer`. The high-level server treats a
tool-argument schema failure as a *tool execution* error: it returns a
successful `CallToolResult(is_error=True)` (the SDK's own `ToolError` /
`ValidationError` handling in `_handle_call_tool` never lets those exceptions
reach the JSON-RPC layer). The assessment spec asks for the opposite:
"Reject invalid formats with standard MCP JSON-RPC error codes" — a
protocol-level `error` object, not an in-band result. The low-level `Server`
has no such interception: an `MCPError` raised from `on_call_tool` propagates
to `handler_exception_to_error_data` in the JSON-RPC dispatcher and is
serialized as a real JSON-RPC error response, which is what we want for
malformed input.

A *valid but unfulfillable* request (well-formatted customer_id that isn't in
the fixture store) is the opposite case: the request itself was fine, so
that's reported as `CallToolResult(is_error=True)`, matching current MCP
convention for tool execution failures. Only schema-invalid input and unknown
tool names use protocol-level `MCPError`.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from typing import Any

import mcp_types as types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel, ValidationError

from mcp_server.models import GetCustomerRecordInput, TriggerRefundInput

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s mcp_server: %(message)s",
)
logger = logging.getLogger("mcp_server")

# Fake in-memory customer store; there is no real backend for this assessment.
CUSTOMERS: dict[str, dict[str, Any]] = {
    "CUST-00001": {"name": "Ada Lovelace", "email": "ada@example.com", "status": "active"},
    "CUST-00002": {"name": "Grace Hopper", "email": "grace@example.com", "status": "active"},
}

TOOLS: dict[str, dict[str, Any]] = {
    "get_customer_record": {
        "model": GetCustomerRecordInput,
        "description": "Look up a customer record by customer ID (format CUST-XXXXX).",
    },
    "trigger_refund": {
        "model": TriggerRefundInput,
        "description": "Trigger a refund for a customer.",
    },
}


def _text_result(text: str, *, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)], is_error=is_error
    )


def _validate(model_cls: type[BaseModel], tool_name: str, arguments: dict[str, Any]) -> BaseModel:
    try:
        return model_cls.model_validate(arguments)
    except ValidationError as exc:
        errors = [
            {"field": ".".join(str(part) for part in err["loc"]), "message": err["msg"]}
            for err in exc.errors(include_url=False, include_context=False)
        ]
        raise MCPError(
            code=types.INVALID_PARAMS,
            message=f"Invalid arguments for tool {tool_name!r}",
            data={"errors": errors},
        ) from exc


def _get_customer_record(params: GetCustomerRecordInput) -> types.CallToolResult:
    record = CUSTOMERS.get(params.customer_id)
    if record is None:
        logger.info("get_customer_record: %s not found", params.customer_id)
        return _text_result(f"Customer {params.customer_id} not found.", is_error=True)
    payload = {"customer_id": params.customer_id, **record}
    return _text_result(json.dumps(payload))


def _trigger_refund(params: TriggerRefundInput) -> types.CallToolResult:
    if params.customer_id not in CUSTOMERS:
        logger.info("trigger_refund: %s not found", params.customer_id)
        return _text_result(f"Customer {params.customer_id} not found.", is_error=True)
    refund_id = f"REF-{uuid.uuid4().hex[:8]}"
    payload = {
        "refund_id": refund_id,
        "customer_id": params.customer_id,
        "amount": params.amount,
        "reason": params.reason,
        "status": "processed",
    }
    logger.info("trigger_refund: processed %s for %s", refund_id, params.customer_id)
    return _text_result(json.dumps(payload))


async def handle_list_tools(
    ctx: ServerRequestContext[None], params: types.PaginatedRequestParams | None
) -> types.ListToolsResult:
    tools = [
        types.Tool(
            name=name,
            description=spec["description"],
            inputSchema=spec["model"].model_json_schema(),
        )
        for name, spec in TOOLS.items()
    ]
    return types.ListToolsResult(tools=tools)


async def handle_call_tool(
    ctx: ServerRequestContext[None], params: types.CallToolRequestParams
) -> types.CallToolResult:
    name = params.name
    arguments = params.arguments or {}

    spec = TOOLS.get(name)
    if spec is None:
        raise MCPError(code=types.INVALID_PARAMS, message=f"Unknown tool: {name!r}")

    validated = _validate(spec["model"], name, arguments)

    if name == "get_customer_record":
        assert isinstance(validated, GetCustomerRecordInput)
        return _get_customer_record(validated)
    if name == "trigger_refund":
        assert isinstance(validated, TriggerRefundInput)
        return _trigger_refund(validated)
    raise AssertionError(f"unreachable: no handler wired for tool {name!r}")  # pragma: no cover


server: Server[None] = Server(
    "fde-takehome-customer-mcp-server",
    version="0.1.0",
    on_list_tools=handle_list_tools,
    on_call_tool=handle_call_tool,
)
