"""Apple/iCloud Calendar integration via CalDAV.

Unlike google_calendar.py, there is no OAuth here -- Apple has no developer
console step and no client id/secret. Each student supplies their own Apple
ID email plus an app-specific password (generated at appleid.apple.com), we
validate it against iCloud, encrypt it, and store it -- that's the whole
"connect" step. Sync is the same one-way (GradMap -> Apple) reconciliation
model as google_calendar.py, just over CalDAV instead of a REST API, onto a
dedicated "GradMap Tasks" calendar we create in the student's iCloud account.
"""

import os
from datetime import date, datetime, timedelta, timezone

import caldav
import icalendar
import psycopg
from caldav.lib.error import AuthorizationError, NotFoundError
from cryptography.fernet import Fernet, InvalidToken
from psycopg.rows import dict_row

SCHOOLS_DB_CONFIG = {
    "host": os.environ["GM_DB_HOST"],
    "port": int(os.environ["GM_DB_PORT"]),
    "user": os.environ["GM_DB_USER"],
    "password": os.environ["GM_DB_PASSWORD"],
    "dbname": os.environ["GM_DB_SCHOOLS_NAME"],
}

ICLOUD_CALDAV_URL = "https://caldav.icloud.com"
GRADMAP_CALENDAR_NAME = "GradMap Tasks"
ALLOWED_SOURCE_TYPES = ("hard_deadline", "target_date", "own_event")

_fernet = Fernet(os.environ["CALDAV_ENCRYPTION_KEY"].encode())


class AppleCalendarAuthError(Exception):
    """Bad Apple ID / app-specific password, or iCloud otherwise refused the connection."""


class AppleNotConnectedError(Exception):
    def __init__(self, student_id):
        super().__init__(f"Student {student_id} has not connected Apple Calendar")


def _encrypt(value: str) -> str:
    return _fernet.encrypt(value.encode()).decode()


def _decrypt(value: str) -> str:
    return _fernet.decrypt(value.encode()).decode()


# --- table setup -----------------------------------------------------------

