"""Thin HTTP client for portal.gradmap.com/api.

Every call here either logs in (to bootstrap the OAuth bridge) or forwards a
student's bearer token straight through. Nothing in this module talks to
Postgres, decodes a JWT to decide trust, or holds any auth logic of its own --
GradMap's API is the only place that happens.
"""

import os

import httpx

# Login always goes to the real GradMap API -- it's the only place with real
# student accounts. GRADMAP_API_BASE_URL is where tool calls (add_award, etc.)
# go; point it at a local main.py during development, since GradMap doesn't
# have endpoints like /students/{id}/honors yet. In production both should be
# the same https://portal.gradmap.com/api.
GRADMAP_AUTH_BASE_URL = os.environ.get("GRADMAP_AUTH_BASE_URL", "https://portal.gradmap.com/api")
GRADMAP_API_BASE_URL = os.environ.get("GRADMAP_API_BASE_URL", "https://portal.gradmap.com/api")

_LOGIN_TIMEOUT = 15.0
# Some endpoints (e.g. recommendations) do several sequential fresh Postgres
# connections plus a live LLM call -- comfortably over 15s. Generous on
# purpose; a real hang should surface as a client-side error eventually, not
# hang forever.
_REQUEST_TIMEOUT = 90.0


class GradMapAPIError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"GradMap API error {status_code}: {detail}")


async def gradmap_login(email: str, password: str) -> dict:
    """POST /auth/login against the real GradMap API. Returns the raw JSON body (contains access_token)."""
    async with httpx.AsyncClient(base_url=GRADMAP_AUTH_BASE_URL, timeout=_LOGIN_TIMEOUT) as client:
        response = await client.post(
            "/auth/login",
            json={"email": email, "password": password, "force": True},
        )
    if response.status_code >= 400:
        raise GradMapAPIError(response.status_code, response.text)
    return response.json()


async def gradmap_request(method: str, path: str, access_token: str, **kwargs) -> dict:
    """Forward a tool call to GRADMAP_API_BASE_URL with the student's bearer token attached."""
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {access_token}"
    async with httpx.AsyncClient(base_url=GRADMAP_API_BASE_URL, timeout=_REQUEST_TIMEOUT) as client:
        response = await client.request(method, path, headers=headers, **kwargs)
    if response.status_code >= 400:
        raise GradMapAPIError(response.status_code, response.text)
    if not response.content:
        return {}
    return response.json()
