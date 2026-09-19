import argparse
import html
import json
import os
from collections import Counter
from datetime import date, timedelta
from typing import Literal
from urllib.parse import urlencode

from dotenv import load_dotenv
from psycopg_pool import ConnectionPool
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

load_dotenv()

from apple_calendar import (
    AppleCalendarAuthError,
    AppleNotConnectedError,
    connect as connect_apple_calendar,
    disconnect as disconnect_apple_calendar,
    is_connected as is_apple_calendar_connected,
    sync_events as sync_apple_calendar_events,
)
from google_calendar import (
    GoogleNotConnectedError,
    build_authorize_url,
    complete_connection,
    create_pending_flow,
    disconnect as disconnect_google_calendar,
    is_connected as is_google_calendar_connected,
    pop_pending_flow,
    sync_events as sync_google_calendar_events,
)
from own_events import (
    add_own_event,
    delete_own_event,
    list_own_events,
    update_own_event,
)
from recommend import (
    ALLOWED_CATEGORIES,
    add_student_task,
    delete_recommendation,
    dismiss_recommendation,
    fetch_all_recommendations,
    fetch_recommendation,
    recommendations,
    set_recommendation_target_date,
    set_recommendation_title,
    update_recommendation_status,
)
from story import get_story, rebuild_story, save_edited_line


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

# The DB host is remote, and opening a fresh connection to it costs ~600ms
# (confirmed by measurement) -- with a couple dozen small queries spread
# across a single /readiness call, connecting fresh every time turned that
# into 15+ seconds. Pools are opened once at import and reused for the life
# of the process; every _fetch_*/_compute_* helper should borrow from these
# instead of calling psycopg.connect() directly.
DB_POOLS = {
    name: ConnectionPool(kwargs={**config, "row_factory": dict_row}, min_size=1, max_size=5, open=True)
    for name, config in DB_CONFIG.items()
}


# act_test/sat_test/high_school/activity_honor each have their own
# auto-increment `id` that does NOT line up with the student's id -- e.g. for
# student_id 13, act_test's row has id 8, and the row with id 13 belongs to a
# different student entirely. Join on each table's own `student_id` column
# instead, never `id`, or this silently pulls another student's data.
# programs_manager is different again: it's one row PER PROGRAM/application
# (a student applying to several schools has several rows, addressed by
# `program_id`, not `student_id`), so a plain join on student_id would fan
# out into multiple rows here. The LATERAL picks the most recently updated
# program as the representative one for this snapshot.
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
    hs.high_school_array,
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
LEFT JOIN act_test at ON at.student_id = pi.id
LEFT JOIN sat_test st ON st.student_id = pi.id
LEFT JOIN high_school hs ON hs.student_id = pi.id
LEFT JOIN activity_honor ah ON ah.student_id = pi.id
LEFT JOIN LATERAL (
    SELECT status, application_form, recommendation_letters, transcripts, resume, essays, reminders
    FROM programs_manager
    WHERE programs_manager.student_id = pi.id
    ORDER BY updated_at DESC NULLS LAST, id DESC
    LIMIT 1
) pm ON true
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


def _first_high_school_entry(raw_high_school_array):
    """high_school_array holds one entry per school the student attended (a
    transfer student can have more than one); we only need whichever school's
    term system applies now, so take the first entry, matching how a student
    with a single school (the common case) is naturally structured."""
    decoded = _decode_json_value(raw_high_school_array)
    if isinstance(decoded, list) and decoded and isinstance(decoded[0], dict):
        return decoded[0]
    return {}


def _normalize_term_system(classes_schedule):
    """classes_schedule is free-ish text from the intake form (seen in real
    data: 'Semester (2 final grades per year)', 'Semesters', 'Quarters',
    'Full year (1 final grade per year)', empty, or missing entirely) --
    normalize by keyword rather than exact match. Defaults to 'semester',
    the most common real value, when nothing recognizable is on file."""
    text = (classes_schedule or "").lower()
    if "quarter" in text:
        return "quarter"
    if "trimester" in text:
        return "trimester"
    if "semester" in text:
        return "semester"
    if "full" in text:
        return "full_year"
    return "semester"


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
            "classes_schedule": _first_high_school_entry(row["high_school_array"]).get("classes_schedule"),
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
    with DB_POOLS["gm_schools"].connection() as connection:
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
    with DB_POOLS["gm_schools"].connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(STUDENT_COLLEGE_LIST_SQL, (student_id,))
            rows = cursor.fetchall()

    colleges = [_school_pick_to_college(row) for row in rows]

    lists = {}
    for college in colleges:
        lists.setdefault(college["list_group_name"], []).append(college)

    return {"total": len(colleges), "lists": lists}


def _fetch_student_snapshot(student_id):
    with DB_POOLS["student"].connection() as connection:
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

# Dev-only: the dashboard HTML is opened straight from disk / a separate dev
# server, so the browser treats it as a different origin from this API.
# Tighten this to the real dashboard origin before deploying.
# allow_private_network is required on top of that: Chrome's Private Network
# Access check treats a file:// page as a public/unknown address space, and
# blocks (client-side, after a 400 on the preflight) its fetches to a private
# address like 127.0.0.1 unless the server explicitly opts in here.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_private_network=True,
)


@app.get("/students/{student_id}/college-list")
def get_college_list(student_id: str):
    return _fetch_college_list(student_id)


# --- "Schools you're looking at" characteristics -----------------------------
# A handful of chips describing the shape of the student's active list (public
# vs. private, size, setting, in-state vs. out-of-state), not a chip per
# school -- a 1-school list and a 20-school list should both read as a couple
# of characteristics, never a wall of chips. A characteristic only becomes a
# chip when it actually describes most of the list (for a single school,
# "most" is trivially all of it); a near-even split says nothing useful about
# the list as a whole, so it's left out rather than mislabeled.

SCHOOL_CHARACTERISTICS_SQL = """
SELECT s.institution_type, s.size_category, s.campus_setting, s.state,
       s.uc_system, s.csu_system, s.hbcu
FROM student_school_picks pick
JOIN schools s ON s.id = pick.school_id
WHERE pick.student_id = %s AND pick.is_active = true
"""

MAX_SCHOOL_CHARACTERISTICS = 5


def _fetch_school_characteristic_rows(student_id):
    with DB_POOLS["gm_schools"].connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(SCHOOL_CHARACTERISTICS_SQL, (student_id,))
            return cursor.fetchall()


def _fetch_student_home_state(student_id):
    # schools.state is a 2-letter code ("CA") -- personal_information.state is
    # the full name ("California"), so state_code is the one that lines up.
    with DB_POOLS["student"].connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT state_code FROM personal_information WHERE id = %s", (student_id,))
            row = cursor.fetchone()
    return row["state_code"] if row else None


def _majority_label(values):
    present = [v for v in values if v]
    if not present:
        return None
    label, count = Counter(present).most_common(1)[0]
    return label if count * 2 > len(present) else None


def _compute_school_characteristics(student_id):
    rows = _fetch_school_characteristic_rows(student_id)
    if not rows:
        return {"characteristics": []}

    chips = []

    institution_type = _majority_label([r["institution_type"] for r in rows])
    if institution_type:
        chips.append(institution_type)

    size_category = _majority_label([r["size_category"] for r in rows])
    if size_category:
        chips.append(size_category)

    campus_setting = _majority_label([r["campus_setting"] for r in rows])
    if campus_setting:
        chips.append(campus_setting)

    home_state = _fetch_student_home_state(student_id)
    school_states = [r["state"] for r in rows if r["state"]]
    if home_state and school_states:
        has_in_state = any(s == home_state for s in school_states)
        has_out_of_state = any(s != home_state for s in school_states)
        if has_in_state and has_out_of_state:
            chips.append("In-state + OOS")
        elif has_in_state:
            chips.append("In-state")
        elif has_out_of_state:
            chips.append("Out-of-state")

    if len(chips) < MAX_SCHOOL_CHARACTERISTICS:
        if all(r["uc_system"] for r in rows):
            chips.append("UC system")
        elif all(r["csu_system"] for r in rows):
            chips.append("CSU system")
        elif all(r["hbcu"] for r in rows):
            chips.append("HBCU")

    return {"characteristics": chips[:MAX_SCHOOL_CHARACTERISTICS]}


