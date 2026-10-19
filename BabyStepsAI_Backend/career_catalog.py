
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent / "babysteps.db"

CATEGORIES = [
    "Software Development",
    "Artificial Intelligence",
    "Data and Analytics",
    "Cybersecurity",
    "Cloud and Infrastructure",
    "Systems and Robotics",
    "Testing and Quality",
    "Specialized Computing",
    "Technology Leadership",
    "IT and Support",
]

# Small starter dataset used to verify the database.
# O*NET and ESCO imports will be added next.
STARTER_ROLES = {
    "Software Development": [
        "Software Engineer",
        "Backend Engineer",
        "Frontend Engineer",
        "Full Stack Developer",
        "Mobile Application Developer",
        "Distributed Systems Engineer",
    ],
    "Artificial Intelligence": [
        "Machine Learning Engineer",
        "Generative AI Engineer",
        "LLM Engineer",
        "Computer Vision Engineer",
    ],
    "Data and Analytics": [
        "Data Scientist",
        "Data Engineer",
        "Analytics Engineer",
    ],
    "Cybersecurity": [
        "Cybersecurity Engineer",
        "Security Analyst",
        "Penetration Tester",
    ],
    "Cloud and Infrastructure": [
        "Cloud Engineer",
        "DevOps Engineer",
        "Site Reliability Engineer",
    ],
    "Systems and Robotics": [
        "Robotics Software Engineer",
        "Firmware Engineer",
        "Embedded Systems Engineer",
    ],
    "Testing and Quality": [
        "QA Engineer",
        "SDET",
    ],
    "Specialized Computing": [
        "Quantum Computing Engineer",
        "Bioinformatics Engineer",
    ],
    "Technology Leadership": [
        "Engineering Manager",
        "Technical Product Manager",
    ],
    "IT and Support": [
        "Systems Administrator",
        "Technical Support Engineer",
    ],
}


class ClosingConnection(sqlite3.Connection):
    """Make ``with connect()`` close the SQLite handle as well as commit."""
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect():
    db = sqlite3.connect(DB, timeout=30, factory=ClosingConnection)
    db.row_factory = sqlite3.Row
    return db


def initialize_catalog():
    with connect() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS career_titles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                category TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'custom',
                source_id TEXT NOT NULL DEFAULT '',
                language TEXT NOT NULL DEFAULT 'en',
                UNIQUE(title, category, language)
            )
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_career_title
            ON career_titles(title)
        """)


def add_career(
    title,
    category,
    source="custom",
    source_id="",
    language="en"
):
    title = title.strip()
    category = category.strip()

    if not title or not category:
        return False

    with connect() as db:
        cursor = db.execute("""
            INSERT OR IGNORE INTO career_titles
            (title, category, source, source_id, language)
            VALUES (?, ?, ?, ?, ?)
        """, (
            title,
            category,
            source,
            source_id,
            language
        ))

        return cursor.rowcount > 0


def search_careers(
    query="",
    categories=None,
    limit=100
):
    initialize_catalog()

    sql = """
        SELECT id, title, category, source, language
        FROM career_titles
        WHERE 1=1
    """

    params = []

    if query.strip():
        sql += " AND title LIKE ?"
        params.append("%" + query.strip() + "%")

    if categories:
        placeholders = ",".join(
            "?" for _ in categories
        )

        sql += (
            f" AND category IN ({placeholders})"
        )

        params.extend(categories)

    sql += " ORDER BY title LIMIT ?"
    params.append(min(max(limit, 1), 500))

    with connect() as db:
        rows = db.execute(sql, params).fetchall()

    return [dict(row) for row in rows]


def count_careers():
    initialize_catalog()

    with connect() as db:
        return db.execute(
            "SELECT COUNT(*) FROM career_titles"
        ).fetchone()[0]


def seed_catalog():
    initialize_catalog()

    for category, titles in STARTER_ROLES.items():
        for title in titles:
            add_career(
                title,
                category,
                source="babysteps_starter"
            )


if __name__ == "__main__":
    seed_catalog()

    print("\nBabyStepsAI Global Career Catalog")
    print("--------------------------------")
    print("Total career titles:", count_careers())

    print("\nSoftware Development:")
    for career in search_careers(
        categories=["Software Development"]
    ):
        print("-", career["title"])

    print("\nSearch example: engineer")
    for career in search_careers("engineer", limit=10):
        print(
            career["title"],
            "|",
            career["category"]
        )
