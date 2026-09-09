"""Google Calendar integration: OAuth connect/refresh/disconnect for a student,
plus one-way sync (GradMap -> Google) of hard deadlines, target dates, and the
student's own calendar events onto a dedicated "GradMap Tasks" calendar we
create in their account.

Nothing here reads changes back from Google -- sync_events() always treats
GradMap's current state as truth and reconciles Google to match it.
"""

import os
import secrets
import time
from datetime import datetime, timedelta, timezone
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

GOOGLE_CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
GOOGLE_REDIRECT_URI = os.environ["GOOGLE_REDIRECT_URI"]

GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GOOGLE_CALENDAR_API = "https://www.googleapis.com/calendar/v3"
GOOGLE_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
GRADMAP_CALENDAR_NAME = "GradMap Tasks"

_REQUEST_TIMEOUT = 15

ALLOWED_SOURCE_TYPES = ("hard_deadline", "target_date", "own_event")


class GoogleNotConnectedError(Exception):
    def __init__(self, student_id):
        super().__init__(f"Student {student_id} has not connected Google Calendar")


# --- pending OAuth flows -----------------------------------------------------
# In-memory and per-process, same trade-off as auth_provider.py's PendingAuthorization:
# fine for a single dev/uvicorn-worker deployment, lost on restart. flow_id is
# the OAuth `state` -- it's what lets the callback (which Google redirects to
# with a fixed, pre-registered URL that can't carry a {student_id} segment)
# recover which student is completing the flow, and it's single-use so a
# replayed callback URL can't be reused to hijack a connection.
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
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": GOOGLE_CALENDAR_SCOPE,
        "access_type": "offline",
        "prompt": "consent",  # forces a refresh_token even on a repeat connect
        "include_granted_scopes": "true",
        "state": flow_id,
    }
    return f"{GOOGLE_AUTHORIZE_URL}?{urlencode(params)}"


# --- token exchange -----------------------------------------------------------

def _exchange_code_for_tokens(code: str) -> dict:
    response = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": GOOGLE_REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=_REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def _refresh_access_token(refresh_token: str) -> dict:
    response = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=_REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def _find_or_create_gradmap_calendar(access_token: str) -> str:
    headers = {"Authorization": f"Bearer {access_token}"}
    list_response = requests.get(f"{GOOGLE_CALENDAR_API}/users/me/calendarList", headers=headers, timeout=_REQUEST_TIMEOUT)
    list_response.raise_for_status()
    for entry in list_response.json().get("items", []):
        if entry.get("summary") == GRADMAP_CALENDAR_NAME:
            return entry["id"]

    create_response = requests.post(
        f"{GOOGLE_CALENDAR_API}/calendars",
        headers=headers,
        json={"summary": GRADMAP_CALENDAR_NAME, "description": "Deadlines and target dates from GradMap."},
        timeout=_REQUEST_TIMEOUT,
    )
    create_response.raise_for_status()
    return create_response.json()["id"]


# --- table setup ---------------------------------------------------------

