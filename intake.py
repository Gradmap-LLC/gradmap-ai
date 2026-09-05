"""Creates and manages the student_intake table in gm_schools.

student_intake holds the one-time (but editable) profile a student fills out
when they first join GradMap -- academics, intended major(s), and activities
grouped by type. One row per student, addressed by student_id (same
one-row-per-student convention as sat_test/act_test).

Each activity-type column (leadership, paid_work, volunteer_work,
passion_projects, academic_prep_programs, internship) is a JSONB array of
entries shaped like:
    {"activity_name": str, "position": str, "hours_per_year": number, "weeks_per_year": number}
"""

import os

import psycopg
from dotenv import load_dotenv

load_dotenv()


SCHOOLS_DB_CONFIG = {
    "host": os.environ["GM_DB_HOST"],
    "port": int(os.environ["GM_DB_PORT"]),
    "user": os.environ["GM_DB_USER"],
    "password": os.environ["GM_DB_PASSWORD"],
    "dbname": os.environ["GM_DB_SCHOOLS_NAME"],
}


CREATE_STUDENT_INTAKE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS student_intake (
    id SERIAL PRIMARY KEY,
    student_id INTEGER NOT NULL UNIQUE,
    grade_level TEXT,
    weighted_gpa NUMERIC(4,3),
    unweighted_gpa NUMERIC(4,3),
    weighted_classes INTEGER,
    weighted_by_12th_grade INTEGER,
    first_major TEXT,
    second_major TEXT,
    leadership JSONB NOT NULL DEFAULT '[]'::jsonb,
    paid_work JSONB NOT NULL DEFAULT '[]'::jsonb,
    volunteer_work JSONB NOT NULL DEFAULT '[]'::jsonb,
    passion_projects JSONB NOT NULL DEFAULT '[]'::jsonb,
    academic_prep_programs JSONB NOT NULL DEFAULT '[]'::jsonb,
    internship JSONB NOT NULL DEFAULT '[]'::jsonb,
    most_important TEXT,
    college_group TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_edited TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


ADD_STUDENT_INTAKE_COLUMNS_SQL = """
ALTER TABLE student_intake
    ADD COLUMN IF NOT EXISTS college_group TEXT
"""


def ensure_student_intake_table():
    with psycopg.connect(**SCHOOLS_DB_CONFIG) as connection:
        with connection.cursor() as cursor:
            cursor.execute(CREATE_STUDENT_INTAKE_TABLE_SQL)
            cursor.execute(ADD_STUDENT_INTAKE_COLUMNS_SQL)


if __name__ == "__main__":
    ensure_student_intake_table()
    print("student_intake table is ready.")
