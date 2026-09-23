"""Standalone test harness for the prompts/ai_context_*.md system prompts.

Runs each synthetic student profile in test_profiles.csv through Claude using
a chosen ai_context_vN.md system prompt (--prompt, default v2), and writes
Claude's recommendations plus its per-recommendation rationale (the "subtext"
field the prompt asks for) to an output CSV -- alongside the profile's own
gold-standard "Expected next recommendations (Michelle)"/"Priority rationale"
columns, for side-by-side comparison. The output filename is named after the
prompt version used (e.g. test_profiles_output_ai_context_v3.csv) unless
--output overrides it, so runs against different prompt versions don't
clobber each other.

Deliberately does not touch the database: recommend.recommendations() would
normally pull active task_templates and a student's already-tracked
recommendations from Postgres, but these are made-up test students that don't
exist there, so this passes empty placeholders for those two context blocks
instead of wiring up a live connection. The only per-student input is what's
in test_profiles.csv -- and specifically NOT the "Expected next
recommendations"/"Priority rationale" columns, which are gold-standard
answers, not model input; they're read only when writing the output row, for
comparison.
"""

import argparse
import csv
import json
import os
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

PROMPTS_DIR = BASE_DIR / "prompts"
DEFAULT_SYSTEM_PROMPT_PATH = PROMPTS_DIR / "ai_context_v2.md"
CONTEXT_PATH = BASE_DIR / "context" / "gradmap_context.json"
DEFAULT_INPUT_CSV = BASE_DIR / "test_profiles.csv"

MODEL = "claude-haiku-4-5"
DEFAULT_MAX_RECOMMENDATIONS = 3  # matches the gold data's "top 3"

EXAMPLE_ROW_ID = "EX"  # legend: "Row 3 is an example of the expected format; replace or delete it"

# CSV columns fed to the model as the student's snapshot (csv header -> label
# shown to the model). Deliberately excludes the two gold-standard columns.
SNAPSHOT_FIELDS = [
    ("Grade", "Grade"),
    ("Season / pretend month", "Season"),
    ("Intended major(s)", "Intended major(s)"),
    ("GPA (W / UW)", "GPA (weighted / unweighted)"),
    ("Rigor", "Rigor"),
    ("Key courses & grades", "Key courses & grades"),
    ("Activities & honors", "Activities & honors"),
    ("Testing status", "Testing status"),
    ("Thin spots / gaps", "Thin spots / gaps"),
]

GOLD_RECOMMENDATIONS_FIELD = "Expected next recommendations (Michelle — top 3)"
GOLD_RATIONALE_FIELD = "Priority rationale"


def _read_profiles(csv_path):
    """test_profiles.csv has a 2-line title/legend preamble and a blank line
    before the real header row -- skip down to the line that actually starts
    with "ID," and hand the rest to DictReader. Also drops the "EX" example
    row, which is gold-standard scaffolding, not a real test profile."""
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        lines = f.readlines()
    header_index = next(i for i, line in enumerate(lines) if line.startswith("ID,"))
    reader = csv.DictReader(lines[header_index:])
    return [row for row in reader if row.get("ID") and row["ID"].strip().upper() != EXAMPLE_ROW_ID]


