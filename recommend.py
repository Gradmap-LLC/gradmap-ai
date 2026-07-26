import json
import os
from pathlib import Path

import anthropic
import psycopg


BASE_DIR = Path(__file__).resolve().parent


SCHOOLS_DB_CONFIG = {
    "host": os.environ["GM_DB_HOST"],
    "port": int(os.environ["GM_DB_PORT"]),
    "user": os.environ["GM_DB_USER"],
    "password": os.environ["GM_DB_PASSWORD"],
    "dbname": os.environ["GM_DB_SCHOOLS_NAME"],
}


CREATE_STUDENT_RECOMMENDATIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS student_recommendations (
    id SERIAL PRIMARY KEY,
    student_id INTEGER NOT NULL,
    urgency_rank TEXT,
    category TEXT,
    title TEXT,
    subtext TEXT,
    link TEXT,
    status TEXT NOT NULL DEFAULT 'not_complete' CHECK (status IN ('not_complete', 'complete')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


ADD_STUDENT_RECOMMENDATIONS_STATUS_COLUMNS_SQL = """
ALTER TABLE student_recommendations
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'not_complete' CHECK (status IN ('not_complete', 'complete')),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
"""


INSERT_RECOMMENDATION_SQL = """
INSERT INTO student_recommendations (student_id, urgency_rank, category, title, subtext, link)
VALUES (%s, %s, %s, %s, %s, %s)
RETURNING id, status
"""


MARK_RECOMMENDATION_COMPLETE_SQL = """
UPDATE student_recommendations
SET status = 'complete', updated_at = now()
WHERE id = %s AND student_id = %s
"""


def ensure_student_recommendations_table():
    with psycopg.connect(**SCHOOLS_DB_CONFIG) as connection:
        with connection.cursor() as cursor:
            cursor.execute(CREATE_STUDENT_RECOMMENDATIONS_TABLE_SQL)
            cursor.execute(ADD_STUDENT_RECOMMENDATIONS_STATUS_COLUMNS_SQL)


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
                ),
            )
            return cursor.fetchone()


def mark_recommendation_complete(student_id, recommendation_id):
    with psycopg.connect(**SCHOOLS_DB_CONFIG) as connection:
        with connection.cursor() as cursor:
            cursor.execute(MARK_RECOMMENDATION_COMPLETE_SQL, (recommendation_id, student_id))


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

def recommendations(student_snapshot, context="context/gradmap_context.json", triggers="context/triggers.json"):
    context_data = _load_json(context)
    triggers_data = _load_json(triggers)
    context_text = _format_context_articles(context_data)
    triggers_text = json.dumps(triggers_data, indent=2)

    with open("prompts/ai_context_v1.md") as f:
        system_prompt = f.read()
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=2000,
        cache_control={"type": "ephemeral"},
        system=system_prompt,
        messages=[{
            "role": "user",
            "content": f"Student snapshot:\n{student_snapshot}\n\nContext articles:\n{context_text}\n\nTrigger rules:\n{triggers_text}\n\nUse the supporting article URLs from the context when making recommendations, and include the relevant links in the response."
        }]

    )
    result = json.loads(_strip_code_fence(response.content[0].text))

    student_id = student_snapshot["id"]
    ensure_student_recommendations_table()
    for recommendation in result.get("recommendations", []):
        recommendation_id, status = _store_recommendation(student_id, recommendation)
        recommendation["id"] = recommendation_id
        recommendation["status"] = status

    return result
