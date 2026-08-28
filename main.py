import argparse
import json
import os
from typing import Literal
from urllib.parse import urlencode

import psycopg
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

load_dotenv()

from recommend import (
    add_student_task,
    delete_recommendation,
    fetch_all_recommendations,
    fetch_recommendation,
    recommendations,
    update_recommendation_status,
)


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


# --- Honors (awards) -------------------------------------------------------
#
# honor_array is a JSON array stored per-student on activity_honor.id. There's
# no per-entry primary key in the schema, so an honor is addressed by its
# position in the array; the response returns that index as `id` for now.

RECOGNITION_LEVEL_FIELDS = {
    "school": "isSchoolLevelRecognition",
    "state": "isStateLevelRecognition",
    "national": "isNational",
    "international": "isInternationalLevelRecognition",
}

# NOTE: field names for 10th/11th grade are inferred from the isGradeNinthLevel /
# isGradeTwelvethLevel naming pattern seen in the activity_honor export and have
# not been directly confirmed against the live schema.
GRADE_LEVEL_FIELDS = {
    "9": "isGradeNinthLevel",
    "10": "isGradeTenthLevel",
    "11": "isGradeEleventhLevel",
    "12": "isGradeTwelvethLevel",
    "post_graduate": "isPostGraduateLevel",
}

SELECT_HONOR_ARRAY_SQL = "SELECT honor_array FROM activity_honor WHERE id = %s"

UPDATE_HONOR_ARRAY_SQL = """
UPDATE activity_honor
SET honor_array = %(honor_array)s,
    number_of_honor = %(number_of_honor)s,
    updated_at = now()
WHERE id = %(student_id)s
RETURNING honor_array
"""


class AddHonorRequest(BaseModel):
    honor_title: str
    honor_type: Literal["Academic", "Non-academic"]
    recognition_level: Literal["school", "state", "national", "international"]
    grade_levels: list[Literal["9", "10", "11", "12", "post_graduate"]] = Field(min_length=1)
    action_to_achieve: str | None = None
    eligibility_requirements: str | None = None
    include_in_common_app: bool = True
    include_in_uc_app: bool = True
    include_in_csu_app: bool = True


def _honor_request_to_record(honor: AddHonorRequest) -> dict:
    record = {
        "typeOfHonor": honor.honor_type,
        "honorTitle": honor.honor_title,
        "actionToAchieveHonor": honor.action_to_achieve or "",
        "eligibilityRequirementsHonor": honor.eligibility_requirements or "",
        "isIncludeIntoCommonApp": honor.include_in_common_app,
        "isIncludeIntoUCApp": honor.include_in_uc_app,
        "isIncludeIntoCSUApp": honor.include_in_csu_app,
    }
    for key, field in RECOGNITION_LEVEL_FIELDS.items():
        record[field] = key == honor.recognition_level
    for key, field in GRADE_LEVEL_FIELDS.items():
        record[field] = key in honor.grade_levels
    return record


def _append_honor(student_id: str, honor: AddHonorRequest) -> tuple[dict, int]:
    with psycopg.connect(**DB_CONFIG["student"], row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(SELECT_HONOR_ARRAY_SQL, (student_id,))
            row = cursor.fetchone()
            if row is None:
                raise ValueError(f"Student with ID {student_id} not found.")

            existing = _decode_json_value(row["honor_array"]) or []
            # Sparse rows in this table sometimes hold placeholder `{}` entries;
            # they aren't real honors, so don't count them when appending.
            existing = [entry for entry in existing if entry]

            record = _honor_request_to_record(honor)
            new_index = len(existing)
            updated = existing + [record]

            cursor.execute(
                UPDATE_HONOR_ARRAY_SQL,
                {
                    "honor_array": json.dumps(updated),
                    "number_of_honor": len(updated),
                    "student_id": student_id,
                },
            )
            connection.commit()

    return record, new_index


@app.post("/students/{student_id}/honors")
def add_honor(student_id: str, body: AddHonorRequest):
    try:
        record, index = _append_honor(student_id, body)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))

    return {"id": index, "student_id": student_id, "honor": record}


# --- Activities --------------------------------------------------------
#
# activity_array is a JSON array stored per-student on activity_honor.id,
# alongside (but independent of) honor_array. Same addressing scheme as
# honors: no per-entry primary key, so an activity is addressed by its
# position in the array.

ACTIVITY_GRADE_LEVEL_FIELDS = {
    "9": "isGrade9ParticipationLevels",
    "10": "isGrade10ParticipationLevels",
    "11": "isGrade11ParticipationLevels",
    "12": "isGrade12ParticipationLevels",
    "post_graduate": "isPostGraduateParticipationLevels",
}

ACTIVITY_TIMING_FIELDS = {
    "during_school_year": "timmingOfParticipation_duringYear",
    "during_break": "timmingOfParticipation_duringBreak",
    "all_year": "timmingOfParticipation_allYear",
}

SELECT_ACTIVITY_ARRAY_SQL = "SELECT activity_array FROM activity_honor WHERE id = %s"

