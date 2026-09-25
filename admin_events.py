"""Admin-curated, permanent calendar events -- shared across every student,
and never editable from the student side (that's what makes them different
from student_own_events.py's per-student, student-editable events).

Three tables:
  admin_api_keys       -- one row per admin who's allowed to upload. Only a
                          SHA-256 hash of each key is ever stored, the same
                          way a password would be -- the raw key is shown to
                          the admin exactly once, at creation time (see
                          generate_admin_api_key).
  global_events        -- the events themselves. UNIQUE(title, event_date)
                          makes re-uploading the same CSV (e.g. after fixing
                          a typo) idempotent: it updates the existing row
                          instead of creating a duplicate.
  global_events_imports -- one row per upload run (counts + errors). See
                          import_global_events for why this exists instead
                          of the shared admin_bulk_operations table.

gm_schools already has admin_bulk_operations/admin_audit_logs tables that
look purpose-built for exactly this (bulk-write tracking with per-row
success/failure and a rollback payload) -- but this app's DB role
(deren_readonly) only has SELECT on anything that predates this app,
including those two, confirmed via has_table_privilege(). It can only write
to tables it creates itself, so imports are logged to global_events_imports
below instead -- a self-contained equivalent, not a reuse of the shared
ones. If GradMap's real admin backend ever grants this role INSERT there,
point import_global_events at admin_bulk_operations instead; nothing else
in this file (or main.py's two routes) would need to change.
"""

import csv
import hashlib
import io
import json
import os
import secrets
from datetime import date

import psycopg
from psycopg.rows import dict_row

SCHOOLS_DB_CONFIG = {
    "host": os.environ["GM_DB_HOST"],
    "port": int(os.environ["GM_DB_PORT"]),
    "user": os.environ["GM_DB_USER"],
    "password": os.environ["GM_DB_PASSWORD"],
    "dbname": os.environ["GM_DB_SCHOOLS_NAME"],
}

ADMIN_API_KEY_PREFIX = "gm_admin_"
ADMIN_API_KEY_PREFIX_DISPLAY_LEN = len(ADMIN_API_KEY_PREFIX) + 6  # enough to recognize a key in a list

CREATE_ADMIN_API_KEYS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS admin_api_keys (
    id SERIAL PRIMARY KEY,
    admin_name TEXT NOT NULL,
    api_key_hash TEXT NOT NULL UNIQUE,
    api_key_prefix TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ
)
"""

CREATE_GLOBAL_EVENTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS global_events (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    sub_info TEXT,
    event_date DATE NOT NULL,
    applicable_grades INTEGER[],
    source_operation_id TEXT,
    created_by_admin_id INTEGER,
    created_by_admin_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (title, event_date)
)
"""

# Self-owned stand-in for admin_bulk_operations (see module docstring for why
# the shared table isn't writable from here). One row per upload run.
CREATE_GLOBAL_EVENTS_IMPORTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS global_events_imports (
    id SERIAL PRIMARY KEY,
    operation_id TEXT NOT NULL UNIQUE,
    total_count INTEGER NOT NULL,
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    error_details JSONB,
    admin_id INTEGER NOT NULL,
    admin_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'in_progress',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
)
"""


def ensure_admin_tables():
    with psycopg.connect(**SCHOOLS_DB_CONFIG) as connection:
        with connection.cursor() as cursor:
            cursor.execute(CREATE_ADMIN_API_KEYS_TABLE_SQL)
            cursor.execute(CREATE_GLOBAL_EVENTS_TABLE_SQL)
            cursor.execute(CREATE_GLOBAL_EVENTS_IMPORTS_TABLE_SQL)


def _hash_api_key(raw_key):
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_admin_api_key(admin_name):
    """Mint a new admin API key. Returns (raw_key, row) -- raw_key is shown
    to the caller exactly once here; only its hash is stored, so there is no
    way to recover it later if lost (mint a new one instead)."""
    ensure_admin_tables()
    raw_key = ADMIN_API_KEY_PREFIX + secrets.token_urlsafe(32)
    key_hash = _hash_api_key(raw_key)
    prefix = raw_key[:ADMIN_API_KEY_PREFIX_DISPLAY_LEN]

    with psycopg.connect(**SCHOOLS_DB_CONFIG, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO admin_api_keys (admin_name, api_key_hash, api_key_prefix)
                VALUES (%s, %s, %s)
                RETURNING id, admin_name, api_key_prefix, is_active, created_at
                """,
                (admin_name, key_hash, prefix),
            )
            row = cursor.fetchone()
    return raw_key, row


def verify_admin_api_key(raw_key):
    """Look up an active admin by their raw key. Returns {id, admin_name} or
    None. Side effect: bumps last_used_at on a match, so a dormant key is
    easy to spot later."""
    if not raw_key:
        return None
    ensure_admin_tables()
    key_hash = _hash_api_key(raw_key)
    with psycopg.connect(**SCHOOLS_DB_CONFIG, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, admin_name FROM admin_api_keys WHERE api_key_hash = %s AND is_active = true",
                (key_hash,),
            )
            row = cursor.fetchone()
            if row:
                cursor.execute("UPDATE admin_api_keys SET last_used_at = now() WHERE id = %s", (row["id"],))
    return row


