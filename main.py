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


# --- College list ----------------------------------------------------------
#
# student_school_picks holds every college a student has saved, grouped by
# list_group_name (a student/counselor-chosen label like "My Colleges" or
# "Mom's list"). Most of what a caller wants -- school name, city, state,
# intended major, application type -- lives inside the metadata JSON blob,
# not as its own column, so it has to be parsed out per row.

STUDENT_COLLEGE_LIST_SQL = """
SELECT
    id,
    school_id,
    is_active,
    student_likelihood_category,
    list_group_name,
    sort_order,
    metadata,
    admission_result,
    admission_result_date
FROM student_school_picks
WHERE student_id = %s AND is_active = true
ORDER BY list_group_name NULLS LAST, sort_order NULLS LAST, id
"""


def _school_pick_to_college(row):
    metadata = _decode_json_value(row["metadata"])
    if not isinstance(metadata, dict):
        metadata = {}

    return {
        "id": row["id"],
        "school_id": row["school_id"],
        "name": metadata.get("name"),
        "city": metadata.get("city"),
        "state": metadata.get("state"),
        "list_group_name": row["list_group_name"] or "My Colleges",
        "likelihood_category": row["student_likelihood_category"] or metadata.get("student_ranking"),
        "admission_result": row["admission_result"],
        "admission_result_date": row["admission_result_date"],
        "is_active": row["is_active"],
        "intended_major": metadata.get("intended_major") or metadata.get("intendedMajor"),
        "application_type": metadata.get("application_type"),
        "url": metadata.get("url_address"),
    }


