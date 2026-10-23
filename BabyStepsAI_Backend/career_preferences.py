
import json
import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from career_catalog import (
    initialize_catalog,
    search_careers,
    count_careers,
    CATEGORIES,
)
from ui_theme import apply_ui_theme
from matching_versions import fingerprint

router = APIRouter()

DB = Path(__file__).resolve().parent / "babysteps.db"
USER = "local-test-user"

FEATURED = [
    "Software Development",
    "Artificial Intelligence",
    "Data and Analytics",
    "Cybersecurity",
    "Cloud and Infrastructure",
    "Systems and Robotics",
    "Testing and Quality",
    "Specialized Computing",
]

LEVELS = [
    "Internship",
    "New Graduate",
    "Entry Level",
    "Junior",
    "Associate",
    "Mid Level",
    "Senior",
    "Staff / Principal",
    "Manager",
    "Director",
    "VP / Executive",
]

TYPES = [
    "Full-time",
    "Part-time",
    "Internship",
    "Contract",
    "Temporary",
    "Freelance",
]

WORK = ["Any", "Remote", "Hybrid", "On-site"]


class Preferences(BaseModel):
    categories: list[str] = Field(
        default_factory=list, max_length=20
    )
    roles: list[str] = Field(
        default_factory=list, max_length=100
    )
    levels: list[str] = Field(default_factory=list)
    employment_types: list[str] = Field(
        default_factory=list
    )
    location: str = Field(
        default="United States", max_length=200
    )
    work_arrangement: str = "Any"
    instructions: str = Field(
        default="", max_length=3000
    )
    max_applications_per_day: int = Field(
        default=200, ge=1, le=200
    )


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect():
    db = sqlite3.connect(DB, timeout=30, factory=ClosingConnection)
    db.row_factory = sqlite3.Row
    return db


initialize_catalog()

with connect() as db:
    db.execute("""
        CREATE TABLE IF NOT EXISTS career_preferences (
            user_id TEXT PRIMARY KEY,
            preferences_json TEXT NOT NULL
        )
    """)
    columns={row[1] for row in db.execute("PRAGMA table_info(career_preferences)")}
    if "preference_version" not in columns:
        db.execute("ALTER TABLE career_preferences ADD COLUMN preference_version INTEGER NOT NULL DEFAULT 1")
    if "preferences_fingerprint" not in columns:
        db.execute("ALTER TABLE career_preferences ADD COLUMN preferences_fingerprint TEXT NOT NULL DEFAULT ''")
    for row in db.execute("SELECT user_id,preferences_json,preferences_fingerprint FROM career_preferences").fetchall():
        digest=fingerprint(json.loads(row["preferences_json"]))
        if not row["preferences_fingerprint"]:
            db.execute("UPDATE career_preferences SET preferences_fingerprint=? WHERE user_id=?",(digest,row["user_id"]))


@router.get("/api/careers")
def get_careers():
    return {
        "featured": FEATURED,
        "categories": CATEGORIES,
        "levels": LEVELS,
        "employment_types": TYPES,
        "work_arrangements": WORK,
        "total_records": count_careers(),
    }


@router.get("/api/careers/search")
def search_catalog(
    q: str = Query(default="", max_length=100),
    categories: str = Query(default=""),
    limit: int = Query(default=100, ge=1, le=500),
):
    selected = [
        item.strip()
        for item in categories.split(",")
        if item.strip()
    ]

    if any(c not in CATEGORIES for c in selected):
        raise HTTPException(
            400, "Invalid career category"
        )

    jobs = search_careers(
        query=q,
        categories=selected or None,
        limit=limit,
    )

    return {
        "results": jobs,
        "returned": len(jobs),
        "limit": limit,
    }