def revoke_admin_api_key(key_id):
    ensure_admin_tables()
    with psycopg.connect(**SCHOOLS_DB_CONFIG, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE admin_api_keys SET is_active = false, revoked_at = now()
                WHERE id = %s
                RETURNING id, admin_name, is_active
                """,
                (key_id,),
            )
            return cursor.fetchone()


def list_admin_api_keys():
    ensure_admin_tables()
    with psycopg.connect(**SCHOOLS_DB_CONFIG, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, admin_name, api_key_prefix, is_active, created_at, last_used_at, revoked_at
                FROM admin_api_keys ORDER BY created_at
                """
            )
            return cursor.fetchall()


# --- CSV parsing -------------------------------------------------------------
#
# Required: title, event_date (YYYY-MM-DD -- ISO format only, deliberately not
# accepting MM/DD/YYYY, to avoid silently misreading a date the wrong way).
# Optional: sub_info (free text), applicable_grades (comma-separated grade
# numbers, e.g. "11,12" -- blank means every grade/student).

REQUIRED_CSV_COLUMNS = {"title", "event_date"}
OPTIONAL_CSV_COLUMNS = {"sub_info", "applicable_grades"}


def parse_global_events_csv(file_bytes):
    """Returns (rows, errors). rows are ready to upsert; errors is a list of
    {"row": <1-based line number, header is row 1>, "error": str} for any row
    that failed validation -- a bad row is skipped, not fatal to the batch."""
    text = file_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV file has no header row.")

    header = {h.strip() for h in reader.fieldnames}
    missing = REQUIRED_CSV_COLUMNS - header
    if missing:
        raise ValueError(f"CSV is missing required column(s): {', '.join(sorted(missing))}")

    rows = []
    errors = []
    for line_number, raw_row in enumerate(reader, start=2):
        title = (raw_row.get("title") or "").strip()
        event_date_str = (raw_row.get("event_date") or "").strip()
        sub_info = (raw_row.get("sub_info") or "").strip() or None
        grades_str = (raw_row.get("applicable_grades") or "").strip()

        if not title:
            errors.append({"row": line_number, "error": "Missing title"})
            continue
        if not event_date_str:
            errors.append({"row": line_number, "error": "Missing event_date"})
            continue
        try:
            event_date = date.fromisoformat(event_date_str)
        except ValueError:
            errors.append({"row": line_number, "error": f"event_date {event_date_str!r} must be YYYY-MM-DD"})
            continue

        applicable_grades = None
        if grades_str:
            try:
                applicable_grades = [int(g.strip()) for g in grades_str.split(",") if g.strip()]
            except ValueError:
                errors.append({
                    "row": line_number,
                    "error": f"applicable_grades {grades_str!r} must be comma-separated whole numbers, e.g. \"11,12\"",
                })
                continue

        rows.append({
            "title": title,
            "sub_info": sub_info,
            "event_date": event_date,
            "applicable_grades": applicable_grades,
        })
    return rows, errors


UPSERT_GLOBAL_EVENT_SQL = """
INSERT INTO global_events
    (title, sub_info, event_date, applicable_grades, source_operation_id, created_by_admin_id, created_by_admin_name)
VALUES
    (%(title)s, %(sub_info)s, %(event_date)s, %(applicable_grades)s, %(source_operation_id)s, %(admin_id)s, %(admin_name)s)
ON CONFLICT (title, event_date) DO UPDATE
SET sub_info = EXCLUDED.sub_info,
    applicable_grades = EXCLUDED.applicable_grades,
    source_operation_id = EXCLUDED.source_operation_id,
    updated_at = now()
RETURNING id, title, event_date, sub_info, applicable_grades
"""


def import_global_events(rows, parse_errors, admin):
    """Upserts already-validated rows and wraps the whole run in one
    global_events_imports row (this app's own stand-in for
    admin_bulk_operations -- see module docstring). admin is the dict
    returned by verify_admin_api_key ({"id", "admin_name"})."""
    ensure_admin_tables()
    operation_id = f"global_events_{secrets.token_hex(8)}"
    written = []
    row_errors = list(parse_errors)

    with psycopg.connect(**SCHOOLS_DB_CONFIG, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO global_events_imports (operation_id, total_count, admin_id, admin_name, status)
                VALUES (%s, %s, %s, %s, 'in_progress')
                """,
                (operation_id, len(rows) + len(parse_errors), admin["id"], admin["admin_name"]),
            )

            for row in rows:
                try:
                    cursor.execute(UPSERT_GLOBAL_EVENT_SQL, {
                        **row,
                        "source_operation_id": operation_id,
                        "admin_id": admin["id"],
                        "admin_name": admin["admin_name"],
                    })
                    written.append(cursor.fetchone())
                except Exception as e:
                    row_errors.append({"row": row, "error": str(e)})

            cursor.execute(
                """
                UPDATE global_events_imports
                SET success_count = %s, failure_count = %s, error_details = %s,
                    status = 'completed', completed_at = now()
                WHERE operation_id = %s
                """,
                (len(written), len(row_errors), json.dumps(row_errors, default=str), operation_id),
            )

    return {
        "operation_id": operation_id,
        "inserted": len(written),
        "failed": len(row_errors),
        "errors": row_errors,
        "events": written,
    }


def fetch_global_events():
    ensure_admin_tables()
    with psycopg.connect(**SCHOOLS_DB_CONFIG, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, title, sub_info, event_date, applicable_grades FROM global_events ORDER BY event_date"
            )
            return cursor.fetchall()
