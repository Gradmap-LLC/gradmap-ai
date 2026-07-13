from http import client
import requests

def recomendations(student_snapshot, context="context/gradmap_context.json", triggers="context/triggers.json"):

    with open("prompts/ai_context_v1.md") as f:
        system_prompt = f.read()
    response = client.messages.create(
    model="claude-haiku-4-5",
    max_tokens=2000,
    system=system_prompt,
    messages=[{
            "role": "user",
            "content": f"Student snapshot:\n{student_snapshot}\n\nContext articles:\n{context}\n\n. Pre-know dates/triggers for the student:\n{triggers}\n\nPlease provide recommendations for the student based on the above."
    }]

    )