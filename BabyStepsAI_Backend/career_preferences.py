
import json
import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator

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
RELOCATION_OPTIONS = {"unspecified", "anywhere", "selected_locations", "no", "per_job"}

US_STATE_ALIASES = {
    "al":"Alabama","ak":"Alaska","az":"Arizona","ar":"Arkansas","ca":"California","co":"Colorado",
    "ct":"Connecticut","de":"Delaware","fl":"Florida","ga":"Georgia","hi":"Hawaii","id":"Idaho",
    "il":"Illinois","in":"Indiana","ia":"Iowa","ks":"Kansas","ky":"Kentucky","la":"Louisiana",
    "me":"Maine","md":"Maryland","ma":"Massachusetts","mi":"Michigan","mn":"Minnesota","ms":"Mississippi",
    "mo":"Missouri","mt":"Montana","ne":"Nebraska","nv":"Nevada","nh":"New Hampshire","nj":"New Jersey",
    "nm":"New Mexico","ny":"New York","nc":"North Carolina","nd":"North Dakota","oh":"Ohio","ok":"Oklahoma",
    "or":"Oregon","pa":"Pennsylvania","ri":"Rhode Island","sc":"South Carolina","sd":"South Dakota",
    "tn":"Tennessee","tx":"Texas","ut":"Utah","vt":"Vermont","va":"Virginia","wa":"Washington",
    "wv":"West Virginia","wi":"Wisconsin","wy":"Wyoming","dc":"District of Columbia"}


class LocationDestination(BaseModel):
    country: str = Field(default="", max_length=100)
    state_province: str = Field(default="", max_length=100)
    city: str = Field(default="", max_length=100)

    @field_validator("country", "state_province", "city")
    @classmethod
    def normalize_place(cls, value):
        return " ".join(str(value or "").split())


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
    relocation_preference: str = Field(default="unspecified", max_length=30)
    relocation_destinations: list[LocationDestination] = Field(default_factory=list, max_length=30)
    instructions: str = Field(
        default="", max_length=3000
    )
    max_applications_per_day: int = Field(
        default=200, ge=1, le=200
    )


