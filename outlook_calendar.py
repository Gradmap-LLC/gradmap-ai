"""Outlook / Microsoft 365 calendar integration: OAuth connect/refresh/disconnect
for a student, plus one-way sync (GradMap -> Outlook) of hard deadlines, target
dates, and the student's own calendar events onto a dedicated "GradMap Tasks"
calendar we create in their account, via the Microsoft Graph API.

Mirrors google_calendar.py. Nothing here reads changes back from Outlook --
sync_events() always treats GradMap's current state as truth and reconciles
Outlook to match it.
"""

import os
import secrets
import time
from datetime import date as date_type, datetime, timedelta, timezone
from urllib.parse import urlencode

import psycopg
import requests
from psycopg.rows import dict_row

SCHOOLS_DB_CONFIG = {
    "host": os.environ["GM_DB_HOST"],
    "port": int(os.environ["GM_DB_PORT"]),
    "user": os.environ["GM_DB_USER"],
    "password": os.environ["GM_DB_PASSWORD"],
    "dbname": os.environ["GM_DB_SCHOOLS_NAME"],
}

# Read with .get() rather than os.environ[...] (unlike Google) so the API still
# boots before the Azure app registration exists; connecting just fails with a
# clear error until these are set.
OUTLOOK_CLIENT_ID = os.environ.get("OUTLOOK_CLIENT_ID")
OUTLOOK_CLIENT_SECRET = os.environ.get("OUTLOOK_CLIENT_SECRET")
OUTLOOK_REDIRECT_URI = os.environ.get("OUTLOOK_REDIRECT_URI")

# "common" accepts both personal (Outlook.com/Hotmail) and work/school accounts.
_AUTHORITY = "https://login.microsoftonline.com/common/oauth2/v2.0"
OUTLOOK_AUTHORIZE_URL = f"{_AUTHORITY}/authorize"
OUTLOOK_TOKEN_URL = f"{_AUTHORITY}/token"
GRAPH_API = "https://graph.microsoft.com/v1.0"
# offline_access is what makes Microsoft return a refresh_token at all.
OUTLOOK_SCOPES = "offline_access User.Read Calendars.ReadWrite"
GRADMAP_CALENDAR_NAME = "GradMap Tasks"

_REQUEST_TIMEOUT = 15

ALLOWED_SOURCE_TYPES = ("hard_deadline", "target_date", "own_event")


class OutlookNotConnectedError(Exception):
    def __init__(self, student_id):
        super().__init__(f"Student {student_id} has not connected Outlook Calendar")


class OutlookNotConfiguredError(Exception):
    def __init__(self):
        super().__init__("OUTLOOK_CLIENT_ID / OUTLOOK_CLIENT_SECRET / OUTLOOK_REDIRECT_URI are not set")


def _require_config():
    if not (OUTLOOK_CLIENT_ID and OUTLOOK_CLIENT_SECRET and OUTLOOK_REDIRECT_URI):
        raise OutlookNotConfiguredError()


# --- pending OAuth flows -----------------------------------------------------
# In-memory and per-process, same trade-off as google_calendar.py: flow_id is
# the OAuth `state`, letting the fixed-URL callback recover which student is
# completing the flow. Single-use so a replayed callback can't hijack a connection.
_PENDING_FLOW_TTL_SECONDS = 10 * 60
_pending_flows: dict[str, dict] = {}


def create_pending_flow(student_id: str) -> str:
    flow_id = secrets.token_urlsafe(24)
    _pending_flows[flow_id] = {"student_id": student_id, "expires_at": time.time() + _PENDING_FLOW_TTL_SECONDS}
    return flow_id


def pop_pending_flow(flow_id: str) -> str | None:
    pending = _pending_flows.pop(flow_id, None)
    if pending is None or pending["expires_at"] < time.time():
        return None
    return pending["student_id"]


def build_authorize_url(flow_id: str) -> str:
    _require_config()
    params = {
        "client_id": OUTLOOK_CLIENT_ID,
        "redirect_uri": OUTLOOK_REDIRECT_URI,
        "response_type": "code",
        "response_mode": "query",
        "scope": OUTLOOK_SCOPES,
        "prompt": "select_account",
        "state": flow_id,
    }
    return f"{OUTLOOK_AUTHORIZE_URL}?{urlencode(params)}"


