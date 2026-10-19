
import csv
import sqlite3
from pathlib import Path

from career_catalog import (
    DB,
    initialize_catalog,
    count_careers,
)

ROOT = Path(__file__).resolve().parent

ONET_FILE = (
    ROOT / "career_data" / "onet" / "job_titles.csv"
)

# O*NET occupation families and selected related roles.
# This is our initial CS-focused import scope.
CS_CODES = (
    "15-",       # Computer and mathematical occupations
    "11-3021",   # Computer and information systems managers
    "17-2061",   # Computer hardware engineers
)

CATEGORY_RULES = {
    "Artificial Intelligence": [
        "artificial intelligence",
        "machine learning",
        "deep learning",
        "computer vision",
        "natural language",
        "neural network",
        "data mining",
    ],
    "Cybersecurity": [
        "cybersecurity",
        "cyber security",
        "information security",
        "security analyst",
        "penetration test",
        "cryptograph",
        "digital forensic",
        "security engineer",
    ],
    "Data and Analytics": [
        "data scientist",
        "data engineer",
        "data analyst",
        "database",
        "business intelligence",
        "analytics",
        "data architect",
    ],
    "Cloud and Infrastructure": [
        "cloud",
        "devops",
        "site reliability",
        "network",
        "infrastructure",
        "systems administrator",
        "platform engineer",
    ],
    "Systems and Robotics": [
        "hardware",
        "embedded",
        "firmware",
        "robotics",
        "computer architecture",
    ],
    "Testing and Quality": [
        "quality assurance",
        "software tester",
        "test engineer",
        "test automation",
        "quality engineer",
    ],
    "Technology Leadership": [
        "technology manager",
        "information systems manager",
        "engineering manager",
        "it director",
        "chief technology",
    ],
    "Specialized Computing": [
        "quantum",
        "bioinformatics",
        "computational",
        "research scientist",
        "geographic information",
    ],
    "IT and Support": [
        "technical support",
        "help desk",
        "computer support",
        "it support",
        "computer operator",
    ],
}

SOFTWARE_TERMS = [
    "software",
    "programmer",
    "developer",
    "web application",
    "application engineer",
    "computer systems",
]

# Certain titles require manual categorization.
# They should not automatically become application matches.


def classify(title, occupation):
    combined = (title + " " + occupation).lower()

    for category, terms in CATEGORY_RULES.items():
        if any(term in combined for term in terms):
            return category

    if any(term in combined for term in SOFTWARE_TERMS):
        return "Software Development"

    return "Unclassified CS"


def is_cs_occupation(code):
    return code.startswith(CS_CODES)


def import_onet():
    if not ONET_FILE.exists():
        print("Missing O*NET file:")
        print(ONET_FILE)
        print("Download job_titles.csv first.")
        return

    initialize_catalog()

    inserted = 0
    skipped = 0

    with sqlite3.connect(DB) as db:
        # Keep the existing schema and starter records.
        with open(
            ONET_FILE,
            newline="",
            encoding="utf-8-sig"
        ) as file:

            reader = csv.DictReader(file)

            required = {
                "O*NET-SOC Code",
                "Title",
                "Job Title",
            }

            if not required.issubset(reader.fieldnames or []):
                print("Unexpected CSV format.")
                print("Columns:", reader.fieldnames)
                return

            for row in reader:
                code = row["O*NET-SOC Code"].strip()
                occupation = row["Title"].strip()
                title = row["Job Title"].strip()

                if not title or not is_cs_occupation(code):
                    skipped += 1
                    continue

                category = classify(title, occupation)

                cursor = db.execute("""
                    INSERT OR IGNORE INTO career_titles
                    (
                        title,
                        category,
                        source,
                        source_id,
                        language
                    )
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    title,
                    category,
                    "ONET_31.0",
                    code,
                    "en"
                ))

                inserted += cursor.rowcount

    print()
    print("BabyStepsAI O*NET Import")
    print("-----------------------")
    print("New career titles imported:", inserted)
    print("Rows skipped:", skipped)
    print("Total catalog records:", count_careers())
    print()
    print("Import complete.")
    print("Unclassified CS titles require review.")


if __name__ == "__main__":
    import_onet()