@app.get("/students/{student_id}/college-list/characteristics")
def get_college_list_characteristics(student_id: str):
    return _compute_school_characteristics(student_id)


# --- Road to CAM: real Colleges/Tests readiness ---------------------------
# Activities & Honors/Major Interests stay the hand-authored mock in the
# dashboard for now -- Profile, Colleges, Tests and Courses & Grades are the
# sections with a real, already-queried data source behind them.

COLLEGE_LIST_TARGET_MIN = 8  # "most students land on 8-12" -- reaching the low end counts as fully ready


# Profile spans four tables. personal_information.id happens to equal its own
# student_id (that's what STUDENT_SNAPSHOT_SQL joins on above), but
# demographics/contact_details/citizenship each have their own independent
# `id` primary key -- joining those on `id` silently pulls the wrong
# student's row, so this joins on student_id instead.
PROFILE_READINESS_SQL = """
SELECT
    pi.first_name, pi.last_name, pi.dob, pi.country, pi.is_have_legal_name,
    pi.sex, pi.sex_self_describe, pi.sex_self_consider,
    pi.address, pi.address_line_1, pi.city, pi.state, pi.zip_code,
    pi.is_should_send_mail, pi.is_share_different_first_name,
    pi.is_different_first_name_pronouns_as_he,
    pi.is_different_first_name_pronouns_as_she,
    pi.is_different_first_name_pronouns_as_they,
    pi.is_different_first_name_pronouns_as_other,
    d.us_armed_forces_status, d.is_dependent_us_military, d.all_apply_array,
    d.is_consider_hispanic_latino, d.best_group_latino_background_array,
    d.best_group_describe_racial_background_array,
    d.number_language_proficient, d.language_array,
    cd.phone_number, cd.country_code, cd.is_authorized_text_message_sent,
    cd.is_allowed_share_contact, cd.is_agree_csu_term,
    c.country AS citizenship_country, c.is_have_us_social_security_number,
    c.is_graduated_california_high_school, c.is_participate_cbo, c.your_cbo_array,
    c.is_financial_qualify_fee_waiver, c.indicator_economic_fee_waiver_array,
    c.csu_info AS citizenship_csu_info
FROM personal_information pi
LEFT JOIN demographics d ON d.student_id = pi.student_id
LEFT JOIN contact_details cd ON cd.student_id = pi.student_id
LEFT JOIN citizenship c ON c.student_id = pi.student_id
WHERE pi.student_id = %s
"""

PROFILE_PRONOUN_FIELDS = [
    "is_different_first_name_pronouns_as_he",
    "is_different_first_name_pronouns_as_she",
    "is_different_first_name_pronouns_as_they",
    "is_different_first_name_pronouns_as_other",
]

# CAASPP release + related certifications/authorizations. These all live as
# keys inside citizenship.csu_info -- a single free-form JSON blob, not their
# own columns -- confirmed against student_id 13's live data.
CAASPP_RELEASE_FIELDS = {
    "is_certify": "certification statement",
    "hereby_authorize_CD_release_CAASPP": "CAASPP release authorization",
    "is_authorize_CSU_release_contact_information": "release contact information authorization",
    "is_authorize_CSU_release_my_application": "release application authorization",
    "authorize_release_CASSID_for_tracking_UC_application": "release student ID for UC tracking authorization",
    "agree_with_guiding_principles": "guiding principles agreement",
}


def _is_filled(value):
    """Booleans and numbers count as answered regardless of their value --
    only NULL or an empty/whitespace string means the field was never filled
    in."""
    if value is None:
        return False
    if isinstance(value, str) and value.strip() == "":
        return False
    return True


def _fetch_profile_readiness_row(student_id):
    with DB_POOLS["student"].connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(PROFILE_READINESS_SQL, (student_id,))
            return cursor.fetchone()


def _compute_profile_readiness(student_id):
    row = _fetch_profile_readiness_row(student_id)
    if row is None:
        return {"pct": 0, "missing": "No profile on file yet."}

    checks = [
        ("first name", _is_filled(row["first_name"])),
        ("last name", _is_filled(row["last_name"])),
        ("date of birth", _is_filled(row["dob"])),
        ("country", _is_filled(row["country"])),
        ("legal name question", _is_filled(row["is_have_legal_name"])),
        ("sex", _is_filled(row["sex"])),
        ("sex self-description", _is_filled(row["sex_self_describe"])),
        ("sexual orientation", _is_filled(row["sex_self_consider"])),
        ("address", _is_filled(row["address"])),
        ("address line 1", _is_filled(row["address_line_1"])),
        ("city", _is_filled(row["city"])),
        ("state", _is_filled(row["state"])),
        ("zip code", _is_filled(row["zip_code"])),
        ("mailing preference", _is_filled(row["is_should_send_mail"])),
        ("different first name question", _is_filled(row["is_share_different_first_name"])),
        ("armed forces status", _is_filled(row["us_armed_forces_status"])),
        ("military affiliation", _is_filled(row["is_dependent_us_military"])),
        ("military/family questions", _is_filled(row["all_apply_array"])),
        ("Hispanic/Latino question", _is_filled(row["is_consider_hispanic_latino"])),
        ("Latino background", _is_filled(row["best_group_latino_background_array"])),
        ("racial background", _is_filled(row["best_group_describe_racial_background_array"])),
        ("number of languages", _is_filled(row["number_language_proficient"])),
        ("language details", _is_filled(row["language_array"])),
        ("phone number", _is_filled(row["phone_number"])),
        ("phone country code", _is_filled(row["country_code"])),
        ("text message authorization", _is_filled(row["is_authorized_text_message_sent"])),
        ("contact sharing authorization", _is_filled(row["is_allowed_share_contact"])),
        ("CSU terms agreement", _is_filled(row["is_agree_csu_term"])),
        ("citizenship country", _is_filled(row["citizenship_country"])),
        ("Social Security number question", _is_filled(row["is_have_us_social_security_number"])),
        ("high school graduation status", _is_filled(row["is_graduated_california_high_school"])),
        ("CBO participation question", _is_filled(row["is_participate_cbo"])),
        ("fee waiver eligibility question", _is_filled(row["is_financial_qualify_fee_waiver"])),
        ("fee waiver details", _is_filled(row["indicator_economic_fee_waiver_array"])),
    ]

    csu_info = _decode_json_value(row["citizenship_csu_info"])
    if not isinstance(csu_info, dict):
        csu_info = {}
    for field, label in CAASPP_RELEASE_FIELDS.items():
        checks.append((label, _is_filled(csu_info.get(field))))

    if row["is_share_different_first_name"]:
        checks.append(("preferred pronoun", any(row[field] for field in PROFILE_PRONOUN_FIELDS)))

    if row["is_participate_cbo"]:
        checks.append(("CBO details", _is_filled(row["your_cbo_array"])))

    missing = [label for label, complete in checks if not complete]
    pct = round((len(checks) - len(missing)) / len(checks) * 100)

    if not missing:
        return {"pct": 100, "missing": "Complete!"}
    return {"pct": pct, "missing": "Some things are missing from your profile!"}


# --- "Verified by you" / send-to-counselor gate ------------------------------
# page_status tracks one row per intake page, addressed by student_id +
# page_name -- a row only exists once the student has actually opened that
# page, so a page the student hasn't touched yet reads the same as one left
# incomplete. This is independent of each section's field-by-field pct above
# -- pct measures how much of the underlying data is filled in, this measures
# whether the student (student_completed) or the counselor
# (counselor_verified) has explicitly marked every required page for that
# section done.

PROFILE_PAGE_NAMES = (
    "Basic Information",
    "Contact Details",
    "Demographics",
    "Citizenship-Residency",
    "CBOs & Fee Waiver",
    "Other Information",
)

