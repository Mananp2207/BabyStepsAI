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
from job_relevance import (evaluate_location, extract_experience_requirements,
                           infer_prompt_preferences, normalize_job_record, relevance_for_job,
                           classify_experience_level, discovery_relevance_score,
                           extract_graduation_windows, extract_education_requirements,
                           classify_career_category)
from ui_theme import apply_ui_theme
from job_connectors import (EmployerSource, canonicalize_job_url, discover_source_candidates, fetch_source,
                            normalize_job, observe_fetch, validate_public_https_url)

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
            ("career_category","TEXT"),("experience_level","TEXT"),("salary_json","TEXT"),
            ("required_qualifications_json","TEXT"),("preferred_qualifications_json","TEXT"),
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
    if job.get("employment_type"):
        return str(job["employment_type"]).casefold()
    metadata = job.get("metadata") if isinstance(job.get("metadata"), list) else []
    values = [str(item.get("value", "")) for item in metadata
              if isinstance(item, dict) and "employment" in str(item.get("name", "")).casefold()]
    return " ".join(values).casefold()


def _confirmed_professional_experience_years():
    """Read only an explicitly confirmed numeric experience statement, if present."""
    with database() as db:
        table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='application_answers'").fetchone()
        if not table:
            return None
        rows = db.execute("""SELECT answer FROM application_answers WHERE user_id=?
            AND question_key IN ('professional_experience','custom:professional_experience')
            AND confirmed=1 AND sensitive=0 AND revoked_at IS NULL""", (USER,)).fetchall()
    for row in rows:
        match = re.search(r"\b(\d{1,2}(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)\b", str(row["answer"] or ""), re.I)
        if match:
            return float(match.group(1))
    return None


