""""Your story" -- one LLM-drafted sentence naming the through-line in a
student's real activities/honors, in the same spirit as recommend.py: pass in
the student's actual profile, let the model find the pattern, store the
result, let the student edit it in their own words.

Unlike recommendations, this never regenerates on its own -- only an explicit
"Rebuild from my profile" click calls the LLM, and that's capped at
MAX_STORY_REBUILDS_PER_DAY per student per day.
"""

import json
import os
from datetime import date

import anthropic
import psycopg
from psycopg.rows import dict_row

SCHOOLS_DB_CONFIG = {
    "host": os.environ["GM_DB_HOST"],
    "port": int(os.environ["GM_DB_PORT"]),
    "user": os.environ["GM_DB_USER"],
    "password": os.environ["GM_DB_PASSWORD"],
    "dbname": os.environ["GM_DB_SCHOOLS_NAME"],
}

MAX_STORY_REBUILDS_PER_DAY = 3

# Same field-name conventions as main.py's activity_honor handling (see
# ACTIVITY_GRADE_LEVEL_FIELDS / RECOGNITION_LEVEL_FIELDS there) -- duplicated
# here rather than imported, since they're just display-formatting labels,
# not shared business logic.
_ACTIVITY_GRADE_LEVEL_FIELDS = {
    "9": "isGrade9ParticipationLevels", "10": "isGrade10ParticipationLevels",
    "11": "isGrade11ParticipationLevels", "12": "isGrade12ParticipationLevels",
    "post_graduate": "isPostGraduateParticipationLevels",
}
_RECOGNITION_LEVEL_FIELDS = {
    "school": "isSchoolLevelRecognition", "state": "isStateLevelRecognition",
    "national": "isNational", "international": "isInternationalLevelRecognition",
}

api_key = os.environ.get("ANTHROPIC_API_KEY")
if not api_key:
    raise RuntimeError("ANTHROPIC_API_KEY cannot be found.")
client = anthropic.Anthropic(api_key=api_key)


def _strip_code_fence(text):
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text


def _format_activities(activities):
    """Returns (text for the prompt, the exact title strings the model is
    allowed to cite back as `built_from`)."""
    titles = []
    lines = []
    for a in activities or []:
        if not a:
            continue
        name = a.get("programName") or a.get("activityExperienceProgramName") or "Untitled activity"
        titles.append(name)
        grades = [g for g, field in _ACTIVITY_GRADE_LEVEL_FIELDS.items() if a.get(field)]
        parts = [f"name={name}"]
        if a.get("isInvolvedLeadershipRole"):
            parts.append("leadership role")
        if grades:
            parts.append(f"grades={','.join(grades)}")
        if a.get("hoursPerWeek"):
            parts.append(f"hrs/wk={a['hoursPerWeek']}")
        description = a.get("describeActivity") or a.get("whatDidYouDo") or ""
        if description:
            parts.append(f"description={description}")
        distinctions = a.get("listIndividualDistinctions") or ""
        if distinctions:
            parts.append(f"distinctions={distinctions}")
        lines.append("- " + " | ".join(parts))
    return ("\n".join(lines) or "None logged yet."), titles


def _format_honors(honors):
    titles = []
    lines = []
    for h in honors or []:
        if not h:
            continue
        title = h.get("honorTitle") or "Untitled honor"
        titles.append(title)
        level = next((k for k, field in _RECOGNITION_LEVEL_FIELDS.items() if h.get(field)), None)
        parts = [f"title={title}"]
        if h.get("typeOfHonor"):
            parts.append(f"type={h['typeOfHonor']}")
        if level:
            parts.append(f"level={level}")
        lines.append("- " + " | ".join(parts))
    return ("\n".join(lines) or "None logged yet."), titles


STORY_SYSTEM_PROMPT = """You write a single sentence that names the through-line in a high school student's activities and honors -- the pattern an admissions reader would notice, not a list of everything they have done.

Rules:
- Exactly one sentence (two only if truly needed), under 220 characters.
- Base it only on the activities/honors given below. Do not invent names, awards, roles, or years that are not present in the data.
- Depth and leadership matter more than breadth. If the entries point in genuinely different directions with no dominant thread, say so honestly rather than forcing a spike that is not there.
- Voice: direct, third person ("A builder who...", "Someone who..."). Not corporate ("passionate", "dynamic", "well-rounded"), not a resume line.
- Also return a short 1-3 word "spike" label naming the theme (e.g. "Robotics", "Debate", "Community service"), and up to 3 of the exact activity/honor names given below that most support the line -- copy them verbatim, do not paraphrase.

Respond with only this JSON, no code fence, no other text:
{"enough": true or false, "line": "...", "spike": "...", "built_from": ["...", "..."]}

Set "enough" to false, and leave "line"/"spike" as empty strings and "built_from" as an empty list, only if there are fewer than 2 real entries total, or the entries are so scattered that no honest thread exists."""


