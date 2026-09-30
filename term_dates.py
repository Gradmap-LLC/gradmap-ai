"""Per-student term-end dates (semester / quarter / trimester / full-year).

Schools all end their terms on slightly different dates, so instead of relying
on the dashboard's generic defaults the student tells us when each term ends.
Stored per school year (school_year = the calendar year the school year
started in) so a new school year starts with no dates and the student is asked
again -- term dates shift from year to year.
Same GM_DB_SCHOOLS_NAME DB as own_events.py, own table.
"""

import os
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

TERM_COUNTS = {"full_year": 1, "semester": 2, "trimester": 3, "quarter": 4}

CREATE_TERM_DATES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS student_term_dates (
    student_id INTEGER NOT NULL,
    school_year INTEGER NOT NULL,
    term_index INTEGER NOT NULL CHECK (term_index BETWEEN 0 AND 3),
    term_end_date DATE NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (student_id, school_year, term_index)
)
"""

LIST_TERM_DATES_SQL = """
SELECT term_index, term_end_date
FROM student_term_dates
WHERE student_id = %s AND school_year = %s
ORDER BY term_index
"""

UPSERT_TERM_DATE_SQL = """
INSERT INTO student_term_dates (student_id, school_year, term_index, term_end_date)
VALUES (%s, %s, %s, %s)
ON CONFLICT (student_id, school_year, term_index) DO UPDATE SET
    term_end_date = EXCLUDED.term_end_date, updated_at = now()
"""

DELETE_TERM_DATES_SQL = "DELETE FROM student_term_dates WHERE student_id = %s AND school_year = %s"


def current_school_year(today: date | None = None) -> int:
    today = today or date.today()
    return today.year if today.month >= 7 else today.year - 1


def ensure_term_dates_table():
    with psycopg.connect(**SCHOOLS_DB_CONFIG) as connection:
        with connection.cursor() as cursor:
            cursor.execute(CREATE_TERM_DATES_TABLE_SQL)


def list_term_dates(student_id) -> dict:
    ensure_term_dates_table()
    school_year = current_school_year()
    with psycopg.connect(**SCHOOLS_DB_CONFIG, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(LIST_TERM_DATES_SQL, (student_id, school_year))
            rows = cursor.fetchall()
    return {
        "school_year": school_year,
        "terms": [{"term_index": r["term_index"], "end_date": r["term_end_date"].isoformat()} for r in rows],
    }


def save_term_dates(student_id, term_system: str, terms: list[dict]) -> dict:
    """terms: [{term_index, end_date 'YYYY-MM-DD'}]. Replaces the student's
    dates for the current school year with exactly this set."""
    if term_system not in TERM_COUNTS:
        raise ValueError(f"term_system must be one of {tuple(TERM_COUNTS)}, got {term_system!r}")

    parsed = {}
    for term in terms:
        index = term["term_index"]
        if not 0 <= index < TERM_COUNTS[term_system]:
            raise ValueError(f"term_index {index} is out of range for a {term_system} schedule")
        try:
            parsed[index] = date.fromisoformat(term["end_date"])
        except ValueError:
            raise ValueError(f"end_date must be YYYY-MM-DD, got {term['end_date']!r}")

    ensure_term_dates_table()
    school_year = current_school_year()
    with psycopg.connect(**SCHOOLS_DB_CONFIG) as connection:
        with connection.cursor() as cursor:
            cursor.execute(DELETE_TERM_DATES_SQL, (student_id, school_year))
            for index, end_date in parsed.items():
                cursor.execute(UPSERT_TERM_DATE_SQL, (student_id, school_year, index, end_date))
    return list_term_dates(student_id)