# Activities & Honors' page list isn't gated by any selection -- all four are
# always part of that screen's own left-nav checklist.
ACTIVITIES_PAGE_NAMES = (
    "Activities",
    "Honors & Awards",
    "EOP and Other Info",
    "Responsibilities and Circumstances",
)

# Tests' page list mirrors exactly the "Indicate all tests you wish to
# report" checkboxes in all_tests_wish_report_array -- a test type the
# student didn't check has no page to require.
TEST_TYPE_PAGE_NAMES = {
    "is_SAT": "SAT Tests",
    "is_ACT": "ACT Tests",
    "is_AP_Subject": "AP Subject Tests",
    "is_IB_Subject": "IB Subject Tests",
    "is_CLEP": "CLEP Tests",
    "is_TOEFL": "TOEFL iBT",
    "is_PTE": "PTE Academic",
    "is_IELTS": "IELTS",
    "is_DuoLingo": "DuoLingo",
}


def _fetch_page_status_rows(student_id, page_names):
    with DB_POOLS["student"].connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT page_name, student_completed, student_completed_at, counselor_verified FROM page_status WHERE student_id = %(student_id)s AND page_name = ANY(%(page_names)s)",
                {"student_id": student_id, "page_names": list(page_names)},
            )
            return cursor.fetchall()


def _self_verification_status(student_id, required_pages):
    if not required_pages:
        return {"verified_by_student_at": None, "counselor_verified": False}

    rows = _fetch_page_status_rows(student_id, required_pages)
    by_page = {row["page_name"]: row for row in rows}

    verified_by_student_at = None
    if all(by_page.get(name, {}).get("student_completed") for name in required_pages):
        completed_at = [by_page[name]["student_completed_at"] for name in required_pages]
        timestamps = [ts for ts in completed_at if ts is not None]
        if timestamps:
            verified_by_student_at = max(timestamps).date().isoformat()

    counselor_verified = all(by_page.get(name, {}).get("counselor_verified") for name in required_pages)

    return {"verified_by_student_at": verified_by_student_at, "counselor_verified": counselor_verified}


def _compute_profile_self_verification(student_id):
    return _self_verification_status(student_id, PROFILE_PAGE_NAMES)


def _compute_activities_self_verification(student_id):
    return _self_verification_status(student_id, ACTIVITIES_PAGE_NAMES)


def _compute_tests_self_verification(student_id, general):
    required_pages = ["Test General Info"]

    if general is not None and general["is_wish_self_report_scores"]:
        wished = _decode_json_value(general["all_tests_wish_report_array"])
        if not isinstance(wished, dict):
            wished = {}
        for flag, page_name in TEST_TYPE_PAGE_NAMES.items():
            if wished.get(flag):
                required_pages.append(page_name)

    return _self_verification_status(student_id, required_pages)


def _compute_courses_self_verification(student_id, grades_row, current_grade):
    required_pages = ["General Info"]
    for level in (9, 10, 11, 12):
        if current_grade is not None and current_grade < level:
            continue
        required_pages.append(f"{level}th Grade")

    if grades_row is not None and _has_real_courses(grades_row["college_course_array"]):
        required_pages.append("College Courses")

    return _self_verification_status(student_id, required_pages)


# A student's overall "streak" -- consecutive weeks (including the current
# one) with at least one page_status row touched. Deliberately global across
# every page, not per-section, since momentum is about the student as a
# whole, not any one Road to CAM card.
def _compute_streak_weeks(student_id):
    with DB_POOLS["student"].connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT updated_at FROM page_status WHERE student_id = %s", (student_id,))
            rows = cursor.fetchall()

    active_weeks = {row["updated_at"].isocalendar()[:2] for row in rows if row["updated_at"]}

    weeks = 0
    cursor_date = date.today()
    while cursor_date.isocalendar()[:2] in active_weeks:
        weeks += 1
        cursor_date -= timedelta(weeks=1)
    return weeks


# --- Family readiness --------------------------------------------------------
# household/parent_no1/parent_no2/siblings are each one row per student,
# addressed by student_id. Step-parent detail only matters when the household
# actually lists step-parents; a parent's employment detail only matters
# while that parent is living; sibling detail only matters once the student
# has said they have siblings at all -- same "required only if chosen"
# convention as the other sections.

HOUSEHOLD_SQL = """
SELECT who_in_household_array, household_size, whom_live_permanently,
       parent_martial_status, household_income, is_have_any_children,
       highest_level_education_parent, how_many_children,
       is_listing_step_parents, how_many_step_parents, legal_guardian,
       year_of_divorce, csu_info
FROM household
WHERE student_id = %s
"""

PARENT_FIELDS_SQL = """
SELECT is_parent_{n}_living, relationship_type, first_name, last_name,
       occupation, highest_level_education, current_employer, job_title
FROM parent_no{n}
WHERE student_id = %s
"""

STEP_PARENT_FIELDS_SQL = """
SELECT first_name, last_name, step_parent_relationship, step_parent_is_living
FROM step_parent_no{n}
WHERE student_id = %s
"""

SIBLINGS_SQL = "SELECT number_of_siblings, siblings_array FROM siblings WHERE student_id = %s"

HOUSEHOLD_CSU_INFO_FIELDS = {
    "household_income_information_statements": "household income statement",
    "parent_gross_income": "parent gross income",
    "parent_untaxed_income": "parent untaxed income",
    "gross_income": "student gross income",
    "untaxed_income": "student untaxed income",
    "parent_1_highest_level_education": "parent 1 education level (financial section)",
    "parent_2_highest_level_education": "parent 2 education level (financial section)",
}


def _is_meaningfully_filled(value):
    """Same as _is_filled, but also treats the literal string "null" as
    unfilled -- household.legal_guardian stores that as text, not a real
    NULL, when the question hasn't been answered."""
    if isinstance(value, str) and value.strip().lower() == "null":
        return False
    return _is_filled(value)


def _fetch_family_row(sql, student_id):
    with DB_POOLS["student"].connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, (student_id,))
            return cursor.fetchone()


def _compute_family_readiness(student_id):
    household = _fetch_family_row(HOUSEHOLD_SQL, student_id)
    parent1 = _fetch_family_row(PARENT_FIELDS_SQL.format(n=1), student_id)
    parent2 = _fetch_family_row(PARENT_FIELDS_SQL.format(n=2), student_id)
    siblings = _fetch_family_row(SIBLINGS_SQL, student_id)

    if household is None and parent1 is None and parent2 is None and siblings is None:
        return {"pct": 0, "missing": "No family information on file yet."}

    checks = []

    if household is not None:
        checks += [
            ("household members question", _is_filled(household["who_in_household_array"])),
            ("household size", _is_filled(household["household_size"])),
            ("who the student lives with", _is_filled(household["whom_live_permanently"])),
            ("parents' marital status", _is_filled(household["parent_martial_status"])),
            ("household income", _is_filled(household["household_income"])),
            ("student's own children question", _is_filled(household["is_have_any_children"])),
            ("parents' highest level of education", _is_filled(household["highest_level_education_parent"])),
            ("number of children in household", _is_filled(household["how_many_children"])),
            ("step-parents question", _is_filled(household["is_listing_step_parents"])),
            ("legal guardian", _is_meaningfully_filled(household["legal_guardian"])),
        ]

        if "divorce" in (household["parent_martial_status"] or "").lower() or "separat" in (household["parent_martial_status"] or "").lower():
            checks.append(("year of divorce/separation", _is_filled(household["year_of_divorce"])))

        listing_step_parents = _csu_bool(household["is_listing_step_parents"])
        if listing_step_parents:
            checks.append(("number of step-parents", _is_filled(household["how_many_step_parents"])))

        csu_info = _decode_json_value(household["csu_info"])
        if not isinstance(csu_info, dict):
            csu_info = {}
        for field, label in HOUSEHOLD_CSU_INFO_FIELDS.items():
            checks.append((label, _is_filled(csu_info.get(field))))

        if listing_step_parents:
            for n in (1, 2):
                step = _fetch_family_row(STEP_PARENT_FIELDS_SQL.format(n=n), student_id)
                checks.append((f"step-parent {n} name", step is not None and _is_filled(step["first_name"]) and _is_filled(step["last_name"])))
                checks.append((f"step-parent {n} relationship", step is not None and _is_filled(step["step_parent_relationship"])))

    for n, parent in ((1, parent1), (2, parent2)):
        if parent is None:
            continue
        is_living = parent[f"is_parent_{n}_living"]
        checks += [
            (f"parent {n} living status", _is_filled(is_living)),
            (f"parent {n} relationship", _is_filled(parent["relationship_type"])),
            (f"parent {n} first name", _is_filled(parent["first_name"])),
            (f"parent {n} last name", _is_filled(parent["last_name"])),
        ]
        if is_living:
            checks += [
                (f"parent {n} occupation", _is_filled(parent["occupation"])),
                (f"parent {n} education level", _is_filled(parent["highest_level_education"])),
                (f"parent {n} employer", _is_filled(parent["current_employer"])),
                (f"parent {n} job title", _is_filled(parent["job_title"])),
            ]

    if siblings is not None:
        checks.append(("number of siblings", _is_filled(siblings["number_of_siblings"])))
        if (siblings["number_of_siblings"] or 0) > 0:
            checks.append(("sibling details", _has_real_entries(siblings["siblings_array"], "fullName")))

    if not checks:
        return {"pct": 0, "missing": "No family information on file yet."}

    missing = [label for label, complete in checks if not complete]
    pct = round((len(checks) - len(missing)) / len(checks) * 100)

    if not missing:
        return {"pct": 100, "missing": "Complete!"}
    return {"pct": pct, "missing": "Some things are missing from your family information."}


