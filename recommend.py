
from http import client


def recomendations(student_snapshot):

    uploaded = client.beta.files.upload(file=(student_snapshot, open(student_snapshot, "rb"), "text/plain"))

    with open("prompts/ai_context_v1.md") as f:
        system_prompt = f.read()
    response = client.messages.create(
    model="claude-opus-4-5",
    max_tokens=1024,
    system=system_prompt,
    messages=[
        {"role": "user", "content": f"Please provide recommendations for the student based on the uploaded snapshot: {student_snapshot}."}
    ]
    )