def _load_json(path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


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


def _build_snapshot_text(row):
    lines = [f"Student ID: {row['ID']}"]
    for csv_field, label in SNAPSHOT_FIELDS:
        value = (row.get(csv_field) or "").strip()
        if value:
            lines.append(f"{label}: {value}")
    return "\n".join(lines)


def _format_recommendations(recommendations):
    parts = []
    for i, rec in enumerate(recommendations, start=1):
        title = rec.get("title") or "(untitled)"
        tags = [v for v in (rec.get("urgency_rank"), rec.get("category"), rec.get("estimated_time")) if v]
        suffix = f" [{', '.join(tags)}]" if tags else ""
        parts.append(f"{i}) {title}{suffix}")
    return "  ".join(parts)


def _format_rationale(recommendations):
    parts = []
    for i, rec in enumerate(recommendations, start=1):
        parts.append(f"{i}) {rec.get('subtext') or ''}".strip())
    return "  ".join(parts)


def get_recommendations(client, system_prompt, context_text, snapshot_text, max_recommendations):
    limit_text = (
        f"Recommendation limit for this request: generate at most {max_recommendations} "
        f"new recommendation(s) this time, even if you have more good ideas. It's fine to "
        f"return fewer than {max_recommendations} if you don't have that many truly relevant ones."
    )
    response = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=[
            {"type": "text", "text": system_prompt},
            # No task_templates/existing-recommendations DB data for these
            # synthetic students -- honest empty placeholders instead, kept
            # as separate blocks to mirror recommend.recommendations()'s shape.
            {"type": "text", "text": "Active tasks:\nNone available for this test run."},
            {"type": "text", "text": "Already tracked recommendations for this student: None yet."},
            {"type": "text", "text": limit_text},
        ],
        messages=[{
            "role": "user",
            "content": (
                f"Student snapshot:\n{snapshot_text}\n\nContext articles:\n{context_text}\n\n"
                "Use the supporting article URLs from the context when making recommendations, "
                "and include the relevant links in the response."
            ),
        }],
    )
    result = json.loads(_strip_code_fence(response.content[0].text))
    return result.get("recommendations", [])


def main():
    parser = argparse.ArgumentParser(
        description="Run test_profiles.csv through an ai_context_vN.md system prompt via Claude and save the results to a CSV."
    )
    parser.add_argument("--input", default=str(DEFAULT_INPUT_CSV), help="Path to the input profiles CSV.")
    parser.add_argument(
        "--prompt", default=str(DEFAULT_SYSTEM_PROMPT_PATH),
        help="Path to the ai_context_vN.md system prompt to test (default: prompts/ai_context_v2.md).",
    )
    parser.add_argument(
        "--output", default=None,
        help="Path to write the results CSV to (default: test_profiles_output_<prompt name>.csv).",
    )
    parser.add_argument(
        "--limit", type=int, default=DEFAULT_MAX_RECOMMENDATIONS,
        help="Max recommendations to request per student (default: 3, matching the gold data's 'top 3').",
    )
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY cannot be found.")
    client = anthropic.Anthropic(api_key=api_key)

    prompt_path = Path(args.prompt)
    output_path = Path(args.output) if args.output else BASE_DIR / f"test_profiles_output_{prompt_path.stem}.csv"

    system_prompt = prompt_path.read_text(encoding="utf-8")
    context_text = _format_context_articles(_load_json(CONTEXT_PATH))

    profiles = _read_profiles(Path(args.input))
    print(f"Loaded {len(profiles)} test profiles from {args.input}")
    print(f"Using system prompt {prompt_path}")

    fieldnames = [
        "id", "grade", "season", "intended_major",
        "recommendations_claude", "rationale_claude",
        "expected_recommendations_michelle", "priority_rationale_michelle",
        "raw_json", "error",
    ]

    with open(output_path, "w", encoding="utf-8", newline="") as out_file:
        writer = csv.DictWriter(out_file, fieldnames=fieldnames)
        writer.writeheader()

        for row in profiles:
            student_id = row["ID"]
            print(f"Running {student_id}...")
            snapshot_text = _build_snapshot_text(row)

            output_row = {
                "id": student_id,
                "grade": row.get("Grade", ""),
                "season": row.get("Season / pretend month", ""),
                "intended_major": row.get("Intended major(s)", ""),
                "expected_recommendations_michelle": row.get(GOLD_RECOMMENDATIONS_FIELD, ""),
                "priority_rationale_michelle": row.get(GOLD_RATIONALE_FIELD, ""),
                "recommendations_claude": "",
                "rationale_claude": "",
                "raw_json": "",
                "error": "",
            }

            try:
                recommendations = get_recommendations(client, system_prompt, context_text, snapshot_text, args.limit)
                output_row["recommendations_claude"] = _format_recommendations(recommendations)
                output_row["rationale_claude"] = _format_rationale(recommendations)
                output_row["raw_json"] = json.dumps(recommendations)
            except Exception as e:
                output_row["error"] = str(e)
                print(f"  ERROR for {student_id}: {e}")

            writer.writerow(output_row)

    print(f"Wrote results to {output_path}")


if __name__ == "__main__":
    main()
