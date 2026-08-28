"""Bridges MCP's OAuth authorization-code flow to GradMap's login endpoint.

portal.gradmap.com/api doesn't expose a browser-redirect /authorize endpoint
today, only POST /auth/login (email + password). So this provider plays the
"third-party OAuth server" role described in the MCP SDK's
OAuthAuthorizationServerProvider.authorize docstring: our own /authorize
hands off to a login form we render ourselves, the form submit calls
GradMap's /auth/login server-side (the student's password never reaches
Claude), and on success we mint a short-lived authorization code and redirect
back to the MCP client.

The access token this provider issues *is* GradMap's own access_token,
unchanged -- every tool call forwards it straight through as
`Authorization: Bearer <token>` to portal.gradmap.com/api. GradMap remains
the source of truth for whether that token is valid; this provider never
verifies its signature or claims to decide trust. The one JWT decode below
(`_peel_claims`) is bookkeeping only -- it labels our local cache entry with
a subject (student_id) and an expiry to know when to drop it -- the actual
trust decision already happened when GradMap's API returned 200 from login.
"""

import json
import secrets
import time
from base64 import urlsafe_b64decode
from dataclasses import dataclass

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from gradmap_client import GradMapAPIError, gradmap_login

AUTH_CODE_TTL_SECONDS = 5 * 60
DEFAULT_ACCESS_TOKEN_TTL_SECONDS = 60 * 60  # fallback when the GradMap JWT has no readable `exp`


class GradMapLoginError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class GradMapAuthorizationCode(AuthorizationCode):
    gradmap_access_token: str = ""


@dataclass
class PendingAuthorization:
    client: OAuthClientInformationFull
    params: AuthorizationParams


def _peel_claims(gradmap_access_token: str) -> tuple[str | None, int | None]:
    try:
        payload_b64 = gradmap_access_token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        claims = json.loads(urlsafe_b64decode(payload_b64))
        student_id = claims.get("id")
        return (str(student_id) if student_id is not None else None), claims.get("exp")
    except Exception:
        return None, None


class GradMapOAuthProvider:
    def __init__(self, *, login_path: str = "/gradmap/login"):
        self.login_path = login_path
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._pending: dict[str, PendingAuthorization] = {}
        self._auth_codes: dict[str, GradMapAuthorizationCode] = {}
        self._access_tokens: dict[str, AccessToken] = {}

    # --- dynamic client registration ---------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients[client_info.client_id] = client_info

    # --- authorize: hand off to our own login page --------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        flow_id = secrets.token_urlsafe(24)
        self._pending[flow_id] = PendingAuthorization(client=client, params=params)
        return f"{self.login_path}?flow_id={flow_id}"

    def get_pending(self, flow_id: str) -> PendingAuthorization | None:
        return self._pending.get(flow_id)

    async def complete_login(self, flow_id: str, email: str, password: str) -> str:
        """Called by the login form's POST handler once the student submits credentials.

        Returns the redirect_uri to send the browser back to. Raises
        GradMapLoginError (with the pending flow left intact, so the student
        can retry) on bad credentials or an unknown/expired flow_id.
        """
        pending = self._pending.get(flow_id)
        if pending is None:
            raise GradMapLoginError("This login link has expired. Please reconnect from Claude and try again.")

        try:
            login_response = await gradmap_login(email, password)
        except GradMapAPIError:
            raise GradMapLoginError("Incorrect email or password.")

        gradmap_access_token = login_response["access_token"]
        student_id, _exp = _peel_claims(gradmap_access_token)
        print(f"[gradmap-auth] decoded student_id={student_id!r} from login JWT")

        del self._pending[flow_id]

        code = secrets.token_urlsafe(32)
        self._auth_codes[code] = GradMapAuthorizationCode(
            code=code,
            scopes=pending.params.scopes or [],
            expires_at=time.time() + AUTH_CODE_TTL_SECONDS,
            client_id=pending.client.client_id,
            code_challenge=pending.params.code_challenge,
            redirect_uri=pending.params.redirect_uri,
            redirect_uri_provided_explicitly=pending.params.redirect_uri_provided_explicitly,
            resource=pending.params.resource,
            subject=student_id,
            gradmap_access_token=gradmap_access_token,
        )

        return construct_redirect_uri(str(pending.params.redirect_uri), code=code, state=pending.params.state)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> GradMapAuthorizationCode | None:
        code = self._auth_codes.get(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: GradMapAuthorizationCode
    ) -> OAuthToken:
        self._auth_codes.pop(authorization_code.code, None)

        _student_id, exp = _peel_claims(authorization_code.gradmap_access_token)
        expires_in = max(int(exp - time.time()), 60) if exp else DEFAULT_ACCESS_TOKEN_TTL_SECONDS

        self._access_tokens[authorization_code.gradmap_access_token] = AccessToken(
            token=authorization_code.gradmap_access_token,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=int(time.time()) + expires_in,
            subject=authorization_code.subject,
        )

        return OAuthToken(
            access_token=authorization_code.gradmap_access_token,
            token_type="Bearer",
            expires_in=expires_in,
            scope=" ".join(authorization_code.scopes) if authorization_code.scopes else None,
        )

    # --- refresh: GradMap's login endpoint doesn't hand us a refresh token yet --

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        return None

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        raise TokenError(
            error="unsupported_grant_type",
            error_description="Refresh tokens aren't supported yet -- reconnect through /authorize instead.",
        )

    # --- resource-server side: checked on every tool call --------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        access_token = self._access_tokens.get(token)
        if access_token is None:
            return None
        if access_token.expires_at is not None and access_token.expires_at < time.time():
            del self._access_tokens[token]
            return None
        return access_token

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self._access_tokens.pop(token.token, None)
