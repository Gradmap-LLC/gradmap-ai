import json
import os
from pathlib import Path

import anthropic


BASE_DIR = Path(__file__).resolve().parent


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



api_key = os.environ.get("ANTHROPIC_API_KEY")

if not api_key:
    raise RuntimeError(
        "ANTHROPIC_API_KEY cannot be found."
    )

client = anthropic.Anthropic(api_key=api_key)

def recomendations(student_snapshot, context="context/gradmap_context.json", triggers="context/triggers.json"):
    context_data = _load_json(context)
    triggers_data = _load_json(triggers)
    context_text = _format_context_articles(context_data)
    triggers_text = json.dumps(triggers_data, indent=2)

    with open("prompts/ai_context_v1.md") as f:
        system_prompt = f.read()
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=2000,
        system=system_prompt,
        messages=[{
            "role": "user",
            "content": f"Student snapshot:\n{student_snapshot}\n\nContext articles:\n{context_text}\n\nTrigger rules:\n{triggers_text}\n\nUse the supporting article URLs from the context when making recommendations, and include the relevant links in the response."
        }]

    )
    return response.content[0].text