@router.get("/api/career-preferences")
def get_preferences():
    with connect() as db:
        row = db.execute(
            "SELECT preferences_json,preference_version "
            "FROM career_preferences WHERE user_id=?",
            (USER,)
        ).fetchone()

    if row:
        response=Preferences(
            **json.loads(row["preferences_json"])
        ).model_dump()
        response["preference_version"]=row["preference_version"]
        return response

    response=Preferences(
        categories=["Software Development"],
        roles=[
            "Software Engineer",
            "Backend Engineer",
            "Full Stack Developer",
        ],
        levels=[
            "New Graduate",
            "Entry Level",
            "Junior",
            "Associate",
        ],
        employment_types=["Full-time"],
    ).model_dump()
    response["preference_version"]=1
    return response


@router.put("/api/career-preferences")
def save_preferences(preferences: Preferences):
    if any(
        category not in CATEGORIES
        for category in preferences.categories
    ):
        raise HTTPException(
            400, "Invalid category"
        )

    if any(
        not role.strip() or len(role) > 100
        for role in preferences.roles
    ):
        raise HTTPException(
            400, "Invalid job title"
        )

    if len({
        role.casefold()
        for role in preferences.roles
    }) != len(preferences.roles):
        raise HTTPException(
            400, "Duplicate job titles"
        )

    if any(
        level not in LEVELS
        for level in preferences.levels
    ):
        raise HTTPException(
            400, "Invalid experience level"
        )

    if any(
        kind not in TYPES
        for kind in preferences.employment_types
    ):
        raise HTTPException(
            400, "Invalid employment type"
        )

    if preferences.work_arrangement not in WORK:
        raise HTTPException(
            400, "Invalid work arrangement"
        )

    data=preferences.model_dump()
    digest=fingerprint(data)
    with connect() as db:
        columns={row[1] for row in db.execute("PRAGMA table_info(career_preferences)")}
        if "preference_version" not in columns:
            db.execute("ALTER TABLE career_preferences ADD COLUMN preference_version INTEGER NOT NULL DEFAULT 1")
        if "preferences_fingerprint" not in columns:
            db.execute("ALTER TABLE career_preferences ADD COLUMN preferences_fingerprint TEXT NOT NULL DEFAULT ''")
        old=db.execute("SELECT preferences_json,preferences_fingerprint,preference_version FROM career_preferences WHERE user_id=?",(USER,)).fetchone()
        old_digest=(old["preferences_fingerprint"] or fingerprint(json.loads(old["preferences_json"]))) if old else ""
        changed=bool(old and old_digest!=digest)
        version=(int(old["preference_version"] or 1)+int(changed)) if old else 1
        db.execute("""
            INSERT INTO career_preferences
            (user_id, preferences_json, preference_version, preferences_fingerprint)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
            preferences_json=excluded.preferences_json,
            preference_version=excluded.preference_version,
            preferences_fingerprint=excluded.preferences_fingerprint
        """, (
            USER,
            preferences.model_dump_json(),version,digest
        ))
        if changed:
            table=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_match_results'").fetchone()
            if table:
                match_columns={row[1] for row in db.execute("PRAGMA table_info(job_match_results)")}
                if "freshness_status" in match_columns:
                    db.execute("UPDATE job_match_results SET freshness_status='stale' WHERE user_id=? AND status='completed'",(USER,))

    return {"saved": True,"preference_version":version}


@router.get("/careers", response_class=HTMLResponse)
def careers_page():
    return apply_ui_theme(PAGE)


PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport"
content="width=device-width,initial-scale=1">

<title>BabyStepsAI - Career Preferences</title>

<style>
* {
    box-sizing: border-box;
}

body {
    font-family: system-ui, sans-serif;
    max-width: 1000px;
    margin: 30px auto;
    padding: 0 18px;
    background: #f8fafc;
    color: #172033;
}

