import json
import os
from pathlib import Path

import anthropic
import psycopg
from psycopg.rows import dict_row


BASE_DIR = Path(__file__).resolve().parent


SCHOOLS_DB_CONFIG = {
    "host": os.environ["GM_DB_HOST"],
    "port": int(os.environ["GM_DB_PORT"]),
    "user": os.environ["GM_DB_USER"],
    "password": os.environ["GM_DB_PASSWORD"],
    "dbname": os.environ["GM_DB_SCHOOLS_NAME"],
}


ALLOWED_CATEGORIES = (
    "essay_planning",
    "course_planning",
    "major",
    "financial_aid",
    "upcoming_events",
    "letters_of_recommendation",
)

ALLOWED_URGENCY_RANKS = ("due_soon", "coming_up", "later")

ALLOWED_STATUSES = ("not_started", "in_progress", "done")

DEFAULT_ESTIMATED_TIME = "30 min"


CREATE_STUDENT_RECOMMENDATIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS student_recommendations (
    id SERIAL PRIMARY KEY,
    student_id INTEGER NOT NULL,
    urgency_rank TEXT,
    category TEXT,
    title TEXT,
    subtext TEXT,
    link TEXT,
    estimated_time TEXT,
    status TEXT NOT NULL DEFAULT 'not_started' CHECK (status IN ('not_started', 'in_progress', 'done')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


ADD_STUDENT_RECOMMENDATIONS_COLUMNS_SQL = """
ALTER TABLE student_recommendations
    ADD COLUMN IF NOT EXISTS estimated_time TEXT,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
"""


INSERT_RECOMMENDATION_SQL = """
INSERT INTO student_recommendations (student_id, urgency_rank, category, title, subtext, link, estimated_time)
VALUES (%s, %s, %s, %s, %s, %s, %s)
RETURNING id, status, estimated_time
"""


UPDATE_RECOMMENDATION_STATUS_SQL = """
UPDATE student_recommendations
SET status = %s, updated_at = now()
WHERE id = %s AND student_id = %s
RETURNING id, status
"""


FETCH_ACTIVE_TASK_TEMPLATES_SQL = """
SELECT id, category, sub_category, description, trigger_rule, typical_month, month_schedule,
       applicable_grades, deadline_type, links{estimated_time_select}
FROM task_templates
WHERE is_active = true
"""


TASK_TEMPLATE_ESTIMATED_TIME_COLUMN_CANDIDATES = ("estimated_time", "duration_min", "estimated_minutes")


FETCH_STUDENT_RECOMMENDATIONS_SQL = """
SELECT title, category, status
FROM student_recommendations
WHERE student_id = %s
ORDER BY created_at
"""


def ensure_student_recommendations_table():
    with psycopg.connect(**SCHOOLS_DB_CONFIG) as connection:
        with connection.cursor() as cursor:
            cursor.execute(CREATE_STUDENT_RECOMMENDATIONS_TABLE_SQL)
            cursor.execute(ADD_STUDENT_RECOMMENDATIONS_COLUMNS_SQL)


def _store_recommendation(student_id, recommendation):
    with psycopg.connect(**SCHOOLS_DB_CONFIG) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                INSERT_RECOMMENDATION_SQL,
                (
                    student_id,
                    recommendation.get("urgency_rank"),
                    recommendation.get("category"),
                    recommendation.get("title"),
                    recommendation.get("subtext"),
                    recommendation.get("link"),
                    recommendation.get("estimated_time"),
                ),
            )
            return cursor.fetchone()


def update_recommendation_status(student_id, recommendation_id, status):
    """Set a recommendation's status to not_started, in_progress, or done.

    Reopening a "done" task (status="not_started") only touches the status
    column, so the task falls back into its existing category/urgency_rank
    automatically.
    """
    if status not in ALLOWED_STATUSES:
        raise ValueError(f"status must be one of {ALLOWED_STATUSES}, got {status!r}")

    with psycopg.connect(**SCHOOLS_DB_CONFIG) as connection:
        with connection.cursor() as cursor:
            cursor.execute(UPDATE_RECOMMENDATION_STATUS_SQL, (status, recommendation_id, student_id))
            return cursor.fetchone()


def add_student_task(student_id, title, subtext=None, link=None, category=None,
                      urgency_rank=None, estimated_time=None):
    """Insert a student-created task into the same student_recommendations table."""
    if category is not None and category not in ALLOWED_CATEGORIES:
        raise ValueError(f"category must be one of {ALLOWED_CATEGORIES}, got {category!r}")
    if urgency_rank is not None and urgency_rank not in ALLOWED_URGENCY_RANKS:
        raise ValueError(f"urgency_rank must be one of {ALLOWED_URGENCY_RANKS}, got {urgency_rank!r}")

    ensure_student_recommendations_table()
    recommendation_id, status, estimated_time = _store_recommendation(
        student_id,
        {
            "urgency_rank": urgency_rank,
            "category": category,
            "title": title,
            "subtext": subtext,
            "link": link,
            "estimated_time": estimated_time or DEFAULT_ESTIMATED_TIME,
        },
    )
    return {
        "id": recommendation_id,
        "urgency_rank": urgency_rank,
        "category": category,
        "title": title,
        "subtext": subtext,
        "link": link,
        "estimated_time": estimated_time,
        "status": status,
    }


def _load_json(relative_path):
    path = BASE_DIR / relative_path
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def _format_context_articles(context_data):
    articles = context_data.get("articles", [])
    lines = []
    for article in articles:
        title = article.get("title", "Untitled")
        url = article.get("url", "")
        lines.append(f"- {title}: {url}")
    return "\n".join(lines)


def _detect_task_template_estimated_time_column(cursor):
    cursor.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'task_templates'"
    )
    existing_columns = {row["column_name"] for row in cursor.fetchall()}
    for candidate in TASK_TEMPLATE_ESTIMATED_TIME_COLUMN_CANDIDATES:
        if candidate in existing_columns:
            return candidate
    return None