def _confirmed_candidate_skills():
    """Load only user-confirmed ordinary skills for local ranking."""
    with database() as db:
        resume_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='resumes'").fetchone()
        verification_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='candidate_verifications'").fetchone()
        resume = db.execute("SELECT id FROM resumes WHERE user_id=? AND active=1 ORDER BY created_at DESC LIMIT 1", (USER,)).fetchone() if resume_table else None
        verification = db.execute("SELECT values_json,confirmed_json FROM candidate_verifications WHERE user_id=? AND resume_id=?", (USER,resume["id"])).fetchone() if resume and verification_table else None
        answer_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='application_answers'").fetchone()
        answers = db.execute("""SELECT answer FROM application_answers WHERE user_id=? AND resume_id=?
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
        skills.extend(re.split(r"[,;\n]", str(row["answer"] or "")))
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

    geo_status, geo_reason = evaluate_location(job, prefs.get("location", ""))
    if geo_status == "mismatch":
        return False, "location", []
    if geo_status == "unknown":
        review_reasons.append(geo_reason)

    arrangement = str(prefs.get("work_arrangement", "Any")).casefold()
    if arrangement in ("remote", "hybrid", "on-site"):
        marker = "onsite" if arrangement == "on-site" else arrangement
        known_arrangements = ("remote", "hybrid", "onsite", "on-site", "in-office", "in office")
        observed_arrangement = any(value in evidence for value in known_arrangements)
        if observed_arrangement and marker not in evidence:
            return False, "work_arrangement", []
        if not observed_arrangement:
            review_reasons.append(f"Work arrangement ({arrangement}) is not clear from the feed.")

    instructions = str(prefs.get("instructions", "")).casefold()
    if ("remote only" in instructions or "must be remote" in instructions) and "remote" not in evidence:
        if any(value in evidence for value in ("hybrid", "onsite", "on-site", "in-office", "in office")):
            return False, "instructions", []
        review_reasons.append("Remote-only instruction could not be verified.")
    if "exclude remote" in instructions and "remote" in evidence:
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


def discover(prompt, prefs, run_id=None, workflow_id=None):
    """Serialize local feed runs and make unexpected interruption resumable."""
    if not _DISCOVERY_LOCK.acquire(blocking=False):
        raise HTTPException(409,"Another employer search is still running. Wait for its checkpoint or cancel it before starting another.")
    run_id=run_id or str(uuid.uuid4())
    try:
        return _discover(prompt,prefs,run_id,workflow_id)
    finally:
        _DISCOVERY_LOCK.release()


def _discover(prompt, prefs, run_id, workflow_id=None):
    prefs = infer_prompt_preferences(prompt, prefs)
    terms = search_terms(prompt, prefs)
    if not terms:
        raise HTTPException(422, "Add a role or career category in Career Preferences, or include a role in your search prompt.")

    sources = _employer_sources()
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
    confirmed_experience_years = _confirmed_professional_experience_years()
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
            if not error and source.source_type in {"greenhouse", "lever", "ashby", "career_page"}:
                present = {}
                for raw in board_jobs:
                    if isinstance(raw, dict):
                        try:
                            normalized=normalize_job(source.source_type,source,raw)
                            present[normalized["job_key"]]=bool(normalized["active"])
                        except (TypeError,ValueError):
                            continue
                prefix=f"{source.source_type}:{source.token}:%"
                with database() as db:
                    db.execute("UPDATE discovered_openings SET active=0 WHERE source_platform=? AND job_key LIKE ?",
                               (source.source_type,prefix))
                    for known_key,is_active in present.items():
                        db.execute("UPDATE discovered_openings SET active=? WHERE user_id=? AND source_platform=? AND job_key=?",
                                   (int(is_active),USER,source.source_type,known_key))
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
                    prior=db.execute("SELECT content_fingerprint FROM discovered_openings WHERE user_id=? AND job_key=?",
                                     (USER,record["job_key"])).fetchone()
                    was_previously_seen=bool(prior)
                    if prior:
                        diagnostics["previously_seen_jobs"] += 1
                    else:
                        diagnostics["new_jobs"] += 1
                    if prior:
                        changed=bool(prior["content_fingerprint"] and prior["content_fingerprint"]!=record["content_fingerprint"])
                        db.execute("""UPDATE discovered_openings SET company=?,title=?,location=?,url=?,employment_type=?,description=?,
                            country=?,work_arrangement=?,posting_date=?,last_verified_at=?,active=?,content_fingerprint=?,job_url=?,salary_json=?
                            WHERE user_id=? AND job_key=?""",
                            (source.name,record["title"],record["location"] or "Unspecified",record["application_url"],record["employment_type"],
                             record["description"],record["country"],record["work_arrangement"],record["posting_date"],record["last_verified_at"],
                             int(record["active"]),record["content_fingerprint"],record["job_url"],json.dumps(record["salary"]) if record["salary"] is not None else None,USER,record["job_key"]))
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
                experience_level, experience_level_evidence = classify_experience_level(
                    job["title"], record["description"], employment_type)
                record["experience_level"] = experience_level
                record["career_category"] = classify_career_category(job["title"], prefs)
                if experience_level_evidence and experience_level == "Ambiguous":
                    review_reasons.append(experience_level_evidence[0])
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
                    "review_status": "needs_review",
                    "review_reasons": review_reasons or ["Verify job details and eligibility before applying."],
                    "discovery_source": "Company Careers Page",
                    "source_platform": source.source_type, "source_id": record["source_id"],
                    "country": record["country"], "work_arrangement": record["work_arrangement"],
                    "description": record["description"], "posting_date": record["posting_date"],
                    "last_verified_at": record["last_verified_at"], "active": 1,
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
                 salary_json,career_category,experience_level,required_qualifications_json,preferred_qualifications_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,job_key) DO UPDATE SET
                company=excluded.company,title=excluded.title,location=excluded.location,url=excluded.url,
                employment_type=excluded.employment_type,location_status=excluded.location_status,location_reason=excluded.location_reason,
                search_prompt=excluded.search_prompt,discovery_source=excluded.discovery_source,
                source_platform=excluded.source_platform,source_id=excluded.source_id,country=excluded.country,
                work_arrangement=excluded.work_arrangement,description=excluded.description,posting_date=excluded.posting_date,
                last_verified_at=excluded.last_verified_at,active=1,content_fingerprint=excluded.content_fingerprint,job_url=excluded.job_url,
                salary_json=excluded.salary_json,career_category=excluded.career_category,experience_level=excluded.experience_level,
                required_qualifications_json=excluded.required_qualifications_json,preferred_qualifications_json=excluded.preferred_qualifications_json""",
                       (USER, item["job_key"], item["company"], item["title"], item["location"], item["url"], timestamp,
                        item["employment_type"], item["location_status"], item["location_reason"], item["search_prompt"],item["discovery_source"],
                        item["source_platform"],item["source_id"],item["country"],item["work_arrangement"],item["description"],item["posting_date"],
                        item["last_verified_at"],item["active"],item["content_fingerprint"],item["job_url"],item["salary_json"],item["career_category"],item["experience_level"],item["required_qualifications_json"],item["preferred_qualifications_json"]))
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
                      career_category=result.get("career_category") or classify_career_category(result.get("title"), prefs),
                      relevance_score=item.get("relevance_score",score),
                      ranking_explanation=item.get("ranking_explanation",ranking_explanation),
                      experience_level_evidence=item.get("experience_level_evidence",level_evidence),
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
                item["matching_failure_reason"]=match.get("error") or ""
        except (ImportError,sqlite3.Error,KeyError,TypeError,ValueError,HTTPException):
            # Search results must remain available if the optional saved-match
            # diagnostic cannot be read in a partially migrated database.
            pass
    results.sort(key=lambda item: (-int(item.get("relevance_score", 50)),
                                   str(item.get("posting_date") or ""),
                                   str(item.get("company") or "").casefold()))
    diagnostics["genuine_early_career_candidates"] = sum(
        item.get("experience_level") in {"New Graduate", "Early Career", "Internship", "Graduate Eligible"}
        for item in results)
    diagnostics["experience_level_needs_review"] = sum(
        item.get("experience_level") in {"Ambiguous", "Mid-Level"} for item in results)
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
    return apply_ui_theme(PAGE)


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
    return discover(request.prompt, prefs, request.run_id)


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
            source_platform,source_id,country,work_arrangement,posting_date,last_verified_at,active,job_url,career_category,experience_level,salary_json
            FROM discovered_openings WHERE user_id=? ORDER BY discovered_at DESC""", (USER,)).fetchall()
    return {"results": [{**dict(row), "review_status": "needs_review"} for row in rows], "count": len(rows)}


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
    opening_counts={};current_counts={};early_counts={};missing_description_counts={}
    for opening in openings:
        company=(opening["company"] or "").casefold()
        opening_counts[company]=opening_counts.get(company,0)+1
        matching_source=next((row for row in eligible_sources if row["source_type"]==opening["source_platform"] and
            row["name"].casefold()==company and opening["job_key"]==f"{row['source_type']}:{row['source_token']}:{opening['source_id']}"),None)
        verified=bool(opening["active"] and opening["last_verified_at"] and opening["last_verified_at"]>=cutoff and matching_source)
        if not verified:continue
        current_counts[company]=current_counts.get(company,0)+1
        level=classify_experience_level(opening["title"],opening["description"],opening["employment_type"])[0]
        if level in {"New Graduate","Early Career","Graduate Eligible"}:
            early_counts[company]=early_counts.get(company,0)+1
        if not usable_posting_description(opening["description"]):
            missing_description_counts[company]=missing_description_counts.get(company,0)+1
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
        group["current_missing_descriptions_count"]=missing_description_counts.get(company_key,0)
        group["active"]=group["active"] or bool(item["active"])
        if not group["careers_url"]:group["careers_url"]=item["careers_url"]
        try: evidence=json.loads(item["verification_evidence_json"] or "{}")
        except (TypeError,json.JSONDecodeError): evidence={}
        group["sources"].append({"employer_id":item["employer_id"],**{field:item[field] for field in ("source_type","source_token","public_feed_url","last_success_at","last_attempt_at","connector_status","error_details","active","candidate_source","candidate_record_url","candidate_license","discovered_at","discovery_status","health_reason","consecutive_failures","next_retry_at","force_revalidate")},
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
    summary={"employers":len({row["employer_id"] for row in rows}),
        "sources":len(rows),"active_employers":len({row["employer_id"] for row in rows if row["active"]}),
        "active_sources":sum(bool(row["active"]) for row in rows),
        "healthy_verified_sources":sum(bool(row["active"] and row["connector_status"]=="healthy" and
            row["discovery_status"]=="verified" and row["last_success_at"]) for row in rows),
        "eligible_discovery_sources":len(eligible_sources),"unsupported_sources":unsupported,
        "awaiting_verification_sources":awaiting,"scheduled_retry_sources":scheduled,
        "saved_openings":len(openings),"current_verified_openings":sum(current_counts.values()),
        "current_early_career_openings":sum(early_counts.values()),
        "current_missing_descriptions":sum(missing_description_counts.values()),
        "current_opening_window_days":30}
    return {"results": list(grouped.values()), "count": len(grouped),"summary":summary}


@router.get("/api/employer-candidates")
def list_employer_candidates(request: Request):
    _require_local_admin(request)
    with database() as db:
        rows=db.execute("SELECT candidate_id,name,website_url,careers_url,evidence_url,candidate_source,candidate_record_url,candidate_license,discovered_at,last_attempt_at,status,diagnostics_json FROM employer_candidate_seeds ORDER BY discovered_at DESC").fetchall()
    results=[]
    for row in rows:
        item=dict(row)
        try:item["diagnostics"]=json.loads(item.pop("diagnostics_json") or "{}")
        except (TypeError,json.JSONDecodeError):item["diagnostics"]={}
        with database() as db:
            provenance=db.execute("SELECT provider,record_url,evidence_url,access_terms,discovered_at FROM employer_candidate_provenance WHERE candidate_id=? ORDER BY discovered_at",(item["candidate_id"],)).fetchall()
        item["provenance"]=[dict(p) for p in provenance]
        results.append(item)
    return {"results":results,"count":len(results)}


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
    return HTMLResponse(EMPLOYER_PAGE)


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
 $('list').replaceChildren();for(const e of d.results){const visible=e.sources.filter(s=>{const hrs=Number($('verifyWindow').value||0),verified=Date.parse(s.last_success_at||'');const dateOk=!hrs||(Number.isFinite(verified)&&(Date.now()-verified)<=hrs*3600000);const term=$('failureFilter').value.trim().toLowerCase();return (!$('sourceProvider').value||s.source_type===$('sourceProvider').value)&&(!$('sourceStatus').value||s.connector_status===$('sourceStatus').value)&&dateOk&&(!term||`${s.health_reason||''} ${s.error_details||''}`.toLowerCase().includes(term))});if(!visible.length)continue;const a=document.createElement('article');const h=document.createElement('h3');h.textContent=e.name;a.append(h);const coverage=document.createElement('p');coverage.textContent=`Saved openings ${e.saved_openings_count||0} · Current verified ${e.current_openings_count||0} · Early-career evidence ${e.early_career_current_openings_count||0} · Missing descriptions ${e.current_missing_descriptions_count||0}`;a.append(coverage);for(const s of visible){const p=document.createElement('p');p.textContent=`${s.source_type} ${s.source_token||''} · ${s.connector_status} (${s.health_reason||'not checked'}) · ${s.active?'active':'inactive'} · Jobs observed ${s.verification_evidence?.postings_observed||0} · Last verified ${s.last_success_at||'never'} · Last attempt ${s.last_attempt_at||'never'} · Next retry ${s.next_retry_at||'eligible'} · Failures ${s.consecutive_failures||0}${s.error_details?` · ${s.error_details}`:''}`;a.append(p);const stage=document.createElement('small');const ev=s.verification_evidence||{};stage.textContent='Evidence: '+(e.evidence_url||s.candidate_record_url||'not recorded')+(ev.recognized_ats_provider?` · ATS ${ev.recognized_ats_provider}`:'')+(ev.http_responses?.length?` · HTTP ${ev.http_responses.map(x=>`${x.host}:${x.status_code}`).join(', ')}`:'')+' · Stages: '+(s.stages||[]).map(x=>`${x.stage_key}=${x.status}`).join(' · ');a.append(stage);const validate=document.createElement('button');validate.textContent='Validate when eligible';validate.onclick=async()=>{try{const x=await api(`/api/employers/${encodeURIComponent(s.employer_id)}/validate`,{method:'POST'});$('status').textContent=x.message;await load()}catch(x){$('status').textContent=x.message}};a.append(validate);const force=document.createElement('button');force.textContent='Force revalidate';force.onclick=async()=>{try{const x=await api(`/api/employers/${encodeURIComponent(s.employer_id)}/validate?force=true`,{method:'POST'});$('status').textContent=x.message;await load()}catch(x){$('status').textContent=x.message}};a.append(force);if(s.active){const disable=document.createElement('button');disable.textContent='Disable';disable.onclick=async()=>{await api(`/api/employers/${encodeURIComponent(s.employer_id)}/disable`,{method:'POST'});load()};a.append(disable)}}$('list').append(a)}}
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
<body><header><h1>Find Jobs</h1><p>Search registered public job sources using your saved career preferences and a prompt.</p><a href="/">Dashboard</a> · <a href="/careers">Career preferences</a> · <a href="/applications">Applications</a> · <a href="/application-settings">Automation permissions</a></header>
<main><h2>What are you looking for?</h2><label for="prompt">Add focus or context for this search</label><textarea id="prompt" placeholder="For example: Find entry level data engineer roles in the United States"></textarea><p class="note">Results use registered public job sources. Matches are preliminary; eligibility must be analyzed and reviewed. Applications are never submitted automatically.</p><button id="search">Find Jobs</button><button id="cancelSearch" hidden>Cancel search</button><button id="analyze" hidden>Analyze Matches</button><label for="batchSize">Jobs per AI batch</label> <select id="batchSize"><option value="3">3</option><option value="4">4</option><option value="5">5</option></select><button id="pause" hidden>Pause Analysis</button><button id="resume" hidden>Resume Analysis</button><p id="status" role="status"></p><p id="matchStatus" role="status"></p><div id="diagnostics"></div><div id="coverageMetrics" class="diagnostics" hidden></div><div id="jobFilters" hidden><label>Search title/company <input id="filterText" type="search"></label> <label>Company <select id="filterCompany"><option value="">All companies</option></select></label> <label>Category <select id="filterCategory"><option value="">All categories</option></select></label> <label>Location <input id="filterLocation" type="search" placeholder="City or country"></label> <label>Experience <select id="filterExperience"><option value="">All levels</option></select></label> <label>Discovery status <select id="filterDiscovery"><option value="">New and previously seen</option><option value="newly_discovered">Newly discovered</option><option value="previously_seen">Previously seen</option></select></label> <label>Eligibility <select id="filterEligibility"><option value="">All classifications</option><option value="needs_review">Needs review</option><option value="potential_match">Potential match</option><option value="not_eligible">Not eligible</option><option value="failed">Failed analysis</option><option value="not_analyzed">Not analyzed</option></select></label> <label>Description <select id="filterDescription"><option value="">Any description status</option><option value="available">Available</option><option value="missing">Missing or incomplete</option></select></label> <label>Work arrangement <select id="filterArrangement"><option value="">All arrangements</option></select></label> <label>Employment type <select id="filterEmployment"><option value="">All employment types</option></select></label> <label>Source <select id="filterSource"><option value="">All sources</option></select></label> <label>Posting recency <select id="filterRecency"><option value="">Any date</option><option value="7">Last 7 days</option><option value="30">Last 30 days</option><option value="90">Last 90 days</option></select></label></div><div id="results"></div><div id="matchResults"></div></main>
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
let currentJobKeys=[];
let currentJobs=[];
let polling=false;
let discoveryRunId=null;
let discoveryTimer=null;
let currentDiagnostics={};

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
  results.replaceChildren();diagnostics.replaceChildren();matchResults.replaceChildren();document.getElementById('coverageMetrics').hidden=true;analyzeButton.hidden=true;currentJobKeys=[];currentJobs=[];currentDiagnostics={};document.getElementById('jobFilters').hidden=true;
  discoveryRunId=(globalThis.crypto&&crypto.randomUUID)?crypto.randomUUID():`run-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  discoveryTimer=setInterval(async()=>{try{const r=await fetch(`/api/jobs/discovery/${encodeURIComponent(discoveryRunId)}`);if(!r.ok)return;const s=await r.json();status.textContent=`Searching ${s.sources_checked} of ${s.total_sources} sources · ${s.postings_retrieved} postings retrieved${s.cancel_requested?' · cancelling after current source':''}`;}catch(_){/* final search response remains authoritative */}},700);
  try{
    const response=await fetch('/api/jobs/search',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt:document.getElementById('prompt').value.trim()||'Find jobs matching my saved preferences',run_id:discoveryRunId})});
    const data=await response.json();if(!response.ok)throw new Error(data.detail||'Search failed');
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
  analyzeButton.disabled=true;matchStatus.textContent='Starting sequential local Ollama analysis…';matchResults.replaceChildren();
  try{
    const response=await fetch('/api/jobs/analyze-matches',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({job_keys:currentJobKeys,batch_size:Number(document.getElementById('batchSize').value)})});
    const data=await response.json();if(!response.ok)throw new Error(data.detail||'Could not queue match analysis');
    startPolling();
  }catch(error){matchStatus.textContent=error.message;matchStatus.className='error'}finally{analyzeButton.disabled=false}
});

async function renderProgress(){
  const poll=await fetch('/api/jobs/match-progress');const state=await poll.json();if(!poll.ok)throw new Error(state.detail||'Could not retrieve analysis status');
  matchStatus.textContent=`AI analysis: ${state.completed+state.failed+state.excluded} of ${state.total} processed · ${state.pending} pending · Potential matches ${state.potential_match} · Needs review ${state.needs_review} · Not eligible ${state.not_eligible} · Excluded as unrelated ${state.excluded} · Status: ${state.status}`;const resultByKey=new Map((state.results||[]).filter(x=>x.job_key).map(x=>[x.job_key,x]));currentJobs=currentJobs.map(job=>{const match=resultByKey.get(job.job_key);return match?{...job,matching_status:match.stale?'stale':match.status,eligibility_classification:match.stale?'needs_review':(match.classification||'needs_review'),matching_failure_reason:match.error||''}:job});renderCoverage();renderJobs();
  pauseButton.hidden=state.status!=='running'&&state.status!=='queued';resumeButton.hidden=state.status!=='paused';
  matchResults.replaceChildren();
      for(const item of state.results||[]){
        const article=document.createElement('article');article.className='job';
        const heading=document.createElement('h3');heading.textContent=`${item.title||'Job'} · ${item.status==='completed'?item.classification:item.status==='excluded'?'unrelated':item.status}`;article.append(heading);
        const company=document.createElement('p');company.textContent=`${item.company||''} · ${item.location||'Location unknown'} · ${item.employment_type||'Employment type unknown'} · Location ${item.location_status||'needs review'}`;article.append(company);
        if(item.url){const link=document.createElement('a');link.href=item.url;link.target='_blank';link.rel='noopener noreferrer';link.textContent='View posting';article.append(link)}
        if(item.status==='failed'||item.status==='excluded'){const message=document.createElement('p');message.className=item.status==='failed'?'error':'note';message.textContent=item.error||'Analysis unavailable';article.append(message)}
        else if(item.match){
          const score=document.createElement('p');score.textContent=`Match score: ${item.match.match_score}/100 · Ranking aid only`;
          const explanation=document.createElement('p');explanation.textContent=`AI assessment (verify against the posting and your profile): ${item.match.explanation}`;article.append(score,explanation);
          for(const field of ['strengths','gaps','missing_information']){if(item.match[field]?.length){const p=document.createElement('p');p.textContent=(field==='missing_information'?'Missing information':field[0].toUpperCase()+field.slice(1))+': '+item.match[field].join('; ');article.append(p)}}
          for(const field of ['required_skills','preferred_skills','required_experience','preferred_experience','education_requirements','education_assessment','experience_assessment','experience_level_assessment','location_assessment','employment_type_assessment']){const value=item.match[field];if(value===undefined)continue;const p=document.createElement('p');p.className='note';p.textContent=field.replaceAll('_',' ')+': '+(Array.isArray(value)?value.map(e=>`${e.requirement} (${e.status}; profile evidence: ${e.candidate_evidence.length?e.candidate_evidence.join(', '):'not provided'})`).join('; '):value);article.append(p)}
        }
        matchResults.append(article);
      }
}
async function startPolling(){if(polling)return;polling=true;try{while(polling){await renderProgress();const state=await (await fetch('/api/jobs/match-progress')).json();if(['idle','completed'].includes(state.status))break;await new Promise(resolve=>setTimeout(resolve,1300));}}catch(error){matchStatus.textContent=error.message;matchStatus.className='error'}finally{polling=false}}
pauseButton.addEventListener('click',async()=>{await fetch('/api/jobs/matches/pause',{method:'POST'});await renderProgress()});
resumeButton.addEventListener('click',async()=>{const response=await fetch('/api/jobs/matches/resume',{method:'POST'});if(!response.ok){const error=await response.json();matchStatus.textContent=error.detail||'Could not resume analysis';return}startPolling()});
renderProgress().catch(()=>{});
</script></body></html>'''