header, section {
    background: white;
    border: 1px solid #e2e8f0;
    border-radius: 14px;
    padding: 22px;
    margin-bottom: 18px;
}
.invalid-field{border:2px solid #b42318!important;box-shadow:0 0 0 3px #b4231822}.status-error{color:#a3261d}.status-success{color:#137451}.optional-note{color:#64748b;font-size:13px}.field-hint{display:block;color:#64748b;font-size:12px;margin-top:4px}

header {
    display: flex;
    align-items: center;
    justify-content: space-between;
}

h1 {
    margin: 0;
}

h2 {
    margin-top: 0;
}

.grid {
    display: grid;
    grid-template-columns:
        repeat(auto-fit, minmax(190px, 1fr));
    gap: 10px;
}

.chip {
    padding: 12px;
    border: 1px solid #cbd5e1;
    border-radius: 9px;
    background: white;
    cursor: pointer;
    text-align: left;
    font-size: 14px;
}

.chip.active {
    background: #eff6ff;
    border-color: #2563eb;
    color: #1d4ed8;
}

input[type=text],
input[type=number],
textarea,
select {
    width: 100%;
    padding: 11px;
    margin: 8px 0;
    border: 1px solid #cbd5e1;
    border-radius: 8px;
    font: inherit;
}

.options {
    display: grid;
    grid-template-columns:
        repeat(auto-fit, minmax(170px, 1fr));
}

.options label {
    padding: 7px;
}

button {
    cursor: pointer;
    font: inherit;
}

.primary {
    padding: 13px 24px;
    background: #2563eb;
    color: white;
    border: none;
    border-radius: 9px;
}

.muted {
    color: #64748b;
}

#results {
    max-height: 330px;
    overflow-y: auto;
}

.pill {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    background: #e2e8f0;
    padding: 8px 12px;
    margin: 5px;
    border-radius: 20px;
}

.pill button {
    border: none;
    background: none;
    font-size: 18px;
}

.row {
    display: flex;
    gap: 10px;
    align-items: center;
}

.row input {
    flex: 1;
}
</style>
</head>

<body>

<header>
    <div>
        <h1>BabyStepsAI</h1>
        <p class="muted">
            Global Computer Science Career Search
        </p>
    </div>

    <a href="/">Back to Resumes</a>
</header>
<p class="optional-note">All career preferences are optional. Unset values do not block saving and keep discovery broader; a specific job may still ask for additional eligibility information.</p>

<section>
    <h2>Popular Technology Career Categories</h2>

    <p class="muted">
        Select one or more categories.
        The search results update automatically.
    </p>

    <div id="featured" class="grid"></div>
</section>

<section>
    <h2>Search Technology Careers</h2>

    <p id="catalog-count" class="muted"></p>

    <input
        id="search"
        type="text"
        placeholder="Search software, security, AI, robotics..."
    >

    <p id="count" class="muted"></p>

    <div id="results" class="grid"></div>

    <h3>Add a Custom Job Title</h3>

    <div class="row">
        <input
            id="custom"
            type="text"
            maxlength="100"
            placeholder="Any technology job title"
        >

        <button id="addCustom">Add</button>
    </div>

    <h3>Selected Job Titles</h3>
    <div id="selected"></div>
</section>

<section>
    <h2>Experience Levels</h2>
    <div id="levels" class="options"></div>
</section>

<section>
    <h2>Employment Types</h2>
    <div id="types" class="options"></div>
</section>

<section>
    <h2>Location and Work Preferences</h2>
    <p class="optional-note">Preferences are optional refinements. Leave a field unset to keep job discovery broader; saving remains available.</p>

    <label>
        Preferred Location <span class="field-hint">Optional</span>
        <input id="location" type="text">
    </label>

    <label>
        Work Arrangement
        <select id="work">
            <option>Any</option>
            <option>Remote</option>
            <option>Hybrid</option>
            <option>On-site</option>
        </select>
    </label>

    <label>
        Maximum Applications Per 24 Hours
        <input
            id="limit"
            type="number"
            min="1"
            max="200"
            value="200"
        >
    </label>
</section>

<section>
    <h2>Additional AI Instructions</h2>

    <textarea
        id="instructions"
        rows="5"
        maxlength="3000"
        placeholder="Describe your ideal jobs..."
    ></textarea>