def _fetch_active_task_templates():
    with psycopg.connect(**SCHOOLS_DB_CONFIG, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            estimated_time_column = _detect_task_template_estimated_time_column(cursor)
            estimated_time_select = f", {estimated_time_column} AS estimated_time" if estimated_time_column else ""
            cursor.execute(
                FETCH_ACTIVE_TASK_TEMPLATES_SQL.format(estimated_time_select=estimated_time_select)
            )
            return cursor.fetchall()


def _fetch_student_recommendations(student_id):
    with psycopg.connect(**SCHOOLS_DB_CONFIG, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(FETCH_STUDENT_RECOMMENDATIONS_SQL, (student_id,))
            return cursor.fetchall()


def _format_existing_recommendations(existing_recommendations):
    if not existing_recommendations:
        return "None yet."
    lines = []
    for recommendation in existing_recommendations:
        category = recommendation.get("category") or "uncategorized"
        status = recommendation.get("status")
        lines.append(f"- [{category}] {recommendation['title']} ({status})")
    return "\n".join(lines)


def _format_estimated_time(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return f"{int(value)} min"
    return str(value)


def _format_task_templates(task_templates):
    lines = []
    for task in task_templates:
        parts = [f"id={task['id']}"]
        for field in ("category", "sub_category", "description", "trigger_rule",
                      "typical_month", "month_schedule", "applicable_grades", "deadline_type", "links"):
            value = task.get(field)
            if value:
                parts.append(f"{field}={value}")
        estimated_time = _format_estimated_time(task.get("estimated_time"))
        if estimated_time:
            parts.append(f"estimated_time={estimated_time}")
        lines.append("- " + " | ".join(parts))
    return "\n".join(lines)


def _strip_code_fence(text):
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text



api_key = os.environ.get("ANTHROPIC_API_KEY")

if not api_key:
    raise RuntimeError(
        "ANTHROPIC_API_KEY cannot be found."
    )

client = anthropic.Anthropic(api_key=api_key)

def recommendations(student_snapshot, context="context/gradmap_context.json"):
    student_id = student_snapshot["id"]
    ensure_student_recommendations_table()

    context_data = _load_json(context)
    context_text = _format_context_articles(context_data)

    task_templates = _fetch_active_task_templates()
    task_templates_text = _format_task_templates(task_templates)

    existing_recommendations = _fetch_student_recommendations(student_id)
    existing_recommendations_text = _format_existing_recommendations(existing_recommendations)

    with open("prompts/ai_context_v1.md") as f:
        system_prompt = f.read()
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=2000,
        system=[
            {"type": "text", "text": system_prompt},
            {
                "type": "text",
                "text": f"Active tasks:\n{task_templates_text}",
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": (
                    "Already tracked recommendations for this student (any status — not_started, "
                    "in_progress, or done). Do not recommend anything with the same underlying goal "
                    f"as these, even if the wording or category differs:\n{existing_recommendations_text}"
                ),
            },
        ],
        messages=[{
            "role": "user",
            "content": f"Student snapshot:\n{student_snapshot}\n\nContext articles:\n{context_text}\n\nUse the supporting article URLs from the context when making recommendations, and include the relevant links in the response."
        }]

    )
    result = json.loads(_strip_code_fence(response.content[0].text))

    for recommendation in result.get("recommendations", []):
        recommendation_id, status, estimated_time = _store_recommendation(student_id, recommendation)
        recommendation["id"] = recommendation_id
        recommendation["status"] = status
        recommendation["estimated_time"] = estimated_time

    return result