def _draft_story(activities, honors):
    activities_text, activity_titles = _format_activities(activities)
    honors_text, honor_titles = _format_honors(honors)

    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=300,
        system=STORY_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"Activities:\n{activities_text}\n\nHonors:\n{honors_text}",
        }],
    )
    result = json.loads(_strip_code_fence(response.content[0].text))

    real_titles = set(activity_titles) | set(honor_titles)
    built_from = [b for b in result.get("built_from", []) if b in real_titles][:3]

    if not result.get("enough"):
        return {"enough": False, "line": "", "spike": "", "built_from": []}
    return {"enough": True, "line": result.get("line", ""), "spike": result.get("spike", ""), "built_from": built_from}


# --- storage -------------------------------------------------------------

CREATE_STUDENT_STORY_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS student_story (
    student_id INTEGER PRIMARY KEY,
    line TEXT NOT NULL DEFAULT '',
    spike TEXT NOT NULL DEFAULT '',
    built_from TEXT NOT NULL DEFAULT '[]',
    edited_by_student BOOLEAN NOT NULL DEFAULT false,
    rebuild_count INTEGER NOT NULL DEFAULT 0,
    rebuild_date DATE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

GET_STORY_SQL = """
SELECT line, spike, built_from, edited_by_student, rebuild_count, rebuild_date
FROM student_story WHERE student_id = %s
"""

UPSERT_STORY_LINE_SQL = """
INSERT INTO student_story (student_id, line, spike, built_from, edited_by_student, updated_at)
VALUES (%(student_id)s, %(line)s, %(spike)s, %(built_from)s, %(edited_by_student)s, now())
ON CONFLICT (student_id) DO UPDATE SET
    line = EXCLUDED.line, spike = EXCLUDED.spike, built_from = EXCLUDED.built_from,
    edited_by_student = EXCLUDED.edited_by_student, updated_at = now()
"""

# Atomically bumps today's rebuild count (resetting it first if the stored
# date isn't today) and returns the new count, in one round trip -- avoids a
# read-then-write race if the button is somehow double-clicked.
BUMP_REBUILD_COUNT_SQL = """
INSERT INTO student_story (student_id, rebuild_count, rebuild_date)
VALUES (%(student_id)s, 1, %(today)s)
ON CONFLICT (student_id) DO UPDATE SET
    rebuild_count = CASE WHEN student_story.rebuild_date = %(today)s THEN student_story.rebuild_count + 1 ELSE 1 END,
    rebuild_date = %(today)s
RETURNING rebuild_count
"""


def ensure_student_story_table():
    with psycopg.connect(**SCHOOLS_DB_CONFIG) as connection:
        with connection.cursor() as cursor:
            cursor.execute(CREATE_STUDENT_STORY_TABLE_SQL)


def get_story(student_id) -> dict:
    ensure_student_story_table()
    with psycopg.connect(**SCHOOLS_DB_CONFIG, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(GET_STORY_SQL, (student_id,))
            row = cursor.fetchone()

    if row is None:
        return {
            "line": "", "spike": "", "built_from": [], "edited_by_student": False,
            "rebuilds_remaining_today": MAX_STORY_REBUILDS_PER_DAY,
        }

    used_today = row["rebuild_count"] if row["rebuild_date"] == date.today() else 0
    return {
        "line": row["line"],
        "spike": row["spike"],
        "built_from": json.loads(row["built_from"] or "[]"),
        "edited_by_student": row["edited_by_student"],
        "rebuilds_remaining_today": max(0, MAX_STORY_REBUILDS_PER_DAY - used_today),
    }


def _save_story_row(student_id, line, spike, built_from, edited_by_student):
    with psycopg.connect(**SCHOOLS_DB_CONFIG) as connection:
        with connection.cursor() as cursor:
            cursor.execute(UPSERT_STORY_LINE_SQL, {
                "student_id": student_id,
                "line": line,
                "spike": spike,
                "built_from": json.dumps(built_from),
                "edited_by_student": edited_by_student,
            })


def save_edited_line(student_id, line: str) -> dict:
    """The student's own words win until they Rebuild. Clearing the text
    (empty string) resets to the no-story state, same as never having one."""
    ensure_student_story_table()
    line = (line or "").strip()
    if line:
        current = get_story(student_id)
        _save_story_row(student_id, line=line, spike=current["spike"], built_from=current["built_from"], edited_by_student=True)
    else:
        _save_story_row(student_id, line="", spike="", built_from=[], edited_by_student=False)
    return get_story(student_id)


def rebuild_story(student_id, student_snapshot) -> dict:
    """Gated by MAX_STORY_REBUILDS_PER_DAY, checked (and consumed) before the
    LLM is ever called, so a student at the cap costs nothing."""
    ensure_student_story_table()
    today = date.today()
    with psycopg.connect(**SCHOOLS_DB_CONFIG, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(BUMP_REBUILD_COUNT_SQL, {"student_id": student_id, "today": today})
            rebuild_count = cursor.fetchone()["rebuild_count"]

    if rebuild_count > MAX_STORY_REBUILDS_PER_DAY:
        return {"generated": False, "story": get_story(student_id)}

    activity_honor = student_snapshot.get("activity_honor") or {}
    draft = _draft_story(activity_honor.get("activity_array"), activity_honor.get("honor_array"))
    _save_story_row(
        student_id,
        line=draft["line"], spike=draft["spike"], built_from=draft["built_from"],
        edited_by_student=False,
    )
    return {"generated": True, "story": get_story(student_id)}
