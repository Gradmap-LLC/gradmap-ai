import argparse
import json
import os

from numpy import conj
import psycopg

from recommend import recomendations


DB_CONFIG = {
    "student": {
    "host": os.environ.get("GM_DB_HOST", "3.14.196.144"),
    "port": int(os.environ.get("GM_DB_PORT", "5432")),
    "user": os.environ.get("GM_DB_USER", "deren_readonly"),
    "password": os.environ.get("GM_DB_PASSWORD", "derenreadonly@3sd1"),
    "dbname": os.environ.get("GM_DB_NAME", "student"),
    },
    "gm_schools": {
    "host": os.environ.get("GM_DB_HOST", "3.14.196.144"),
    "port": int(os.environ.get("GM_DB_PORT", "5432")),
    "user": os.environ.get("GM_DB_USER", "deren_readonly"),
    "password": os.environ.get("GM_DB_PASSWORD", "derenreadonly@3sd1"),
    "dbname": os.environ.get("GM_DB_NAME", "gm_schools"),
    }

}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--student", type=str, help="The user ID of the student")

    args = parser.parse_args()

    if not args.student:
        parser.error("--student is required.")

    student = _fetch_student_snapshot(args.student)
    print(student)