UPDATE_ACTIVITY_ARRAY_SQL = """
UPDATE activity_honor
SET activity_array = %(activity_array)s,
    is_have_any_activity_to_report = true,
    updated_at = now()
WHERE id = %(student_id)s
RETURNING activity_array
"""


class AddActivityRequest(BaseModel):
    program_name: str
    category: str
    category_uc: str
    activity_type: str
    position_description: str
    is_leadership_role: bool
    grade_levels: list[Literal["9", "10", "11", "12", "post_graduate"]] = Field(min_length=1)
    timing: Literal["during_school_year", "during_break", "all_year"]
    hours_per_week: float
    weeks_per_year: float
    description: str
    is_currently_participating: bool = True
    intends_to_continue: bool = False
    notable_distinctions: str | None = None
    is_paid_work: bool = False
    organization_description: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    hours_per_week_low: float | None = None
    hours_per_week_high: float | None = None


def _activity_request_to_record(activity: AddActivityRequest) -> dict:
    record = {
        "category": activity.category,
        "categoryUC": activity.category_uc,
        "activityType": activity.activity_type,
        "programName": activity.program_name,
        "activityNameUC": "",
        "activityExperienceProgramName": activity.program_name,
        "isInvolvedLeadershipRole": activity.is_leadership_role,
        "positionDescription": activity.position_description,
        "isCurrentlyParticipatingActivity": activity.is_currently_participating,
        "timmingOfParticipation": "",
        "hoursPerWeek": activity.hours_per_week,
        "weeksPerYear": activity.weeks_per_year,
        "isIntendParticipateSimilarActivity": activity.intends_to_continue,
        "listIndividualDistinctions": activity.notable_distinctions or "",
        "describeActivity": activity.description,
        "whatDidYouDo": activity.description,
        "descriptionExperience": activity.description,
        "programNameForEducationPreparation": "",
        "programNameForEducationPreparationSpecify": "",
        "describeOrganization": activity.organization_description or "",
        "describeCompany": activity.organization_description or "" if activity.is_paid_work else "",
        "isStillWork": "true" if (activity.is_paid_work and not activity.end_date) else "",
        "startDate": activity.start_date or "",
        "endDate": activity.end_date or "",
        "hoursPerWeekLowEnd": activity.hours_per_week_low if activity.hours_per_week_low is not None else "",
        "hoursPerWeekHighEnd": activity.hours_per_week_high if activity.hours_per_week_high is not None else "",
        "otherCoursewordName": "",
        "brieflyDescribe": "",
    }
    for key, field in ACTIVITY_GRADE_LEVEL_FIELDS.items():
        record[field] = key in activity.grade_levels
    for key, field in ACTIVITY_TIMING_FIELDS.items():
        record[field] = key == activity.timing
    return record


def _append_activity(student_id: str, activity: AddActivityRequest) -> tuple[dict, int]:
    with psycopg.connect(**DB_CONFIG["student"], row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(SELECT_ACTIVITY_ARRAY_SQL, (student_id,))
            row = cursor.fetchone()
            if row is None:
                raise ValueError(f"Student with ID {student_id} not found.")

            existing = _decode_json_value(row["activity_array"]) or []
            # Sparse rows in this table sometimes hold placeholder `{}` entries;
            # they aren't real activities, so don't count them when appending.
            existing = [entry for entry in existing if entry]

            record = _activity_request_to_record(activity)
            new_index = len(existing)
            updated = existing + [record]

            cursor.execute(
                UPDATE_ACTIVITY_ARRAY_SQL,
                {
                    "activity_array": json.dumps(updated),
                    "student_id": student_id,
                },
            )
            connection.commit()

    return record, new_index


@app.post("/students/{student_id}/activities")
def add_activity(student_id: str, body: AddActivityRequest):
    try:
        record, index = _append_activity(student_id, body)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))

    return {"id": index, "student_id": student_id, "activity": record}


@app.post("/students/{student_id}/recommendations")
def create_recommendations(student_id: str):
    try:
        student = _fetch_student_snapshot(student_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))

    return recommendations(student)


@app.get("/students/{student_id}/recommendations")
def list_recommendations(student_id: str):
    """List the student's current recommendations/tasks as already stored --
    unlike POST (which calls the LLM and inserts new ones), this just reads."""
    return {"recommendations": fetch_all_recommendations(student_id)}


class AddRecommendationRequest(BaseModel):
    title: str
    subtext: str | None = None
    link: str | None = None
    category: str | None = None
    urgency_rank: str | None = None
    estimated_time: str | None = None


@app.post("/students/{student_id}/recommendations/custom")
def add_custom_recommendation(student_id: str, body: AddRecommendationRequest):
    try:
        return add_student_task(
            student_id,
            body.title,
            subtext=body.subtext,
            link=body.link,
            category=body.category,
            urgency_rank=body.urgency_rank,
            estimated_time=body.estimated_time,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))


@app.delete("/students/{student_id}/recommendations/{recommendation_id}")
def remove_recommendation(student_id: str, recommendation_id: int):
    result = delete_recommendation(student_id, recommendation_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Recommendation not found")

    return {"id": result["id"], "title": result["title"], "removed": True}


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
