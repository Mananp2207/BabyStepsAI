"""Rule-based discovery from the supported public Greenhouse job boards."""

import json
import logging
import os
import re
import sqlite3
import socket
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import ipaddress
import uuid
import email.utils
from datetime import timedelta
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from io import StringIO

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from job_relevance import (_arrangement, evaluate_location, evaluate_relocation_preference, extract_experience_requirements,
                           infer_prompt_preferences, normalize_job_record, relevance_for_job,
                           classify_experience_level, discovery_relevance_score,
                           extract_graduation_windows, extract_education_requirements,
                           extract_gpa_requirements, extract_internship_requirements,
                           classify_career_category, infer_country_from_location,
                           classify_role_level, extract_sponsorship_requirement)
from ui_theme import apply_ui_theme
from job_connectors import (EmployerSource, canonicalize_job_url, discover_source_candidates, fetch_source,
                            fetch_public_job_url, normalize_job, observe_fetch, validate_public_https_url)

router = APIRouter()
DB = Path(__file__).resolve().parent / "babysteps.db"
USER = "local-test-user"
FEED_CACHE_TTL = 15 * 60
EMPLOYER_DISCOVERY_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="employer-discovery")
BOARDS = {"Cloudflare": "cloudflare", "Datadog": "datadog", "Figma": "figma"}
SENIOR = re.compile(r"\b(senior|staff|principal|lead|director|manager|vp|head of)\b", re.I)
EARLY = re.compile(r"\b(intern|internship|junior|entry[- ]level|new grad|graduate|associate|software engineer\s+i|swe\s+i)\b", re.I)
INTERN = re.compile(r"\b(intern|internship)\b", re.I)
SOFTWARE_ROLE_ALIASES = (
    "software engineer", "software developer", "software development engineer",
    "software engineering", "swe", "backend engineer", "back-end engineer",
    "backend software engineer", "back-end software engineer", "frontend engineer",
    "front-end engineer", "frontend software engineer", "front-end software engineer",
    "full stack engineer", "full-stack engineer", "fullstack engineer",
    "full stack developer", "full-stack developer", "fullstack developer",
    "application engineer", "application developer",
)
CATEGORY_TERMS = {
    "software development": SOFTWARE_ROLE_ALIASES + ("programmer",),
    "artificial intelligence": ("machine learning", "ai engineer", "artificial intelligence", "llm", "research scientist"),
    "data and analytics": ("data engineer", "data scientist", "data analyst", "analytics engineer"),
    "cybersecurity": ("security engineer", "cybersecurity", "security analyst", "penetration tester"),
    "cloud and infrastructure": ("cloud engineer", "devops", "infrastructure engineer", "site reliability", "platform engineer"),
    "systems and robotics": ("systems engineer", "robotics", "embedded", "firmware"),
    "testing and quality": ("test engineer", "quality engineer", "qa engineer", "sdet"),
    "specialized computing": ("quantum", "bioinformatics"),
    "technology leadership": ("engineering manager", "technical product manager"),
    "it and support": ("technical support", "systems administrator", "help desk"),
}
_FEED_CACHE = {}
_FEED_CACHE_LOCK = threading.Lock()
_DISCOVERY_LOCK = threading.Lock()
DISCOVERY_LEASE_SECONDS = 90
DISCOVERY_HEARTBEAT_SECONDS = 15
logger=logging.getLogger("babysteps.job_discovery")


class SearchRequest(BaseModel):
    prompt: str = Field(min_length=3, max_length=1500)
    run_id: str | None = Field(default=None, max_length=64)
    source_keys: list[str] = Field(default_factory=list, max_length=15)


class JobUrlImportRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)


