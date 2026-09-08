"""Strict Pydantic input schemas for the customer-support MCP tools.

`extra="forbid"` on every model so unexpected fields are rejected (not
silently dropped), and every constraint (regex, bounds, length) is expressed
as a `Field` so it round-trips into an accurate JSON Schema for `tools/list`
via `model_json_schema()`.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

CUSTOMER_ID_PATTERN = r"^CUST-\d{5}$"


class GetCustomerRecordInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: str = Field(
        ...,
        pattern=CUSTOMER_ID_PATTERN,
        description="Customer ID formatted as CUST-XXXXX (5 digits).",
    )


class TriggerRefundInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: str = Field(
        ...,
        pattern=CUSTOMER_ID_PATTERN,
        description="Customer ID formatted as CUST-XXXXX (5 digits).",
    )
    amount: float = Field(..., gt=0, description="Refund amount; must be a positive number.")
    reason: str = Field(
        ..., min_length=10, description="Reason for the refund (minimum 10 characters)."
    )