CREATE_APPLE_CALENDAR_CONNECTIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS apple_calendar_connections (
    student_id INTEGER PRIMARY KEY,
    apple_id TEXT NOT NULL,
    encrypted_password TEXT NOT NULL,
    calendar_url TEXT NOT NULL,
    connected_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

CREATE_APPLE_CALENDAR_EVENTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS apple_calendar_events (
    id SERIAL PRIMARY KEY,
    student_id INTEGER NOT NULL,
    source_type TEXT NOT NULL CHECK (source_type IN ('hard_deadline', 'target_date', 'own_event')),
    source_id TEXT NOT NULL,
    caldav_uid TEXT NOT NULL,
    event_url TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (student_id, source_type, source_id)
)
"""

# event_url predates the fix below (event_by_uid's REPORT search 412s against
# iCloud); kept as a migration for any table created before this column existed.
ADD_APPLE_CALENDAR_EVENTS_COLUMNS_SQL = """
ALTER TABLE apple_calendar_events
    ADD COLUMN IF NOT EXISTS event_url TEXT
"""


def ensure_apple_calendar_tables():
    with psycopg.connect(**SCHOOLS_DB_CONFIG) as connection:
        with connection.cursor() as cursor:
            cursor.execute(CREATE_APPLE_CALENDAR_CONNECTIONS_TABLE_SQL)
            cursor.execute(CREATE_APPLE_CALENDAR_EVENTS_TABLE_SQL)
            cursor.execute(ADD_APPLE_CALENDAR_EVENTS_COLUMNS_SQL)


# --- connection storage ----------------------------------------------------

GET_CONNECTION_SQL = """
SELECT apple_id, encrypted_password, calendar_url
FROM apple_calendar_connections WHERE student_id = %s
"""

UPSERT_CONNECTION_SQL = """
INSERT INTO apple_calendar_connections (student_id, apple_id, encrypted_password, calendar_url)
VALUES (%(student_id)s, %(apple_id)s, %(encrypted_password)s, %(calendar_url)s)
ON CONFLICT (student_id) DO UPDATE SET
    apple_id = EXCLUDED.apple_id,
    encrypted_password = EXCLUDED.encrypted_password,
    calendar_url = EXCLUDED.calendar_url
"""

DELETE_CONNECTION_SQL = "DELETE FROM apple_calendar_connections WHERE student_id = %s"
DELETE_EVENTS_FOR_STUDENT_SQL = "DELETE FROM apple_calendar_events WHERE student_id = %s"


def get_connection(student_id):
    ensure_apple_calendar_tables()
    with psycopg.connect(**SCHOOLS_DB_CONFIG, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(GET_CONNECTION_SQL, (student_id,))
            return cursor.fetchone()


def is_connected(student_id) -> bool:
    return get_connection(student_id) is not None


def _dav_client(apple_id: str, app_specific_password: str) -> caldav.DAVClient:
    return caldav.DAVClient(url=ICLOUD_CALDAV_URL, username=apple_id, password=app_specific_password)


def _find_or_create_gradmap_calendar(client: caldav.DAVClient) -> str:
    principal = client.principal()
    for existing in principal.calendars():
        if existing.name == GRADMAP_CALENDAR_NAME:
            return str(existing.url)
    created = principal.make_calendar(name=GRADMAP_CALENDAR_NAME)
    return str(created.url)


def connect(student_id, apple_id: str, app_specific_password: str) -> None:
    """Validates the credentials against iCloud (this is the only real check
    we get -- there's no separate "authorize" step), finds or creates this
    student's GradMap calendar, and stores the connection."""
    ensure_apple_calendar_tables()
    try:
        client = _dav_client(apple_id, app_specific_password)
        calendar_url = _find_or_create_gradmap_calendar(client)
    except AuthorizationError as error:
        raise AppleCalendarAuthError("Incorrect Apple ID or app-specific password.") from error
    except Exception as error:
        raise AppleCalendarAuthError("Could not connect to iCloud Calendar. Please try again.") from error

    with psycopg.connect(**SCHOOLS_DB_CONFIG) as connection:
        with connection.cursor() as cursor:
            cursor.execute(UPSERT_CONNECTION_SQL, {
                "student_id": student_id,
                "apple_id": apple_id,
                "encrypted_password": _encrypt(app_specific_password),
                "calendar_url": calendar_url,
            })


def disconnect(student_id) -> None:
    """Forgets the credential locally. Unlike Google, there's nothing to
    revoke server-side -- the app-specific password stays valid until the
    student removes it themselves at appleid.apple.com."""
    with psycopg.connect(**SCHOOLS_DB_CONFIG) as connection:
        with connection.cursor() as cursor:
            cursor.execute(DELETE_EVENTS_FOR_STUDENT_SQL, (student_id,))
            cursor.execute(DELETE_CONNECTION_SQL, (student_id,))


def _connect_calendar(student_id):
    """Returns a ready-to-use Calendar for this student, or raises
    AppleNotConnectedError / AppleCalendarAuthError."""
    row = get_connection(student_id)
    if row is None:
        raise AppleNotConnectedError(student_id)
    try:
        password = _decrypt(row["encrypted_password"])
    except InvalidToken as error:
        raise AppleCalendarAuthError("Stored Apple credentials could not be read; please reconnect.") from error
    client = _dav_client(row["apple_id"], password)
    return client.calendar(url=row["calendar_url"])


# --- event sync --------------------------------------------------------------

GET_EVENT_MAPPING_SQL = """
SELECT caldav_uid, event_url FROM apple_calendar_events
WHERE student_id = %s AND source_type = %s AND source_id = %s
"""

GET_ALL_EVENT_MAPPINGS_FOR_STUDENT_SQL = """
SELECT source_type, source_id, caldav_uid
FROM apple_calendar_events WHERE student_id = %s
"""

UPSERT_EVENT_MAPPING_SQL = """
INSERT INTO apple_calendar_events (student_id, source_type, source_id, caldav_uid, event_url)
VALUES (%(student_id)s, %(source_type)s, %(source_id)s, %(caldav_uid)s, %(event_url)s)
ON CONFLICT (student_id, source_type, source_id) DO UPDATE SET
    caldav_uid = EXCLUDED.caldav_uid, event_url = EXCLUDED.event_url, updated_at = now()
"""

DELETE_EVENT_MAPPING_SQL = """
DELETE FROM apple_calendar_events
WHERE student_id = %s AND source_type = %s AND source_id = %s
"""


def _get_event_mapping(student_id, source_type, source_id) -> dict | None:
    with psycopg.connect(**SCHOOLS_DB_CONFIG, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(GET_EVENT_MAPPING_SQL, (student_id, source_type, str(source_id)))
            return cursor.fetchone()


def _save_event_mapping(student_id, source_type, source_id, caldav_uid, event_url):
    with psycopg.connect(**SCHOOLS_DB_CONFIG) as connection:
        with connection.cursor() as cursor:
            cursor.execute(UPSERT_EVENT_MAPPING_SQL, {
                "student_id": student_id,
                "source_type": source_type,
                "source_id": str(source_id),
                "caldav_uid": caldav_uid,
                "event_url": event_url,
            })


def _build_ics_event(uid: str, title: str, event_date: date, description: str = "") -> bytes:
    cal = icalendar.Calendar()
    cal.add("prodid", "-//GradMap//Calendar Sync//EN")
    cal.add("version", "2.0")
    event = icalendar.Event()
    event.add("uid", uid)
    event.add("summary", title)
    event.add("description", description or "")
    event.add("dtstart", event_date)              # a date (not datetime) -> an all-day VEVENT
    event.add("dtend", event_date + timedelta(days=1))  # CalDAV all-day events use an exclusive end
    event.add("dtstamp", datetime.now(timezone.utc))
    cal.add_component(event)
    return cal.to_ical()


def upsert_event(student_id, source_type, source_id, title, date_str, description=None) -> None:
    """date_str: 'YYYY-MM-DD'.

    Updates go straight to the event's stored URL (a plain PUT) rather than
    looking it up via calendar.event_by_uid() -- that does a UID-search REPORT
    query, and iCloud's CalDAV server reliably answers that with
    '412 Precondition Failed' for this account. Fetching/writing by URL
    sidesteps that broken query path entirely.
    """
    if source_type not in ALLOWED_SOURCE_TYPES:
        raise ValueError(f"source_type must be one of {ALLOWED_SOURCE_TYPES}, got {source_type!r}")

    calendar = _connect_calendar(student_id)
    event_date = datetime.strptime(date_str, "%Y-%m-%d").date()

    mapping = _get_event_mapping(student_id, source_type, source_id)
    uid = mapping["caldav_uid"] if mapping else f"gradmap-{student_id}-{source_type}-{source_id}@gradmap.com"
    ics = _build_ics_event(uid, title, event_date, description)

    if mapping and mapping["event_url"]:
        # parent is required even with a full url -- save()'s internal path
        # resolution does self.parent.url.join(path) unconditionally.
        caldav.Event(client=calendar.client, parent=calendar, url=mapping["event_url"], data=ics).save()
        return

    created = calendar.save_event(ics)
    _save_event_mapping(student_id, source_type, source_id, uid, str(created.url))


def delete_event(student_id, source_type, source_id) -> None:
    mapping = _get_event_mapping(student_id, source_type, source_id)
    if mapping and mapping["event_url"]:
        calendar = _connect_calendar(student_id)
        try:
            caldav.Event(client=calendar.client, url=mapping["event_url"]).delete()
        except NotFoundError:
            pass

    with psycopg.connect(**SCHOOLS_DB_CONFIG) as connection:
        with connection.cursor() as cursor:
            cursor.execute(DELETE_EVENT_MAPPING_SQL, (student_id, source_type, str(source_id)))


def sync_events(student_id, items) -> None:
    """items: iterable of dicts with source_type/source_id/title/date/description,
    representing the full current set of calendar-relevant dates for this
    student. Full reconciliation, same as google_calendar.py: anything
    previously synced but missing from `items` is deleted from Apple."""
    ensure_apple_calendar_tables()
    if not is_connected(student_id):
        raise AppleNotConnectedError(student_id)

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