</section>

<section>
    <button id="save" class="primary">
        Save My Preferences
    </button>

    <p id="status" role="status" aria-live="polite"></p>
</section>

<script>
const $ = id => document.getElementById(id);

let selected = new Set();
let selectedCategories = new Set();
let levels = new Set();
let types = new Set();

let allCategories = [];
let searchVersion = 0;
let searchTimer = null;

async function api(url, options) {
    const response = await fetch(url, options);
    const data = await response.json();

    if (!response.ok) {
        const detail=data.detail||data;
        const message=Array.isArray(detail)?detail.map(x=>{
            const field=Array.isArray(x.loc)?x.loc.filter(y=>y!=="body").join(" → "):"";
            return (field?field+": ":"")+(x.msg||"Invalid value");
        }).join("; "):(typeof detail==="string"?detail:"The request could not be saved. Review the highlighted settings and try again.");
        const error=new Error(message);error.detail=detail;throw error;
    }

    return data;
}

function careerButton(title) {
    const button = document.createElement("button");
    button.type = "button";

    button.className = "chip" +
        (selected.has(title) ? " active" : "");

    button.textContent =
        (selected.has(title) ? "✓ " : "+ ") + title;

    button.onclick = () => {
        if (selected.has(title)) {
            selected.delete(title);
        } else {
            selected.add(title);
        }

        renderSelected();
        scheduleSearch();
    };

    return button;
}

function renderCategories() {
    $("featured").replaceChildren(
        ...allCategories.map(category => {
            const button = document.createElement("button");

            button.type = "button";

            button.className = "chip" +
                (selectedCategories.has(category)
                    ? " active" : "");

            button.textContent =
                (selectedCategories.has(category)
                    ? "✓ " : "+ ") + category;

            button.onclick = () => {
                if (selectedCategories.has(category)) {
                    selectedCategories.delete(category);
                } else {
                    selectedCategories.add(category);
                }

                renderCategories();
                scheduleSearch();
            };

            return button;
        })
    );
}

function renderSelected() {
    const pills = [...selected].map(title => {
        const pill = document.createElement("span");
        pill.className = "pill";

        pill.append(
            document.createTextNode(title)
        );

        const remove = document.createElement("button");
        remove.type = "button";
        remove.textContent = "×";

        remove.onclick = () => {
            selected.delete(title);
            renderSelected();
            scheduleSearch();
        };

        pill.append(remove);
        return pill;
    });

    $("selected").replaceChildren(...pills);
}

function scheduleSearch() {
    clearTimeout(searchTimer);

    const version = ++searchVersion;

    searchTimer = setTimeout(
        () => searchDatabase(version),
        200
    );
}

async function searchDatabase(version) {
    const params = new URLSearchParams({
        q: $("search").value.trim(),
        categories: [...selectedCategories].join(","),
        limit: "100"
    });

    $("count").textContent = "Searching database...";

    try {
        const data = await api(
            "/api/careers/search?" + params.toString()
        );

        if (version !== searchVersion) {
            return;
        }

        $("results").replaceChildren(
            ...data.results.map(item =>
                careerButton(item.title)
            )
        );

        $("count").textContent =
            "Showing " + data.returned +
            " titles. Refine your search to see more.";

    } catch (error) {
        if (version === searchVersion) {
            $("count").textContent =
                "Search failed: " + error.message;
        }
    }
}

function checkboxes(id, values, selectedSet) {
    $(id).replaceChildren(
        ...values.map(value => {
            const label = document.createElement("label");
            const input = document.createElement("input");

            input.type = "checkbox";
            input.checked = selectedSet.has(value);

            input.onchange = () => {
                if (input.checked) {
                    selectedSet.add(value);
                } else {
                    selectedSet.delete(value);
                }
            };

            label.append(
                input,
                document.createTextNode(" " + value)
            );

            return label;
        })
    );
}

