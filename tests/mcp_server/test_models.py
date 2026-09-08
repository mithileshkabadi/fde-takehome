import pytest
from pydantic import ValidationError

from mcp_server.models import GetCustomerRecordInput, TriggerRefundInput

VALID_CUSTOMER_ID = "CUST-12345"


@pytest.mark.parametrize(
    "customer_id",
    [
        "CUST-1234",  # too few digits
        "CUST-123456",  # too many digits
        "cust-12345",  # lowercase prefix
        "CUST12345",  # missing dash
        "CUST-ABCDE",  # non-digits
        "",  # empty
        " CUST-12345",  # leading whitespace
        "CUST-12345 ",  # trailing whitespace
    ],
)
def test_get_customer_record_rejects_malformed_customer_id(customer_id):
    with pytest.raises(ValidationError):
        GetCustomerRecordInput(customer_id=customer_id)


def test_get_customer_record_accepts_well_formed_customer_id():
    model = GetCustomerRecordInput(customer_id=VALID_CUSTOMER_ID)
    assert model.customer_id == VALID_CUSTOMER_ID


def test_get_customer_record_rejects_unknown_extra_fields():
    with pytest.raises(ValidationError):
        GetCustomerRecordInput(customer_id=VALID_CUSTOMER_ID, extra_field="nope")


@pytest.mark.parametrize("amount", [0, -0.01, -100])
def test_trigger_refund_rejects_non_positive_amount(amount):
    with pytest.raises(ValidationError):
        TriggerRefundInput(customer_id=VALID_CUSTOMER_ID, amount=amount, reason="valid reason text")


@pytest.mark.parametrize("reason", ["", "short", "123456789"])  # 0, 5, 9 chars
def test_trigger_refund_rejects_reason_below_minimum_length(reason):
    with pytest.raises(ValidationError):
        TriggerRefundInput(customer_id=VALID_CUSTOMER_ID, amount=10.0, reason=reason)


def test_trigger_refund_accepts_reason_at_exactly_minimum_length():
    model = TriggerRefundInput(customer_id=VALID_CUSTOMER_ID, amount=10.0, reason="1234567890")
    assert len(model.reason) == 10


def test_trigger_refund_rejects_malformed_customer_id():
    with pytest.raises(ValidationError):
        TriggerRefundInput(customer_id="not-a-customer-id", amount=10.0, reason="valid reason text")


def test_trigger_refund_accepts_valid_input():
    model = TriggerRefundInput(
        customer_id=VALID_CUSTOMER_ID, amount=49.99, reason="Item arrived damaged"
    )
    assert model.amount == 49.99
    assert model.customer_id == VALID_CUSTOMER_ID


def test_trigger_refund_rejects_unknown_extra_fields():
    with pytest.raises(ValidationError):
        TriggerRefundInput(
            customer_id=VALID_CUSTOMER_ID, amount=10.0, reason="valid reason text", note="nope"
        )