def _compute_colleges_readiness(student_id):
    total = _fetch_college_list(student_id)["total"]
    pct = min(100, round(total / COLLEGE_LIST_TARGET_MIN * 100)) if total else 0
    return {
        "pct": pct,
        "missing": f"{total} school{'' if total == 1 else 's'} saved. Most students land on 8–12.",
    }


# test_general_info.all_tests_wish_report_array is the single source of truth
# for which tests a student is reporting (it mirrors the "Indicate all tests
# you wish to report" checkboxes) -- a test type left unchecked there is
# simply not required, no matter how empty its own table is. All of these
# tables are addressed by their own student_id column, queried directly
# rather than through _fetch_student_snapshot's broader join.

ALL_TEST_TYPE_KEYS = (
    "is_SAT", "is_ACT", "is_AP_Subject", "is_IB_Subject", "is_CLEP",
    "is_TOEFL", "is_PTE", "is_IELTS", "is_DuoLingo",
)

TEST_GENERAL_INFO_SQL = """
SELECT is_wish_self_report_scores, all_tests_wish_report_array, is_promotion_within_educational_system
FROM test_general_info
WHERE student_id = %s
"""

SAT_TEST_READINESS_SQL = "SELECT is_have_sat_scores_report, future_testing_date_1 FROM sat_test WHERE student_id = %s"
ACT_TEST_READINESS_SQL = "SELECT is_have_act_score_report, future_testing_date_1, have_taken_act_plus_writing_test FROM act_test WHERE student_id = %s"
AP_SUBJECT_TEST_SQL = "SELECT is_have_ap_exam_report, number_of_ap_test_report FROM act_subject_test WHERE student_id = %s"
IB_SUBJECT_TEST_SQL = "SELECT is_have_ib_exam_report, number_of_ib_test_report, is_completed_full_ib FROM ib_subject_test WHERE student_id = %s"
CLEP_TEST_SQL = "SELECT id FROM clep_test WHERE student_id = %s"
DOULINGO_TEST_SQL = "SELECT id FROM doulingo_test WHERE student_id = %s"
IELTS_PTE_SQL = "SELECT is_not_required_pte_test, is_not_required_ielts FROM ielts_pte WHERE student_id = %s"
OTHER_TEST_SQL = """
SELECT is_not_required_toefl_test, is_have_advanced_level_exam_wish_report,
       is_have_predicted_advanced_level_exam_wish_report
FROM other_test
WHERE student_id = %s
"""


def _fetch_test_row(sql, student_id):
    with DB_POOLS["student"].connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, (student_id,))
            return cursor.fetchone()


def _compute_tests_readiness(student_id, general):
    if general is None:
        return {"pct": 0, "missing": "Answer whether you want to self-report test scores."}

    checks = [
        ("wish to self-report test scores question", _is_filled(general["is_wish_self_report_scores"])),
        ("international leaving exam question", _is_filled(general["is_promotion_within_educational_system"])),
    ]

    other_row = None  # shared by the advanced-level and TOEFL checks below
    if general["is_promotion_within_educational_system"]:
        other_row = _fetch_test_row(OTHER_TEST_SQL, student_id)
        checks += [
            ("advanced-level exam report question", other_row is not None and _is_filled(other_row["is_have_advanced_level_exam_wish_report"])),
            ("predicted advanced-level exam report question", other_row is not None and _is_filled(other_row["is_have_predicted_advanced_level_exam_wish_report"])),
        ]

    if general["is_wish_self_report_scores"]:
        wished = _decode_json_value(general["all_tests_wish_report_array"])
        if not isinstance(wished, dict):
            wished = {}

        # Saying "yes" to self-reporting without checking any test box is an
        # incomplete answer, not a valid "nothing to report" state -- it must
        # count against the percentage rather than silently skip every check
        # below and read as done.
        checks.append(("at least one test type selected", any(wished.get(key) for key in ALL_TEST_TYPE_KEYS)))

        if wished.get("is_SAT"):
            sat = _fetch_test_row(SAT_TEST_READINESS_SQL, student_id)
            has_score = sat is not None and bool(sat["is_have_sat_scores_report"])
            has_future = sat is not None and bool(sat["future_testing_date_1"])
            checks.append(("SAT score reported or a future test date on file", has_score or has_future))

        if wished.get("is_ACT"):
            act = _fetch_test_row(ACT_TEST_READINESS_SQL, student_id)
            has_score = act is not None and bool(act["is_have_act_score_report"])
            has_future = act is not None and bool(act["future_testing_date_1"])
            checks.append(("ACT score reported or a future test date on file", has_score or has_future))
            checks.append(("ACT Plus Writing question", act is not None and _is_filled(act["have_taken_act_plus_writing_test"])))

        if wished.get("is_AP_Subject"):
            ap = _fetch_test_row(AP_SUBJECT_TEST_SQL, student_id)
            checks.append(("AP exam report question", ap is not None and _is_filled(ap["is_have_ap_exam_report"])))
            if ap is not None and ap["is_have_ap_exam_report"]:
                checks.append(("AP test count on file", bool(ap["number_of_ap_test_report"])))

        if wished.get("is_IB_Subject"):
            ib = _fetch_test_row(IB_SUBJECT_TEST_SQL, student_id)
            checks.append(("IB exam report question", ib is not None and _is_filled(ib["is_have_ib_exam_report"])))
            checks.append(("IB program completion question", ib is not None and _is_filled(ib["is_completed_full_ib"])))
            if ib is not None and ib["is_have_ib_exam_report"]:
                checks.append(("IB test count on file", bool(ib["number_of_ib_test_report"])))

        if wished.get("is_CLEP"):
            clep = _fetch_test_row(CLEP_TEST_SQL, student_id)
            checks.append(("CLEP test information on file", clep is not None))

        if wished.get("is_TOEFL"):
            if other_row is None:
                other_row = _fetch_test_row(OTHER_TEST_SQL, student_id)
            checks.append(("TOEFL requirement question", other_row is not None and _is_filled(other_row["is_not_required_toefl_test"])))

        ielts_pte_row = None
        if wished.get("is_PTE") or wished.get("is_IELTS"):
            ielts_pte_row = _fetch_test_row(IELTS_PTE_SQL, student_id)
        if wished.get("is_PTE"):
            checks.append(("PTE requirement question", ielts_pte_row is not None and _is_filled(ielts_pte_row["is_not_required_pte_test"])))
        if wished.get("is_IELTS"):
            checks.append(("IELTS requirement question", ielts_pte_row is not None and _is_filled(ielts_pte_row["is_not_required_ielts"])))

        if wished.get("is_DuoLingo"):
            duolingo = _fetch_test_row(DOULINGO_TEST_SQL, student_id)
            checks.append(("DuoLingo test information on file", duolingo is not None))

    missing = [label for label, complete in checks if not complete]
    pct = round((len(checks) - len(missing)) / len(checks) * 100)

    if not missing:
        return {"pct": 100, "missing": "Complete!"}
    return {"pct": pct, "missing": "Some things are missing from your tests."}


