import argparse
import json
import os
from urllib.parse import urlencode

import psycopg
from fastapi import FastAPI, HTTPException
from psycopg.rows import dict_row
from pydantic import BaseModel
from recommend import fetch_recommendation, recommendations, update_recommendation_status


GOOGLE_CALENDAR_EVENT_URL = "https://calendar.google.com/calendar/render"


DB_CONFIG = {
    "student": {
    "host": os.environ["GM_DB_HOST"],
    "port": int(os.environ["GM_DB_PORT"]),
    "user": os.environ["GM_DB_USER"],
    "password": os.environ["GM_DB_PASSWORD"],
    "dbname": os.environ["GM_DB_STUDENT_NAME"],
    },
    "gm_schools": {
    "host": os.environ["GM_DB_HOST"],
    "port": int(os.environ["GM_DB_PORT"]),
    "user": os.environ["GM_DB_USER"],
    "password": os.environ["GM_DB_PASSWORD"],
    "dbname": os.environ["GM_DB_SCHOOLS_NAME"],
    }

}


STUDENT_SNAPSHOT_SQL = """
SELECT
    pi.id,
    pi.year_finish_high_school,
    pi.first_name,
    pi.last_name,
    st.is_have_sat_scores_report,
    at.is_have_act_score_report,
    at.superscore_calculated_by_act,
    at.future_testing_date_1 AS act_future_testing_date_1,
    st.highest_total_score,
    st.future_testing_date_1 AS sat_future_testing_date_1,
    hs.culmative_gpa,
    hs.gpa_weighting,
    ah.activity_array,
    ah.honor_array,
    pm.status,
    pm.application_form,
    pm.recommendation_letters,
    pm.transcripts,
    pm.resume,
    pm.essays,
    pm.reminders
FROM personal_information pi
LEFT JOIN act_test at ON at.id = pi.id
LEFT JOIN sat_test st ON st.id = pi.id
LEFT JOIN high_school hs ON hs.id = pi.id
LEFT JOIN activity_honor ah ON ah.id = pi.id
LEFT JOIN programs_manager pm ON pm.program_id = pi.id
WHERE pi.id = %s
"""


STUDENT_SCHOOL_PICKS_SQL = """
SELECT
    id,
    is_active,
    student_likelihood_category,
    metadata
FROM student_school_picks
WHERE student_id = %s
"""


def _decode_json_value(value):
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("[") or text.startswith("{"):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return value
    return value


def _row_to_snapshot(row):
    return {
        "id": row["id"],
        "program_id": row["id"],
        "personal_information": {
            "year_finish_high_school": row["year_finish_high_school"],
            #"first_name": row["first_name"],
            #"last_name": row["last_name"],
        },
        "act_test": {
            "is_have_act_score_report": row["is_have_act_score_report"],
            "superscore_calculated_by_act": row["superscore_calculated_by_act"],
            "future_testing_date_1": row["act_future_testing_date_1"],
        },
        "sat_test": {
            "is_have_sat_scores_report": row["is_have_sat_scores_report"],
            "highest_total_score": row["highest_total_score"],
            "future_testing_date_1": row["sat_future_testing_date_1"],
        },
        "high_school": {
            "culmative_gpa": row["culmative_gpa"],
            "gpa_weighting": row["gpa_weighting"],
        },
        "activity_honor": {
            "activity_array": _decode_json_value(row["activity_array"]),
            "honor_array": _decode_json_value(row["honor_array"]),
        },
        "programs_manager": {
            "status": _decode_json_value(row["status"]),
            "application_form": _decode_json_value(row["application_form"]),
            "recommendation_letters": row["recommendation_letters"],
            "transcripts": row["transcripts"],
            "resume": row["resume"],
            "essays": row["essays"],
            "reminders": row["reminders"],
        },
    }


def _fetch_student_school_picks(student_id):
    with psycopg.connect(**DB_CONFIG["gm_schools"], row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(STUDENT_SCHOOL_PICKS_SQL, (student_id,))
            rows = cursor.fetchall()

    if not rows:
        return []

    return [
        {
            "id": row["id"],
            "is_active": row["is_active"],
            "student_likelihood_category": row["student_likelihood_category"],
            "metadata": _decode_json_value(row["metadata"]),
        }
        for row in rows
    ]


def _fetch_student_snapshot(student_id):
    with psycopg.connect(**DB_CONFIG["student"], row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(STUDENT_SNAPSHOT_SQL, (student_id,))
            row = cursor.fetchone()

    if row is None:
        raise ValueError(f"Student with ID {student_id} not found.")

    snapshot = _row_to_snapshot(row)
    student_school_picks = _fetch_student_school_picks(student_id)
    snapshot["student_school_picks"] = student_school_picks

    return snapshot


app = FastAPI()


class RecommendationStatusUpdate(BaseModel):
    status: str = "not_started"


@app.post("/students/{student_id}/recommendations")
def create_recommendations(student_id: str):
    try:
        student = _fetch_student_snapshot(student_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))

    return recommendations(student)


@app.patch("/students/{student_id}/recommendations/{recommendation_id}")
def set_recommendation_status(student_id: str, recommendation_id: int, body: RecommendationStatusUpdate):
    try:
        result = update_recommendation_status(student_id, recommendation_id, body.status)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))

    if result is None:
        raise HTTPException(status_code=404, detail="Recommendation not found")

    updated_id, status, urgency_rank = result
    return {"id": updated_id, "status": status, "urgency_rank": urgency_rank}


@app.get("/students/{student_id}/recommendations/{recommendation_id}/calendar-link")
def get_recommendation_calendar_link(student_id: str, recommendation_id: int):
    recommendation = fetch_recommendation(student_id, recommendation_id)
    if recommendation is None:
        raise HTTPException(status_code=404, detail="Recommendation not found")

    params = {"action": "TEMPLATE", "text": recommendation["title"]}
    return {"calendar_url": f"{GOOGLE_CALENDAR_EVENT_URL}?{urlencode(params)}"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--student", dest="student_id", type=str, required=True, help="Enter the student ID")

    args = parser.parse_args()

    student = _fetch_student_snapshot(args.student_id)
    #print(json.dumps(student, indent=2, default=str))
    print(recommendations(student))


if __name__ == "__main__":
    main()