CREATE_GOOGLE_CALENDAR_CONNECTIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS google_calendar_connections (
    student_id INTEGER PRIMARY KEY,
    access_token TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    token_expiry TIMESTAMPTZ NOT NULL,
    calendar_id TEXT NOT NULL,
    connected_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

CREATE_GOOGLE_CALENDAR_EVENTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS google_calendar_events (
    id SERIAL PRIMARY KEY,
    student_id INTEGER NOT NULL,
    source_type TEXT NOT NULL CHECK (source_type IN ('hard_deadline', 'target_date', 'own_event')),
    source_id TEXT NOT NULL,
    google_event_id TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (student_id, source_type, source_id)
)
"""


def ensure_google_calendar_tables():
    with psycopg.connect(**SCHOOLS_DB_CONFIG) as connection:
        with connection.cursor() as cursor:
            cursor.execute(CREATE_GOOGLE_CALENDAR_CONNECTIONS_TABLE_SQL)
            cursor.execute(CREATE_GOOGLE_CALENDAR_EVENTS_TABLE_SQL)


# --- connection storage ----------------------------------------------------

UPSERT_CONNECTION_SQL = """
INSERT INTO google_calendar_connections (student_id, access_token, refresh_token, token_expiry, calendar_id)
VALUES (%(student_id)s, %(access_token)s, %(refresh_token)s, %(token_expiry)s, %(calendar_id)s)
ON CONFLICT (student_id) DO UPDATE SET
    access_token = EXCLUDED.access_token,
    refresh_token = COALESCE(EXCLUDED.refresh_token, google_calendar_connections.refresh_token),
    token_expiry = EXCLUDED.token_expiry,
    calendar_id = EXCLUDED.calendar_id
"""

UPDATE_ACCESS_TOKEN_SQL = """
UPDATE google_calendar_connections
SET access_token = %(access_token)s, token_expiry = %(token_expiry)s
WHERE student_id = %(student_id)s
"""

GET_CONNECTION_SQL = """
SELECT student_id, access_token, refresh_token, token_expiry, calendar_id
FROM google_calendar_connections
WHERE student_id = %s
"""

DELETE_CONNECTION_SQL = "DELETE FROM google_calendar_connections WHERE student_id = %s"
DELETE_EVENTS_FOR_STUDENT_SQL = "DELETE FROM google_calendar_events WHERE student_id = %s"


def get_connection(student_id):
    ensure_google_calendar_tables()
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
                "refresh_token": tokens.get("refresh_token"),
                "token_expiry": expiry,
                "calendar_id": calendar_id,
            })


def complete_connection(student_id: str, code: str) -> None:
    """Finishes the OAuth flow: exchanges the code, finds or creates this
    student's GradMap calendar, and stores the connection."""
    ensure_google_calendar_tables()
    tokens = _exchange_code_for_tokens(code)
    calendar_id = _find_or_create_gradmap_calendar(tokens["access_token"])
    _save_connection(student_id, tokens, calendar_id)


def disconnect(student_id) -> None:
    connection_row = get_connection(student_id)
    if connection_row is None:
        return
    try:
        requests.post(GOOGLE_REVOKE_URL, params={"token": connection_row["refresh_token"]}, timeout=_REQUEST_TIMEOUT)
    except requests.RequestException:
        pass  # best-effort -- the row is deleted either way, so GradMap forgets the connection locally regardless
    with psycopg.connect(**SCHOOLS_DB_CONFIG) as connection:
        with connection.cursor() as cursor:
            cursor.execute(DELETE_EVENTS_FOR_STUDENT_SQL, (student_id,))
            cursor.execute(DELETE_CONNECTION_SQL, (student_id,))


def _get_valid_access_token(student_id) -> tuple[str, str]:
    """Returns (access_token, calendar_id), refreshing the access token first if it's expired or about to be."""
    connection_row = get_connection(student_id)
    if connection_row is None:
        raise GoogleNotConnectedError(student_id)

    expiry = connection_row["token_expiry"]
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if expiry - timedelta(minutes=2) > datetime.now(timezone.utc):
        return connection_row["access_token"], connection_row["calendar_id"]

    tokens = _refresh_access_token(connection_row["refresh_token"])
    new_expiry = datetime.now(timezone.utc) + timedelta(seconds=tokens["expires_in"])
    with psycopg.connect(**SCHOOLS_DB_CONFIG) as connection:
        with connection.cursor() as cursor:
            cursor.execute(UPDATE_ACCESS_TOKEN_SQL, {
                "student_id": student_id,
                "access_token": tokens["access_token"],
                "token_expiry": new_expiry,
            })
    return tokens["access_token"], connection_row["calendar_id"]


# --- event sync --------------------------------------------------------------

GET_EVENT_MAPPING_SQL = """
SELECT google_event_id FROM google_calendar_events
WHERE student_id = %s AND source_type = %s AND source_id = %s
"""

GET_ALL_EVENT_MAPPINGS_FOR_STUDENT_SQL = """
SELECT source_type, source_id, google_event_id
FROM google_calendar_events
WHERE student_id = %s
"""

UPSERT_EVENT_MAPPING_SQL = """
INSERT INTO google_calendar_events (student_id, source_type, source_id, google_event_id)
VALUES (%(student_id)s, %(source_type)s, %(source_id)s, %(google_event_id)s)
ON CONFLICT (student_id, source_type, source_id) DO UPDATE SET
    google_event_id = EXCLUDED.google_event_id, updated_at = now()
"""

DELETE_EVENT_MAPPING_SQL = """
DELETE FROM google_calendar_events
WHERE student_id = %s AND source_type = %s AND source_id = %s
"""


def _get_event_mapping(student_id, source_type, source_id) -> str | None:
    with psycopg.connect(**SCHOOLS_DB_CONFIG, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(GET_EVENT_MAPPING_SQL, (student_id, source_type, str(source_id)))
            row = cursor.fetchone()
            return row["google_event_id"] if row else None


def upsert_event(student_id, source_type, source_id, title, date, description=None) -> None:
    """date: 'YYYY-MM-DD'. All-day event, since these are dates, not times."""
    if source_type not in ALLOWED_SOURCE_TYPES:
        raise ValueError(f"source_type must be one of {ALLOWED_SOURCE_TYPES}, got {source_type!r}")

    access_token, calendar_id = _get_valid_access_token(student_id)
    headers = {"Authorization": f"Bearer {access_token}"}
    body = {
        "summary": title,
        "description": description or "",
        "start": {"date": date},
        "end": {"date": date},
    }

    google_event_id = _get_event_mapping(student_id, source_type, source_id)
    if google_event_id:
        response = requests.patch(
            f"{GOOGLE_CALENDAR_API}/calendars/{calendar_id}/events/{google_event_id}",
            headers=headers, json=body, timeout=_REQUEST_TIMEOUT,
        )
        if response.status_code == 404:
            # The student (or something else) removed it on the Google side; recreate below.
            google_event_id = None
        else:
            response.raise_for_status()

    if not google_event_id:
        response = requests.post(
            f"{GOOGLE_CALENDAR_API}/calendars/{calendar_id}/events",
            headers=headers, json=body, timeout=_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        with psycopg.connect(**SCHOOLS_DB_CONFIG) as connection:
            with connection.cursor() as cursor:
                cursor.execute(UPSERT_EVENT_MAPPING_SQL, {
                    "student_id": student_id,
                    "source_type": source_type,
                    "source_id": str(source_id),
                    "google_event_id": response.json()["id"],
                })


def delete_event(student_id, source_type, source_id) -> None:
    google_event_id = _get_event_mapping(student_id, source_type, source_id)
    if google_event_id:
        access_token, calendar_id = _get_valid_access_token(student_id)
        headers = {"Authorization": f"Bearer {access_token}"}
        response = requests.delete(
            f"{GOOGLE_CALENDAR_API}/calendars/{calendar_id}/events/{google_event_id}",
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
    previously synced item missing from `items` is deleted from Google."""
    ensure_google_calendar_tables()
    if not is_connected(student_id):
        raise GoogleNotConnectedError(student_id)

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
