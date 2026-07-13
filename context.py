import json
import argparse

from recommend import recomendations

parser = argparse.ArgumentParser()
parser.add_argument("--student", type=str, required=True, help="The user ID of the student")
args = parser.parse_args()

user_id = args.student

with open("mock_students_sample.json", "r") as file:
    data = json.load(file)

for student in data["students"]:
    if student["student_id"] == user_id:
        print(student)
        break
else:
    raise ValueError(f"Student with ID {user_id} not found.")

print(recomendations(student))    