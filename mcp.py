import base64
import json

import requests

BASE_URL = "https://portal.gradmap.com/api"

# --- Hardcoded for a quick local test only. Do NOT commit this file with real creds. ---
EMAIL = "XXXXXX"
PASSWORD = "XXXXXX"


def login(email: str, password: str) -> str:
    """POST credentials, return the access_token. Raises on failure."""
    resp = requests.post(
        f"{BASE_URL}/auth/login",
        json={"email": email, "password": password, "force": True},  # force: kick any other active session
        headers={"Content-Type": "application/json"},
    )
    if not resp.ok:
        print("Status:", resp.status_code)
        print("Headers:", resp.headers)
        print("Body:", resp.text)
    resp.raise_for_status()  # will raise if not 2xx
    data = resp.json()
    return data["access_token"]


def get_student_id(access_token: str) -> str:
    """Decode the id claim straight out of the login JWT (no extra request needed)."""
    payload_b64 = access_token.split(".")[1]
    payload_b64 += "=" * (-len(payload_b64) % 4)  # restore stripped base64 padding
    claims = json.loads(base64.urlsafe_b64decode(payload_b64))
    return claims["id"]


token = login(EMAIL, PASSWORD)
student_id = get_student_id(token)
print(student_id)