def _normalize_relocation(data):
    mode=str(data.get("relocation_preference") or "unspecified").strip().casefold()
    if mode not in RELOCATION_OPTIONS:
        raise HTTPException(400,"Choose a supported relocation option.")
    destinations=[];seen=set()
    for item in data.get("relocation_destinations",[]):
        if isinstance(item,dict):
            country=str(item.get("country") or "").strip();state=str(item.get("state_province") or "").strip();city=str(item.get("city") or "").strip()
        else:
            country=item.country.strip();state=item.state_province.strip();city=item.city.strip()
        aliases={"us":"United States","u.s.":"United States","usa":"United States","uk":"United Kingdom","u.k.":"United Kingdom"}
        country=aliases.get(country.casefold(),country)
        state=US_STATE_ALIASES.get(state.casefold(),state)
        if not (country or state or city):continue
        value={"country":country,"state_province":state,"city":city}
        key=tuple(str(value[k]).casefold() for k in ("country","state_province","city"))
        if key not in seen:destinations.append(value);seen.add(key)
    data["relocation_preference"]=mode
    data["relocation_destinations"]=destinations
    return data


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

    data=_normalize_relocation(preferences.model_dump())
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
            json.dumps(data,ensure_ascii=False),version,digest
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
* { box-sizing: border-box; }
:root {
    --cp-ink: var(--bsa-ink, #17263b);
    --cp-muted: #52647b;
    --cp-line: var(--bsa-line, #dfe7f1);
    --cp-surface: var(--bsa-surface, #ffffff);
    --cp-wash: var(--bsa-wash, #f4f7fb);
    --cp-accent: #2859a5;
    --cp-selected: #e8f0ff;
    --cp-selected-ink: #173f79;
    --cp-input: #ffffff;
    --cp-focus: #245fcc;
}
html { color-scheme: light; }
html[data-bsa-theme="dark"] {
    color-scheme: dark;
    --cp-ink: #e7edf6;
    --cp-muted: #bdc8d8;
    --cp-line: #46556c;
    --cp-surface: #182337;
    --cp-wash: #101827;
    --cp-accent: #a9c4ff;
    --cp-selected: #293a58;
    --cp-selected-ink: #e7edf6;
    --cp-input: #111b2b;
    --cp-focus: #a9c4ff;
}
body {
    font-family: system-ui, sans-serif;
    max-width: 1000px;
    margin: 30px auto;
    padding: 0 18px;
    background: var(--cp-wash);
    color: var(--cp-ink);
}
header, section {
    background: var(--cp-surface);
    color: var(--cp-ink);
    border: 1px solid var(--cp-line);
    border-radius: 14px;
    padding: 22px;
    margin-bottom: 18px;
}
.invalid-field{border:2px solid #b42318!important;box-shadow:0 0 0 3px #b4231822}
.status-error{color:#a3261d!important}.status-success{color:#137451!important}
html[data-bsa-theme="dark"] .status-error{color:#ffb4ab!important}
html[data-bsa-theme="dark"] .status-success{color:#8fe0b4!important}
.optional-note,.field-hint{color:var(--cp-muted);font-size:13px}
.field-hint{display:block;font-size:12px;margin-top:4px}
header { display:flex; align-items:center; justify-content:space-between; }
h1 { margin:0; } h2 { margin-top:0; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:10px; }
.chip {
    padding:12px; min-height:44px; border:1px solid var(--cp-line);
    border-radius:9px; background:var(--cp-surface); color:var(--cp-ink);
    cursor:pointer; text-align:left; font-size:14px;
}
.chip.active {
    background:var(--cp-selected); border:2px solid var(--cp-accent);
    color:var(--cp-selected-ink); font-weight:650;
}
input[type=text], input[type=search], input[type=number], textarea, select {
    width:100%; padding:11px; margin:8px 0; border:1px solid var(--cp-line);
    border-radius:8px; font:inherit; background:var(--cp-input); color:var(--cp-ink);
}
input::placeholder,textarea::placeholder{color:var(--cp-muted);opacity:1}
input:focus-visible,textarea:focus-visible,select:focus-visible,button:focus-visible,a:focus-visible{outline:3px solid var(--cp-focus);outline-offset:2px}
.options { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); }
.options label { padding:7px; color:var(--cp-ink); border-radius:8px; }
input[type=checkbox]{accent-color:var(--cp-accent)}
button { cursor:pointer; font:inherit; }
.primary { padding:13px 24px; min-height:44px; background:#174f9b; color:#fff; border:0; border-radius:9px; }
html[data-bsa-theme="dark"] .primary{background:#a9c4ff;color:#111b2b}
.secondary,#add-destination,.destination-row button,#addCustom {
    min-height:42px; padding:9px 14px; border:1px solid var(--cp-line);
    border-radius:9px; background:var(--cp-surface); color:var(--cp-ink);
}
#add-destination{margin:8px 0 2px}
.muted { color:var(--cp-muted); }
#results { max-height:330px; overflow-y:auto; }
.pill { display:inline-flex; align-items:center; gap:8px; background:var(--cp-wash); color:var(--cp-ink); border:1px solid var(--cp-line); padding:8px 12px; margin:5px; border-radius:20px; }
.pill button { border:0; background:none; color:var(--cp-ink); font-size:18px; }
.row { display:flex; gap:10px; align-items:center; }
.row input { flex:1; }
.destination-row {
    display:grid; grid-template-columns:repeat(3,minmax(0,1fr)) auto;
    gap:12px; align-items:end; padding:14px; margin:10px 0;
    background:var(--cp-wash); border:1px solid var(--cp-line); border-radius:10px;
}
.destination-row label{min-width:0;font-size:14px;color:var(--cp-ink)}
.destination-row input{margin-bottom:0}
.destination-intro{margin:4px 0 8px}
button:disabled{opacity:.65;cursor:wait}
@media(max-width:640px){
    body{margin:14px auto;padding:0 12px}
    header,section{padding:16px}
    header{align-items:flex-start;gap:12px;flex-wrap:wrap}
    .destination-row{grid-template-columns:1fr;gap:4px}
    .row{flex-wrap:wrap}
    .row button{min-height:44px}
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
        Relocation Preference <span class="field-hint">Does not establish current residence or work authorization</span>
        <select id="relocation">
            <option value="unspecified">Not decided yet</option>
            <option value="anywhere">Willing to relocate anywhere</option>
            <option value="selected_locations">Willing to relocate only to selected locations</option>
            <option value="no">Not willing to relocate</option>
            <option value="per_job">Decide separately for each job</option>
        </select>
    </label>
    <div id="relocation-destinations" hidden></div>
    <button id="add-destination" type="button" hidden>Add relocation destination</button>
    <p class="field-hint destination-intro" hidden>Enter a country, state or province, or city. Destinations are retained when you switch options, but apply only while selected locations is active.</p>

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
let relocationDestinations = [];

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
    button.setAttribute("aria-pressed", String(selected.has(title)));

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
            button.setAttribute("aria-pressed", String(selectedCategories.has(category)));

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

function renderRelocationDestinations(){
    const root=$("relocation-destinations");root.replaceChildren();
    const active=$("relocation").value==="selected_locations";
    root.hidden=!active;
    $("add-destination").hidden=!active;
    document.querySelector(".destination-intro").hidden=!active;
    const suggestions={country:["United States","Canada","United Kingdom","Australia","India","Germany","France","Ireland","Netherlands","New Zealand","Singapore","Mexico"],state_province:["Alabama","Alaska","Arizona","California","Colorado","Florida","Georgia","Illinois","Massachusetts","Michigan","New Jersey","New York","North Carolina","Ohio","Oregon","Pennsylvania","Texas","Virginia","Washington","Ontario","Quebec","British Columbia","Alberta"]};
    for(const [key,values] of Object.entries(suggestions)){const list=document.createElement("datalist");list.id="relocation-"+key+"-options";for(const value of values){const option=document.createElement("option");option.value=value;list.append(option)}root.append(list)}
    relocationDestinations.forEach((item,index)=>{
        const row=document.createElement("div");row.className="destination-row";
        for(const [key,labelText] of [["country","Country"],["state_province","State or province"],["city","City"]]){
            const label=document.createElement("label");label.textContent=labelText;
            const input=document.createElement("input");input.type="search";input.value=item[key]||"";
            if(key!=="city")input.setAttribute("list","relocation-"+key+"-options");
            input.setAttribute("aria-label",labelText+" for relocation destination "+(index+1));
            input.oninput=()=>item[key]=input.value;label.append(input);row.append(label);
        }
        const remove=document.createElement("button");remove.type="button";remove.textContent="Remove";
        remove.onclick=()=>{relocationDestinations.splice(index,1);renderRelocationDestinations()};row.append(remove);root.append(row);
    });
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
        $("relocation").value=preferences.relocation_preference||"unspecified";
        relocationDestinations=Array.isArray(preferences.relocation_destinations)?preferences.relocation_destinations.map(x=>({...x})):[];
        renderRelocationDestinations();

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
        relocation_preference:$("relocation").value,
        relocation_destinations:relocationDestinations.filter(x=>x.country||x.state_province||x.city),
        instructions: $("instructions").value,
        max_applications_per_day: Number(
            $("limit").value
        )
    };

    button.disabled=true;button.setAttribute("aria-busy","true");status.className="";status.textContent="";
    document.querySelectorAll(".invalid-field").forEach(x=>{x.classList.remove("invalid-field");x.removeAttribute("aria-invalid")});
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
        const keys=["categories","roles","levels","employment_types","location","work_arrangement","relocation_preference","instructions","max_applications_per_day"];
        const canonical=value=>Array.isArray(value)?"["+value.map(canonical).join(",")+"]":value&&typeof value==="object"?"{"+Object.keys(value).sort().map(k=>JSON.stringify(k)+":"+canonical(value[k])).join(",")+"}":JSON.stringify(value);
        for(const key of keys){if(canonical(persisted[key])!==canonical(preferences[key]))throw new Error("The saved "+key.replaceAll("_"," ")+" could not be verified. No success confirmation was shown.");}
        const stateNames={al:'alabama',ak:'alaska',az:'arizona',ar:'arkansas',ca:'california',co:'colorado',ct:'connecticut',de:'delaware',fl:'florida',ga:'georgia',hi:'hawaii',id:'idaho',il:'illinois',in:'indiana',ia:'iowa',ks:'kansas',ky:'kentucky',la:'louisiana',me:'maine',md:'maryland',ma:'massachusetts',mi:'michigan',mn:'minnesota',ms:'mississippi',mo:'missouri',mt:'montana',ne:'nebraska',nv:'nevada',nh:'new hampshire',nj:'new jersey',nm:'new mexico',ny:'new york',nc:'north carolina',nd:'north dakota',oh:'ohio',ok:'oklahoma',or:'oregon',pa:'pennsylvania',ri:'rhode island',sc:'south carolina',sd:'south dakota',tn:'tennessee',tx:'texas',ut:'utah',vt:'vermont',va:'virginia',wa:'washington',wv:'west virginia',wi:'wisconsin',wy:'wyoming',dc:'district of columbia'};
        const destinationKey=item=>{let country=(item.country||'').trim().toLowerCase(),state=(item.state_province||'').trim().toLowerCase(),city=(item.city||'').trim().toLowerCase();country=({us:'united states','u.s.':'united states',usa:'united states',uk:'united kingdom','u.k.':'united kingdom'})[country]||country;state=stateNames[state]||state;return JSON.stringify([country,state,city])};
        const expectedDestinations=[...new Set(preferences.relocation_destinations.map(destinationKey))].sort(),actualDestinations=[...new Set((persisted.relocation_destinations||[]).map(destinationKey))].sort();
        if(JSON.stringify(expectedDestinations)!==JSON.stringify(actualDestinations))throw new Error("The saved relocation destinations could not be verified.");
        relocationDestinations=persisted.relocation_destinations.map(x=>({...x}));
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

$("relocation").onchange=renderRelocationDestinations;
$("add-destination").onclick=()=>{relocationDestinations.push({country:"",state_province:"",city:""});renderRelocationDestinations();$("relocation-destinations").querySelector("input")?.focus()};

initialize();
</script>

</body>
</html>
"""
