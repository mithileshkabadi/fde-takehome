import pytest

from mcp_gateway.auth import extract_role


@pytest.mark.parametrize(
    ("header", "expected_role"),
    [
        ("Bearer admin-token", "admin"),
        ("Bearer viewer-token", "viewer"),
        ("Bearer bogus-token", None),
        (None, None),
        ("", None),
        ("Bearer", None),  # scheme with no token
        ("Basic admin-token", None),  # wrong scheme
        ("bearer admin-token", "admin"),  # scheme is case-insensitive
    ],
)
def test_extract_role(header, expected_role):
    assert extract_role(header) == expected_role
