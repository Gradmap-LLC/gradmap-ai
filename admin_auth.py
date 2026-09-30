"""Sample bearer-token verification for the admin page (admin.html).

The admin page lives inside another project where the admin is already
signed in via OAuth; that project hands the page the admin's access token and
the page sends it here as `Authorization: Bearer <token>`. This verifies it
against the OAuth provider's public keys (JWKS), so no shared secret is needed.

Configured entirely by env vars -- unset ADMIN_JWKS_URL and bearer auth is
simply off (the X-Admin-Key / dev-flag paths in main.py's require_admin still
work):
  ADMIN_JWKS_URL        provider's JWKS endpoint (required to enable)
  ADMIN_TOKEN_ISSUER    expected `iss` (optional but recommended)
  ADMIN_TOKEN_AUDIENCE  expected `aud` (optional but recommended)
  ADMIN_ROLE_CLAIM      claim holding the role(s)        (default: role)
  ADMIN_ROLE_VALUE      value that marks an admin        (default: admin)
  ADMIN_NAME_CLAIM      claim used as the audit name     (default: name)

This is a sample: signature + expiry + issuer/audience + one role claim. Adjust
verify_admin_bearer_token to however your provider marks admins.
"""

import os

import jwt

ADMIN_JWKS_URL = os.environ.get("ADMIN_JWKS_URL")
ADMIN_TOKEN_ISSUER = os.environ.get("ADMIN_TOKEN_ISSUER")
ADMIN_TOKEN_AUDIENCE = os.environ.get("ADMIN_TOKEN_AUDIENCE")
ADMIN_ROLE_CLAIM = os.environ.get("ADMIN_ROLE_CLAIM", "role")
ADMIN_ROLE_VALUE = os.environ.get("ADMIN_ROLE_VALUE", "admin")
ADMIN_NAME_CLAIM = os.environ.get("ADMIN_NAME_CLAIM", "name")

_jwks_client = jwt.PyJWKClient(ADMIN_JWKS_URL) if ADMIN_JWKS_URL else None


def bearer_auth_enabled() -> bool:
    return _jwks_client is not None


def verify_admin_bearer_token(token: str):
    """Returns {"id", "admin_name"} for a valid token belonging to an admin,
    else None. `id` is the numeric `sub` when it is one (the audit columns are
    integers) and 0 otherwise -- admin_name is what actually identifies the
    person in the audit trail."""
    if _jwks_client is None or not token:
        return None
    try:
        signing_key = _jwks_client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            issuer=ADMIN_TOKEN_ISSUER,
            audience=ADMIN_TOKEN_AUDIENCE,
            options={"verify_iss": bool(ADMIN_TOKEN_ISSUER), "verify_aud": bool(ADMIN_TOKEN_AUDIENCE)},
        )
    except jwt.PyJWTError:
        return None

    roles = claims.get(ADMIN_ROLE_CLAIM)
    roles = roles if isinstance(roles, list) else [roles]
    if ADMIN_ROLE_VALUE not in roles:
        return None

    sub = str(claims.get("sub", ""))
    return {
        "id": int(sub) if sub.isdigit() else 0,
        "admin_name": str(claims.get(ADMIN_NAME_CLAIM) or claims.get("email") or sub or "Admin"),
    }