# --- Courses & Grades readiness ---------------------------------------------
# course_general_info and grade_and_college_course are both one-row-per-student
# tables, addressed by student_id (like sat_test/act_test, unlike
# activity_honor's id-doubles-as-student-id convention). Only fields that are
# actually required contribute to the percentage -- optional/free-form detail
# (e.g. specify_language_instruction when it doesn't apply) never drags the
# score down just because it's blank.

COURSE_GENERAL_INFO_SQL = """
SELECT
    is_able_obtain_copy_transcript,
    is_counselor_submit_transcript,
    is_transcript_show_grade_completed,
    is_counselor_submit_transcript_2,
    is_take_high_school_math_in_grade_7_or_8,
    is_take_high_school_english_in_grade_7_or_8,
    is_attend_school_outside_us_6_through_8,
    language_instruction,
    specify_language_instruction,
    take_high_school_math_array,
    take_high_school_english_array
FROM course_general_info
WHERE student_id = %s
"""

GRADE_AND_COLLEGE_COURSE_SQL = """
SELECT
    grade_9_course_array, is_reported_all_grade_9,
    grade_10_course_array, is_reported_all_grade_10,
    grade_11_course_array, is_reported_all_grade_11,
    grade_12_course_array, is_reported_all_grade_12,
    college_course_array, is_finish_adding_all_college_grade,
    course_scheduling_system_is_using
FROM grade_and_college_course
WHERE student_id = %s
"""


def _fetch_course_general_info_row(student_id):
    with DB_POOLS["student"].connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(COURSE_GENERAL_INFO_SQL, (student_id,))
            return cursor.fetchone()


def _fetch_grade_and_college_course_row(student_id):
    with DB_POOLS["student"].connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(GRADE_AND_COLLEGE_COURSE_SQL, (student_id,))
            return cursor.fetchone()


def _has_real_entries(raw_array, name_field):
    """Course arrays keep a placeholder entry (blank name field) once a row
    exists but nothing's been entered yet, so an empty-looking array isn't
    necessarily [] -- only an entry with an actual name counts. The 7th/8th
    grade math/English arrays use `name`; the per-grade course arrays use
    `courseName` -- different shapes, same intake form family."""
    decoded = _decode_json_value(raw_array)
    if not isinstance(decoded, list):
        return False
    return any(isinstance(entry, dict) and (entry.get(name_field) or "").strip() for entry in decoded)


def _has_real_courses(raw_course_array):
    return _has_real_entries(raw_course_array, "courseName")


def _current_grade_level(student_id):
    try:
        snapshot = _fetch_student_snapshot(student_id)
    except ValueError:
        return None
    grade = _compute_class_year_info(snapshot["personal_information"]["year_finish_high_school"])["grade"]
    return grade if isinstance(grade, int) else None


def _compute_courses_readiness(general, grades_row, current_grade):
    if general is None and grades_row is None:
        return {"pct": 0, "missing": "No course information on file yet.", "term_system": None}

    checks = []

    if general is not None:
        checks += [
            ("transcript availability question", _is_filled(general["is_able_obtain_copy_transcript"])),
            ("counselor transcript submission question", _is_filled(general["is_counselor_submit_transcript"])),
            ("transcript grade completion question", _is_filled(general["is_transcript_show_grade_completed"])),
            ("counselor transcript submission confirmation", _is_filled(general["is_counselor_submit_transcript_2"])),
            ("7th/8th grade math question", _is_filled(general["is_take_high_school_math_in_grade_7_or_8"])),
            ("7th/8th grade English question", _is_filled(general["is_take_high_school_english_in_grade_7_or_8"])),
            ("schooling outside the US (grades 6-8) question", _is_filled(general["is_attend_school_outside_us_6_through_8"])),
            ("language of instruction", _is_filled(general["language_instruction"])),
        ]
        if general["is_take_high_school_math_in_grade_7_or_8"]:
            checks.append(("7th/8th grade math course detail", _has_real_entries(general["take_high_school_math_array"], "name")))
        if general["is_take_high_school_english_in_grade_7_or_8"]:
            checks.append(("7th/8th grade English course detail", _has_real_entries(general["take_high_school_english_array"], "name")))
        if (general["language_instruction"] or "").strip().lower() == "other":
            checks.append(("language of instruction detail", _is_filled(general["specify_language_instruction"])))

    term_system = None
    if grades_row is not None:
        checks.append(("course scheduling system", _is_filled(grades_row["course_scheduling_system_is_using"])))
        term_system = _normalize_term_system(grades_row["course_scheduling_system_is_using"])

        # Only grades the student has actually reached count against them --
        # a 10th grader isn't missing 11th/12th grade courses yet.
        for level in (9, 10, 11, 12):
            if current_grade is not None and current_grade < level:
                continue
            checks.append((f"grade {level} courses reported", bool(grades_row[f"is_reported_all_grade_{level}"])))
            checks.append((f"grade {level} course list", _has_real_courses(grades_row[f"grade_{level}_course_array"])))

        if _has_real_courses(grades_row["college_course_array"]):
            checks.append(("college course list completion", bool(grades_row["is_finish_adding_all_college_grade"])))

    if not checks:
        return {"pct": 0, "missing": "No course information on file yet.", "term_system": term_system}

    missing = [label for label, complete in checks if not complete]
    pct = round((len(checks) - len(missing)) / len(checks) * 100)

    if not missing:
        return {"pct": 100, "missing": "Complete!", "term_system": term_system}
    return {"pct": pct, "missing": "Some things are missing from your courses & grades.", "term_system": term_system}


# --- Activities & Honors readiness -------------------------------------------
# activity_honor is one row per student (student_id, not id -- same convention
# as the STUDENT_SNAPSHOT_SQL fix above). Honors have no "do you have any
# honors to report" gate the way activities do
# (is_have_any_activity_to_report), so an empty honor_array is left alone --
# reporting honors is purely opt-in, unlike activities.
#
# educational_program_participation.csu_info bundles CSU's program and
# background questions into one JSON blob -- note its yes/no answers are the
# strings "true"/"false" or "Yes"/"No", not real JSON booleans. Each base
# program/background question is required, and its follow-up detail (e.g.
# which year a program was attended) only becomes required once the base
# question is answered "yes", the same conditional-requirement convention as
# CAASPP_RELEASE_FIELDS above.

ACTIVITY_HONOR_READINESS_SQL = """
SELECT is_have_any_activity_to_report, activity_array, honor_array
FROM activity_honor
WHERE student_id = %s
"""

EDUCATIONAL_PROGRAM_PARTICIPATION_SQL = "SELECT csu_info FROM educational_program_participation WHERE student_id = %s"

EDUCATIONAL_PROGRAM_BASE_FIELDS = {
    "avid": "AVID program question",
    "upward_bound": "Upward Bound program question",
    "talent_search": "Talent Search program question",
    "puente_project": "Puente Project question",
    "ilp": "Independent Living Program (ILP) question",
    "MESA_project": "MESA Project question",
    "other_program": "other program question",
    "federal_outreach_program": "federal outreach program question",
    "eap_eop_s_camp": "EAP/EOP summer camp question",
    "umoja_project": "Umoja Project question",
    "average_hours_worked_per_week": "average hours worked per week",
    "is_more_25_percent_work_related_major": "work related to major question",
    "average_hours_activities_per_week": "average hours on activities per week",
    "is_leadership_positions": "leadership positions question",
    "wish_to_apply_EOP": "EOP application interest question",
    "where_plan_to_live": "planned living situation question",
    "number_brothers_and_sisters_k12": "siblings in K-12 count",
    "number_brothers_and_sisters_college": "siblings in college count",
    "number_brothers_and_sisters_received_bachelor_degree": "siblings with a bachelor's degree count",
    "languages_spoken_in_home": "languages spoken at home",
    "is_received_income_from_public_assistance_program": "public assistance income question",
    "is_participated_in_publicly_funded_programs": "publicly funded programs question",
    "is_work_primarily_to_contribute": "work-to-contribute question",
}