class EmployerImportItem(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    source_type: str = Field(pattern="^(greenhouse|lever|ashby|smartrecruiters|career_page|public_html|rss|sitemap|workday)$")
    source_token: str = Field(default="", max_length=200)
    careers_url: str = Field(default="", max_length=1000)
    geographic_coverage: str = Field(default="", max_length=300)
    website_url: str = Field(default="", max_length=1000)
    evidence_url: str = Field(default="", max_length=1000)
    candidate_source: str = Field(default="", max_length=120)
    candidate_record_url: str = Field(default="", max_length=1000)
    candidate_license: str = Field(default="", max_length=80)


class EmployerImportRequest(BaseModel):
    sources: list[EmployerImportItem] = Field(min_length=1, max_length=100)


class EmployerSourceDiscoveryRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    website_url: str = Field(min_length=8, max_length=1000)


class EmployerDiscoveryRunRequest(BaseModel):
    providers: list[str] = Field(default_factory=lambda:["yc_api"], min_length=1, max_length=2)
    limit: int = Field(default=20, ge=1, le=100)
    seed_limit: int = Field(default=100, ge=1, le=500)


def connect():
    db = sqlite3.connect(DB, timeout=30)
    db.row_factory = sqlite3.Row
    return db


@contextmanager
def database():
    db = connect()
    try:
        yield db
        db.commit()
    finally:
        db.close()


def initialize():
    with database() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS discovered_openings (
            user_id TEXT NOT NULL, job_key TEXT NOT NULL,
            company TEXT NOT NULL, title TEXT NOT NULL,
            location TEXT NOT NULL, url TEXT NOT NULL,
            discovered_at TEXT NOT NULL,
            PRIMARY KEY(user_id, job_key)
        )""")
        existing = {row[1] for row in db.execute("PRAGMA table_info(discovered_openings)").fetchall()}
        for name, declaration in (("employment_type", "TEXT NOT NULL DEFAULT ''"),
                                  ("location_status", "TEXT NOT NULL DEFAULT 'unknown'"),
                                  ("location_reason", "TEXT NOT NULL DEFAULT ''"),
                                  ("search_prompt", "TEXT NOT NULL DEFAULT ''"),
                                  ("discovery_source", "TEXT NOT NULL DEFAULT 'Company Careers Page'")):
            if name not in existing:
                db.execute(f"ALTER TABLE discovered_openings ADD COLUMN {name} {declaration}")
        additions = (
            ("source_platform", "TEXT NOT NULL DEFAULT 'greenhouse'"), ("source_id", "TEXT NOT NULL DEFAULT ''"),
            ("country", "TEXT NOT NULL DEFAULT ''"), ("work_arrangement", "TEXT NOT NULL DEFAULT ''"),
            ("description", "TEXT NOT NULL DEFAULT ''"), ("posting_date", "TEXT"),
            ("last_verified_at", "TEXT"), ("active", "INTEGER NOT NULL DEFAULT 1"),
            ("content_fingerprint", "TEXT NOT NULL DEFAULT ''"), ("job_url", "TEXT NOT NULL DEFAULT ''"),
            ("department", "TEXT NOT NULL DEFAULT ''"),
            ("career_category","TEXT"),("experience_level","TEXT"),("salary_json","TEXT"),
            ("required_qualifications_json","TEXT"),("preferred_qualifications_json","TEXT"),
            ("graduation_windows_json","TEXT NOT NULL DEFAULT '[]'"),
            ("availability_status","TEXT NOT NULL DEFAULT 'unknown'"),
            ("reopened_review_required","INTEGER NOT NULL DEFAULT 0"),
            ("imported_source_url","TEXT NOT NULL DEFAULT ''"),
            ("import_eligibility_status","TEXT NOT NULL DEFAULT 'needs_review'"),
            ("import_review_json","TEXT NOT NULL DEFAULT '[]'"),
        )
        for name, declaration in additions:
            if name not in existing:
                db.execute(f"ALTER TABLE discovered_openings ADD COLUMN {name} {declaration}")
        db.execute("""CREATE TABLE IF NOT EXISTS employers (
            employer_id TEXT PRIMARY KEY, name TEXT NOT NULL, careers_url TEXT NOT NULL DEFAULT '',
            source_type TEXT NOT NULL, source_token TEXT NOT NULL, public_feed_url TEXT NOT NULL DEFAULT '',
            geographic_coverage TEXT NOT NULL DEFAULT '', last_success_at TEXT, last_attempt_at TEXT,
            connector_status TEXT NOT NULL DEFAULT 'ready', error_details TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(source_type,source_token))""")
        employer_columns = {row[1] for row in db.execute("PRAGMA table_info(employers)").fetchall()}
        if "verification_evidence_json" not in employer_columns:
            db.execute("ALTER TABLE employers ADD COLUMN verification_evidence_json TEXT NOT NULL DEFAULT '{}'")
        # Additive provenance fields preserve all existing employers and user data.
        for name, declaration in (("website_url", "TEXT NOT NULL DEFAULT ''"),
                                  ("evidence_url", "TEXT NOT NULL DEFAULT ''"),
                                  ("candidate_source", "TEXT NOT NULL DEFAULT ''"),
                                  ("candidate_record_url", "TEXT NOT NULL DEFAULT ''"),
                                  ("candidate_license", "TEXT NOT NULL DEFAULT ''"),
                                  ("discovered_at", "TEXT"),
                                  ("discovery_status", "TEXT NOT NULL DEFAULT 'needs_verification'")):
            if name not in employer_columns:
                db.execute(f"ALTER TABLE employers ADD COLUMN {name} {declaration}")
        # Health and retry fields are additive; existing source state and jobs
        # remain intact. Fingerprints let changed source configurations retry
        # immediately while unchanged unsupported pages cool down.
        for name, declaration in (("health_reason", "TEXT NOT NULL DEFAULT ''"),
                                  ("consecutive_failures", "INTEGER NOT NULL DEFAULT 0"),
                                  ("next_retry_at", "TEXT"),
                                  ("source_fingerprint", "TEXT NOT NULL DEFAULT ''"),
                                  ("force_revalidate", "INTEGER NOT NULL DEFAULT 0")):
            if name not in employer_columns:
                db.execute(f"ALTER TABLE employers ADD COLUMN {name} {declaration}")
        # One-time backfill for already-attempted records. The source URL,
        # last evidence, and original error text remain untouched; only add a
        # scheduler fingerprint and refine the known generic parser result.
        for old in db.execute("SELECT * FROM employers WHERE source_fingerprint='' AND last_attempt_at IS NOT NULL").fetchall():
            fingerprint=hashlib.sha256("\0".join(str(old[key] or "").strip() for key in
                ("source_type","source_token","careers_url","public_feed_url")).encode()).hexdigest()
            old_status=old["connector_status"]
            if old_status=="unsupported" and "unsupported career page layout" in str(old["error_details"] or "").casefold():
                status="unsupported_structure";health_reason=status;delay=timedelta(days=30)
            elif old_status in {"healthy","ready"} and old["last_success_at"]:
                status="healthy"
                try:
                    prior=json.loads(old["verification_evidence_json"] or "{}")
                except (TypeError,json.JSONDecodeError): prior={}
                health_reason="valid_zero_postings" if not prior.get("postings_observed") else "valid_with_postings"
                delay=timedelta(hours=24)
            else:
                status=old_status;health_reason=old_status or "needs_verification"
                delay=timedelta(minutes=15)
            try: base=datetime.fromisoformat(old["last_attempt_at"])
            except (TypeError,ValueError): base=datetime.now(timezone.utc)
            if base.tzinfo is None: base=base.replace(tzinfo=timezone.utc)
            retry=(base+delay).isoformat()
            db.execute("UPDATE employers SET connector_status=?,health_reason=?,consecutive_failures=CASE WHEN ?='healthy' THEN 0 ELSE 1 END,next_retry_at=?,source_fingerprint=? WHERE employer_id=?",
                (status,health_reason,status,retry,fingerprint,old["employer_id"]))
        db.execute("""CREATE TABLE IF NOT EXISTS job_source_provenance (
            source_platform TEXT NOT NULL, source_job_key TEXT NOT NULL, canonical_url TEXT NOT NULL DEFAULT '',
            content_fingerprint TEXT NOT NULL DEFAULT '', first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
            PRIMARY KEY(source_platform,source_job_key))""")
        db.execute("""CREATE TABLE IF NOT EXISTS employer_discovery_stages (
            employer_id TEXT NOT NULL, stage_key TEXT NOT NULL, status TEXT NOT NULL,
            diagnostics_json TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL,
            PRIMARY KEY(employer_id,stage_key))""")
        db.execute("""CREATE TABLE IF NOT EXISTS employer_candidate_seeds (
            candidate_id TEXT PRIMARY KEY, name TEXT NOT NULL, website_url TEXT NOT NULL,
            candidate_source TEXT NOT NULL, candidate_record_url TEXT NOT NULL DEFAULT '',
            candidate_license TEXT NOT NULL DEFAULT '', discovered_at TEXT NOT NULL,
            last_attempt_at TEXT, status TEXT NOT NULL DEFAULT 'candidate',
            diagnostics_json TEXT NOT NULL DEFAULT '{}', UNIQUE(candidate_source,candidate_record_url))""")
        seed_columns={row[1] for row in db.execute("PRAGMA table_info(employer_candidate_seeds)").fetchall()}
        for column,declaration in (("careers_url","TEXT NOT NULL DEFAULT ''"),("evidence_url","TEXT NOT NULL DEFAULT ''"),
                                  ("normalized_name","TEXT NOT NULL DEFAULT ''"),("normalized_domain","TEXT NOT NULL DEFAULT ''")):
            if column not in seed_columns: db.execute(f"ALTER TABLE employer_candidate_seeds ADD COLUMN {column} {declaration}")
        for seed in db.execute("SELECT candidate_id,name,website_url FROM employer_candidate_seeds WHERE normalized_name='' OR normalized_domain=''").fetchall():
            normalized_name=re.sub(r"[^a-z0-9]+"," ",seed["name"].casefold()).strip()
            normalized_domain=(urlparse(seed["website_url"]).hostname or "").casefold().removeprefix("www.")
            db.execute("UPDATE employer_candidate_seeds SET normalized_name=?,normalized_domain=? WHERE candidate_id=?",
                (normalized_name,normalized_domain,seed["candidate_id"]))
        db.execute("""CREATE TABLE IF NOT EXISTS employer_candidate_provenance (
            candidate_id TEXT NOT NULL,provider TEXT NOT NULL,record_url TEXT NOT NULL DEFAULT '',
            evidence_url TEXT NOT NULL DEFAULT '',access_terms TEXT NOT NULL DEFAULT '',discovered_at TEXT NOT NULL,
            PRIMARY KEY(candidate_id,provider,record_url))""")
        db.execute("""CREATE TABLE IF NOT EXISTS employer_import_checkpoints (
            source_key TEXT PRIMARY KEY, cursor_value TEXT NOT NULL DEFAULT '', pages_completed INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending', last_error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS employer_provider_runs (
            run_id TEXT PRIMARY KEY,providers_json TEXT NOT NULL,status TEXT NOT NULL,
            diagnostics_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""")
        db.execute("UPDATE employer_provider_runs SET status='interrupted',updated_at=? WHERE status='running'",
            (datetime.now(timezone.utc).isoformat(),))
        # Older versions treated an empty first Wikidata page as a completed
        # import. Keep its cursor, but make the checkpoint retryable and explain
        # why it is being retried on the next expansion run.
        db.execute("""UPDATE employer_import_checkpoints SET status='empty_result',
            last_error='Previous run completed with no usable first-page records; retry from the beginning.'
            WHERE status='complete' AND cursor_value='' AND pages_completed>0""")
        db.execute("""CREATE TABLE IF NOT EXISTS job_discovery_runs (
            run_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,prompt TEXT NOT NULL,status TEXT NOT NULL,
            total_sources INTEGER NOT NULL DEFAULT 0,sources_checked INTEGER NOT NULL DEFAULT 0,
            postings_retrieved INTEGER NOT NULL DEFAULT 0,diagnostics_json TEXT NOT NULL DEFAULT '{}',
            cancel_requested INTEGER NOT NULL DEFAULT 0,error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""")
        # Ownership is additive. Importing the app must never rewrite another
        # process's running row; its durable lease determines whether it is live.
        run_columns={row[1] for row in db.execute("PRAGMA table_info(job_discovery_runs)")}
        for name,declaration in (("workflow_id","TEXT NOT NULL DEFAULT ''"),
                                 ("owner_token","TEXT NOT NULL DEFAULT ''"),
                                 ("owner_pid","INTEGER"),("owner_host","TEXT NOT NULL DEFAULT ''"),
                                 ("heartbeat_at","TEXT"),("lease_expires_at","TEXT")):
            if name not in run_columns:
                db.execute(f"ALTER TABLE job_discovery_runs ADD COLUMN {name} {declaration}")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_one_running_discovery_per_user ON job_discovery_runs(user_id) WHERE status='running'")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_one_discovery_run_per_workflow ON job_discovery_runs(workflow_id) WHERE workflow_id<>''")
        # Migrate existing configured employers without touching saved jobs.
        stamp = datetime.now(timezone.utc).isoformat()
        for company, board in BOARDS.items():
            db.execute("""INSERT OR IGNORE INTO employers
                (employer_id,name,careers_url,source_type,source_token,public_feed_url,geographic_coverage,connector_status,active,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,1,?,?)""",
                (f"greenhouse:{board}",company,"","greenhouse",board,
                 f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true","", "ready",stamp,stamp))
        # Backfill stage diagnostics for pre-migration employers without changing
        # their verification, activation, jobs, or application history.
        for row in db.execute("SELECT employer_id,website_url,evidence_url,last_success_at,active FROM employers").fetchall():
            for key, status in (("candidate_registration","registered"),
                                ("official_source_discovery","evidence_provided" if row["evidence_url"] or row["website_url"] else "legacy_configured"),
                                ("source_validation","verified" if row["last_success_at"] else "pending"),
                                ("source_activation","active" if row["active"] else "inactive")):
                db.execute("INSERT OR IGNORE INTO employer_discovery_stages(employer_id,stage_key,status,diagnostics_json,updated_at) VALUES(?,?,?,?,?)",
                           (row["employer_id"],key,status,"{}",stamp))


initialize()


def preferences():
    with database() as db:
        row = db.execute(
            "SELECT preferences_json FROM career_preferences WHERE user_id=?", (USER,)
        ).fetchone()
    if not row:
        return None
    try:
        data = json.loads(row["preferences_json"])
    except (TypeError, json.JSONDecodeError):
        raise HTTPException(500, "Saved career preferences are invalid.")
    if not isinstance(data, dict):
        raise HTTPException(500, "Saved career preferences are invalid.")
    return data


def _strings(value):
    return [item.strip() for item in value if isinstance(item, str) and item.strip()] if isinstance(value, list) else []


def search_terms(prompt, prefs):
    """Return preference terms, using the prompt only when saved roles are absent."""
    roles = _strings(prefs.get("roles", []))
    categories = _strings(prefs.get("categories", []))
    terms = [role.casefold() for role in roles]
    for category in categories:
        terms.extend(CATEGORY_TERMS.get(category.casefold(), ()))
    if roles or categories:
        return list(dict.fromkeys(terms))

    p = prompt.casefold()
    prompt_groups = {
        "software": SOFTWARE_ROLE_ALIASES,
        "backend": ("backend engineer", "back-end engineer", "backend developer", "back-end developer"),
        "front end": ("frontend engineer", "front-end engineer", "frontend developer", "front-end developer"),
        "frontend": ("frontend engineer", "front-end engineer", "frontend developer", "front-end developer"),
        "full stack": ("full stack engineer", "full-stack engineer", "full stack developer", "full-stack developer"),
        "data": CATEGORY_TERMS["data and analytics"],
        "security": CATEGORY_TERMS["cybersecurity"],
        "machine learning": CATEGORY_TERMS["artificial intelligence"],
        "cloud": CATEGORY_TERMS["cloud and infrastructure"],
    }
    for trigger, group in prompt_groups.items():
        if trigger in p:
            terms.extend(group)
    return list(dict.fromkeys(terms))


def title_relevance(title, prefs, terms):
    """Return strong/related/unclear/unrelated; related titles still need human review."""
    relevant, reason, evidence = relevance_for_job({"title": title}, prefs)
    if relevant:
        return "strong" if evidence["basis"] == "title_alias" else "related"
    return "unrelated"


def _job_location(job):
    value = job.get("location")
    return str(value.get("name", "")) if isinstance(value, dict) else str(value or "")


def _employment(job):
    return str(normalize_job_record(job).get("employment_type") or "").casefold()


def _employment_type_matches(preference, observed):
    """Compare ATS employment labels without treating punctuation/casing as meaning."""
    expected = re.sub(r"[^a-z0-9]+", "", str(preference or "").casefold())
    actual = re.sub(r"[^a-z0-9]+", "", str(observed or "").casefold())
    return bool(expected and actual and (expected in actual or actual in expected))


def _confirmed_professional_experience_years(job=None):
    """Compute experience only from confirmed dated profile records."""
    try:
        from resume_job_matcher import active_candidate
        from candidate_verification import canonical_verified_candidate_facts
        _,status,profile=active_candidate()
        if status not in {"user_reviewed","field_verified"}:return None
        facts=canonical_verified_candidate_facts(profile)
    except (ImportError,sqlite3.Error,TypeError,ValueError):
        return None
    scope=str((job or {}).get("title") or "")+" "+str((job or {}).get("description") or (job or {}).get("content") or "")
    if re.search(r"\b(teaching|tutoring|instructor|instructional|educator|classroom)\b",scope,re.I):
        months=int(facts.get("dated_teaching_experience_months") or 0)
    elif re.search(r"\b(software|developer|programmer|engineering|coding|development|technical)\b",scope,re.I):
        months=int(facts.get("dated_software_experience_months") or 0)
    else:
        months=int(facts.get("dated_professional_experience_months") or 0)
    return months/12 if months else None


def _confirmed_candidate_skills():
    """Load only user-confirmed ordinary skills for local ranking."""
    with database() as db:
        resume_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='resumes'").fetchone()
        verification_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='candidate_verifications'").fetchone()
        resume = db.execute("SELECT id FROM resumes WHERE user_id=? AND active=1 ORDER BY created_at DESC LIMIT 1", (USER,)).fetchone() if resume_table else None
        verification = db.execute("SELECT values_json,confirmed_json FROM candidate_verifications WHERE user_id=? AND resume_id=?", (USER,resume["id"])).fetchone() if resume and verification_table else None
        answer_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='application_answers'").fetchone()
        answer_columns={column[1] for column in db.execute("PRAGMA table_info(application_answers)")} if answer_table else set()
        question_select="question_text" if "question_text" in answer_columns else "'' AS question_text"
        answers = db.execute(f"""SELECT {question_select},answer FROM application_answers WHERE user_id=? AND resume_id=?
            AND question_key='technical_skills' AND confirmed=1 AND sensitive=0 AND revoked_at IS NULL""", (USER,resume["id"])).fetchall() if resume and answer_table else []
    skills = []
    if verification:
        try:
            from candidate_verification import verified_values_from_json
            values = verified_values_from_json(verification["values_json"])
            flags = json.loads(verification["confirmed_json"] or "{}")
            for key in ("technical_skills", "skills"):
                if flags.get(key):
                    value = values.get(key, [])
                    skills.extend(value if isinstance(value, list) else re.split(r"[,;\n]", str(value)))
        except (TypeError, ValueError, RuntimeError, json.JSONDecodeError):
            pass
    for row in answers:
        try:
            from candidate_verification import validate_application_answer
            value=validate_application_answer(row["question_text"] or "Technical skills",row["answer"])
        except (ImportError,ValueError):continue
        skills.extend(re.split(r"[,;\n]",str(value or "")))
    return list(dict.fromkeys(value.strip() for value in skills if isinstance(value, str) and value.strip()))


def assess_job(job, prefs, terms, confirmed_experience_years=None):
    """Apply hard conflicts from saved settings; leave unknown facts for review."""
    title = str(job.get("title", ""))
    normalized_title = title.casefold()
    location = _job_location(job).casefold()
    content = re.sub(r"<[^>]+>", " ", str(job.get("content") or job.get("description", ""))).casefold()
    evidence = f"{normalized_title} {location} {content}"
    review_reasons = []
    if prefs.get("_prompt_exclude_internships") and INTERN.search(f"{title} {content}"):
        return False, "employment_type", []
    if prefs.get("_prompt_exclude_senior") and SENIOR.search(title):
        return False, "experience_level", []
    if confirmed_experience_years is None:
        confirmed_experience_years = prefs.get("_confirmed_experience_years")
    if confirmed_experience_years is None:
        confirmed_experience_years = _confirmed_professional_experience_years(job)

    normalized = normalize_job_record(job)
    is_relevant, relevance_reason, relevance_evidence = relevance_for_job(normalized, prefs)
    relevance = "strong" if is_relevant and relevance_evidence["basis"] == "title_alias" else ("related" if is_relevant else "unrelated")
    if relevance == "unrelated":
        return False, "role", []
    if relevance in ("related", "unclear"):
        review_reasons.append("Role relevance is based on a related title and needs confirmation.")

    levels = [level.casefold() for level in _strings(prefs.get("levels", []))]
    early_selected = any(level in ("new graduate", "entry level", "junior", "associate", "internship") for level in levels)
    senior_selected = any(level in ("senior", "staff / principal", "manager", "director", "vp / executive") for level in levels)
    if SENIOR.search(normalized_title) and early_selected and not senior_selected:
        return False, "experience_level", []
    if EARLY.search(normalized_title) and senior_selected and not early_selected:
        return False, "experience_level", []
    if not SENIOR.search(normalized_title) and not EARLY.search(normalized_title):
        review_reasons.append("Experience level is not clear from the posting title.")
    if early_selected and not senior_selected:
        experience_requirements = extract_experience_requirements(content)
        for item in experience_requirements["required"]:
            minimum_years = float(re.match(r"\d+(?:\.\d+)?", item["years"]).group())
            if confirmed_experience_years is not None and confirmed_experience_years < minimum_years:
                return False, "experience_level", []
            if confirmed_experience_years is None:
                review_reasons.append(
                    f"Posting requires at least {minimum_years:g} years of experience; confirmed professional experience is unavailable, so eligibility needs review."
                )

    types = [value.casefold().replace("-", " ") for value in _strings(prefs.get("employment_types", []))]
    employment = _employment(job).replace("-", " ").replace("_", " ")
    is_internship = bool(INTERN.search(title) or "intern" in employment)
    if is_internship and types and "internship" not in types:
        return False, "employment_type", []
    if types:
        if employment:
            if not any(kind in employment for kind in types) and not (is_internship and "internship" in types):
                return False, "employment_type", []
        elif is_internship and "internship" not in types:
            return False, "employment_type", []
        else:
            review_reasons.append("Employment type is not provided in Greenhouse metadata.")

    relocation=evaluate_relocation_preference(job,prefs)
    geo_status, geo_reason = relocation or evaluate_location(job, prefs.get("location", ""))
    if geo_status == "mismatch":
        return False, "location", []
    if geo_status == "unknown":
        review_reasons.append(geo_reason)

    arrangement = str(prefs.get("work_arrangement", "Any")).strip()
    selected_arrangement = _arrangement(arrangement)
    observed_arrangement = normalized.get("work_arrangement") or _arrangement(evidence)
    if selected_arrangement:
        if observed_arrangement and observed_arrangement != selected_arrangement:
            return False, "work_arrangement", []
        if not observed_arrangement:
            review_reasons.append(f"Work arrangement ({selected_arrangement}) is not clear from the feed.")

    instructions = str(prefs.get("instructions", "")).casefold()
    if ("remote only" in instructions or "must be remote" in instructions) and observed_arrangement != "Remote":
        if observed_arrangement in {"Hybrid", "On-site"}:
            return False, "instructions", []
        review_reasons.append("Remote-only instruction could not be verified.")
    if "exclude remote" in instructions and observed_arrangement == "Remote":
        return False, "instructions", []
    exclude = re.search(r"\b(?:exclude|avoid|no)\s+([a-z][a-z -]{2,35})", instructions)
    if exclude:
        forbidden = re.sub(r"\b(roles?|jobs?|positions?|openings?)\b", " ", exclude.group(1))
        forbidden_terms = [part.strip() for part in re.split(r"\b(?:or|and)\b|,", forbidden) if part.strip()]
        if any(re.search(r"\b" + re.escape(term) + r"\b", evidence) for term in forbidden_terms):
            return False, "instructions", []

    return True, None, list(dict.fromkeys(review_reasons))


def valid_application_url(value):
    if not isinstance(value, str):
        return None
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    host = parsed.hostname.lower().rstrip(".")
    if host in {"localhost", "metadata.google.internal"} or host.endswith((".localhost", ".local", ".internal")):
        return None
    return value


def source_application_url(value, source):
    value=valid_application_url(value)
    if not value: return None
    host=urlparse(value).hostname.casefold()
    approved={"greenhouse":{"boards.greenhouse.io","job-boards.greenhouse.io"},
              "lever":{"jobs.lever.co","jobs.eu.lever.co"},
              "ashby":{"jobs.ashbyhq.com"},
              "smartrecruiters":{"smartrecruiters.com","www.smartrecruiters.com","careers.smartrecruiters.com"}}
    if host in set().union(*approved.values()): return value
    career_host=urlparse(source.careers_url).hostname if source.careers_url else None
    if career_host and host==career_host.casefold(): return value
    return None


def _employer_sources():
    """Return only currently live-verified and healthy employer feeds.

    ``next_retry_at`` schedules source revalidation after failures; a future
    scheduled review does not invalidate an otherwise healthy verified feed.
    """
    with database() as db:
        rows = db.execute("""SELECT * FROM employers
            WHERE active=1 AND connector_status='healthy' AND discovery_status='verified'
              AND last_success_at IS NOT NULL
            ORDER BY name COLLATE NOCASE""").fetchall()
    return [EmployerSource(name=row["name"], source_type=row["source_type"], token=row["source_token"],
                           careers_url=row["careers_url"], geographic_coverage=row["geographic_coverage"])
            for row in rows if row["source_fingerprint"] == _source_fingerprint(dict(row))]


def _require_local_admin(request: Request):
    host = getattr(getattr(request, "client", None), "host", None)
    try:
        if not host or not ipaddress.ip_address(host).is_loopback:
            raise HTTPException(403, "Employer administration is available only from this computer.")
    except ValueError:
        raise HTTPException(403, "Employer administration is available only from this computer.")


def _source_health(error):
    if not error:
        return "healthy"
    message = str(error).casefold()
    if "robots.txt" in message or "robots disallow" in message:
        return "robots_disallowed"
    if "javascript-rendered" in message or "javascript rendered" in message:
        return "javascript_rendered"
    if "redirected to recognized ats" in message:
        return "redirect_to_ats"
    if "429" in message or "rate limit" in message or "rate limited" in message:
        return "rate_limited"
    if "401" in message or ("authentication required" in message and "403" not in message):
        return "authentication_required"
    if "403" in message or "access restricted" in message or "permission required" in message:
        return "access_restricted"
    if "unsupported career page layout" in message or "unsupported html" in message:
        return "unsupported_structure"
    if "no documented universal" in message or "has no verified public feed" in message:
        return "unsupported_provider"
    if "timed out" in message or "timeout" in message:
        return "network_timeout"
    if "404" in message or "410" in message or "permanently unavailable" in message:
        return "permanently_unavailable"
    if "invalid provider identifier" in message or "invalid configuration" in message:
        return "invalid_provider_identifier"
    if "malformed" in message or "not valid json" in message or "rejected" in message:
        return "malformed_response"
    if "invalid" in message:
        return "invalid_configuration"
    return "temporarily_unavailable"


def _source_fingerprint(row):
    values = (row["source_type"], row["source_token"], row["careers_url"], row["public_feed_url"])
    return hashlib.sha256("\0".join(str(value or "").strip() for value in values).encode()).hexdigest()


def _next_source_retry(status, error, consecutive_failures, now=None):
    now = now or datetime.now(timezone.utc)
    if status == "unsupported_structure":
        delay = timedelta(days=30)
    elif status in {"unsupported_provider", "invalid_provider_identifier", "invalid_configuration", "malformed_response"}:
        delay = timedelta(days=14)
    elif status in {"authentication_required", "access_restricted", "robots_disallowed"}:
        delay = timedelta(days=7)
    elif status == "rate_limited":
        seconds = None
        match = re.search(r"retry-after\s+([0-9.]+)\s*s?", str(error or ""), re.I)
        if match:
            seconds = float(match.group(1))
        if seconds is None:
            delay = timedelta(hours=1)
        else:
            delay = timedelta(seconds=min(max(seconds, 60), 7 * 24 * 3600))
    else:
        delay = timedelta(minutes=min(15 * (2 ** max(0, consecutive_failures - 1)), 24 * 60))
    return (now + delay).isoformat()


def employer_retry_eligible(row, *, now=None, force=False):
    if force or bool(row.get("force_revalidate")):
        return True
    fingerprint = _source_fingerprint(row)
    if not row.get("source_fingerprint") or row.get("source_fingerprint") != fingerprint:
        return True
    retry_at = row.get("next_retry_at")
    if not retry_at:
        # Legacy unhealthy rows are eligible once; validation will establish
        # their first fingerprint and retry deadline.
        return True
    return retry_at <= (now or datetime.now(timezone.utc)).isoformat()


def usable_posting_description(value):
    """Match the enrichment pipeline's minimum for a usable job description."""
    text=re.sub(r"<[^>]*>"," ",str(value or ""))
    return len(text)>=180 and len(re.findall(r"[A-Za-z]",text))>=120


def _legacy_job(record):
    """Map normalized source fields to the small common shape used by rule filters."""
    return {**record, "id": record["source_id"], "absolute_url": record["application_url"],
            "location": {"name": record["location"]}, "content": record["description"],
            "employment_type": record["employment_type"], "country": record["country"],
            "workplaceType": record["work_arrangement"]}


def fetch_board(company, board):
    """Fetch and cache complete Greenhouse job records, including descriptions."""
    now = time.monotonic()
    with _FEED_CACHE_LOCK:
        cached = _FEED_CACHE.get(board)
        if cached and cached[0] > now:
            return cached[1], cached[2]
        result, error = _fetch_board(company, board)
        ttl = 30 if error else FEED_CACHE_TTL
        _FEED_CACHE[board] = (now + ttl, result, error)
        return result, error


def _fetch_board(company, board):
    return fetch_source(EmployerSource(company, "greenhouse", board))


def _persist_discovered_items(items, timestamp):
    """Persist accepted source jobs before advancing a discovery checkpoint."""
    if not items:
        return
    with database() as db:
        for item in items:
            prior=db.execute("SELECT content_fingerprint FROM discovered_openings WHERE user_id=? AND job_key=?",
                             (USER,item["job_key"])).fetchone()
            if prior and prior["content_fingerprint"] and prior["content_fingerprint"]!=item["content_fingerprint"]:
                table=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_match_results'").fetchone()
                if table:
                    cols={row[1] for row in db.execute("PRAGMA table_info(job_match_results)")}
                    if "freshness_status" in cols:
                        db.execute("UPDATE job_match_results SET freshness_status='stale' WHERE user_id=? AND job_key=?",
                                   (USER,item["job_key"]))
            db.execute("""INSERT INTO discovered_openings
                (user_id,job_key,company,title,location,url,discovered_at,employment_type,location_status,location_reason,search_prompt,discovery_source,
                 source_platform,source_id,country,work_arrangement,description,posting_date,last_verified_at,active,content_fingerprint,job_url,
                 salary_json,career_category,experience_level,required_qualifications_json,preferred_qualifications_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,job_key) DO UPDATE SET
                company=excluded.company,title=excluded.title,location=excluded.location,url=excluded.url,
                employment_type=excluded.employment_type,location_status=excluded.location_status,location_reason=excluded.location_reason,
                search_prompt=excluded.search_prompt,discovery_source=excluded.discovery_source,
                source_platform=excluded.source_platform,source_id=excluded.source_id,country=excluded.country,
                work_arrangement=excluded.work_arrangement,description=excluded.description,posting_date=excluded.posting_date,
                last_verified_at=excluded.last_verified_at,active=excluded.active,content_fingerprint=excluded.content_fingerprint,job_url=excluded.job_url,
                salary_json=excluded.salary_json,career_category=excluded.career_category,experience_level=excluded.experience_level,
                required_qualifications_json=excluded.required_qualifications_json,preferred_qualifications_json=excluded.preferred_qualifications_json""",
                (USER,item["job_key"],item["company"],item["title"],item["location"],item["url"],timestamp,
                 item["employment_type"],item["location_status"],item["location_reason"],item["search_prompt"],item["discovery_source"],
                 item["source_platform"],item["source_id"],item["country"],item["work_arrangement"],item["description"],item["posting_date"],
                 item["last_verified_at"],item["active"],item["content_fingerprint"],item["job_url"],item["salary_json"],item["career_category"],
                 item["experience_level"],item["required_qualifications_json"],item["preferred_qualifications_json"]))
            canonical=canonicalize_job_url(item["url"])
            db.execute("""INSERT INTO job_source_provenance(source_platform,source_job_key,canonical_url,content_fingerprint,first_seen_at,last_seen_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(source_platform,source_job_key) DO UPDATE SET canonical_url=excluded.canonical_url,
                content_fingerprint=excluded.content_fingerprint,last_seen_at=excluded.last_seen_at""",
                (item["source_platform"],item["job_key"],canonical,item["content_fingerprint"],timestamp,timestamp))


def active_discovery():
    """Return active-run health without exposing prompts or ownership tokens."""
    stamp=datetime.now(timezone.utc).isoformat()
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        for stale in db.execute("SELECT run_id,workflow_id,owner_pid,owner_host,lease_expires_at FROM job_discovery_runs WHERE user_id=? AND status='running' AND lease_expires_at IS NOT NULL AND lease_expires_at<=?",
                                (USER,stamp)).fetchall():
            if stale["owner_host"]==socket.gethostname() and not _pid_is_alive(stale["owner_pid"]):
                changed=db.execute("""UPDATE job_discovery_runs SET status='interrupted',error='worker_lease_expired',
                    owner_token='',owner_pid=NULL,owner_host='',heartbeat_at=NULL,lease_expires_at=NULL,updated_at=?
                    WHERE run_id=? AND status='running' AND lease_expires_at=?""",
                    (stamp,stale["run_id"],stale["lease_expires_at"])).rowcount
                if changed:_pause_recovered_workflow(db,stale["workflow_id"],stamp)
        row=db.execute("""SELECT run_id,workflow_id,status,cancel_requested,updated_at,
            owner_pid,owner_host,heartbeat_at,lease_expires_at
            FROM job_discovery_runs WHERE user_id=? AND status='running'
            ORDER BY updated_at DESC LIMIT 1""",(USER,)).fetchone()
    if not row:return None
    item=dict(row)
    item["ownership_state"]=("legacy_unleased" if not item["lease_expires_at"] else
        "lease_valid" if item["lease_expires_at"]>datetime.now(timezone.utc).isoformat() else "lease_expired")
    return item


def _pid_is_alive(pid):
    if not pid:return False
    try:os.kill(int(pid),0)
    except ProcessLookupError:return False
    except PermissionError:return True
    except (OSError,ValueError,TypeError):return False
    return True


def _lease_expiry(stamp=None):
    base=stamp or datetime.now(timezone.utc)
    return (base+timedelta(seconds=DISCOVERY_LEASE_SECONDS)).isoformat()


def _pause_recovered_workflow(db,workflow_id,stamp):
    if not workflow_id:return
    table=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_workflows'").fetchone()
    if not table:return
    row=db.execute("SELECT status,diagnostics_json FROM user_workflows WHERE workflow_id=? AND user_id=?",
                   (workflow_id,USER)).fetchone()
    if not row or row["status"] not in {"queued","running"}:return
    try:diagnostics=json.loads(row["diagnostics_json"] or "{}")
    except (TypeError,json.JSONDecodeError):diagnostics={}
    diagnostics["recovery"]="Discovery worker lease expired after process exit; resume to continue from the saved checkpoint."
    db.execute("""UPDATE user_workflows SET status='paused',current_stage='discovering',pause_requested=1,
        diagnostics_json=?,updated_at=? WHERE workflow_id=? AND user_id=?""",
        (json.dumps(diagnostics),stamp,workflow_id,USER))


def _acquire_discovery_run(prompt,run_id,workflow_id=None,total_sources=0):
    """Claim a run ID and per-user slot atomically across processes."""
    stamp=datetime.now(timezone.utc).isoformat();token=uuid.uuid4().hex
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        run=db.execute("SELECT * FROM job_discovery_runs WHERE run_id=?",(run_id,)).fetchone()
        if run and run["user_id"]!=USER:
            raise HTTPException(409,"duplicate_run_id: this discovery run ID belongs to another user.")
        if run and run["workflow_id"] and workflow_id and run["workflow_id"]!=workflow_id:
            raise HTTPException(409,"inconsistent_association: this run ID is associated with another workflow.")
        if run and run["status"]=="running":
            lease=run["lease_expires_at"]
            if lease and lease<=stamp and run["owner_host"]==socket.gethostname() and not _pid_is_alive(run["owner_pid"]):
                changed=db.execute("""UPDATE job_discovery_runs SET status='interrupted',error='worker_lease_expired',
                    owner_token='',owner_pid=NULL,owner_host='',heartbeat_at=NULL,lease_expires_at=NULL,updated_at=?
                    WHERE run_id=? AND status='running' AND lease_expires_at=?""",
                    (stamp,run_id,lease)).rowcount
                if changed:
                    _pause_recovered_workflow(db,run["workflow_id"],stamp)
                    run=db.execute("SELECT * FROM job_discovery_runs WHERE run_id=?",(run_id,)).fetchone()
            else:
                kind="legacy_unleased" if not lease else ("stale_lease_owner_alive" if lease<=stamp else "active_worker")
                raise HTTPException(409,f"{kind}: the requested discovery run still has an owner; inspect or recover it.")
        if run and run["status"] not in {"queued","interrupted"}:
            raise HTTPException(409,"duplicate_run_id: this discovery run is already finished or cancelled.")
        other=db.execute("SELECT * FROM job_discovery_runs WHERE user_id=? AND status='running' AND run_id<>? LIMIT 1",
                         (USER,run_id)).fetchone()
        if other:
            lease=other["lease_expires_at"]
            if lease and lease<=stamp and other["owner_host"]==socket.gethostname() and not _pid_is_alive(other["owner_pid"]):
                changed=db.execute("""UPDATE job_discovery_runs SET status='interrupted',error='worker_lease_expired',
                    owner_token='',owner_pid=NULL,owner_host='',heartbeat_at=NULL,lease_expires_at=NULL,updated_at=?
                    WHERE run_id=? AND status='running' AND lease_expires_at=?""",
                    (stamp,other["run_id"],lease)).rowcount
                if changed:_pause_recovered_workflow(db,other["workflow_id"],stamp)
            else:
                kind="legacy_unleased" if not lease else ("stale_lease_owner_alive" if lease<=stamp else "active_worker")
                raise HTTPException(409,f"{kind}: discovery run {other['run_id'][:8]}… owns this user's active slot; inspect or recover that run.")
        prior={}
        if run:
            try:prior=json.loads(run["diagnostics_json"] or "{}")
            except (TypeError,json.JSONDecodeError):prior={}
            db.execute("""UPDATE job_discovery_runs SET status='running',cancel_requested=0,error=NULL,
                total_sources=?,workflow_id=CASE WHEN workflow_id='' THEN ? ELSE workflow_id END,owner_token=?,owner_pid=?,
                owner_host=?,heartbeat_at=?,lease_expires_at=?,updated_at=? WHERE run_id=? AND user_id=?""",
                (total_sources,workflow_id or "",token,os.getpid(),socket.gethostname(),stamp,_lease_expiry(),stamp,run_id,USER))
        else:
            db.execute("""INSERT INTO job_discovery_runs(run_id,user_id,prompt,status,total_sources,workflow_id,
                owner_token,owner_pid,owner_host,heartbeat_at,lease_expires_at,created_at,updated_at)
                VALUES(?,?,?,'running',?,?,?,?,?,?,?,?,?)""",
                (run_id,USER,str(prompt or "")[:1500],total_sources,workflow_id or "",token,os.getpid(),socket.gethostname(),stamp,
                 _lease_expiry(),stamp,stamp))
    return token,prior


def _heartbeat_discovery(run_id,token,stop):
    while not stop.wait(DISCOVERY_HEARTBEAT_SECONDS):
        stamp=datetime.now(timezone.utc).isoformat()
        try:
            with database() as db:
                changed=db.execute("""UPDATE job_discovery_runs SET heartbeat_at=?,lease_expires_at=?,updated_at=?
                    WHERE run_id=? AND user_id=? AND status='running' AND owner_token=?""",
                    (stamp,_lease_expiry(),stamp,run_id,USER,token)).rowcount
            if not changed:return
        except sqlite3.Error:
            logger.exception("Discovery lease renewal failed for run %s",run_id[:8])


def _finish_discovery_ownership(run_id,token,*,interrupted=False,error=None):
    stamp=datetime.now(timezone.utc).isoformat()
    with database() as db:
        db.execute("""UPDATE job_discovery_runs SET status=CASE WHEN ? THEN 'interrupted' ELSE status END,
            error=CASE WHEN ? THEN ? ELSE error END,owner_token='',owner_pid=NULL,owner_host='',
            heartbeat_at=NULL,lease_expires_at=NULL,updated_at=? WHERE run_id=? AND user_id=? AND owner_token=?""",
            (int(interrupted),int(interrupted),error,stamp,run_id,USER,token))


def recover_abandoned_discovery(run_id,confirmed_abandoned=False):
    """Explicitly recover an unowned/expired run while preserving its history."""
    if not confirmed_abandoned:
        raise HTTPException(422,"Confirm that the discovery worker is abandoned before recovery.")
    now_dt=datetime.now(timezone.utc);stamp=now_dt.isoformat()
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("SELECT * FROM job_discovery_runs WHERE run_id=? AND user_id=?",(run_id,USER)).fetchone()
        if not row:raise HTTPException(404,"Discovery run not found.")
        if row["status"]!="running":
            return {"run_id":run_id,"status":row["status"],"recovered":False,
                    "sources_checked":row["sources_checked"],"postings_retrieved":row["postings_retrieved"]}
        try:last_update=datetime.fromisoformat(row["updated_at"])
        except (TypeError,ValueError):last_update=now_dt
        if last_update.tzinfo is None:last_update=last_update.replace(tzinfo=timezone.utc)
        age=(now_dt-last_update).total_seconds()
        lease=row["lease_expires_at"]
        if lease and lease>stamp:
            raise HTTPException(409,"active_worker: the discovery worker's lease is still valid.")
        if not lease and row["owner_pid"] and row["owner_host"]==socket.gethostname() and _pid_is_alive(row["owner_pid"]):
            raise HTTPException(409,"inconsistent_ownership: a worker process is recorded without a lease; inspect the server before recovery.")
        if age<120:
            raise HTTPException(409,"stale_lease: wait until the run has been unchanged for at least two minutes before confirming recovery.")
        workflow_id=row["workflow_id"]
        if workflow_id:
            tables=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_workflows'").fetchone()
            if tables:
                wf=db.execute("SELECT status,current_stage FROM user_workflows WHERE workflow_id=? AND user_id=?",
                              (workflow_id,USER)).fetchone()
                if wf and wf["status"] in {"queued","running"} and wf["current_stage"]!="discovering":
                    raise HTTPException(409,"inconsistent_workflow: the associated workflow is active outside discovery; inspect it before recovery.")
        changed=db.execute("""UPDATE job_discovery_runs SET status='interrupted',error='abandoned_run_recovered',
            owner_token='',owner_pid=NULL,owner_host='',heartbeat_at=NULL,lease_expires_at=NULL,updated_at=?
            WHERE run_id=? AND user_id=? AND status='running'""",(stamp,run_id,USER)).rowcount
        if changed and workflow_id:
            tables=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_workflows'").fetchone()
            if tables:
                wf=db.execute("SELECT diagnostics_json,status FROM user_workflows WHERE workflow_id=? AND user_id=?",
                              (workflow_id,USER)).fetchone()
                if wf and wf["status"] in {"queued","running","paused"}:
                    try:diagnostics=json.loads(wf["diagnostics_json"] or "{}")
                    except (TypeError,json.JSONDecodeError):diagnostics={}
                    diagnostics["recovery"]="Abandoned discovery lease recovered; resume this workflow to continue from its checkpoint."
                    db.execute("""UPDATE user_workflows SET status='paused',current_stage='discovering',pause_requested=1,
                        diagnostics_json=?,updated_at=? WHERE workflow_id=? AND user_id=?""",
                        (json.dumps(diagnostics),stamp,workflow_id,USER))
    return {"run_id":run_id,"status":"interrupted","recovered":bool(changed),
            "sources_checked":row["sources_checked"],"postings_retrieved":row["postings_retrieved"]}


def select_discovery_sources(source_keys=None):
    """Return active verified sources, optionally constrained to a bounded set."""
    available=_employer_sources()
    if not source_keys:return available
    requested=list(dict.fromkeys(str(value or "").strip() for value in source_keys if str(value or "").strip()))
    if len(requested)>15:raise HTTPException(422,"A targeted discovery run can select at most 15 verified sources.")
    by_key={f"{item.source_type}:{item.token}":item for item in available}
    unknown=[key for key in requested if key not in by_key]
    if unknown:raise HTTPException(422,"Targeted discovery accepts only currently active, verified employer source keys.")
    return [by_key[key] for key in requested]


def discover(prompt, prefs, run_id=None, workflow_id=None, source_keys=None):
    """Serialize local feed runs and make unexpected interruption resumable."""
    if not _DISCOVERY_LOCK.acquire(blocking=False):
        raise HTTPException(409,"Another employer search is still running. Wait for its checkpoint or cancel it before starting another.")
    run_id=run_id or str(uuid.uuid4())
    try:
        return _discover(prompt,prefs,run_id,workflow_id,source_keys)
    finally:
        _DISCOVERY_LOCK.release()


def _discover(prompt, prefs, run_id, workflow_id=None, source_keys=None):
    prefs = infer_prompt_preferences(prompt, prefs)
    terms = search_terms(prompt, prefs)
    if not terms:
        raise HTTPException(422, "Add a role or career category in Career Preferences, or include a role in your search prompt.")

    sources = select_discovery_sources(source_keys)
    with database() as db:
        configured_employers = db.execute("SELECT COUNT(DISTINCT lower(trim(name))) FROM employers").fetchone()[0]
    started_at = datetime.now(timezone.utc).isoformat()
    token,prior_diagnostics=_acquire_discovery_run(prompt,run_id,workflow_id,len(sources))
    stop=threading.Event()
    heartbeat=threading.Thread(target=_heartbeat_discovery,args=(run_id,token,stop),daemon=True)
    heartbeat.start()
    try:
        return _discover_owned(prompt,prefs,run_id,terms,sources,configured_employers,prior_diagnostics,started_at,token)
    except Exception as error:
        _finish_discovery_ownership(run_id,token,interrupted=True,
            error=f"{type(error).__name__} during discovery; resume from the last saved checkpoint.")
        raise
    finally:
        stop.set();heartbeat.join(timeout=2)
        _finish_discovery_ownership(run_id,token)


def _discover_owned(prompt,prefs,run_id,terms,sources,configured_employers,prior_diagnostics,started_at,owner_token):
    confirmed_experience_years = None
    confirmed_skills = _confirmed_candidate_skills()
    prefs = {**prefs, "_confirmed_experience_years": confirmed_experience_years}
    diagnostics = {
        "employers_configured": configured_employers, "companies_checked": 0,
        "sources_checked": 0, "source_platforms": [], "postings_retrieved": 0,
        "new_jobs": 0, "previously_seen_jobs": 0,
        "relevant_jobs": 0, "rejected_unrelated": 0,
        "duplicate_listings_removed": 0, "jobs_requiring_analysis": 0,
        "expired_or_removed": 0,
        "geographic_mismatches": 0, "location_needs_review": 0,
        "experience_mismatches": 0,
        "rejected_by_filter": {key: 0 for key in (
            "invalid_posting", "role", "experience_level", "employment_type",
            "location", "work_arrangement", "instructions", "invalid_application_url",
        )},
        "preliminary_matches": 0, "needs_review": 0, "feed_failures": [],
        "completed_sources": [], "found_job_keys": [], "checked_sources": [], "source_stats": {},
        "current_source": None, "last_successful_checkpoint": None,
    }
    # A workflow resume reuses its run ID. Restore cumulative counters and skip
    # feeds that were fully checkpointed before an interruption.
    has_source_checkpoints="completed_sources" in prior_diagnostics
    if has_source_checkpoints:
        for key,value in prior_diagnostics.items():
            if key in diagnostics:
                diagnostics[key]=value
    found = []
    completed_sources=set(diagnostics.get("completed_sources") or [])
    checked_sources=set(diagnostics.get("checked_sources") or [])
    source_stats=diagnostics.get("source_stats") if isinstance(diagnostics.get("source_stats"),dict) else {}
    diagnostics["source_stats"]=source_stats
    seen_urls = set()
    saved_keys=list(dict.fromkeys(diagnostics.get("found_job_keys") or []))
    if saved_keys:
        placeholders=",".join("?" for _ in saved_keys)
        with database() as db:
            saved=db.execute(f"SELECT * FROM discovered_openings WHERE user_id=? AND job_key IN ({placeholders})",
                             (USER,*saved_keys)).fetchall()
        for row in saved:
            item=dict(row)
            item.update(review_status="needs_review",review_reasons=["Verify job details and eligibility before applying."])
            found.append(item)
            seen_urls.add(canonicalize_job_url(item["url"]))
    timestamp = datetime.now(timezone.utc).isoformat()
    for source in sources:
        source_key=f"{source.source_type}:{source.token}"
        if source_key in completed_sources:
            continue
        # A source that failed during an earlier partial run is retried. Replace
        # its prior counter contribution so resumed diagnostics remain totals
        # over distinct sources rather than repeated attempts.
        prior_stats=source_stats.get(source_key,{})
        for key,value in prior_stats.items():
            if key=="rejected_by_filter" and isinstance(value,dict):
                for reason,count in value.items():diagnostics["rejected_by_filter"][reason]=max(0,diagnostics["rejected_by_filter"].get(reason,0)-int(count or 0))
            elif key in diagnostics and isinstance(diagnostics[key],int):
                diagnostics[key]=max(0,diagnostics[key]-int(value or 0))
        diagnostics["feed_failures"]=[failure for failure in diagnostics["feed_failures"] if failure.get("source_key")!=source_key]
        baseline={key:value for key,value in diagnostics.items() if isinstance(value,int)}
        baseline_rejected=dict(diagnostics["rejected_by_filter"])
        with database() as db:
            run = db.execute("SELECT cancel_requested FROM job_discovery_runs WHERE run_id=? AND user_id=?",(run_id,USER)).fetchone()
        if run and run["cancel_requested"]:
            break
        source_found_start=len(found)
        source_stage="feed_fetch"
        source_success=False
        try:
            checked_sources.add(source_key)
            diagnostics["checked_sources"]=sorted(checked_sources)
            diagnostics["sources_checked"]=len(checked_sources)
            diagnostics["companies_checked"] += 1
            diagnostics["current_source"]={"company":source.name,"provider":source.source_type,"stage":"feed_fetch"}
            with database() as db:
                db.execute("UPDATE job_discovery_runs SET sources_checked=?,diagnostics_json=?,updated_at=? WHERE run_id=? AND user_id=?",
                           (diagnostics["sources_checked"],json.dumps(diagnostics),datetime.now(timezone.utc).isoformat(),run_id,USER))
            if source.source_type not in diagnostics["source_platforms"]:
                diagnostics["source_platforms"].append(source.source_type)
            source_stage="feed_fetch"
            if source.source_type == "greenhouse":
                board_jobs, error = fetch_board(source.name, source.token)
            else:
                board_jobs, error = fetch_source(source)
            source_stage="feed_postprocessing"
            if error:
                diagnostics["feed_failures"].append({"company": source.name, "source_platform": source.source_type,
                    "stage":"feed_fetch","reason":error,"source_key":source_key})
                with database() as db:
                    stamp = datetime.now(timezone.utc).isoformat()
                    db.execute("UPDATE employers SET last_attempt_at=?,connector_status=?,error_details=?,updated_at=? WHERE source_type=? AND source_token=?",
                               (stamp,_source_health(error),error[:500],stamp,source.source_type,source.token))
                    current=db.execute("SELECT consecutive_failures FROM employers WHERE source_type=? AND source_token=?",
                                       (source.source_type,source.token)).fetchone()
                    failures=(int(current[0] or 0)+1) if current else 1
                    state=_source_health(error)
                    retry=_next_source_retry(state,error,failures)
                    db.execute("UPDATE employers SET consecutive_failures=?,next_retry_at=? WHERE source_type=? AND source_token=?",
                               (failures,retry,source.source_type,source.token))
                    db.execute("UPDATE job_discovery_runs SET sources_checked=?,postings_retrieved=?,diagnostics_json=?,updated_at=? WHERE run_id=? AND user_id=?",
                               (diagnostics["sources_checked"],diagnostics["postings_retrieved"],json.dumps(diagnostics),stamp,run_id,USER))
                if not isinstance(board_jobs,list) or not board_jobs:
                    continue
            if not isinstance(board_jobs, list):
                diagnostics["feed_failures"].append({"company": source.name, "source_platform": source.source_type,
                    "stage":"feed_response_validation","reason": "Malformed response: jobs was not a list.","source_key":source_key})
                continue
            if not error and source.source_type in {"greenhouse", "lever", "ashby", "smartrecruiters", "career_page", "public_html", "rss", "sitemap"}:
                present = {}
                for raw in board_jobs:
                    if isinstance(raw, dict):
                        try:
                            normalized=normalize_job(source.source_type,source,raw)
                            present[normalized["job_key"]]=normalized
                        except (TypeError,ValueError):
                            continue
                with database() as db:
                    saved_rows=db.execute("SELECT job_key,active,availability_status,reopened_review_required FROM discovered_openings WHERE user_id=? AND source_platform=?",
                                          (USER,source.source_type)).fetchall()
                    has_match_table=bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_match_results'").fetchone())
                    source_job_prefix=f"{source.source_type}:{source.token}:"
                    for saved in saved_rows:
                        if not str(saved["job_key"] or "").startswith(source_job_prefix):
                            continue
                        observed=present.get(saved["job_key"])
                        if observed is None:
                            # Absence from a successful feed is not proof of expiry.
                            # Withdraw it from the active set and return availability
                            # to unknown unless explicit closure evidence was saved.
                            if saved["active"]:
                                db.execute("UPDATE discovered_openings SET active=0,availability_status=CASE WHEN availability_status IN ('expired','closed','removed') THEN availability_status ELSE 'unknown' END WHERE user_id=? AND job_key=?",
                                           (USER,saved["job_key"]))
                                if has_match_table:
                                    db.execute("UPDATE job_match_results SET freshness_status='stale' WHERE user_id=? AND job_key=? AND freshness_status='current'",
                                               (USER,saved["job_key"]))
                            continue
                        state=observed.get("availability_status") or "unknown"
                        was_explicitly_closed=(not bool(saved["active"]) and str(saved["availability_status"]).casefold() in {"closed","expired","removed"})
                        reopened=bool(was_explicitly_closed and observed["active"] and state=="current")
                        db.execute("UPDATE discovered_openings SET active=?,availability_status=?,last_verified_at=?,reopened_review_required=CASE WHEN ? THEN 1 ELSE reopened_review_required END WHERE user_id=? AND job_key=?",
                                   (int(bool(observed["active"])),state,observed["last_verified_at"],int(reopened),USER,saved["job_key"]))
                        if has_match_table and (not observed["active"] or state!="current") and saved["active"]:
                            db.execute("UPDATE job_match_results SET freshness_status='stale' WHERE user_id=? AND job_key=? AND freshness_status='current'",
                                       (USER,saved["job_key"]))
            if not error:
                stamp = datetime.now(timezone.utc).isoformat()
                with database() as db:
                    db.execute("""UPDATE employers SET last_attempt_at=?,last_success_at=?,connector_status='healthy',error_details='',
                        consecutive_failures=0,next_retry_at=NULL,updated_at=? WHERE source_type=? AND source_token=?""",
                               (stamp,stamp,stamp,source.source_type,source.token))
            diagnostics["postings_retrieved"] += len(board_jobs)
            source_stage="job_normalization_and_filtering"
            for raw_job in board_jobs:
                if not isinstance(raw_job, dict):
                    diagnostics["rejected_by_filter"]["invalid_posting"] += 1
                    continue
                try:
                    record = normalize_job(source.source_type, source, raw_job)
                except (TypeError, ValueError):
                    diagnostics["rejected_by_filter"]["invalid_posting"] += 1
                    continue
                with database() as db:
                    prior=db.execute("SELECT content_fingerprint,active,availability_status,reopened_review_required FROM discovered_openings WHERE user_id=? AND job_key=?",
                                     (USER,record["job_key"])).fetchone()
                    was_previously_seen=bool(prior)
                    if prior:
                        diagnostics["previously_seen_jobs"] += 1
                    else:
                        diagnostics["new_jobs"] += 1
                    if prior:
                        changed=bool(prior["content_fingerprint"] and prior["content_fingerprint"]!=record["content_fingerprint"])
                        reopened=bool(not prior["active"] and prior["availability_status"] in {"closed","expired","removed"} and record["active"] and record["availability_status"]=="current")
                        db.execute("""UPDATE discovered_openings SET company=?,title=?,location=?,url=?,employment_type=?,description=?,
                            country=?,work_arrangement=?,posting_date=?,last_verified_at=?,active=?,availability_status=?,content_fingerprint=?,job_url=?,salary_json=?,
                            reopened_review_required=CASE WHEN ? THEN 1 ELSE reopened_review_required END
                            WHERE user_id=? AND job_key=?""",
                            (source.name,record["title"],record["location"] or "Unspecified",record["application_url"],record["employment_type"],
                             record["description"],record["country"],record["work_arrangement"],record["posting_date"],record["last_verified_at"],
                             int(record["active"]),record["availability_status"],record["content_fingerprint"],record["job_url"],json.dumps(record["salary"]) if record["salary"] is not None else None,int(reopened),USER,record["job_key"]))
                        if changed:
                            table=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_match_results'").fetchone()
                            if table:
                                cols={row[1] for row in db.execute("PRAGMA table_info(job_match_results)")}
                                if "freshness_status" in cols:
                                    db.execute("UPDATE job_match_results SET freshness_status='stale' WHERE user_id=? AND job_key=?",(USER,record["job_key"]))
                if not record["active"]:
                    diagnostics["expired_or_removed"] += 1
                    continue
                job = _legacy_job(record)
                normalized_job = normalize_job_record(job, source.source_type, source.name, record["job_key"])
                relevant, _, relevance_evidence = relevance_for_job(normalized_job, prefs, prompt)
                if not relevant:
                    diagnostics["rejected_by_filter"]["role"] += 1
                    diagnostics["rejected_unrelated"] += 1
                    continue
                diagnostics["relevant_jobs"] += 1
                accepted, reason, review_reasons = assess_job(job, prefs, terms)
                if not accepted:
                    diagnostics["rejected_by_filter"][reason] += 1
                    if reason == "location":
                        diagnostics["geographic_mismatches"] += 1
                    if reason == "experience_level":
                        diagnostics["experience_mismatches"] += 1
                    continue
                geo_status, geo_reason = evaluate_location(job, prefs.get("location", ""))
                if geo_status == "unknown":
                    diagnostics["location_needs_review"] += 1
                url = source_application_url(record["application_url"], source)
                if not url:
                    diagnostics["rejected_by_filter"]["invalid_application_url"] += 1
                    continue
                location = _job_location(job) or "Unspecified"
                employment_type = _employment(job)
                experience_level, experience_level_evidence = classify_role_level(
                    job["title"], record["description"], employment_type)
                record["experience_level"] = experience_level
                record["career_category"] = classify_career_category(job["title"], prefs)
                if experience_level=="Unknown":
                    review_reasons.extend(experience_level_evidence)
                score, ranking_explanation, _, _ = discovery_relevance_score(
                    {**job, "title": job["title"], "description": record["description"],
                     "employment_type": employment_type, "location_status": geo_status,
                     "posting_date": record["posting_date"]}, prefs, confirmed_skills)
                canonical = canonicalize_job_url(url)
                if canonical in seen_urls:
                    diagnostics["duplicate_listings_removed"] += 1
                    # Retain source provenance without deleting or rewriting the
                    # source-specific saved opening.
                    with database() as db:
                        db.execute("INSERT OR IGNORE INTO job_source_provenance(source_platform,source_job_key,canonical_url,content_fingerprint,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?)",
                                   (source.source_type, record["job_key"], canonical, record["content_fingerprint"], timestamp, timestamp))
                    continue
                with database() as db:
                    primary_source = db.execute("SELECT source_job_key FROM job_source_provenance WHERE canonical_url=? ORDER BY first_seen_at,source_platform LIMIT 1",
                                                (canonical,)).fetchone()
                    if primary_source and primary_source["source_job_key"] != record["job_key"]:
                        diagnostics["duplicate_listings_removed"] += 1
                        db.execute("INSERT OR IGNORE INTO job_source_provenance(source_platform,source_job_key,canonical_url,content_fingerprint,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?)",
                                   (source.source_type, record["job_key"], canonical, record["content_fingerprint"], timestamp, timestamp))
                        continue
                seen_urls.add(canonical)
                found.append({
                    "job_key": record["job_key"], "company": source.name,
                    "discovery_status":"previously_seen" if was_previously_seen else "newly_discovered",
                    "description_available":usable_posting_description(record["description"]),
                    "title": job["title"], "location": location, "url": url,
                    "employment_type": employment_type, "location_status": geo_status,
                    "location_reason": geo_reason, "search_prompt": str(prompt or "")[:1500],
                    "role_level":experience_level,"experience_level":experience_level,
                    "experience_level_evidence":experience_level_evidence,
                    "experience_requirements":extract_experience_requirements(record["description"]),
                    "graduation_requirements":extract_graduation_windows(record["description"]),
                    "sponsorship_requirement":extract_sponsorship_requirement(record["description"]),
                    "review_status": "needs_review",
                    "review_reasons": review_reasons or ["Verify job details and eligibility before applying."],
                    "discovery_source": "Company Careers Page",
                    "source_platform": source.source_type, "source_id": record["source_id"],
                    "country": record["country"], "work_arrangement": record["work_arrangement"],
                    "description": record["description"], "posting_date": record["posting_date"],
                    "last_verified_at": record["last_verified_at"], "active": 1,
                    "availability_status": record["availability_status"],
                    "content_fingerprint": record["content_fingerprint"], "job_url": record["job_url"],
                    "salary_json":json.dumps(record["salary"]) if record["salary"] is not None else None,
                    "career_category":record["career_category"],"experience_level":record["experience_level"],
                    "required_qualifications_json":None,"preferred_qualifications_json":None,
                    "experience_level_evidence":experience_level_evidence,
                    "graduation_windows":extract_graduation_windows(record["description"]),
                    "education_requirements":extract_education_requirements(record["description"]),
                    "relevance_score":score,"ranking_explanation":ranking_explanation,
                })
            source_stage="source_progress_persistence"
            with database() as db:
                db.execute("UPDATE job_discovery_runs SET sources_checked=?,postings_retrieved=?,diagnostics_json=?,updated_at=? WHERE run_id=? AND user_id=?",
                           (diagnostics["sources_checked"],diagnostics["postings_retrieved"],json.dumps(diagnostics),datetime.now(timezone.utc).isoformat(),run_id,USER))
            source_success=not bool(error)
        except Exception as error:
            # Employer feeds are independent. Keep the workflow moving while
            # recording a sanitized source/phase diagnostic (never raw job text).
            diagnostic_ref=uuid.uuid4().hex[:10]
            logger.exception("Employer discovery source failed: company=%s provider=%s stage=%s error_type=%s diagnostic_ref=%s",
                             source.name,source.source_type,source_stage,type(error).__name__,diagnostic_ref)
            diagnostics["feed_failures"].append({"company":source.name,"source_platform":source.source_type,
                "stage":source_stage,"reason":f"{type(error).__name__} during {source_stage}; backend log reference {diagnostic_ref}.",
                "diagnostic_ref":diagnostic_ref,"source_key":source_key})
        finally:
            _persist_discovered_items(found[source_found_start:],timestamp)
            if source_success:
                completed_sources.add(source_key)
                diagnostics["last_successful_checkpoint"]={"company":source.name,"provider":source.source_type,
                    "at":datetime.now(timezone.utc).isoformat()}
            diagnostics["current_source"]=None
            source_stats[source_key]={key:max(0,diagnostics.get(key,0)-baseline.get(key,0))
                for key in baseline if key!="sources_checked"}
            source_stats[source_key]["rejected_by_filter"]={key:max(0,diagnostics["rejected_by_filter"].get(key,0)-baseline_rejected.get(key,0))
                for key in diagnostics["rejected_by_filter"]}
            diagnostics["completed_sources"]=sorted(completed_sources)
            diagnostics["found_job_keys"]=list(dict.fromkeys(item["job_key"] for item in found))
            with database() as db:
                db.execute("UPDATE job_discovery_runs SET sources_checked=?,postings_retrieved=?,diagnostics_json=?,updated_at=? WHERE run_id=? AND user_id=?",
                           (diagnostics["sources_checked"],diagnostics["postings_retrieved"],json.dumps(diagnostics),datetime.now(timezone.utc).isoformat(),run_id,USER))

    diagnostics["preliminary_matches"] = len(found)
    diagnostics["needs_review"] = len(found)
    all_failed = bool(sources) and len(diagnostics["feed_failures"]) == len(sources)
    if all_failed:
        status = "all_sources_unavailable"
    elif found:
        status = "preliminary_matches"
    elif diagnostics["postings_retrieved"] == 0:
        status = "no_matching_openings"
    else:
        status = "filters_too_restrictive"
    with database() as db:
        cancelled=db.execute("SELECT cancel_requested FROM job_discovery_runs WHERE run_id=? AND user_id=?",(run_id,USER)).fetchone()
        cancel_reason=db.execute("SELECT error FROM job_discovery_runs WHERE run_id=? AND user_id=?",(run_id,USER)).fetchone()
    if cancelled and cancelled["cancel_requested"]:
        status="interrupted" if cancel_reason and cancel_reason["error"]=="pause_requested" else "cancelled"

    timestamp = datetime.now(timezone.utc).isoformat()
    with database() as db:
        for item in found:
            prior=db.execute("SELECT content_fingerprint FROM discovered_openings WHERE user_id=? AND job_key=?",(USER,item["job_key"])).fetchone()
            if prior and prior["content_fingerprint"] and prior["content_fingerprint"]!=item["content_fingerprint"]:
                table=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_match_results'").fetchone()
                if table:
                    cols={row[1] for row in db.execute("PRAGMA table_info(job_match_results)")}
                    if "freshness_status" in cols:
                        db.execute("UPDATE job_match_results SET freshness_status='stale' WHERE user_id=? AND job_key=?",(USER,item["job_key"]))
            db.execute("""INSERT INTO discovered_openings
                (user_id,job_key,company,title,location,url,discovered_at,employment_type,location_status,location_reason,search_prompt,discovery_source,
                 source_platform,source_id,country,work_arrangement,description,posting_date,last_verified_at,active,content_fingerprint,job_url,
                 salary_json,career_category,experience_level,required_qualifications_json,preferred_qualifications_json,availability_status)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,job_key) DO UPDATE SET
                company=excluded.company,title=excluded.title,location=excluded.location,url=excluded.url,
                employment_type=excluded.employment_type,location_status=excluded.location_status,location_reason=excluded.location_reason,
                search_prompt=excluded.search_prompt,discovery_source=excluded.discovery_source,
                source_platform=excluded.source_platform,source_id=excluded.source_id,country=excluded.country,
                work_arrangement=excluded.work_arrangement,description=excluded.description,posting_date=excluded.posting_date,
                last_verified_at=excluded.last_verified_at,active=excluded.active,availability_status=excluded.availability_status,
                content_fingerprint=excluded.content_fingerprint,job_url=excluded.job_url,
                salary_json=excluded.salary_json,career_category=excluded.career_category,experience_level=excluded.experience_level,
                required_qualifications_json=excluded.required_qualifications_json,preferred_qualifications_json=excluded.preferred_qualifications_json""",
                       (USER, item["job_key"], item["company"], item["title"], item["location"], item["url"], timestamp,
                        item["employment_type"], item["location_status"], item["location_reason"], item["search_prompt"],item["discovery_source"],
                        item["source_platform"],item["source_id"],item["country"],item["work_arrangement"],item["description"],item["posting_date"],
                        item["last_verified_at"],item["active"],item["content_fingerprint"],item["job_url"],item["salary_json"],item["career_category"],item["experience_level"],item["required_qualifications_json"],item["preferred_qualifications_json"],item["availability_status"]))
            canonical = canonicalize_job_url(item["url"])
            db.execute("INSERT INTO job_source_provenance(source_platform,source_job_key,canonical_url,content_fingerprint,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?) ON CONFLICT(source_platform,source_job_key) DO UPDATE SET canonical_url=excluded.canonical_url,content_fingerprint=excluded.content_fingerprint,last_seen_at=excluded.last_seen_at",
                       (item["source_platform"],item["job_key"],canonical,item["content_fingerprint"],timestamp,timestamp))
        keys = [item["job_key"] for item in found]
        stored = {}
        if keys:
            placeholders = ",".join("?" for _ in keys)
            rows = db.execute(f"""SELECT company,title,location,url,job_key,discovered_at,employment_type,location_status,location_reason,search_prompt,
                source_platform,source_id,country,work_arrangement,description,posting_date,last_verified_at,active,content_fingerprint,job_url,
                salary_json,career_category,experience_level,required_qualifications_json,preferred_qualifications_json
                FROM discovered_openings WHERE user_id=? AND job_key IN ({placeholders})""", (USER, *keys)).fetchall()
            stored = {row["job_key"]: dict(row) for row in rows}

    results = []
    ranking_skills = _confirmed_candidate_skills()
    for item in found:
        if item["job_key"] not in stored: continue
        result={**stored[item["job_key"]],"review_status":item["review_status"],"review_reasons":item["review_reasons"]}
        score, ranking_explanation, experience_level, level_evidence = discovery_relevance_score(result, prefs, ranking_skills)
        result.update(experience_level=result.get("experience_level") or experience_level,
                      role_level=item.get("role_level",result.get("experience_level")),
                      career_category=result.get("career_category") or classify_career_category(result.get("title"), prefs),
                      relevance_score=item.get("relevance_score",score),
                      ranking_explanation=item.get("ranking_explanation",ranking_explanation),
                      experience_level_evidence=item.get("experience_level_evidence",level_evidence),
                      experience_requirements=item.get("experience_requirements",extract_experience_requirements(result.get("description"))),
                      graduation_requirements=item.get("graduation_requirements",extract_graduation_windows(result.get("description"))),
                      sponsorship_requirement=item.get("sponsorship_requirement",extract_sponsorship_requirement(result.get("description"))),
                      graduation_windows=item.get("graduation_windows",extract_graduation_windows(result.get("description"))),
                      education_requirements=item.get("education_requirements",extract_education_requirements(result.get("description"))))
        result.update(discovery_status=item.get("discovery_status","previously_seen"),
                      description_available=usable_posting_description(result.get("description")),
                      matching_status="not_analyzed",eligibility_classification="needs_review",
                      matching_failure_reason="")
        for field in ("salary_json","required_qualifications_json","preferred_qualifications_json"):
            raw=result.pop(field,None)
            try: result[field.removesuffix("_json")]=json.loads(raw) if raw else None
            except (TypeError,json.JSONDecodeError): result[field.removesuffix("_json")]=None
        results.append(result)
    if results:
        try:
            # Reuse only assessments that the matcher itself considers fresh;
            # older saved results remain visible as needing reassessment.
            import resume_job_matcher
            active_resume,_,_=resume_job_matcher.active_candidate()
            existing=resume_job_matcher._match_rows([item["job_key"] for item in results],active_resume)
            by_key={item["job_key"]:item for item in existing}
            for item in results:
                match=by_key.get(item["job_key"])
                if not match:continue
                item["matching_status"]="stale" if match.get("stale") else match.get("status","not_analyzed")
                item["eligibility_classification"]=(match.get("classification") if match.get("status")=="completed" and not match.get("stale")
                    else "needs_review")
                item["matching_failure_reason"]=_public_match_error(match.get("error")) if match.get("status")=="failed" else ""
        except (ImportError,sqlite3.Error,KeyError,TypeError,ValueError,HTTPException):
            # Search results must remain available if the optional saved-match
            # diagnostic cannot be read in a partially migrated database.
            pass
    results.sort(key=lambda item: (-int(item.get("relevance_score", 50)),
                                   str(item.get("posting_date") or ""),
                                   str(item.get("company") or "").casefold()))
    diagnostics["genuine_early_career_candidates"] = sum(
        item.get("role_level") in {"New Graduate","University Graduate","Entry Level","Junior","Associate","Software Engineer I"}
        for item in results)
    diagnostics["experience_level_needs_review"] = sum(item.get("role_level")=="Unknown" for item in results)
    diagnostics["internship_candidates"] = sum(item.get("role_level")=="Internship" for item in results)
    diagnostics["mid_level_or_senior_candidates"] = sum(item.get("role_level") in {"Mid-Level","Senior","Staff/Principal"} for item in results)
    diagnostics["jobs_requiring_analysis"] = len(results)
    warnings = [f"Some sources were unavailable; results cover only feeds that responded ({len(diagnostics['feed_failures'])}/{len(sources)} failed)."
                for _ in [0] if diagnostics["feed_failures"] and not all_failed]
    final_state=status if status in {"cancelled","interrupted"} else "completed"
    with database() as db:
        db.execute("UPDATE job_discovery_runs SET status=?,sources_checked=?,postings_retrieved=?,diagnostics_json=?,updated_at=? WHERE run_id=? AND user_id=?",
                   (final_state,diagnostics["sources_checked"],diagnostics["postings_retrieved"],json.dumps(diagnostics),datetime.now(timezone.utc).isoformat(),run_id,USER))
    return {
        "status": status, "results": results, "count": len(results),
        "preliminary": True,
        "matching": "Rule-based title and saved-preference checks. Every result needs human review; title similarity is not approval.",
        "diagnostics": diagnostics, "feed_warnings": warnings,"discovery_run_id":run_id,
    }


@router.get("/find-jobs", response_class=HTMLResponse)
def find_jobs_page():
    shortlist_panel='''<main class="panel" aria-labelledby="shortlistHeading"><h2 id="shortlistHeading">Saved opportunities shortlist</h2><p class="note">Ranked by posting verification, early-career evidence, description completeness, saved preferences, and match freshness. Ranking is not an eligibility decision.</p><p class="note">Analyze targets only openings labeled “Ready for matching” in the visible shortlist (up to five). Current results and paused checkpoints are excluded; starting analysis requires this button.</p><button id="refreshShortlist" type="button">Refresh shortlist</button> <button id="analyzeShortlist" type="button" disabled>Analyze current shortlist with AI</button><p id="shortlistStatus" role="status" aria-live="polite">Loading saved openings…</p><div id="shortlistCounts" class="diagnostics"></div><div id="shortlistResults"></div></main>'''
    shortlist_script=r'''const shortlistRoot=document.getElementById('shortlistResults');
let shortlistLoad=null;
function addEvidenceDetails(card,evidence){
  if(!evidence)return;
  const alignment=evidence.candidate_alignment||{};
  const groups=[['Mandatory qualifications',evidence.mandatory],['Confirmed matches',evidence.confirmed_matches],['Confirmed conflicts',evidence.confirmed_conflicts],['Needs applicant evidence',evidence.needs_review],['Preferred qualifications',evidence.preferred]];
  const present=groups.some(([,items])=>Array.isArray(items)&&items.length)||evidence.missing_information?.length||evidence.employer_metadata_missing?.length||Object.keys(alignment).length;
  if(!present)return;
  const details=document.createElement('details'),summary=document.createElement('summary');summary.textContent='Eligibility evidence and remaining details';details.append(summary);
  for(const [label,items] of groups){if(!Array.isArray(items)||!items.length)continue;const heading=document.createElement('h4');heading.textContent=label;details.append(heading);const list=document.createElement('ul');for(const item of items){const row=document.createElement('li');const cited=(item.candidate_evidence||[]).join('; ');row.textContent=`${item.requirement} · ${String(item.status||'needs review').replaceAll('_',' ')}${cited?` · Candidate evidence cited: ${cited}`:''}`;list.append(row)}details.append(list)}
  if(Object.keys(alignment).length){const heading=document.createElement('h4');heading.textContent='Candidate and preference fit';details.append(heading);const list=document.createElement('ul');for(const [label,value] of [['Role alignment',alignment.role_relevance_status],['Saved location preference',alignment.target_location_reason],['Relocation',alignment.effective_location_reason],['Work authorization',alignment.work_authorization_status==='needs_applicant_confirmation'?`Applicant confirmation needed for ${alignment.country||'the posting country'}`:String(alignment.work_authorization_status||'Unknown').replaceAll('_',' ')],['Employment type',String(alignment.employment_type_status||'unknown').replaceAll('_',' ')],['Work arrangement',String(alignment.work_arrangement_status||'unknown').replaceAll('_',' ')],['Seniority',String(alignment.experience_level_status||'unknown').replaceAll('_',' ')],['Mandatory pre-screen',alignment.mandatory_conflict||'No confirmed mandatory conflict found']]){if(!value)continue;const row=document.createElement('li');row.textContent=`${label}: ${value}`;list.append(row)}details.append(list)}
  if(evidence.missing_information?.length){const p=document.createElement('p');p.textContent='Missing applicant evidence: '+evidence.missing_information.join('; ');details.append(p)}
  if(evidence.employer_metadata_missing?.length){const p=document.createElement('p');p.textContent='Employer metadata not stated: '+evidence.employer_metadata_missing.join(', ');details.append(p)}card.append(details)
}
async function loadShortlist(){if(shortlistLoad)return shortlistLoad;shortlistLoad=(async()=>{const status=document.getElementById('shortlistStatus');status.className='';status.textContent='Loading saved openings…';try{const response=await fetch('/api/jobs/shortlist?limit=10');const data=await readApiResponse(response,'Saved opportunities could not be loaded.');const counts=data.freshness_counts||{},mc=data.match_freshness_counts||{};document.getElementById('shortlistCounts').textContent=`Saved ${data.saved_count||0} · Current postings ${counts.currently_verified||0} · Recheck required ${counts.previously_verified_recheck_required||0} · Expired/closed ${counts.expired_or_closed||0} · Source unavailable ${counts.source_unavailable||0} · Unverified ${counts.unverified||0} · Current matches ${mc.current||0} · Stale ${mc.stale||0} · Legacy unknown ${mc.legacy_unknown||0} · Queued ${mc.queued||0} · Never analyzed ${mc.never_analyzed||0} · Failed ${mc.failed||0}`;shortlistRoot.replaceChildren();if(data.confirmed_ineligible?.length){const group=document.createElement('details'),summary=document.createElement('summary');summary.textContent=`Confirmed ineligible (${data.confirmed_ineligible.length})`;group.append(summary);for(const item of data.confirmed_ineligible){const row=document.createElement('p');row.textContent=`${item.title} · ${item.company} · ${item.location}${item.reasons?.length?' · Mandatory conflict: '+item.reasons.join('; '):''} · ${item.next_action}`;group.append(row)}shortlistRoot.append(group)}if(data.preference_conflicts?.length){const group=document.createElement("details"),summary=document.createElement("summary");summary.textContent=`Saved preference conflicts (${data.preference_conflicts.length})`;group.append(summary);for(const item of data.preference_conflicts){const row=document.createElement("p");row.textContent=`${item.title} · ${item.company} · ${item.location||"Location not listed"} · ${item.reason} · ${item.next_action}`;group.append(row)}shortlistRoot.append(group)}for(const job of data.results||[]){const card=document.createElement('article');card.className='job';const h=document.createElement('h3');h.textContent=`${job.title||'Untitled role'} · ${job.company||'Employer unknown'}`;const details=document.createElement('p');details.textContent=`${job.location} · ${job.work_arrangement} · ${job.employment_type}`;const freshness=document.createElement('p');freshness.textContent=`Posting: ${String(job.posting_freshness||'unverified').replaceAll('_',' ')}${job.last_verified_at?' · last verified '+new Date(job.last_verified_at).toLocaleDateString():''} · Level: ${job.experience_level_evidence} · Employer: ${String(job.employer_identity_status||'needs review').replaceAll('_',' ')} · ${job.source_reliability}`;const statusLabels={potential_match:'Potential match',needs_review:'Needs review',not_eligible:'Not eligible'};const match=document.createElement('p');match.textContent=`Readiness: ${String(job.readiness_status||'needs review').replaceAll('_',' ')} · Match: ${String(job.match_freshness||'never analyzed').replaceAll('_',' ')} · Eligibility: ${statusLabels[job.eligibility_status]||job.eligibility_status||'Needs review'}${job.match_score===null||job.match_score===undefined?'':` · score ${job.match_score} (ranking aid only)`}`;const next=document.createElement('p');next.textContent=`Next action: ${job.next_action}${job.missing_requirements?.length?' · Missing: '+job.missing_requirements.join('; '):''}`;card.append(h,details,freshness,match,next);if(job.match_error){const error=document.createElement('p');error.className='error';error.textContent=job.match_error;card.append(error)}addEvidenceDetails(card,job.qualification_evidence);if(job.url){const link=document.createElement('a');link.href=job.url;link.target='_blank';link.rel='noopener noreferrer';link.textContent='Open official posting';card.append(link)}if(job.match_freshness==='failed'&&data.matching_authorized&&job.posting_freshness==='currently_verified'&&job.employer_identity_status==='verified'&&job.description_available){const retry=document.createElement('button');retry.type='button';retry.textContent='Retry this job only';retry.setAttribute('aria-label',`Retry matching for ${job.title} at ${job.company}`);retry.addEventListener('click',async()=>{retry.disabled=true;status.className='';status.textContent=`Rechecking the official posting before retrying ${job.title}…`;try{const retried=await readApiResponse(await fetch('/api/jobs/matches/retry-failed',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({job_key:job.job_key})}),'Selected job retry could not start.');currentMatchJobKeys=[job.job_key];currentMatchSessionId=retried.session_id||null;pollingGeneration++;polling=false;matchResults.replaceChildren();matchStatus.textContent='A new matching-only session started for this failed job. Paused sessions were left unchanged; no application was queued.';if(currentMatchSessionId)startPolling(currentMatchSessionId);status.textContent='Retry started for the selected job only.'}catch(error){status.className='error';status.textContent=error.message}finally{retry.disabled=false;loadShortlist()}});card.append(retry)}shortlistRoot.append(card)}const analyze=document.getElementById('analyzeShortlist');analyze.dataset.jobKeys=(data.ready_job_keys||[]).join(',');analyze.disabled=!data.matching_authorized||!data.ready_count;analyze.title=!data.matching_authorized?'Enable AI matching in Application settings first.':(!data.ready_count?'No current verified jobs need a new matching run. Failed jobs have a separate targeted retry action.':'Starts a bounded matching run only after you click.');status.className='';status.textContent=data.count?`Showing ${data.count} prioritized openings. A match score is not eligibility approval.`:'No saved postings currently meet the relevance and freshness filters. Recheck sources or import another official posting.'}catch(error){shortlistRoot.replaceChildren();status.className='error';status.textContent=error.message}finally{shortlistLoad=null}})();return shortlistLoad}
'''
    shortlist_script=shortlist_script.replace("for(const job of data.results||[]){",
  "const groupLabels={strong_early_career:'Strong early-career opportunities',promising_needs_review:'Promising jobs needing review',level_or_location_uncertain:'Level or location uncertain',awaiting_posting_verification:'Awaiting posting verification'};for(const groupKey of Object.keys(groupLabels)){const jobs=(data.results||[]).filter(job=>job.opportunity_group===groupKey);if(!jobs.length)continue;const heading=document.createElement('h2');heading.textContent=groupLabels[groupKey]+` (${jobs.length})`;shortlistRoot.append(heading);for(const job of jobs){")
    shortlist_script=shortlist_script.replace("shortlistRoot.append(card)}const analyze=document.getElementById('analyzeShortlist');",
  "shortlistRoot.append(card)}}if(!data.results?.length){const empty=document.createElement('p');empty.textContent='No saved openings currently meet the relevance and freshness checks.';shortlistRoot.append(empty)}const analyze=document.getElementById('analyzeShortlist');")
    shortlist_script=shortlist_script.replace("['Preferred qualifications',evidence.preferred]",
  "['Preferred qualifications',evidence.preferred],['Professional experience requirements',evidence.experience_requirements],['Education requirements',evidence.education_requirements],['GPA requirements',evidence.gpa_requirements],['Graduation requirements',evidence.graduation_requirements],['Internship requirements',evidence.internship_requirements?.required]")
    shortlist_script=shortlist_script.replace("const counts=data.freshness_counts||{},mc=data.match_freshness_counts||{};",
  "const counts=data.freshness_counts||{},mc=data.match_freshness_counts||{},oc=data.opportunity_counts||{};")
    shortlist_script=shortlist_script.replace(" · Failed ${mc.failed||0}`;shortlistRoot.replaceChildren();",
  " · Failed ${mc.failed||0} · Strong early-career ${oc.strong_early_career||0} · Promising/review ${oc.promising_needs_review||0} · Level/location uncertain ${oc.level_or_location_uncertain||0} · Awaiting verification ${oc.awaiting_posting_verification||0}`;shortlistRoot.replaceChildren();")
    shortlist_script += r'''document.getElementById('refreshShortlist').addEventListener('click',loadShortlist);
document.getElementById('analyzeShortlist').addEventListener('click',async event=>{const button=event.currentTarget,keys=(button.dataset.jobKeys||'').split(',').filter(Boolean);if(!keys.length)return;button.disabled=true;const status=document.getElementById('shortlistStatus');status.textContent=`Starting matching for ${keys.length} selected saved jobs…`;try{const response=await fetch('/api/jobs/analyze-matches',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({job_keys:keys,batch_size:Math.min(5,Math.max(3,keys.length))})});const data=await readApiResponse(response,'Matching could not start.');currentMatchJobKeys=keys;currentMatchSessionId=data.session_id||null;pollingGeneration++;polling=false;matchResults.replaceChildren();matchStatus.textContent='Bounded matching session started for selected saved jobs. No unrelated paused session was resumed.';if(currentMatchSessionId)startPolling(currentMatchSessionId);status.textContent='Matching started. Check the analysis progress below.'}catch(error){status.className='error';status.textContent=error.message}finally{button.disabled=false;loadShortlist()}});
loadShortlist();'''
    page=PAGE.replace("<script>\nconst button",shortlist_panel+"\n<script>\n"+shortlist_script+"\nconst button",1)
    return apply_ui_theme(page)


@router.post("/api/jobs/search")
def search_jobs(request: SearchRequest):
    with database() as db:
        table=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='application_authorizations'").fetchone()
        authorization=db.execute("SELECT settings_json FROM application_authorizations WHERE user_id=?",(USER,)).fetchone() if table else None
    try: discovery_allowed=bool(json.loads(authorization["settings_json"] or "{}").get("allow_automatic_discovery",False)) if authorization else False
    except (TypeError,json.JSONDecodeError): discovery_allowed=False
    if not discovery_allowed:
        raise HTTPException(403,"Enable job discovery in Application settings before searching.")
    prefs = preferences()
    if prefs is None:
        raise HTTPException(422, "Save your career preferences before searching for jobs.")
    return discover(request.prompt, prefs, request.run_id,source_keys=request.source_keys)


@router.get("/api/jobs/discovery/{run_id}")
def discovery_progress(run_id: str):
    with database() as db:
        row=db.execute("SELECT status,total_sources,sources_checked,postings_retrieved,diagnostics_json,cancel_requested,error,created_at,updated_at FROM job_discovery_runs WHERE user_id=? AND run_id=?",
                       (USER,run_id)).fetchone()
    if not row: raise HTTPException(404,"Discovery run not found.")
    try: diagnostics=json.loads(row["diagnostics_json"] or "{}")
    except (TypeError,json.JSONDecodeError): diagnostics={}
    return {"run_id":run_id,"status":row["status"],"total_sources":row["total_sources"],
            "sources_checked":row["sources_checked"],"postings_retrieved":row["postings_retrieved"],
            "cancel_requested":bool(row["cancel_requested"]),"diagnostics":diagnostics,
            "created_at":row["created_at"],"updated_at":row["updated_at"]}


@router.post("/api/jobs/discovery/{run_id}/cancel")
def cancel_discovery(run_id: str):
    with database() as db:
        cursor=db.execute("UPDATE job_discovery_runs SET cancel_requested=1,error='cancel_requested',updated_at=? WHERE user_id=? AND run_id=? AND status='running'",
                          (datetime.now(timezone.utc).isoformat(),USER,run_id))
        if not cursor.rowcount:
            row=db.execute("SELECT status FROM job_discovery_runs WHERE user_id=? AND run_id=?",(USER,run_id)).fetchone()
            if not row: raise HTTPException(404,"Discovery run not found.")
            return {"cancel_requested":False,"status":row["status"]}
    return {"cancel_requested":True,"status":"cancelling"}


def pause_discovery(run_id):
    """Stop at the next source boundary while retaining a resumable run ID."""
    with database() as db:
        cursor=db.execute("UPDATE job_discovery_runs SET cancel_requested=1,error='pause_requested',updated_at=? WHERE user_id=? AND run_id=? AND status='running'",
                          (datetime.now(timezone.utc).isoformat(),USER,run_id))
        if not cursor.rowcount:
            row=db.execute("SELECT status FROM job_discovery_runs WHERE user_id=? AND run_id=?",(USER,run_id)).fetchone()
            return {"cancel_requested":False,"status":row["status"] if row else "not_found"}
    return {"cancel_requested":True,"status":"pausing"}


@router.get("/api/jobs/saved")
def saved_jobs():
    with database() as db:
        rows = db.execute("""SELECT company,title,location,url,job_key,discovered_at,employment_type,location_status,location_reason,search_prompt,
            source_platform,source_id,country,work_arrangement,posting_date,last_verified_at,active,job_url,career_category,experience_level,salary_json,
            graduation_windows_json,availability_status,imported_source_url,import_eligibility_status,import_review_json
            FROM discovered_openings WHERE user_id=? ORDER BY discovered_at DESC""", (USER,)).fetchall()
    results=[]
    for row in rows:
        item=dict(row)
        item["eligibility_classification"]=item.pop("import_eligibility_status","needs_review")
        try:item["review_reasons"]=json.loads(item.pop("import_review_json","[]") or "[]")
        except (TypeError,json.JSONDecodeError):item["review_reasons"]=["Eligibility review is required."]
        item["review_status"]="needs_review"
        results.append(item)
    return {"results":results,"count":len(results)}


def _parse_utc(value):
    try:
        parsed=datetime.fromisoformat(str(value or "").replace("Z","+00:00"))
        if parsed.tzinfo is None: parsed=parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError,ValueError):
        return None


def _public_match_error(error):
    """Map stored model diagnostics to a short actionable message."""
    message=str(error or "").casefold()
    if "read timed out" in message or "read timeout" in message:
        return "Local model timed out while generating this match. Check Ollama, then retry this job."
    if "connection refused" in message or "not reachable" in message or "connectionerror" in message:
        return "Local Ollama is unavailable. Start or check the model service, then retry this job."
    if "malformed" in message or "schema validation" in message:
        return "The model returned invalid structured data. Retry this job once the local model is responsive."
    if "expired" in message or "no longer present" in message:
        return "The posting could not be found in the latest source feed. Refresh posting availability before matching."
    if message:
        return "Matching failed. Review the source and model status, then retry this job."
    return "Matching failed. Retry this job after checking the local model."


def _posting_freshness(item, now=None, source_status=""):
    """Conservative saved-posting status; network failure never means expired."""
    now=now or datetime.now(timezone.utc)
    availability=str(item.get("availability_status") or "unknown").casefold()
    verified=_parse_utc(item.get("last_verified_at"))
    age_days=(now-verified).total_seconds()/86400 if verified else None
    if availability in {"expired","closed","removed"}:
        return "expired_or_closed"
    if not item.get("active") and availability in {"expired","closed","removed"}:
        return "expired_or_closed"
    if availability in {"unavailable","source_unavailable","network_failure"} or source_status in {
        "temporarily_unavailable","network_timeout","rate_limited","access_restricted","robots_disallowed",
        "authentication_required","permanently_unavailable"
    }:
        return "source_unavailable"
    if availability=="current" and item.get("active") and verified and age_days is not None and 0<=age_days<=30:
        return "currently_verified"
    if verified or item.get("imported_source_url"):
        return "previously_verified_recheck_required"
    return "unverified"


def _saved_job_shortlist(limit=10):
    """Build a read-only, deterministic shortlist from saved jobs and current evidence."""
    limit=max(1,min(int(limit),10))
    prefs=preferences() or {}
    with database() as db:
        rows=db.execute("""SELECT company,title,location,url,job_key,discovered_at,employment_type,location_status,
            location_reason,search_prompt,source_platform,source_id,country,work_arrangement,posting_date,
            last_verified_at,active,job_url,career_category,experience_level,salary_json,description,
            content_fingerprint,graduation_windows_json,availability_status,imported_source_url,
            import_eligibility_status,import_review_json FROM discovered_openings WHERE user_id=?""",(USER,)).fetchall()
        sources=db.execute("SELECT source_type,source_token,connector_status,discovery_status,health_reason,last_attempt_at FROM employers").fetchall()
        paused_job_keys=set()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_match_sessions'").fetchone():
            for paused in db.execute("SELECT job_keys_json FROM job_match_sessions WHERE user_id=? AND status='paused'",(USER,)):
                try:paused_job_keys.update(str(key) for key in json.loads(paused["job_keys_json"] or "[]"))
                except (TypeError,json.JSONDecodeError):continue
    sources_by_key={(str(s["source_type"] or "").casefold(),str(s["source_token"] or "").casefold()):dict(s) for s in sources}
    jobs=[dict(row) for row in rows]
    matches={};matcher=None;profile_status="unverified";profile={};confirmed_facts={}
    if jobs:
        try:
            import resume_job_matcher
            matcher=resume_job_matcher
            active_resume,profile_status,profile=resume_job_matcher.active_candidate()
            if resume_job_matcher._profile_is_verified(profile_status):
                confirmed_facts=resume_job_matcher.canonical_verified_candidate_facts(profile)
            matched=resume_job_matcher._match_rows([j["job_key"] for j in jobs],active_resume)
            matches={str(m.get("job_key")):m for m in matched}
        except (ImportError,sqlite3.Error,KeyError,TypeError,ValueError,HTTPException):
            matches={}
    now=datetime.now(timezone.utc)
    candidates=[];confirmed_ineligible=[];preference_conflicts=[]
    counts={"currently_verified":0,"previously_verified_recheck_required":0,"expired_or_closed":0,"source_unavailable":0,"unverified":0}
    for item in jobs:
        key=str(item.get("job_key") or "")
        try: provider,token,_=key.split(":",2)
        except ValueError: provider,token=str(item.get("source_platform") or ""),""
        source=sources_by_key.get((provider.casefold(),token.casefold()),{})
        freshness=_posting_freshness(item,now,source.get("connector_status", "") if source.get("discovery_status")=="verified" else "")
        counts[freshness]=counts.get(freshness,0)+1
        match=matches.get(key)
        if not match:
            match_status="never_analyzed"; match_freshness="never_analyzed"
        elif match.get("status")=="queued":
            match_status="queued"; match_freshness="queued"
        elif match.get("status")=="excluded":
            match_status="excluded_before_ai"; match_freshness="excluded_before_ai"
        elif match.get("status")!="completed":
            match_status=str(match.get("status") or "failed"); match_freshness="failed" if match_status in {"failed","error"} else match_status
        elif match.get("freshness_status") in {None,"","legacy"}:
            match_status=str(match.get("classification") or "needs_review"); match_freshness="legacy_unknown"
        elif match.get("stale") or match.get("freshness_status")=="stale":
            match_status=str(match.get("classification") or "needs_review"); match_freshness="stale"
        else:
            match_status=str(match.get("classification") or "needs_review"); match_freshness="current"
        item["posting_freshness"]=freshness
        item["match_freshness"]=match_freshness
        item["matching_status"]=match_status
        item["match_score"]=match.get("match_score") if match_freshness=="current" else None
        generic_employer_label=bool(re.search(r"\b(careers? page|job board|company careers|unknown employer)\b",str(item.get("company") or ""),re.I))
        source_verified=source.get("connector_status")=="healthy" and source.get("discovery_status")=="verified"
        item["employer_identity_status"]=("needs_review" if generic_employer_label or not str(item.get("company") or "").strip()
            else "verified" if source_verified or str(item.get("source_platform") or "").casefold() in {"greenhouse","ashby","lever"}
            else "needs_review")
        item["eligibility_status"]=(match_status if match_freshness=="current" else
            str(item.get("import_eligibility_status") or "needs_review"))
        item["description_available"]=len(str(item.get("description") or "").strip())>=240
        item["role_level"],item["experience_level_reasons"]=classify_role_level(
            item.get("title"),item.get("description"),item.get("employment_type"))
        item["experience_level_evidence"]=item["role_level"]
        item["experience_requirements"]=extract_experience_requirements(item.get("description"))
        item["graduation_requirements"]=extract_graduation_windows(item.get("description"))
        item["sponsorship_requirement"]=extract_sponsorship_requirement(item.get("description"))
        item["location"]=item.get("location") or "Location not listed"
        item["work_arrangement"]=item.get("work_arrangement") or "Unknown"
        item["employment_type"]=item.get("employment_type") or "Unknown"
        item["missing_requirements"]=(match.get("match") or {}).get("missing_information",[]) if match_freshness=="current" else []
        match_detail=match.get("match") or {} if match else {}
        mandatory=[];preferred=[];confirmed=[];conflicts=[];uncertain=[]
        if match_freshness=="current" and isinstance(match_detail,dict):
            for field in ("required_skills","required_experience","education_requirements"):
                for evidence in match_detail.get(field) or []:
                    if not isinstance(evidence,dict):continue
                    entry={"requirement":str(evidence.get("requirement") or ""),
                           "status":str(evidence.get("status") or "unclear"),
                           "candidate_evidence":list(evidence.get("candidate_evidence") or [])}
                    mandatory.append(entry)
                    if entry["status"]=="met":confirmed.append(entry)
                    elif entry["status"]=="confirmed_mismatch":conflicts.append(entry)
                    elif entry["status"] in {"gap","unclear"}:uncertain.append(entry)
            for field in ("preferred_skills","preferred_experience","preferred_education_requirements"):
                for evidence in match_detail.get(field) or []:
                    if isinstance(evidence,dict):preferred.append({"requirement":str(evidence.get("requirement") or ""),
                        "status":str(evidence.get("status") or "preferred"),"candidate_evidence":list(evidence.get("candidate_evidence") or [])})
        unresolved_year_requirements=[str(evidence.get("requirement") or "") for evidence in
            (match_detail.get("required_experience") or []) if isinstance(evidence,dict) and
            evidence.get("status") in {"gap","unclear"} and re.search(r"\b\d+(?:\s*[-–]\s*\d+)?\+?\s+years?\b",str(evidence.get("requirement") or ""),re.I)]
        item["match_error"]=_public_match_error(match.get("error")) if match and match_freshness=="failed" else ""
        target_location_status,target_location_reason=evaluate_location(item,prefs.get("location", ""))
        relocation=evaluate_relocation_preference(item,prefs)
        effective_location=relocation or (target_location_status,target_location_reason)
        mandatory_conflict=(matcher._pre_ai_mandatory_conflict(item,profile,profile_status)
                            if matcher and profile else None)
        assessment_ok,assessment_reason,assessment_notes=assess_job(
            item,prefs,str(item.get("search_prompt") or ""))
        relevant,relevance_reason,relevance_evidence=relevance_for_job(item,prefs)
        relevance_basis=str((relevance_evidence or {}).get("basis") or "")
        role_review=bool(relevant and relevance_basis!="title_alias")
        level=item["role_level"]
        early_preference=any(str(value).casefold() in {"new graduate","entry level","junior","associate","early career"}
                             for value in prefs.get("levels",[]) if isinstance(value,str))
        experience_evidence_missing=any("confirmed professional experience is unavailable" in str(note).casefold()
                                        for note in assessment_notes or [])
        seniority_preference_conflict=(early_preference and level in {"Senior","Staff/Principal","Mid-Level"}
                                       and not experience_evidence_missing)
        role_seniority_missing=any(re.search(r"experience level.{0,60}(?:not clear|unclear|not established)",
            str(value),re.I) for value in (match_detail.get("missing_information") or []))
        preference_conflict=(not assessment_ok and assessment_reason in {
            "location","experience_level","employment_type","work_arrangement","instructions"})
        if experience_evidence_missing and assessment_reason=="experience_level":
            preference_conflict=False
        preference_conflict=preference_conflict or seniority_preference_conflict
        if seniority_preference_conflict and not assessment_reason:assessment_reason="experience_level"
        if item.get("employment_type")!="Unknown":
            employment_status="compatible" if not prefs.get("employment_types") or any(
                _employment_type_matches(value,item["employment_type"])
                for value in prefs.get("employment_types",[])) else "mismatch"
        else:
            employment_status="unknown" if prefs.get("employment_types") else "not_configured"
        selected_arrangement=_arrangement(str(prefs.get("work_arrangement") or ""))
        observed_arrangement=_arrangement(str(item.get("work_arrangement") or ""))
        arrangement_status=("not_configured" if not selected_arrangement else "unknown" if not observed_arrangement else
            "compatible" if selected_arrangement==observed_arrangement else "mismatch")
        country=infer_country_from_location(item.get("location"),item.get("country"))
        if country:
            auth_confirmed=bool(matcher and profile and matcher._profile_is_verified(profile_status) and
                matcher._confirmed_work_authorization_for_country(confirmed_facts,country))
            authorization_status="confirmed_for_country" if auth_confirmed else "needs_applicant_confirmation"
        else:
            authorization_status="country_not_stated"
        experience_evidence=[]
        for requirement in item["experience_requirements"]["required"]:
            if matcher and profile and matcher._profile_is_verified(profile_status):
                requirement_status,requirement_evidence=matcher._experience_requirement_status(
                    requirement["requirement"],confirmed_facts,item)
            else:requirement_status,requirement_evidence="unclear",[]
            experience_evidence.append({"requirement":requirement["requirement"],"status":requirement_status,
                "candidate_evidence":requirement_evidence})
        for requirement in item["experience_requirements"]["preferred"]:
            experience_evidence.append({"requirement":requirement["requirement"],"status":"preferred","candidate_evidence":[]})
        item["experience_requirement_evidence"]=experience_evidence
        sponsorship_status=item["sponsorship_requirement"]["status"]
        if sponsorship_status=="not_available":
            sponsor_facts=[confirmed_facts.get(key) for key in (
                "sponsorship_required_now","sponsorship_required_future","requires_sponsorship")]
            sponsor_facts=[str(value).strip().casefold() for value in sponsor_facts if value not in (None,"",[],{})]
            if any(value in {"yes","true","required","1"} for value in sponsor_facts):
                item["sponsorship_compatibility"]="confirmed_conflict"
                mandatory_conflict=mandatory_conflict or "mandatory: employer states sponsorship is unavailable, but confirmed applicant facts indicate sponsorship is required"
            elif sponsor_facts and all(value in {"no","false","not required","0"} for value in sponsor_facts):
                item["sponsorship_compatibility"]="confirmed_compatible"
            else:item["sponsorship_compatibility"]="needs_applicant_confirmation"
        else:item["sponsorship_compatibility"]="not_stated"
        employment_unknown=item["employment_type"]=="Unknown"
        arrangement_unknown=arrangement_status=="unknown"
        role_level_unknown=level=="Unknown"
        education_requirements=extract_education_requirements(item.get("description"))
        education_evidence=[]
        for requirement in education_requirements["required"]:
            state,evidence=(matcher._degree_requirement_status(requirement,confirmed_facts)
                if matcher and profile and matcher._profile_is_verified(profile_status) else ("unclear",[]))
            education_evidence.append({"requirement":requirement,"status":state,"candidate_evidence":evidence})
        gpa_requirements=extract_gpa_requirements(item.get("description"))
        gpa_evidence=[]
        for requirement in gpa_requirements["required"]:
            state,evidence=(matcher._gpa_requirement_status(requirement,confirmed_facts)
                if matcher and profile and matcher._profile_is_verified(profile_status) else ("unclear",[]))
            gpa_evidence.append({"requirement":requirement,"status":state,"candidate_evidence":evidence})
        graduation_evidence=[]
        graduation_dates=list(dict.fromkeys([str(confirmed_facts.get("graduation_date") or ""),
            *[str(row.get("graduation_date") or "") for row in confirmed_facts.get("education",[]) if isinstance(row,dict)]]))
        graduation_dates=[value for value in graduation_dates if re.fullmatch(r"20\d{2}-(?:0[1-9]|1[0-2])",value)]
        for requirement in item["graduation_requirements"]:
            if requirement.get("status") not in {"required","unclear"}:continue
            states=[matcher._graduation_window_matches(requirement,date) for date in graduation_dates] if matcher else []
            state=("met" if True in states else "confirmed_mismatch" if requirement.get("status")=="required" and states and all(value is False for value in states)
                else "unclear")
            graduation_evidence.append({"requirement":requirement.get("requirement"),"status":state,
                "candidate_evidence":["Confirmed graduation date matches the posting window."] if state=="met" else []})
        if item["sponsorship_requirement"]["status"]=="not_available" and item["sponsorship_compatibility"]=="needs_applicant_confirmation":
            item["missing_requirements"]=list(dict.fromkeys([*item["missing_requirements"],
                "Employer does not offer sponsorship; confirm whether sponsorship will be required."]))
        if role_level_unknown:
            item["missing_requirements"]=list(dict.fromkeys([*item["missing_requirements"],*item["experience_level_reasons"]]))
        if country and authorization_status!="confirmed_for_country":
            item["missing_requirements"]=list(dict.fromkeys([*item["missing_requirements"],
                f"Work authorization for {country.replace('_',' ').title()} is not confirmed."]))
        elif not country:
            item["missing_requirements"]=list(dict.fromkeys([*item["missing_requirements"],
                "The employer has not identified the eligible country for remote work."]))
        if match_freshness=="current" and unresolved_year_requirements:
            item["missing_requirements"]=list(dict.fromkeys([*item["missing_requirements"],
                *["Confirm relevant professional experience for the stated minimum: "+value
                  for value in unresolved_year_requirements]]))
        for evidence in experience_evidence+education_evidence+gpa_evidence+graduation_evidence:
            if evidence.get("status")=="unclear":
                requirement=str(evidence.get("requirement") or "").strip()
                if requirement:item["missing_requirements"]=list(dict.fromkeys([*item["missing_requirements"],
                    "Confirm evidence for mandatory qualification: "+requirement]))
            if evidence.get("status")=="confirmed_mismatch":
                mandatory_conflict=mandatory_conflict or ("mandatory: verified candidate evidence conflicts with "+str(evidence.get("requirement") or "a required qualification"))
        item["qualification_evidence"]={"mandatory":mandatory,"confirmed_matches":confirmed,
            "confirmed_conflicts":conflicts,"needs_review":uncertain,"preferred":preferred,
            "experience_requirements":experience_evidence,"education_requirements":education_evidence,
            "gpa_requirements":gpa_evidence,"graduation_requirements":graduation_evidence,
            "internship_requirements":extract_internship_requirements(item.get("description")),
            "sponsorship_requirement":item["sponsorship_requirement"],
            "missing_information":list(dict.fromkeys([*item["missing_requirements"],
                *((match_detail.get("missing_information") or []) if match_freshness=="current" else [])])),
            "employer_metadata_missing":[label for label,value in (("employment type",item.get("employment_type")),
                ("work arrangement",item.get("work_arrangement")),("country for remote work",country),
                ("role level",item.get("role_level"))) if str(value or "").strip().casefold() in {"","unknown","not listed","n/a"}]}
        item["candidate_alignment"]={
            "role_relevance_status":("title_aligned" if relevant and not role_review else
                "related_or_ambiguous_needs_review" if relevant else "unrelated"),
            "role_relevance_reason":relevance_reason or "Saved role preference matches the posting title.",
            "target_location_status":target_location_status,"target_location_reason":target_location_reason,
            "relocation_status":relocation[0] if relocation else "not_applicable",
            "effective_location_status":effective_location[0],"effective_location_reason":effective_location[1],
            "work_authorization_status":authorization_status,"country":country or None,
            "sponsorship_status":sponsorship_status,
            "sponsorship_compatibility":item["sponsorship_compatibility"],
            "employment_type_status":employment_status,"work_arrangement_status":arrangement_status,
            "experience_level_status":("early_career_evidence" if level in {"New Graduate","University Graduate","Entry Level","Junior","Associate","Software Engineer I"}
                else "senior_evidence" if level in {"Senior","Staff/Principal"}
                else "mid_level_evidence" if level=="Mid-Level" else "needs_review"),
            "experience_review_reasons":[str(note) for note in (assessment_notes or [])
                if "experience" in str(note).casefold()],
            "preference_review_reasons":list(assessment_notes or []),
            "mandatory_conflict":mandatory_conflict,
        }
        item["qualification_evidence"]["candidate_alignment"]=item["candidate_alignment"]
        if mandatory_conflict and match_freshness!="current":
            item["eligibility_status"]="not_eligible"
            item["readiness_status"]="confirmed_ineligible"
        elif (match_freshness=="current" and match_status=="not_eligible") or mandatory_conflict:
            item["readiness_status"]="confirmed_ineligible"
        elif freshness!="currently_verified" or item["employer_identity_status"]!="verified":
            item["readiness_status"]="posting_verification_required"
        elif early_preference and level in {"Mid-Level","Senior","Staff/Principal"}:
            item["readiness_status"]="preference_conflict"
        elif match_freshness=="current" and role_seniority_missing and level not in {"New Graduate","University Graduate","Entry Level","Junior","Associate","Software Engineer I","Internship"}:
            item["readiness_status"]="needs_applicant_review"
        elif match_freshness=="current" and (not country or (country and authorization_status!="confirmed_for_country")
                or employment_unknown
                or (selected_arrangement and arrangement_unknown)
                or item["sponsorship_compatibility"]=="needs_applicant_confirmation"):
            item["readiness_status"]="needs_applicant_review"
        elif match_freshness=="current":
            item["readiness_status"]="current_match"
        elif match_freshness=="failed":
            item["readiness_status"]="failed_analysis"
        elif item["job_key"] in paused_job_keys:
            item["readiness_status"]="paused_checkpoint_preserved"
        elif match_freshness=="queued":
            item["readiness_status"]="matching_queued"
        elif preference_conflict:
            item["readiness_status"]="preference_conflict"
        elif role_review:
            item["readiness_status"]="needs_applicant_review"
        elif experience_evidence_missing:
            item["readiness_status"]="needs_applicant_review"
        elif role_level_unknown:
            item["readiness_status"]="needs_applicant_review"
        elif item["sponsorship_compatibility"]=="needs_applicant_confirmation":
            item["readiness_status"]="needs_applicant_review"
        elif country and authorization_status!="confirmed_for_country":
            item["readiness_status"]="needs_applicant_review"
        elif not country:
            item["readiness_status"]="needs_applicant_review"
        elif employment_unknown or arrangement_unknown:
            item["readiness_status"]="needs_applicant_review"
        elif not item["description_available"]:
            item["readiness_status"]="description_incomplete"
        elif match_freshness in {"never_analyzed","stale","legacy_unknown"}:
            item["readiness_status"]="ready_for_matching"
        else:
            item["readiness_status"]="needs_review"
        item["next_action"]=("Do not prepare; confirmed mandatory mismatch" if item["readiness_status"]=="confirmed_ineligible" else
            "Review saved preference conflict" if item["readiness_status"]=="preference_conflict" else
            "Review whether this job family matches your target roles" if role_review and match_freshness!="current" else
            "Retry this failed match" if match_freshness=="failed" else
            "Review role seniority; the posting does not establish early-career suitability" if role_seniority_missing and match_freshness=="current" else
            "Confirm experience for the stated minimum" if experience_evidence_missing and match_freshness!="current" else
            "Verify role level against the posting requirements" if role_level_unknown else
            "Confirm whether employer sponsorship will be required" if item["sponsorship_compatibility"]=="needs_applicant_confirmation" else
            "Confirm remote hiring country with the employer" if not country else
            "Confirm country-specific work authorization" if authorization_status=="needs_applicant_confirmation" else
            "Verify employment type and work arrangement with the employer" if employment_unknown or arrangement_unknown else
            "Confirm professional experience for the stated minimum" if unresolved_year_requirements or experience_evidence_missing else
            "Review mandatory qualification evidence" if any(str(value).startswith("Confirm evidence for mandatory qualification:") for value in item["missing_requirements"]) else
            "Review eligibility" if item["eligibility_status"]=="needs_review" and match_freshness=="current" else
            "Not eligible" if item["eligibility_status"]=="not_eligible" and match_freshness=="current" else
            "Confirm employer identity" if item["employer_identity_status"]!="verified" else
            "Recheck posting" if freshness!="currently_verified" else
            "Add or enrich description" if not item["description_available"] else
            "Queued in an existing matching session" if match_freshness=="queued" else
            "Retry this failed match" if match_freshness=="failed" else
            "Review preserved paused checkpoint" if item["readiness_status"]=="paused_checkpoint_preserved" else
            "Ready for matching" if item["readiness_status"]=="ready_for_matching" else "Review result")
        if preference_conflict:
            preference_conflicts.append({"company":item.get("company"),"title":item.get("title"),
                "location":item.get("location"),"reason":assessment_reason or effective_location[1],
                "next_action":"Review saved career preferences before treating this opening as a candidate-aligned opportunity."})
        title=str(item.get("title") or "")
        # Keep a broad software title eligible, but descriptions alone cannot
        # turn clearly unrelated operations/support roles into opportunities.
        relevant,_,_=relevance_for_job(item,prefs)
        level=item["experience_level_evidence"]
        early=level in {"New Graduate","University Graduate","Entry Level","Junior","Associate","Software Engineer I"}
        senior=level in {"Senior","Staff/Principal"}
        mid=level=="Mid-Level"
        exclude=(not relevant or senior or (mid and early_preference and not experience_evidence_missing) or freshness=="expired_or_closed" or preference_conflict or mandatory_conflict or
                 (match_freshness=="current" and match_status=="not_eligible"))
        item["shortlist_eligible"]=not exclude
        if freshness!="currently_verified" or item["employer_identity_status"]!="verified":
            item["opportunity_group"]="awaiting_posting_verification"
        elif item["readiness_status"]=="confirmed_ineligible":
            item["opportunity_group"]="confirmed_ineligible"
        elif level=="Unknown" or not country:
            item["opportunity_group"]="level_or_location_uncertain"
        elif early and match_freshness=="current" and match_status=="potential_match" and not item["missing_requirements"] and not mandatory_conflict:
            item["opportunity_group"]="strong_early_career"
        elif early:
            item["opportunity_group"]="promising_needs_review"
        else:
            item["opportunity_group"]="level_or_location_uncertain"
        if (match_freshness=="current" and match_status=="not_eligible") or mandatory_conflict:
            reasons=[str(evidence.get("requirement") or "Confirmed mandatory conflict")
                for field in ("required_skills","required_experience","education_requirements")
                for evidence in (match_detail.get(field) or [])
                if isinstance(evidence,dict) and evidence.get("status")=="confirmed_mismatch"]
            if mandatory_conflict:reasons.append(mandatory_conflict.removeprefix("mandatory:").strip())
            confirmed_ineligible.append({"company":item.get("company"),"title":item.get("title"),
                "location":item.get("location") or "Location not listed","reasons":reasons[:8],
                "next_action":"Do not prepare; verified evidence conflicts with a mandatory qualification."})
        target_location_bonus=35 if target_location_status=="eligible" else (-15 if target_location_status=="mismatch" else 0)
        employment_bonus=5 if employment_status=="compatible" else (-3 if employment_status=="unknown" else 0)
        arrangement_bonus=4 if arrangement_status=="compatible" else (-2 if arrangement_status=="unknown" else 0)
        item["priority_score"]=(
            (100 if freshness=="currently_verified" else 0)+(50 if early else 0)+target_location_bonus+
            (15 if item["description_available"] else 0)+(12 if match_freshness=="current" else 0)+
            (10 if source.get("connector_status")=="healthy" and source.get("discovery_status")=="verified" else 0)+
            employment_bonus+arrangement_bonus-(10 if authorization_status=="needs_applicant_confirmation" else 0)-
            (15 if role_review else 0)-
            (20 if level in {"Unknown","Mid-Level"} else 0)+
            (25 if item["readiness_status"]=="ready_for_matching" else
             20 if item["readiness_status"]=="current_match" and item["eligibility_status"]=="potential_match" else
             -5 if item["readiness_status"]=="needs_applicant_review" else
             -20 if item["readiness_status"]=="failed_analysis" else
             -30 if item["readiness_status"]=="paused_checkpoint_preserved" else 0))
        item["source_reliability"]=("Verified employer feed" if source.get("connector_status")=="healthy" and source.get("discovery_status")=="verified"
            else "Official ATS provenance" if str(item.get("source_platform") or "").casefold() in {"greenhouse","ashby","lever"} and item.get("imported_source_url")
            else "Source not currently verified")
        candidates.append(item)
    candidates=[j for j in candidates if j["shortlist_eligible"]]
    candidates.sort(key=lambda j:(-int(j["priority_score"]),str(j.get("company") or "").casefold(),str(j.get("title") or "").casefold()))
    selected=[]; seen_urls={}; duplicate_count=0
    for item in candidates:
        canonical=canonicalize_job_url(item.get("url") or "")
        if canonical and canonical in seen_urls:
            duplicate_count+=1
            continue
        if canonical:seen_urls[canonical]=item.get("job_key")
        selected.append(item)
        if len(selected)>=limit:break
    ready=[j["job_key"] for j in selected if j["readiness_status"]=="ready_for_matching" and
           j["posting_freshness"]=="currently_verified" and j["employer_identity_status"]=="verified" and
           j["description_available"] and j["match_freshness"] in {"never_analyzed","stale","legacy_unknown"} and
           j["role_level"] in {"New Graduate","University Graduate","Entry Level","Junior","Associate","Software Engineer I"} and
           j["candidate_alignment"].get("work_authorization_status")=="confirmed_for_country" and
           j["candidate_alignment"].get("sponsorship_compatibility")!="needs_applicant_confirmation" and
           j["job_key"] not in paused_job_keys]
    with database() as db:
        row=db.execute("SELECT settings_json FROM application_authorizations WHERE user_id=?",(USER,)).fetchone() if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='application_authorizations'").fetchone() else None
    try: matching_allowed=bool(json.loads(row["settings_json"] or "{}").get("allow_ai_matching",False)) if row else False
    except (TypeError,json.JSONDecodeError): matching_allowed=False
    match_counts={"current":0,"stale":0,"legacy_unknown":0,"never_analyzed":0,"failed":0,"excluded_before_ai":0,"queued":0,"processing":0}
    for item in jobs:
        match=matches.get(str(item.get("job_key")))
        if not match: status="never_analyzed"
        elif match.get("status") in {"failed","error"}: status="failed"
        elif match.get("status")=="excluded": status="excluded_before_ai"
        elif match.get("status")=="queued": status="queued"
        elif match.get("status")!="completed": status="processing"
        elif match.get("freshness_status") in {None,"","legacy"}: status="legacy_unknown"
        elif match.get("stale") or match.get("freshness_status")=="stale": status="stale"
        else: status="current"
        match_counts[status]+=1
    readiness_counts={}
    for item in jobs:
        state=item.get("readiness_status","needs_review")
        readiness_counts[state]=readiness_counts.get(state,0)+1
    opportunity_counts={"strong_early_career":0,"promising_needs_review":0,
        "level_or_location_uncertain":0,"confirmed_ineligible":len(confirmed_ineligible),
        "awaiting_posting_verification":0}
    for item in selected:
        state=item.get("opportunity_group")
        if state in opportunity_counts:opportunity_counts[state]+=1
    return {"results":selected,"confirmed_ineligible":confirmed_ineligible[:10],
            "preference_conflicts":preference_conflicts[:10],"readiness_counts":readiness_counts,
            "opportunity_counts":opportunity_counts,
            "count":len(selected),"saved_count":len(jobs),"duplicate_postings_excluded":duplicate_count,"freshness_counts":counts,
            "match_freshness_counts":match_counts,
            "never_analyzed_count":match_counts["never_analyzed"],"ready_job_keys":ready[:5],"ready_count":min(len(ready),5),
            "matching_authorized":matching_allowed,
            "ranking_note":"Priority order is a discovery aid only; it does not establish eligibility or application readiness."}


@router.get("/api/jobs/shortlist")
def saved_job_shortlist(limit: int = 10):
    return _saved_job_shortlist(limit)


def _verified_registered_employer(domain: str) -> str:
    """Resolve only domains already tied to a healthy, verified local record."""
    host=(urlparse(domain if "://" in domain else "https://"+domain).hostname or "").casefold().rstrip(".")
    if not host:
        return ""
    matches=set()
    with database() as db:
        rows=db.execute("SELECT * FROM employers").fetchall()
    for row in rows:
        if not row["active"] or row["connector_status"]!="healthy" or row["discovery_status"]!="verified" or not row["last_success_at"]:
            continue
        record=dict(row)
        if record.get("source_fingerprint")!=_source_fingerprint(record):
            continue
        trusted_hosts={urlparse(record.get(key) or "").hostname.casefold().rstrip(".")
                       for key in ("careers_url","website_url") if urlparse(record.get(key) or "").hostname}
        if host in trusted_hosts:
            matches.add(str(row["name"]).strip())
    return next(iter(matches)) if len(matches)==1 else ""


@router.post("/api/jobs/import-url")
def import_job_url(payload: JobUrlImportRequest, request: Request):
    """Import one public posting into the existing saved-opening/matching flow."""
    client_host = getattr(getattr(request, "client", None), "host", None)
    try:
        if not client_host or not ipaddress.ip_address(client_host).is_loopback:
            raise HTTPException(403, "Job URL import is available only from this computer.")
    except ValueError as exc:
        raise HTTPException(403, "Job URL import is available only from this computer.") from exc
    with database() as db:
        table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='application_authorizations'").fetchone()
        authorization = db.execute("SELECT settings_json FROM application_authorizations WHERE user_id=?", (USER,)).fetchone() if table else None
    try:
        allowed = bool(json.loads(authorization["settings_json"] or "{}").get("allow_automatic_discovery", False)) if authorization else False
    except (TypeError, json.JSONDecodeError):
        allowed = False
    if not allowed:
        raise HTTPException(403, "Enable public job discovery in Application settings before importing a company posting.")

    try:
        detail, error = fetch_public_job_url(payload.url)
    except ValueError as exc:
        raise HTTPException(422, f"The job URL could not be imported safely: {exc}") from exc
    if error or not detail:
        raise HTTPException(422, error or "The public posting did not contain usable job details.")

    if not detail.get("active", True):
        raise HTTPException(422, "This job posting is expired and was not saved for matching.")

    company = str(detail.get("employer") or "").strip()
    if not company:
        if detail.get("provider") == "greenhouse":
            with database() as db:
                rows=db.execute("SELECT * FROM employers WHERE source_type='greenhouse' AND source_token=?",(detail.get("source_token"),)).fetchall()
            verified={str(row["name"]).strip() for row in rows if row["active"] and row["connector_status"]=="healthy" and row["discovery_status"]=="verified" and row["last_success_at"] and row["source_fingerprint"]==_source_fingerprint(dict(row))}
            if len(verified)==1:
                company=next(iter(verified))
                detail["employer_identity_source"]="verified_greenhouse_board_record"
        # Resolve identity from the actual page host only. A publisher-controlled
        # hiringOrganization.url field alone is not proof that it owns the post.
        if not company:
            company=_verified_registered_employer(str(detail.get("retrieved_url") or payload.url))
    title = str(detail.get("title") or "").strip()
    if not company:
        raise HTTPException(422, "Employer identity is unavailable: the posting has no hiring-organization name or supported Workday employer metadata, and no verified employer record matches its official domain. Try the employer's official careers page or another public posting URL that exposes the hiring organization; no job was saved.")
    if not title:
        raise HTTPException(422, "The public posting does not identify a job title; it was not saved.")

    provider = str(detail.get("provider") or "public_html")
    source_token = str(detail.get("source_token") or urlparse(detail.get("retrieved_url") or payload.url).hostname or "")
    source_id = str(detail.get("id") or detail.get("url") or "")
    if not source_token or not source_id:
        raise HTTPException(422, "The posting did not provide a stable source identity; it was not saved.")
    job_key = f"{provider}:{source_token}:{source_id}"
    posting_url = canonicalize_job_url(detail.get("url") or payload.url)
    application_url = str(detail.get("application_url") or "")
    description = str(detail.get("description") or "")
    employment_type = str(detail.get("employment_type") or "")
    department = str(detail.get("department") or "")
    location = str(detail.get("location") or "")
    country = str(detail.get("country") or "")
    fingerprint = hashlib.sha256((title.casefold() + "\n" + description).encode()).hexdigest()
    level, level_evidence = classify_experience_level(title, description, employment_type)
    experience = extract_experience_requirements(description)
    education = extract_education_requirements(description)
    graduation_windows = extract_graduation_windows(description)
    qualifications = {"experience": experience, "education": education,
                      "structured": detail.get("required_qualifications")}
    prefs = preferences() or {}
    location_status, location_reason = evaluate_location({"location": location, "country": country, "description": description}, prefs.get("location", ""))
    review_reasons = []
    eligibility = "needs_review"
    try:
        if not (prefs.get("roles") or prefs.get("categories")):
            raise KeyError("No saved role preferences to compare")
        accepted, conflict, review_reasons = assess_job(
            {"title": title, "location": location, "country": country, "content": description,
             "description": description, "employment_type": employment_type},
            prefs, search_terms("", prefs), _confirmed_professional_experience_years({"title":title,"description":description}))
        if not accepted:
            eligibility = "not_eligible"
            review_reasons = [f"Saved preference conflict: {conflict}."]
        elif review_reasons or not usable_posting_description(description):
            eligibility = "needs_review"
        else:
            review_reasons = ["Imported jobs require a fresh matching assessment and explicit eligibility review before application preparation."]
    except (TypeError, ValueError, KeyError):
        review_reasons = ["Eligibility could not be fully assessed from the imported posting; review is required."]

    stamp = str(detail.get("retrieved_at") or datetime.now(timezone.utc).isoformat())
    with database() as db:
        existing = db.execute("""SELECT o.job_key,o.source_platform,o.source_id,o.discovered_at,o.content_fingerprint,o.active,o.availability_status,o.reopened_review_required
            FROM discovered_openings o LEFT JOIN job_source_provenance p ON p.source_job_key=o.job_key
            WHERE o.user_id=? AND p.canonical_url=? LIMIT 1""",
            (USER, posting_url)).fetchone()
        # Older feed imports may lack provenance; compare their saved canonical
        # posting URLs in Python without changing any historical records.
        if not existing:
            for row in db.execute("SELECT job_key,source_platform,source_id,discovered_at,content_fingerprint,job_url FROM discovered_openings WHERE user_id=?", (USER,)).fetchall():
                if row["job_url"] and canonicalize_job_url(row["job_url"]) == posting_url:
                    existing = row
                    break
        if existing:
            job_key = existing["job_key"]
            provider = existing["source_platform"]
            source_id = existing["source_id"]
            changed = bool(existing["content_fingerprint"] and existing["content_fingerprint"] != fingerprint)
            reopened=bool(not existing["active"] and existing["availability_status"] in {"closed","expired","removed"} and detail.get("active",True))
            if changed:
                table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_match_results'").fetchone()
                if table:
                    cols = {row[1] for row in db.execute("PRAGMA table_info(job_match_results)")}
                    if "freshness_status" in cols:
                        db.execute("UPDATE job_match_results SET freshness_status='stale' WHERE user_id=? AND job_key=?", (USER, job_key))
            db.execute("""UPDATE discovered_openings SET company=?,title=?,location=?,url=?,employment_type=?,department=?,country=?,
                 work_arrangement=?,description=?,posting_date=?,last_verified_at=?,active=?,content_fingerprint=?,job_url=?,
                experience_level=?,required_qualifications_json=?,preferred_qualifications_json=?,graduation_windows_json=?,
                 availability_status=?,imported_source_url=?,import_eligibility_status=?,import_review_json=?,
                 reopened_review_required=CASE WHEN ? THEN 1 ELSE reopened_review_required END
                WHERE user_id=? AND job_key=?""",
                (company,title,location,application_url,employment_type,department,country,str(detail.get("work_arrangement") or ""),description,detail.get("date_posted"),stamp,
                 int(detail.get("active", True)),fingerprint,posting_url,level,json.dumps(qualifications),json.dumps(detail.get("preferred_qualifications") or []),
                 json.dumps(graduation_windows),"expired" if not detail.get("active",True) else "current",str(detail.get("source_url") or payload.url),eligibility,json.dumps(review_reasons),int(reopened),USER,job_key))
        else:
            db.execute("""INSERT INTO discovered_openings
                (user_id,job_key,company,title,location,url,discovered_at,employment_type,location_status,location_reason,
                 search_prompt,discovery_source,source_platform,source_id,country,work_arrangement,description,posting_date,
                 last_verified_at,active,content_fingerprint,job_url,department,career_category,experience_level,
                 required_qualifications_json,preferred_qualifications_json,graduation_windows_json,availability_status,imported_source_url,
                 import_eligibility_status,import_review_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (USER,job_key,company,title,location,application_url,stamp,employment_type,location_status,location_reason,
                 "Direct official job URL import",f"Official company posting ({provider})",provider,source_id,country,str(detail.get("work_arrangement") or ""),description,
                 detail.get("date_posted"),stamp,int(detail.get("active", True)),fingerprint,posting_url,department,
                 classify_career_category(title,prefs),level,json.dumps(qualifications),json.dumps(detail.get("preferred_qualifications") or []),
                 json.dumps(graduation_windows),"expired" if not detail.get("active",True) else "current",str(detail.get("source_url") or payload.url),
                 eligibility,json.dumps(review_reasons)))
        db.execute("""INSERT INTO job_source_provenance(source_platform,source_job_key,canonical_url,content_fingerprint,first_seen_at,last_seen_at)
            VALUES(?,?,?,?,?,?) ON CONFLICT(source_platform,source_job_key) DO UPDATE SET
            canonical_url=excluded.canonical_url,content_fingerprint=excluded.content_fingerprint,last_seen_at=excluded.last_seen_at""",
            (provider,job_key,posting_url,fingerprint,existing["discovered_at"] if existing else stamp,stamp))

    return {"job_key":job_key,"provider":provider,"employer":company,
        "employer_identity_source":detail.get("employer_identity_source") or ("verified_employer_record" if not detail.get("employer") else "provider_posting_metadata"),"title":title,"location":location,
        "employment_type":employment_type,"department":department,"posting_url":posting_url,"application_url":application_url,
        "application_url_source":detail.get("application_url_source") or "structured_application_url",
        "description_available":usable_posting_description(description),"description_length":len(description),
        "experience_level":level,"experience_level_evidence":level_evidence,"experience_requirements":experience,
        "education_requirements":education,"graduation_windows":graduation_windows,"availability_status":"expired" if not detail.get("active",True) else "current","eligibility_classification":eligibility,
        "review_reasons":review_reasons,"duplicate":bool(existing),"saved":True,"needs_matching":True,
        "application_preparation_allowed":False,"submission_enabled":False}


@router.get("/api/employers")
def list_employers(request: Request):
    _require_local_admin(request)
    current=datetime.now(timezone.utc)
    with database() as db:
        rows = db.execute("""SELECT employer_id,name,careers_url,source_type,source_token,public_feed_url,geographic_coverage,
            last_success_at,last_attempt_at,connector_status,error_details,verification_evidence_json,active,
            health_reason,consecutive_failures,next_retry_at,source_fingerprint,force_revalidate,
            website_url,evidence_url,candidate_source,candidate_record_url,candidate_license,discovered_at,discovery_status
            FROM employers ORDER BY name COLLATE NOCASE""").fetchall()
        openings=[dict(row) for row in db.execute("""SELECT company,source_platform,source_id,job_key,title,description,
            employment_type,active,last_verified_at,posting_date FROM discovered_openings WHERE user_id=?""",(USER,)).fetchall()]
    eligible_sources=[row for row in rows if row["active"] and row["connector_status"]=="healthy" and
        row["discovery_status"]=="verified" and row["last_success_at"] and
        row["source_fingerprint"]==_source_fingerprint(dict(row))]
    cutoff=(current-timedelta(days=30)).isoformat()
    opening_counts={};current_counts={};early_counts={};university_counts={};missing_description_counts={}
    source_current_counts={};source_early_counts={};source_university_counts={};source_missing_counts={}
    for opening in openings:
        company=(opening["company"] or "").casefold()
        opening_counts[company]=opening_counts.get(company,0)+1
        matching_source=next((row for row in eligible_sources if row["source_type"]==opening["source_platform"] and
            row["name"].casefold()==company and opening["job_key"]==f"{row['source_type']}:{row['source_token']}:{opening['source_id']}"),None)
        verified=bool(opening["active"] and opening["last_verified_at"] and opening["last_verified_at"]>=cutoff and matching_source)
        if not verified:continue
        current_counts[company]=current_counts.get(company,0)+1
        level=classify_role_level(opening["title"],opening["description"],opening["employment_type"])[0]
        source_id=matching_source["employer_id"]
        source_current_counts[source_id]=source_current_counts.get(source_id,0)+1
        if level in {"New Graduate","University Graduate","Entry Level","Junior","Associate","Software Engineer I"}:
            early_counts[company]=early_counts.get(company,0)+1
            source_early_counts[source_id]=source_early_counts.get(source_id,0)+1
        if level in {"New Graduate","University Graduate"}:
            university_counts[company]=university_counts.get(company,0)+1
            source_university_counts[source_id]=source_university_counts.get(source_id,0)+1
        if not usable_posting_description(opening["description"]):
            missing_description_counts[company]=missing_description_counts.get(company,0)+1
            source_missing_counts[source_id]=source_missing_counts.get(source_id,0)+1
    grouped={}
    for row in rows:
        item=dict(row)
        if item["connector_status"] == "ready" and not item["last_success_at"]:
            item["connector_status"] = "needs_verification"
        key=re.sub(r"[^a-z0-9]+"," ",item["name"].casefold()).strip()
        group=grouped.setdefault(key,{"name":item["name"],"careers_url":item["careers_url"],
            "geographic_coverage":item["geographic_coverage"],"website_url":item["website_url"],
            "evidence_url":item["evidence_url"],"active":False,"sources":[]})
        company_key=item["name"].casefold()
        group["saved_openings_count"]=opening_counts.get(company_key,0)
        group["current_openings_count"]=current_counts.get(company_key,0)
        group["early_career_current_openings_count"]=early_counts.get(company_key,0)
        group["university_hiring_evidence_count"]=university_counts.get(company_key,0)
        group["current_missing_descriptions_count"]=missing_description_counts.get(company_key,0)
        group["active"]=group["active"] or bool(item["active"])
        if not group["careers_url"]:group["careers_url"]=item["careers_url"]
        try: evidence=json.loads(item["verification_evidence_json"] or "{}")
        except (TypeError,json.JSONDecodeError): evidence={}
        group["sources"].append({"employer_id":item["employer_id"],**{field:item[field] for field in ("source_type","source_token","public_feed_url","last_success_at","last_attempt_at","connector_status","error_details","active","candidate_source","candidate_record_url","candidate_license","discovered_at","discovery_status","health_reason","consecutive_failures","next_retry_at","force_revalidate")},
                                 "current_verified_openings_count":source_current_counts.get(item["employer_id"],0),
                                 "early_career_current_openings_count":source_early_counts.get(item["employer_id"],0),
                                 "university_hiring_evidence_count":source_university_counts.get(item["employer_id"],0),
                                 "current_missing_descriptions_count":source_missing_counts.get(item["employer_id"],0),
                                 "verification_evidence":evidence})
    with database() as db:
        for employer in grouped.values():
            for source in employer["sources"]:
                stages=db.execute("SELECT stage_key,status,diagnostics_json,updated_at FROM employer_discovery_stages WHERE employer_id=? ORDER BY stage_key",(source["employer_id"],)).fetchall()
                source["stages"]=[{**dict(stage),"diagnostics":json.loads(stage["diagnostics_json"] or "{}")} for stage in stages]
    unsupported=sum("unsupported" in str(row["connector_status"]).casefold() for row in rows)
    awaiting=sum(row["connector_status"] in {"ready","needs_verification"} or
        row["discovery_status"] in {"needs_verification","registered","candidate"} for row in rows)
    scheduled=sum(bool(row["next_retry_at"] and row["next_retry_at"]>current.isoformat()) for row in rows)
    recent_cutoff=(current-timedelta(days=30)).isoformat()
    recent_sources=sum(bool(row["discovered_at"] and row["discovered_at"]>=recent_cutoff) for row in rows)
    failure_reasons={}
    for row in rows:
        reason=str(row["health_reason"] or row["connector_status"] or "unknown")
        if reason not in {"valid_with_postings","valid_zero_postings"}:
            failure_reasons[reason]=failure_reasons.get(reason,0)+1
    verified_source_ids={row["employer_id"] for row in eligible_sources}
    verified_early_source_count=sum(bool(source_early_counts.get(source_id)) for source_id in verified_source_ids)
    summary={"employers":len({row["employer_id"] for row in rows}),
        "sources":len(rows),"active_employers":len({row["employer_id"] for row in rows if row["active"]}),
        "active_sources":sum(bool(row["active"]) for row in rows),
        "healthy_verified_sources":sum(bool(row["active"] and row["connector_status"]=="healthy" and
            row["discovery_status"]=="verified" and row["last_success_at"]) for row in rows),
        "eligible_discovery_sources":len(eligible_sources),"unsupported_sources":unsupported,
        "awaiting_verification_sources":awaiting,"scheduled_retry_sources":scheduled,
        "recently_discovered_sources":recent_sources,"source_failure_reasons":failure_reasons,
        "verified_early_career_sources":verified_early_source_count,
        "verified_sources_with_no_early_career_openings":max(0,len(verified_source_ids)-verified_early_source_count),
        "unsupported_providers":sorted({str(row["source_type"]) for row in rows if "unsupported" in str(row["connector_status"]).casefold()}),
        "saved_openings":len(openings),"current_verified_openings":sum(current_counts.values()),
        "current_early_career_openings":sum(early_counts.values()),
        "current_missing_descriptions":sum(missing_description_counts.values()),
        "current_opening_window_days":30}
    return {"results": list(grouped.values()), "count": len(grouped),"summary":summary}


@router.get("/api/employer-candidates")
def list_employer_candidates(request: Request, limit: int = 50):
    _require_local_admin(request)
    limit=max(1,min(int(limit),100))
    with database() as db:
        total=int(db.execute("SELECT COUNT(*) FROM employer_candidate_seeds").fetchone()[0])
        status_counts={str(row[0]):int(row[1]) for row in db.execute("SELECT status,COUNT(*) FROM employer_candidate_seeds GROUP BY status")}
        recent_cutoff=(datetime.now(timezone.utc)-timedelta(days=30)).isoformat()
        recent=int(db.execute("SELECT COUNT(*) FROM employer_candidate_seeds WHERE discovered_at>=?",(recent_cutoff,)).fetchone()[0])
        rows=db.execute("SELECT candidate_id,name,website_url,careers_url,evidence_url,candidate_source,candidate_record_url,candidate_license,discovered_at,last_attempt_at,status,diagnostics_json FROM employer_candidate_seeds ORDER BY discovered_at DESC LIMIT ?",(limit,)).fetchall()
    results=[]
    candidate_ids=[row["candidate_id"] for row in rows]
    provenance_by_candidate={candidate_id:[] for candidate_id in candidate_ids}
    if candidate_ids:
        placeholders=",".join("?" for _ in candidate_ids)
        with database() as db:
            provenance_rows=db.execute(f"SELECT candidate_id,provider,record_url,evidence_url,access_terms,discovered_at FROM employer_candidate_provenance WHERE candidate_id IN ({placeholders}) ORDER BY discovered_at",candidate_ids).fetchall()
        for provenance in provenance_rows:
            provenance_by_candidate[provenance["candidate_id"]].append({key:provenance[key] for key in ("provider","record_url","evidence_url","access_terms","discovered_at")})
    for row in rows:
        item=dict(row)
        try:item["diagnostics"]=json.loads(item.pop("diagnostics_json") or "{}")
        except (TypeError,json.JSONDecodeError):item["diagnostics"]={}
        item["provenance"]=provenance_by_candidate.get(item["candidate_id"],[])
        results.append(item)
    return {"results":results,"count":len(results),"summary":{"total":total,"showing":len(results),"recently_discovered":recent,"by_status":status_counts}}


def _run_employer_discovery_job(run_id: str, providers: list[str], limit: int, seed_limit: int):
    output=StringIO()
    report_path=DB.parent/f"employer-expansion-{run_id}.json"
    try:
        import expand_employers
        with redirect_stdout(output):
            exit_code=expand_employers.main(["--providers",",".join(providers),"--limit",str(limit),
                "--seed-limit",str(seed_limit),"--report",str(report_path)])
        report=json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
        status="completed" if exit_code==0 else ("no_work" if exit_code==2 else "failed")
        detail={"exit_code":exit_code,"report":report,"output":output.getvalue()[-12000:]}
    except Exception as error:
        status="failed"
        detail={"error":type(error).__name__,"output":output.getvalue()[-12000:]}
    stamp=datetime.now(timezone.utc).isoformat()
    with database() as db:
        db.execute("UPDATE employer_provider_runs SET status=?,diagnostics_json=?,updated_at=? WHERE run_id=?",
                   (status,json.dumps(detail,ensure_ascii=False),stamp,run_id))


@router.get("/api/employer-discovery/providers")
def employer_discovery_providers(request: Request):
    _require_local_admin(request)
    from candidate_importers import PROVIDER_REGISTRY
    with database() as db:
        counts={r[0]:r[1] for r in db.execute("SELECT candidate_source,COUNT(*) FROM employer_candidate_seeds GROUP BY candidate_source")}
        checkpoints={r[0]:dict(r) for r in db.execute("SELECT source_key,cursor_value,pages_completed,status,last_error,updated_at FROM employer_import_checkpoints")}
        latest=db.execute("SELECT run_id,providers_json,status,diagnostics_json,updated_at FROM employer_provider_runs ORDER BY created_at DESC LIMIT 1").fetchone()
    latest_data=dict(latest) if latest else None
    if latest_data:
        latest_data["providers"]=json.loads(latest_data.pop("providers_json"))
        latest_data["diagnostics"]=json.loads(latest_data.pop("diagnostics_json") or "{}")
    return {"providers":[{"provider":key,"label":value["label"],"endpoint":value["endpoint"],
            "access_terms":value["access_terms"],"candidate_count":counts.get(key,0),
            "checkpoint":checkpoints.get(key)} for key,value in PROVIDER_REGISTRY.items()],
        "saved_candidate_count":sum(counts.values()),"latest_run":latest_data}


@router.post("/api/employer-discovery/run",status_code=202)
def start_employer_discovery(payload: EmployerDiscoveryRunRequest, request: Request):
    _require_local_admin(request)
    from candidate_importers import PROVIDER_REGISTRY
    providers=list(dict.fromkeys(payload.providers))
    if any(provider not in PROVIDER_REGISTRY for provider in providers):
        raise HTTPException(422,"Choose a supported employer discovery provider.")
    with database() as db:
        running=db.execute("SELECT run_id FROM employer_provider_runs WHERE status='running' LIMIT 1").fetchone()
        if running: raise HTTPException(409,"An employer discovery run is already in progress.")
        run_id=uuid.uuid4().hex
        stamp=datetime.now(timezone.utc).isoformat()
        db.execute("INSERT INTO employer_provider_runs(run_id,providers_json,status,diagnostics_json,created_at,updated_at) VALUES(?,?,'running','{}',?,?)",
            (run_id,json.dumps(providers),stamp,stamp))
    try:
        EMPLOYER_DISCOVERY_POOL.submit(_run_employer_discovery_job,run_id,providers,payload.limit,payload.seed_limit)
    except RuntimeError as error:
        with database() as db: db.execute("UPDATE employer_provider_runs SET status='failed',updated_at=? WHERE run_id=?",(datetime.now(timezone.utc).isoformat(),run_id))
        raise HTTPException(503,"Employer discovery worker is unavailable.") from error
    return {"run_id":run_id,"status":"running","providers":providers}


@router.get("/api/employer-discovery/runs/{run_id}")
def employer_discovery_run_status(run_id: str, request: Request):
    _require_local_admin(request)
    with database() as db:
        row=db.execute("SELECT run_id,providers_json,status,diagnostics_json,created_at,updated_at FROM employer_provider_runs WHERE run_id=?",(run_id,)).fetchone()
    if not row: raise HTTPException(404,"Employer discovery run not found.")
    item=dict(row)
    item["providers"]=json.loads(item.pop("providers_json"))
    item["diagnostics"]=json.loads(item.pop("diagnostics_json") or "{}")
    return item


@router.get("/employers", response_class=HTMLResponse)
def employer_sources_page(request: Request):
    _require_local_admin(request)
    page=apply_ui_theme(EMPLOYER_PAGE)
    candidate_script=r'''async function loadProviderHealth(){const d=await api('/api/employer-discovery/providers');$('providers').replaceChildren();for(const p of d.providers){const label=document.createElement('label');const input=document.createElement('input');input.type='checkbox';input.name='provider';input.value=p.provider;input.checked=p.provider==='yc_api';label.append(input,document.createTextNode(` ${p.label} · saved candidates ${p.candidate_count} · ${p.access_terms}${p.checkpoint?` · checkpoint ${p.checkpoint.status}`:''}`));$('providers').append(label,document.createElement('br'))}const c=await api('/api/employer-candidates?limit=50');let summary=document.getElementById('candidateSummary');if(!summary){summary=document.createElement('p');summary.id='candidateSummary';summary.className='muted';$('candidateList').before(summary)}const counts=Object.entries(c.summary?.by_status||{}).map(([name,count])=>`${name}: ${count}`).join(' · ');summary.textContent=`Candidates ${c.summary?.total||0} · Recently discovered ${c.summary?.recently_discovered||0} · ${counts} · Showing ${c.summary?.showing||0} most recent`;$('candidateList').replaceChildren();for(const x of c.results){const item=document.createElement('p');const reason=x.diagnostics?.reason||x.diagnostics?.failure?.reason||x.diagnostics?.error||'';item.textContent=`${x.name} · ${x.candidate_source} · ${x.status}${reason?` · ${reason}`:''} · ${x.website_url} · evidence ${x.evidence_url||x.candidate_record_url}`;$('candidateList').append(item)}}'''
    page=re.sub(r"async function loadProviderHealth\(\)\{.*?\}\nasync function pollRun",candidate_script+"\nasync function pollRun",page,count=1,flags=re.S)
    page=page.replace(" · Awaiting verification ${c.awaiting_verification_sources??'—'}",
        " · Newly discovered sources ${c.recently_discovered_sources??'—'} · Awaiting verification ${c.awaiting_verification_sources??'—'}")
    page=page.replace(" · Scheduled retry ${c.scheduled_retry_sources??'—'}",
        " · Scheduled retry ${c.scheduled_retry_sources??'—'} · Source issue categories ${Object.entries(c.source_failure_reasons||{}).map(([reason,count])=>`${reason}: ${count}`).join(', ')||'none'}")
    page=page.replace(" · Early-career openings ${c.current_early_career_openings??'—'}",
        " · Early-career openings ${c.current_early_career_openings??'—'} · Verified early-career sources ${c.verified_early_career_sources??0} · Verified sources without early-career openings ${c.verified_sources_with_no_early_career_openings??0}")
    page=page.replace(" · Unsupported ${c.unsupported_sources??'—'}",
        " · Unsupported ${c.unsupported_sources??'—'} (${(c.unsupported_providers||[]).join(', ')||'none'})")
    page=page.replace(" · Jobs observed ${s.verification_evidence?.postings_observed||0}",
        " · Current early-career jobs ${s.early_career_current_openings_count||0} · Current university hiring jobs ${s.university_hiring_evidence_count||0} · Current verified jobs ${s.current_verified_openings_count||0} · Jobs observed ${s.verification_evidence?.postings_observed||0}")
    return HTMLResponse(page)


def register_employer_sources(payload: EmployerImportRequest):
    """Add public source candidates idempotently; every new row stays inactive."""
    stamp = datetime.now(timezone.utc).isoformat()
    imported = 0
    with database() as db:
        for item in payload.sources:
            source_type = item.source_type.casefold()
            token = item.source_token.strip()
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", token) and source_type not in {"career_page", "public_html", "rss", "sitemap", "workday"}:
                raise HTTPException(422, "Career-board identifiers may contain only letters, numbers, dots, underscores, and hyphens.")
            careers_url = item.careers_url.strip()
            if source_type in {"career_page", "public_html", "rss", "sitemap", "workday"}:
                try:
                    validate_public_https_url(careers_url)
                except ValueError as error:
                    raise HTTPException(422, f"Career website rejected: {error}")
            if source_type in {"career_page", "public_html", "rss", "sitemap", "workday"}:
                token = hashlib.sha256(careers_url.encode("utf-8")).hexdigest()[:24]
            feeds = {
                "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true",
                "lever": f"https://api.lever.co/v0/postings/{token}?mode=json",
                "ashby": f"https://api.ashbyhq.com/posting-api/job-board/{token}",
                "smartrecruiters": f"https://api.smartrecruiters.com/v1/companies/{token}/postings",
                "career_page": careers_url,
                "public_html": careers_url,
                "rss": careers_url,
                "sitemap": careers_url,
                "workday": "",
            }
            status = "unsupported" if source_type == "workday" else "needs_verification"
            for value in (item.website_url.strip(), item.evidence_url.strip()):
                if value:
                    try: validate_public_https_url(value)
                    except ValueError as error: raise HTTPException(422, f"Evidence URL rejected: {error}")
            if item.candidate_record_url.strip():
                try: validate_public_https_url(item.candidate_record_url.strip())
                except ValueError as error: raise HTTPException(422, f"Candidate provenance URL rejected: {error}")
            db.execute("""INSERT INTO employers(employer_id,name,careers_url,source_type,source_token,public_feed_url,
                geographic_coverage,connector_status,active,created_at,updated_at,website_url,evidence_url,candidate_source,candidate_record_url,candidate_license,discovered_at,discovery_status)
                VALUES(?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,?,?,'registered') ON CONFLICT(source_type,source_token) DO NOTHING""",
                (f"{source_type}:{token}",item.name.strip(),careers_url,source_type,token,feeds[source_type],
                 item.geographic_coverage.strip(),status,stamp,stamp,item.website_url.strip(),item.evidence_url.strip(),
                 item.candidate_source.strip(),item.candidate_record_url.strip(),item.candidate_license.strip(),stamp))
            inserted = db.execute("SELECT changes()").fetchone()[0]
            imported += inserted
            if inserted:
                stage_rows = [("candidate_registration", "registered", {}),
                    ("official_source_discovery", "evidence_provided" if item.evidence_url or item.website_url else "seed_source_provided",
                    {"website_url": item.website_url.strip(), "evidence_url": item.evidence_url.strip(),
                     "candidate_source": item.candidate_source.strip(), "candidate_record_url": item.candidate_record_url.strip(),
                     "candidate_license": item.candidate_license.strip()}),
                    ("source_validation", "pending", {}), ("source_activation", "inactive", {})]
                db.executemany("INSERT INTO employer_discovery_stages(employer_id,stage_key,status,diagnostics_json,updated_at) VALUES(?,?,?,?,?)",
                    [(f"{source_type}:{token}", key, stage, json.dumps(details), stamp) for key, stage, details in stage_rows])
    return {"imported": imported, "active": 0, "message": "New sources stay inactive until read-only validation succeeds."}


def persist_candidate_seeds(candidates: list[dict], *, return_details: bool = False):
    """Persist company-directory records before career-source discovery.

    The default integer return is retained for existing callers. Expansion runs
    can request an auditable inserted/reused/rejected breakdown.
    """
    stamp=datetime.now(timezone.utc).isoformat(); imported=0; reused=0; rejected=0; reasons={}
    with database() as db:
        for item in candidates:
            name=str(item.get("name") or "").strip()
            website=str(item.get("website_url") or "").strip()
            source=str(item.get("candidate_source") or "").strip()
            record=str(item.get("candidate_record_url") or "").strip()
            license_name=str(item.get("candidate_license") or "").strip()
            if not name or len(name)>160 or not source or len(source)>120:
                rejected+=1; reasons["missing_or_invalid_identity"]=reasons.get("missing_or_invalid_identity",0)+1
                continue
            try:
                validate_public_https_url(website)
                if record: validate_public_https_url(record)
            except ValueError:
                rejected+=1; reasons["unsafe_or_invalid_url"]=reasons.get("unsafe_or_invalid_url",0)+1
                continue
            record_key=record or website
            normalized_name=re.sub(r"[^a-z0-9]+"," ",name.casefold()).strip()
            normalized_domain=(urlparse(website).hostname or "").casefold().removeprefix("www.")
            existing=db.execute("SELECT candidate_id FROM employer_candidate_seeds WHERE candidate_source=? AND candidate_record_url=?",(source,record_key)).fetchone()
            if not existing:
                existing=db.execute("SELECT candidate_id FROM employer_candidate_seeds WHERE normalized_domain=? ORDER BY discovered_at LIMIT 1",(normalized_domain,)).fetchone()
            if not existing:
                existing=db.execute("SELECT candidate_id FROM employer_candidate_seeds WHERE normalized_name=? AND normalized_domain='' ORDER BY discovered_at LIMIT 1",(normalized_name,)).fetchone()
            candidate_id=existing[0] if existing else hashlib.sha256(("employer\n"+normalized_domain).encode()).hexdigest()
            if existing:
                db.execute("UPDATE employer_candidate_seeds SET normalized_name=?,normalized_domain=?,careers_url=CASE WHEN careers_url='' THEN ? ELSE careers_url END,evidence_url=CASE WHEN evidence_url='' THEN ? ELSE evidence_url END WHERE candidate_id=?",
                    (normalized_name,normalized_domain,str(item.get("careers_url") or "").strip(),str(item.get("evidence_url") or "").strip(),candidate_id))
                reused+=1
            else:
                db.execute("""INSERT INTO employer_candidate_seeds(candidate_id,name,website_url,candidate_source,
                    candidate_record_url,candidate_license,discovered_at,status,diagnostics_json,careers_url,evidence_url,normalized_name,normalized_domain)
                    VALUES(?,?,?,?,?,?,?,'candidate','{}',?,?,?,?)""",
                    (candidate_id,name,website,source,record_key,license_name,stamp,str(item.get("careers_url") or "").strip(),
                     str(item.get("evidence_url") or "").strip(),normalized_name,normalized_domain))
                imported+=1
            db.execute("""INSERT INTO employer_candidate_provenance(candidate_id,provider,record_url,evidence_url,access_terms,discovered_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(candidate_id,provider,record_url) DO UPDATE SET
                evidence_url=excluded.evidence_url,access_terms=excluded.access_terms""",
                (candidate_id,source,record_key,str(item.get("evidence_url") or "").strip(),license_name,stamp))
    if return_details:
        return {"inserted":imported,"reused":reused,"rejected":rejected,"rejection_reasons":reasons}
    return imported


def update_candidate_seed(candidate: dict, status: str, diagnostics: dict | None = None):
    record=str(candidate.get("candidate_record_url") or candidate.get("website_url") or "").strip()
    source=str(candidate.get("candidate_source") or "").strip()
    website=str(candidate.get("website_url") or "").strip()
    stamp=datetime.now(timezone.utc).isoformat()
    details=diagnostics or {}
    with database() as db:
        db.execute("""UPDATE employer_candidate_seeds SET status=?,last_attempt_at=?,diagnostics_json=?,
            careers_url=CASE WHEN ?<>'' THEN ? ELSE careers_url END,
            evidence_url=CASE WHEN ?<>'' THEN ? ELSE evidence_url END
            WHERE candidate_source=? AND candidate_record_url=?""",
            (status,stamp,json.dumps(details)[:2000],str(details.get("careers_url") or ""),str(details.get("careers_url") or ""),
             str(details.get("evidence_url") or ""),str(details.get("evidence_url") or ""),source,record))


def get_pending_candidate_seeds(limit: int = 500, source: str | None = None):
    params=[]
    sql="SELECT name,website_url,careers_url,evidence_url,candidate_source,candidate_record_url,candidate_license,status,normalized_name,normalized_domain FROM employer_candidate_seeds WHERE status IN ('candidate','discovery_failed','no_public_career_source')"
    if source:
        sql+=" AND candidate_source=?";params.append(source)
    sql+=" ORDER BY discovered_at,candidate_id LIMIT ?";params.append(int(limit))
    with database() as db: rows=db.execute(sql,params).fetchall()
    return [dict(row) for row in rows]


def pending_candidate_employer_ids(source: str | None, limit: int = 100):
    with database() as db:
        sql="SELECT * FROM employers WHERE active=0 AND connector_status!='disabled'"; params=[]
        if source: sql+=" AND candidate_source=?"; params.append(source)
        rows=[dict(row) for row in db.execute(sql,params).fetchall()]
    eligible=[row for row in rows if employer_retry_eligible(row)]
    eligible.sort(key=lambda row:(row["source_type"] not in {"greenhouse","lever","ashby","smartrecruiters"},row.get("last_attempt_at") or "",row["created_at"],row["employer_id"]))
    return [row["employer_id"] for row in eligible[:int(limit)]]


def get_employer_import_checkpoint(source_key: str):
    with database() as db:
        row=db.execute("SELECT source_key,cursor_value,pages_completed,status,last_error,updated_at FROM employer_import_checkpoints WHERE source_key=?",(source_key,)).fetchone()
    return dict(row) if row else {"source_key":source_key,"cursor_value":"","pages_completed":0,"status":"pending","last_error":""}


def save_employer_import_checkpoint(source_key: str, cursor_value: str, pages_completed: int,
                                    status: str, error: str = ""):
    stamp=datetime.now(timezone.utc).isoformat()
    with database() as db:
        db.execute("""INSERT INTO employer_import_checkpoints(source_key,cursor_value,pages_completed,status,last_error,updated_at)
            VALUES(?,?,?,?,?,?) ON CONFLICT(source_key) DO UPDATE SET cursor_value=excluded.cursor_value,
            pages_completed=excluded.pages_completed,status=excluded.status,last_error=excluded.last_error,updated_at=excluded.updated_at""",
            (source_key,cursor_value,int(pages_completed),status,error[:300],stamp))


@router.post("/api/employers/import")
def import_employers(payload: EmployerImportRequest, request: Request):
    """Local-only registration; activation requires a successful validation."""
    _require_local_admin(request)
    return register_employer_sources(payload)


@router.post("/api/employers/discover-source")
def discover_employer_source(payload: EmployerSourceDiscoveryRequest, request: Request):
    """Inspect one verified company page for explicit, public ATS links."""
    _require_local_admin(request)
    candidates, error = discover_source_candidates(payload.name.strip(), payload.website_url.strip())
    if error:
        raise HTTPException(422, error)
    # Persist discoveries as inactive rows immediately, with the official page
    # and page that exposed the public job source retained as provenance.
    imported=[]
    for candidate in candidates:
        imported.append(EmployerImportItem(name=candidate["name"],source_type=candidate["source_type"],
            source_token=candidate["source_token"],careers_url=candidate["careers_url"],
            website_url=candidate.get("website_url",payload.website_url),evidence_url=candidate.get("evidence_url",payload.website_url)))
    registration=register_employer_sources(EmployerImportRequest(sources=imported))
    return {"results": candidates, "count": len(candidates), "activated": False,
            "registered":registration["imported"],
            "message": "Candidates were registered inactive; live validation is required before activation."}


def verify_employer_source_record(employer_id: str, *, fetcher=None, force=False):
    """Perform read-only verification and persist bounded, non-sensitive evidence."""
    fetcher = fetcher or fetch_source
    with database() as db:
        row = db.execute("SELECT * FROM employers WHERE employer_id=?", (employer_id,)).fetchone()
    if not row:
        return None
    original=dict(row)
    if not employer_retry_eligible(original,force=force):
        return {"valid":False,"status":"retry_deferred","active":bool(row["active"]),"jobs_found":0,
                "message":"Source is not eligible for another check yet. Force revalidation to override the retry schedule.",
                "error":"retry deferred","evidence":{}}
    source = EmployerSource(row["name"], row["source_type"], row["source_token"], row["careers_url"], row["geographic_coverage"])
    evidence = {"method":"read_only_public_source_check", "source_type":source.source_type,
                "checked_at":datetime.now(timezone.utc).isoformat(), "postings_observed":0,
                "records_valid":0,"records_invalid":0,"records_with_location":0,
                "records_with_application_url":0,"request_hosts":[],"http_statuses":[],"http_responses":[],"cache_hits":0}
    try:
        with observe_fetch() as observer:
            jobs, error = fetcher(source)
    except Exception as exc:
        # Store only the exception class; exception text may contain request
        # URLs or other details that do not belong in logs/reports.
        jobs, error = [], f"Connector execution failed: {type(exc).__name__}."
        observer={"responses":[],"cache_hits":0}
    evidence["request_hosts"] = sorted({str(item.get("host", "")) for item in observer.get("responses", []) if item.get("host")})
    evidence["http_statuses"] = sorted({item.get("status_code") for item in observer.get("responses", []) if isinstance(item.get("status_code"), int)})
    evidence["http_responses"] = [{"host":item.get("host"),"status_code":item.get("status_code")}
        for item in observer.get("responses",[]) if item.get("host") and isinstance(item.get("status_code"),int)]
    evidence["cache_hits"] = int(observer.get("cache_hits",0))
    evidence["postings_observed"] = len(jobs) if isinstance(jobs,list) else 0
    if not error and isinstance(jobs,list):
        # A well-formed empty feed is valid. For non-empty feeds, require at
        # least one posting whose identifier, title and application URL are
        # usable; inspect a bounded number of records on large boards.
        dns_checked_hosts=set()
        for raw in jobs[:250]:
            try:
                normalized=normalize_job(source.source_type,source,raw)
                application_url=normalized.get("application_url")
                if not application_url:
                    raise ValueError("missing application URL")
                host=(urlparse(application_url).hostname or "").casefold()
                validate_public_https_url(application_url,resolve=host not in dns_checked_hosts)
                dns_checked_hosts.add(host)
                evidence["records_valid"] += 1
                if normalized.get("location"):
                    evidence["records_with_location"] += 1
                evidence["records_with_application_url"] += 1
            except (TypeError,ValueError):
                evidence["records_invalid"] += 1
        if jobs and not evidence["records_valid"]:
            error=f"Feed returned {min(len(jobs),250)} sampled postings but none had a valid identifier, title, and public application URL."
    elif not error and not isinstance(jobs,list):
        error="Malformed response: source adapter did not return a jobs list."
    status = _source_health(error)
    stamp = datetime.now(timezone.utc).isoformat()
    evidence["checked_at"]=stamp
    valid = error is None and isinstance(jobs, list)
    # A successful empty documented feed is a valid source, but no job fields
    # can be demonstrated until it publishes a posting.
    active = 1 if valid else (int(row["active"]) if status in {"temporarily_unavailable", "network_timeout", "rate_limited"} else 0)
    failures=0 if valid else int(row["consecutive_failures"] or 0)+1
    fingerprint=_source_fingerprint(original)
    retry_at=(datetime.now(timezone.utc)+timedelta(hours=24)).isoformat() if valid else _next_source_retry(status,error,failures)
    observed_hosts=set(evidence["request_hosts"])
    redirected_provider=next((provider for provider,hosts in {
        "greenhouse":{"boards-api.greenhouse.io"},"lever":{"api.lever.co"},
        "ashby":{"api.ashbyhq.com"},"smartrecruiters":{"api.smartrecruiters.com"},
    }.items() if source.source_type in {"public_html","career_page"} and observed_hosts.intersection(hosts)),None)
    if redirected_provider: evidence["recognized_ats_provider"]=redirected_provider
    reason=("valid_via_ats_redirect" if valid and redirected_provider else
            "valid_zero_postings" if valid and not jobs else ("valid_with_postings" if valid else status))
    with database() as db:
        db.execute("UPDATE employers SET last_attempt_at=?,last_success_at=CASE WHEN ? THEN ? ELSE last_success_at END,connector_status=?,error_details=?,verification_evidence_json=?,active=?,updated_at=?,discovery_status=?,health_reason=?,consecutive_failures=?,next_retry_at=?,source_fingerprint=?,force_revalidate=0 WHERE employer_id=?",
                   (stamp,int(valid),stamp,"healthy" if valid else status,(error or "")[:500],json.dumps(evidence),active,stamp,"verified" if valid else status,reason,failures,retry_at,fingerprint,employer_id))
        db.executemany("""INSERT INTO employer_discovery_stages(employer_id,stage_key,status,diagnostics_json,updated_at)
            VALUES(?,?,?,?,?) ON CONFLICT(employer_id,stage_key) DO UPDATE SET status=excluded.status,
            diagnostics_json=excluded.diagnostics_json,updated_at=excluded.updated_at""",
            [(employer_id,"source_validation","verified" if valid else status,json.dumps(evidence),stamp),
             (employer_id,"source_activation","active" if active else "inactive",json.dumps({"active":bool(active)}),stamp)])
    return {"valid":valid,"status":"healthy" if valid else status,"health_reason":reason,"jobs_found":len(jobs) if valid else 0,
            "active":bool(active),"message":"Source validated and activated." if valid else error,
            "evidence":evidence,"error":error}


@router.post("/api/employers/{employer_id}/validate")
def validate_employer_source(employer_id: str, request: Request, force: bool = False):
    """Probe public read-only source; activate only after a valid response."""
    _require_local_admin(request)
    result=verify_employer_source_record(employer_id,force=force)
    if result is None:
        raise HTTPException(404, "Employer source not found.")
    result.pop("error",None)
    return result


@router.post("/api/employers/{employer_id}/disable")
def disable_employer_source(employer_id: str, request: Request):
    _require_local_admin(request)
    with database() as db:
        cursor = db.execute("UPDATE employers SET active=0,connector_status='disabled',updated_at=? WHERE employer_id=?",
                            (datetime.now(timezone.utc).isoformat(), employer_id))
        if not cursor.rowcount:
            raise HTTPException(404, "Employer source not found.")
    return {"disabled": True}


EMPLOYER_PAGE = r'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Employer Sources · BabyStepsAI</title>
<style>body{font:16px system-ui;max-width:1000px;margin:32px auto;padding:0 18px;color:#172033;background:#f8fafc}main,header{background:white;border:1px solid #e2e8f0;border-radius:12px;padding:22px;margin-bottom:16px}input,button{font:inherit;padding:9px;margin:4px}button{cursor:pointer}article{padding:14px 0;border-top:1px solid #ddd}.muted{color:#526174}</style>
<header><h1>Employer Sources</h1><p class="muted">Local administration · sources stay inactive until a live public validation succeeds.</p><a href="/find-jobs">Find jobs</a></header><main><div id="summary">Loading…</div><h2>Discover from an official company website</h2><input id="name" placeholder="Company name"><input id="website" placeholder="https://company.example"><button id="discover">Discover and register</button><p id="status" role="status"></p></main><main><h2>Automated candidate providers</h2><div id="providers">Loading provider health…</div><p><label>Sources to validate <input id="runLimit" type="number" min="1" max="100" value="20"></label> <label>Companies to inspect <input id="seedLimit" type="number" min="1" max="500" value="100"></label></p><button id="runDiscovery">Run selected discovery</button><p id="discoveryStatus" role="status"></p><div id="candidateList"></div></main><main><h2>Import candidate CSV or JSON</h2><input id="file" type="file" accept=".csv,.json"><button id="import">Import inactive candidates</button></main><main><h2>Configured employers</h2><label>Provider <select id="sourceProvider"><option value="">All providers</option><option>greenhouse</option><option>lever</option><option>ashby</option><option>smartrecruiters</option><option>public_html</option><option>rss</option><option>sitemap</option><option>workday</option></select></label> <label>Status <select id="sourceStatus"><option value="">All statuses</option><option>healthy</option><option>needs_verification</option><option>unsupported_structure</option><option>unsupported_provider</option><option>javascript_rendered</option><option>temporarily_unavailable</option><option>permanently_unavailable</option><option>network_timeout</option><option>rate_limited</option><option>authentication_required</option><option>access_restricted</option><option>robots_disallowed</option></select></label> <label>Verified <select id="verifyWindow"><option value="0">Any date</option><option value="24">Last 24 hours</option><option value="168">Last 7 days</option><option value="720">Last 30 days</option></select></label> <label>Failure reason <input id="failureFilter" type="search" placeholder="Search error or health reason"></label><div id="list"></div></main>
<script>
const $=id=>document.getElementById(id);const api=async(url,opts={})=>{const r=await fetch(url,opts);const d=await r.json();if(!r.ok)throw Error(d.detail||'Request failed');return d};
async function load(){const d=await api('/api/employers');let sources=d.results.flatMap(e=>e.sources.map(s=>({...s,name:e.name})));const c=d.summary||{};$('summary').textContent=`Employers ${c.employers??d.count} · Active employers ${c.active_employers??'—'} · Sources ${c.sources??sources.length} · Healthy verified ${c.healthy_verified_sources??'—'} · Eligible for discovery ${c.eligible_discovery_sources??'—'} · Awaiting verification ${c.awaiting_verification_sources??'—'} · Unsupported ${c.unsupported_sources??'—'} · Scheduled retry ${c.scheduled_retry_sources??'—'} · Saved openings ${c.saved_openings??'—'} · Current verified openings ${c.current_verified_openings??'—'} · Early-career openings ${c.current_early_career_openings??'—'} · Missing descriptions ${c.current_missing_descriptions??'—'} (current window: ${c.current_opening_window_days??30} days)`;
 $('list').replaceChildren();for(const e of d.results){const visible=e.sources.filter(s=>{const hrs=Number($('verifyWindow').value||0),verified=Date.parse(s.last_success_at||'');const dateOk=!hrs||(Number.isFinite(verified)&&(Date.now()-verified)<=hrs*3600000);const term=$('failureFilter').value.trim().toLowerCase();return (!$('sourceProvider').value||s.source_type===$('sourceProvider').value)&&(!$('sourceStatus').value||s.connector_status===$('sourceStatus').value)&&dateOk&&(!term||`${s.health_reason||''} ${s.error_details||''}`.toLowerCase().includes(term))});if(!visible.length)continue;const a=document.createElement('article');const h=document.createElement('h3');h.textContent=e.name;a.append(h);const coverage=document.createElement('p');coverage.textContent=`Saved openings ${e.saved_openings_count||0} · Current verified ${e.current_openings_count||0} · Early-career evidence ${e.early_career_current_openings_count||0} · University hiring evidence ${e.university_hiring_evidence_count||0} · Missing descriptions ${e.current_missing_descriptions_count||0}`;a.append(coverage);for(const s of visible){const p=document.createElement('p');p.textContent=`${s.source_type} ${s.source_token||''} · ${s.connector_status} (${s.health_reason||'not checked'}) · ${s.active?'active':'inactive'} · Jobs observed ${s.verification_evidence?.postings_observed||0} · Last verified ${s.last_success_at||'never'} · Last attempt ${s.last_attempt_at||'never'} · Next retry ${s.next_retry_at||'eligible'} · Failures ${s.consecutive_failures||0}${s.error_details?` · ${s.error_details}`:''}`;a.append(p);const stage=document.createElement('small');const ev=s.verification_evidence||{};stage.textContent='Evidence: '+(e.evidence_url||s.candidate_record_url||'not recorded')+(ev.recognized_ats_provider?` · ATS ${ev.recognized_ats_provider}`:'')+(ev.http_responses?.length?` · HTTP ${ev.http_responses.map(x=>`${x.host}:${x.status_code}`).join(', ')}`:'')+' · Stages: '+(s.stages||[]).map(x=>`${x.stage_key}=${x.status}`).join(' · ');a.append(stage);const validate=document.createElement('button');validate.textContent='Validate when eligible';validate.onclick=async()=>{try{const x=await api(`/api/employers/${encodeURIComponent(s.employer_id)}/validate`,{method:'POST'});$('status').textContent=x.message;await load()}catch(x){$('status').textContent=x.message}};a.append(validate);const force=document.createElement('button');force.textContent='Force revalidate';force.onclick=async()=>{try{const x=await api(`/api/employers/${encodeURIComponent(s.employer_id)}/validate?force=true`,{method:'POST'});$('status').textContent=x.message;await load()}catch(x){$('status').textContent=x.message}};a.append(force);if(s.active){const disable=document.createElement('button');disable.textContent='Disable';disable.onclick=async()=>{await api(`/api/employers/${encodeURIComponent(s.employer_id)}/disable`,{method:'POST'});load()};a.append(disable)}}$('list').append(a)}}
$('discover').onclick=async()=>{try{const d=await api('/api/employers/discover-source',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:$('name').value.trim(),website_url:$('website').value.trim()})});$('status').textContent=`Found ${d.count}, registered ${d.registered} inactive source(s). Use Validate on a source to activate after public response validation.`;await load()}catch(e){$('status').textContent=e.message}};
function csvRows(text){const lines=text.split(/\r?\n/).filter(Boolean),parse=line=>{const out=[];let v='',q=false;for(let i=0;i<line.length;i++){if(line[i]==='"'&&q&&line[i+1]==='"'){v+='"';i++}else if(line[i]==='"')q=!q;else if(line[i]===','&&!q){out.push(v);v=''}else v+=line[i]}out.push(v);return out};const heads=parse(lines.shift()).map(x=>x.trim());return lines.map(line=>Object.fromEntries(parse(line).map((v,i)=>[heads[i],v.trim()])))}
$('import').onclick=async()=>{try{const f=$('file').files[0];if(!f)throw Error('Choose a candidate file.');const text=await f.text(),data=f.name.toLowerCase().endsWith('.json')?JSON.parse(text):null;const rows=data?(Array.isArray(data)?data:data.sources):csvRows(text);const d=await api('/api/employers/import',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sources:rows})});$('status').textContent=`Imported ${d.imported} inactive source(s).`;await load()}catch(e){$('status').textContent=e.message}};
async function loadProviderHealth(){const d=await api('/api/employer-discovery/providers');$('providers').replaceChildren();for(const p of d.providers){const label=document.createElement('label');const input=document.createElement('input');input.type='checkbox';input.name='provider';input.value=p.provider;input.checked=p.provider==='yc_api';label.append(input,document.createTextNode(` ${p.label} · saved candidates ${p.candidate_count} · ${p.access_terms}${p.checkpoint?` · checkpoint ${p.checkpoint.status}`:''}`));$('providers').append(label,document.createElement('br'))}const c=await api('/api/employer-candidates');$('candidateList').replaceChildren();for(const x of c.results){const item=document.createElement('p');item.textContent=`${x.name} · ${x.candidate_source} · ${x.status} · ${x.website_url} · evidence ${x.evidence_url||x.candidate_record_url}`;$('candidateList').append(item)}}
async function pollRun(id){for(let i=0;i<180;i++){const d=await api(`/api/employer-discovery/runs/${encodeURIComponent(id)}`);$('discoveryStatus').textContent=`Discovery ${d.status}${d.diagnostics?.output?` · ${d.diagnostics.output.slice(-1000)}`:''}`;if(i%5===0)await loadProviderHealth();if(d.status!=='running'){await load();await loadProviderHealth();return}await new Promise(resolve=>setTimeout(resolve,1000))}}
$('runDiscovery').onclick=async()=>{try{const providers=[...document.querySelectorAll('input[name="provider"]:checked')].map(x=>x.value);if(!providers.length)throw Error('Select at least one provider.');const d=await api('/api/employer-discovery/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({providers,limit:Number($('runLimit').value),seed_limit:Number($('seedLimit').value)})});$('discoveryStatus').textContent='Discovery started.';pollRun(d.run_id)}catch(e){$('discoveryStatus').textContent=e.message}};
Promise.all([load(),loadProviderHealth()]).catch(e=>$('summary').textContent=e.message);
$('sourceProvider').onchange=load;$('sourceStatus').onchange=load;$('verifyWindow').onchange=load;$('failureFilter').oninput=load;
</script></html>'''


PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Find Jobs · BabyStepsAI</title><style>
*{box-sizing:border-box}body{font-family:system-ui,sans-serif;max-width:900px;margin:36px auto;padding:0 18px;background:#f8fafc;color:#172033}header,main{background:white;border:1px solid #e2e8f0;border-radius:14px;padding:24px;margin-bottom:18px}h1{margin:0 0 8px}p{color:#536174}textarea{width:100%;min-height:100px;padding:12px;border:1px solid #cbd5e1;border-radius:8px;font:inherit}button{margin-top:12px;background:#2563eb;color:white;border:0;border-radius:8px;padding:11px 18px;font:inherit;cursor:pointer}button:disabled{opacity:.6}.note{font-size:14px}.job{padding:16px 0;border-top:1px solid #e2e8f0}.job h3{margin:0 0 6px}.job p{margin:4px 0}.error{color:#b91c1c}.diagnostics{font-size:14px;background:#f1f5f9;padding:12px;border-radius:8px}</style></head>
<body><header class="top"><h1>Find Jobs</h1><p>Search registered public job sources using your saved career preferences and a prompt.</p><a href="/">Dashboard</a> · <a href="/careers">Career preferences</a> · <a href="/applications">Applications</a> · <a href="/application-settings">Automation permissions</a></header>
<main class="panel"><section class="import-panel" aria-labelledby="importHeading"><h2 id="importHeading">Import Job from Company Website</h2><label for="jobUrl">Official public job-posting URL</label><input id="jobUrl" type="url" maxlength="2048" placeholder="https://careers.company.com/jobs/123" autocomplete="url" style="width:100%;padding:12px;border:1px solid #cbd5e1;border-radius:8px;font:inherit"><p class="note">Imports one public posting from supported ATS pages or company pages with structured JobPosting data. Restricted pages are not accessed.</p><button id="importJob">Import Job</button><p id="importStatus" role="status"></p><div id="importDetails" class="diagnostics" hidden></div><label id="reviewImportLabel" hidden><input type="checkbox" id="reviewImport"> I reviewed the job requirements and approve dry-run preparation if eligibility checks allow it.</label><br><button id="enrichImport" hidden>Retry Description Enrichment</button><button id="analyzeImport" hidden>Analyze with AI</button><button id="prepareImport" hidden>Prepare Application as Dry Run</button></section><hr><h2>What are you looking for?</h2><label for="prompt">Add focus or context for this search</label><textarea id="prompt" placeholder="For example: Find entry level data engineer roles in the United States"></textarea><p class="note">Results use registered public job sources. Matches are preliminary; eligibility must be analyzed and reviewed. Applications are never submitted automatically.</p><button id="search">Find Jobs</button><button id="cancelSearch" hidden>Cancel search</button><button id="analyze" hidden>Analyze Matches</button><label for="batchSize">Jobs per AI batch</label> <select id="batchSize"><option value="3">3</option><option value="4">4</option><option value="5">5</option></select><button id="pause" hidden>Pause Analysis</button><button id="resume" hidden>Resume Analysis</button><p id="status" role="status"></p><p id="matchStatus" role="status"></p><div id="diagnostics"></div><div id="coverageMetrics" class="diagnostics" hidden></div><div id="jobFilters" hidden><label>Search title/company <input id="filterText" type="search"></label> <label>Company <select id="filterCompany"><option value="">All companies</option></select></label> <label>Category <select id="filterCategory"><option value="">All categories</option></select></label> <label>Location <input id="filterLocation" type="search" placeholder="City or country"></label> <label>Experience <select id="filterExperience"><option value="">All levels</option></select></label> <label>Discovery status <select id="filterDiscovery"><option value="">New and previously seen</option><option value="newly_discovered">Newly discovered</option><option value="previously_seen">Previously seen</option></select></label> <label>Eligibility <select id="filterEligibility"><option value="">All classifications</option><option value="needs_review">Needs review</option><option value="potential_match">Potential match</option><option value="not_eligible">Not eligible</option><option value="failed">Failed analysis</option><option value="not_analyzed">Not analyzed</option></select></label> <label>Description <select id="filterDescription"><option value="">Any description status</option><option value="available">Available</option><option value="missing">Missing or incomplete</option></select></label> <label>Work arrangement <select id="filterArrangement"><option value="">All arrangements</option></select></label> <label>Employment type <select id="filterEmployment"><option value="">All employment types</option></select></label> <label>Source <select id="filterSource"><option value="">All sources</option></select></label> <label>Posting recency <select id="filterRecency"><option value="">Any date</option><option value="7">Last 7 days</option><option value="30">Last 30 days</option><option value="90">Last 90 days</option></select></div><div id="results"></div><div id="matchResults"></div></main>
<script>
const button=document.getElementById('search');
const cancelSearch=document.getElementById('cancelSearch');
const status=document.getElementById('status');
const results=document.getElementById('results');
const diagnostics=document.getElementById('diagnostics');
const analyzeButton=document.getElementById('analyze');
const matchStatus=document.getElementById('matchStatus');
const matchResults=document.getElementById('matchResults');
const pauseButton=document.getElementById('pause');
const resumeButton=document.getElementById('resume');
const recoverButton=document.createElement('button');recoverButton.textContent='Recover abandoned analysis';recoverButton.hidden=true;resumeButton.insertAdjacentElement('afterend',recoverButton);
let currentJobKeys=[];
let currentJobs=[];
let polling=false;
let pollingGeneration=0;
let currentMatchSessionId=null;
let currentMatchJobKeys=[];
let discoveryRunId=null;
let discoveryTimer=null;
let currentDiagnostics={};
let importedJobKey=null;
function apiErrorMessage(payload,fallback='The request could not be completed.'){
  const detail=payload&&typeof payload==='object'&&'detail' in payload?payload.detail:payload;
  if(typeof detail==='string'&&detail.trim())return detail;
  if(Array.isArray(detail))return detail.map(item=>{const where=Array.isArray(item?.loc)?item.loc.filter(x=>x!=='body').join('.'):'';return `${where?where+': ':''}${item?.msg||item?.message||'Invalid request'}`}).join('; ')||fallback;
  if(detail&&typeof detail==='object'){
    if(detail.code==='active_worker'&&detail.status==='paused')return `A separate matching session${Number(detail.total_jobs)?` (${Number(detail.total_jobs)} jobs)`:''} is paused but still owns the matching slot. This selected job was not started. Resume or finish that session in its own view, then retry Analyze with AI. This page did not change that session.`;
    if(detail.code==='active_worker')return 'Another matching session is using the worker. This selected job was not started. Wait for that session to finish, then retry Analyze with AI.';
    return String(detail.message||detail.error||fallback);
  }
  return fallback;
}
async function readApiResponse(response,fallback){
  let payload;try{payload=await response.json()}catch(_){throw new Error(`The server returned an unreadable response (HTTP ${response.status}).`)}
  if(!response.ok){const error=new Error(apiErrorMessage(payload,fallback));const detail=payload?.detail;error.recovery=detail&&typeof detail==='object'?detail:null;throw error}
  if(!payload||typeof payload!=='object'||Array.isArray(payload))throw new Error('The server returned an unexpected response. Please retry.');
  return payload;
}
document.getElementById('importJob').addEventListener('click',async()=>{
  const button=document.getElementById('importJob'),status=document.getElementById('importStatus'),details=document.getElementById('importDetails');
  button.disabled=true;status.className='';status.textContent='Checking public access and reading the posting…';details.hidden=true;
  document.getElementById('enrichImport').hidden=true;document.getElementById('analyzeImport').hidden=true;document.getElementById('prepareImport').hidden=true;document.getElementById('reviewImportLabel').hidden=true;
  try{
    const response=await fetch('/api/jobs/import-url',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:document.getElementById('jobUrl').value.trim()})});
    const data=await readApiResponse(response,'The job posting could not be imported.');
    importedJobKey=data.job_key;currentJobKeys=[importedJobKey];currentMatchJobKeys=[importedJobKey];currentMatchSessionId=null;pollingGeneration++;polling=false;matchStatus.textContent='';matchStatus.className='';matchResults.replaceChildren();pauseButton.hidden=true;resumeButton.hidden=true;recoverButton.hidden=true;details.hidden=false;
    details.textContent=`${data.employer} · ${data.title}\nProvider: ${data.provider}\nEmployer identity: ${data.employer_identity_source||'public posting metadata'}\nDescription: ${data.description_available?'available':'missing or incomplete'}${data.description_length?` (${data.description_length} characters)`:''}\nExperience evidence: ${data.experience_level} — ${(data.experience_level_evidence||[]).join(' ')}\nEligibility: ${data.eligibility_classification}${data.review_reasons?.length?`\nReview: ${data.review_reasons.join(' ')}`:''}\nApplication entry: ${data.application_url_source==='official_posting_page'?'official posting page (no separate application link was published)':'official application link'}\n${data.duplicate?'This posting was already saved; the existing record was updated.':'Saved to your existing jobs list.'}`;
    const postingLink=document.createElement('a');postingLink.href=data.posting_url;postingLink.target='_blank';postingLink.rel='noopener noreferrer';postingLink.textContent='View original posting';details.append(document.createElement('br'),postingLink);
    status.textContent='Import complete. Matching and dry-run actions use your existing authorizations and eligibility checks.';
    renderProgress().then(state=>{if(state&&['queued','running','paused'].includes(state.status))startPolling(state.session_id)}).catch(()=>{});
    document.getElementById('enrichImport').hidden=!!data.description_available;document.getElementById('analyzeImport').hidden=!data.description_available;document.getElementById('reviewImportLabel').hidden=false;document.getElementById('prepareImport').hidden=false;
  }catch(error){status.className='error';status.textContent=error.message}
  finally{button.disabled=false}
});
document.getElementById('enrichImport').addEventListener('click',async()=>{
  if(!importedJobKey)return;const button=document.getElementById('enrichImport');button.disabled=true;
  try{const response=await fetch('/api/jobs/enrich-descriptions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({job_keys:[importedJobKey],force:false})});const data=await readApiResponse(response,'Description enrichment could not start.');const item=data.results?.[0];document.getElementById('importStatus').textContent=item?.status==='succeeded'?`Description retrieved from ${item.source}. Run AI analysis when ready.`:item?.reason||'No public description was available.';if(item?.status==='succeeded'){document.getElementById('enrichImport').hidden=true;document.getElementById('analyzeImport').hidden=false}}
  catch(error){document.getElementById('importStatus').className='error';document.getElementById('importStatus').textContent=error.message}
  finally{button.disabled=false}
});
document.getElementById('analyzeImport').addEventListener('click',async()=>{
  if(!importedJobKey)return;const button=document.getElementById('analyzeImport');button.disabled=true;currentMatchJobKeys=[importedJobKey];currentMatchSessionId=null;pollingGeneration++;polling=false;matchStatus.className='';matchStatus.textContent='Starting AI analysis for the imported job…';recoverButton.hidden=true;
  try{const response=await fetch('/api/jobs/analyze-matches',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({job_keys:[importedJobKey],batch_size:3})});const data=await readApiResponse(response,'Could not start AI analysis.');if(!data.session_id&&data.status!=='completed')throw new Error('The server did not create a matching session for this job. Please retry.');currentMatchSessionId=data.session_id||null;matchStatus.textContent='Analysis queued for this job only. Progress and eligibility will appear below.';startPolling(currentMatchSessionId)}
  catch(error){matchStatus.className='error';matchStatus.textContent=error.message;const recovery=error.recovery;if(recovery?.recovery_available&&recovery?.same_jobs&&recovery?.session_id){currentMatchSessionId=recovery.session_id;recoverButton.hidden=false;recoverButton.dataset.sessionId=recovery.session_id}}
  finally{button.disabled=false}
});
document.getElementById('prepareImport').addEventListener('click',async()=>{
  if(!importedJobKey)return;if(!document.getElementById('reviewImport').checked){document.getElementById('importStatus').className='error';document.getElementById('importStatus').textContent='Review the job and confirm before requesting a dry run.';return}
  const button=document.getElementById('prepareImport');button.disabled=true;
  try{let response=await fetch(`/api/applications/saved-jobs/${encodeURIComponent(importedJobKey)}/review`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reviewed:true})});await readApiResponse(response,'Eligibility review could not be saved.');response=await fetch('/api/applications/prepare',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({job_key:importedJobKey})});const data=await readApiResponse(response,'Dry-run preparation is not available for this job.');document.getElementById('importStatus').textContent=data.message||`Dry run status: ${data.status}. Live submission is disabled.`}
  catch(error){document.getElementById('importStatus').className='error';document.getElementById('importStatus').textContent=error.message}
  finally{button.disabled=false}
});

function renderCoverage(){const root=document.getElementById('coverageMetrics');if(!currentJobs.length){root.hidden=true;return}const count=f=>currentJobs.filter(f).length,early=new Set(['New Graduate','Early Career','Graduate Eligible']);const values=[['Newly discovered',count(j=>j.discovery_status==='newly_discovered')],['Previously seen',count(j=>j.discovery_status==='previously_seen')],['Early-career evidence',count(j=>early.has(j.experience_level))],['Needs review',count(j=>(j.eligibility_classification||'needs_review')==='needs_review')],['Confirmed ineligible / filter conflicts',count(j=>j.eligibility_classification==='not_eligible')+Number(currentDiagnostics.experience_mismatches||0)+Number(currentDiagnostics.geographic_mismatches||0)+Number(currentDiagnostics.rejected_by_filter?.employment_type||0)+Number(currentDiagnostics.rejected_by_filter?.work_arrangement||0)+Number(currentDiagnostics.rejected_by_filter?.instructions||0)],['Descriptions missing',count(j=>!j.description_available)],['Failed employer sources',(currentDiagnostics.feed_failures||[]).length]];root.hidden=false;root.replaceChildren(...values.map(([label,value])=>{const item=document.createElement('span');item.textContent=`${label}: ${value}`;return item}))}

function renderJobs(){
  const text=(document.getElementById('filterText').value||'').toLowerCase();
  const company=document.getElementById('filterCompany').value;
  const category=document.getElementById('filterCategory').value;
  const location=(document.getElementById('filterLocation').value||'').toLowerCase();
  const level=document.getElementById('filterExperience').value;
  const arrangement=document.getElementById('filterArrangement').value;
  const employment=document.getElementById('filterEmployment').value;
  const source=document.getElementById('filterSource').value;
  const recency=Number(document.getElementById('filterRecency').value||0);
  const discovery=document.getElementById('filterDiscovery').value;
  const eligibility=document.getElementById('filterEligibility').value;
  const description=document.getElementById('filterDescription').value;
  results.replaceChildren();
  const visible=currentJobs.filter(job=>{
    const title=String(job.title||'').toLowerCase(),name=String(job.company||'');
    return (!text||title.includes(text)||name.toLowerCase().includes(text))&&(!company||name===company)&&
      (!category||(job.career_category||'Unknown')===category)&&(!location||String(job.location||'').toLowerCase().includes(location))&&
      (!level||(job.experience_level||'Unknown')===level)&&(!arrangement||(job.work_arrangement||'Unknown')===arrangement)&&
      (!employment||(job.employment_type||'Unknown')===employment)&&(!source||(job.source_platform||'Unknown')===source)&&
      (!discovery||(job.discovery_status||'previously_seen')===discovery)&&
      (!eligibility||(eligibility==='failed'?job.matching_status==='failed':eligibility==='not_analyzed'?['not_analyzed','stale'].includes(job.matching_status||'not_analyzed'):(job.eligibility_classification||'needs_review')===eligibility))&&
      (!description||(description==='available'?job.description_available:!job.description_available))&&
      (!recency||((job.posting_date||job.last_verified_at)&&((Date.now()-Date.parse(job.posting_date||job.last_verified_at))/(86400000)<=recency)));
  });
  for(const job of visible){const article=document.createElement('article');article.className='job';const title=document.createElement('h3');title.textContent=job.title;const locationLabel=job.location_status==='eligible'?'location verified':job.location_status==='unknown'?'location needs review':job.location_status||'location needs review';const detail=document.createElement('p');detail.textContent=`${job.company} · ${job.location||'Location unknown'} · ${job.employment_type||'Employment type unknown'} · ${locationLabel} · ${job.source_platform||'source unknown'} · Preliminary`;const level=document.createElement('p');level.className='note';level.textContent=`Experience level: ${job.experience_level||'Ambiguous'} · relevance rank ${job.relevance_score??'—'}/100. ${[...(job.experience_level_evidence||[]),...(job.ranking_explanation||[])].join(' ')}`;const why=document.createElement('p');why.className='note';why.textContent=[...new Set([...(job.review_reasons||[]),job.location_reason||''].filter(Boolean))].join(' ');const windows=document.createElement('p');windows.className='note';windows.textContent=(job.graduation_windows||[]).map(x=>`Graduation eligibility: ${x.start_year}${x.start_year===x.end_year?'':`–${x.end_year}`}`).join(' · ');const education=job.education_requirements||{},educationLine=[...(education.required||[]).map(x=>'Required: '+x),...(education.preferred||[]).map(x=>'Preferred: '+x),...(education.unclear||[]).map(x=>'Unclear: '+x)];const educationNote=document.createElement('p');educationNote.className='note';educationNote.textContent=educationLine.length?'Education evidence: '+educationLine.join(' · '):'';const link=document.createElement('a');link.href=job.job_url||job.url;link.target='_blank';link.rel='noopener noreferrer';link.textContent='View original posting';const apply=document.createElement('a');apply.href=job.url;apply.target='_blank';apply.rel='noopener noreferrer';apply.textContent='Application page';apply.style.marginLeft='14px';const classification=document.createElement('p');classification.className='note';classification.textContent=`Eligibility: ${job.eligibility_classification||'needs_review'} · matching: ${job.matching_status||'not_analyzed'}${job.matching_failure_reason?' · '+job.matching_failure_reason:''}`;const description=document.createElement('p');description.className='note';description.textContent=job.description_available?'Usable job description available':'Description missing or too incomplete for reliable eligibility matching';article.append(title,detail,level,classification,description,why);if(windows.textContent)article.append(windows);if(educationNote.textContent)article.append(educationNote);article.append(link,apply);results.append(article)}
  if(!visible.length&&currentJobs.length)results.textContent='No openings match these filters.';
}
for(const id of ['filterText','filterCompany','filterCategory','filterLocation','filterExperience','filterDiscovery','filterEligibility','filterDescription','filterArrangement','filterEmployment','filterSource','filterRecency'])document.getElementById(id).addEventListener('input',renderJobs);

button.addEventListener('click',async()=>{
  button.disabled=true;cancelSearch.hidden=false;status.textContent='Starting discovery…';status.className='';
  currentMatchSessionId=null;currentMatchJobKeys=[];pollingGeneration++;polling=false;
  results.replaceChildren();diagnostics.replaceChildren();matchResults.replaceChildren();document.getElementById('coverageMetrics').hidden=true;analyzeButton.hidden=true;currentJobKeys=[];currentJobs=[];currentDiagnostics={};document.getElementById('jobFilters').hidden=true;
  discoveryRunId=(globalThis.crypto&&crypto.randomUUID)?crypto.randomUUID():`run-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  discoveryTimer=setInterval(async()=>{try{const r=await fetch(`/api/jobs/discovery/${encodeURIComponent(discoveryRunId)}`);if(!r.ok)return;const s=await r.json();status.textContent=`Searching ${s.sources_checked} of ${s.total_sources} sources · ${s.postings_retrieved} postings retrieved${s.cancel_requested?' · cancelling after current source':''}`;}catch(_){/* final search response remains authoritative */}},700);
  try{
    const response=await fetch('/api/jobs/search',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt:document.getElementById('prompt').value.trim()||'Find jobs matching my saved preferences',run_id:discoveryRunId})});
    const data=await readApiResponse(response,'Search failed');
    const messages={cancelled:'Discovery cancelled safely; completed source results were retained.',all_sources_unavailable:'All configured job sources are unavailable.',no_matching_openings:'No matching openings were returned by the available sources.',filters_too_restrictive:'Openings were retrieved, but the current filters rejected them.',preliminary_matches:`${data.count} preliminary match${data.count===1?'':'es'} need review.`};
    status.textContent=messages[data.status]||'Search complete.';
    const d=data.diagnostics;currentDiagnostics=d||{};
    if(d){const p=document.createElement('p');p.className='diagnostics';p.textContent=`Employers configured: ${d.employers_configured??'—'} · Employers checked: ${d.companies_checked} · Sources: ${(d.source_platforms||[]).join(', ')||'none'} · Jobs discovered: ${d.postings_retrieved} · New: ${d.new_jobs??'—'} · Previously seen: ${d.previously_seen_jobs??'—'} · Relevant roles: ${d.relevant_jobs??d.preliminary_matches} · Genuine early-career candidates: ${d.genuine_early_career_candidates??0} · Experience level needs review: ${d.experience_level_needs_review??0} · Duplicates removed: ${d.duplicate_listings_removed??0} · Expired postings: ${d.expired_or_removed??0} · Unrelated excluded: ${d.rejected_unrelated??d.rejected_by_filter.role} · Geographic mismatches: ${d.geographic_mismatches??d.rejected_by_filter.location} · Confirmed experience mismatches: ${d.experience_mismatches??d.rejected_by_filter.experience_level} · Location needs review: ${d.location_needs_review??0} · Preliminary matches: ${d.preliminary_matches} · Needs review: ${d.needs_review}`;diagnostics.append(p);for(const failure of d.feed_failures){const f=document.createElement('p');f.className='note';f.textContent=`${failure.company} (${failure.source_platform||'source'}): ${failure.reason}`;diagnostics.append(f)}}
    for(const warning of data.feed_warnings||[]){const p=document.createElement('p');p.className='note';p.textContent=warning;diagnostics.append(p)}
    currentJobs=data.results||[];for(const job of currentJobs)currentJobKeys.push(job.job_key);
    document.getElementById('jobFilters').hidden=!currentJobs.length;
    const selectOptions={filterDiscovery:[...new Set(currentJobs.map(j=>j.discovery_status||'previously_seen'))],filterDescription:[...new Set(currentJobs.map(j=>j.description_available?'available':'missing'))],filterCompany:[...new Set(currentJobs.map(j=>j.company).filter(Boolean))],filterCategory:[...new Set(currentJobs.map(j=>j.career_category||'Unknown'))],filterExperience:[...new Set(currentJobs.map(j=>j.experience_level||'Unknown'))],filterArrangement:[...new Set(currentJobs.map(j=>j.work_arrangement||'Unknown'))],filterEmployment:[...new Set(currentJobs.map(j=>j.employment_type||'Unknown'))],filterSource:[...new Set(currentJobs.map(j=>j.source_platform||'Unknown'))]};
    for(const [id,values] of Object.entries(selectOptions)){const select=document.getElementById(id),label=select.options[0].textContent;select.replaceChildren(new Option(label,''));for(const value of values.sort())select.add(new Option(value,value));}
    renderCoverage();renderJobs();
    analyzeButton.hidden=currentJobKeys.length===0;matchStatus.textContent='';
  }catch(error){status.textContent=error.message;status.className='error'}finally{button.disabled=false;cancelSearch.hidden=true;clearInterval(discoveryTimer)}
});

cancelSearch.addEventListener('click',async()=>{if(!discoveryRunId)return;cancelSearch.disabled=true;status.textContent='Cancelling after the current public feed request…';try{await fetch(`/api/jobs/discovery/${encodeURIComponent(discoveryRunId)}/cancel`,{method:'POST'})}catch(_){status.textContent='Cancellation request could not be sent; the current request will finish normally.'}finally{cancelSearch.disabled=false}});

analyzeButton.addEventListener('click',async()=>{
  if(!currentJobKeys.length)return;
  analyzeButton.disabled=true;currentMatchJobKeys=[...currentJobKeys];currentMatchSessionId=null;pollingGeneration++;polling=false;matchStatus.textContent='Starting matching for the selected jobs…';matchStatus.className='';recoverButton.hidden=true;matchResults.replaceChildren();
  try{
    const response=await fetch('/api/jobs/analyze-matches',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({job_keys:currentJobKeys,batch_size:Number(document.getElementById('batchSize').value)})});
    const data=await readApiResponse(response,'Could not queue match analysis');if(!data.session_id&&data.status!=='completed')throw new Error('The server did not create a matching session. Please retry.');
    currentMatchSessionId=data.session_id||null;startPolling(currentMatchSessionId);
  }catch(error){matchStatus.textContent=error.message;matchStatus.className='error';const recovery=error.recovery;if(recovery?.recovery_available&&recovery?.same_jobs&&recovery?.session_id){currentMatchSessionId=recovery.session_id;recoverButton.hidden=false;recoverButton.dataset.sessionId=recovery.session_id}}finally{analyzeButton.disabled=false}
});

function matchProgressUrl(){if(currentMatchSessionId){const scope=currentMatchJobKeys.length?`&job_keys=${encodeURIComponent(currentMatchJobKeys.join(','))}`:'';return `/api/jobs/match-progress?session_id=${encodeURIComponent(currentMatchSessionId)}${scope}`}if(currentMatchJobKeys.length)return `/api/jobs/match-progress?job_keys=${encodeURIComponent(currentMatchJobKeys.join(','))}`;return null}
async function renderProgress(){
  const url=matchProgressUrl();if(!url)return null;const poll=await fetch(url);const state=await readApiResponse(poll,'Could not retrieve analysis status');if(state.session_id)currentMatchSessionId=state.session_id;
  if(!state.session_id&&state.status==='idle'){pauseButton.hidden=true;resumeButton.hidden=true;recoverButton.hidden=true;return state}
  const currentLabel=state.current_job?` · Current: ${state.current_job.company} — ${state.current_job.title}${state.current_elapsed_seconds!==null?` (${state.current_elapsed_seconds}s)`:''}`:'';
  const pauseLabel=state.status==='paused'?` · Paused: ${state.error||'Checkpoint saved; Resume continues this session without rediscovery.'}`:state.worker_health==='stalled_recoverable'?' · Worker appears abandoned; safe recovery is available.':'';
  const failureLabel=state.error&&state.status!=='paused'?` · ${state.error}`:'';
  matchStatus.textContent=`AI analysis: ${state.completed+state.failed+state.excluded} of ${state.total} processed · ${state.pending} pending · Potential matches ${state.potential_match} · Needs review ${state.needs_review} · Not eligible ${state.not_eligible} · Excluded as unrelated ${state.excluded} · Status: ${state.status}${currentLabel}${pauseLabel}${failureLabel}`;const resultByKey=new Map((state.results||[]).filter(x=>x.job_key).map(x=>[x.job_key,x]));currentJobs=currentJobs.map(job=>{const match=resultByKey.get(job.job_key);return match?{...job,matching_status:match.stale?'stale':match.status,eligibility_classification:match.stale?'needs_review':(match.classification||'needs_review'),matching_failure_reason:match.error||''}:job});renderCoverage();renderJobs();
  pauseButton.hidden=state.status!=='running'&&state.status!=='queued';resumeButton.hidden=state.status!=='paused';recoverButton.hidden=!state.recovery_available||!state.session_id;recoverButton.dataset.sessionId=state.session_id||'';
  matchResults.replaceChildren();
      if(state.status==='failed'||state.status==='interrupted'||state.failed>0){const retry=document.createElement('p');retry.className='error';retry.textContent='This matching attempt did not finish. Completed results are preserved; use Analyze with AI to retry this selected job.';matchResults.append(retry)}
      for(const item of state.results||[]){
        const article=document.createElement('article');article.className='job';
        const heading=document.createElement('h3');heading.textContent=`${item.title||'Job'} · ${item.status==='completed'?item.classification:item.status==='excluded'?'unrelated':item.status}`;article.append(heading);
        const company=document.createElement('p');company.textContent=`${item.company||''} · ${item.location||'Location unknown'} · ${item.employment_type||'Employment type unknown'} · Location ${item.location_status||'needs review'}`;article.append(company);
        if(item.url){const link=document.createElement('a');link.href=item.url;link.target='_blank';link.rel='noopener noreferrer';link.textContent='View posting';article.append(link)}
        if(item.status==='failed'||item.status==='excluded'){const message=document.createElement('p');message.className=item.status==='failed'?'error':'note';message.textContent=item.error||'Analysis unavailable';article.append(message)}
        else if(item.match){
          const score=document.createElement('p');score.textContent=`Match score: ${item.match.match_score}/100 · Ranking aid only`;
          const explanation=document.createElement('p');explanation.textContent=`Evidence-based assessment: ${item.match.explanation}`;article.append(score,explanation);
          for(const field of ['evidence_consistency','strengths','gaps','missing_information']){if(item.match[field]?.length){const p=document.createElement('p');p.className=field==='evidence_consistency'?'error':'note';p.textContent=(field==='missing_information'?'Missing information':field==='evidence_consistency'?'Evidence consistency review':field[0].toUpperCase()+field.slice(1))+': '+item.match[field].join('; ');article.append(p)}}
          for(const field of ['required_skills','preferred_skills','required_experience','preferred_experience','education_requirements','preferred_education_requirements','education_assessment','gpa_assessment','graduation_assessment','experience_assessment','experience_level_assessment','location_assessment','employment_type_assessment']){const value=item.match[field];if(value===undefined)continue;const p=document.createElement('p');p.className='note';p.textContent=field.replaceAll('_',' ')+': '+(Array.isArray(value)?value.map(e=>`${e.requirement} (${e.status}; profile evidence: ${e.candidate_evidence.length?e.candidate_evidence.join(', '):'not provided'})`).join('; '):value);article.append(p)}
        }
        matchResults.append(article);
      }
}
async function startPolling(sessionId=currentMatchSessionId){if(sessionId)currentMatchSessionId=sessionId;const generation=++pollingGeneration;polling=true;try{while(polling&&generation===pollingGeneration){const state=await renderProgress();if(!state||['idle','completed','failed','cancelled','interrupted','paused'].includes(state.status))break;await new Promise(resolve=>setTimeout(resolve,1300));}}catch(error){matchStatus.textContent=error.message;matchStatus.className='error'}finally{if(generation===pollingGeneration)polling=false}}
pauseButton.addEventListener('click',async()=>{if(!currentMatchSessionId)return;const url=`/api/jobs/matches/pause?session_id=${encodeURIComponent(currentMatchSessionId)}`;try{await readApiResponse(await fetch(url,{method:'POST'}),'Could not pause analysis');await renderProgress()}catch(error){matchStatus.textContent=error.message;matchStatus.className='error'}});
resumeButton.addEventListener('click',async()=>{if(!currentMatchSessionId)return;const url=`/api/jobs/matches/resume?session_id=${encodeURIComponent(currentMatchSessionId)}`;try{await readApiResponse(await fetch(url,{method:'POST'}),'Could not resume analysis');startPolling(currentMatchSessionId)}catch(error){matchStatus.textContent=error.message;matchStatus.className='error'}});
recoverButton.addEventListener('click',async()=>{const sessionId=recoverButton.dataset.sessionId;if(!sessionId||!confirm('Confirm this selected matching worker is abandoned. Recovery preserves completed results and does not resume matching automatically.'))return;recoverButton.disabled=true;try{await readApiResponse(await fetch(`/api/jobs/matches/${encodeURIComponent(sessionId)}/recover`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirm_abandoned:true})}),'Could not recover the selected checkpoint');currentMatchSessionId=null;recoverButton.hidden=true;matchStatus.textContent='Abandoned checkpoint recovered. Completed results are preserved; click Analyze with AI to retry the remaining selected job.'}catch(error){matchStatus.textContent=error.message;matchStatus.className='error'}finally{recoverButton.disabled=false}});
</script></body></html>'''