def _fetch_college_list(student_id):
    with psycopg.connect(**DB_CONFIG["gm_schools"], row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(STUDENT_COLLEGE_LIST_SQL, (student_id,))
            rows = cursor.fetchall()

    colleges = [_school_pick_to_college(row) for row in rows]

    lists = {}
    for college in colleges:
        lists.setdefault(college["list_group_name"], []).append(college)

    return {"total": len(colleges), "lists": lists}


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


@app.get("/students/{student_id}/college-list")
def get_college_list(student_id: str):
    return _fetch_college_list(student_id)


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
    action_to_achieve: str
    eligibility_requirements: str
    include_in_common_app: bool = True
    include_in_uc_app: bool = True
    include_in_csu_app: bool = True


def _honor_request_to_record(honor: AddHonorRequest) -> dict:
    record = {
        "typeOfHonor": honor.honor_type,
        "honorTitle": honor.honor_title,
        "actionToAchieveHonor": honor.action_to_achieve,
        "eligibilityRequirementsHonor": honor.eligibility_requirements,
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


# --- SAT scores ---------------------------------------------------------
#
# sat_test has one row per student, addressed by its own student_id column
# (unlike activity_honor, where the table's id doubles as the student id).
# Score history lives in csu_info.sat_score (a JSON array); College Board ID
# lives alongside it at csu_info.collegeBoardId. future_testing_date_1 is a
# JSON object with up to three date slots (test1/test2/test3) despite the
# singular column name.

class MissingCollegeBoardIdError(Exception):
    pass


SELECT_SAT_ROW_SQL = """
SELECT number_of_past_sat_scores, future_sat_tests_plan_to_take, future_testing_date_1, csu_info
FROM sat_test
WHERE student_id = %s
"""

UPDATE_SAT_ROW_SQL = """
UPDATE sat_test
SET csu_info = %(csu_info)s,
    number_of_past_sat_scores = %(number_of_past_sat_scores)s,
    future_sat_tests_plan_to_take = %(future_sat_tests_plan_to_take)s,
    future_testing_date_1 = %(future_testing_date_1)s,
    is_have_sat_scores_report = true,
    updated_at = now()
WHERE student_id = %(student_id)s
"""


class AddSatScoreRequest(BaseModel):
    test_date: str
    total_score: int
    math_score: int
    reading_writing_score: int
    collegeboard_id: str | None = None
    has_future_test: bool = False
    future_test_date: str | None = None


def _append_sat_score(student_id: str, body: AddSatScoreRequest) -> dict:
    with psycopg.connect(**DB_CONFIG["student"], row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(SELECT_SAT_ROW_SQL, (student_id,))
            row = cursor.fetchone()
            if row is None:
                raise ValueError(f"No SAT record found for student {student_id}.")

            csu_info = _decode_json_value(row["csu_info"])
            if not isinstance(csu_info, dict):
                csu_info = {}

            existing_collegeboard_id = csu_info.get("collegeBoardId") or None
            if not existing_collegeboard_id and not body.collegeboard_id:
                raise MissingCollegeBoardIdError(
                    "This student doesn't have a College Board ID on file yet."
                )

            collegeboard_id = body.collegeboard_id or existing_collegeboard_id
            csu_info["collegeBoardId"] = collegeboard_id

            sat_scores = csu_info.get("sat_score")
            if not isinstance(sat_scores, list):
                sat_scores = []
            new_score = {
                "test_date": body.test_date,
                "total_score": body.total_score,
                "reading_writing_score": body.reading_writing_score,
                "math_score": body.math_score,
                "essay_scores": "",
                "essay_reading": "",
                "essay_analysis": "",
                "essay_writing": "",
            }
            sat_scores.append(new_score)
            csu_info["sat_score"] = sat_scores

            try:
                past_count = int(row["number_of_past_sat_scores"] or 0)
            except (TypeError, ValueError):
                past_count = 0
            new_past_count = past_count + 1

            future_dates = _decode_json_value(row["future_testing_date_1"])
            if not isinstance(future_dates, dict):
                future_dates = {}
            future_count = row["future_sat_tests_plan_to_take"] or 0

            if body.has_future_test and body.future_test_date:
                slot = next(
                    (key for key in ("test1", "test2", "test3") if not future_dates.get(key)),
                    "test3",
                )
                future_dates[slot] = body.future_test_date
                future_count += 1

            cursor.execute(
                UPDATE_SAT_ROW_SQL,
                {
                    "csu_info": json.dumps(csu_info),
                    "number_of_past_sat_scores": str(new_past_count),
                    "future_sat_tests_plan_to_take": future_count,
                    "future_testing_date_1": json.dumps(future_dates),
                    "student_id": student_id,
                },
            )
            connection.commit()

    return {
        "collegeboard_id": collegeboard_id,
        "number_of_past_sat_scores": new_past_count,
        "future_sat_tests_plan_to_take": future_count,
        "latest_score": new_score,
    }


@app.post("/students/{student_id}/sat-scores")
def add_sat_score(student_id: str, body: AddSatScoreRequest):
    try:
        result = _append_sat_score(student_id, body)
    except MissingCollegeBoardIdError as error:
        raise HTTPException(status_code=409, detail=str(error))
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))

    return {"student_id": student_id, **result}


# --- ACT scores ---------------------------------------------------------
#
# act_test has one row per student, addressed by its own student_id column,
# same convention as sat_test. Score history lives in csu_info.act_score (a
# JSON array); the ACT ID number is its own top-level column (act_id_number),
# unlike SAT's College Board ID which lives inside csu_info. future_testing_date_1
# is a JSON object with up to three date slots (test1/test2/test3). The
# highest_* / superscore_calculated_by_act columns are derived/aggregated
# elsewhere and are intentionally left untouched here.

class MissingActIdError(Exception):
    pass


SELECT_ACT_ROW_SQL = """
SELECT number_of_act_score_report, future_act_test_plan_to_take, future_testing_date_1,
       csu_info, act_id_number, have_taken_act_plus_writing_test
FROM act_test
WHERE student_id = %s
"""

UPDATE_ACT_ROW_SQL = """
UPDATE act_test
SET csu_info = %(csu_info)s,
    number_of_act_score_report = %(number_of_act_score_report)s,
    future_act_test_plan_to_take = %(future_act_test_plan_to_take)s,
    future_testing_date_1 = %(future_testing_date_1)s,
    act_id_number = %(act_id_number)s,
    have_taken_act_plus_writing_test = %(have_taken_act_plus_writing_test)s,
    is_have_act_score_report = true,
    updated_at = now()
WHERE student_id = %(student_id)s
"""


class AddActScoreRequest(BaseModel):
    test_date: str
    composite_score: int
    english_score: int
    math_score: int
    reading_score: int
    science_score: int
    took_writing_section: bool = False
    writing_score: int | None = None
    act_id_number: str | None = None
    has_future_test: bool = False
    future_test_date: str | None = None


def _append_act_score(student_id: str, body: AddActScoreRequest) -> dict:
    with psycopg.connect(**DB_CONFIG["student"], row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(SELECT_ACT_ROW_SQL, (student_id,))
            row = cursor.fetchone()
            if row is None:
                raise ValueError(f"No ACT record found for student {student_id}.")

            existing_act_id_number = row["act_id_number"] or None
            if existing_act_id_number == "0":
                existing_act_id_number = None
            if not existing_act_id_number and not body.act_id_number:
                raise MissingActIdError("This student doesn't have an ACT ID number on file yet.")

            act_id_number = body.act_id_number or existing_act_id_number

            csu_info = _decode_json_value(row["csu_info"])
            if not isinstance(csu_info, dict):
                csu_info = {}

            act_scores = csu_info.get("act_score")
            if not isinstance(act_scores, list):
                act_scores = []
            new_score = {
                "test_date": body.test_date,
                "composite_score": body.composite_score,
                "english": body.english_score,
                "mathematics": body.math_score,
                "reading": body.reading_score,
                "science": body.science_score,
                "writing": body.writing_score if body.took_writing_section else "",
            }
            act_scores.append(new_score)
            csu_info["act_score"] = act_scores

            new_past_count = (row["number_of_act_score_report"] or 0) + 1

            future_dates = _decode_json_value(row["future_testing_date_1"])
            if not isinstance(future_dates, dict):
                future_dates = {}
            future_count = row["future_act_test_plan_to_take"] or 0

            if body.has_future_test and body.future_test_date:
                slot = next(
                    (key for key in ("test1", "test2", "test3") if not future_dates.get(key)),
                    "test3",
                )
                future_dates[slot] = body.future_test_date
                future_count += 1

            have_taken_writing = bool(row["have_taken_act_plus_writing_test"]) or body.took_writing_section

            cursor.execute(
                UPDATE_ACT_ROW_SQL,
                {
                    "csu_info": json.dumps(csu_info),
                    "number_of_act_score_report": new_past_count,
                    "future_act_test_plan_to_take": future_count,
                    "future_testing_date_1": json.dumps(future_dates),
                    "act_id_number": act_id_number,
                    "have_taken_act_plus_writing_test": have_taken_writing,
                    "student_id": student_id,
                },
            )
            connection.commit()

    return {
        "act_id_number": act_id_number,
        "number_of_act_score_report": new_past_count,
        "future_act_test_plan_to_take": future_count,
        "latest_score": new_score,
    }


@app.post("/students/{student_id}/act-scores")
def add_act_score(student_id: str, body: AddActScoreRequest):
    try:
        result = _append_act_score(student_id, body)
    except MissingActIdError as error:
        raise HTTPException(status_code=409, detail=str(error))
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))

    return {"student_id": student_id, **result}


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
