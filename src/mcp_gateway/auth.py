"""Bearer-token to role mapping for the MCP security gateway.

No real identity provider for this assessment: tokens map to roles through a
static lookup table. Swap `TOKEN_ROLES` for a real verifier (JWT, an auth
service, a database lookup...) without touching the authorization logic in
`app.py`, which only ever sees the resolved role string.
"""

from __future__ import annotations

TOKEN_ROLES: dict[str, str] = {
    "admin-token": "admin",
    "viewer-token": "viewer",
}


def extract_role(authorization_header: str | None) -> str | None:
    """Return the role for a `Bearer <token>` header, or None if missing,
    malformed, or the token isn't recognized."""
    if not authorization_header:
        return None
    scheme, _, token = authorization_header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return TOKEN_ROLES.get(token)