# program flag -> its "what year did you participate" follow-up, only
# required once the program itself is flagged "true"
EDUCATIONAL_PROGRAM_YEAR_FIELDS = {
    "avid": "year_participated_in_AVID",
    "upward_bound": "year_participated_in_upward_bound",
    "talent_search": "year_participated_in_talent_search",
    "puente_project": "year_participated_in_puente_project",
    "ilp": "year_participated_in_ilp",
    "MESA_project": "year_participated_in_MESA_project",
    "other_program": "year_participated_in_other_program",
}


def _csu_bool(value):
    """csu_info blobs store yes/no answers as the strings "true"/"false" or
    "Yes"/"No", not real JSON booleans."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes")
    return bool(value)


def _compute_activities_readiness(student_id, activity_row):
    program_row = _fetch_test_row(EDUCATIONAL_PROGRAM_PARTICIPATION_SQL, student_id)
    if activity_row is None and program_row is None:
        return {"pct": 0, "missing": "No activities or honors on file yet."}

    checks = []

    if activity_row is not None:
        checks.append(("activity report question", _is_filled(activity_row["is_have_any_activity_to_report"])))
        if activity_row["is_have_any_activity_to_report"]:
            checks.append(("activity list", _has_real_entries(activity_row["activity_array"], "programName")))

    if program_row is not None:
        csu_info = _decode_json_value(program_row["csu_info"])
        if not isinstance(csu_info, dict):
            csu_info = {}

        for field, label in EDUCATIONAL_PROGRAM_BASE_FIELDS.items():
            checks.append((label, _is_filled(csu_info.get(field))))

        for flag, year_field in EDUCATIONAL_PROGRAM_YEAR_FIELDS.items():
            if _csu_bool(csu_info.get(flag)):
                checks.append((f"{flag} participation year", _is_filled(csu_info.get(year_field))))

        if _csu_bool(csu_info.get("is_received_income_from_public_assistance_program")):
            checks.append(("public assistance years received", _is_filled(csu_info.get("number_year_received_income"))))
            checks.append(("public assistance aid type", _is_filled(csu_info.get("type_of_aid"))))

        if _csu_bool(csu_info.get("is_participated_in_publicly_funded_programs")):
            checks.append(("publicly funded programs detail", _is_filled(csu_info.get("publicly_funded_programs"))))

        if _csu_bool(csu_info.get("is_work_primarily_to_contribute")):
            checks.append(("work-to-contribute detail", _is_filled(csu_info.get("work_primarily_to_contribute"))))

        if _csu_bool(csu_info.get("wish_to_apply_EOP")):
            checks.append(("EOP enrollment question", _is_filled(csu_info.get("is_enrolled_EOP"))))
            if _csu_bool(csu_info.get("is_enrolled_EOP")):
                checks.append(("EOP campus", _is_filled(csu_info.get("campus_enrolled_EOP"))))

    if not checks:
        return {"pct": 0, "missing": "No activities or honors on file yet."}

    missing = [label for label, complete in checks if not complete]
    pct = round((len(checks) - len(missing)) / len(checks) * 100)

    if not missing:
        return {"pct": 100, "missing": "Complete!"}
    return {"pct": pct, "missing": "Some things are missing from your activities & honors."}


# --- "Spike / distinction" pillar --------------------------------------------
# A strength score, not a readiness percentage -- there's nothing to "finish"
# here. Deliberately cheap: it reuses the same activity_honor row the
# Activities & Honors readiness check already fetches (no extra query) and
# just aggregates the JSON arrays already sitting in Python, so it stays fast
# even computed fresh for hundreds of students on every dashboard load. Takes
# the MAX of each signal across entries rather than summing, so one deep,
# recognized thing scores higher than a resume padded with shallow one-offs
# -- which is what "spike" is supposed to reward.

SPIKE_RECOGNITION_POINTS = {
    "isSchoolLevelRecognition": 10,
    "isStateLevelRecognition": 25,
    "isNational": 50,
    "isInternationalLevelRecognition": 70,
}

SPIKE_TENURE_FIELDS = (
    "isGrade9ParticipationLevels",
    "isGrade10ParticipationLevels",
    "isGrade11ParticipationLevels",
    "isGrade12ParticipationLevels",
)
SPIKE_TENURE_MIN_GRADES = 3
SPIKE_TENURE_POINTS = 10

SPIKE_LEADERSHIP_POINTS = 15

SPIKE_DEPTH_HIGH_HOURS_PER_YEAR = 300
SPIKE_DEPTH_HIGH_POINTS = 15
SPIKE_DEPTH_LOW_HOURS_PER_YEAR = 150
SPIKE_DEPTH_LOW_POINTS = 8


def _compute_spike_pct(row):
    if row is None:
        return 0

    honors = _decode_json_value(row["honor_array"])
    if not isinstance(honors, list):
        honors = []

    recognition_points = 0
    for honor in honors:
        if not isinstance(honor, dict):
            continue
        for field, points in SPIKE_RECOGNITION_POINTS.items():
            if _csu_bool(honor.get(field)):
                recognition_points = max(recognition_points, points)

    activities = _decode_json_value(row["activity_array"])
    if not isinstance(activities, list):
        activities = []

    leadership_points = 0
    depth_points = 0
    tenure_points = 0
    for activity in activities:
        if not isinstance(activity, dict):
            continue

        if _csu_bool(activity.get("isInvolvedLeadershipRole")):
            leadership_points = SPIKE_LEADERSHIP_POINTS

        try:
            yearly_hours = float(activity.get("hoursPerWeek")) * float(activity.get("weeksPerYear"))
        except (TypeError, ValueError):
            yearly_hours = 0
        if yearly_hours >= SPIKE_DEPTH_HIGH_HOURS_PER_YEAR:
            depth_points = max(depth_points, SPIKE_DEPTH_HIGH_POINTS)
        elif yearly_hours >= SPIKE_DEPTH_LOW_HOURS_PER_YEAR:
            depth_points = max(depth_points, SPIKE_DEPTH_LOW_POINTS)

        grades_spanned = sum(1 for field in SPIKE_TENURE_FIELDS if _csu_bool(activity.get(field)))
        if grades_spanned >= SPIKE_TENURE_MIN_GRADES:
            tenure_points = SPIKE_TENURE_POINTS

    return min(100, recognition_points + leadership_points + depth_points + tenure_points)


@app.get("/students/{student_id}/readiness")
def get_readiness(student_id: str):
    profile = _compute_profile_readiness(student_id)
    profile.update(_compute_profile_self_verification(student_id))

    # Each of these rows is needed by both a section's pct and its
    # self-verification (and activity_honor by Spike too) -- fetched once
    # here and passed through, instead of every function re-querying the same
    # row, which is what made /readiness slow on a remote DB.
    test_general = _fetch_test_row(TEST_GENERAL_INFO_SQL, student_id)
    tests = _compute_tests_readiness(student_id, test_general)
    tests.update(_compute_tests_self_verification(student_id, test_general))

    course_general = _fetch_course_general_info_row(student_id)
    grades_row = _fetch_grade_and_college_course_row(student_id)
    current_grade = _current_grade_level(student_id)
    courses = _compute_courses_readiness(course_general, grades_row, current_grade)
    courses.update(_compute_courses_self_verification(student_id, grades_row, current_grade))

    activity_row = _fetch_test_row(ACTIVITY_HONOR_READINESS_SQL, student_id)
    activities = _compute_activities_readiness(student_id, activity_row)
    activities.update(_compute_activities_self_verification(student_id))

    return {
        "profile": profile,
        "family": _compute_family_readiness(student_id),
        "colleges": _compute_colleges_readiness(student_id),
        "tests": tests,
        "courses": courses,
        "activities": activities,
        "spike": {"pct": _compute_spike_pct(activity_row)},
    }


@app.get("/students/{student_id}/streak")
def get_streak(student_id: str):
    return {"weeks": _compute_streak_weeks(student_id)}


# --- Class year / grade / season -------------------------------------------
# US school year runs roughly Aug-May; a student who finishes high school in
# calendar year Y is in 12th grade for the school year ending in Y, 11th for
# the one ending in Y-1, etc. July/August is treated as the start of the next
# school year (matches when most schools actually resume).

def _ordinal(n):
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _compute_class_year_info(year_finish_high_school):
    if year_finish_high_school is None:
        return {"year_finish_high_school": None, "grade": None, "grade_label": None, "season": None}

    today = date.today()
    school_year_end = today.year + 1 if today.month >= 7 else today.year
    grade = 12 - (year_finish_high_school - school_year_end)

    if today.month in (8, 9, 10, 11, 12):
        season = "Fall"
    elif today.month in (6, 7):
        season = "Summer"
    else:
        season = "Spring"

    grade_label = f"{_ordinal(grade)} grade" if 1 <= grade <= 12 else ("Graduated" if grade > 12 else None)
    return {
        "year_finish_high_school": year_finish_high_school,
        "grade": grade,
        "grade_label": grade_label,
        "season": season,
    }


@app.get("/students/{student_id}/class-year")
def get_class_year(student_id: str):
    try:
        snapshot = _fetch_student_snapshot(student_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))

    info = _compute_class_year_info(snapshot["personal_information"]["year_finish_high_school"])
    info["term_system"] = _normalize_term_system(snapshot["high_school"]["classes_schedule"])
    return info


# student.additional_info is a JSON blob on the login/account row itself
# (email, password, etc.) -- unlike every other per-student table, its
# primary key `id` IS the same id used everywhere else as {student_id}, no
# student_id column to join on.
@app.get("/students/{student_id}/majors-of-interest")
def get_majors_of_interest(student_id: str):
    with DB_POOLS["student"].connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT additional_info FROM student WHERE id = %s", (student_id,))
            row = cursor.fetchone()

    info = _decode_json_value(row["additional_info"]) if row else None
    if not isinstance(info, dict):
        info = {}

    majors = [info.get("majors_interest_1"), info.get("majors_interest_2")]
    return {"majors": [m for m in majors if m]}


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
    with DB_POOLS["student"].connection() as connection:
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
    with DB_POOLS["student"].connection() as connection:
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
    with DB_POOLS["student"].connection() as connection:
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
    act_id_number: int | None = None
    has_future_test: bool = False
    future_test_date: str | None = None


def _append_act_score(student_id: str, body: AddActScoreRequest) -> dict:
    with DB_POOLS["student"].connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(SELECT_ACT_ROW_SQL, (student_id,))
            row = cursor.fetchone()
            if row is None:
                raise ValueError(f"No ACT record found for student {student_id}.")

            existing_act_id_number = row["act_id_number"] or None
            if existing_act_id_number == 0:
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


MAX_OUTSTANDING_RECOMMENDATIONS = 10
MAX_SUGGEST_BATCH = 3


def _remaining_recommendation_slots(student_id):
    """Outstanding = not yet done. Dismissed rows are already excluded by
    fetch_all_recommendations, so a dismiss also frees up its slot."""
    existing = fetch_all_recommendations(student_id)
    outstanding_task_count = sum(1 for r in existing if r["status"] != "done")
    remaining_slots = MAX_OUTSTANDING_RECOMMENDATIONS - outstanding_task_count
    return existing, outstanding_task_count, remaining_slots


@app.post("/students/{student_id}/recommendations/generate")
def generate_recommendations(student_id: str):
    """Gated entry point for generating new AI recommendations. Unlike the raw
    POST /recommendations above, this only calls the LLM (and inserts new rows)
    up to however many slots are left under MAX_OUTSTANDING_RECOMMENDATIONS --
    e.g. a student with 8 outstanding tasks only gets up to 2 more, not a full
    fresh batch, so outstanding count can never exceed the cap after this call."""
    existing, outstanding_task_count, remaining_slots = _remaining_recommendation_slots(student_id)
    if remaining_slots <= 0:
        return {"generated": False, "outstanding_task_count": outstanding_task_count, "recommendations": existing}

    try:
        student = _fetch_student_snapshot(student_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))

    recommendations(student, max_recommendations=remaining_slots)  # stores new recommendations as a side effect
    updated = fetch_all_recommendations(student_id)
    return {"generated": True, "outstanding_task_count": outstanding_task_count, "recommendations": updated}


class SuggestRequest(BaseModel):
    category: str | None = None


@app.post("/students/{student_id}/recommendations/suggest")
def suggest_recommendation(student_id: str, body: SuggestRequest):
    """Backs the dashboard's "Suggest something" button. Unlike /generate
    (which tops up all the way to the outstanding-recommendation cap), this
    always asks the LLM for a small batch -- at most MAX_SUGGEST_BATCH -- and
    can be narrowed to a single category via `category`, still capped by
    however many slots are left under MAX_OUTSTANDING_RECOMMENDATIONS."""
    if body.category is not None and body.category not in ALLOWED_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"category must be one of {ALLOWED_CATEGORIES}")

    existing, outstanding_task_count, remaining_slots = _remaining_recommendation_slots(student_id)
    if remaining_slots <= 0:
        return {"generated": False, "outstanding_task_count": outstanding_task_count, "recommendations": existing}

    try:
        student = _fetch_student_snapshot(student_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))

    batch_size = min(MAX_SUGGEST_BATCH, remaining_slots)
    recommendations(student, max_recommendations=batch_size, category=body.category)  # stores new recommendations as a side effect
    updated = fetch_all_recommendations(student_id)
    return {"generated": True, "outstanding_task_count": outstanding_task_count, "recommendations": updated}


class AddRecommendationRequest(BaseModel):
    title: str
    subtext: str | None = None
    link: str | None = None
    category: str | None = None
    urgency_rank: str | None = None
    estimated_time: str | None = None
    target_date: str | None = None  # 'YYYY-MM-DD'


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
            target_date=body.target_date,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))


@app.delete("/students/{student_id}/recommendations/{recommendation_id}")
def remove_recommendation(student_id: str, recommendation_id: int):
    result = delete_recommendation(student_id, recommendation_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Recommendation not found")

    return {"id": result["id"], "title": result["title"], "removed": True}


@app.post("/students/{student_id}/recommendations/{recommendation_id}/dismiss")
def dismiss_recommendation_endpoint(student_id: str, recommendation_id: int):
    """Unlike DELETE above, this keeps the row -- it just stops showing up
    anywhere for this student (fetch_all_recommendations filters it out, so
    it also stops counting against MAX_OUTSTANDING_RECOMMENDATIONS)."""
    result = dismiss_recommendation(student_id, recommendation_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Recommendation not found")

    return {"id": result["id"], "title": result["title"], "dismissed": result["dismissed"]}


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


class RecommendationTargetDateUpdate(BaseModel):
    target_date: str | None = None  # 'YYYY-MM-DD', or null to clear it


@app.patch("/students/{student_id}/recommendations/{recommendation_id}/target-date")
def set_recommendation_target_date_endpoint(student_id: str, recommendation_id: int, body: RecommendationTargetDateUpdate):
    result = set_recommendation_target_date(student_id, recommendation_id, body.target_date)
    if result is None:
        raise HTTPException(status_code=404, detail="Recommendation not found")

    return {"id": result["id"], "target_date": str(result["target_date"]) if result["target_date"] else None}


class RecommendationTitleUpdate(BaseModel):
    title: str


@app.patch("/students/{student_id}/recommendations/{recommendation_id}/title")
def set_recommendation_title_endpoint(student_id: str, recommendation_id: int, body: RecommendationTitleUpdate):
    result = set_recommendation_title(student_id, recommendation_id, body.title)
    if result is None:
        raise HTTPException(status_code=404, detail="Recommendation not found")

    return {"id": result["id"], "title": result["title"]}


@app.get("/students/{student_id}/recommendations/{recommendation_id}/calendar-link")
def get_recommendation_calendar_link(student_id: str, recommendation_id: int):
    recommendation = fetch_recommendation(student_id, recommendation_id)
    if recommendation is None:
        raise HTTPException(status_code=404, detail="Recommendation not found")

    params = {"action": "TEMPLATE", "text": recommendation["title"]}
    return {"calendar_url": f"{GOOGLE_CALENDAR_EVENT_URL}?{urlencode(params)}"}


# --- Google Calendar OAuth + sync --------------------------------------------
# The dashboard opens a popup to /connect's authorize_url; Google eventually
# redirects the popup itself (not the dashboard tab) to /oauth-callback, which
# closes itself and messages the opener via postMessage. See google_calendar.py.

_GOOGLE_CALLBACK_HTML = """<!doctype html>
<html><body style="font-family:system-ui;text-align:center;padding:40px">
<p>{message}</p>
<script>
  if (window.opener) {{
    window.opener.postMessage({{ source: 'gradmap-google-calendar', status: '{status}' }}, '*');
    window.close();
  }}