async function initialize() {
    try {
        const [careers, preferences] = await Promise.all([
            api("/api/careers"),
            api("/api/career-preferences")
        ]);

        // FIX: Handle categories returned as either
        // a Python list or a Python dictionary.
        allCategories = Array.isArray(careers.categories)
            ? careers.categories
            : Object.keys(careers.categories);

        selected = new Set(preferences.roles);

        selectedCategories = new Set(
            preferences.categories || []
        );

        levels = new Set(preferences.levels);
        types = new Set(preferences.employment_types);

        $("catalog-count").textContent =
            careers.total_records +
            " career-title records in the database";

        $("location").value = preferences.location;
        $("work").value = preferences.work_arrangement;

        $("limit").value =
            preferences.max_applications_per_day;

        $("instructions").value =
            preferences.instructions;

        checkboxes(
            "levels",
            careers.levels,
            levels
        );

        checkboxes(
            "types",
            careers.employment_types,
            types
        );

        renderCategories();
        renderSelected();
        scheduleSearch();

    } catch (error) {
        $("status").textContent =
            "Loading failed: " + error.message;
    }
}

$("search").oninput = scheduleSearch;

$("addCustom").onclick = () => {
    const title = $("custom").value.trim();

    if (!title || title.length > 100) {
        return;
    }

    selected.add(title);
    $("custom").value = "";
    renderSelected();
};

$("save").onclick = async () => {
    const button=$("save"),status=$("status");
    const preferences = {
        categories: [...selectedCategories],
        roles: [...selected],
        levels: [...levels],
        employment_types: [...types],
        location: $("location").value.trim(),
        work_arrangement: $("work").value,
        instructions: $("instructions").value,
        max_applications_per_day: Number(
            $("limit").value
        )
    };

    button.disabled=true;button.setAttribute("aria-busy","true");status.className="";status.textContent="";
    document.querySelectorAll(".invalid-field").forEach(x=>x.classList.remove("invalid-field"));
    try {
        const saved=await api("/api/career-preferences", {
            method: "PUT",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify(preferences)
        });
        if(!saved.saved)throw new Error("The server did not confirm that preferences were saved.");
        const persisted=await api("/api/career-preferences");
        const keys=["categories","roles","levels","employment_types","location","work_arrangement","instructions","max_applications_per_day"];
        const canonical=value=>Array.isArray(value)?"["+value.map(canonical).join(",")+"]":value&&typeof value==="object"?"{"+Object.keys(value).sort().map(k=>JSON.stringify(k)+":"+canonical(value[k])).join(",")+"}":JSON.stringify(value);
        for(const key of keys){if(canonical(persisted[key])!==canonical(preferences[key]))throw new Error("The saved "+key.replaceAll("_"," ")+" could not be verified. No success confirmation was shown.");}
        status.className="status-success";
        status.textContent="Preferences saved and verified. Unset preferences remain optional and do not block saving.";

    } catch (error) {
        status.className="status-error";status.textContent="Save failed: "+error.message;
        const detail=error.detail;
        const firstIssue=Array.isArray(detail)?detail.find(x=>Array.isArray(x.loc)):null;const parts=firstIssue?.loc||[];const loc=parts.length?parts[parts.length-1]:null;
        const fieldMap={location:$("location"),work_arrangement:$("work"),instructions:$("instructions"),max_applications_per_day:$("limit")};
        if(fieldMap[loc]){fieldMap[loc].classList.add("invalid-field");fieldMap[loc].setAttribute("aria-invalid","true");fieldMap[loc].focus();}
        else if(/category/i.test(error.message))document.querySelector("#featured button")?.focus();
        else if(/experience level/i.test(error.message))document.querySelector("#levels input")?.focus();
        else if(/employment type/i.test(error.message))document.querySelector("#types input")?.focus();
    } finally {
        button.disabled=false;button.removeAttribute("aria-busy");
    }
};

initialize();
</script>

</body>
</html>
"""
