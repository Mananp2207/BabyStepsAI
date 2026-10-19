
import json
import re
import pymupdf

RESUME = "resume.pdf"
PROFILE = "candidate_profile.json"


def extract_education(text):
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]

    education = []
    degree_pattern = re.compile(
        r"\b(bachelor|master|ph\.?d\.?|doctorate|"
        r"b\.?s\.?|b\.?a\.?|m\.?s\.?|m\.?a\.?|"
        r"associate degree|b\.?tech|m\.?tech)\b",
        re.IGNORECASE
    )

    for index, line in enumerate(lines):
        if degree_pattern.search(line):
            start = max(0, index - 2)
            end = min(len(lines), index + 3)

            context = " | ".join(lines[start:end])

            education.append({
                "degree_text": line,
                "context": context,
                "verified": False
            })

    return education


def main():
    with pymupdf.open(RESUME) as doc:
        text = "\n".join(
            page.get_text() for page in doc
        )

    with open(PROFILE) as file:
        profile = json.load(file)

    education = extract_education(text)

    if education:
        profile["education"] = education

        with open(PROFILE, "w") as file:
            json.dump(profile, file, indent=4)

    print("\nBabyStepsAI Education Extraction")
    print("--------------------------------")
    print("Education entries found:", len(education))

    for entry in education:
        print("\nDegree:", entry["degree_text"])
        print("Context:", entry["context"])

    if not education:
        print("No degree information detected.")
        print("Education requires manual verification.")


if __name__ == "__main__":
    main()