</script>
</body></html>"""


def _google_callback_page(status: str, message: str) -> HTMLResponse:
    return HTMLResponse(_GOOGLE_CALLBACK_HTML.format(status=status, message=html.escape(message)))


@app.get("/students/{student_id}/google-calendar/connect")
def google_calendar_connect(student_id: str):
    flow_id = create_pending_flow(student_id)
    return {"authorize_url": build_authorize_url(flow_id)}


@app.get("/google-calendar/oauth-callback")
def google_calendar_oauth_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    """Fixed, pre-registered redirect URI -- Google redirects here with no way
    to carry a {student_id} path segment, so `state` (the flow_id from
    /connect) is what recovers which student this is for."""
    if error or not code or not state:
        return _google_callback_page("error", error or "Google did not return an authorization code.")

    student_id = pop_pending_flow(state)
    if student_id is None:
        return _google_callback_page("error", "This connection link expired. Please try connecting again.")

    try:
        complete_connection(student_id, code)
    except Exception:
        return _google_callback_page("error", "Could not connect Google Calendar. Please try again.")

    return _google_callback_page("success", "Google Calendar connected. You can close this tab.")


@app.get("/students/{student_id}/google-calendar/status")
def google_calendar_status(student_id: str):
    return {"connected": is_google_calendar_connected(student_id)}


@app.post("/students/{student_id}/google-calendar/disconnect")
def google_calendar_disconnect(student_id: str):
    disconnect_google_calendar(student_id)
    return {"connected": False}


class GoogleCalendarSyncItem(BaseModel):
    source_type: Literal["hard_deadline", "target_date", "own_event"]
    source_id: str
    title: str
    date: str  # YYYY-MM-DD
    description: str | None = None


class GoogleCalendarSyncRequest(BaseModel):
    items: list[GoogleCalendarSyncItem]


@app.post("/students/{student_id}/google-calendar/sync")
def google_calendar_sync(student_id: str, body: GoogleCalendarSyncRequest):
    """Full reconciliation, not an incremental append: `items` is treated as
    the complete current set of calendar-relevant dates for this student, and
    anything previously synced but missing from it is deleted from Google."""
    try:
        sync_google_calendar_events(student_id, [item.model_dump() for item in body.items])
    except GoogleNotConnectedError:
        raise HTTPException(status_code=409, detail="Google Calendar is not connected for this student")
    return {"synced": len(body.items)}


# --- Apple/iCloud Calendar via CalDAV ----------------------------------------
# No OAuth, no popup: the student submits their Apple ID + an app-specific
# password directly (generated at appleid.apple.com), which we validate
# against iCloud on the spot. See apple_calendar.py.

class AppleCalendarConnectRequest(BaseModel):
    apple_id: str
    app_specific_password: str


@app.post("/students/{student_id}/apple-calendar/connect")
def apple_calendar_connect(student_id: str, body: AppleCalendarConnectRequest):
    try:
        connect_apple_calendar(student_id, body.apple_id, body.app_specific_password)
    except AppleCalendarAuthError as error:
        raise HTTPException(status_code=401, detail=str(error))
    return {"connected": True}


@app.get("/students/{student_id}/apple-calendar/status")
def apple_calendar_status(student_id: str):
    return {"connected": is_apple_calendar_connected(student_id)}


@app.post("/students/{student_id}/apple-calendar/disconnect")
def apple_calendar_disconnect(student_id: str):
    disconnect_apple_calendar(student_id)
    return {"connected": False}


class AppleCalendarSyncItem(BaseModel):
    source_type: Literal["hard_deadline", "target_date", "own_event"]
    source_id: str
    title: str
    date: str  # YYYY-MM-DD
    description: str | None = None


class AppleCalendarSyncRequest(BaseModel):
    items: list[AppleCalendarSyncItem]


@app.post("/students/{student_id}/apple-calendar/sync")
def apple_calendar_sync(student_id: str, body: AppleCalendarSyncRequest):
    """Same full-reconciliation contract as /google-calendar/sync."""
    try:
        sync_apple_calendar_events(student_id, [item.model_dump() for item in body.items])
    except AppleNotConnectedError:
        raise HTTPException(status_code=409, detail="Apple Calendar is not connected for this student")
    except AppleCalendarAuthError as error:
        raise HTTPException(status_code=401, detail=str(error))
    return {"synced": len(body.items)}


# --- Student's own calendar events (campus visits, test days, etc.) --------
# Separate from tasks/recommendations: these are events the student typed in
# directly on the calendar screen, not something GradMap suggested.

class AddOwnEventRequest(BaseModel):
    title: str
    event_date: str  # 'YYYY-MM-DD'
    kind: Literal["campus_visit", "test_day", "school_event", "info_session", "family_personal", "other"]


@app.post("/students/{student_id}/own-events")
def add_own_event_endpoint(student_id: str, body: AddOwnEventRequest):
    try:
        return add_own_event(student_id, body.title, body.event_date, body.kind)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))


@app.get("/students/{student_id}/own-events")
def list_own_events_endpoint(student_id: str):
    return {"events": list_own_events(student_id)}


class UpdateOwnEventRequest(BaseModel):
    title: str
    event_date: str  # 'YYYY-MM-DD'


@app.patch("/students/{student_id}/own-events/{event_id}")
def update_own_event_endpoint(student_id: str, event_id: int, body: UpdateOwnEventRequest):
    result = update_own_event(student_id, event_id, body.title, body.event_date)
    if result is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return result


@app.delete("/students/{student_id}/own-events/{event_id}")
def delete_own_event_endpoint(student_id: str, event_id: int):
    result = delete_own_event(student_id, event_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return {"id": result["id"], "title": result["title"], "removed": True}


# --- "Your story" -------------------------------------------------------
# Unlike recommendations, this never regenerates itself -- only an explicit
# rebuild call reaches the LLM, and that's capped server-side (story.py).

@app.get("/students/{student_id}/story")
def get_story_endpoint(student_id: str):
    return get_story(student_id)


@app.post("/students/{student_id}/story/rebuild")
def rebuild_story_endpoint(student_id: str):
    try:
        student = _fetch_student_snapshot(student_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))
    return rebuild_story(student_id, student)


class StoryEditRequest(BaseModel):
    line: str


@app.patch("/students/{student_id}/story")
def edit_story_endpoint(student_id: str, body: StoryEditRequest):
    return save_edited_line(student_id, body.line)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--student", dest="student_id", type=str, required=True, help="Enter the student ID")

    args = parser.parse_args()

    student = _fetch_student_snapshot(args.student_id)
    #print(json.dumps(student, indent=2, default=str))
    print(recommendations(student))


if __name__ == "__main__":
    main()
