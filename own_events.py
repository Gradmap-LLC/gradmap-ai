"""Student-added "own" calendar events (campus visits, test days, etc.) --
the events a student types into "Add an event" on the calendar screen,
separate from tasks/recommendations and from hard deadlines/target dates.
Same student_recommendations-adjacent DB (GM_DB_SCHOOLS_NAME), own table.
"""

import os

import psycopg
from psycopg.rows import dict_row

SCHOOLS_DB_CONFIG = {
    "host": os.environ["GM_DB_HOST"],
    "port": int(os.environ["GM_DB_PORT"]),
    "user": os.environ["GM_DB_USER"],
    "password": os.environ["GM_DB_PASSWORD"],
    "dbname": os.environ["GM_DB_SCHOOLS_NAME"],
}

ALLOWED_EVENT_KINDS = (
    "campus_visit",
    "test_day",
    "school_event",
    "info_session",
    "family_personal",
    "other",
)

CREATE_OWN_EVENTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS student_own_events (
    id SERIAL PRIMARY KEY,
    student_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    event_date DATE NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN (
        'campus_visit', 'test_day', 'school_event', 'info_session', 'family_personal', 'other'
    )),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

INSERT_OWN_EVENT_SQL = """
INSERT INTO student_own_events (student_id, title, event_date, kind)
VALUES (%(student_id)s, %(title)s, %(event_date)s, %(kind)s)
RETURNING id, title, event_date, kind
"""

FETCH_OWN_EVENTS_SQL = """
SELECT id, title, event_date, kind
FROM student_own_events
WHERE student_id = %s
ORDER BY event_date
"""

UPDATE_OWN_EVENT_SQL = """
UPDATE student_own_events
SET title = %(title)s, event_date = %(event_date)s, updated_at = now()
WHERE id = %(id)s AND student_id = %(student_id)s
RETURNING id, title, event_date, kind
"""

DELETE_OWN_EVENT_SQL = """
DELETE FROM student_own_events
WHERE id = %s AND student_id = %s
RETURNING id, title
"""


def ensure_own_events_table():
    with psycopg.connect(**SCHOOLS_DB_CONFIG) as connection:
        with connection.cursor() as cursor:
            cursor.execute(CREATE_OWN_EVENTS_TABLE_SQL)


def add_own_event(student_id, title, event_date, kind):
    """event_date: an ISO date string ('YYYY-MM-DD')."""
    if kind not in ALLOWED_EVENT_KINDS:
        raise ValueError(f"kind must be one of {ALLOWED_EVENT_KINDS}, got {kind!r}")

    ensure_own_events_table()
    with psycopg.connect(**SCHOOLS_DB_CONFIG, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(INSERT_OWN_EVENT_SQL, {
                "student_id": student_id, "title": title, "event_date": event_date, "kind": kind,
            })
            return cursor.fetchone()


def list_own_events(student_id):
    ensure_own_events_table()
    with psycopg.connect(**SCHOOLS_DB_CONFIG, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(FETCH_OWN_EVENTS_SQL, (student_id,))
            return cursor.fetchall()


def update_own_event(student_id, event_id, title, event_date):
    ensure_own_events_table()
    with psycopg.connect(**SCHOOLS_DB_CONFIG, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(UPDATE_OWN_EVENT_SQL, {
                "id": event_id, "student_id": student_id, "title": title, "event_date": event_date,
            })
            return cursor.fetchone()


def delete_own_event(student_id, event_id):
    with psycopg.connect(**SCHOOLS_DB_CONFIG, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(DELETE_OWN_EVENT_SQL, (event_id, student_id))
            return cursor.fetchone()