# --- token exchange -----------------------------------------------------------

def _exchange_code_for_tokens(code: str) -> dict:
    _require_config()
    response = requests.post(
        OUTLOOK_TOKEN_URL,
        data={
            "code": code,
            "client_id": OUTLOOK_CLIENT_ID,
            "client_secret": OUTLOOK_CLIENT_SECRET,
            "redirect_uri": OUTLOOK_REDIRECT_URI,
            "grant_type": "authorization_code",
            "scope": OUTLOOK_SCOPES,
        },
        timeout=_REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def _refresh_access_token(refresh_token: str) -> dict:
    _require_config()
    response = requests.post(
        OUTLOOK_TOKEN_URL,
        data={
            "client_id": OUTLOOK_CLIENT_ID,
            "client_secret": OUTLOOK_CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
            "scope": OUTLOOK_SCOPES,
        },
        timeout=_REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def _find_or_create_gradmap_calendar(access_token: str) -> str:
    headers = {"Authorization": f"Bearer {access_token}"}
    url = f"{GRAPH_API}/me/calendars?$top=100"
    while url:
        list_response = requests.get(url, headers=headers, timeout=_REQUEST_TIMEOUT)
        list_response.raise_for_status()
        payload = list_response.json()
        for entry in payload.get("value", []):
            if entry.get("name") == GRADMAP_CALENDAR_NAME:
                return entry["id"]
        url = payload.get("@odata.nextLink")

    create_response = requests.post(
        f"{GRAPH_API}/me/calendars",
        headers=headers,
        json={"name": GRADMAP_CALENDAR_NAME},
        timeout=_REQUEST_TIMEOUT,
    )
    create_response.raise_for_status()
    return create_response.json()["id"]


# --- table setup ---------------------------------------------------------

CREATE_OUTLOOK_CALENDAR_CONNECTIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS outlook_calendar_connections (
    student_id INTEGER PRIMARY KEY,
    access_token TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    token_expiry TIMESTAMPTZ NOT NULL,
    calendar_id TEXT NOT NULL,
    connected_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

CREATE_OUTLOOK_CALENDAR_EVENTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS outlook_calendar_events (
    id SERIAL PRIMARY KEY,
    student_id INTEGER NOT NULL,
    source_type TEXT NOT NULL CHECK (source_type IN ('hard_deadline', 'target_date', 'own_event')),
    source_id TEXT NOT NULL,
    outlook_event_id TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (student_id, source_type, source_id)
)
"""


def ensure_outlook_calendar_tables():
    with psycopg.connect(**SCHOOLS_DB_CONFIG) as connection:
        with connection.cursor() as cursor:
            cursor.execute(CREATE_OUTLOOK_CALENDAR_CONNECTIONS_TABLE_SQL)
            cursor.execute(CREATE_OUTLOOK_CALENDAR_EVENTS_TABLE_SQL)


# --- connection storage ----------------------------------------------------

UPSERT_CONNECTION_SQL = """
INSERT INTO outlook_calendar_connections (student_id, access_token, refresh_token, token_expiry, calendar_id)
VALUES (%(student_id)s, %(access_token)s, %(refresh_token)s, %(token_expiry)s, %(calendar_id)s)
ON CONFLICT (student_id) DO UPDATE SET
    access_token = EXCLUDED.access_token,
    refresh_token = EXCLUDED.refresh_token,
    token_expiry = EXCLUDED.token_expiry,
    calendar_id = EXCLUDED.calendar_id
"""

# Unlike Google, Microsoft rotates the refresh token on every refresh: the old
# one is soon invalid, so the new one must be saved alongside the access token.
UPDATE_TOKENS_SQL = """
UPDATE outlook_calendar_connections
SET access_token = %(access_token)s, refresh_token = %(refresh_token)s, token_expiry = %(token_expiry)s
WHERE student_id = %(student_id)s
"""

GET_CONNECTION_SQL = """
SELECT student_id, access_token, refresh_token, token_expiry, calendar_id
FROM outlook_calendar_connections
WHERE student_id = %s
"""

DELETE_CONNECTION_SQL = "DELETE FROM outlook_calendar_connections WHERE student_id = %s"
DELETE_EVENTS_FOR_STUDENT_SQL = "DELETE FROM outlook_calendar_events WHERE student_id = %s"


def get_connection(student_id):
    ensure_outlook_calendar_tables()
    with psycopg.connect(**SCHOOLS_DB_CONFIG, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(GET_CONNECTION_SQL, (student_id,))
            return cursor.fetchone()


def is_connected(student_id) -> bool:
    return get_connection(student_id) is not None


def _save_connection(student_id, tokens, calendar_id):
    expiry = datetime.now(timezone.utc) + timedelta(seconds=tokens["expires_in"])
    with psycopg.connect(**SCHOOLS_DB_CONFIG) as connection:
        with connection.cursor() as cursor:
            cursor.execute(UPSERT_CONNECTION_SQL, {
                "student_id": student_id,
                "access_token": tokens["access_token"],
                "refresh_token": tokens["refresh_token"],
                "token_expiry": expiry,
                "calendar_id": calendar_id,
            })


def complete_connection(student_id: str, code: str) -> None:
    """Finishes the OAuth flow: exchanges the code, finds or creates this
    student's GradMap calendar, and stores the connection."""
    ensure_outlook_calendar_tables()
    tokens = _exchange_code_for_tokens(code)
    calendar_id = _find_or_create_gradmap_calendar(tokens["access_token"])
    _save_connection(student_id, tokens, calendar_id)


def disconnect(student_id) -> None:
    """Microsoft has no token-revocation endpoint (students can remove the app
    at myaccount.microsoft.com), so this only forgets the connection locally."""
    if get_connection(student_id) is None:
        return
    with psycopg.connect(**SCHOOLS_DB_CONFIG) as connection:
        with connection.cursor() as cursor:
            cursor.execute(DELETE_EVENTS_FOR_STUDENT_SQL, (student_id,))
            cursor.execute(DELETE_CONNECTION_SQL, (student_id,))


def _get_valid_access_token(student_id) -> tuple[str, str]:
    """Returns (access_token, calendar_id), refreshing the access token first if it's expired or about to be."""
    connection_row = get_connection(student_id)
    if connection_row is None:
        raise OutlookNotConnectedError(student_id)

    expiry = connection_row["token_expiry"]
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if expiry - timedelta(minutes=2) > datetime.now(timezone.utc):
        return connection_row["access_token"], connection_row["calendar_id"]

    tokens = _refresh_access_token(connection_row["refresh_token"])
    new_expiry = datetime.now(timezone.utc) + timedelta(seconds=tokens["expires_in"])
    with psycopg.connect(**SCHOOLS_DB_CONFIG) as connection:
        with connection.cursor() as cursor:
            cursor.execute(UPDATE_TOKENS_SQL, {
                "student_id": student_id,
                "access_token": tokens["access_token"],
                "refresh_token": tokens.get("refresh_token", connection_row["refresh_token"]),
                "token_expiry": new_expiry,
            })
    return tokens["access_token"], connection_row["calendar_id"]


# --- event sync --------------------------------------------------------------

GET_EVENT_MAPPING_SQL = """
SELECT outlook_event_id FROM outlook_calendar_events
WHERE student_id = %s AND source_type = %s AND source_id = %s
"""

GET_ALL_EVENT_MAPPINGS_FOR_STUDENT_SQL = """
SELECT source_type, source_id, outlook_event_id
FROM outlook_calendar_events
WHERE student_id = %s
"""

UPSERT_EVENT_MAPPING_SQL = """
INSERT INTO outlook_calendar_events (student_id, source_type, source_id, outlook_event_id)
VALUES (%(student_id)s, %(source_type)s, %(source_id)s, %(outlook_event_id)s)
ON CONFLICT (student_id, source_type, source_id) DO UPDATE SET
    outlook_event_id = EXCLUDED.outlook_event_id, updated_at = now()
"""

DELETE_EVENT_MAPPING_SQL = """
DELETE FROM outlook_calendar_events
WHERE student_id = %s AND source_type = %s AND source_id = %s
"""


def _get_event_mapping(student_id, source_type, source_id) -> str | None:
    with psycopg.connect(**SCHOOLS_DB_CONFIG, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(GET_EVENT_MAPPING_SQL, (student_id, source_type, str(source_id)))
            row = cursor.fetchone()
            return row["outlook_event_id"] if row else None


def _build_event_body(title, date, description) -> dict:
    """Graph all-day events run from midnight to midnight of the *next* day
    (end is exclusive), and both must be at 00:00:00."""
    start = date_type.fromisoformat(date)
    end = start + timedelta(days=1)
    return {
        "subject": title,
        "body": {"contentType": "text", "content": description or ""},
        "isAllDay": True,
        "start": {"dateTime": f"{start.isoformat()}T00:00:00", "timeZone": "UTC"},
        "end": {"dateTime": f"{end.isoformat()}T00:00:00", "timeZone": "UTC"},
    }


def upsert_event(student_id, source_type, source_id, title, date, description=None) -> None:
    """date: 'YYYY-MM-DD'. All-day event, since these are dates, not times."""
    if source_type not in ALLOWED_SOURCE_TYPES:
        raise ValueError(f"source_type must be one of {ALLOWED_SOURCE_TYPES}, got {source_type!r}")

    access_token, calendar_id = _get_valid_access_token(student_id)
    headers = {"Authorization": f"Bearer {access_token}"}
    body = _build_event_body(title, date, description)

    outlook_event_id = _get_event_mapping(student_id, source_type, source_id)
    if outlook_event_id:
        response = requests.patch(
            f"{GRAPH_API}/me/events/{outlook_event_id}",
            headers=headers, json=body, timeout=_REQUEST_TIMEOUT,
        )
        if response.status_code == 404:
            # The student (or something else) removed it on the Outlook side; recreate below.
            outlook_event_id = None
        else:
            response.raise_for_status()

    if not outlook_event_id:
        response = requests.post(
            f"{GRAPH_API}/me/calendars/{calendar_id}/events",
            headers=headers, json=body, timeout=_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        with psycopg.connect(**SCHOOLS_DB_CONFIG) as connection:
            with connection.cursor() as cursor:
                cursor.execute(UPSERT_EVENT_MAPPING_SQL, {
                    "student_id": student_id,
                    "source_type": source_type,
                    "source_id": str(source_id),
                    "outlook_event_id": response.json()["id"],
                })


def delete_event(student_id, source_type, source_id) -> None:
    outlook_event_id = _get_event_mapping(student_id, source_type, source_id)
    if outlook_event_id:
        access_token, _ = _get_valid_access_token(student_id)
        headers = {"Authorization": f"Bearer {access_token}"}
        response = requests.delete(
            f"{GRAPH_API}/me/events/{outlook_event_id}",
            headers=headers, timeout=_REQUEST_TIMEOUT,
        )
        if response.status_code not in (200, 204, 404):
            response.raise_for_status()

    with psycopg.connect(**SCHOOLS_DB_CONFIG) as connection:
        with connection.cursor() as cursor:
            cursor.execute(DELETE_EVENT_MAPPING_SQL, (student_id, source_type, str(source_id)))


def sync_events(student_id, items) -> None:
    """items: iterable of dicts with source_type/source_id/title/date/description,
    representing the full current set of calendar-relevant dates for this
    student. This is a full reconciliation, not an incremental append -- any
    previously synced item missing from `items` is deleted from Outlook."""
    ensure_outlook_calendar_tables()
    if not is_connected(student_id):
        raise OutlookNotConnectedError(student_id)

    wanted = {(str(item["source_type"]), str(item["source_id"])) for item in items}

    with psycopg.connect(**SCHOOLS_DB_CONFIG, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(GET_ALL_EVENT_MAPPINGS_FOR_STUDENT_SQL, (student_id,))
            existing_keys = {(row["source_type"], row["source_id"]) for row in cursor.fetchall()}

    for source_type, source_id in existing_keys - wanted:
        delete_event(student_id, source_type, source_id)

    for item in items:
        upsert_event(
            student_id, item["source_type"], item["source_id"],
            item["title"], item["date"], item.get("description"),
        )
