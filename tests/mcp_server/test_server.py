import asyncio
import json

import mcp_types as types
import pytest
from mcp.shared.exceptions import MCPError

from mcp_server.server import handle_call_tool, handle_list_tools

# The handlers under test never touch `ctx`, so a placeholder is fine here.
CTX = None


def _call(name: str, arguments: dict) -> types.CallToolResult:
    params = types.CallToolRequestParams(name=name, arguments=arguments)
    return asyncio.run(handle_call_tool(CTX, params))


def test_list_tools_exposes_both_tools_with_json_schema():
    result = asyncio.run(handle_list_tools(CTX, None))
    names = {tool.name for tool in result.tools}
    assert names == {"get_customer_record", "trigger_refund"}
    for tool in result.tools:
        assert tool.input_schema["type"] == "object"
        assert tool.input_schema["additionalProperties"] is False


def test_get_customer_record_returns_known_customer():
    result = _call("get_customer_record", {"customer_id": "CUST-00001"})
    assert result.is_error is not True
    payload = json.loads(result.content[0].text)
    assert payload["customer_id"] == "CUST-00001"
    assert payload["name"] == "Ada Lovelace"


def test_get_customer_record_unknown_customer_is_a_tool_result_error():
    # Well-formed but nonexistent: a tool execution failure (is_error), not a
    # JSON-RPC protocol error, since the request itself was valid.
    result = _call("get_customer_record", {"customer_id": "CUST-99999"})
    assert result.is_error is True
    assert "not found" in result.content[0].text


def test_get_customer_record_malformed_id_raises_protocol_level_invalid_params():
    with pytest.raises(MCPError) as exc_info:
        _call("get_customer_record", {"customer_id": "not-valid"})
    assert exc_info.value.code == types.INVALID_PARAMS


def test_get_customer_record_missing_field_raises_invalid_params():
    with pytest.raises(MCPError) as exc_info:
        _call("get_customer_record", {})
    assert exc_info.value.code == types.INVALID_PARAMS


def test_trigger_refund_succeeds_for_known_customer():
    result = _call(
        "trigger_refund",
        {"customer_id": "CUST-00002", "amount": 25.5, "reason": "Duplicate charge on card"},
    )
    assert result.is_error is not True
    payload = json.loads(result.content[0].text)
    assert payload["status"] == "processed"
    assert payload["amount"] == 25.5
    assert payload["refund_id"].startswith("REF-")


def test_trigger_refund_unknown_customer_is_a_tool_result_error():
    result = _call(
        "trigger_refund",
        {"customer_id": "CUST-99999", "amount": 25.5, "reason": "Duplicate charge on card"},
    )
    assert result.is_error is True


def test_trigger_refund_negative_amount_raises_invalid_params():
    with pytest.raises(MCPError) as exc_info:
        _call(
            "trigger_refund",
            {"customer_id": "CUST-00001", "amount": -5, "reason": "Duplicate charge on card"},
        )
    assert exc_info.value.code == types.INVALID_PARAMS


def test_trigger_refund_short_reason_raises_invalid_params():
    with pytest.raises(MCPError) as exc_info:
        _call(
            "trigger_refund",
            {"customer_id": "CUST-00001", "amount": 5, "reason": "too short"},
        )
    assert exc_info.value.code == types.INVALID_PARAMS


def test_unknown_tool_name_raises_invalid_params():
    with pytest.raises(MCPError) as exc_info:
        _call("delete_everything", {})
    assert exc_info.value.code == types.INVALID_PARAMS
