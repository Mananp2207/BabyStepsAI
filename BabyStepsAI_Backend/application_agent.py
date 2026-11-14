"""Playwright dry-run application agent; real submissions are disabled."""
import ipaddress
import html
import json
import os
import re
import socket
import ssl
import sqlite3
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.robotparser
import urllib.request
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
import hashlib
import asyncio

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from job_search_agent import USER, DB as JOB_DB, fetch_board
from job_connectors import canonicalize_job_url
from candidate_verification import (verified_profile_facts, save_application_answer, AnswerUpdate,
                                    verified_values_from_json, _answer_plaintext,
                                    canonical_answer_identity, stable_answer_key, _validate_profile_url,
                                    validate_application_answer, application_answer_value_kind)
from job_relevance import evaluate_relocation_preference, normalize_text
from application_browser import (apply_targets, classify_control, detect_application_provider,
                                 page_interruption, submission_controls, summarize_form_metadata)
from application_answer_assistant import get_authorization, generate_draft, draft_safe_profile, ORDINARY_PROMPT, SENSITIVE_PROMPT, MAX_ANSWER_CHARS
from provider_config import groq_api_key
from ui_theme import apply_ui_theme

DB = JOB_DB
ROOT = Path(__file__).resolve().parent
STORAGE = ROOT / "private_resumes"
ARTIFACTS = ROOT / "private_application_artifacts"
ARTIFACTS.mkdir(mode=0o700, exist_ok=True)
router = APIRouter()
executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="application-dry-run")
_attempt_lock = threading.RLock()
_active_attempts = set()
_queue_worker_lock = threading.RLock()
_queue_worker_running = False
_inspection_lock = threading.Lock()
_robots_cache_lock = threading.Lock()
_robots_cache = {}
_resume_fingerprint_lock=threading.Lock()
_resume_fingerprint_cache={}
ROBOTS_CACHE_TTL_SECONDS = 1800
ROBOTS_MAX_ATTEMPTS = 3
ROBOTS_MAX_BYTES = 256000
MAX_STEPS = 6
MAX_APPLICATION_ATTEMPTS = 3
MOCK_PATH_PREFIX = "/mock-apps/"

def browser_headless():
    """Default to the safe background mode; opt into a visible local browser."""
    return os.getenv("BABYSTEPS_BROWSER_HEADLESS", "true").strip().casefold() not in {"0", "false", "no", "off"}

def now(): return datetime.now(timezone.utc).isoformat()

def _opening_identity(job):
    """Stable identity for a verified ATS requisition across discovery boards."""
    key=str(job.get("job_key") or "").strip()
    pieces=key.split(":")
    provider=pieces[0].casefold() if len(pieces)>=3 else ""
    requisition=pieces[-1].strip().casefold() if provider else ""
    employer=re.sub(r"[^a-z0-9]+","",str(job.get("company") or job.get("employer") or "").casefold())
    url=canonicalize_job_url(str(job.get("application_url") or job.get("job_url") or job.get("url") or ""))
    # Different requisition IDs remain distinct. Board tokens are omitted so
    # the same ATS requisition found through another board is reserved once.
    parts={"provider":provider,"employer":employer,"requisition":requisition}
    if not provider or not requisition:
        parts={"url":url}
    digest=hashlib.sha256(json.dumps(parts,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()
    return {"hash":digest,"parts":parts,"url":url}

def _record_application_attention(job, blocker_code, reason, *, eligibility_state="needs_review",
                                  form_state="unknown", report=None):
    """Create or refresh one durable handoff per canonical opening and blocker.

    This ledger is separate from application attempts: an unsupported or blocked
    form must never look like a preparation attempt.
    """
    report=report if isinstance(report,dict) else {}
    identity=_opening_identity(job)
    posting_url=_sanitized_inspection_url(job.get("job_url") or job.get("url") or "")
    application_url=_sanitized_inspection_url(job.get("application_url") or job.get("url") or "")
    blocker_code=str(blocker_code or "human_review")[:60]
    reason=str(reason or "Review this opening manually.")[:600]
    actions={"unsupported_form":"Open the official employer application and complete it manually.",
        "human_verification":"Complete the requested human verification on the employer site, then return here.",
        "applicant_information":"Confirm the missing applicant information before preparation.",
        "employer_clarification":"Review the official posting or contact the employer to clarify the requirement.",
        "posting_review":"Verify the current posting and requisition status on the official employer site.",
        "preparation_failure":"Review the preparation details and retry only after resolving the listed issue."}
    action=actions.get(blocker_code,"Review the official employer posting and resolve this item manually.")
    stamp=now()
    form_fingerprint=str(report.get("form_fingerprint") or "")[:128]
    posting_fingerprint=str(job.get("content_fingerprint") or "")[:128]
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        prior_application=db.execute("""SELECT a.state FROM applications a WHERE a.user_id=? AND
            (a.job_key=? OR a.id=(SELECT r.application_id FROM application_opening_reservations r
                WHERE r.user_id=? AND r.identity_hash=? LIMIT 1)) ORDER BY a.updated_at DESC LIMIT 1""",
            (USER,str(job.get("job_key") or "")[:250],USER,identity["hash"])).fetchone()
        preparation_state=str(prior_application["state"] if prior_application else "not_prepared")[:40]
        current=db.execute("SELECT attention_id FROM application_attention_items WHERE user_id=? AND identity_hash=? AND blocker_code=? AND status IN ('open','acknowledged')",
            (USER,identity["hash"],blocker_code)).fetchone()
        if current:
            db.execute("""UPDATE application_attention_items SET job_key=?,employer=?,title=?,posting_url=?,application_url=?,
                reason=?,applicant_action=?,eligibility_state=?,form_state=?,preparation_state=?,form_fingerprint=?,posting_fingerprint=?,last_seen_at=?,updated_at=?
                WHERE attention_id=? AND status IN ('open','acknowledged')""",
                (str(job.get("job_key") or "")[:250],str(job.get("company") or job.get("employer") or "")[:160],
                 str(job.get("title") or "")[:200],posting_url[:2048],application_url[:2048],reason,action,
                 str(eligibility_state)[:40],str(form_state)[:40],preparation_state,form_fingerprint,posting_fingerprint,stamp,stamp,current["attention_id"]))
            return current["attention_id"]
        attention_id=str(uuid.uuid4())
        db.execute("""INSERT INTO application_attention_items(attention_id,user_id,identity_hash,job_key,employer,title,
            posting_url,application_url,blocker_code,reason,applicant_action,eligibility_state,form_state,preparation_state,
            form_fingerprint,posting_fingerprint,status,created_at,updated_at,last_seen_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open',?,?,?)""",
            (attention_id,USER,identity["hash"],str(job.get("job_key") or "")[:250],
             str(job.get("company") or job.get("employer") or "")[:160],str(job.get("title") or "")[:200],
             posting_url[:2048],application_url[:2048],blocker_code,reason,action,str(eligibility_state)[:40],
             str(form_state)[:40],preparation_state,form_fingerprint,posting_fingerprint,stamp,stamp,stamp))
    return attention_id
class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try: return super().__exit__(exc_type, exc_value, traceback)
        finally: self.close()

def connect():
    db = sqlite3.connect(DB, timeout=30, factory=ClosingConnection); db.row_factory = sqlite3.Row; return db

def initialize():
    with connect() as db:
        opening_table=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='discovered_openings'").fetchone()
        if opening_table:
            opening_columns={row[1] for row in db.execute("PRAGMA table_info(discovered_openings)")}
            for name,declaration in (("country","TEXT NOT NULL DEFAULT ''"),("work_arrangement","TEXT NOT NULL DEFAULT ''"),
                                     ("description","TEXT NOT NULL DEFAULT ''"),("posting_date","TEXT"),
                                     ("last_verified_at","TEXT"),("active","INTEGER NOT NULL DEFAULT 1"),
                                     ("content_fingerprint","TEXT NOT NULL DEFAULT ''"),("job_url","TEXT NOT NULL DEFAULT ''"),
                                     ("source_platform","TEXT NOT NULL DEFAULT 'greenhouse'"),("source_id","TEXT NOT NULL DEFAULT ''"),
                                     ("availability_status","TEXT NOT NULL DEFAULT 'unknown'"),
                                     ("career_category","TEXT"),("experience_level","TEXT"),("salary_json","TEXT"),
                                     ("required_qualifications_json","TEXT"),("preferred_qualifications_json","TEXT"),
                                     ("reopened_review_required","INTEGER NOT NULL DEFAULT 0")):
                if name not in opening_columns: db.execute(f"ALTER TABLE discovered_openings ADD COLUMN {name} {declaration}")
        db.execute("""CREATE TABLE IF NOT EXISTS applications (
            id TEXT PRIMARY KEY,user_id TEXT NOT NULL,resume_id TEXT NOT NULL,job_key TEXT NOT NULL,
            company TEXT NOT NULL,title TEXT NOT NULL,application_url TEXT NOT NULL,state TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'dry_run',diagnostic_json TEXT NOT NULL DEFAULT '{}',error TEXT,
            created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(user_id,job_key),
            FOREIGN KEY(resume_id) REFERENCES resumes(id))""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_applications_user_state ON applications(user_id,state)")
        db.execute("""CREATE TABLE IF NOT EXISTS application_opening_reservations(
            user_id TEXT NOT NULL,identity_hash TEXT NOT NULL,application_id TEXT NOT NULL,
            identity_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL,
            PRIMARY KEY(user_id,identity_hash),UNIQUE(user_id,application_id))""")
        db.execute("""CREATE TABLE IF NOT EXISTS application_opening_aliases(
            user_id TEXT NOT NULL,canonical_url TEXT NOT NULL,identity_hash TEXT NOT NULL,
            application_id TEXT NOT NULL,identity_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL,
            PRIMARY KEY(user_id,canonical_url,identity_hash))""")
        db.execute("""CREATE TABLE IF NOT EXISTS eligibility_evidence_reviews (
            review_id INTEGER PRIMARY KEY AUTOINCREMENT,user_id TEXT NOT NULL,job_key TEXT NOT NULL,
            resume_id TEXT NOT NULL,decision TEXT NOT NULL,match_updated_at TEXT NOT NULL,
            profile_fingerprint TEXT NOT NULL,preferences_fingerprint TEXT NOT NULL,
            job_fingerprint TEXT NOT NULL,matching_engine_version TEXT NOT NULL,
            details_json TEXT NOT NULL DEFAULT '{}',reviewed_at TEXT NOT NULL)""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_evidence_reviews_job ON eligibility_evidence_reviews(user_id,job_key,resume_id,reviewed_at DESC)")
        db.execute("""CREATE TABLE IF NOT EXISTS application_eligibility_reviews (
            user_id TEXT NOT NULL, job_key TEXT NOT NULL, resume_id TEXT NOT NULL,
            classification TEXT NOT NULL, reviewed_at TEXT NOT NULL,match_updated_at TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(user_id,job_key,resume_id))""")
        review_columns={row[1] for row in db.execute("PRAGMA table_info(application_eligibility_reviews)")}
        if "match_updated_at" not in review_columns:
            db.execute("ALTER TABLE application_eligibility_reviews ADD COLUMN match_updated_at TEXT NOT NULL DEFAULT ''")
        if "decision" not in review_columns:
            db.execute("ALTER TABLE application_eligibility_reviews ADD COLUMN decision TEXT NOT NULL DEFAULT 'approved'")
        existing = {row[1] for row in db.execute("PRAGMA table_info(applications)")}
        for name in ("started_at", "state_changed_at", "heartbeat_at", "attempt_number", "queue_priority", "submitted_at"):
            if name not in existing:
                declaration = "INTEGER NOT NULL DEFAULT 0" if name in {"attempt_number", "queue_priority"} else "TEXT"
                db.execute(f"ALTER TABLE applications ADD COLUMN {name} " + declaration)
        db.execute("UPDATE applications SET submitted_at=updated_at WHERE state='submitted' AND submitted_at IS NULL")
        db.execute("CREATE INDEX IF NOT EXISTS idx_applications_heartbeat ON applications(state,heartbeat_at)")
        match_table=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_match_results'").fetchone()
        if match_table:
            match_columns={row[1] for row in db.execute("PRAGMA table_info(job_match_results)")}
            for name,declaration in (("candidate_fingerprint","TEXT NOT NULL DEFAULT ''"),
                                     ("preferences_fingerprint","TEXT NOT NULL DEFAULT ''"),
                                     ("job_fingerprint","TEXT NOT NULL DEFAULT ''"),
                                     ("matching_model_version","TEXT NOT NULL DEFAULT ''"),
                                     ("freshness_status","TEXT NOT NULL DEFAULT 'legacy'")):
                if name not in match_columns:
                    db.execute(f"ALTER TABLE job_match_results ADD COLUMN {name} {declaration}")
        db.execute("""CREATE TABLE IF NOT EXISTS application_controls (
            user_id TEXT PRIMARY KEY,paused INTEGER NOT NULL DEFAULT 0,daily_limit INTEGER NOT NULL DEFAULT 200,
            submission_enabled INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS application_attempts (
            id TEXT PRIMARY KEY,application_id TEXT NOT NULL,attempt_number INTEGER NOT NULL,
            state TEXT NOT NULL,started_at TEXT,state_changed_at TEXT,heartbeat_at TEXT,
            diagnostic_json TEXT NOT NULL DEFAULT '{}',error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
            UNIQUE(application_id,attempt_number),FOREIGN KEY(application_id) REFERENCES applications(id))""")
        # Additive audit ledger. Rollback keeps this table and its events intact;
        # older application code ignores it, so no destructive down migration is needed.
        db.execute("""CREATE TABLE IF NOT EXISTS manual_submission_events (
            event_id TEXT PRIMARY KEY,application_id TEXT NOT NULL,user_id TEXT NOT NULL,
            attempt_number INTEGER NOT NULL,outcome TEXT NOT NULL CHECK(outcome IN
            ('submitted_manually','not_submitted','needs_follow_up','employer_confirmation_received','outcome_unknown')),
            confirmed_by_applicant INTEGER NOT NULL CHECK(confirmed_by_applicant=1),recorded_at TEXT NOT NULL,
            FOREIGN KEY(application_id) REFERENCES applications(id))""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_manual_outcomes_app ON manual_submission_events(user_id,application_id,recorded_at DESC)")
        db.execute("INSERT OR IGNORE INTO application_controls VALUES(?,0,200,0,?)", (USER, now()))
        auth_table=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='application_authorizations'").fetchone()
        if auth_table:
            auth=db.execute("SELECT settings_json FROM application_authorizations WHERE user_id=?",(USER,)).fetchone()
            try: configured=min(200,max(1,int(json.loads(auth["settings_json"] or "{}").get("max_applications_per_24_hours",20)))) if auth else 20
            except (TypeError,ValueError,AttributeError,json.JSONDecodeError): configured=20
            # Import-time initialization is idempotent: do not make a live
            # settings row appear newly changed merely because the module was
            # imported for a read-only page request or a test run.
            db.execute("""UPDATE application_controls SET daily_limit=?,submission_enabled=0,updated_at=?
                WHERE user_id=? AND (daily_limit<>? OR submission_enabled<>0)""",
                (configured,now(),USER,configured))
        db.execute("""CREATE TABLE IF NOT EXISTS submission_quota_reservations (
            id TEXT PRIMARY KEY,user_id TEXT NOT NULL,application_id TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('reserved','succeeded','failed','uncertain')),
            created_at TEXT NOT NULL,updated_at TEXT NOT NULL,confirmed_at TEXT)""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_submission_quota_user_time ON submission_quota_reservations(user_id,state,confirmed_at)")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_submission_quota_app ON submission_quota_reservations(user_id,application_id)")
        db.execute("""CREATE TABLE IF NOT EXISTS application_form_inspections (
            id TEXT PRIMARY KEY,user_id TEXT NOT NULL,job_key TEXT NOT NULL,provider TEXT NOT NULL,
            outcome TEXT NOT NULL,report_json TEXT NOT NULL,inspected_at TEXT NOT NULL)""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_form_inspections_user_job ON application_form_inspections(user_id,job_key,inspected_at)")
        db.execute("""CREATE TABLE IF NOT EXISTS application_attention_items(
            attention_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,identity_hash TEXT NOT NULL,job_key TEXT NOT NULL,
            employer TEXT NOT NULL,title TEXT NOT NULL,posting_url TEXT NOT NULL,application_url TEXT NOT NULL,
            blocker_code TEXT NOT NULL,reason TEXT NOT NULL,applicant_action TEXT NOT NULL,
            eligibility_state TEXT NOT NULL,form_state TEXT NOT NULL,preparation_state TEXT NOT NULL DEFAULT 'not_prepared',
            form_fingerprint TEXT NOT NULL DEFAULT '',posting_fingerprint TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','acknowledged','resolved')),
            created_at TEXT NOT NULL,updated_at TEXT NOT NULL,last_seen_at TEXT NOT NULL,acknowledged_at TEXT,resolved_at TEXT)""")
        attention_index=db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='uq_application_attention_open'").fetchone()
        if attention_index and "acknowledged" not in str(attention_index["sql"] or ""):
            db.execute("DROP INDEX uq_application_attention_open")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_application_attention_open ON application_attention_items(user_id,identity_hash,blocker_code) WHERE status IN ('open','acknowledged')")
        db.execute("CREATE INDEX IF NOT EXISTS idx_application_attention_status ON application_attention_items(user_id,status,updated_at DESC)")
        db.execute("""CREATE TABLE IF NOT EXISTS application_preparation_authorizations(
            authorization_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,job_key TEXT NOT NULL,
            application_url_hash TEXT NOT NULL,resume_id TEXT NOT NULL,match_updated_at TEXT NOT NULL,
            matching_engine_version TEXT NOT NULL,profile_fingerprint TEXT NOT NULL,
            answers_fingerprint TEXT NOT NULL,documents_fingerprint TEXT NOT NULL,
            settings_version INTEGER NOT NULL DEFAULT 0,field_keys_json TEXT NOT NULL,
            document_types_json TEXT NOT NULL,allow_ai_answers INTEGER NOT NULL DEFAULT 0,
            allow_cloud_ai INTEGER NOT NULL DEFAULT 0,consent_version TEXT NOT NULL,
            authorized_at TEXT NOT NULL,revoked_at TEXT)""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_prep_auth_user_job ON application_preparation_authorizations(user_id,job_key,authorized_at)")
        auth_columns={row[1] for row in db.execute("PRAGMA table_info(application_preparation_authorizations)")}
        if "authorization_source" not in auth_columns:
            db.execute("ALTER TABLE application_preparation_authorizations ADD COLUMN authorization_source TEXT NOT NULL DEFAULT 'applicant_job_specific'")
        db.execute("""CREATE TABLE IF NOT EXISTS standing_policy_events(
            event_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,job_key TEXT NOT NULL,
            authorization_id TEXT NOT NULL,policy_version INTEGER NOT NULL,authorized_at TEXT NOT NULL)""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_standing_policy_events_user_time ON standing_policy_events(user_id,authorized_at)")
        db.execute("INSERT OR IGNORE INTO submission_quota_reservations(id,user_id,application_id,state,created_at,updated_at,confirmed_at) SELECT 'historic:'||id,user_id,id,'succeeded',created_at,updated_at,COALESCE(submitted_at,updated_at) FROM applications WHERE state='submitted'")
        # Older versions left in-flight records without heartbeat timestamps.
        # Recovery is explicit and conservative: never retry automatically.
        _recover_stale_in_db(db, startup=True)
        # Upgrade existing application rows into a one-attempt history without
        # dropping or altering their saved diagnostics.
        db.execute("UPDATE applications SET state_changed_at=COALESCE(state_changed_at,updated_at)")
        rows=db.execute("SELECT id,state,diagnostic_json,error,created_at,updated_at,started_at,state_changed_at,heartbeat_at,attempt_number,job_key,company,application_url FROM applications").fetchall()
        for row in rows:
            number=max(1,int(row["attempt_number"] or 0))
            db.execute("UPDATE applications SET attempt_number=? WHERE id=?",(number,row["id"]))
            db.execute("INSERT OR IGNORE INTO application_attempts(id,application_id,attempt_number,state,started_at,state_changed_at,heartbeat_at,diagnostic_json,error,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (str(uuid.uuid4()),row["id"],number,row["state"],row["started_at"],row["state_changed_at"] or row["updated_at"],row["heartbeat_at"],row["diagnostic_json"] or "{}",row["error"],row["created_at"],row["updated_at"]))
            identity=_opening_identity({"job_key":row["job_key"],"company":row["company"],"application_url":row["application_url"]})
            db.execute("INSERT OR IGNORE INTO application_opening_reservations(user_id,identity_hash,application_id,identity_json,created_at) VALUES(?,?,?,?,?)",
                       (USER,identity["hash"],row["id"],json.dumps(identity["parts"],sort_keys=True),row["created_at"]))
            if identity["url"]:
                db.execute("INSERT OR IGNORE INTO application_opening_aliases(user_id,canonical_url,identity_hash,application_id,identity_json,created_at) VALUES(?,?,?,?,?,?)",
                           (USER,identity["url"],identity["hash"],row["id"],json.dumps(identity["parts"],sort_keys=True),row["created_at"]))
ACTIVE_STATES = {"queued", "opening", "inspecting", "filling", "uploading_resume"}
TERMINAL_STATES = {"ready_for_review", "submitted", "failed", "unsupported", "needs_user_input", "skipped_duplicate"}
TRANSITIONS = {
    "queued": {"opening", "paused", "failed", "needs_user_input", "unsupported"},
    "opening": {"inspecting", "filling", "paused", "failed", "unsupported", "needs_user_input", "ready_for_review"},
    "inspecting": {"filling", "paused", "failed", "unsupported", "needs_user_input", "ready_for_review"},
    "filling": {"filling", "uploading_resume", "paused", "failed", "unsupported", "needs_user_input", "ready_for_review"},
    "uploading_resume": {"filling", "paused", "failed", "unsupported", "needs_user_input"},
    "paused": {"queued"}, "failed": {"queued"}, "needs_user_input": {"queued"}, "unsupported": {"queued"},
    "ready_for_review": set(), "submitted": set(), "skipped_duplicate": set(),
}
STALE_AFTER = timedelta(minutes=2)


def _recover_stale_in_db(db, startup=False):
    stamp = now()
    # Queued records can also be orphaned if the process exits after persisting
    # the queue item but before its executor starts. Treat those as interrupted
    # too; they are never automatically resubmitted after recovery.
    rows = db.execute("SELECT id,state,heartbeat_at,updated_at,diagnostic_json FROM applications WHERE user_id=? AND state IN ('queued','opening','inspecting','filling','uploading_resume')", (USER,)).fetchall()
    recovered = 0
    cutoff = datetime.now(timezone.utc) - STALE_AFTER
    for row in rows:
        with _attempt_lock:
            running_here = row["id"] in _active_attempts
        last = row["heartbeat_at"] or row["updated_at"]
        try: stale = startup or datetime.fromisoformat(last) < cutoff
        except (TypeError, ValueError): stale = True
        if running_here or not stale:
            continue
        try: diagnostic = json.loads(row["diagnostic_json"] or "{}")
        except (TypeError, json.JSONDecodeError): diagnostic = {}
        diagnostic.update({"interrupted": True, "message": "The previous dry-run worker stopped before confirming an outcome. Review diagnostics, then retry explicitly if safe."})
        db.execute("UPDATE applications SET state='paused',diagnostic_json=?,error=NULL,updated_at=?,state_changed_at=?,heartbeat_at=NULL WHERE id=?",
                   (json.dumps(diagnostic), stamp, stamp, row["id"]))
        db.execute("UPDATE application_attempts SET state='paused',diagnostic_json=?,error=NULL,updated_at=?,state_changed_at=?,heartbeat_at=NULL WHERE application_id=? AND attempt_number=(SELECT attempt_number FROM applications WHERE id=?)",
                   (json.dumps(diagnostic),stamp,stamp,row["id"],row["id"]))
        recovered += 1
    return recovered


def recover_stale_applications():
    with connect() as db:
        return _recover_stale_in_db(db)


initialize()


def _quota_limit(db, user_id=USER):
    row=db.execute("SELECT daily_limit FROM application_controls WHERE user_id=?",(user_id,)).fetchone()
    return min(200,max(1,int(row["daily_limit"] if row else 200)))


def rolling_submission_quota(user_id=USER, at=None):
    """Return rolling-window success count and safe capacity holds.

    Only confirmed successes count toward the published submission total. Active
    reservations and uncertain outcomes temporarily hold capacity to prevent
    concurrent workers from exceeding the configured maximum.
    """
    instant=at or datetime.now(timezone.utc)
    if instant.tzinfo is None: instant=instant.replace(tzinfo=timezone.utc)
    cutoff=(instant-timedelta(hours=24)).isoformat()
    with connect() as db:
        limit=_quota_limit(db,user_id)
        successful=int(db.execute("SELECT COUNT(*) FROM submission_quota_reservations WHERE user_id=? AND state='succeeded' AND confirmed_at>=?",(user_id,cutoff)).fetchone()[0])
        held=int(db.execute("SELECT COUNT(*) FROM submission_quota_reservations WHERE user_id=? AND state IN ('reserved','uncertain')",(user_id,)).fetchone()[0])
    return {"limit":limit,"successful_last_24h":successful,"capacity_held":held,"remaining":max(0,limit-successful-held),"submission_enabled":False}


def reserve_submission_capacity(application_id, user_id=USER, at=None):
    """Atomically reserve future submission capacity; no submit path calls this yet."""
    instant=at or datetime.now(timezone.utc)
    if instant.tzinfo is None: instant=instant.replace(tzinfo=timezone.utc)
    stamp=instant.isoformat(); cutoff=(instant-timedelta(hours=24)).isoformat()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        controls=db.execute("SELECT daily_limit,submission_enabled FROM application_controls WHERE user_id=?",(user_id,)).fetchone()
        if not controls or not controls["submission_enabled"]:
            return {"reserved":False,"reason":"Live submission is disabled.","reservation_id":None}
        application=db.execute("SELECT state FROM applications WHERE id=? AND user_id=?",(application_id,user_id)).fetchone()
        if not application or application["state"]!="ready_for_review":
            return {"reserved":False,"reason":"Only a reviewed application can reserve future submission capacity.","reservation_id":None}
        limit=min(200,max(1,int(controls["daily_limit"])))
        prior=db.execute("SELECT id,state FROM submission_quota_reservations WHERE user_id=? AND application_id=?",(user_id,application_id)).fetchone()
        if prior and prior["state"] in {"reserved","succeeded","uncertain"}:
            return {"reserved":False,"reason":"This application already has a submission reservation or recorded outcome.","reservation_id":prior["id"]}
        successful=int(db.execute("SELECT COUNT(*) FROM submission_quota_reservations WHERE user_id=? AND state='succeeded' AND confirmed_at>=?",(user_id,cutoff)).fetchone()[0])
        held=int(db.execute("SELECT COUNT(*) FROM submission_quota_reservations WHERE user_id=? AND state IN ('reserved','uncertain')",(user_id,)).fetchone()[0])
        if successful+held>=limit:
            return {"reserved":False,"reason":"The rolling 24-hour submission capacity is full.","reservation_id":None}
        reservation=prior["id"] if prior else str(uuid.uuid4())
        if prior:
            db.execute("UPDATE submission_quota_reservations SET state='reserved',created_at=?,updated_at=?,confirmed_at=NULL WHERE id=?",(stamp,stamp,reservation))
        else:
            db.execute("INSERT INTO submission_quota_reservations(id,user_id,application_id,state,created_at,updated_at) VALUES(?,?,?,'reserved',?,?)",(reservation,user_id,application_id,stamp,stamp))
        return {"reserved":True,"reason":"Capacity reserved; live submission is still disabled in this phase.","reservation_id":reservation}


def finalize_submission_reservation(reservation_id, outcome, at=None):
    if outcome not in {"succeeded","failed","uncertain"}: raise ValueError("Unsupported submission outcome.")
    instant=at or datetime.now(timezone.utc); stamp=instant.isoformat()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("SELECT state FROM submission_quota_reservations WHERE id=? AND user_id=?",(reservation_id,USER)).fetchone()
        if not row: raise ValueError("Submission reservation not found.")
        if row["state"]!="reserved": raise ValueError("Submission reservation is already finalized.")
        db.execute("UPDATE submission_quota_reservations SET state=?,updated_at=?,confirmed_at=? WHERE id=?",(outcome,stamp,stamp if outcome=="succeeded" else None,reservation_id))
    return {"state":outcome,"confirmed_at":stamp if outcome=="succeeded" else None}


def resolve_uncertain_submission(reservation_id, confirmed_success, at=None):
    """Release a held future quota slot only after an explicit human outcome check."""
    instant=at or datetime.now(timezone.utc); stamp=instant.isoformat()
    state="succeeded" if bool(confirmed_success) else "failed"
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("SELECT state FROM submission_quota_reservations WHERE id=? AND user_id=?",(reservation_id,USER)).fetchone()
        if not row: raise ValueError("Submission reservation not found.")
        if row["state"]!="uncertain": raise ValueError("Only an uncertain outcome can be resolved this way.")
        db.execute("UPDATE submission_quota_reservations SET state=?,updated_at=?,confirmed_at=? WHERE id=?",(state,stamp,stamp if state=="succeeded" else None,reservation_id))
    return {"state":state,"confirmed_at":stamp if state=="succeeded" else None}

def _mock_allowed(url):
    p=urlparse(url)
    return p.scheme=="http" and p.hostname in {"127.0.0.1","localhost"} and p.port==8000 and p.path.startswith(MOCK_PATH_PREFIX) and not p.username and not p.password

class ApplicationURLValidationError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code=code


def _sanitized_inspection_url(value):
    """Drop query, fragment, and any userinfo before a URL enters diagnostics."""
    try:
        parsed=urlparse(str(value or ""))
        host=parsed.hostname or ""
        if not host: return ""
        if ":" in host and not host.startswith("["): host="["+host+"]"
        port=parsed.port
        netloc=host+(":"+str(port) if port else "")
        return parsed._replace(netloc=netloc,query="",fragment="").geturl()
    except (TypeError,ValueError):
        return ""


class RobotsRedirectBlocked(urllib.error.URLError):
    """A robots redirect was rejected by the public-destination guard."""


def _robots_reason(category):
    return {
        "dns_failure": "The employer's robots.txt host could not be resolved by this computer. Inspection was stopped; check DNS or network access and retry.",
        "timeout": "The employer's robots.txt request timed out after bounded retries. Inspection was stopped; retry when the host is reachable.",
        "tls_error": "A secure TLS connection to the employer's robots.txt could not be verified. Inspection was stopped.",
        "tls_configuration": "Python's verified TLS trust configuration could not be loaded. Install the application requirements or correct the configured CA bundle; inspection was stopped.",
        "redirect_blocked": "The employer's robots.txt redirected to a destination that failed public URL safety checks. Inspection was stopped.",
        "http_status": "The employer's robots.txt returned an HTTP error, so permission could not be confirmed. Inspection was stopped.",
        "policy_missing": "The employer returned HTTP 404 for robots.txt. Because no policy could be verified, inspection was stopped.",
        "network_error": "The employer's robots.txt could not be reached after bounded retries. Inspection was stopped; check network access and retry.",
        "malformed_policy": "The employer's robots.txt response was malformed or too large to verify safely. Inspection was stopped.",
        "unsafe_destination": "The robots.txt destination did not pass public URL safety checks. Inspection was stopped.",
        "policy_disallowed": "The employer's robots.txt explicitly disallows this read-only inspection path.",
    }.get(category, "The employer's robots.txt policy could not be verified. Inspection was stopped.")


def _robots_tls_failure(cause):
    exception_name=type(cause).__name__
    if isinstance(cause,ssl.SSLCertVerificationError):
        return "TLS certificate verification failed ("+exception_name+"). The server certificate was not trusted; inspection was stopped."
    return "The TLS handshake failed ("+exception_name+"). Inspection was stopped."


def _robots_ssl_context():
    """Build a certificate-verifying context using explicit CAs or the OS trust store."""
    explicit_cafile=(os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE") or
                     os.environ.get("CURL_CA_BUNDLE"))
    explicit_capath=os.environ.get("SSL_CERT_DIR")
    if explicit_cafile or explicit_capath:
        context=ssl.create_default_context(cafile=explicit_cafile or None,capath=explicit_capath or None)
    else:
        try:
            import truststore
        except ImportError:
            # Safe fallback for a partial install. certifi supplies a verified
            # public CA bundle; production installs truststore from requirements
            # to honor the macOS Keychain and managed system trust configuration.
            try:
                import certifi
                context=ssl.create_default_context(cafile=certifi.where())
            except ImportError:
                context=ssl.create_default_context()
        else:
            context=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.verify_mode=ssl.CERT_REQUIRED
    context.check_hostname=True
    return context


class SafeRobotsRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Validate each redirect target before urllib follows it."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        destination=urllib.parse.urljoin(req.full_url,newurl)
        try:
            validate_application_url(destination)
        except ValueError as exc:
            raise RobotsRedirectBlocked("robots redirect destination was rejected") from exc
        return super().redirect_request(req,fp,code,msg,headers,destination)


def validate_application_url(url):
    if not isinstance(url,str) or len(url)>2048:
        raise ApplicationURLValidationError("invalid_url","Application URL is missing or too long.")
    try: p=urlparse(url)
    except ValueError as exc: raise ApplicationURLValidationError("invalid_url","Application URL is malformed.") from exc
    if _mock_allowed(url): return url
    if p.scheme!="https" or not p.hostname or p.username or p.password:
        raise ApplicationURLValidationError("invalid_scheme","Application links must use public HTTPS or configured local mock pages.")
    host=p.hostname.rstrip(".").casefold()
    if host in {"localhost","localhost.localdomain","metadata.google.internal"} or host.endswith(".localhost"):
        raise ApplicationURLValidationError("private_destination","Private and localhost application destinations are blocked.")
    try: addresses={x[4][0] for x in socket.getaddrinfo(host,p.port or 443,type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise ApplicationURLValidationError("dns_failure","The public host could not be resolved safely.") from exc
    except OSError as exc:
        raise ApplicationURLValidationError("dns_failure","The public host could not be resolved safely.") from exc
    try: private=any(not ipaddress.ip_address(address).is_global for address in addresses)
    except ValueError as exc: raise ApplicationURLValidationError("invalid_address","The host returned an invalid IP address.") from exc
    if private: raise ApplicationURLValidationError("private_destination","Private, loopback, and metadata-service destinations are blocked.")
    return url

def _safe_request(url):
    try: validate_application_url(url); return True
    except ValueError: return False


def _record_blocked_request(policy_state, request):
    """Keep bounded endpoint diagnostics without reading request data."""
    if policy_state is None:
        return
    policy_state["blocked_non_read_only_request_count"] = int(
        policy_state.get("blocked_non_read_only_request_count",0)) + 1
    records=policy_state.setdefault("blocked_non_read_only_requests",[])
    if len(records)>=8:
        return
    try:
        parsed=urlparse(str(getattr(request,"url","") or ""))
        host=(parsed.hostname or "").casefold().rstrip(".")
        known_hosts={"jobs.ashbyhq.com","api.ashbyhq.com","job-boards.greenhouse.io",
                     "boards-api.greenhouse.io","jobs.lever.co","api.lever.co"}
        host=host if host in known_hosts else "other"
        endpoint=(parsed.path.rstrip("/").rsplit("/",1)[-1] or "root").casefold()
        if (len(endpoint)>48 or not re.fullmatch(r"[a-z0-9_-]+",endpoint) or
                re.fullmatch(r"(?:[0-9a-f]{8}-[0-9a-f-]{27,}|[0-9]{5,})",endpoint)):
            endpoint="other"
        resource=str(getattr(request,"resource_type","other") or "other").casefold()
        if resource not in {"document","script","xhr","fetch","image","stylesheet","font"}:
            resource="other"
        records.append({"method":str(getattr(request,"method","unknown") or "unknown").upper()[:12],
                        "host":host,"endpoint":endpoint,"resource_type":resource})
    except Exception:
        records.append({"method":"unknown","host":"other","endpoint":"other","resource_type":"other"})

def _guard_browser_request(route,policy_state=None):
    request=route.request
    if request.method.upper() not in {"GET","HEAD","OPTIONS"}:
        if policy_state is not None:
            _record_blocked_request(policy_state,request)
            if getattr(request,"is_navigation_request",lambda:False)():
                policy_state.update({"category":"read_only_method_blocked","reason":"A page navigation attempted a non-read-only HTTP method. No application interaction was performed."})
        try:route.abort()
        except (Exception,asyncio.CancelledError):pass
        return
    if not _safe_request(request.url) and not request.url.startswith(("about:","data:")):
        if policy_state is not None and getattr(request,"is_navigation_request",lambda:False)():
            policy_state.update({"category":"unsafe_destination","reason":"A browser navigation destination failed public URL safety checks."})
        try:route.abort()
        except (Exception,asyncio.CancelledError):pass
        return
    if (policy_state is not None and getattr(request,"is_navigation_request",lambda:False)()
            and not _mock_allowed(request.url)):
        permitted,reason,category=_robots_permission(request.url)
        if not permitted:
            policy_state.update({"category":category,"reason":reason or "Robots policy did not permit this navigation."})
            try:route.abort()
            except (Exception,asyncio.CancelledError):pass
            return
    # A page can close while Chromium still has routed subresource requests
    # queued. Those cancellations are expected during safe cleanup and should
    # not leak asynchronous Playwright callback tracebacks to logs.
    try:route.continue_()
    except (Exception,asyncio.CancelledError):pass


def _close_browser_safely(page,browser):
    """Detach routed requests before closing Chromium's page/context."""
    if page is not None:
        try:page.unroute_all(behavior="wait")
        except TypeError:
            try:page.unroute_all()
            except (Exception,asyncio.CancelledError):pass
        except (Exception,asyncio.CancelledError):pass
    if browser is not None:
        try:browser.close()
        except (Exception,asyncio.CancelledError):pass


class _ManagedPlaywrightContext:
    """Close routed pages before the sync Playwright driver shuts down."""
    def __init__(self,factory):self.factory=factory;self.manager=None;self.playwright=None;self.page=None;self.browser=None
    def __enter__(self):
        self.manager=self.factory();self.playwright=self.manager.__enter__();return self
    def __getattr__(self,name):return getattr(self.playwright,name)
    def attach(self,page,browser):self.page=page;self.browser=browser
    def __exit__(self,exc_type,exc_value,traceback):
        _close_browser_safely(self.page,self.browser)
        return self.manager.__exit__(exc_type,exc_value,traceback)


def _robots_permission(url):
    """Return (allowed, reason, category), failing closed on unknown policy.

    Cache only bounded, successfully retrieved policy documents. Permission is always
    evaluated against the requested path, not cached as a host-wide boolean.
    """
    parsed=urlparse(url)
    robots_url=parsed._replace(path="/robots.txt",params="",query="",fragment="").geturl()
    content=None
    with _robots_cache_lock:
        cached=_robots_cache.get(robots_url)
        if cached and time.monotonic()-cached[0] < ROBOTS_CACHE_TTL_SECONDS:
            content=cached[1]
        elif cached:
            _robots_cache.pop(robots_url,None)
    if content is None:
        request=urllib.request.Request(robots_url,headers={"User-Agent":"BabyStepsAI-ReadOnlyInspector/1.0"})
        try:
            tls_context=_robots_ssl_context()
            opener=urllib.request.build_opener(SafeRobotsRedirectHandler(),urllib.request.HTTPSHandler(context=tls_context))
        except (ImportError,OSError,ssl.SSLError,ValueError) as exc:
            return False,"Verified TLS setup failed ("+type(exc).__name__+"). Install BabyStepsAI requirements or check SSL_CERT_FILE / SSL_CERT_DIR; inspection was stopped.","tls_configuration"
        last_category="network_error"
        last_detail=""
        for attempt in range(ROBOTS_MAX_ATTEMPTS):
            try:
                validate_application_url(robots_url)
                with opener.open(request,timeout=5) as response:
                    status=int(response.status)
                    raw=response.read(ROBOTS_MAX_BYTES+1)
                if len(raw)>ROBOTS_MAX_BYTES:
                    return False,_robots_reason("malformed_policy"),"malformed_policy"
                if status!=200:
                    return False,"The employer's robots.txt returned HTTP "+str(status)+", so permission could not be confirmed. Inspection was stopped.","http_status"
                try: content=raw.decode("utf-8-sig")
                except UnicodeDecodeError:
                    return False,_robots_reason("malformed_policy"),"malformed_policy"
                stripped=content.lstrip().casefold()
                if stripped.startswith(("<!doctype html","<html","<?xml")):
                    return False,_robots_reason("malformed_policy"),"malformed_policy"
                meaningful=[line.split("#",1)[0].strip() for line in content.splitlines()]
                meaningful=[line for line in meaningful if line]
                if meaningful and not any(re.match(r"(?i)^user-agent\s*:\s*\S+",line) for line in meaningful):
                    return False,_robots_reason("malformed_policy"),"malformed_policy"
                with _robots_cache_lock: _robots_cache[robots_url]=(time.monotonic(),content)
                break
            except urllib.error.HTTPError as exc:
                if exc.code==404:
                    return False,"The employer returned HTTP 404 for robots.txt. Permission could not be verified, so inspection was stopped.","policy_missing"
                if exc.code in {408,429,500,502,503,504}:
                    last_category="http_status"
                    last_detail="The employer's robots.txt returned HTTP "+str(exc.code)+" after bounded retries. Inspection was stopped."
                else:
                    return False,"The employer's robots.txt returned HTTP "+str(exc.code)+", so permission could not be confirmed. Inspection was stopped.","http_status"
            except ApplicationURLValidationError as exc:
                if exc.code=="dns_failure":
                    last_category="dns_failure"
                else:
                    return False,_robots_reason("unsafe_destination"),"unsafe_destination"
            except RobotsRedirectBlocked:
                return False,_robots_reason("redirect_blocked"),"redirect_blocked"
            except ssl.SSLError as exc:
                return False,_robots_tls_failure(exc),"tls_error"
            except (TimeoutError, socket.timeout):
                last_category="timeout"
            except urllib.error.URLError as exc:
                reason=getattr(exc,"reason",None)
                if isinstance(reason,ssl.SSLError):
                    return False,_robots_tls_failure(reason),"tls_error"
                if isinstance(reason,(socket.gaierror,)):
                    last_category="dns_failure"
                elif isinstance(reason,(TimeoutError,socket.timeout)):
                    last_category="timeout"
                elif isinstance(reason,OSError):
                    last_category="network_error"
                else:
                    last_category="network_error"
            except OSError as exc:
                if isinstance(exc,ssl.SSLError):
                    return False,_robots_tls_failure(exc),"tls_error"
                if isinstance(exc,socket.gaierror): last_category="dns_failure"
                elif isinstance(exc,(TimeoutError,socket.timeout)): last_category="timeout"
                else: last_category="network_error"
            if attempt+1<ROBOTS_MAX_ATTEMPTS:
                time.sleep(0.1*(attempt+1))
        else:
            return False,last_detail or _robots_reason(last_category),last_category
    parser=urllib.robotparser.RobotFileParser();parser.set_url(robots_url);parser.parse(content.splitlines())
    if not parser.can_fetch("BabyStepsAI-ReadOnlyInspector",url):
        return False,_robots_reason("policy_disallowed"),"policy_disallowed"
    return True,"","policy_verified"

class DryRunRequest(BaseModel):
    job_key: str|None=None
    job: dict|None=None
    application_url: str|None=None


class ApplicationQueueRequest(BaseModel):
    job_keys: list[str]


class FormInspectionRequest(BaseModel):
    job_key: str


class StandaloneFormCompatibilityRequest(BaseModel):
    application_url: str

class PreparationAuthorizationRequest(BaseModel):
    confirmed: bool = False
    confirmed_employer: str = Field(min_length=1,max_length=200)
    confirmed_title: str = Field(min_length=1,max_length=300)
    confirmed_application_url: str = Field(min_length=8,max_length=2048)
    field_keys: list[str] = Field(default_factory=list,max_length=100)
    document_types: list[str] = Field(default_factory=list,max_length=20)
    allow_ai_answers: bool = False
    allow_cloud_ai: bool = False

class EligibilityReviewRequest(BaseModel):
    reviewed: bool = False

class TargetedEligibilityReviewRequest(BaseModel):
    accept_unspecified_employment_type: bool = False
    accept_unclear_role_seniority: bool = False
    acknowledge_ai_diagnostics: bool = False
    match_updated_at: str = Field(min_length=1,max_length=100)
    profile_fingerprint: str = Field(min_length=32,max_length=128)
    preferences_fingerprint: str = Field(min_length=32,max_length=128)
    job_fingerprint: str = Field(min_length=32,max_length=128)
    matching_engine_version: str = Field(min_length=1,max_length=300)

class DeterministicReassessmentRequest(BaseModel):
    match_updated_at: str = Field(min_length=1,max_length=100)

class MissingAnswerRequest(BaseModel):
    question: str
    answer: str
    reusable: bool = False
    reuse_scope: str = ""
    confirmed: bool = False

class GroupedAnswerRequest(BaseModel):
    question: str = Field(min_length=3,max_length=500)
    answer: str = Field(min_length=1,max_length=4000)
    application_ids: list[str] = Field(default_factory=list,max_length=50)
    reuse_scope: str = "this_job"
    job_key: str = Field(default="",max_length=300)
    confirmed: bool = False
    acknowledge_existing_conflict: bool = False

class TechnicalEvidenceAnswerRequest(BaseModel):
    question: str = Field(min_length=30,max_length=500)
    answer: str = Field(min_length=30,max_length=4000)
    job_keys: list[str] = Field(default_factory=list,max_length=50)
    job_key: str = Field(default="",max_length=300)
    reuse_scope: str = "this_job"
    confirmed: bool = False
    acknowledge_existing_conflict: bool = False

class ManualSubmissionOutcomeRequest(BaseModel):
    outcome: str = Field(pattern="^(submitted_manually|not_submitted|needs_follow_up|employer_confirmation_received|outcome_unknown)$")
    confirmed: bool = False
    event_id: str = Field(default="",max_length=80)

def _get_job(req):
    if req.job:
        job=dict(req.job); job["application_url"]=req.application_url or job.get("application_url") or job.get("url")
        if not job.get("application_url"): raise HTTPException(400,"A job application URL is required.")
        job.setdefault("job_key","url:"+str(job["application_url"]).split("?",1)[0].rstrip("/").casefold())
        return job
    if not req.job_key: raise HTTPException(400,"Provide a saved job key or normalized job record.")
    with connect() as db: row=db.execute("SELECT * FROM discovered_openings WHERE user_id=? AND job_key=?",(USER,req.job_key)).fetchone()
    if not row: raise HTTPException(404,"Saved job not found.")
    if "active" in row.keys() and not row["active"]:
        raise HTTPException(410,"This posting is no longer active in its public source.")
    job=dict(row); job["application_url"]=job["url"]
    match=re.match(r"greenhouse:([^:]+):(\d+)$",req.job_key)
    if match:
        board=match.group(1); companies={"cloudflare":"Cloudflare","datadog":"Datadog","figma":"Figma"}
        records,error=fetch_board(companies.get(board,board),board)
        if not error:
            item=next((x for x in records if str(x.get("id"))==match.group(2)),None)
            if item: job["description"]=item.get("content",""); job["application_url"]=item.get("absolute_url") or job["application_url"]
    return job

def _posting_preparation_blocker(job_key):
    if not job_key:return ""
    with connect() as db:
        row=db.execute("SELECT active,availability_status,reopened_review_required FROM discovered_openings WHERE user_id=? AND job_key=?",
                       (USER,job_key)).fetchone()
    if not row:return ""
    if row["reopened_review_required"]:
        return "This requisition closed and reopened. Human review is required before any preparation or reapplication."
    if not row["active"] and row["availability_status"] in {"closed","expired","removed"}:
        return "The saved posting is no longer verified as currently available. Review the official employer posting before continuing."
    return ""


def _url_syntax_allowed(url):
    if _mock_allowed(url): return True
    try: parsed=urlparse(str(url or ""))
    except ValueError: return False
    if parsed.scheme!="https" or not parsed.hostname or parsed.username or parsed.password: return False
    host=parsed.hostname.rstrip(".").casefold()
    if host in {"localhost","localhost.localdomain","metadata.google.internal"} or host.endswith(".localhost"): return False
    try:
        address=ipaddress.ip_address(host)
        if not address.is_global: return False
    except ValueError:
        pass
    return True


PREPARATION_FIELD_LABELS={
    "full_name":"Full name","preferred_name":"Preferred name","email":"Email","phone":"Phone",
    "phone_country_code":"Phone country code","location":"Current residence","city":"City",
    "state_province":"State or province","country":"Country","postal_code":"Postal code",
    "linkedin":"LinkedIn","github":"GitHub","portfolio":"Portfolio","website":"Personal website",
    "university":"Education and university","education":"Education history","degree":"Degree",
    "major":"Major","graduation_date":"Graduation date","graduation_month":"Graduation month",
    "graduation_year":"Graduation year","work_experience":"Employment history",
    "internships":"Internship history","technical_skills":"Technical skills","projects":"Projects",
    "certifications":"Certifications","licenses":"Licenses","availability":"Availability",
    "earliest_available_date":"Earliest start date","relocation_willingness":"Relocation willingness",
    "willingness_to_travel":"Travel willingness","work_arrangement_preferences":"Work arrangement preferences",
    "employment_preferences":"Employment preferences","work_authorization":"Work authorization",
    "requires_sponsorship":"Sponsorship requirements","salary_expectations":"Salary expectations",
    "salary_currency":"Salary currency","additional_information":"Additional information",
}
PREPARATION_DOCUMENT_TYPES={"resume","transcript","certificate","cover_letter","portfolio"}
PREPARATION_CONSENT_VERSION="phase12.4-v1"


def _sha256_json(value):
    encoded=json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False,default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _public_url_for_confirmation(url):
    # Query strings can carry signed links or session tokens. The underlying
    # saved URL remains fully bound into the authorization fingerprint, but
    # user-facing confirmation and diagnostics show only the stable public URL.
    try:return urlparse(str(url or ""))._replace(query="",fragment="").geturl()
    except ValueError:return ""


def _preparation_snapshot(job_key,resume_id,application_url):
    with connect() as db:
        match=db.execute("SELECT status,classification,updated_at,freshness_status,matching_model_version FROM job_match_results WHERE user_id=? AND job_key=? AND resume_id=? ORDER BY created_at DESC LIMIT 1",(USER,job_key,resume_id)).fetchone()
        opening=db.execute("SELECT company,title,COALESCE(job_url,url) AS posting_url,url,content_fingerprint,description FROM discovered_openings WHERE user_id=? AND job_key=?",(USER,job_key)).fetchone()
        profile=db.execute("SELECT values_json,confirmed_json,updated_at FROM candidate_verifications WHERE user_id=? AND resume_id=?",(USER,resume_id)).fetchone()
        resume=db.execute("SELECT stored_name FROM resumes WHERE user_id=? AND id=?",(USER,resume_id)).fetchone()
        try:answer_rows=db.execute("""SELECT question_key,updated_at,confirmed,revoked_at,auto_reuse,sensitive,reuse_scope,scope_job_key,question_category,question_context
            FROM application_answers WHERE user_id=? AND resume_id=? AND
            (reuse_scope IN ('matching_jobs','general_preference') OR (reuse_scope='job_specific' AND scope_job_key=?)) ORDER BY question_key""",
            (USER,resume_id,job_key)).fetchall()
        except sqlite3.OperationalError:answer_rows=[]
        try:document_rows=db.execute("SELECT document_id,document_type,sha256,updated_at FROM application_documents WHERE user_id=? AND resume_id=? ORDER BY document_id",(USER,resume_id)).fetchall()
        except sqlite3.OperationalError:document_rows=[]
        settings=db.execute("SELECT version FROM application_authorizations WHERE user_id=?",(USER,)).fetchone()
    try:
        from resume_job_matcher import matching_engine_version
        engine_version=matching_engine_version()
    except Exception:engine_version=""
    resume_file_hash=""
    if resume:
        name=str(resume["stored_name"] or "")
        path=(STORAGE/Path(name).name).resolve()
        if name and Path(name).name==name and path.parent==STORAGE.resolve() and path.is_file():
            try:
                stat=path.stat(); cache_key=(str(resume_id),str(path),stat.st_size,stat.st_mtime_ns)
                with _resume_fingerprint_lock: resume_file_hash=_resume_fingerprint_cache.get(cache_key,"")
                if not resume_file_hash:
                    digest=hashlib.sha256()
                    with path.open("rb") as source:
                        for block in iter(lambda:source.read(1024*1024),b""):digest.update(block)
                    resume_file_hash=digest.hexdigest()
                    with _resume_fingerprint_lock:
                        _resume_fingerprint_cache.clear();_resume_fingerprint_cache[cache_key]=resume_file_hash
            except OSError:resume_file_hash=""
    return {
        "resume_id":str(resume_id or ""),
        "resume_file_hash":resume_file_hash,
        "application_url_hash":_sha256_json({"application_url":str(application_url),"employer":str(opening["company"] or "") if opening else "",
            "title":str(opening["title"] or "") if opening else "","posting_url":str(opening["posting_url"] or "") if opening else "",
            "job_content_fingerprint":str(opening["content_fingerprint"] or "") if opening else "",
            "description_fingerprint":hashlib.sha256(str(opening["description"] or "").encode("utf-8")).hexdigest() if opening else ""}),
        "match_updated_at":str(match["updated_at"] or "") if match else "",
        "matching_engine_version":engine_version,
        "profile_fingerprint":_sha256_json({"values":profile["values_json"],"confirmed":profile["confirmed_json"],"updated_at":profile["updated_at"],"resume_file_hash":resume_file_hash}) if profile else "",
        "answers_fingerprint":_sha256_json([dict(row) for row in answer_rows]),
        "documents_fingerprint":_sha256_json([dict(row) for row in document_rows]),
        "settings_version":int(settings["version"] or 0) if settings else 0,
        "match_status":str(match["status"] or "") if match else "",
        "classification":str(match["classification"] or "") if match else "",
        "freshness_status":str(match["freshness_status"] or "") if match else "",
    }


def _preparation_options(candidate,answers):
    fields=[]
    for key,label in PREPARATION_FIELD_LABELS.items():
        value=candidate.get(key)
        if key=="location":
            value=candidate.get("location") or any(candidate.get(k) for k in ("city","state_province","country","postal_code"))
        elif key=="university":
            value=candidate.get("university") or candidate.get("education")
        elif key=="education":
            value=candidate.get("education")
        elif key=="work_authorization":
            value=candidate.get(key) or candidate.get("country_work_authorizations")
        elif key=="requires_sponsorship":
            value=candidate.get(key) or candidate.get("sponsorship_needs_by_country")
        if value not in (None,"",[],{}):
            fields.append({"key":key,"label":label,"sensitive":key in {"work_authorization","requires_sponsorship","salary_expectations"}})
    job_key=answers.get("__job_key","")
    for key,item in answers.items():
        if not key.startswith("custom:") or not isinstance(item,dict):continue
        scope=item.get("reuse_scope") or ("matching_jobs" if item.get("auto_reuse") else "none")
        in_scope=scope in {"matching_jobs","general_preference"} or (scope=="job_specific" and item.get("scope_job_key")==job_key)
        if item.get("confirmed") and item.get("auto_reuse") and in_scope and not item.get("sensitive") and item.get("answer"):
            fields.append({"key":key,"label":"Reusable answer: "+str(item.get("question") or "Saved answer")[:180],"sensitive":False})
    documents=[{"type":"resume","label":"Active resume"}]
    seen=set()
    for item in candidate.get("__application_documents",[]):
        kind=str(item.get("document_type") or "").casefold()
        if kind in PREPARATION_DOCUMENT_TYPES-{"resume"} and kind not in seen:
            seen.add(kind);documents.append({"type":kind,"label":kind.replace("_"," ").title()})
    return fields,documents


def _current_preparation_authorization(job_key,resume_id,application_url):
    snapshot=_preparation_snapshot(job_key,resume_id,application_url)
    with connect() as db:
        rows=db.execute("SELECT * FROM application_preparation_authorizations WHERE user_id=? AND job_key=? AND revoked_at IS NULL ORDER BY authorized_at DESC",(USER,job_key)).fetchall()
    for row in rows:
        if (row["resume_id"]==snapshot["resume_id"] and
            row["application_url_hash"]==snapshot["application_url_hash"] and
            row["match_updated_at"]==snapshot["match_updated_at"] and
            row["matching_engine_version"]==snapshot["matching_engine_version"] and
            row["profile_fingerprint"]==snapshot["profile_fingerprint"] and
            row["answers_fingerprint"]==snapshot["answers_fingerprint"] and
            row["documents_fingerprint"]==snapshot["documents_fingerprint"] and
            int(row["settings_version"] or 0)==snapshot["settings_version"] and
            snapshot["match_status"]=="completed" and snapshot["freshness_status"]=="current"):
            try:
                source=str(row["authorization_source"] or "applicant_job_specific") if "authorization_source" in row.keys() else "applicant_job_specific"
                if source=="standing_policy" and not _standing_policy_still_current():continue
                return {"authorization_id":row["authorization_id"],"authorization_source":source,"field_keys":json.loads(row["field_keys_json"] or "[]"),
                    "document_types":json.loads(row["document_types_json"] or "[]"),
                    "allow_ai_answers":bool(row["allow_ai_answers"]),"allow_cloud_ai":bool(row["allow_cloud_ai"]),
                    "authorized_at":row["authorized_at"],"snapshot":snapshot}
            except (TypeError,json.JSONDecodeError):continue
    return None


def _standing_policy_still_current():
    """A standing grant is valid only while the approved policy and its pref scope are current."""
    try:
        settings=get_authorization()["settings"]
        if not settings.get("standing_policy_enabled"):return False
        import career_preferences
        prefs=career_preferences.get_preferences() or {}
        digest=_sha256_json(prefs)
        return bool(settings.get("standing_policy_preferences_fingerprint") and
                    settings["standing_policy_preferences_fingerprint"]==digest)
    except Exception:return False


def _standing_policy_blocker(job):
    settings=get_authorization()["settings"]
    if not settings.get("standing_policy_enabled"):return "No approved standing preparation policy covers this job."
    if not _standing_policy_still_current():return "Career preferences changed; review and re-approve the standing policy."
    if not job.get("active") or str(job.get("availability_status") or "").casefold()!="current":
        return "The saved posting is inactive or is not verified as currently available."
    if job.get("reopened_review_required"):
        return "This requisition closed and reopened. Human review is required before any preparation or reapplication."
    norm=lambda value:re.sub(r"[^a-z0-9]+"," ",str(value or "").casefold()).strip()
    employer=norm(job.get("company"))
    if any(norm(value) and norm(value) in employer for value in settings.get("standing_policy_excluded_employers",[])):
        return "This employer is excluded by the standing policy."
    category=norm(job.get("career_category") or job.get("category"))
    category_aliases={"software development":"software engineering"}
    category=category_aliases.get(category,category)
    if not category and re.search(r"\bsoftware\s+engineer\b",norm(job.get("title"))):
        # A verified ATS title is explicit enough to satisfy this category
        # scope; leave other missing or ambiguous categories fail-closed.
        category="software engineering"
    allowed_categories={category_aliases.get(norm(value),norm(value)) for value in settings.get("standing_policy_categories",[])}
    if not category or category not in allowed_categories:return "The job category is outside or missing from the standing policy scope."
    employment_alias={"fulltime":"full time","full time":"full time","parttime":"part time","part time":"part time",
        "contract":"contract","intern":"internship","internship":"internship","temporary":"temporary"}
    employment=employment_alias.get(norm(job.get("employment_type")),norm(job.get("employment_type")))
    allowed_employment={employment_alias.get(norm(value),norm(value)) for value in settings.get("standing_policy_employment_types",[])}
    if not employment or employment not in allowed_employment:
        return "Employment type is outside or missing from the standing policy scope."
    country=norm(job.get("country"))
    if not country:
        try:
            import resume_job_matcher
            country=norm(resume_job_matcher.infer_country_from_location(job.get("location"),job.get("country")))
        except Exception:country=""
    if not country or country not in {norm(value) for value in settings.get("standing_policy_countries",[])}:
        return "Job country is outside or missing from the standing policy scope."
    job_location=norm(job.get("location"))
    allowed_locations={norm(value) for value in settings.get("standing_policy_locations",[])}
    if not job_location or not allowed_locations or not (
            "any" in allowed_locations or "anywhere" in allowed_locations or
            any(value and (value==job_location or value in job_location) for value in allowed_locations)):
        return "Job location is outside or missing from the standing policy scope."
    arrangement=norm(job.get("work_arrangement"))
    allowed_arrangements={norm(value) for value in settings.get("standing_policy_work_arrangements",[])}
    if not arrangement or not allowed_arrangements or not ("any" in allowed_arrangements or arrangement in allowed_arrangements):
        return "Work arrangement is outside or missing from the standing policy scope."
    try:
        from job_relevance import classify_experience_level
        level,_=classify_experience_level(job.get("title"),job.get("description"),job.get("employment_type"))
    except Exception:level="Unknown"
    aliases={"new graduate":"new grad","new graduate role":"new grad","entry level":"entry level",
        "early career":"entry level","junior":"junior","associate":"associate","mid level":"mid level",
        "senior":"senior","internship":"internship"}
    canonical=aliases.get(norm(level),norm(level))
    selected={aliases.get(norm(value),norm(value)) for value in settings.get("standing_policy_experience_levels",[])}
    if not canonical or canonical in {"unknown","ambiguous"} or canonical not in selected:
        return "Role level is unknown or outside the standing policy scope."
    try:
        verified=datetime.fromisoformat(str(job.get("last_verified_at") or "").replace("Z","+00:00"))
        if verified.tzinfo is None:verified=verified.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc)-verified).total_seconds()>86400:return "The official posting verification is older than 24 hours."
    except (TypeError,ValueError):return "The official posting has no current verification timestamp."
    with connect() as db:
        try:
            import resume_job_matcher
            if not resume_job_matcher._verified_saved_employer_source(db,job):return "The official employer source is not currently verified and healthy."
        except Exception:return "The official employer source could not be verified."
    return ""


def _derive_standing_policy_authorization(job_key,resume_id,application_url):
    """Persist an auditable per-job grant narrowed from the explicitly approved standing policy."""
    if not get_authorization()["settings"].get("standing_policy_enabled"):return False,_standing_policy_blocker({})
    # Auto Apply must perform the same public, GET-only compatibility check a
    # user can request manually. The inspection contains no candidate data.
    # Reuse a current report, including negative reports, to avoid repeated
    # network work for a form that requires human review.
    try:
        job=_get_job(DryRunRequest(job_key=job_key))
    except HTTPException as error:
        return False,str(error.detail)[:300]
    if _public_url_for_confirmation(application_url)!=_public_url_for_confirmation(job.get("application_url")):
        return False,"The application URL changed since the saved posting was loaded."
    posting_blocker=_standing_policy_blocker(job)
    if posting_blocker:
        if any(term in posting_blocker.casefold() for term in ("closed and reopened","no longer verified","inactive","not currently available")):
            _record_application_attention(job,"posting_review",posting_blocker,eligibility_state="needs_review",form_state="not_checked")
        return False,posting_blocker
    inspection=None
    with connect() as db:
        row=db.execute("SELECT report_json,inspected_at FROM application_form_inspections WHERE user_id=? AND job_key=? ORDER BY inspected_at DESC LIMIT 1",
                       (USER,job_key)).fetchone()
    if row:
        try:
            saved_report=json.loads(row["report_json"] or "{}")
            inspected_at=datetime.fromisoformat(str(row["inspected_at"] or "").replace("Z","+00:00"))
            if inspected_at.tzinfo is None:inspected_at=inspected_at.replace(tzinfo=timezone.utc)
            age=(datetime.now(timezone.utc)-inspected_at).total_seconds()
            if (saved_report.get("requested_application_url")==_public_url_for_confirmation(application_url)
                    and 0<=age<=86400):
                inspection=saved_report
        except (TypeError,ValueError,json.JSONDecodeError):
            inspection=None
    if inspection is None:
        try:
            inspection=inspect_public_application_form(job.get("application_url"))
            inspection=_save_form_inspection(job,inspection,preparation_preflight=True)
        except (HTTPException,ValueError) as error:
            reason=(str(getattr(error,"detail",error)) or "The public form inspection could not be completed safely.")[:500]
            _record_application_attention(job,"employer_clarification",reason,eligibility_state="needs_review",form_state="inspection_blocked")
            return False,reason[:300]
        except Exception as error:
            reason=f"The public form inspection failed safely ({type(error).__name__})."
            _record_application_attention(job,"employer_clarification",reason,eligibility_state="needs_review",form_state="inspection_blocked")
            return False,reason
    try:job,resume,candidate,answers,fields,documents,report,snapshot=_saved_preparation_context(job_key)
    except HTTPException as error:return False,str(error.detail)[:300]
    if _public_url_for_confirmation(application_url)!=_public_url_for_confirmation(job.get("application_url")):
        return False,"The application URL changed since the saved posting was loaded."
    blocker=_standing_policy_blocker(job)
    if blocker:return False,blocker
    if not report or _preparation_inspection_blocker(report):
        reason=(str((report or {}).get("reason") or "The employer form is not verified as supported; human review is required.")[:500])
        report=report or {}
        code=("human_verification" if report.get("human_actions_required") or report.get("inspection_outcome")=="needs_human_action"
              else "unsupported_form" if not report.get("form_detected") or report.get("compatibility_status") in {"unsupported","posting_only"}
              else "employer_clarification")
        _record_application_attention(job,code,reason,eligibility_state="needs_review",
            form_state=str(report.get("compatibility_status") or report.get("inspection_outcome") or "unknown"),report=report)
        return False,reason
    if _unmapped_requirements_blocker(job_key,resume_id):
        reason="Mandatory posting requirements remain unresolved."
        _record_application_attention(job,"applicant_information",reason,eligibility_state="needs_review",
                                      form_state=str(report.get("compatibility_status") or "unknown"),report=report)
        return False,reason
    settings=get_authorization()["settings"]
    available={str(item["key"]) for item in fields}
    approved=set(settings.get("standing_policy_field_keys") or [])
    if settings.get("standing_policy_reuse_confirmed_answers") and settings.get("allow_auto_use_confirmed_answers"):
        if "reusable_confirmed_answers" in approved:
            approved.update(key for key in available if key.startswith("custom:"))
    approved.discard("reusable_confirmed_answers")
    allowed_fields=sorted((approved & available)-{"work_authorization","requires_sponsorship","salary_expectations"})
    docs_available={str(item["type"]) for item in documents}
    allowed_docs=sorted(set(settings.get("standing_policy_document_types") or []) & docs_available)
    if "resume" not in allowed_docs:return False,"The active resume is not authorized by the standing policy."
    ai=bool(settings.get("standing_policy_allow_ai_answers") and settings.get("allow_ai_drafting") and
        (settings.get("matching_provider")=="ollama" or
         (settings.get("matching_provider")=="gemini" and settings.get("standing_policy_allow_cloud_ai")) or
         (settings.get("matching_provider")=="groq" and settings.get("allow_groq_cloud_ai") and settings.get("standing_policy_allow_groq"))))
    cloud=bool(ai and ((settings.get("standing_policy_allow_cloud_ai") and settings.get("matching_provider")=="gemini" and os.getenv("GEMINI_API_KEY","" ).strip()) or
        (settings.get("allow_groq_cloud_ai") and settings.get("standing_policy_allow_groq") and settings.get("matching_provider")=="groq" and groq_api_key())))
    stamp=now();authorization_id=str(uuid.uuid4())
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        cutoff=(datetime.now(timezone.utc)-timedelta(hours=24)).isoformat()
        existing_event=db.execute("SELECT 1 FROM standing_policy_events WHERE user_id=? AND job_key=? AND authorized_at>=? LIMIT 1",
            (USER,job_key,cutoff)).fetchone()
        used=int(db.execute("SELECT COUNT(DISTINCT job_key) FROM standing_policy_events WHERE user_id=? AND authorized_at>=?",
            (USER,cutoff)).fetchone()[0])
        if not existing_event and used>=int(settings.get("standing_policy_daily_limit",3)):
            db.rollback()
            return False,"The standing policy daily preparation limit has been reached."
        db.execute("UPDATE application_preparation_authorizations SET revoked_at=? WHERE user_id=? AND job_key=? AND revoked_at IS NULL",(stamp,USER,job_key))
        db.execute("""INSERT INTO application_preparation_authorizations(
            authorization_id,user_id,job_key,application_url_hash,resume_id,match_updated_at,
            matching_engine_version,profile_fingerprint,answers_fingerprint,documents_fingerprint,
            settings_version,field_keys_json,document_types_json,allow_ai_answers,allow_cloud_ai,
            consent_version,authorized_at,revoked_at,authorization_source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,'standing_policy')""",
            (authorization_id,USER,job_key,snapshot["application_url_hash"],resume_id,snapshot["match_updated_at"],
             snapshot["matching_engine_version"],snapshot["profile_fingerprint"],snapshot["answers_fingerprint"],snapshot["documents_fingerprint"],
             snapshot["settings_version"],json.dumps(allowed_fields),json.dumps(allowed_docs),int(ai),int(cloud),
             PREPARATION_CONSENT_VERSION,stamp))
        db.execute("INSERT INTO standing_policy_events(event_id,user_id,job_key,authorization_id,policy_version,authorized_at) VALUES(?,?,?,?,?,?)",
            (str(uuid.uuid4()),USER,job_key,authorization_id,snapshot["settings_version"],stamp))
    return True,""


def _scoped_candidate(candidate,scope):
    allowed=set(scope.get("field_keys") or [])
    scoped={key:value for key,value in candidate.items() if key.startswith("__") and key!="__application_documents"}
    for key in allowed:
        if key in candidate:scoped[key]=candidate[key]
    if "location" in allowed:
        for key in ("city","state_province","country","postal_code","location"):
            if key in candidate:scoped[key]=candidate[key]
    if "university" in allowed and "education" in candidate:scoped["education"]=candidate["education"]
    if "work_authorization" in allowed and "country_work_authorizations" in candidate:scoped["country_work_authorizations"]=candidate["country_work_authorizations"]
    if "requires_sponsorship" in allowed and "sponsorship_needs_by_country" in candidate:scoped["sponsorship_needs_by_country"]=candidate["sponsorship_needs_by_country"]
    allowed_docs=set(scope.get("document_types") or [])
    if "__application_documents" in candidate:
        scoped["__application_documents"]=[item for item in candidate["__application_documents"] if item.get("document_type") in allowed_docs]
    return scoped


def _scoped_answers(answers,scope):
    allowed=set(scope.get("field_keys") or [])
    result={key:value for key,value in answers.items() if key.startswith("__")}
    result.update({key:value for key,value in answers.items() if key in allowed})
    result["__preparation_authorization"]={key:scope.get(key) for key in
        ("authorization_id","field_keys","document_types","allow_ai_answers","allow_cloud_ai","authorized_at")}
    settings=dict(result.get("__settings") or {})
    # Confirmed ordinary profile fields are authorized independently from
    # reusable answer-bank entries. The candidate has already been filtered
    # to this grant's allowlisted field keys.
    settings["allow_confirmed_profile_fields"]=bool(set(scope.get("field_keys") or [])-{"reusable_confirmed_answers"})
    # Answer-bank reuse still requires an explicitly scoped custom answer.
    settings["allow_auto_use_confirmed_answers"]=any(key.startswith("custom:") for key in allowed)
    result["__settings"]=settings
    return result


def _authorized_preparation_payload(job,resume,candidate,answers):
    if _mock_allowed(job.get("application_url","")):
        answers=_application_context(job,answers)
        return candidate,answers
    if not job.get("job_key"):
        raise HTTPException(403,"Real employer preparation requires a saved job with a current, job-specific authorization.")
    scope=_current_preparation_authorization(job["job_key"],resume["id"],job.get("application_url"))
    if not scope:
        raise HTTPException(403,"Authorize this employer, exact application URL, selected profile fields, documents, and AI use before preparation.")
    candidate=_scoped_candidate(candidate,scope)
    answers=_application_context(job,answers)
    answers=_scoped_answers(answers,scope)
    return candidate,answers


def _save_form_inspection(job,report,*,preparation_preflight=False):
    try:
        requested=_public_url_for_confirmation(job.get("application_url"))
        posting=_public_url_for_confirmation(job.get("job_url") or job.get("application_url"))
        stamp=now()
        page_meta=report.get("page_metadata") if isinstance(report.get("page_metadata"),dict) else {}
        employer=str(job.get("company") or page_meta.get("employer") or "")[:160]
        title=str(job.get("title") or page_meta.get("job_title") or "")[:200]
        canonical_application=_public_url_for_confirmation(report.get("application_url") or requested)
        report.update({"job_key":str(job.get("job_key") or "")[:250],"employer":employer,
            "job_title":title,"posting_url":posting,
            "requested_application_url":requested,"canonical_application_url":canonical_application,
            "inspected_at":stamp,"inspection_timestamp":stamp,"preparation_preflight":bool(preparation_preflight)})
        if not report.get("form_fingerprint"):
            report["form_fingerprint"]=_sha256_json({"provider":(report.get("provider") or {}).get("provider"),
                "questions":report.get("questions") or [],"fields":report.get("fields") or [],
                "submission_control_status":report.get("submission_control_status")})
        if not report.get("compatibility_status"):
            if report.get("failure_category") or report.get("robots_policy")!="verified_allow":status="inspection_blocked"
            elif report.get("page_classification") in {"job_posting","posting_redirected"}:status="posting_only"
            elif report.get("inspection_outcome")=="needs_human_action":status="needs_human_action"
            elif report.get("inspection_outcome")=="inaccessible":status="inaccessible"
            elif not report.get("form_detected"):status="unsupported"
            elif report.get("unresolved_controls") or report.get("submission_control_status") not in {"detected_visible","detected_hidden"}:status="unresolved"
            elif report.get("inspection_outcome")=="partially_compatible":status="partially_compatible"
            else:status="compatible"
            report["compatibility_status"]=status
        with connect() as db:
            db.execute("INSERT INTO application_form_inspections(id,user_id,job_key,provider,outcome,report_json,inspected_at) VALUES(?,?,?,?,?,?,?)",
                (str(report.get("inspection_id") or uuid.uuid4()),USER,str(job.get("job_key") or "")[:250],str((report.get("provider") or {}).get("provider") or "unknown")[:40],
                 str(report.get("inspection_outcome") or "unsupported")[:40],json.dumps(report,ensure_ascii=False,separators=(",",":")),report["inspected_at"]))
        blocker=_preparation_inspection_blocker(report) if preparation_preflight else ""
        if blocker and job.get("job_key") and not str(job.get("job_key")).startswith("standalone:"):
            code=("human_verification" if report.get("human_actions_required") or report.get("inspection_outcome")=="needs_human_action"
                  else "unsupported_form" if not report.get("form_detected") or report.get("compatibility_status") in {"unsupported","posting_only"}
                  else "employer_clarification")
            _record_application_attention(job,code,blocker,eligibility_state="needs_review",
                form_state=str(report.get("compatibility_status") or report.get("inspection_outcome") or "unknown"),report=report)
    except sqlite3.Error as exc:
        raise HTTPException(500,"The read-only employer form check ran, but its report could not be saved.") from exc
    return report


def _preparation_inspection_blocker(report):
    if not report.get("robots_policy")=="verified_allow":return report.get("reason") or "Robots policy did not permit verifying this form."
    if not report.get("form_detected") or report.get("page_classification")!="application_form":return report.get("reason") or "A job-specific application form could not be verified."
    if report.get("inspection_outcome") not in {"compatible","partially_compatible"}:return report.get("reason") or "The form needs a human action before preparation."
    if report.get("human_actions_required"):return "The employer form requires a human action. Resolve it manually before preparation."
    unsupported=set(report.get("unsupported_fields") or [])
    required_unsupported=[item.get("label") for item in report.get("fields",[]) if item.get("required") and item.get("label") in unsupported]
    if required_unsupported:return "Required form controls are not supported: "+", ".join(required_unsupported[:8])
    unresolved=[item for item in report.get("unresolved_controls",[]) if item.get("required") is not False]
    if unresolved:return "Required form controls could not be identified reliably: "+", ".join(str(item.get("question") or "Unlabeled field") for item in unresolved[:8])
    unknown_requirements=report.get("unknown_requirement_questions") or []
    if unknown_requirements:return "Required status is unknown for one or more employer questions. Review the form manually before preparation."
    if report.get("submission_control_status") not in {"detected_visible","detected_hidden"}:
        return "The final submission control could not be identified. Form safety remains unresolved, so preparation is blocked."
    return ""


def _saved_preparation_context(job_key,*,allow_unresolved=False,validate_destination=True):
    job=_get_job(DryRunRequest(job_key=job_key))
    if not job.get("active",1):raise HTTPException(410,"This posting is no longer active.")
    try:
        if validate_destination:validate_application_url(job.get("application_url"))
        elif not _url_syntax_allowed(job.get("application_url")):
            raise ApplicationURLValidationError("invalid_scheme","Application links must use a public HTTPS destination.")
    except ValueError as error:raise HTTPException(400,str(error))
    if _mock_allowed(job.get("application_url")):
        raise HTTPException(409,"Local synthetic forms use the separate test mode and do not need real-employer authorization.")
    resume,candidate,answers=_candidate()
    answers=_application_context(job,answers)
    with connect() as db:
        classification,reviewed,unresolved=_eligibility_gate(db,job_key,resume["id"])
        match=db.execute("SELECT status,classification,updated_at,freshness_status,matching_model_version FROM job_match_results WHERE user_id=? AND job_key=? AND resume_id=? ORDER BY created_at DESC LIMIT 1",(USER,job_key,resume["id"])).fetchone()
        inspected=db.execute("SELECT report_json,inspected_at FROM application_form_inspections WHERE user_id=? AND job_key=? ORDER BY inspected_at DESC LIMIT 1",(USER,job_key)).fetchone()
    if (not match or match["status"]!="completed" or match["freshness_status"]!="current" or
        classification not in {"potential_match","needs_review"} or
        (not allow_unresolved and (unresolved or (classification=="needs_review" and not reviewed)))):
        raise HTTPException(409,"A fresh eligible result or reviewed, resolved eligibility result is required before job-specific authorization.")
    report=None
    if inspected:
        try:report=json.loads(inspected["report_json"] or "{}")
        except (TypeError,json.JSONDecodeError):report=None
    current_url=_public_url_for_confirmation(job.get("application_url"))
    if not isinstance(report,dict) or report.get("requested_application_url")!=current_url:
        report=None
    if report:
        try:
            inspected_at=datetime.fromisoformat(str(inspected["inspected_at"]).replace("Z","+00:00"))
            if inspected_at.tzinfo is None:inspected_at=inspected_at.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc)-inspected_at).total_seconds()>86400:report=None
        except (TypeError,ValueError):report=None
    fields,documents=_preparation_options(candidate,answers)
    snapshot=_preparation_snapshot(job_key,resume["id"],job.get("application_url"))
    return job,resume,candidate,answers,fields,documents,report,snapshot


@router.get("/api/applications/saved-jobs/{job_key}/preparation-authorization")
def preparation_authorization_plan(job_key:str):
    settings=get_authorization()["settings"]
    if not settings.get("allow_automated_form_preparation"):
        raise HTTPException(403,"Enable automated form preparation in Application settings before authorizing a real employer.")
    job,resume,candidate,answers,fields,documents,report,snapshot=_saved_preparation_context(
        job_key,allow_unresolved=True,validate_destination=False)
    blocker=_preparation_inspection_blocker(report) if report else "Read-only form inspection is required before authorizing this employer."
    requirements_blocker=_unmapped_requirements_blocker(job_key,resume["id"])
    if requirements_blocker:blocker=(blocker+" "+requirements_blocker).strip()
    existing=_current_preparation_authorization(job_key,resume["id"],job.get("application_url"))
    saved=next((item for item in list_saved_application_jobs(eligibility="all")["results"] if item.get("job_key")==job_key),{})
    eligibility=saved.get("eligibility_review_assessment") if isinstance(saved.get("eligibility_review_assessment"),dict) else {}
    with connect() as db:
        match_row=db.execute("SELECT result_json FROM job_match_results WHERE user_id=? AND job_key=? AND resume_id=? ORDER BY created_at DESC LIMIT 1",(USER,job_key,resume["id"])).fetchone()
        attempt_count=db.execute("SELECT COUNT(*) FROM applications WHERE user_id=? AND job_key=?",(USER,job_key)).fetchone()[0]
        settings_row=db.execute("SELECT settings_json FROM application_authorizations WHERE user_id=?",(USER,)).fetchone()
    try:match_data=json.loads(match_row["result_json"] or "{}") if match_row else {}
    except (TypeError,json.JSONDecodeError):match_data={}
    try:application_settings=json.loads(settings_row["settings_json"] or "{}") if settings_row else {}
    except (TypeError,json.JSONDecodeError):application_settings={}
    qualitative_questions=_qualitative_missing_question_keys(match_data)
    missing_questions=list(dict.fromkeys([
        *[str(value)[:500] for value in match_data.get("missing_information",[]) if str(value or "").strip()
          and str(value).strip().casefold() not in qualitative_questions],
        *[str(item.get("missing_question"))[:500] for item in match_data.get("technical_qualifications",[])
          if isinstance(item,dict) and item.get("requiredness")=="required" and item.get("status")=="needs_review" and item.get("missing_question")
          and not _is_qualitative_employer_expectation(item.get("requirement"))],
        *([str(match_data.get("availability_assessment",{}).get("missing_question"))[:500]]
          if isinstance(match_data.get("availability_assessment"),dict) and match_data["availability_assessment"].get("missing_question") else [])]))
    blockers=list(eligibility.get("blockers",[]))
    if blocker:blockers.append({"category":"form_or_authorization_gate","requirement":"Application preparation gate","explanation":blocker})
    if eligibility.get("next_action"):blockers.append({"category":"next_action","requirement":"Applicant review","explanation":eligibility["next_action"]})
    blockers=list({(str(item.get("category")),str(item.get("requirement")),str(item.get("explanation"))):item for item in blockers if isinstance(item,dict)}.values())
    preparation_ready=bool(not blocker and not eligibility.get("preparation_blocked") and snapshot.get("classification")=="potential_match")
    current_authorization=bool(existing and preparation_ready)
    return {"job_key":job_key,"employer":str(job.get("company") or "")[:200],"title":str(job.get("title") or "")[:300],
        "posting_url":_public_url_for_confirmation(job.get("job_url") or job.get("application_url")),
        "application_url":_public_url_for_confirmation(job.get("application_url")),
        "posting_verified_at":str(job.get("last_verified_at") or ""),"provider":detect_application_provider(job.get("application_url")),"eligibility":snapshot["classification"],
        "eligibility_assessment":eligibility,"missing_requirements":[
            *[item for field in ("required_skills","required_experience","education_requirements","technical_qualifications")
              for item in match_data.get(field,[]) if isinstance(item,dict) and item.get("requiredness")!="preferred" and
              not _is_qualitative_employer_expectation(item.get("requirement")) and item.get("status") in {"gap","unclear","needs_review","confirmed_mismatch"}],
            *([{**match_data["availability_assessment"],"category":"availability"}] if isinstance(match_data.get("availability_assessment"),dict) and
               match_data["availability_assessment"].get("requiredness")=="required" and match_data["availability_assessment"].get("status") in {"needs_review","confirmed_mismatch"} else []),
            *[{"requirement":item,"requiredness":"required","status":"unassessed","explanation":"This explicit mandatory posting item has not been evaluated."}
              for item in _saved_unmapped_required_items(job_key,resume["id"])]],
        "missing_applicant_information":missing_questions,"preparation_blockers":blockers,"preparation_ready":preparation_ready,
        "next_action":eligibility.get("next_action") or "Resolve the listed eligibility and form blockers before authorization.",
        "candidate_fields":fields,"available_documents":documents,"inspection":report,
        "inspection_blocker":blocker,"authorization_current":current_authorization,"preparation_authorized":current_authorization,
        "attempt_history_count":int(attempt_count),"live_submission_enabled":False,
        "ai_answers_proposed":[],"ai_answers_authorized":False,
        "cloud_processing_permission":"Requires a separate explicit job-specific applicant choice; no cloud processing is authorized by this proposal.",
        "ai_drafting_enabled":bool(settings.get("allow_ai_drafting")),
        "provider_selected":settings.get("matching_provider","ollama"),
        "local_ai_answer_permission_available":bool(settings.get("allow_ai_drafting") and settings.get("matching_provider","ollama")=="ollama"),
        "cloud_provider_configured":(bool(os.getenv("GEMINI_API_KEY","" ).strip()) if settings.get("matching_provider")=="gemini" else
            bool(groq_api_key()) if settings.get("matching_provider")=="groq" else False),
        "groq_cloud_permission_available":bool(settings.get("allow_groq_cloud_ai")),
        "cloud_ai_authorized":False,"human_only_actions":(report.get("human_actions_required",[]) if isinstance(report,dict) else []) or
            ([] if isinstance(report,dict) and report.get("form_detected") else ["Employer application form controls and final submission boundary remain unverified."]),
        "authorization_version":snapshot["settings_version"]}


@router.post("/api/applications/saved-jobs/{job_key}/preparation-authorization")
def authorize_saved_job_preparation(job_key:str,request:PreparationAuthorizationRequest):
    if not request.confirmed:raise HTTPException(422,"Confirm the employer, role, and exact application URL before saving authorization.")
    settings=get_authorization()["settings"]
    if not settings.get("allow_automated_form_preparation"):
        raise HTTPException(403,"Enable automated form preparation in Application settings first.")
    job,resume,candidate,answers,fields,documents,report,snapshot=_saved_preparation_context(job_key)
    if _public_url_for_confirmation(request.confirmed_application_url)!=_public_url_for_confirmation(job.get("application_url")):
        raise HTTPException(409,"The application URL changed. Review the current employer link and authorize it again.")
    if str(request.confirmed_employer).strip()!=str(job.get("company") or "").strip() or str(request.confirmed_title).strip()!=str(job.get("title") or "").strip():
        raise HTTPException(409,"The saved employer or job title changed. Refresh the job card and confirm again.")
    blocker=_preparation_inspection_blocker(report) if report else "Read-only form inspection is required before authorizing this employer."
    if blocker:raise HTTPException(409,blocker)
    requirements_blocker=_unmapped_requirements_blocker(job_key,resume["id"])
    if requirements_blocker:raise HTTPException(409,requirements_blocker)
    available_fields={str(item["key"]) for item in fields}
    requested_fields=list(dict.fromkeys(str(key) for key in request.field_keys))
    if set(requested_fields)-available_fields:raise HTTPException(422,"The selected candidate field list is out of date. Refresh and review the authorization plan.")
    available_documents={str(item["type"]) for item in documents}
    requested_documents=list(dict.fromkeys(str(kind).casefold() for kind in request.document_types))
    if set(requested_documents)-available_documents or set(requested_documents)-PREPARATION_DOCUMENT_TYPES:
        raise HTTPException(422,"A selected document is not currently available for this resume version.")
    provider=settings.get("matching_provider","ollama")
    if request.allow_ai_answers and not settings.get("allow_ai_drafting"):
        raise HTTPException(403,"Enable AI drafting in Application settings before authorizing generated answers.")
    if request.allow_ai_answers and provider=="gemini" and not request.allow_cloud_ai:
        raise HTTPException(409,"Gemini would process the selected facts and job description in the cloud. Authorize that explicitly or turn off AI answers for this job.")
    if request.allow_ai_answers and provider=="groq" and not request.allow_cloud_ai:
        raise HTTPException(409,"Groq would process the selected facts and job description in the cloud. Authorize Groq explicitly for this job or turn off AI answers.")
    if request.allow_cloud_ai and provider=="gemini" and not os.getenv("GEMINI_API_KEY","" ).strip():
        raise HTTPException(409,"Gemini cloud AI authorization requires GEMINI_API_KEY configured.")
    if request.allow_cloud_ai and provider=="groq" and (not groq_api_key() or not settings.get("allow_groq_cloud_ai")):
        raise HTTPException(409,"Groq cloud AI requires explicit Groq permission in Application Settings and a configured GROQ_API_KEY.")
    if request.allow_cloud_ai and provider not in {"gemini","groq"}:
        raise HTTPException(409,"Cloud AI authorization requires an explicitly selected cloud provider.")
    if request.allow_cloud_ai and not request.allow_ai_answers:
        raise HTTPException(422,"Authorize AI-generated answers before authorizing cloud AI processing.")
    stamp=now();authorization_id=str(uuid.uuid4())
    with connect() as db:
        db.execute("UPDATE application_preparation_authorizations SET revoked_at=? WHERE user_id=? AND job_key=? AND revoked_at IS NULL",(stamp,USER,job_key))
        db.execute("""INSERT INTO application_preparation_authorizations(
            authorization_id,user_id,job_key,application_url_hash,resume_id,match_updated_at,
            matching_engine_version,profile_fingerprint,answers_fingerprint,documents_fingerprint,
            settings_version,field_keys_json,document_types_json,allow_ai_answers,allow_cloud_ai,
            consent_version,authorized_at,revoked_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)""",
            (authorization_id,USER,job_key,snapshot["application_url_hash"],resume["id"],snapshot["match_updated_at"],
             snapshot["matching_engine_version"],snapshot["profile_fingerprint"],snapshot["answers_fingerprint"],snapshot["documents_fingerprint"],
             snapshot["settings_version"],json.dumps(requested_fields),json.dumps(requested_documents),int(request.allow_ai_answers),
             int(request.allow_cloud_ai),PREPARATION_CONSENT_VERSION,stamp))
    return {"authorized":True,"authorization_id":authorization_id,"authorized_at":stamp,
        "field_count":len(requested_fields),"document_types":requested_documents,
        "ai_answers_authorized":request.allow_ai_answers,"cloud_ai_authorized":request.allow_cloud_ai,
        "message":"Job-specific preparation authorization saved. It is limited to this posting, resume/profile version, match result, selected fields, and selected documents. Submission remains disabled."}


@router.delete("/api/applications/saved-jobs/{job_key}/preparation-authorization")
def revoke_saved_job_preparation(job_key:str):
    stamp=now()
    with connect() as db:
        changed=db.execute("UPDATE application_preparation_authorizations SET revoked_at=? WHERE user_id=? AND job_key=? AND revoked_at IS NULL",(stamp,USER,job_key)).rowcount
    return {"revoked":bool(changed),"message":"Job-specific preparation authorization revoked. Existing application history is preserved."}


@router.get("/api/applications/saved-jobs")
def list_saved_application_jobs(q:str="",eligibility:str="all"):
    """Saved openings enriched with the active resume's match and application state."""
    query=(q or "").strip().casefold()[:200]
    if eligibility not in {"all","potential_match","needs_review","not_eligible","unmatched"}:
        raise HTTPException(422,"Unknown eligibility filter.")
    with connect() as db:
        rows=db.execute("""SELECT o.company,o.title,o.location,o.url,o.job_url,o.job_key,o.discovered_at,o.reopened_review_required,
                o.employment_type,o.work_arrangement,o.location_status,o.location_reason,o.active,o.source_platform,o.posting_date,
                m.classification,m.match_score,m.status AS match_status,m.result_json AS match_result_json,m.freshness_status,m.matching_model_version,
                m.candidate_fingerprint,m.preferences_fingerprint,m.job_fingerprint,m.updated_at AS match_updated_at,
                a.id AS application_id,a.state AS application_state,a.updated_at AS application_updated_at,
                a.diagnostic_json AS application_diagnostic,
                (SELECT fi.report_json FROM application_form_inspections fi WHERE fi.user_id=o.user_id AND fi.job_key=o.job_key ORDER BY fi.inspected_at DESC LIMIT 1) AS form_inspection_json,
                r.id AS active_resume_id, CASE WHEN r.id IS NULL THEN 0 ELSE 1 END AS has_active_resume,
                r.stored_name AS active_resume_stored_name
            FROM discovered_openings o
            LEFT JOIN resumes r ON r.user_id=o.user_id AND r.active=1
            LEFT JOIN job_match_results m ON m.user_id=o.user_id AND m.job_key=o.job_key AND m.resume_id=r.id
            LEFT JOIN applications a ON a.user_id=o.user_id AND
                (a.job_key=o.job_key OR lower(rtrim(a.application_url,'/'))=lower(rtrim(o.url,'/')))
            WHERE o.user_id=? ORDER BY o.discovered_at DESC""",(USER,)).fetchall()
    authorized=bool(get_authorization()["settings"].get("allow_automated_form_preparation"))
    results=[]
    confirmed_projects_available=False
    active_resume_id=next((row["active_resume_id"] for row in rows if row["active_resume_id"]),"")
    if active_resume_id:
        with connect() as db:
            verified=db.execute("SELECT values_json,confirmed_json FROM candidate_verifications WHERE user_id=? AND resume_id=?",
                (USER,active_resume_id)).fetchone()
        if verified:
            try:
                confirmed_profile=verified_profile_facts(verified_values_from_json(verified["values_json"]),
                    json.loads(verified["confirmed_json"] or "{}"))
                confirmed_projects_available=bool(confirmed_profile.get("project_entries") or confirmed_profile.get("projects"))
            except (TypeError,ValueError,json.JSONDecodeError):
                confirmed_projects_available=False
    for row in rows:
        item=dict(row)
        if query and query not in " ".join(str(item.get(k) or "") for k in ("company","title","location","employment_type")).casefold():
            continue
        classification=item.get("classification")
        if eligibility=="unmatched" and classification: continue
        if eligibility not in {"all","unmatched"} and classification!=eligibility: continue
        item["application_state"]=item.get("application_state") or "not_started"
        try: match_data=json.loads(item.pop("match_result_json",None) or "{}")
        except (TypeError,json.JSONDecodeError): match_data={}
        item["eligibility_explanation"]=str(match_data.get("explanation") or "")[:600]
        item["application_status_label"]=("Needs Human Review · Reopened requisition" if item.get("reopened_review_required") else {"not_started":"Not prepared","queued":"Queued","opening":"Opening employer site","inspecting":"Inspecting form","filling":"Preparing form","uploading_resume":"Uploading resume","needs_user_input":"Needs your input","ready_for_review":"Ready for review","failed":"Failed","unsupported":"Unsupported website","paused":"Paused","submitted":"Submitted","skipped_duplicate":"Duplicate skipped"}.get(item["application_state"],"In progress"))
        try:item["job_posting_url"]=urlparse(str(item.get("job_url") or item.get("url") or ""))._replace(query="",fragment="").geturl()
        except ValueError:item["job_posting_url"]=""
        item.pop("job_url",None)
        try:
            app_diagnostic=json.loads(item.pop("application_diagnostic",None) or "{}")
        except (TypeError,json.JSONDecodeError):
            app_diagnostic={}
        item["application_message"]=str(app_diagnostic.get("message") or "")[:500]
        try:
            inspection=json.loads(item.pop("form_inspection_json",None) or "null")
        except (TypeError,json.JSONDecodeError):
            inspection=None
        try: current_application_url=urlparse(str(item.get("url") or ""))._replace(query="",fragment="").geturl()
        except ValueError: current_application_url=""
        if isinstance(inspection,dict) and inspection.get("requested_application_url")!=current_application_url:
            inspection=None
        item["form_inspection"]=inspection if isinstance(inspection,dict) else None
        item["question_groups"]=_group_questions(app_diagnostic.get("unanswered_fields",[]))
        item["missing_questions"]=[entry["question"] for group in item["question_groups"] for entry in group["questions"]]
        item["url_syntax_allowed"]=_url_syntax_allowed(item.get("url"))
        resume_name=Path(str(item.get("active_resume_stored_name") or ""))
        resume_path=(STORAGE/resume_name.name).resolve()
        resume_available=bool(item["has_active_resume"] and resume_name.name==str(item.get("active_resume_stored_name") or "") and resume_path.parent==STORAGE.resolve() and resume_path.is_file())
        with connect() as db:
            gate_class,reviewed,unresolved=_eligibility_gate(db,item.get("job_key"),item.get("active_resume_id")) if item.get("active_resume_id") else ("not_analyzed",False,True)
            opening_description=db.execute("SELECT description,work_arrangement FROM discovered_openings WHERE user_id=? AND job_key=?",
                (USER,item.get("job_key"))).fetchone()
        item["eligibility_reviewed"]=reviewed
        try:
            from resume_job_matcher import matching_engine_version
            expected_version=matching_engine_version()
        except Exception:expected_version=""
        item["eligibility_stale"]=bool(item.get("freshness_status")=="stale" or
            (item.get("matching_model_version") not in (None,"") and item.get("matching_model_version")!=expected_version))
        match_data=match_data if isinstance(match_data,dict) else {}
        item["technical_qualifications"]=(match_data.get("technical_qualifications")
            if isinstance(match_data.get("technical_qualifications"),list) else [])
        unmapped_required=_unmapped_required_posting_items(
            (opening_description["description"] if opening_description else item.get("description")) or "",match_data)
        review_job={**item,"description":(opening_description["description"] if opening_description else ""),
            "work_arrangement":(opening_description["work_arrangement"] if opening_description else item.get("work_arrangement"))}
        item["eligibility_review_assessment"]=_eligibility_review_assessment(review_job,match_data,
            stale=item["eligibility_stale"],unmapped=unmapped_required,
            confirmed_projects_available=confirmed_projects_available)
        item["technical_qualification_snapshot"]={
            "assessed_at":match_data.get("technical_qualification_assessed_at") or item.get("match_updated_at") or "",
            "candidate_fingerprint":item.get("candidate_fingerprint") or "",
            "preferences_fingerprint":item.get("preferences_fingerprint") or "",
            "job_fingerprint":item.get("job_fingerprint") or "",
            "matching_engine_version":item.get("matching_model_version") or ""}
        review_missing=match_data.get("missing_information") if isinstance(match_data.get("missing_information"),list) else []
        qualitative_questions=_qualitative_missing_question_keys(match_data)
        employment_unknown=not str(item.get("employment_type") or "").strip()
        role_seniority_unknown=any(entry.get("category")=="employer_metadata_uncertainty" and
            "seniority" in str(entry.get("requirement") or "").casefold()
            for entry in item["eligibility_review_assessment"].get("employer_uncertainties",[]))
        has_qualitative_expectation=any(_is_qualitative_employer_expectation(q.get("requirement"))
            for q in match_data.get("technical_qualifications",[]) if isinstance(q,dict) and q.get("requiredness")=="required")
        reviewable_missing=[value for value in review_missing if not _is_qualitative_employer_expectation(value) and str(value).strip().casefold() not in qualitative_questions and not (
            (employment_unknown and re.search(r"employment type.{0,90}(?:not provided|not specified|unknown)",str(value),re.I)) or
            (role_seniority_unknown and re.search(r"experience level.{0,60}(?:not clear|unclear)",str(value),re.I)))]
        item["evidence_review"]={
            "available":bool(item.get("match_status")=="completed" and item.get("classification")=="needs_review" and
                not item["eligibility_stale"] and not unresolved and not reviewable_missing and
                not unmapped_required and
                bool(item.get("candidate_fingerprint") and item.get("preferences_fingerprint") and item.get("job_fingerprint")) and
                (employment_unknown or role_seniority_unknown or match_data.get("ai_evidence_diagnostics") or has_qualitative_expectation)),
            "employment_type_unknown":employment_unknown,
            "role_seniority_unknown":role_seniority_unknown,
            "ai_diagnostics":list(dict.fromkeys([
                *(match_data.get("ai_evidence_diagnostics",[]) if isinstance(match_data.get("ai_evidence_diagnostics"),list) else []),
                *(match_data.get("evidence_consistency",[]) if isinstance(match_data.get("evidence_consistency"),list) else [])]))[:20],
            "snapshot":{"match_updated_at":item.get("match_updated_at") or "",
                "profile_fingerprint":item.get("candidate_fingerprint") or "",
                "preferences_fingerprint":item.get("preferences_fingerprint") or "",
                "job_fingerprint":item.get("job_fingerprint") or "",
                "matching_engine_version":item.get("matching_model_version") or ""},
            "unresolved_information":reviewable_missing,
            "unmapped_required_qualifications":unmapped_required}
        with connect() as db:
            latest_evidence_review=db.execute("""SELECT decision,reviewed_at FROM eligibility_evidence_reviews
                WHERE user_id=? AND job_key=? AND resume_id=? ORDER BY review_id DESC LIMIT 1""",
                (USER,item.get("job_key"),item.get("active_resume_id"))).fetchone() if item.get("active_resume_id") else None
        item["targeted_evidence_review"]={
            "recorded":bool(latest_evidence_review and not item["eligibility_stale"] and gate_class=="potential_match"),
            "decision":latest_evidence_review["decision"] if latest_evidence_review else "",
            "reviewed_at":latest_evidence_review["reviewed_at"] if latest_evidence_review else ""}
        item["evidence_review"]["blocked_reason"]=(
            "The saved assessment is stale. Reassess saved evidence without AI before reviewing it."
            if item["eligibility_stale"] else
            "A mandatory qualification or candidate fact still needs confirmation; an applicant review cannot override it."
            if unresolved else
            "The saved assessment did not evaluate every explicit mandatory qualification ("+"; ".join(unmapped_required)+"). Reassess the posting and candidate evidence before reviewing it."
            if unmapped_required else
            "The saved evidence is ready for a job-specific review."
            if item["evidence_review"]["available"] else
            "No job-specific review is currently required.")
        item["deterministic_reassessment_available"]=bool(item.get("match_status")=="completed" and
            item.get("eligibility_stale") and item.get("match_updated_at"))
        preparation_requirement_blocker=_unmapped_requirements_blocker(item.get("job_key"),item.get("active_resume_id")) if item.get("active_resume_id") else ""
        item["preparation_reason"]=("The saved assessment is stale. Reassess saved evidence without AI before continuing." if item["eligibility_stale"] else
            "AI matching is required before preparation." if gate_class=="not_analyzed" else
            "This opening is not eligible according to the latest match." if gate_class=="not_eligible" else
            "Resolve mandatory requirements before preparing; a review cannot override them." if unresolved else
            preparation_requirement_blocker if preparation_requirement_blocker else
            "Review this match before preparation." if gate_class=="needs_review" and not reviewed else "")
        is_local=_mock_allowed(str(item.get("url") or ""))
        item["preparation_plan_available"]=bool(not is_local and item.get("match_status")=="completed" and
            item.get("freshness_status")=="current" and not item.get("eligibility_stale") and
            gate_class in {"potential_match","needs_review"} and item.get("active") and item.get("url_syntax_allowed"))
        scoped_authorization=(_current_preparation_authorization(item.get("job_key"),item.get("active_resume_id"),item.get("url"))
            if item.get("active_resume_id") and not is_local and gate_class in {"potential_match","needs_review"} and not unresolved else None)
        item["preparation_authorized"]=bool(is_local or scoped_authorization)
        item["preparation_authorization_required"]=bool(not is_local and gate_class in {"potential_match","needs_review"} and not unresolved and not scoped_authorization)
        item["preparation_allowed"]=bool(authorized and item["url_syntax_allowed"] and resume_available and item["application_state"]=="not_started" and not item.get("reopened_review_required") and
            item.get("active",1) and
            gate_class not in {"not_eligible","not_analyzed","rejected","deferred"} and not unresolved and
            (gate_class=="potential_match" or (gate_class=="needs_review" and reviewed)) and item["preparation_authorized"])
        if item.get("reopened_review_required"):
            item["preparation_reason"]="This requisition closed and reopened. Human review is required before any preparation or reapplication."
        elif item["eligibility_stale"]:
            item["preparation_reason"]="The saved assessment is stale. Reassess saved evidence without AI before continuing."
        elif gate_class=="needs_review" and not reviewed:
            assessment=item["eligibility_review_assessment"]
            if assessment.get("confirmed_conflicts"):
                item["preparation_reason"]="A confirmed mandatory conflict blocks preparation and cannot be overridden by review."
            elif assessment.get("mandatory_unresolved"):
                item["preparation_reason"]="Confirm the missing mandatory evidence in Candidate Profile. Acknowledgment cannot replace qualification evidence."
            elif assessment.get("blockers"):
                item["preparation_reason"]="Resolve the listed evidence blockers before requesting job-specific preparation authorization."
            elif assessment.get("employer_uncertainties"):
                item["preparation_reason"]="Review the employer-side uncertainty for this job; no applicant facts or eligibility approval have been assumed."
            else:
                item["preparation_reason"]="Review this match before requesting job-specific preparation authorization."
        if item["preparation_authorization_required"] and gate_class!="needs_review":
            item["preparation_reason"]="Authorize this employer and exact application URL, and choose the profile fields, documents, and AI use for this job."
        item["eligibility_review_assessment"]["preparation_blocked"]=not item["preparation_allowed"]
        item["eligibility_review_assessment"]["preparation_block_reason"]=(
            str(item.get("preparation_reason") or "")[:500] if not item["preparation_allowed"] else "")
        # Query strings and fragments may contain signed or session-like tokens.
        # Keep the original private in SQLite for preparation, but don't return it to the dashboard.
        try: item["url"]=urlparse(str(item.get("url") or ""))._replace(query="",fragment="").geturl()
        except ValueError: item["url"]=""
        item.pop("has_active_resume",None); item.pop("active_resume_stored_name",None); item.pop("active_resume_id",None)
        results.append(item)
    return {"results":results,"count":len(results)}


@router.post("/api/applications/form-inspections", status_code=200)
def inspect_saved_application_form(request: FormInspectionRequest):
    """Read-only compatibility check for a fresh match or reviewable opening.

    Inspecting a public form does not establish eligibility or authorize
    preparation. Keep those gates separate so applicants can review form
    constraints while qualification evidence is unresolved.
    """
    jobs=list_saved_application_jobs(eligibility="all")["results"]
    job=next((item for item in jobs if item.get("job_key")==request.job_key),None)
    if not job:
        raise HTTPException(404,"Saved job not found.")
    if (job.get("classification") not in {"potential_match","needs_review"} or job.get("eligibility_stale") or
            job.get("match_status")!="completed" or job.get("freshness_status")!="current"):
        raise HTTPException(409,"Only a current potential match or needs-review job can be inspected from this action. Reassess the job first.")
    if not job.get("active"):
        raise HTTPException(410,"This posting is no longer active in its saved source.")
    if not job.get("url_syntax_allowed"):
        raise HTTPException(400,"The saved application link is not a permitted public HTTPS destination.")
    with connect() as db:
        saved=db.execute("SELECT url,job_url,title,company FROM discovered_openings WHERE user_id=? AND job_key=?",
                         (USER,request.job_key)).fetchone()
    if not saved:
        raise HTTPException(404,"Saved job not found.")
    report=inspect_public_application_form(saved["url"])
    try: requested_application_url=urlparse(str(saved["url"] or ""))._replace(query="",fragment="").geturl()
    except ValueError: requested_application_url=""
    report.update({"requested_application_url":requested_application_url})
    return _save_form_inspection({"job_key":request.job_key,"company":saved["company"],"title":saved["title"],
        "job_url":saved["job_url"],"application_url":saved["url"]},report)


@router.post("/api/applications/form-compatibility-tests", status_code=200)
def inspect_standalone_application_form(request: StandaloneFormCompatibilityRequest):
    """Inspect a pasted public employer URL without a saved match or candidate profile."""
    raw_url=str(request.application_url or "").strip()
    if not raw_url or len(raw_url)>2048:
        raise HTTPException(400,"Enter a public HTTPS employer job URL (maximum 2,048 characters).")
    source_url=_sanitized_inspection_url(raw_url)
    report=inspect_public_application_form(raw_url)
    report.setdefault("provider",detect_application_provider(raw_url))
    inspection_id=str(uuid.uuid4())
    report.update({"inspection_id":inspection_id,"inspection_mode":"standalone_read_only","source_url":source_url})
    for key in ("application_url","source_url","posting_url","requested_application_url"):
        if report.get(key): report[key]=_sanitized_inspection_url(report[key])
    return _save_form_inspection({"job_key":"standalone:"+inspection_id,"application_url":source_url,"job_url":source_url},report)

def _candidate():
    with connect() as db:
        resume=db.execute("SELECT r.id,r.stored_name,r.original_name FROM resumes r WHERE r.user_id=? AND r.active=1 LIMIT 1",(USER,)).fetchone()
        if not resume: raise HTTPException(409,"Upload and activate a resume before starting a dry run.")
        v=db.execute("SELECT values_json,confirmed_json FROM candidate_verifications WHERE resume_id=? AND user_id=?",(resume["id"],USER)).fetchone()
        ans=db.execute("SELECT id,question_key,question_text,answer,answer_encrypted,sensitive,auto_reuse,answer_type,jurisdiction,question_category,question_context,reuse_scope,scope_job_key,profile_version FROM application_answers WHERE user_id=? AND resume_id=? AND confirmed=1 AND revoked_at IS NULL",(USER,resume["id"])).fetchall()
        answers_by_key={}
        for item in ans:
            answer_value=_answer_plaintext(item,db)
            try:answer_valid=bool(validate_application_answer(item["question_text"],answer_value))
            except ValueError:answer_valid=False
            answers_by_key[item["question_key"]]={"answer":answer_value,"question":item["question_text"],
                "sensitive":bool(item["sensitive"]),"confirmed":True,
                "answer_valid":answer_valid,"auto_reuse":bool(item["auto_reuse"] and answer_valid),
                "answer_type":item["answer_type"],"jurisdiction":item["jurisdiction"],
                "category":item["question_category"],"context":item["question_context"],
                "reuse_scope":item["reuse_scope"],"scope_job_key":item["scope_job_key"],
                "profile_version":item["profile_version"]}
    stored=Path(str(resume["stored_name"] or ""))
    resume_path=(STORAGE/stored.name).resolve()
    if stored.name!=str(resume["stored_name"] or "") or resume_path.parent!=STORAGE.resolve() or not resume_path.is_file():
        raise HTTPException(409,"The active resume file is unavailable or invalid. Upload and activate a valid PDF first.")
    values={}; confirmed={}
    if v:
        try: values=verified_values_from_json(v["values_json"]); confirmed=json.loads(v["confirmed_json"])
        except (TypeError,json.JSONDecodeError): raise HTTPException(409,"Candidate verification data is invalid.")
    date_value=str(values.get("graduation_date") or "")
    if date_value and re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])",date_value) and confirmed.get("graduation_date"):
        values.setdefault("graduation_year",date_value[:4]); values.setdefault("graduation_month",date_value[5:7])
        confirmed.setdefault("graduation_year",True); confirmed.setdefault("graduation_month",True)
    facts=verified_profile_facts(values,confirmed)
    # User-uploaded attachments remain private and are surfaced to the form
    # filler only after checking the active resume scope and canonical path.
    try:
        with connect() as db:
            docs=db.execute("SELECT document_type,stored_name,original_name,mime_type FROM application_documents WHERE user_id=? AND resume_id=? ORDER BY created_at DESC",
                            (USER,resume["id"])).fetchall()
        private_docs=(Path(__file__).resolve().parent/"private_documents").resolve()
        facts["__application_documents"]=[]
        for doc in docs:
            name=str(doc["stored_name"] or "")
            if not re.fullmatch(r"[0-9a-f-]{36}\.(?:pdf|docx|png|jpg|jpeg)",name,re.I):continue
            path=(private_docs/name).resolve()
            if path.parent==private_docs and path.is_file():
                facts["__application_documents"].append({"document_type":doc["document_type"],
                    "original_name":doc["original_name"],"mime_type":doc["mime_type"],"path":str(path)})
    except sqlite3.OperationalError:
        facts["__application_documents"]=[]
    answers=answers_by_key
    answers["__settings"]=get_authorization()["settings"]
    return resume,facts,answers

def _fingerprint(job): return str(job.get("job_key") or ("url:"+str(job.get("application_url","" )).split("?",1)[0].rstrip("/").casefold())).strip()

def _create(job,resume,allow_queue=False,priority=0):
    try: validate_application_url(job.get("application_url"))
    except ValueError as e: raise HTTPException(400,str(e))
    key=_fingerprint(job); stamp=now()
    with _attempt_lock, connect() as db:
        db.execute("BEGIN IMMEDIATE")
        _recover_stale_in_db(db)
        identity=_opening_identity(job)
        reservation=db.execute("SELECT a.id,a.state FROM application_opening_reservations r JOIN applications a ON a.id=r.application_id WHERE r.user_id=? AND r.identity_hash=?",
                               (USER,identity["hash"])).fetchone()
        if not reservation and identity["url"]:
            aliases=db.execute("SELECT a.id,a.state,x.identity_json FROM application_opening_aliases x JOIN applications a ON a.id=x.application_id WHERE x.user_id=? AND x.canonical_url=?",
                               (USER,identity["url"])).fetchall()
            for alias in aliases:
                try:prior_identity=json.loads(alias["identity_json"] or "{}")
                except (TypeError,json.JSONDecodeError):prior_identity={}
                old_req=prior_identity.get("requisition");new_req=identity["parts"].get("requisition")
                if old_req and new_req and old_req!=new_req:continue
                reservation=alias;break
        old=db.execute("SELECT id,state,attempt_number FROM applications WHERE user_id=? AND job_key=?",(USER,key)).fetchone()
        if reservation and (not old or reservation["id"]!=old["id"]):
            return reservation["id"],"skipped_duplicate",False
        active=db.execute("SELECT id FROM applications WHERE user_id=? AND state IN ('queued','opening','inspecting','filling','uploading_resume') LIMIT 1",(USER,)).fetchone()
        if active and (not old or active["id"]!=old["id"]) and not allow_queue:
            raise HTTPException(409,"Another application dry run is active. Wait for it to finish before preparing another job.")
        normalized_url=str(job["application_url"]).split("?",1)[0].rstrip("/").casefold()
        if not old and not identity["parts"].get("requisition"):
            previous=db.execute("SELECT id,state,application_url FROM applications WHERE user_id=?",(USER,)).fetchall()
            old=next((row for row in previous if str(row["application_url"]).split("?",1)[0].rstrip("/").casefold()==normalized_url),None)
        if old and old["state"]=="submitted": return old["id"],"skipped_duplicate",False
        if old and old["state"] in {"failed","paused","needs_user_input"} and int(old["attempt_number"] or 1)>=MAX_APPLICATION_ATTEMPTS:
            if allow_queue:return old["id"],old["state"],False
            raise HTTPException(409,"The bounded preparation retry limit was reached. Continue manually at the official employer site.")
        if old and old["state"] in {"queued","opening","inspecting","filling","uploading_resume"}:
            if allow_queue and old["state"]=="queued": return old["id"],old["state"],False
            raise HTTPException(409,"This application is already being processed.")
        if old and allow_queue and old["state"] in {"failed","paused","needs_user_input","unsupported"}:
            return old["id"],old["state"],False
        if old and old["state"] not in {"failed","paused","needs_user_input","unsupported"}:
            return old["id"],old["state"],False
        if old:
            app_id=old["id"]; db.execute("UPDATE applications SET resume_id=?,company=?,title=?,application_url=?,state='queued',queue_priority=?,diagnostic_json='{}',error=NULL,updated_at=?,state_changed_at=?,started_at=NULL,heartbeat_at=NULL,attempt_number=attempt_number+1 WHERE id=?",(resume["id"],str(job.get("company",""))[:200],str(job.get("title",""))[:300],job["application_url"],int(priority),stamp,stamp,app_id))
            number=db.execute("SELECT attempt_number FROM applications WHERE id=?",(app_id,)).fetchone()[0]
        else:
            app_id=str(uuid.uuid4()); db.execute("INSERT INTO applications(id,user_id,resume_id,job_key,company,title,application_url,state,mode,created_at,updated_at,state_changed_at,attempt_number,queue_priority) VALUES(?,?,?,?,?,?,?,'queued','dry_run',?,?,?,1,?)",(app_id,USER,resume["id"],key,str(job.get("company",""))[:200],str(job.get("title",""))[:300],job["application_url"],stamp,stamp,stamp,int(priority)))
            number=1
        db.execute("INSERT OR IGNORE INTO application_opening_reservations(user_id,identity_hash,application_id,identity_json,created_at) VALUES(?,?,?,?,?)",
                   (USER,identity["hash"],app_id,json.dumps(identity["parts"],sort_keys=True),stamp))
        if identity["url"]:
            db.execute("INSERT OR IGNORE INTO application_opening_aliases(user_id,canonical_url,identity_hash,application_id,identity_json,created_at) VALUES(?,?,?,?,?,?)",
                       (USER,identity["url"],identity["hash"],app_id,json.dumps(identity["parts"],sort_keys=True),stamp))
        db.execute("INSERT INTO application_attempts(id,application_id,attempt_number,state,state_changed_at,diagnostic_json,created_at,updated_at) VALUES(?,?,?,'queued',?,'{}',?,?)",
                   (str(uuid.uuid4()),app_id,number,stamp,stamp,stamp))
    return app_id,"queued",True

def _update(app_id,state,diagnostic=None,error=None):
    stamp=now()
    with connect() as db:
        row=db.execute("SELECT state,started_at,attempt_number FROM applications WHERE id=? AND user_id=?",(app_id,USER)).fetchone()
        if not row: return
        attempt=db.execute("SELECT state,started_at FROM application_attempts WHERE application_id=? AND attempt_number=?",(app_id,row["attempt_number"])).fetchone()
        current=attempt["state"] if attempt else row["state"]
        if state!=current and state not in TRANSITIONS.get(current,set()):
            raise ValueError(f"Invalid application transition: {current} -> {state}")
        started=(attempt["started_at"] if attempt else row["started_at"]) or (stamp if state=="opening" else None)
        # The append-only manual outcome ledger is a separate table. Preserve
        # only its latest state marker in transient preparation diagnostics.
        try: previous=json.loads(db.execute("SELECT diagnostic_json FROM applications WHERE id=?",(app_id,)).fetchone()[0] or "{}")
        except (TypeError,json.JSONDecodeError): previous={}
        next_diagnostic=dict(diagnostic or {})
        if previous.get("manual_submission_outcome"):
            next_diagnostic.setdefault("manual_submission_outcome",previous["manual_submission_outcome"])
        heartbeat=stamp if state in {"opening","filling"} else None
        db.execute("UPDATE applications SET state=?,diagnostic_json=?,error=?,updated_at=?,state_changed_at=CASE WHEN state<>? THEN ? ELSE state_changed_at END,started_at=COALESCE(started_at,?),heartbeat_at=? WHERE id=? AND user_id=?",
                   (state,json.dumps(next_diagnostic,ensure_ascii=False)[:30000],(error or "")[:300] or None,stamp,state,stamp,started,heartbeat,app_id,USER))
        db.execute("UPDATE application_attempts SET state=?,diagnostic_json=?,error=?,updated_at=?,state_changed_at=CASE WHEN state<>? THEN ? ELSE state_changed_at END,started_at=COALESCE(started_at,?),heartbeat_at=? WHERE application_id=? AND attempt_number=?",
                   (state,json.dumps(next_diagnostic,ensure_ascii=False)[:30000],(error or "")[:300] or None,stamp,state,stamp,started,heartbeat,app_id,row["attempt_number"]))

def _paused():
    with connect() as db: row=db.execute("SELECT paused FROM application_controls WHERE user_id=?",(USER,)).fetchone()
    return bool(row and row["paused"])

def _label(info): return re.sub(r"[^a-z0-9]+"," "," ".join(str(info.get(k) or "") for k in ("label","name","id","placeholder","aria_label")).casefold()).strip()

def _required_marker(label):
    """Recognize explicit visual required markers used by employer forms."""
    return bool(re.search(r"[*✱＊]\s*$|(?<!\w)[*✱＊](?!\w)|\brequired\b\s*$",str(label or ""),re.I))

def _application_context(job,answers):
    context={"company":job.get("company",""),"title":job.get("title",""),"location":job.get("location",""),
             "work_arrangement":job.get("work_arrangement",""),"source":""}
    preferences={}
    with connect() as db:
        try:
            row=db.execute("SELECT discovery_source,search_prompt FROM discovered_openings WHERE user_id=? AND job_key=?",(USER,job.get("job_key"))).fetchone()
            if row:
                context["source"]=row["discovery_source"] or ""
                context["search_prompt"]=row["search_prompt"] or ""
        except sqlite3.OperationalError: pass
        try:
            row=db.execute("SELECT preferences_json FROM career_preferences WHERE user_id=?",(USER,)).fetchone()
            if row: preferences=json.loads(row["preferences_json"] or "{}")
        except (sqlite3.OperationalError,TypeError,json.JSONDecodeError): pass
    answers["__job_context"]=context
    answers["__job_key"]=str(job.get("job_key") or "")
    answers["__job_description"]=str(job.get("description") or "")[:12000]
    answers["__preferences"]=preferences
    preferences["_reusable_answers"]=[{"question":v.get("question",""),"answer":v.get("answer",""),"type":v.get("answer_type","factual")} for k,v in answers.items() if k.startswith("custom:") and isinstance(v,dict) and v.get("auto_reuse") and v.get("reuse_scope") in {"matching_jobs","general_preference"} and not v.get("sensitive")]
    answers["__job_source"]=context["source"]
    answers["__is_local_mock"]=_mock_allowed(job.get("application_url",""))
    return answers

def _country_answer(candidate,label,field):
    target=""
    for country in ("United States","USA","US","Canada","United Kingdom","UK","Australia"):
        if country.casefold() in label: target="United States" if country in {"United States","USA","US"} else "United Kingdom" if country in {"United Kingdom","UK"} else country; break
    collection=candidate.get("country_work_authorizations" if field=="work_authorization" else "sponsorship_needs_by_country",[])
    if isinstance(collection,list):
        matches=[]
        for entry in collection:
            if not isinstance(entry,dict): continue
            country=str(entry.get("country") or "").casefold()
            aliases={"us":"united states","usa":"united states","uk":"united kingdom"}
            normalized=aliases.get(country,country)
            if target and target.casefold() not in {country,normalized}: continue
            value=entry.get("status") if field=="work_authorization" else entry.get("requires_sponsorship")
            if value: matches.append(str(value))
        if len(matches)==1: return matches[0]
        if len(matches)>1: return None
    legacy=candidate.get(field)
    if legacy and (not target or target.casefold() in str(legacy).casefold()): return str(legacy)
    return None

def _confirmed_location(candidate):
    """Compose residence text only from individually confirmed profile facts."""
    return ", ".join(str(candidate.get(key)).strip() for key in ("city","state_province","country")
                      if candidate.get(key) and str(candidate.get(key)).strip())

_QUALITATIVE_EXPECTATION=re.compile(
    r"\b(?:customer empathy|product thinking|product builder|founder[- ]level ownership|high ownership|ownership mindset|low ego|"
    r"self[ -]motivated|curious|curiosity|excellent communicator|communication skills?|collaborat(?:ion|e)|"
    r"teamwork|team player|interpersonal skills?|fast[ -]paced|passion(?:ate)?|desire to|excited to|"
    r"culture fit|work ethic|ability to thrive)\b",re.I)
_OBJECTIVE_QUALIFICATION=re.compile(
    r"\b(?:minimum of|at least|\d{1,2}(?:\s*[-–]\s*\d{1,2})?\+?\s+years?|"
    r"degree|bachelor|master|ph\.?d|gpa|graduat(?:e|ion)|available to start|work authorization|"
    r"authorized to work|visa|sponsorship|citizenship|security clearance|certification|license|"
    r"intern(?:ship)?(?:/co[ -]?op)? experience|co[ -]?op experience|"
    r"multiple release cycles|two release cycles|experience (?:with|in|building|developing|delivering|shipping)|"
    r"proficien(?:t|cy)|fluency (?:with|in)|fundamentals in|knowledge of|"
    r"on[ -]site|in[ -]office|hybrid schedule|"
    r"(?:must|required|requires?)\b.{0,80}\b(?:python|typescript|javascript|java|react|node(?:\.js)?|"
    r"sql|database|cloud|full[ -]?stack|front[ -]?end|back[ -]?end|data structures?|algorithms?)\b)\b",re.I)

def _is_qualitative_employer_expectation(requirement):
    """Treat broad behavioral/product expectations as review context, not hard prerequisites."""
    text=str(requirement or "").strip()
    text=text.translate(str.maketrans({"\u2010":"-","\u2011":"-","\u2012":"-","\u2013":"-","\u2014":"-"}))
    if not text or _OBJECTIVE_QUALIFICATION.search(text):return False
    return bool(_QUALITATIVE_EXPECTATION.search(text))

def _qualitative_missing_question_keys(match):
    """Map saved missing-information prefixes back to qualitative-only questions."""
    result=set()
    entries=match.get("technical_qualifications",[]) if isinstance(match,dict) else []
    for item in entries if isinstance(entries,list) else []:
        if not isinstance(item,dict) or item.get("requiredness")=="preferred" or not _is_qualitative_employer_expectation(item.get("requirement")):
            continue
        question=str(item.get("missing_question") or "").strip()
        if question:
            result.add(question.casefold())
            result.add(("Technical qualification needs confirmation: "+question).casefold())
    return result

def _unresolved_mandatory(result_json,job_description=""):
    try: result=json.loads(result_json or "{}")
    except (TypeError,json.JSONDecodeError): return True
    for key in ("required_skills","required_experience","education_requirements"):
        entries=result.get(key,[])
        if not isinstance(entries,list): continue
        for entry in entries:
            if key=="required_skills" and isinstance(entry,dict) and _is_qualitative_employer_expectation(entry.get("requirement")):
                continue
            if isinstance(entry,dict) and str(entry.get("status","")).casefold() in {"gap","unclear","confirmed_mismatch","unknown"}:
                return True
    technical=result.get("technical_qualifications",[])
    if isinstance(technical,list) and any(isinstance(item,dict) and item.get("requiredness")=="required"
        and not _is_qualitative_employer_expectation(item.get("requirement"))
        and item.get("status") in {"needs_review","confirmed_mismatch"} for item in technical):
        return True
    missing=result.get("missing_information",[])
    if isinstance(missing,list) and any(re.search(r"work authorization|sponsor|visa|location|graduat|education|professional experience|years of experience",str(item),re.I) for item in missing):
        return True
    availability=result.get("availability_assessment")
    if isinstance(availability,dict) and availability.get("requiredness")=="required" and availability.get("status")!="met":
        return True
    try:
        from job_relevance import extract_experience_requirements
        extracted=extract_experience_requirements(job_description)
        required_years=extracted.get("required",[])+extracted.get("unclear",[])
    except (TypeError,AttributeError):required_years=[]
    for requirement in required_years:
        years=requirement.get("years")
        evidence=[entry for entry in result.get("required_experience",[]) if isinstance(entry,dict)
                  and str(years or "").casefold() in str(entry.get("requirement") or "").casefold()
                  and str(entry.get("status") or "").casefold()=="met"
                  and any(str(item).strip() for item in entry.get("candidate_evidence",[]) if isinstance(item,str))]
        if not evidence:return True
    return False

def _unmapped_required_posting_items(description,result):
    """Return explicit requirement-list items that the saved assessment never evaluated.

    Applicant acknowledgment may resolve ordinary uncertainty, but it must not turn
    an incomplete extraction of mandatory posting qualifications into approval.
    This intentionally blocks review rather than guessing how a requirement maps
    to the candidate profile.
    """
    source=str(description or "")
    if not source.strip():return []
    text=html.unescape(source)
    text=re.sub(r"</(?:li|p|div|ul|ol|h[1-6]|br)\s*>","\n",text,flags=re.I)
    text=re.sub(r"<br\s*/?>","\n",text,flags=re.I)
    text=re.sub(r"<[^>]+>"," ",text)
    text=re.sub(r"\s+(?=(?:requirements|required qualifications|minimum qualifications|basic qualifications|what you(?:'|’)ll bring|nice to have|preferred qualifications|what you(?:'|’)ll do)\s*:)","\n",text,flags=re.I)
    # Preserve date ranges such as "November 2026 - June 2027" while treating
    # surrounding standalone hyphens as flattened bullet separators.
    months=r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    text=re.sub(rf"\b({months}\s+\d{{4}})\s+-\s+({months}\s+\d{{4}})\b",r"\1 – \2",text,flags=re.I)
    text=re.sub(r",\s+(?=ideally\b)","\n",text,flags=re.I)
    text=re.sub(r"\s+[-•]\s+","\n",text)
    text=re.sub(r"\s+(?=we need to hire|we are hiring|candidates? must|applicants? must)\b","\n",text,flags=re.I)
    text=re.sub(r"\s+(?=we are an equal opportunity employer)\b","\n",text,flags=re.I)
    # Some ATS descriptions flatten headings into the preceding bullet. Split
    # known section boundaries even when there is no punctuation before them.
    text=re.sub(r"\s+(?=interesting technical challenges\??\s|what you(?:'|’)ll work on\s)","\n",text,flags=re.I)
    text=re.sub(r"(?<=[.!?])\s+(?=[A-Z])","\n",text)
    lines=[re.sub(r"\s+"," ",line).strip(" \t\r\n•-*") for line in text.splitlines()]
    heading=re.compile(r"^(requirements|required qualifications|minimum qualifications|basic qualifications|what you(?:'|’)ll bring|nice to haves?|preferred qualifications|preferred experience|responsibilities|what you(?:'|’)ll do|interesting technical challenges|what you(?:'|’)ll work on|our values|equal opportunity employer)$",re.I)
    required_sections={"requirements","required qualifications","minimum qualifications","basic qualifications","what you'll bring","what you’ll bring"}
    preferred_sections={"nice to have","nice to haves","preferred qualifications","preferred experience"}
    required_items=[];section=""
    for line in lines:
        clean=re.sub(r"[^a-z'’ ]+"," ",line.casefold()).strip()
        if heading.fullmatch(clean):
            section=clean
            continue
        if section in required_sections and line and len(line)>8 and not re.search(r"\b(?:nice[-\u2011 ]to[-\u2011 ]have|preferred|ideally|preferably|bonus|a plus|no need to have|not required|not a requirement)\b",line,re.I):
            required_items.append(line)
        elif section in preferred_sections or section in {"responsibilities","what you'll do","what you’ll do","our values","equal opportunity employer"}:
            continue
    # Ashby and other ATS feeds often flatten section headings and bullets into
    # plain text. Availability windows are explicit role constraints even when
    # the posting has no formal degree/experience section.
    availability=re.findall(r"(?:we need to hire|we are hiring|candidates? must|applicants? must)[^.\n]*\bavailable to start\b[^.\n]*\.?",text,re.I)
    required_items.extend(re.sub(r"\s+"," ",item).strip() for item in availability)
    if not required_items:return []
    assessed=[]
    for key in ("required_skills","required_experience","education_requirements","technical_qualifications"):
        values=result.get(key,[])
        if isinstance(values,list):
            for item in values:
                if not isinstance(item,dict):continue
                value=str(item.get("requirement") or "").strip()
                # A flattened whole-section assessment cannot prove that each
                # qualification inside it was separately evaluated.
                if len(re.findall(r"\b[\w+#.]+\b",value))>32 or re.search(r"\brequirements?\s*[-:]",value,re.I):
                    continue
                if value:assessed.append(value)
    availability=result.get("availability_assessment")
    if isinstance(availability,dict) and availability.get("requirement"):
        assessed.append(str(availability["requirement"]))
    availability_requirement=str(availability.get("requirement") or "").casefold() if isinstance(availability,dict) else ""
    assessment_tokens=[set(re.findall(r"[a-z0-9+#]+",value.casefold())) for value in assessed]
    location_text=str(result.get("location_assessment") or "").casefold()
    unmapped=[]
    for item in dict.fromkeys(required_items):
        lower=item.casefold()
        if _is_qualitative_employer_expectation(item):continue
        if availability_requirement and "available to start" in lower and "available to start" in availability_requirement:
            continue
        if re.search(r"\b(?:on.site|in[ -]person|work location|office|relocat|in our)\b",lower) and location_text:
            continue
        tokens={word for word in re.findall(r"[a-z0-9+#]+",lower) if len(word)>2 and word not in {"the","and","for","with","from","that","both","you","your","our","are","not","need","expert","about","into","across"}}
        if not tokens or not any(len(tokens & values)/len(tokens)>=0.5 for values in assessment_tokens):
            unmapped.append(item)
    return unmapped[:20]


def _saved_unmapped_required_items(job_key,resume_id):
    """Mandatory posting evidence the current saved match did not assess."""
    with connect() as db:
        row=db.execute("""SELECT o.description,m.result_json FROM discovered_openings o
            LEFT JOIN job_match_results m ON m.user_id=o.user_id AND m.job_key=o.job_key AND m.resume_id=?
            WHERE o.user_id=? AND o.job_key=? ORDER BY m.created_at DESC LIMIT 1""",(resume_id,USER,job_key)).fetchone()
    if not row:return []
    try:result=json.loads(row["result_json"] or "{}") if row["result_json"] else {}
    except (TypeError,json.JSONDecodeError):result={}
    return _unmapped_required_posting_items(row["description"] or "",result)


def _unmapped_requirements_blocker(job_key,resume_id):
    items=_saved_unmapped_required_items(job_key,resume_id)
    if not items:return ""
    return "The saved assessment did not evaluate explicit mandatory posting requirements: "+"; ".join(items[:4])+". Reassess the posting and confirmed candidate evidence before preparation."


def _review_experience_level(job, match):
    try:
        from job_relevance import classify_experience_level
        level,_=classify_experience_level(job.get("title"),job.get("description"),job.get("employment_type"))
    except Exception:level="Ambiguous"
    if level=="Ambiguous":
        for item in match.get("required_experience",[]) if isinstance(match.get("required_experience"),list) else []:
            if not isinstance(item,dict):continue
            found=re.search(r"(\d{1,2})\s*\+?\s+years?",str(item.get("requirement") or ""),re.I)
            if found and int(found.group(1))>=3:return "Mid-Level"
    return level


def _eligibility_review_assessment(job, match, *, stale=False, unmapped=None,
                                   confirmed_projects_available=False):
    """Turn stored match evidence into applicant-facing findings without changing eligibility.

    In particular, model explanations and score never establish a mismatch. The
    assessment is a read-only view over the saved posting, deterministic result
    fields, and a boolean indicating whether confirmed project narratives exist.
    """
    match = match if isinstance(match, dict) else {}
    blockers=[]
    mandatory_met=[]
    mandatory_unresolved=[]
    confirmed_conflicts=[]
    employer_uncertainties=[]
    preferred=[]
    unsupported=[]
    unmapped=list(unmapped or [])
    fields=("required_skills","required_experience","education_requirements","technical_qualifications")
    for field in fields:
        entries=match.get(field,[])
        if not isinstance(entries,list):continue
        for entry in entries:
            if not isinstance(entry,dict) or entry.get("requiredness")=="preferred":continue
            requirement=str(entry.get("requirement") or "Mandatory qualification").strip()
            if _is_qualitative_employer_expectation(requirement):
                employer_uncertainties.append({"category":"qualitative_employer_expectation","requirement":requirement,
                    "explanation":"This broad behavioral or product expectation remains visible for applicant context, but is not an objective eligibility prerequisite by itself."})
                continue
            status=str(entry.get("status") or "unclear").casefold()
            if status=="met":mandatory_met.append(requirement)
            elif status=="confirmed_mismatch":
                confirmed_conflicts.append(requirement)
                blockers.append({"category":"confirmed_mandatory_mismatch","requirement":requirement,
                    "explanation":"Verified candidate evidence conflicts with this mandatory requirement."})
            elif status in {"gap","unclear","needs_review"}:
                mandatory_unresolved.append(requirement)
                blockers.append({"category":"missing_mandatory_applicant_evidence","requirement":requirement,
                    "explanation":str(entry.get("explanation") or "Confirmed applicant evidence is incomplete.")[:500]})
    for entry in match.get("preferred_skills",[])+match.get("preferred_experience",[])+match.get("preferred_education_requirements",[]) if all(isinstance(match.get(k,[]),list) for k in ("preferred_skills","preferred_experience","preferred_education_requirements")) else []:
        if isinstance(entry,dict):preferred.append({"requirement":str(entry.get("requirement") or "Preferred qualification"),
            "status":str(entry.get("status") or "preferred")})
    for value in unmapped:
        blockers.append({"category":"qualification_extraction_incomplete","requirement":str(value)[:500],
            "explanation":"The saved result did not evaluate this explicit requirement-list item."})
    missing=match.get("missing_information",[])
    qualitative_questions=_qualitative_missing_question_keys(match)
    if isinstance(missing,list):
        for value in missing:
            text=str(value or "").strip()
            if not text or text.casefold() in qualitative_questions:continue
            if _is_qualitative_employer_expectation(text):continue
            country_auth=re.search(r"Work authorization for (.+?) is not confirmed",text,re.I)
            if country_auth:
                requirement=f"Work authorization or employer sponsorship for {country_auth.group(1).strip()}"
                mandatory_unresolved.append(requirement)
                blockers.append({"category":"missing_mandatory_applicant_evidence","requirement":requirement,
                    "explanation":"Relocation willingness does not establish legal authorization to work in this country. Confirm the country-specific fact; employer sponsorship support may also need verification."})
            elif re.search(r"experience level is not clear|experience level is unclear",text,re.I):
                # Prefer explicit title/description evidence over stale AI prose.
                level=_review_experience_level(job,match)
                if level=="Ambiguous":
                    employer_uncertainties.append({"category":"employer_metadata_uncertainty",
                        "requirement":"Role seniority is not explicit in the posting.","explanation":text[:500]})
            elif not any(item.get("requirement") in text for item in blockers):
                blockers.append({"category":"missing_applicant_information","requirement":text[:500],
                    "explanation":"This saved assessment requests applicant evidence; it has not been supplied or confirmed."})
    diagnostics=match.get("ai_evidence_diagnostics",[])
    if isinstance(diagnostics,list):
        for value in diagnostics:
            text=str(value or "").strip()
            if text:unsupported.append({"category":"unsupported_ai_evidence","explanation":text[:500]})
    explanation=str(match.get("experience_level_assessment") or "")
    if re.search(r"experience level.{0,60}(?:unclear|not clear)",explanation,re.I):
        level=_review_experience_level(job,match)
        if level!="Ambiguous":
            unsupported.append({"category":"unsupported_ai_evidence",
                "explanation":f"The saved AI assessment calls role seniority unclear, but deterministic posting evidence classifies it as {level}. This correction is diagnostic only and does not change eligibility."})
    if confirmed_projects_available and re.search(r"profile does not mention.*(?:project|coursework)|no relevant.*(?:project|coursework)",explanation,re.I):
        unsupported.append({"category":"unsupported_ai_evidence",
            "explanation":"The saved AI explanation says relevant projects or coursework are absent, but the verified profile contains confirmed project evidence. This contradiction is diagnostic only."})
    if confirmed_projects_available and re.search(r"(?:does not have|has no|lacks?).{0,100}(?:programming languages|language experience)",explanation,re.I):
        unsupported.append({"category":"unsupported_ai_evidence",
            "explanation":"The saved AI explanation makes a categorical denial of programming-language experience despite confirmed project records. This narrative is not qualification evidence; it does not establish Haskell proficiency or professional experience."})
    if re.search(r"candidate has 3\+ years|candidate has at least 3 years",explanation,re.I):
        experience_items=match.get("required_experience",[])
        has_support=isinstance(experience_items,list) and any(
            isinstance(item,dict) and item.get("status")=="met" and item.get("candidate_evidence")
            for item in experience_items)
        if not has_support:
            unsupported.append({"category":"unsupported_ai_evidence",
                "explanation":"The AI explanation claims 3+ years, but the saved result contains no verified experience evidence supporting that claim."})
    if not str(job.get("employment_type") or "").strip():
        employer_uncertainties.append({"category":"employer_metadata_uncertainty","requirement":"Employment type is not stated in saved posting metadata.",
            "explanation":"Unknown employment type remains unknown and is not treated as applicant ineligibility."})
    if not str(job.get("work_arrangement") or "").strip() and not re.search(r"\b(remote|hybrid|on[- ]site|in person|in office)\b",str(job.get("description") or ""),re.I):
        employer_uncertainties.append({"category":"employer_metadata_uncertainty","requirement":"Work arrangement is not stated in saved posting metadata.",
            "explanation":"Remote, hybrid, or on-site requirements cannot be inferred from missing metadata."})
    if stale:
        blockers.insert(0,{"category":"stale_posting_or_matching_result","requirement":"Saved posting or match is stale.",
            "explanation":"Reassess saved evidence before relying on this classification."})
    if str(match.get("classification") or "") == "needs_review" and not blockers and not employer_uncertainties:
        blockers.append({"category":"technical_extraction_or_normalization_defect",
            "requirement":"The stored result says needs review but lists no unresolved mandatory evidence.",
            "explanation":"Keep this result in review until its classification is reconciled deterministically; no applicant decision was inferred."})
    if confirmed_conflicts:
        next_action="This confirmed mandatory conflict blocks preparation. Applicant review cannot override it."
    elif mandatory_unresolved:
        unresolved_technical=any(isinstance(item,dict) and item.get("requiredness")!="preferred" and
            not _is_qualitative_employer_expectation(item.get("requirement")) and
            item.get("status") in {"gap","unclear","needs_review"} for item in match.get("technical_qualifications",[]))
        availability=match.get("availability_assessment") if isinstance(match.get("availability_assessment"),dict) else {}
        if availability.get("status")=="needs_review":
            next_action="Confirm your earliest full-time start date or range in Candidate Profile, then review the mandatory technical evidence questions in Applications → Technical Qualifications. Graduation date and notice period are separate facts."
        elif unresolved_technical:
            next_action="Review the specific mandatory evidence questions in Applications → Technical Qualifications. Confirm only examples that are accurate; a yes/no acknowledgment alone will not establish them."
        else:
            next_action="Confirm or correct the missing mandatory evidence in Candidate Profile, including dated employment, education, or other structured facts. A yes/no acknowledgment alone will not satisfy a technical or experience requirement."
    elif unmapped:
        next_action="Review the posting extraction before eligibility can be resolved."
    elif employer_uncertainties:
        next_action="Review the employer-side uncertainty; no applicant fact or eligibility approval has been assumed."
    elif blockers:
        next_action="Resolve the listed evidence or classification issue before considering preparation."
    elif str(match.get("classification") or "") == "potential_match":
        next_action="Mandatory evidence is supported. Job-specific preparation authorization remains separate."
    else:
        next_action="No mandatory evidence gap is recorded, but the saved needs-review classification has not been converted to a potential match."
    preparation_blocked=bool(stale or blockers or mandatory_unresolved or confirmed_conflicts or unmapped or
        str(match.get("classification") or "not_analyzed")!="potential_match")
    return {"classification":str(match.get("classification") or "not_analyzed"),
        "mandatory_met":list(dict.fromkeys(mandatory_met)),"mandatory_unresolved":list(dict.fromkeys(mandatory_unresolved)),
        "confirmed_conflicts":list(dict.fromkeys(confirmed_conflicts)),"blockers":blockers[:30],
        "employer_uncertainties":employer_uncertainties[:20],"preferred_qualifications":preferred[:30],
        "unsupported_ai_evidence":unsupported[:20],"next_action":next_action,
        "preparation_blocked":preparation_blocked,
        "preparation_block_reason":next_action if preparation_blocked else "Eligibility evidence is resolved; current job-specific authorization and form checks are still required.",
        "eligibility_resolved":bool(not stale and not blockers and not mandatory_unresolved and not confirmed_conflicts and
            not unmapped and str(match.get("classification") or "")=="potential_match")}


def _experience_requirement_question(requirement, description):
    """Create a fact-specific prompt from the saved posting, never from model prose."""
    years=re.search(r"\b\d{1,2}(?:\s*[-–]\s*\d{1,2})?\+?\s+years?\b",str(requirement or ""),re.I)
    number=re.search(r"\d{1,2}(?:\s*[-–]\s*\d{1,2})?",years.group(0)).group(0) if years else ""
    detail=""
    source=re.sub(r"\s+"," ",str(description or ""))
    posted=re.search(r"\b"+re.escape(number)+r"\s*\+?\s+years?\s+of\s+experience[^.!?\-]{0,100}",source,re.I) if number else None
    if posted:detail=posted.group(0).strip()
    if not detail:detail=f"{years.group(0) if years else 'the required number of years'} of relevant professional experience"
    return (f"Do you meet this mandatory requirement: {detail}? If yes, confirm the employer, role, and dates in Candidate Profile. "
            "Projects and coursework do not count as industry employment years.")

def _eligibility_gate(db,job_key,resume_id):
    match=db.execute("SELECT classification,result_json,updated_at,freshness_status,matching_model_version FROM job_match_results WHERE user_id=? AND job_key=? AND resume_id=? ORDER BY created_at DESC LIMIT 1",(USER,job_key,resume_id)).fetchone()
    if not match: return "not_analyzed",False,True
    if match["freshness_status"]!="current": return "not_analyzed",False,True
    model_version=match["matching_model_version"] or ""
    try:
        from resume_job_matcher import matching_engine_version
        expected_version=matching_engine_version()
    except Exception:
        expected_version=""
    if not expected_version or model_version!=expected_version:return "not_analyzed",False,True
    profile=db.execute("SELECT updated_at,values_json,confirmed_json FROM candidate_verifications WHERE user_id=? AND resume_id=?",(USER,resume_id)).fetchone()
    if not profile: return "not_analyzed",False,True
    try:
        if not verified_profile_facts(verified_values_from_json(profile["values_json"]),json.loads(profile["confirmed_json"] or "{}")):
            return "not_analyzed",False,True
    except (TypeError,json.JSONDecodeError): return "not_analyzed",False,True
    if profile:
        try:
            analyzed=datetime.fromisoformat(str(match["updated_at"]).replace("Z","+00:00"))
            edited=datetime.fromisoformat(str(profile["updated_at"]).replace("Z","+00:00"))
            if analyzed.tzinfo is None: analyzed=analyzed.replace(tzinfo=timezone.utc)
            if edited.tzinfo is None: edited=edited.replace(tzinfo=timezone.utc)
            if analyzed < edited: return "not_analyzed",False,True
        except (TypeError,ValueError): return "not_analyzed",False,True
    classification=match["classification"] or "not_analyzed"
    evidence_review=db.execute("""SELECT * FROM eligibility_evidence_reviews WHERE user_id=? AND job_key=? AND resume_id=?
        ORDER BY review_id DESC LIMIT 1""",(USER,job_key,resume_id)).fetchone()
    if evidence_review:
        try:
            import resume_job_matcher as matcher
            current_resume,current_status,current_profile=matcher.active_candidate()
            current_preferences=matcher.preferences() or {}
            opening_row=db.execute("SELECT * FROM discovered_openings WHERE user_id=? AND job_key=?",(USER,job_key)).fetchone()
            review_is_current=(current_resume==resume_id and opening_row is not None and
                evidence_review["match_updated_at"]==str(match["updated_at"] or "") and
                evidence_review["matching_engine_version"]==model_version==matcher.matching_engine_version() and
                evidence_review["profile_fingerprint"]==matcher.profile_fingerprint(current_profile,resume_id) and
                evidence_review["preferences_fingerprint"]==matcher.stable_fingerprint(current_preferences) and
                evidence_review["job_fingerprint"]==matcher.job_fingerprint(dict(opening_row)))
        except (HTTPException,sqlite3.OperationalError,TypeError,ValueError):
            review_is_current=False
        if not review_is_current:
            return "not_analyzed",False,True
    opening=db.execute("SELECT description FROM discovered_openings WHERE user_id=? AND job_key=?",(USER,job_key)).fetchone()
    description=opening["description"] if opening and "description" in opening.keys() else ""
    decision=db.execute("SELECT decision FROM application_eligibility_reviews WHERE user_id=? AND job_key=? AND resume_id=? AND classification=? AND match_updated_at=?",(USER,job_key,resume_id,classification,str(match["updated_at"] or ""))).fetchone()
    if decision and decision["decision"] in {"rejected","deferred"}:return decision["decision"],False,True
    reviewed=bool(decision and decision["decision"]=="approved")
    return classification,reviewed,_unresolved_mandatory(match["result_json"],description)

def _question_category(text):
    value=str(text or "").casefold()
    if re.search(r"captcha|human verification|verification code|login|sign in",value): return "Human verification"
    if re.search(r"human confirmation required|sensitive or legal question",value): return "Legal or sensitive questions"
    if _requires_human_confirmation({"label":value}) or _sensitive({"label":value}): return "Legal or sensitive questions"
    if re.search(r"country|state|province|city|location|postal|zip|address",value): return "Location"
    if re.search(r"education|university|college|degree|graduat|gpa",value): return "Education"
    if re.search(r"experience|employer|employment|internship|work history",value): return "Work experience"
    if re.search(r"name|email|phone|linkedin|github|portfolio",value): return "Personal information"
    if re.search(r"required answer|choice matches|no matching choice|unknown question|custom question",value): return "Unsupported questions"
    return "Job-specific questions"

def _display_question(text):
    value=re.sub(r"\s+"," ",str(text or "")).strip()
    prefixes=(r"Required answer is missing for:\s*",r"No confirmed choice matches:\s*",r"No matching choice for:\s*",
        r"A confirmed choice is needed for:\s*",r"Human confirmation of explicit verified information is required for:\s*",
        r"AI draft needs your review for:\s*",r"AI draft unavailable for\s*",r"The confirmed answer is longer than this field allows \(\d+ characters\):\s*")
    for prefix in prefixes:
        value=re.sub(r"^"+prefix,"",value,flags=re.I)
    return value[:300]

def _group_questions(values):
    groups={}
    seen=set()
    for raw in values if isinstance(values,list) else []:
        raw_text=re.sub(r"\s+"," ",str(raw or "")).strip()[:500]
        text=_display_question(raw_text)
        norm=re.sub(r"[^a-z0-9]+"," ",text.casefold()).strip()
        if not text or norm in seen: continue
        seen.add(norm)
        category=_question_category(text)
        groups.setdefault(category,[]).append({"question":text,"diagnostic_question":raw_text,"category":category,"sensitive":category=="Legal or sensitive questions"})
    order=("Personal information","Location","Education","Work experience","Job-specific questions","Legal or sensitive questions","Human verification","Unsupported questions")
    return [{"category":key,"questions":groups[key]} for key in order if key in groups]

def _option_normal(value):
    return re.sub(r"[^a-z0-9]+"," ",str(value).casefold()).strip()

def _option_alias(value,field_label):
    normalized=_option_normal(value)
    if re.search(r"country",field_label):
        countries={"us":"united states","usa":"united states","u s a":"united states","uk":"united kingdom","u k":"united kingdom","ca":"canada","au":"australia"}
        return countries.get(normalized,normalized)
    if re.search(r"state|province|region",field_label):
        states={"al":"alabama","ak":"alaska","az":"arizona","ar":"arkansas","ca":"california","co":"colorado","ct":"connecticut","de":"delaware","fl":"florida","ga":"georgia","hi":"hawaii","id":"idaho","il":"illinois","in":"indiana","ia":"iowa","ks":"kansas","ky":"kentucky","la":"louisiana","me":"maine","md":"maryland","ma":"massachusetts","mi":"michigan","mn":"minnesota","ms":"mississippi","mo":"missouri","mt":"montana","ne":"nebraska","nv":"nevada","nh":"new hampshire","nj":"new jersey","nm":"new mexico","ny":"new york","nc":"north carolina","nd":"north dakota","oh":"ohio","ok":"oklahoma","or":"oregon","pa":"pennsylvania","ri":"rhode island","sc":"south carolina","sd":"south dakota","tn":"tennessee","tx":"texas","ut":"utah","vt":"vermont","va":"virginia","wa":"washington","wv":"west virginia","wi":"wisconsin","wy":"wyoming","dc":"district of columbia"}
        return states.get(normalized,normalized)
    return normalized

def _answer(info,candidate,answers):
    label=_label(info)
    category,question_context,question_sensitive=canonical_answer_identity(label)
    if category in {"preferred_work_location","job_conditions"}:
        return None,None
    if category=="relocation":
        preferences=answers.get("__preferences",{})
        context=answers.get("__job_context",{})
        if question_context and normalize_text(question_context) not in normalize_text(context.get("location","")):
            return None,None
        for saved_key,saved in answers.items():
            if (saved_key.startswith("custom:") and saved.get("confirmed") and saved.get("auto_reuse") and
                not saved.get("sensitive") and saved.get("reuse_scope")=="job_specific" and
                saved.get("scope_job_key")==answers.get("__job_key") and saved.get("category")=="relocation" and
                saved.get("context","")==question_context and saved.get("answer")):
                try:return validate_application_answer(label,saved["answer"]),saved_key
                except ValueError:continue
        mode=str(preferences.get("relocation_preference") or "unspecified").casefold()
        if mode=="anywhere":return "Yes","relocation_preference"
        if mode in {"selected_locations","no"}:
            decision=evaluate_relocation_preference({"location":context.get("location",""),
                "work_arrangement":context.get("work_arrangement","")},preferences)
            if decision and decision[0]=="eligible":return ("No" if mode=="no" else "Yes"),"relocation_preference"
        return None,None
    if re.search(r"city.*state.*country|city state and country|city.{0,35}state|state.{0,35}country|where do you live|residential location",label):
        location=_confirmed_location(candidate)
        if location: return location,"location"
    if re.search(r"how did you hear|where did you (?:hear|find)|source of (?:application|job)|how did you find",label):
        source=answers.get("__job_source")
        if source: return str(source),"discovery_source"
    if re.search(r"authorization|authorisation|authorized to work|authorised to work",label):
        value=_country_answer(candidate,label,"work_authorization")
        if value: return value,"work_authorization"
    if re.search(r"sponsorship|sponsor|visa",label):
        value=_country_answer(candidate,label,"requires_sponsorship")
        if value: return value,"requires_sponsorship"
    if re.search(r"preferred name|name you go by",label):
        value=candidate.get("preferred_name")
        if value:return str(value),"preferred_name"
        saved=answers.get("preferred_name")
        if isinstance(saved,dict) and saved.get("answer") and saved.get("auto_reuse"):return saved["answer"],"preferred_name"
        return None,None
    direct=((r"postal|zip code|postcode","postal_code"),(r"phone country code|country calling code","phone_country_code"),
      (r"address line 2|apartment|suite number","address_line2"),(r"street address|address line 1|mailing address|residential address|home address","address_line1"),
      (r"country|country of residence|country where you are located","country"),(r"state|province|region","state_province"),
      (r"\bcity\b|\btown\b","city"),
      (r"email|e mail","email"),(r"phone|mobile|telephone","phone"),(r"full name|first and last name|your name|name","full_name"),
      (r"github","github"),(r"linkedin","linkedin"),(r"portfolio","portfolio"),(r"personal website|website|web site","website"),(r"current location|city and state|where are you located|location","location"),
      (r"education history|education details|university|school|college","university"),(r"degree","degree"),(r"major|field of study","major"),(r"graduation month.{0,24}year|graduation date","graduation_date"),(r"graduation month","graduation_month"),(r"graduation year|expected graduation","graduation_year"),
      (r"authorization|authorisation|authorized to work|authorised to work","work_authorization"),(r"sponsorship|sponsor|visa","requires_sponsorship"),(r"salary|compensation expectation","salary_expectations"),(r"salary currency|currency","salary_currency"),(r"earliest available|earliest start|available to start","earliest_available_date"),(r"availability|start date|notice period","availability"),(r"travel|willingness to travel","willingness_to_travel"),
      (r"work experience|employment history|professional experience","work_experience"),(r"internship","internships"),(r"technical skills|programming languages","technical_skills"),(r"project(s)? description|projects","projects"),(r"certification","certifications"),(r"license|licence","licenses"),(r"employment preference|full time or part time","employment_preferences"),(r"additional information|anything else","additional_information"))
    for pattern,key in direct:
        if re.search(pattern,label):
            val=_country_answer(candidate,label,key) if key in {"work_authorization","requires_sponsorship"} else candidate.get(key)
            if key=="phone" and candidate.get("phone_country_code") and val:
                val=str(candidate["phone_country_code"])+" "+str(val)
            if key=="location": val=_confirmed_location(candidate) or candidate.get("location")
            if key=="earliest_available_date" and isinstance(val,str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}",val):
                pass
            if key=="university" and candidate.get("education"):
                val=candidate["education"]
            if key=="graduation_date" and isinstance(val,str) and re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])",val):
                val=datetime.strptime(val,"%Y-%m").strftime("%B %Y")
            if isinstance(val,list): val="\n".join(_format_profile_item(x) for x in val)
            if val: return str(val),key
            saved=answers.get(key)
            if isinstance(saved,dict) and saved.get("answer") and saved.get("auto_reuse"):
                saved_question=saved.get("question")
                if saved_question and canonical_answer_identity(saved_question)[:2]!=(category,question_context):continue
                try:return validate_application_answer(label,saved["answer"]),key
                except ValueError:continue
    # Reuse requires an exact deterministic category and context match.
    for key,item in answers.items():
        if (not key.startswith("custom:") or item.get("sensitive") or not item.get("confirmed") or
            not item.get("auto_reuse") or _sensitive(info) or _requires_human_confirmation(info)): continue
        item_category,item_context,_=canonical_answer_identity(item.get("question",""))
        scope=item.get("reuse_scope") or ("matching_jobs" if item.get("auto_reuse") else "none")
        scope_ok=(scope in {"matching_jobs","general_preference"} or (scope=="job_specific" and item.get("scope_job_key")==answers.get("__job_key")))
        if not scope_ok or item_category!=category or item_context!=question_context or question_sensitive:continue
        if item.get("answer"):
            try:return validate_application_answer(label,item["answer"]),key
            except ValueError:continue
    if info.get("type")=="file":
        if re.search(r"resume|curriculum vitae|cv upload",label):return "__RESUME__","resume"
        doc_type=("transcript" if re.search(r"transcript",label) else
                  "cover_letter" if re.search(r"cover letter",label) else
                  "certificate" if re.search(r"certificate|certification",label) else
                  "portfolio" if re.search(r"portfolio|work sample",label) else None)
        choices=[item for item in candidate.get("__application_documents",[]) if item.get("document_type")==doc_type] if doc_type else []
        if len(choices)==1:return "__DOCUMENT__:"+choices[0]["path"],"document"
    return None,None


def _format_profile_item(item):
    if isinstance(item,str): return item
    if not isinstance(item,dict): return str(item)
    if item.get("institution") or item.get("degree"):
        title=" — ".join(str(x) for x in (item.get("degree"),item.get("major")) if x)
        pieces=[" at ".join(x for x in (str(title),str(item.get("institution") or "")) if x)]
        if item.get("graduation_date"):
            try: pieces.append("Graduation: "+datetime.strptime(item["graduation_date"],"%Y-%m").strftime("%B %Y"))
            except (ValueError,TypeError): pass
        if item.get("gpa"): pieces.append("GPA: "+str(item["gpa"]))
    elif item.get("job_title") or item.get("employer"):
        pieces=[" at ".join(x for x in (str(item.get("job_title") or ""),str(item.get("employer") or "")) if x)]
        if item.get("location"): pieces.append("Location: "+str(item["location"]))
        if item.get("employment_type"): pieces.append("Employment type: "+str(item["employment_type"]))
        period=" – ".join(str(x) for x in (item.get("start_date"),"Present" if item.get("currently_working") else item.get("end_date")) if x)
        if period: pieces.append("Dates: "+period)
    else:
        pieces=[str(item.get("name") or "Project")]
    details=item.get("responsibilities") or item.get("description") or []
    tech=item.get("technologies") or []
    if isinstance(details,list): pieces.extend(str(x) for x in details)
    elif details: pieces.append(str(details))
    if isinstance(tech,list) and tech: pieces.append("Technologies: "+", ".join(str(x) for x in tech))
    for label,key in (("GitHub","github_url"),("Demo","demo_url")):
        if item.get(key): pieces.append(label+": "+str(item[key]))
    return "\n".join(x for x in pieces if x)

def _sensitive(info): return bool(SENSITIVE_PROMPT.search(_label(info)) or re.search(r"work authorization|work authorisation|sponsor|visa|legally|attest|certify|criminal|background|disability|veteran|gender|race|ethnic|captcha|verification code",_label(info)))

def _requires_human_confirmation(info):
    return bool(re.search(r"captcha|human verification|verification code|\backnowledge\b|\bconfirm(?:ation)?\b|attest|certify|legal declaration|terms and conditions|gender|race|ethnic|disability|veteran|criminal background|did you use (?:ai|automation)|have you used (?:ai|automation)|do not use automation|no automation",_label(info)))

def _ordinary_open_question(info):
    label=_label(info)
    return (info.get("tag")=="textarea" or info.get("type") in {"text","textarea"}) and bool(ORDINARY_PROMPT.search(label)) and not SENSITIVE_PROMPT.search(label)

def _fill_page(page,candidate,answers,resume_path,on_upload=None):
    fields=page.locator("input:not([type=hidden]),textarea,select,[role=textbox],[role=checkbox],[role=radio],[contenteditable=true]"); report=[]; missing=[]; drafts=[]; processed_radios=set()
    try: semantic_fields=[item for item in _visible_form_metadata(page) if isinstance(item,dict) and not item.get("__inspection_meta__")]
    except Exception: semantic_fields=[]
    for i in range(min(fields.count(),150)):
        field=fields.nth(i); info={}
        try:
            try: file_input=field.get_attribute("type")=="file"
            except Exception: file_input=False
            # Many employer pages visually hide their native file input behind
            # an accessible upload button. Playwright can safely target that
            # exact native input with set_input_files after job-specific upload
            # authorization; no button click or page interaction is needed.
            if (not field.is_visible() and not file_input) or not field.is_enabled(): continue
            info=field.evaluate("""el=>{let labels=[...(el.labels||[])].map(x=>x.innerText);let labelledBy=(el.getAttribute('aria-labelledby')||'').split(/\\s+/).map(id=>document.getElementById(id)?.innerText||'').join(' ');let role=el.getAttribute('role')||'';let explicit=labels.join(' ')||el.getAttribute('aria-label')||labelledBy||el.closest('label')?.innerText||el.closest('fieldset')?.querySelector('legend')?.innerText||'';return {tag:el.tagName.toLowerCase(),type:(el.type||(role==='textbox'||el.isContentEditable?'text':role==='checkbox'?'checkbox':role==='radio'?'radio':'')).toLowerCase(),input_type:(el.getAttribute('type')||'').toLowerCase(),role,name:el.name||el.getAttribute('name')||'',id:el.id||'',value:el.value||el.getAttribute('value')||el.innerText||'',placeholder:el.placeholder||el.getAttribute('placeholder')||'',aria_label:el.getAttribute('aria-label')||'',accept:el.getAttribute('accept')||'',label:(explicit||el.placeholder||el.name||el.id||'').slice(0,240),has_associated_label:!!explicit,required:el.required||el.getAttribute('aria-required')==='true'||el.getAttribute('aria-required')==='1',max_length:el.maxLength>0?el.maxLength:null,disabled:el.disabled,hidden:el.type==='hidden'||el.getAttribute('aria-hidden')==='true',contenteditable:!!el.isContentEditable}}""")
            info["required"]=bool(info.get("required") or _required_marker(info.get("label")))
            semantic=next((item for item in semantic_fields if
                (info.get("id") and item.get("id")==info.get("id")) or
                (info.get("name") and item.get("name")==info.get("name"))),None)
            if semantic:
                info["label"]=semantic.get("question") or info.get("label")
                info["required"]=bool(semantic.get("required") or _required_marker(info.get("label")))
                info["role"]=semantic.get("role") or info.get("role")
                info["accept"]=semantic.get("accept") or info.get("accept")
            if info["type"] in {"submit","button","hidden"} or info.get("hidden"): continue
            if info.get("role")=="combobox":
                # Handled below by the exact-option custom combobox path. Do
                # not type arbitrary text into a selector's backing input.
                continue
            closed_choice=bool(re.match(r"^(are you|do you|did you|will you|would you|what archetype|select |choose )",str(info.get("label") or ""),re.I))
            no_stable_label=info.get("tag")=="input" and not info.get("id") and not info.get("name") and not info.get("role") and not info.get("has_associated_label")
            if no_stable_label or (closed_choice and info.get("type")=="text" and not info.get("role")):
                if info["required"]:missing.append("A required control could not be identified safely; review the employer form manually.")
                report.append({"field":info.get("label") or "Unresolved form control","result":"unresolved_control","required":bool(info["required"])});continue
            matching_answer_key=None
            value,source=_answer(info,candidate,answers)
            settings=answers.get("__settings",{})
            local_mock=bool(answers.get("__is_local_mock"))
            scope=answers.get("__preparation_authorization") or {}
            if not local_mock and source=="resume" and "resume" not in (scope.get("document_types") or []):
                value,source=None,None
            if not local_mock and isinstance(source,str) and source.startswith("custom:") and source not in (scope.get("field_keys") or []):
                value,source=None,None
            if source in candidate and not settings.get("allow_confirmed_profile_fields",False) and not local_mock:
                value,source=None,None
            if isinstance(source,str) and source.startswith("custom:") and not settings.get("allow_auto_use_confirmed_answers",False) and not local_mock:
                value,source=None,None
            if source=="document" and not settings.get("allow_auto_use_confirmed_answers",False) and not local_mock:
                value,source=None,None
            if isinstance(source,str) and source.startswith("custom:"):
                matching_answer_key=source
            if _requires_human_confirmation(info):
                if info["required"]: missing.append("Human confirmation required for a sensitive or legal question.")
                report.append({"field":info.get("label") or info.get("name") or "Question","result":"requires_user_confirmation"}); continue
            if _sensitive(info) and (value is None or (not settings.get("allow_auto_use_confirmed_answers",False) and not local_mock)):
                if info["required"]: missing.append("Human confirmation of explicit verified information is required for: "+(info.get("label") or "a sensitive question"))
                report.append({"field":info.get("label") or info.get("name") or "Question","result":"requires_user_confirmation"}); continue
            if matching_answer_key and answers[matching_answer_key].get("sensitive"):
                if info["required"]: missing.append("Sensitive answers require your explicit entry at this employer.")
                report.append({"field":info.get("label") or "Question","result":"requires_user_confirmation"}); continue
            if value is not None and info.get("max_length") and len(str(value))>int(info["max_length"]):
                missing.append("The confirmed answer is longer than this field allows ("+str(info["max_length"])+" characters): "+(info.get("label") or "required question"))
                report.append({"field":info.get("label") or "Question","result":"field_limit_exceeded"}); continue
            if info["type"]=="file" and value=="__RESUME__":
                accept=str(info.get("accept") or "").casefold()
                if accept and not any(token in accept for token in (".pdf","application/pdf","*/*")):
                    missing.append("The active resume format is not accepted by the upload field.")
                    report.append({"field":"Resume upload","result":"unsupported_file_type"}); continue
                if on_upload: on_upload()
                field.set_input_files(str(resume_path))
                uploaded=field.evaluate("el=>({count:el.files?.length||0,name:el.files?.[0]?.name||''})")
                if not uploaded.get("count"):
                    missing.append("The employer did not accept the active resume upload.")
                    report.append({"field":"Resume upload","result":"upload_unverified"}); continue
                report.append({"field":"Resume upload","result":"filled","source":"active_resume","required":bool(info.get("required"))}); continue
            if info["type"]=="file" and isinstance(value,str) and value.startswith("__DOCUMENT__:"):
                path=Path(value.split(":",1)[1]).resolve();private_docs=(Path(__file__).resolve().parent/"private_documents").resolve()
                accept=str(info.get("accept") or "").casefold()
                if path.parent!=private_docs or not path.is_file():
                    missing.append("The selected private application document is unavailable.");continue
                if accept and not any(token in accept for token in (path.suffix.casefold(),"*/*")):
                    missing.append("The saved application document format is not accepted by this field.");continue
                if on_upload:on_upload()
                field.set_input_files(str(path))
                uploaded=field.evaluate("el=>({count:el.files?.length||0,name:el.files?.[0]?.name||''})")
                if not uploaded.get("count"):
                    missing.append("The employer did not accept the required private document upload.")
                    report.append({"field":info.get("label") or "Application document","result":"upload_unverified"});continue
                report.append({"field":info.get("label") or "Application document","result":"filled","source":"private_document","required":bool(info.get("required"))});continue
            if info["type"]=="radio":
                group=info.get("name") or info.get("id") or info.get("label")
                if group in processed_radios: continue
                processed_radios.add(group)
                if value is None:
                    if info["required"]: missing.append("Required answer is missing for: "+(info.get("label") or info.get("name") or "an application question"))
                    continue
                desired=str(value).strip().casefold(); chosen=None
                for option_index in range(min(fields.count(),150)):
                    option=fields.nth(option_index)
                    try:
                        option_info=option.evaluate("""el=>({type:(el.type||el.getAttribute('role')||'').toLowerCase(),name:el.name||el.getAttribute('name')||'',value:el.value||el.getAttribute('value')||'',label:((el.labels||[]).map(x=>x.innerText).join(' ')||el.getAttribute('aria-label')||el.closest('label')?.innerText||'').trim()})""")
                        if option_info["type"]!="radio" or option_info["name"]!=info.get("name"): continue
                        label=option_info["label"].casefold().strip()
                        if desired==str(option_info["value"]).casefold() or desired==label or (len(desired)>2 and desired in label): chosen=option; break
                    except Exception: continue
                if chosen:
                    chosen.check()
                    if not chosen.is_checked():
                        missing.append("The selected answer could not be verified for: "+(info.get("label") or info.get("name") or "required selection"))
                        report.append({"field":info.get("label") or "Selection","result":"verification_failed","required":bool(info.get("required"))});continue
                    report.append({"field":info.get("label") or "Selection","result":"filled","source":"verified_profile" if source in candidate else "confirmed_answer","required":bool(info.get("required"))})
                elif info["required"]: missing.append("No confirmed choice matches: "+(info.get("label") or info.get("name") or "required selection"))
                continue
            provider=settings.get("matching_provider","ollama")
            scoped_ai=bool(local_mock or (scope and scope.get("allow_ai_answers") and
                (provider=="ollama" or scope.get("allow_cloud_ai"))) or
                (not scope and settings.get("allow_ai_drafting") and
                 (provider=="ollama" or settings.get("allow_groq_cloud_ai") if provider=="groq" else provider!="gemini")))
            if value is None and _ordinary_open_question(info) and settings.get("allow_ai_drafting") and scoped_ai:
                try:
                    question=info.get("label") or info.get("placeholder") or "Application question"
                    field_limit=min(int(info["max_length"]) if info.get("max_length") else MAX_ANSWER_CHARS,MAX_ANSWER_CHARS)
                    word_match=re.search(r"\b(\d{1,4})\s+words?\b",question,re.I)
                    word_limit=int(word_match.group(1)) if word_match else None
                    draft=generate_draft(question,draft_safe_profile(candidate),answers.get("__preferences",{}),
                        answers.get("__job_context",{}),answers.get("__job_description",""),max_chars=field_limit,max_words=word_limit)
                    drafts.append({"question":question[:300],"answer":draft.get("answer",""),"evidence_used":draft.get("evidence_used",[]),"max_chars":draft.get("max_chars",field_limit),"max_words":draft.get("max_words",word_limit),
                        "validation":draft.get("validation",{}),"status":"ready_to_review" if draft.get("needs_user_input") else "grounded_draft",
                        "approved":False})
                    if not draft["needs_user_input"]:
                        value=draft["answer"]; source="ai_draft"
                    elif info["required"]:
                        reasons=draft.get("validation",{}).get("problems",[])
                        missing.append("AI draft needs your review for: "+(info.get("label") or "this question")+(" — "+" ".join(reasons) if reasons else ""))
                except HTTPException as error:
                    if info["required"]: missing.append("AI draft unavailable for "+(info.get("label") or "this question")+": "+str(error.detail))
            if value is None:
                if info["required"]: missing.append("Required answer is missing for: "+(info.get("label") or info.get("name") or "an application question"))
                report.append({"field":info.get("label") or info.get("name") or "Question","result":"unanswered" if info["required"] else "optional_unanswered"}); continue
            if info["tag"]=="select":
                opts=field.locator("option").all_text_contents(); label=_label(info)
                desired=_option_alias(value,label)
                choice=next((x for x in opts if _option_alias(x,label)==desired),None)
                if not choice: choice=next((x for x in opts if desired and (desired in _option_normal(x) or _option_normal(x) in desired)),None)
                if not choice and source=="discovery_source":
                    aliases={"company careers page":("company website","company careers","employer website","company site"),"linkedin":("linkedin",),"indeed":("indeed",),"handshake":("handshake",)}
                    source_terms=aliases.get(value.casefold(),(value.casefold(),))
                    choice=next((x for x in opts if any(term in x.casefold() for term in source_terms)),None)
                if choice:
                    field.select_option(label=choice)
                    selected_label=field.evaluate("el=>el.selectedOptions?.[0]?.textContent?.trim()||''")
                    if str(selected_label).strip()!=str(choice).strip():
                        missing.append("The selected answer could not be verified for: "+(info.get("label") or "required selection"))
                        report.append({"field":info.get("label") or "Selection","result":"verification_failed","required":bool(info.get("required"))});continue
                elif info["required"]: missing.append("No matching choice for: "+(info.get("label") or "required selection")); continue
            elif info["type"]!="checkbox": field.fill(value)
            elif info["type"]=="checkbox":
                tokens={part.strip().casefold() for part in str(value).splitlines()}
                option=str(info.get("value") or "").casefold()
                if value.casefold() in {"yes","true","1","agree"} or (option and option in tokens): field.check()
                elif value.casefold() in {"no","false","0","disagree"} or option: field.uncheck()
                elif info["required"]: missing.append("A confirmed choice is needed for: "+(info.get("label") or "required checkbox")); continue
            if info["type"]=="email" and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+",value):
                missing.append("The confirmed email address has an invalid format.")
            if info["type"]=="url" and not re.match(r"^https://[^\s]+$",value,re.I):
                missing.append("A URL field requires a valid HTTPS link.")
            if info["type"] not in {"file","radio"} and not _verify_field_value(field,info,choice if info["tag"]=="select" and choice else value):
                missing.append("The entered answer could not be verified for: "+(info.get("label") or info.get("name") or "required field"))
                report.append({"field":info.get("label") or info.get("name") or "Question","result":"verification_failed","required":bool(info.get("required"))});continue
            report.append({"field":info.get("label") or info.get("name") or "Question","result":"filled",
                           "source":source if source=="ai_draft" else ("verified_profile" if source in candidate else "confirmed_answer"),
                           "required":bool(info.get("required"))})
        except Exception:
            if info.get("type")=="file":
                missing.append("The active resume could not be uploaded. Check the employer's accepted file format and retry.")
                report.append({"field":"Resume upload","result":"upload_failed"})
            elif info.get("required"): missing.append("A required field could not be filled: "+(info.get("label") or "application question"))
    # Support accessible custom selects only when an exact normalized option
    # exists; do not guess among fuzzy employer-specific choices.
    try:
        widgets=page.locator('[role="combobox"]')
        for index in range(min(widgets.count(),40)):
            widget=widgets.nth(index)
            try:
                if not widget.is_visible() or not widget.is_enabled(): continue
                info=widget.evaluate("""el=>({tag:'custom-select',role:el.getAttribute('role'),type:'select',name:el.getAttribute('name')||'',id:el.id||'',placeholder:el.getAttribute('placeholder')||'',aria_label:el.getAttribute('aria-label')||'',label:(el.getAttribute('aria-label')||el.closest('label')?.innerText||el.parentElement?.innerText||'').slice(0,240),required:el.getAttribute('aria-required')==='true'||el.getAttribute('aria-required')==='1'})""")
                if info.get('role')!='combobox': continue
                value,source=_answer(info,candidate,answers)
                settings=answers.get('__settings',{})
                if ((source in candidate and not settings.get('allow_confirmed_profile_fields',False)) or
                    (isinstance(source,str) and source.startswith('custom:') and not settings.get('allow_auto_use_confirmed_answers',False))) and not answers.get('__is_local_mock'):
                    value=None
                if _requires_human_confirmation(info) or (_sensitive(info) and value is None):
                    if info['required']: missing.append('Human confirmation is required for: '+(info.get('label') or 'a sensitive selection'))
                    continue
                if value is None:
                    if info['required']: missing.append('Required answer is missing for: '+(info.get('label') or 'a selection'))
                    continue
                widget.click()
                options=page.locator('[role="option"]'); wanted=re.sub(r'[^a-z0-9]+',' ',str(value).casefold()).strip(); selected=None
                for option_index in range(min(options.count(),100)):
                    option=options.nth(option_index)
                    try:
                        if not option.is_visible(): continue
                        text=re.sub(r'[^a-z0-9]+',' ',option.inner_text().casefold()).strip()
                        if text==wanted: selected=option; break
                    except Exception: continue
                if selected:
                    selected.click(); report.append({'field':info.get('label') or 'Selection','result':'filled','source':'verified_profile' if source in candidate else 'confirmed_answer','required':bool(info.get('required'))})
                elif info['required']: missing.append('No exact confirmed choice matches: '+(info.get('label') or 'required selection'))
            except Exception: continue
    except Exception: pass
    return report,missing,drafts


def _inspect_controls(page):
    """Read visible navigation/submission controls without activating them."""
    try:
        controls=page.locator("button,a[href],input[type=button],input[type=submit]")
        result=[]
        for index in range(min(controls.count(),100)):
            node=controls.nth(index)
            try:
                if not node.is_visible(): continue
                item=node.evaluate("""el=>({tag:el.tagName.toLowerCase(),type:(el.type||'').toLowerCase(),label:(el.innerText||el.value||el.getAttribute('aria-label')||'').trim().slice(0,180),href:el.href||'',inside_form:!!el.closest('form')})""")
                item["index"]=index
                item["action"]=classify_control(item.get("label"),item.get("tag"),item.get("type"),item.get("inside_form"))
                result.append(item)
            except Exception: continue
        return result
    except Exception:
        return []


def _public_page_metadata(page):
    """Read public page identity from a visible heading or JSON-LD, never form values."""
    try:
        result=page.evaluate("""()=>{const clean=x=>String(x||'').replace(/\\s+/g,' ').trim().slice(0,240);let employer='',jobTitle='',employerSource='',titleSource='';for(const script of [...document.querySelectorAll('script[type="application/ld+json"]')].slice(0,12)){const raw=script.textContent||'';if(raw.length>64000)continue;try{const parsed=JSON.parse(raw),stack=Array.isArray(parsed)?[...parsed]:[parsed];while(stack.length){const item=stack.pop();if(!item||typeof item!=='object')continue;if(Array.isArray(item)){stack.push(...item.slice(0,40));continue}if(item['@graph'])stack.push(item['@graph']);const kinds=Array.isArray(item['@type'])?item['@type']:[item['@type']];if(kinds.includes('JobPosting')){const org=item.hiringOrganization;const name=typeof org==='string'?org:org?.name;if(!jobTitle&&clean(item.title)){jobTitle=clean(item.title);titleSource='job_posting_jsonld'}if(!employer&&clean(name)){employer=clean(name);employerSource='job_posting_jsonld'}}}}catch(_){}}const heading=[...document.querySelectorAll('h1')].find(el=>{const r=el.getBoundingClientRect(),s=getComputedStyle(el);return r.width>1&&r.height>1&&s.display!=='none'&&s.visibility!=='hidden'});if(!jobTitle&&heading&&clean(heading.innerText)){jobTitle=clean(heading.innerText);titleSource='visible_heading'}const pageTitle=clean(document.title);if(!employer){const match=pageTitle.match(/^Job Application for .+ at (.+)$/i);if(match&&clean(match[1])){employer=clean(match[1]);employerSource='document_title'}}return {page_title:pageTitle,employer,job_title:jobTitle,employer_source:employerSource,title_source:titleSource}}""")
        return result if isinstance(result,dict) else {}
    except Exception:return {}


def _visible_form_metadata(page):
    """Read public form labels and control types only; never read form values."""
    try:
        return page.locator("body").evaluate("""body=>{
          const selector='input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="reset"]):not([type="image"]),textarea,select,[role="textbox"],[role="combobox"],[role="checkbox"],[role="radio"],[contenteditable="true"],[role="button"][aria-haspopup]';
          const all=[...body.querySelectorAll(selector)].slice(0,150);
          const visible=el=>{const r=el.getBoundingClientRect(),s=getComputedStyle(el);return el.getClientRects().length&&r.width>1&&r.height>1&&r.x>-2000&&r.y>-2000&&s.display!=='none'&&s.visibility!=='hidden'&&s.opacity!=='0'&&!el.closest('[aria-hidden="true"]')};
          const ignored=all.filter(el=>!visible(el)).map(el=>{const r=el.getBoundingClientRect(),s=getComputedStyle(el),clean=x=>String(x||'').replace(/\\s+/g,' ').trim().slice(0,240);const labels=[...(el.labels||[])].map(x=>clean(x.innerText)).filter(Boolean);const labelledBy=(el.getAttribute('aria-labelledby')||'').split(/\\s+/).map(id=>clean(document.getElementById(id)?.innerText)).filter(Boolean).join(' ');const box=el.closest('fieldset,[role="group"],[data-field],[data-qa*="field" i],[data-qa*="question" i],[class*="application--field" i],[class*="application-field" i],[class*="field-container" i],[class*="question" i]');const question=clean(box?.querySelector('legend,label,[data-qa*="label" i],[class*="label" i],[class*="question" i')?.innerText);const identity=[el.id,el.name,labels.join(' '),el.getAttribute('aria-label'),labelledBy,question].join(' ').toLowerCase();const purpose=/resume|curriculum vitae|\\bcv\\b/.test(identity)?'resume':/cover[ _-]?letter/.test(identity)?'cover_letter':/transcript/.test(identity)?'transcript':/certificate/.test(identity)?'certificate':/portfolio|work sample/.test(identity)?'portfolio':'unspecified';return {tag:el.tagName.toLowerCase(),input_type:(el.getAttribute('type')||'').toLowerCase(),role:el.getAttribute('role')||'',id:(el.id||'').slice(0,100),name:(el.name||el.getAttribute('name')||'').slice(0,100),required:el.hasAttribute('required')||el.getAttribute('aria-required')==='true'||el.getAttribute('aria-required')==='1',reason:el.closest('[aria-hidden="true"]')?'aria_hidden':s.display==='none'?'display_none':s.visibility==='hidden'?'visibility_hidden':s.opacity==='0'?'transparent':r.width<=1||r.height<=1?'zero_size':'offscreen',accept:(el.getAttribute('accept')||'').slice(0,120),label_text:labels.join(' ').slice(0,240),aria_label:(el.getAttribute('aria-label')||'').slice(0,180),aria_labelledby_text:labelledBy.slice(0,240),associated_question:question,upload_purpose:el.type==='file'?purpose:''}});
          const result=all.filter(el=>visible(el)&&!el.disabled).slice(0,100).map(el=>{
            const clean=x=>String(x||'').replace(/\\s+/g,' ').trim();
            const type=(el.getAttribute('type')||'').toLowerCase();
            const role=(el.getAttribute('role')||'').toLowerCase();
            const combo=role==='combobox'||el.getAttribute('aria-autocomplete')!==null||el.getAttribute('aria-haspopup')==='listbox'||!!el.getAttribute('list')||!!el.closest('[role="combobox"]');
            let kind=type||({combobox:'combobox',checkbox:'checkbox',radio:'radio',textbox:'text',button:'combobox'}[role]||(el.tagName==='INPUT'?'text':el.tagName.toLowerCase()));
            if(el.tagName==='INPUT'&&(!type||type==='text')&&combo)kind='combobox';
            if(el.tagName==='TEXTAREA') kind='textarea';
            if(el.tagName==='SELECT') kind='select';
            if(el.isContentEditable) kind='contenteditable';
            const labelledBy=(el.getAttribute('aria-labelledby')||'').split(/\\s+/).map(id=>document.getElementById(id)?.innerText||'').join(' ');
            const semanticContainer=el.closest('fieldset,[role="group"],[role="radiogroup"],[data-field],[data-qa*="field" i],[data-qa*="question" i],[class*="application--field" i],[class*="application-field" i],[class*="field-container" i],[class*="question" i]');
            const semanticText=clean(semanticContainer?.innerText||'');
            const container=semanticContainer&&semanticText.length<=500?semanticContainer:el.parentElement?.parentElement||el.parentElement;
            const controls=container?[...container.querySelectorAll('input:not([type="hidden"]),textarea,select,[role="textbox"],[role="combobox"],[role="checkbox"],[role="radio"]')].filter(x=>visible(x)&&!x.disabled).length:1;
            const candidates=container?[...container.querySelectorAll('legend,label,[data-qa*="label" i],[class*="label" i],[class*="question" i]')].map(x=>String(x.innerText||'').replace(/\\s+/g,' ').trim()).filter(x=>x&&x.length<=240):[];
            const direct=[...(el.labels||[])].map(x=>x.innerText||'').join(' ')||el.getAttribute('aria-label')||labelledBy||el.closest('label')?.innerText||el.closest('fieldset')?.querySelector('legend')?.innerText||'';
            const informative=candidates.sort((a,b)=>((/\\?\\s*$/.test(b)||/[*✱＊]\\s*$/.test(b)?4:0)+Math.min(b.length,100)/100)-((/\\?\\s*$/.test(a)||/[*✱＊]\\s*$/.test(a)?4:0)+Math.min(a.length,100)/100))[0]||'';
            const directText=clean(direct);
            const groupPrompt=clean(el.closest('fieldset,[role="radiogroup"],[role="group"]')?.querySelector('legend,[aria-label]')?.innerText||el.closest('[role="radiogroup"],[role="group"]')?.getAttribute('aria-label')||'');
            const noStableSemantics=el.tagName==='INPUT'&&!type&&!el.id&&!(el.name||el.getAttribute('name'))&&!role;
            const broadContainer=noStableSemantics&&!directText;
            const question=broadContainer?'':(type==='radio'||role==='radio'||type==='checkbox'||role==='checkbox')?(groupPrompt||informative||directText):(directText&&directText!=='Attach'?directText:informative||directText||'');
            const normalizedLabel=clean(directText||question||el.getAttribute('placeholder')||'').slice(0,160);
            const visualRequired=/[*✱＊]\\s*$|\\brequired\\b\\s*$/i.test(clean(question+' '+normalizedLabel));
            const questionText=clean(question).slice(0,160);
            const containerToken=container&&(container.id||container.getAttribute('data-field')||container.getAttribute('data-qa'));
            const questionKey=questionText?(containerToken?'container:'+clean(containerToken).slice(0,120):'question:'+clean(questionText).toLowerCase()):'';
            const uploadContext=(questionText+' '+normalizedLabel).toLowerCase();
            const uploadPurpose=kind==='file'?( /resume|curriculum vitae|\\bcv\\b/.test(uploadContext)?'resume':/transcript/.test(uploadContext)?'transcript':/cover letter/.test(uploadContext)?'cover_letter':/certificate/.test(uploadContext)?'certificate':/portfolio|work sample/.test(uploadContext)?'portfolio':'unspecified') : '';
            const closedQuestion=/^(are you|do you|did you|will you|would you|what archetype|select |choose )/i.test(questionText);
            const possibleCustom=kind==='text'&&closedQuestion;
            const classHint=String(el.className&&typeof el.className==='string'?el.className:'').replace(/[^a-zA-Z0-9 _-]/g,' ').replace(/\\s+/g,' ').trim().slice(0,120);
            const containerClass=String(container?.className&&typeof container.className==='string'?container.className:'').replace(/[^a-zA-Z0-9 _-]/g,' ').replace(/\\s+/g,' ').trim().slice(0,120);
            const ariaHints={autocomplete:el.getAttribute('aria-autocomplete')||'',haspopup:el.getAttribute('aria-haspopup')||'',controls:(el.getAttribute('aria-controls')||'').slice(0,120),placeholder:(el.getAttribute('placeholder')||'').slice(0,120)};
            return {kind,label:normalizedLabel,question:questionText,question_key:questionKey,tag:el.tagName.toLowerCase(),input_type:type,role,
              name:(el.name||el.getAttribute('name')||'').slice(0,120),id:(el.id||'').slice(0,120),
              required:broadContainer?null:!!el.required||el.getAttribute('aria-required')==='true'||el.getAttribute('aria-required')==='1'||visualRequired,
              inside_form:!!el.closest('form,[role="form"]'),accept:kind==='file'?(el.getAttribute('accept')||'').slice(0,120):'',
              upload_purpose:uploadPurpose,possible_custom_selector:possibleCustom,
              class_hint:classHint,container_class_hint:containerClass,aria_hints:ariaHints,
              conditional:!!(el.closest('[data-conditional],[data-conditional-field],[aria-controls]')||container?.matches('[data-conditional],[data-conditional-field]')),
              unresolved_reason:broadContainer?'Visible control has no stable field semantics and shares a broad container; possible implementation companion, not safely attributable':!questionText?'No associated label or question text was exposed by the visible control':noStableSemantics?'Visible control has no input type, id, name, or ARIA role; possible implementation companion, not confirmed applicant field':'',container_control_count:controls};
          });
          result.push({__inspection_meta__:true,ignored_controls:ignored.slice(0,100)});
          return result;
        }""") or []
    except Exception:
        return []


def _inspect_public_controls(page):
    """Inspect button/link semantics, including custom ARIA buttons, without activation."""
    try:
        controls=page.locator("button,a[href],input[type=button],input[type=submit],input[type=image],[role=button]")
        result=[]
        for index in range(min(controls.count(),100)):
            control=controls.nth(index)
            try:
                visible=control.is_visible()
                item=control.evaluate("""el=>{const label=(el.innerText||el.getAttribute('aria-label')||el.getAttribute('title')||((el.tagName==='INPUT')?el.value||el.getAttribute('value'):'')||'').trim().slice(0,180);const box=el.closest('[class*="application--field" i],[class*="application-field" i],[class*="field-container" i],[class*="question" i],[data-qa*="field" i],[data-qa*="question" i],fieldset,[role="group"],[role="form"],form')||el.parentElement;const context=String(box?.innerText||'').slice(0,800);const upload=/^(attach|upload|choose file|browse)(\\s+(file|resume|document))?$/i.test(label);const lines=context.split(/\\n+/).map(x=>x.trim()).filter(Boolean);const visibleLabel=box?.querySelector('label,[data-qa*="label" i],[class*="label" i],legend')?.innerText||'';const question=[visibleLabel,...lines].map(x=>String(x||'').replace(/\\s+/g,' ').trim()).find(x=>/resume|curriculum vitae|\\bcv\\b|transcript|cover letter|certificate|portfolio|work sample/i.test(x))||'';const purpose=/resume|curriculum vitae|\\bcv\\b/i.test(question)?'resume':/transcript/i.test(question)?'transcript':/cover letter/i.test(question)?'cover_letter':/transcript/i.test(question)?'transcript':/certificate/i.test(question)?'certificate':/portfolio|work sample/i.test(question)?'portfolio':'unspecified';const type=el.getAttribute('type')||el.getAttribute('data-type')||(el.tagName==='BUTTON'?(el.closest('form')?'submit':'button'):(el.type||'button'));const form=el.closest('form,[role="form"]');const action=form?.getAttribute('action')||'';let actionPath='';try{actionPath=action?new URL(action,location.href).pathname:''}catch{}return {tag:el.getAttribute('role')==='button'?'button':el.tagName.toLowerCase(),type:type.toLowerCase(),label,aria_label:(el.getAttribute('aria-label')||'').slice(0,180),id:(el.id||'').slice(0,100),name:(el.name||'').slice(0,100),inside_form:!!form,form_id:(form?.id||'').slice(0,100),form_action_path:actionPath.slice(0,240),form_method:(form?.getAttribute('method')||'').toLowerCase(),disabled:!!el.disabled,upload_prompt:upload&&!!form,upload_question:question.slice(0,180),upload_purpose:purpose,upload_required:/[*✱＊]/.test(question),aria_controls:(el.getAttribute('aria-controls')||'').slice(0,120),for:(el.getAttribute('for')||'').slice(0,120)}}""")
                item["action"]=classify_control(item.get("label"),item.get("tag"),item.get("type"),item.get("inside_form"))
                item["visible"]=bool(visible)
                if not visible and item["action"]!="final_submission":continue
                result.append(item)
            except Exception: continue
        return result
    except Exception:
        return []


def inspect_public_application_form(url):
    """Inspect a public application page in an empty, GET-only browser context.

    This code path does not call candidate/profile APIs, read field values,
    upload files, click controls, or create an application attempt.
    """
    clean_url = ""
    provider={"provider":"employer_site","support_level":"generic_accessible_form","adapter":"shared_semantic_form"}
    try:
        if not isinstance(url,str) or len(url)>2048: raise ValueError("invalid URL")
        parsed=urlparse(url)
        if not parsed.hostname or parsed.username or parsed.password: raise ValueError("invalid URL")
        clean_url = parsed._replace(query="", fragment="").geturl()
        provider=detect_application_provider(url)
    except (TypeError, ValueError):
        return {"application_url": "", "inspection_outcome": "inaccessible",
                "reason": "This destination is not a permitted public HTTPS page.","failure_category":"unsafe_destination"}
    if not _inspection_lock.acquire(blocking=False):
        return {"application_url": clean_url, "inspection_outcome": "inaccessible",
                "provider":provider,"form_detected":False,"page_classification":"not_inspected",
                "reason": "Another public form inspection is already running. Try again shortly."}
    browser = context = page = None
    policy_state={}
    try:
        permitted, policy_reason, policy_category=_robots_permission(url)
        if not permitted:
            return {"application_url": clean_url, "inspection_outcome": "inaccessible", "reason": policy_reason,
                    "provider":provider,"form_detected":False,"page_classification":"not_inspected",
                    "robots_policy":"not_verified","failure_category":policy_category}
        from playwright.sync_api import sync_playwright
        with _ManagedPlaywrightContext(sync_playwright) as p:
            browser = p.chromium.launch(headless=True)
            p.attach(None,browser)
            context = browser.new_context()
            page = context.new_page()
            p.attach(page,browser)
            page.route("**/*", lambda route:_guard_browser_request(route,policy_state))
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=20000)
                # Greenhouse job boards can hydrate application controls after
                # the document event. Wait briefly for visible controls without
                # clicking or typing; otherwise inspect the posting as-is.
                try:
                    page.wait_for_function("""()=>[...document.querySelectorAll('input:not([type="hidden"]),textarea,select,[role="textbox"],[role="combobox"],[role="checkbox"],[role="radio"]')].some(el=>{const r=el.getBoundingClientRect(),s=getComputedStyle(el);return r.width>1&&r.height>1&&s.display!=='none'&&s.visibility!=='hidden'&&s.opacity!=='0'&&!el.disabled})""",timeout=2500)
                except Exception:
                    # Hydration waiting is best-effort. Older Playwright builds
                    # and test doubles may not provide wait_for_timeout.
                    try:page.wait_for_timeout(350)
                    except Exception:pass
            except Exception as exc:
                if policy_state.get("category"):
                    return {"application_url":clean_url,"inspection_outcome":"inaccessible","provider":provider,
                        "form_detected":False,"page_classification":"not_inspected","robots_policy":"not_verified",
                        "failure_category":policy_state.get("category","navigation_blocked"),
                        "reason":policy_state.get("reason","The employer navigation was blocked by a safety policy.")}
                reason = "Employer page could not be opened (" + type(exc).__name__ + "). Check that the public application link is current and accessible."
                return {"application_url": clean_url, "inspection_outcome": "inaccessible", "provider":provider,
                        "form_detected":False,"page_classification":"not_inspected","robots_policy":"verified_allow",
                        "failure_category":"navigation_error","reason": reason}
            if policy_state.get("category"):
                return {"application_url":clean_url,"inspection_outcome":"inaccessible","provider":provider,
                    "form_detected":False,"page_classification":"not_inspected","robots_policy":"not_verified",
                    "failure_category":policy_state.get("category","navigation_blocked"),
                    "reason":policy_state.get("reason","The public page attempted a request that could not be safely inspected.")}
            # A redirect is subject to the same public HTTPS/IP checks as the
            # original link. No Apply links or form controls are activated.
            try:
                validate_application_url(page.url)
            except ValueError:
                return {"application_url": clean_url, "inspection_outcome": "inaccessible",
                        "provider":provider,"form_detected":False,"page_classification":"not_inspected",
                        "robots_policy":"verified_allow","failure_category":"unsafe_destination",
                        "reason": "The employer page redirected to a restricted destination."}
            permitted,policy_reason,policy_category=_robots_permission(page.url)
            if not permitted:
                return {"application_url":clean_url,"inspection_outcome":"inaccessible","provider":provider,
                    "form_detected":False,"page_classification":"not_inspected","robots_policy":"not_verified",
                    "failure_category":policy_category,"reason":policy_reason}
            fields = _visible_form_metadata(page)
            controls = _inspect_public_controls(page)
            interruption = _interruption(page)
            try:
                body = page.locator("body").inner_text(timeout=2000)[:10000]
            except Exception:
                body = ""
            expired = page_interruption(body) == "expired_listing"
            application_form,page_classification=_classify_inspected_page(page.url,fields,controls,body)
            report = summarize_form_metadata(detect_application_provider(page.url), fields, controls,
                                             interruption=interruption, expired=expired)
            report["page_metadata"]=_public_page_metadata(page)
            report["blocked_non_read_only_request_count"]=int(policy_state.get("blocked_non_read_only_request_count",0))
            report["blocked_non_read_only_requests"]=policy_state.get("blocked_non_read_only_requests",[])[:8]
            report["form_detected"]=application_form
            report["page_classification"]=page_classification
            if not application_form and _posting_redirected_away(clean_url, page.url):
                report["page_classification"]="posting_redirected"
                report["inspection_outcome"]="inaccessible"
                report["reason"]="The job-specific URL redirected away from its posting. The posting and application form could not be verified."
            if not application_form and not expired and not report.get("human_actions_required"):
                if report.get("page_classification") != "posting_redirected":
                    report["inspection_outcome"]="unsupported"
                    report["reason"]=("A public job posting was reached, but no application form was exposed on this page."
                                      if page_classification=="job_posting" else
                                      "The public page loaded, but an employer application form could not be verified.")
            report["application_url"] = urlparse(page.url)._replace(query="", fragment="").geturl()
            report["http_status"] = int(response.status) if response else None
            report["robots_policy"] = "verified_allow"
            if response and response.status >= 400:
                report["inspection_outcome"] = "inaccessible"
                report["reason"] = "The public employer page returned HTTP " + str(response.status) + "."
            return report
    except Exception as exc:
        return {"application_url": clean_url, "inspection_outcome": "inaccessible",
                "reason": "The public form could not be inspected (" + type(exc).__name__ + ").",
                "failure_category":"inspection_error"}
    finally:
        _inspection_lock.release()


def _apply_links(page):
    try:
        raw=page.locator("a[href]").evaluate_all("""els=>els.filter(e=>e.getClientRects().length).map(e=>({label:(e.innerText||e.getAttribute('aria-label')||'').trim(),href:e.href}))""")
        return apply_targets(page.url,raw)
    except Exception:
        return []


def _has_form_fields(page):
    try:
        return (page.locator("input:not([type=hidden]),textarea,select,[role=textbox],[role=checkbox],[role=radio],[contenteditable=true]").count()>0
                or page.locator('[role="combobox"]').count()>0)
    except Exception:
        return False


def _step_signature(page):
    """Fingerprint visible controls before and after a safe next-step action."""
    values=[]
    fields=page.locator("input:not([type=hidden]),textarea,select,[role=textbox],[role=checkbox],[role=radio],[contenteditable=true]")
    try:
        for index in range(min(fields.count(),100)):
            field=fields.nth(index)
            if not field.is_visible():
                continue
            info=field.evaluate("""el=>({name:el.name||el.getAttribute('name')||'',id:el.id||'',type:(el.type||el.getAttribute('role')||el.tagName).toLowerCase(),label:((el.labels||[]).map(x=>x.innerText).join(' ')||el.getAttribute('aria-label')||el.placeholder||el.parentElement?.innerText||'').trim().slice(0,120),required:el.required||el.getAttribute('aria-required')==='true'||el.getAttribute('aria-required')==='1'})""")
            values.append(tuple(info.get(key, "" if key!="required" else False) for key in ("name","id","type","label","required")))
    except Exception:
        pass
    return (str(getattr(page,"url","")),tuple(values))


def _required_visible_fields(page):
    result=[]
    fields=page.locator("input:not([type=hidden]),textarea,select,[role=textbox],[role=checkbox],[role=radio],[contenteditable=true]")
    try:
        for index in range(min(fields.count(),100)):
            field=fields.nth(index)
            if not field.is_visible() or not field.is_enabled():
                continue
            info=field.evaluate("""el=>({required:!!el.required||el.getAttribute('aria-required')==='true'||el.getAttribute('aria-required')==='1',label:((el.labels||[]).map(x=>x.innerText).join(' ')||el.getAttribute('aria-label')||el.closest('fieldset')?.querySelector('legend')?.innerText||el.placeholder||el.name||el.id||'Unresolved required field').trim().slice(0,160)})""")
            if info.get("required") or _required_marker(info.get("label")):
                result.append(info.get("label") or "Required field")
    except Exception:
        return list(dict.fromkeys(result))
    return list(dict.fromkeys(result))


def _required_field_issues(page):
    """Return labels only for visible required controls that remain incomplete.

    Values are examined in the page process but never copied into diagnostics.
    This second pass catches conditional controls that appeared after the first
    field scan, including accessible custom controls.
    """
    try:
        return page.locator("body").evaluate("""body=>{
          const selector='input:not([type="hidden"]):not([type="submit"]):not([type="button"]),textarea,select,[role="textbox"],[role="combobox"],[role="checkbox"],[role="radio"],[contenteditable="true"]';
          return [...body.querySelectorAll(selector)].filter(el=>el.getClientRects().length&&!el.disabled).flatMap(el=>{
            const labels=[...(el.labels||[])].map(x=>x.innerText||'').join(' ');
            const labelledBy=(el.getAttribute('aria-labelledby')||'').split(/\\s+/).map(id=>document.getElementById(id)?.innerText||'').join(' ');
            const direct=String(labels||el.getAttribute('aria-label')||labelledBy||el.closest('label')?.innerText||el.closest('fieldset')?.querySelector('legend')?.innerText||'').replace(/\\s+/g,' ').trim();
            const container=el.closest('fieldset,[role="group"],[role="radiogroup"],[data-field],[data-qa*="field" i],[data-qa*="question" i],[class*="application--field" i],[class*="application-field" i],[class*="field-container" i],[class*="question" i]')||el.parentElement?.parentElement||el.parentElement;
            const text=String(container?.innerText||'').replace(/\\s+/g,' ').trim();
            const candidates=container?[...container.querySelectorAll('legend,label,[data-qa*="label" i],[class*="label" i],[class*="question" i]')].map(x=>String(x.innerText||'').replace(/\\s+/g,' ').trim()).filter(x=>x&&x.length<=240):[];
            const candidate=candidates.sort((a,b)=>((/\\?\\s*$|[*✱＊]\\s*$/.test(b)?4:0)+Math.min(b.length,100)/100)-((/\\?\\s*$|[*✱＊]\\s*$/.test(a)?4:0)+Math.min(a.length,100)/100))[0]||'';
            const noStable=el.tagName==='INPUT'&&!(el.getAttribute('type')||'')&&!el.id&&!(el.name||el.getAttribute('name'))&&!el.getAttribute('role')&&!direct;
            const label=String(direct||(text.length<=500?candidate:'')||el.placeholder||el.name||el.id||'Unresolved required field').replace(/\\s+/g,' ').trim().slice(0,160);
            const required=!!el.required||el.getAttribute('aria-required')==='true'||el.getAttribute('aria-required')==='1'||/[*✱＊]\\s*$|(?<!\\w)[*✱＊](?!\\w)|\\brequired\\b\\s*$/i.test(label);
            if(noStable)return required?['Unresolved required field']:[];
            if(!required)return [];
            const type=(el.type||'').toLowerCase(),role=(el.getAttribute('role')||'').toLowerCase();
            const group=(type==='radio'||role==='radio')?el.name||el.getAttribute('name')||label:null;
            if(group){const peers=[...body.querySelectorAll('input[type="radio"],[role="radio"]')].filter(x=>(x.name||x.getAttribute('name')||'')===group&&x.getClientRects().length);if(peers.some(x=>x.checked||x.getAttribute('aria-checked')==='true'))return [];}
            if(type==='checkbox'||role==='checkbox')return (el.checked||el.getAttribute('aria-checked')==='true')?[]:[label];
            if(type==='file')return el.files?.length?[]:[label];
            if((type==='select-one'||el.tagName==='SELECT')&&el.selectedOptions?.length)return el.selectedOptions[0].value?[]:[label];
            const value=el.isContentEditable?el.textContent:(el.value??el.innerText??'');
            return String(value||'').trim()?[]:[label];
          });
        }""") or []
    except Exception:
        # The native validity check below remains a second line of defense.
        return []


def _verify_field_value(field,info,expected):
    """Verify an interaction took effect without returning the entered value."""
    kind=str(info.get("type") or "").casefold()
    try:
        if kind=="checkbox":
            should_check=str(expected).strip().casefold() in {"yes","true","1","agree"} or str(info.get("value") or "").casefold() in {part.strip().casefold() for part in str(expected).splitlines()}
            return bool(field.is_checked()) is should_check
        if info.get("tag")=="select":
            selected=field.evaluate("el=>el.selectedOptions?.[0]?.textContent?.trim()||''")
            return str(selected or "").strip()==str(expected or "").strip()
        if kind in {"text","email","tel","url","number","date","textarea","textbox"}:
            actual=field.evaluate("el=>('value' in el ? el.value : el.textContent)||''") if info.get("contenteditable") else field.input_value()
            return str(actual or "").strip()==str(expected or "").strip()
    except Exception:
        return False
    return True


def _has_password(page):
    try:
        fields=page.locator('input[type="password"]')
        return any(fields.nth(i).is_visible() for i in range(min(fields.count(),10)))
    except Exception: return False


def _has_captcha_widget(page):
    try:
        return page.locator('iframe[src*="recaptcha" i],iframe[src*="hcaptcha" i],[data-sitekey],.g-recaptcha,.h-captcha').count()>0
    except Exception: return False


def _interruption(page):
    try: text=page.locator("body").inner_text(timeout=2000)
    except Exception: text=""
    try:
        if re.search(r"/(?:login|log-in|signin|sign-in|auth|sso)(?:/|$)",urlparse(page.url).path,re.I):
            return "authentication_required"
    except (TypeError,ValueError): pass
    if _has_captcha_widget(page): return "human_verification"
    return page_interruption(text,_has_password(page))


def _classify_inspected_page(url,fields,controls,body_text):
    """Distinguish an application form from a job posting or unrelated page."""
    fields=[item for item in (fields or []) if isinstance(item,dict) and not item.get("__inspection_meta__")]
    controls=controls or []
    text=str(body_text or "")[:12000]
    path=(urlparse(str(url or "")).path or "").casefold()
    application_context=bool(re.search(r"\b(application|apply for|apply to|candidate application)\b",text,re.I) or
                             re.search(r"/(?:apply|application)(?:/|$)",path))
    strong_field=re.compile(r"\b(resume|curriculum vitae|cover letter|linkedin|work authorization|sponsorship|graduation|education|degree|employment history|work experience)\b",re.I)
    labels=[str(item.get("label") or "") for item in fields]
    has_application_field=any(strong_field.search(label) for label in labels)
    contact_fields=sum(bool(re.search(r"\b(name|email|phone)\b",label,re.I)) for label in labels)
    form_bound=any(item.get("inside_form") for item in fields)
    apply_link=any(item.get("action")=="apply_link" for item in controls)
    final_control=any(item.get("action")=="final_submission" for item in controls)
    detected=bool(fields) and (has_application_field or
                              (application_context and ((form_bound and len(fields)>=2) or final_control or contact_fields>=2)))
    if detected:
        classification="application_form"
    elif apply_link or re.search(r"\b(job description|responsibilities|qualifications|apply now)\b",text,re.I):
        classification="job_posting"
    elif fields:
        classification="public_form_unverified"
    else:
        classification="public_page_unverified"
    return detected,classification


def _posting_redirected_away(requested_url, landed_url):
    """Recognize common job-detail URLs that land on a different, non-detail page."""
    try:
        requested=urlparse(str(requested_url or ""))
        landed=urlparse(str(landed_url or ""))
        if not requested.hostname or not landed.hostname:
            return False
        if requested.hostname.casefold() != landed.hostname.casefold():
            return False
        requested_path=requested.path.rstrip("/")
        landed_path=landed.path.rstrip("/")
        patterns=(r"/(?:jobs?|positions?)/([^/]+)$", r"/([0-9a-f]{8}-[0-9a-f-]{27,})$")
        identifier=None
        for pattern in patterns:
            match=re.search(pattern,requested_path,re.I)
            if match:
                identifier=match.group(1).casefold()
                break
        if not identifier:
            return False
        return identifier not in landed_path.casefold() and not re.search(r"/(?:apply|application)(?:/|$)",landed_path,re.I)
    except (TypeError,ValueError):
        return False

def _screenshot(page,app_id):
    try:
        with connect() as db: row=db.execute("SELECT attempt_number FROM applications WHERE id=? AND user_id=?",(app_id,USER)).fetchone()
        number=max(1,int(row["attempt_number"] or 1)) if row else 1
        name=f"{app_id}-attempt-{number}.png"
        page.screenshot(path=str(ARTIFACTS/name),full_page=True,timeout=5000); return name
    except Exception: return None

def _process_application_inner(app_id,job,resume,candidate,answers):
    if "__is_local_mock" not in answers:
        answers=_application_context(job,answers)
    diagnostic={"mode":"dry_run","provider":detect_application_provider(job.get("application_url")),
        "filled_fields":[],"required_fields":[],"unsupported_fields":[],"human_actions_required":[],
        "unanswered_fields":[],"draft_answers":[],"steps":0,"submission_performed":False,"screenshot":None,"message":""}
    if _paused(): _update(app_id,"paused",diagnostic); return
    posting_blocker=_posting_preparation_blocker(job.get("job_key"))
    if posting_blocker:
        diagnostic["message"]=posting_blocker
        diagnostic["human_actions_required"]=[posting_blocker]
        diagnostic["unanswered_fields"]=[posting_blocker]
        _update(app_id,"needs_user_input",diagnostic);return
    _update(app_id,"opening",diagnostic)
    local_mock=_mock_allowed(job.get("application_url",""))
    policy_state={}
    try:
        validate_application_url(job["application_url"])
        if not local_mock:
            scope=_current_preparation_authorization(job.get("job_key"),resume["id"],job["application_url"])
            if not scope:
                diagnostic["message"]="Job-specific authorization is missing or stale. Review the employer and authorization plan before continuing."
                diagnostic["unanswered_fields"]=["Authorize this employer and exact application URL again."]
                _update(app_id,"needs_user_input",diagnostic);return
            diagnostic["authorization"]={"status":"current","authorized_at":scope.get("authorized_at"),
                "selected_field_count":len(scope.get("field_keys") or []),
                "selected_document_types":list(scope.get("document_types") or []),
                "ai_answers_authorized":bool(scope.get("allow_ai_answers")),
                "cloud_ai_authorized":bool(scope.get("allow_cloud_ai")),
                "consent_version":PREPARATION_CONSENT_VERSION}
            candidate=_scoped_candidate(candidate,scope)
            answers=_application_context(job,answers)
            answers=_scoped_answers(answers,scope)
            diagnostic["phase"]="read_only_preflight"
            _update(app_id,"inspecting",diagnostic)
            preflight=inspect_public_application_form(job["application_url"])
            preflight=_save_form_inspection(job,preflight,preparation_preflight=True)
            blocker=_preparation_inspection_blocker(preflight)
            diagnostic["compatibility_report"]={key:preflight.get(key) for key in
                ("provider","form_detected","page_classification","inspection_outcome","compatibility_status","page_metadata","canonical_application_url","inspection_timestamp","form_fingerprint","required_fields","unsupported_fields",
                 "required_control_count","total_dom_control_count","visible_form_control_count","required_dom_control_count","unique_question_count","unique_required_question_count","unique_optional_question_count","unique_unknown_requirement_question_count",
                 "questions","unresolved_controls","ignored_controls","ignored_control_count","conditional_questions","conditional_detection_note",
                 "file_upload_controls","upload_prompts","upload_control_details","human_actions_required","multi_step_detected","submission_control_detected","submission_controls",
                 "submission_control_status","blocked_non_read_only_request_count","blocked_non_read_only_requests","robots_policy","form_fingerprint","reason")}
            if blocker:
                diagnostic["message"]=blocker
                diagnostic["human_actions_required"]=list(preflight.get("human_actions_required") or [])
                diagnostic["unanswered_fields"]=[blocker]
                diagnostic["screenshot"]=None
                _update(app_id,"needs_user_input" if diagnostic["human_actions_required"] else "unsupported",diagnostic);return
        from playwright.sync_api import sync_playwright
        resume_path=STORAGE/resume["stored_name"]
        if not resume_path.is_file(): raise RuntimeError("The active resume file is unavailable.")
        with _ManagedPlaywrightContext(sync_playwright) as p:
            diagnostic["phase"]="browser_launch"
            browser=p.chromium.launch(headless=browser_headless());p.attach(None,browser)
            context=browser.new_context(accept_downloads=False); page=context.new_page();p.attach(page,browser)
            page.set_default_timeout(20000)
            page.route("**/*",lambda route:_guard_browser_request(route,policy_state))
            target=job["application_url"]; visited=set()
            for _ in range(4):
                if target in visited:
                    diagnostic["message"]="The employer page redirected repeatedly. Review the job link manually."; diagnostic["screenshot"]=_screenshot(page,app_id)
                    _close_browser_safely(page,browser); _update(app_id,"unsupported",diagnostic); return
                visited.add(target); validate_application_url(target)
                if not local_mock:
                    permitted,reason,category=_robots_permission(target)
                    if not permitted:
                        diagnostic["failure_code"]=category
                        diagnostic["message"]=reason or "Robots policy did not permit this employer page."
                        diagnostic["unanswered_fields"]=[diagnostic["message"]]
                        _close_browser_safely(page,browser);_update(app_id,"unsupported",diagnostic);return
                diagnostic["phase"]="navigation"
                page.goto(target,wait_until="domcontentloaded",timeout=25000); page.wait_for_timeout(100)
                if policy_state:
                    diagnostic["failure_code"]=policy_state.get("category")
                    diagnostic["message"]=policy_state.get("reason") or "A navigation was blocked by a safety policy."
                    diagnostic["unanswered_fields"]=[diagnostic["message"]]
                    _close_browser_safely(page,browser);_update(app_id,"unsupported",diagnostic);return
                validate_application_url(page.url)
                _update(app_id,"inspecting",diagnostic)
                interruption=_interruption(page)
                if interruption:
                    diagnostic["message"]={"human_verification":"Human verification detected. No bypass was attempted.","authentication_required":"The employer requires sign-in. Complete authentication manually before retrying.","expired_listing":"This job listing appears expired or closed.","automation_restricted":"This employer page says automated application preparation is not permitted. The dry run stopped."}[interruption]
                    diagnostic["screenshot"]=_screenshot(page,app_id)
                    _close_browser_safely(page,browser); _update(app_id,"unsupported" if interruption in {"human_verification","expired_listing","automation_restricted"} else "needs_user_input",diagnostic); return
                if _has_form_fields(page): break
                links=_apply_links(page)
                if not links:
                    apply_button=next((x for x in _inspect_controls(page) if x["action"]=="apply_button"),None)
                    if apply_button:
                        try:
                            page.locator("button,a[href],input[type=button],input[type=submit]").nth(apply_button["index"]).click(timeout=2000)
                            page.wait_for_timeout(150)
                            if page.url==target and not _has_form_fields(page):
                                diagnostic["message"]="The Apply button did not open a supported form. No submission was attempted."; diagnostic["screenshot"]=_screenshot(page,app_id)
                                _close_browser_safely(page,browser); _update(app_id,"unsupported",diagnostic); return
                            target=page.url
                            continue
                        except Exception:
                            diagnostic["message"]="The Apply button could not be followed safely."; diagnostic["screenshot"]=_screenshot(page,app_id)
                            _close_browser_safely(page,browser); _update(app_id,"unsupported",diagnostic); return
                    diagnostic["message"]="No supported application form or clearly labeled Apply link was found."; diagnostic["screenshot"]=_screenshot(page,app_id)
                    _close_browser_safely(page,browser); _update(app_id,"unsupported",diagnostic); return
                next_target=links[0]
                if next_target in visited:
                    diagnostic["message"]="The Apply link loops back to a page already visited."; diagnostic["screenshot"]=_screenshot(page,app_id)
                    _close_browser_safely(page,browser); _update(app_id,"unsupported",diagnostic); return
                validate_application_url(next_target)
                if not local_mock:
                    permitted,reason,category=_robots_permission(next_target)
                    if not permitted:
                        diagnostic["failure_code"]=category
                        diagnostic["message"]=reason or "Robots policy did not permit the employer's application link."
                        diagnostic["unanswered_fields"]=[diagnostic["message"]]
                        _close_browser_safely(page,browser);_update(app_id,"unsupported",diagnostic);return
                target=next_target
            else:
                diagnostic["message"]="The application page could not be reached after several navigation steps."; diagnostic["screenshot"]=_screenshot(page,app_id)
                _close_browser_safely(page,browser); _update(app_id,"unsupported",diagnostic); return
            diagnostic["form_detected"]=bool(_has_form_fields(page))
            if not diagnostic["form_detected"]:
                diagnostic["message"]="The application page opened, but no supported form controls were detected. No application was submitted."
                diagnostic["screenshot"]=_screenshot(page,app_id)
                _close_browser_safely(page,browser); _update(app_id,"unsupported",diagnostic); return
            diagnostic["phase"]="form_filling"
            _update(app_id,"filling",diagnostic); missing=[]
            for step in range(MAX_STEPS):
                interruption=_interruption(page)
                if interruption:
                    messages={"human_verification":"Human verification detected. No bypass was attempted.",
                        "authentication_required":"The employer requires sign-in. Complete authentication manually before retrying.",
                        "expired_listing":"This job listing appears expired or closed.",
                        "automation_restricted":"This employer page says automated application preparation is not permitted. The dry run stopped."}
                    diagnostic["message"]=messages.get(interruption,"The employer requires a human action.")
                    diagnostic["human_actions_required"]=[diagnostic["message"]]
                    diagnostic["unanswered_fields"]=[diagnostic["message"]]
                    diagnostic["screenshot"]=_screenshot(page,app_id)
                    _close_browser_safely(page,browser);_update(app_id,"unsupported" if interruption in {"expired_listing","automation_restricted"} else "needs_user_input",diagnostic);return
                if _paused():
                    diagnostic["message"]="Paused by user; prior field entries in this browser attempt are retained."; diagnostic["screenshot"]=_screenshot(page,app_id)
                    _close_browser_safely(page,browser); _update(app_id,"paused",diagnostic); return
                if not answers.get("__is_local_mock") and not get_authorization()["settings"].get("allow_automated_form_preparation"):
                    diagnostic["message"]="Automated form preparation permission was revoked. The dry run stopped safely."; diagnostic["unanswered_fields"].append("Re-enable form preparation in Application settings to continue.")
                    diagnostic["screenshot"]=_screenshot(page,app_id); _close_browser_safely(page,browser); _update(app_id,"needs_user_input",diagnostic); return
                if not local_mock:
                    current_scope=_current_preparation_authorization(job.get("job_key"),resume["id"],job["application_url"])
                    saved_scope=answers.get("__preparation_authorization") or {}
                    if not current_scope or current_scope.get("authorization_id")!=saved_scope.get("authorization_id"):
                        diagnostic.setdefault("authorization",{})["status"]="revoked_or_stale"
                        diagnostic["message"]="Job-specific authorization was revoked or changed. No further candidate information will be entered."
                        diagnostic["unanswered_fields"].append("Review and confirm the current employer-specific authorization.")
                        diagnostic["screenshot"]=_screenshot(page,app_id);_close_browser_safely(page,browser);_update(app_id,"needs_user_input",diagnostic);return
                def mark_upload(): _update(app_id,"uploading_resume",diagnostic)
                filled,unanswered,drafts=_fill_page(page,candidate,answers,resume_path,on_upload=mark_upload)
                diagnostic["filled_fields"].extend(filled); missing.extend(unanswered); diagnostic["draft_answers"].extend(drafts); diagnostic["steps"]=step+1
                missing.extend("Required field is still empty: "+str(label) for label in _required_field_issues(page))
                diagnostic["required_fields"]=list(dict.fromkeys(
                    diagnostic["required_fields"]+_required_visible_fields(page)+
                    [str(item.get("field") or "Required field") for item in filled if item.get("required")]))
                diagnostic["unsupported_fields"]=list(dict.fromkeys(
                    str(item.get("field") or "Application field") for item in filled
                    if item.get("result") in {"unsupported_file_type","upload_unverified","field_limit_exceeded"}))
                diagnostic["human_actions_required"]=list(dict.fromkeys(
                    str(item.get("field") or "Employer action") for item in filled
                    if item.get("result")=="requires_user_confirmation"))
                # Persist progress after each step so status survives a server restart.
                _update(app_id,"filling",diagnostic)
                controls=_inspect_controls(page)
                blocked=submission_controls(controls)
                if blocked: diagnostic["submission_controls"]=[item["label"] for item in blocked]
                ambiguous=[x["label"] for x in blocked if x["action"]=="ambiguous"]
                if ambiguous:
                    missing.append("An application control has unclear behavior and needs your review: "+", ".join(ambiguous))
                    break
                safe=next((x for x in controls if x["action"]=="safe_next"),None)
                # Apply/Submit/Review controls are never clicked. Only explicitly typed button controls
                # with unambiguous next-step wording can advance a multi-step form.
                if safe and step+1<MAX_STEPS:
                    try:
                        before=_step_signature(page)
                        page.locator("button,a[href],input[type=button],input[type=submit]").nth(safe["index"]).click(timeout=1000)
                        page.wait_for_timeout(250)
                        if policy_state:
                            missing.append(policy_state.get("reason") or "The next page was blocked by a read-only or robots safety check.")
                            break
                        if _step_signature(page)==before:
                            missing.append("The application did not advance to its next step using a safe navigation control. Review it manually.")
                            break
                        continue
                    except Exception:
                        missing.append("The next application step could not be opened safely.")
                break
            try:
                validation=page.locator("form").evaluate_all("""forms=>forms.flatMap(f=>Array.from(f.elements).filter(e=>e.willValidate&&!e.checkValidity()&&e.getClientRects().length).map(e=>({field:((e.labels||[])[0]?.innerText||e.name||'Required field').slice(0,160),reason:e.validity.typeMismatch?'invalid format':'required or conditional value missing'})))""")
                diagnostic["validation_errors"]=validation[:50]
                missing.extend("Validation: "+item["field"]+" ("+item["reason"]+")" for item in validation)
            except Exception:
                diagnostic["validation_errors"]=[]
            diagnostic["unanswered_fields"]=list(dict.fromkeys(missing)); diagnostic["screenshot"]=_screenshot(page,app_id)
            diagnostic["authorization_status"]="current" if local_mock else "current_job_specific_authorization"
            diagnostic["documents_uploaded"]=sum(1 for item in diagnostic["filled_fields"]
                if item.get("source") in {"active_resume","private_document"} and item.get("result")=="filled")
            diagnostic["ai_answers_drafted"]=sum(1 for item in diagnostic["draft_answers"] if item.get("status") in {"grounded_draft","ready_to_review"})
            diagnostic["required_fields_remaining"]=len(diagnostic["unanswered_fields"])
            if missing:
                diagnostic["message"]="Required or sensitive questions need your input. The form was not submitted."; state="needs_user_input"
            else:
                diagnostic["message"]="Dry run complete. Review the form; final submission was not performed."; state="ready_for_review"
            _close_browser_safely(page,browser); _update(app_id,state,diagnostic)
    except Exception as error:
        name=type(error).__name__
        phase=diagnostic.get("phase")
        if policy_state:
            diagnostic["failure_code"]=policy_state.get("category") or "navigation_blocked"
            diagnostic["message"]=policy_state.get("reason") or "A navigation was blocked by a safety policy."
            diagnostic["unanswered_fields"]=[diagnostic["message"]]
            _update(app_id,"unsupported",diagnostic,name)
        elif phase=="browser_launch":
            diagnostic["failure_code"]="browser_launch_failed"
            diagnostic["message"]="Chromium could not start. Check the Playwright browser installation and local browser permissions."
        elif "Timeout" in name:
            diagnostic["failure_code"]="navigation_timeout" if phase=="navigation" else "workflow_timeout"
            diagnostic["message"]="The employer page timed out. Check the job link and try again."
        elif phase=="navigation":
            diagnostic["failure_code"]="navigation_failed"
            diagnostic["message"]="The employer page could not be reached. Check the network and job link."
        else:
            diagnostic["failure_code"]="application_workflow_failed"
            diagnostic["message"]="The application page could not be safely completed. Review the saved diagnostics before retrying."
        # Exception strings may contain a signed URL or user-provided field value.
        _update(app_id,"failed",diagnostic,name)


def process_application(app_id,job,resume,candidate,answers):
    with _attempt_lock:
        if app_id in _active_attempts:
            return
        _active_attempts.add(app_id)
    try:
        return _process_application_inner(app_id,job,resume,candidate,answers)
    finally:
        with _attempt_lock:
            _active_attempts.discard(app_id)


def _drain_application_queue():
    global _queue_worker_running
    try:
        while not _paused():
            with connect() as db:
                row=db.execute("SELECT * FROM applications WHERE user_id=? AND state='queued' ORDER BY queue_priority DESC,created_at ASC LIMIT 1",(USER,)).fetchone()
            if not row: break
            try:
                resume,base_candidate,base_answers=_candidate()
                if row["resume_id"]!=resume["id"]:
                    _update(row["id"],"needs_user_input",{"mode":"dry_run","submission_performed":False,"message":"Activate the resume version associated with this queued application before continuing.","unanswered_fields":["The active resume differs from the version selected when this job was queued."]})
                    continue
                candidate=base_candidate
                answers=dict(base_answers)
                job=_get_job(DryRunRequest(job_key=row["job_key"])) if not str(row["job_key"]).startswith("local-mock:") else {"job_key":row["job_key"],"company":row["company"],"title":row["title"],"application_url":row["application_url"]}
                job.update({"job_key":row["job_key"],"company":row["company"],"title":row["title"],"application_url":row["application_url"]})
                posting_blocker=_posting_preparation_blocker(row["job_key"])
                if posting_blocker:
                    _update(row["id"],"needs_user_input",{"mode":"dry_run","submission_performed":False,
                        "message":posting_blocker,"human_actions_required":[posting_blocker],"unanswered_fields":[posting_blocker]})
                    continue
                if not _mock_allowed(row["application_url"]):
                    with connect() as db:
                        saved=db.execute("SELECT 1 FROM discovered_openings WHERE user_id=? AND job_key=?",(USER,row["job_key"])).fetchone()
                        gate= _eligibility_gate(db,row["job_key"],resume["id"]) if saved else ("potential_match",True,False)
                    if gate[0]=="not_eligible" or gate[0]=="not_analyzed" or (gate[0] not in {"potential_match","needs_review"}) or ((gate[0]=="needs_review" or gate[2]) and not gate[1]):
                        _update(row["id"],"needs_user_input",{"mode":"dry_run","submission_performed":False,"message":"Eligibility must be analyzed or reviewed again before preparation can continue.","unanswered_fields":["Review the latest AI eligibility result and any unresolved mandatory requirements."]})
                        continue
                    scope=_current_preparation_authorization(row["job_key"],resume["id"],row["application_url"])
                    if not scope:
                        _update(row["id"],"needs_user_input",{"mode":"dry_run","submission_performed":False,"message":"The employer authorization is missing or stale. Confirm this employer, URL, selected fields, documents, and AI processing again.","unanswered_fields":["Current job-specific authorization required."]})
                        continue
                answers=_application_context(job,answers)
                if not _mock_allowed(row["application_url"]):
                    candidate=_scoped_candidate(candidate,scope)
                    answers=_scoped_answers(answers,scope)
                process_application(row["id"],job,resume,candidate,answers)
            except HTTPException as error:
                _update(row["id"],"needs_user_input",{"mode":"dry_run","submission_performed":False,"message":"Candidate information is needed before this dry run can start.","unanswered_fields":[str(error.detail)[:300]]})
            except Exception as error:
                _update(row["id"],"failed",{"mode":"dry_run","submission_performed":False,"message":"This queued dry run could not start. Other queued applications will continue.","failure_code":"queue_worker_failed"},type(error).__name__)
    finally:
        with _queue_worker_lock: _queue_worker_running=False
    with connect() as db:
        pending=db.execute("SELECT 1 FROM applications WHERE user_id=? AND state='queued' LIMIT 1",(USER,)).fetchone()
    if pending and not _paused(): _schedule_application_queue()


def _schedule_application_queue():
    global _queue_worker_running
    with _queue_worker_lock:
        if _queue_worker_running or _paused(): return False
        _queue_worker_running=True
    try: executor.submit(_drain_application_queue)
    except Exception:
        with _queue_worker_lock: _queue_worker_running=False
        raise
    return True


@router.post("/api/applications/queue",status_code=202)
def enqueue_saved_jobs(request:ApplicationQueueRequest):
    keys=list(dict.fromkeys(str(key)[:250] for key in request.job_keys if str(key).strip()))
    if not keys: raise HTTPException(400,"Select at least one saved opening.")
    if len(keys)>20: raise HTTPException(400,"Queue up to 20 selected openings at a time.")
    if not get_authorization()["settings"].get("allow_automated_form_preparation"):
        raise HTTPException(403,"Enable automated form preparation in Application settings first.")
    resume,_,_=_candidate()
    prepared=[]; skipped=[]
    for key in keys:
        try: job=_get_job(DryRunRequest(job_key=key))
        except HTTPException as error:
            if error.status_code in {404,410}:
                with connect() as db:
                    stale=db.execute("SELECT * FROM discovered_openings WHERE user_id=? AND job_key=?",(USER,key)).fetchone()
                if stale:
                    job=dict(stale);job["application_url"]=job.get("url") or ""
                    _record_application_attention(job,"posting_review",str(error.detail)[:500],eligibility_state="needs_review",form_state="not_checked")
            skipped.append({"job_key":key,"reason":str(error.detail)[:300]}); continue
        try: validate_application_url(job.get("application_url"))
        except ValueError as error: skipped.append({"job_key":key,"reason":str(error)}); continue
        if job.get("reopened_review_required"):
            reason="This requisition closed and reopened. Human review is required before any preparation or reapplication."
            _record_application_attention(job,"posting_review",reason,eligibility_state="needs_review",form_state="not_checked")
            skipped.append({"job_key":key,"reason":reason}); continue
        with connect() as db:
            match=db.execute("SELECT classification,match_score,result_json FROM job_match_results WHERE user_id=? AND job_key=? AND resume_id=? ORDER BY created_at DESC LIMIT 1",(USER,key,resume["id"])).fetchone()
            posting=db.execute("SELECT discovered_at,location_status,reopened_review_required FROM discovered_openings WHERE user_id=? AND job_key=?",(USER,key)).fetchone()
            gate_class,reviewed,unresolved=_eligibility_gate(db,key,resume["id"])
        if gate_class not in {"potential_match","needs_review"} or unresolved or (gate_class=="needs_review" and not reviewed):
            reason="AI matching marked this opening not eligible." if gate_class=="not_eligible" else "You rejected this opening." if gate_class=="rejected" else "You deferred this opening's review." if gate_class=="deferred" else "Run AI matching first." if gate_class=="not_analyzed" else "Resolve mandatory requirements before queueing." if unresolved else "Review this match before queueing."
            if unresolved or (gate_class=="needs_review" and not reviewed):
                _record_application_attention(job,"applicant_information",reason,eligibility_state=gate_class,form_state="not_checked")
            skipped.append({"job_key":key,"reason":reason}); continue
        unmapped_reason=_unmapped_requirements_blocker(key,resume["id"])
        if unmapped_reason:
            _record_application_attention(job,"applicant_information",unmapped_reason,eligibility_state=gate_class,form_state="not_checked")
            skipped.append({"job_key":key,"reason":unmapped_reason}); continue
        if not _mock_allowed(job.get("application_url","")) and not _current_preparation_authorization(key,resume["id"],job.get("application_url")):
            try:granted,reason=_derive_standing_policy_authorization(key,resume["id"],job.get("application_url"))
            except Exception as error:
                skipped.append({"job_key":key,"reason":f"Preparation preflight failed safely ({type(error).__name__}); other openings will continue."})
                continue
            if not granted or not _current_preparation_authorization(key,resume["id"],job.get("application_url")):
                skipped.append({"job_key":key,"reason":reason or "A current job-specific authorization or approved standing policy is required before queueing this real application."}); continue
        score=float(match["match_score"] or 0) if match else 0
        recency=0
        try:
            age=max(0,(datetime.now(timezone.utc)-datetime.fromisoformat(posting["discovered_at"].replace("Z","+00:00"))).total_seconds()/86400)
            recency=max(0,250-int(age*8))
        except (TypeError,ValueError,AttributeError): pass
        location_bonus=250 if posting and posting["location_status"]=="eligible" else 0
        priority=2000+min(500,max(0,int(score*5)))+recency+location_bonus
        try:
            app_id,state,created=_create(job,resume,allow_queue=True,priority=priority)
            if created or state=="queued": prepared.append({"application_id":app_id,"status":state,"created":created})
            else:
                reason=("This canonical ATS opening already has an application or attempt; duplicate processing is blocked."
                        if state=="skipped_duplicate" else "Existing application state: "+state.replace("_"," ")+".")
                skipped.append({"job_key":key,"reason":reason})
        except HTTPException as error: skipped.append({"job_key":key,"reason":str(error.detail)[:300]})
        except Exception as error: skipped.append({"job_key":key,"reason":f"Queue reservation failed safely ({type(error).__name__}); other openings will continue."})
    scheduled=_schedule_application_queue() if prepared else False
    return {"queued":len(prepared),"scheduled":scheduled,"items":prepared,"skipped":skipped,"mode":"dry_run","submission_enabled":False}

@router.post("/api/applications/dry-run")
def start_dry_run(request:DryRunRequest):
    job=_get_job(request); resume,candidate,answers=_candidate()
    is_local=_mock_allowed(job.get("application_url",""))
    with connect() as db:
        saved=db.execute("SELECT reopened_review_required FROM discovered_openings WHERE user_id=? AND job_key=?",(USER,job.get("job_key"))).fetchone()
        if saved:
            if saved["reopened_review_required"]:raise HTTPException(409,"This requisition closed and reopened. Human review is required before any preparation or reapplication.")
            gate_class,reviewed,unresolved=_eligibility_gate(db,job["job_key"],resume["id"])
            if gate_class=="not_eligible": raise HTTPException(409,"AI matching marked this opening not eligible; preparation is blocked.")
            if gate_class=="not_analyzed": raise HTTPException(409,"Run AI matching before preparing this saved opening.")
            if gate_class not in {"potential_match","needs_review"} or unresolved or (gate_class=="needs_review" and not reviewed):
                raise HTTPException(409,"Review the eligibility result and unresolved mandatory requirements before preparing this opening.")
            unmapped_blocker=_unmapped_requirements_blocker(job["job_key"],resume["id"])
            if unmapped_blocker:raise HTTPException(409,unmapped_blocker)
    if not is_local and not get_authorization()["settings"].get("allow_automated_form_preparation"):
        raise HTTPException(403,"Enable automated form preparation in Application settings before preparing an employer application.")
    candidate,answers=_authorized_preparation_payload(job,resume,candidate,answers)
    app_id,state,created=_create(job,resume)
    if state=="skipped_duplicate": return {"application_id":app_id,"status":state,"message":"Already submitted; duplicate prevented."}
    if created: executor.submit(process_application,app_id,job,resume,candidate,answers)
    messages={"queued":"Preparing application. The selected dry run is queued.","opening":"This application is already being prepared.","inspecting":"This application is already finding its application form.","filling":"This application is already filling confirmed information.","uploading_resume":"This application is already uploading the resume.","needs_user_input":"This attempt needs your input. Review its missing questions before retrying.","ready_for_review":"This application is already ready for your review.","paused":"This attempt is paused. Resume it explicitly when ready.","failed":"A previous attempt failed. Review its diagnostics and retry explicitly.","unsupported":"A previous attempt stopped at an unsupported workflow. Review diagnostics before retrying."}
    return {"application_id":app_id,"status":state,"mode":"dry_run","created":created,"message":messages.get(state,"Existing application status: "+state.replace("_"," ")+".")}


@router.post("/api/applications/prepare",status_code=202)
def prepare_saved_job(request:DryRunRequest):
    if not request.job_key or request.job is not None:
        raise HTTPException(400,"Select a saved job before preparing an application.")
    job=_get_job(request)
    try: validate_application_url(job.get("application_url"))
    except ValueError as error: raise HTTPException(400,str(error))
    resume,_,_=_candidate()
    with connect() as db:
        opening=db.execute("SELECT reopened_review_required FROM discovered_openings WHERE user_id=? AND job_key=?",(USER,request.job_key)).fetchone()
        gate_class,reviewed,unresolved=_eligibility_gate(db,request.job_key,resume["id"])
    if opening and opening["reopened_review_required"]:raise HTTPException(409,"This requisition closed and reopened. Human review is required before any preparation or reapplication.")
    if gate_class=="not_eligible": raise HTTPException(409,"AI matching marked this opening not eligible. Preparation is blocked.")
    if gate_class=="not_analyzed": raise HTTPException(409,"Run AI matching before preparing this saved opening.")
    if gate_class not in {"potential_match","needs_review"} or unresolved or (gate_class=="needs_review" and not reviewed):
        raise HTTPException(409,"Review this match and unresolved mandatory requirements before preparing this opening.")
    unmapped_blocker=_unmapped_requirements_blocker(request.job_key,resume["id"])
    if unmapped_blocker:raise HTTPException(409,unmapped_blocker)
    normalized=str(job["application_url"]).split("?",1)[0].rstrip("/").casefold()
    with connect() as db:
        existing=db.execute("SELECT id,state FROM applications WHERE user_id=? AND (job_key=? OR lower(rtrim(application_url,'/'))=?) ORDER BY updated_at DESC LIMIT 1",(USER,request.job_key,normalized)).fetchone()
    if existing:
        state=existing["state"]
        messages={"needs_user_input":"This attempt needs your input. Review the missing questions in Application history, update confirmed answers, then use Retry dry run.","ready_for_review":"This application is already ready for your review. No new browser run was started.","submitted":"This application is already recorded as submitted; duplicate protection blocked another run.","failed":"A previous attempt failed. Review its diagnostics, then use Retry dry run.","paused":"This attempt is paused. Use Retry dry run only when you are ready to continue.","unsupported":"This website workflow was previously unsupported. Review diagnostics before retrying."}
        if state not in {"failed","paused","unsupported"}:
            return {"application_id":existing["id"],"status":state,"mode":"dry_run","created":False,"message":messages.get(state,"This application is already being processed.")}
    return start_dry_run(DryRunRequest(job_key=request.job_key))

@router.post("/api/applications/saved-jobs/{job_key}/review")
def review_saved_job_eligibility(job_key:str,request:EligibilityReviewRequest):
    if not request.reviewed: raise HTTPException(422,"Explicitly review the eligibility result before continuing.")
    resume,_,_=_candidate()
    with connect() as db:
        classification,_,unresolved=_eligibility_gate(db,job_key,resume["id"])
        if classification in {"rejected","deferred"}:
            current=db.execute("SELECT classification,result_json FROM job_match_results WHERE user_id=? AND job_key=? AND resume_id=? ORDER BY created_at DESC LIMIT 1",(USER,job_key,resume["id"])).fetchone()
            classification=current["classification"] if current else classification
            opening=db.execute("SELECT description FROM discovered_openings WHERE user_id=? AND job_key=?",(USER,job_key)).fetchone()
            unresolved=_unresolved_mandatory(current["result_json"] if current else "{}",opening["description"] if opening else "")
        if classification=="not_eligible": raise HTTPException(409,"This opening is marked not eligible and cannot be queued.")
        if classification=="not_analyzed": raise HTTPException(409,"Run AI matching before reviewing this opening.")
        if classification not in {"potential_match","needs_review"}: raise HTTPException(409,"A completed eligibility result is required.")
        if unresolved:raise HTTPException(409,"Unresolved mandatory requirements cannot be overridden by a review. Confirm missing facts and reassess first.")
        if classification=="needs_review":
            current=db.execute("SELECT result_json FROM job_match_results WHERE user_id=? AND job_key=? AND resume_id=? ORDER BY created_at DESC LIMIT 1",(USER,job_key,resume["id"])).fetchone()
            try:review_data=json.loads(current["result_json"] or "{}") if current else {}
            except (TypeError,json.JSONDecodeError):review_data={}
            opening=db.execute("SELECT employment_type FROM discovered_openings WHERE user_id=? AND job_key=?",(USER,job_key)).fetchone()
            if review_data.get("ai_evidence_diagnostics") or (opening and not str(opening["employment_type"] or "").strip()):
                raise HTTPException(409,"This result has specific evidence items to resolve. Use the targeted evidence review so unknown employment type remains explicit.")
        match=db.execute("SELECT updated_at FROM job_match_results WHERE user_id=? AND job_key=? AND resume_id=? ORDER BY created_at DESC LIMIT 1",(USER,job_key,resume["id"])).fetchone()
        review_stamp=now();match_updated_at=str(match["updated_at"] or "")
        db.execute("INSERT INTO application_eligibility_reviews(user_id,job_key,resume_id,classification,reviewed_at,match_updated_at,decision) VALUES(?,?,?,?,?,?,'approved') ON CONFLICT(user_id,job_key,resume_id) DO UPDATE SET classification=excluded.classification,reviewed_at=excluded.reviewed_at,match_updated_at=excluded.match_updated_at,decision='approved'",(USER,job_key,resume["id"],classification,review_stamp,match_updated_at))
    with connect() as db:
        persisted=db.execute("SELECT classification,match_updated_at,reviewed_at,decision FROM application_eligibility_reviews WHERE user_id=? AND job_key=? AND resume_id=?",(USER,job_key,resume["id"])).fetchone()
    if not persisted or persisted["decision"]!="approved" or persisted["match_updated_at"]!=match_updated_at or persisted["reviewed_at"]!=review_stamp:
        raise HTTPException(500,"The review response was received but the saved decision could not be verified. Refresh the dashboard before continuing.")
    return {"reviewed":True,"persisted_verified":True,"classification":classification,"mandatory_requirements_need_review":unresolved,"message":"Review saved and verified for this resume version. It does not guarantee eligibility or authorize preparation."}


@router.post("/api/applications/saved-jobs/{job_key}/deterministic-reassessment")
def deterministic_reassess_saved_job(job_key:str,request:DeterministicReassessmentRequest):
    """Refresh saved deterministic eligibility from confirmed facts without invoking an AI provider."""
    with connect() as db:
        row=db.execute("SELECT status,updated_at,matching_model_version FROM job_match_results WHERE user_id=? AND job_key=? ORDER BY updated_at DESC LIMIT 1",(USER,job_key)).fetchone()
        opening=db.execute("SELECT 1 FROM discovered_openings WHERE user_id=? AND job_key=?",(USER,job_key)).fetchone()
    if not row or row["status"]!="completed":raise HTTPException(409,"A completed saved AI result is required for deterministic reassessment.")
    if str(row["updated_at"] or "")!=request.match_updated_at:
        raise HTTPException(409,"The saved assessment changed. Refresh the job review before reassessing.")
    if not opening:raise HTTPException(404,"Saved job posting not found.")
    import resume_job_matcher as matcher
    configured_provider,configured_model=matcher.current_provider_config()
    if str(row["matching_model_version"] or "").split("/",1)[0]!=f"{configured_provider}:{configured_model}":
        raise HTTPException(409,"The AI provider or model changed. A deterministic refresh cannot make that older model result current.")
    result=matcher.reassess_saved_match_result(job_key)
    return {"job_key":job_key,"status":"completed","classification":result["classification"],
        "matching_engine_version":result["matching_model_version"],"ai_provider_called":False,
        "message":"Saved evidence was reassessed deterministically. No AI matching call or application preparation was started."}


@router.post("/api/applications/saved-jobs/{job_key}/resolve-evidence-review")
def resolve_saved_job_evidence_review(job_key:str,request:TargetedEligibilityReviewRequest):
    """Record a scoped applicant decision for non-mandatory unresolved evidence."""
    import resume_job_matcher as matcher
    resume_id,profile_status,profile=matcher.active_candidate()
    prefs=matcher.preferences() or {}
    with connect() as db:
        row=db.execute("SELECT * FROM job_match_results WHERE user_id=? AND job_key=? AND resume_id=?",(USER,job_key,resume_id)).fetchone()
        opening=db.execute("SELECT * FROM discovered_openings WHERE user_id=? AND job_key=?",(USER,job_key)).fetchone()
    if not row or row["status"]!="completed" or not row["result_json"]:
        raise HTTPException(409,"A completed matching result is required before resolving evidence review.")
    if not opening:raise HTTPException(404,"Saved job posting not found.")
    opening=dict(opening)
    current_version=matcher.matching_engine_version()
    current_profile_fingerprint=matcher.profile_fingerprint(profile,resume_id)
    current_preferences_fingerprint=matcher.stable_fingerprint(prefs)
    current_job_fingerprint=matcher.job_fingerprint(opening)
    expected={"match_updated_at":str(row["updated_at"] or ""),"profile_fingerprint":current_profile_fingerprint,
        "preferences_fingerprint":current_preferences_fingerprint,"job_fingerprint":current_job_fingerprint,
        "matching_engine_version":current_version}
    supplied=request.model_dump()
    if (row["freshness_status"]!="current" or row["matching_model_version"]!=current_version or
        row["candidate_fingerprint"]!=current_profile_fingerprint or
        row["preferences_fingerprint"]!=current_preferences_fingerprint or
        row["job_fingerprint"]!=current_job_fingerprint or any(
        supplied.get(key)!=value for key,value in expected.items())):
        raise HTTPException(409,"This review is stale because the job, confirmed profile, preferences, or matching rules changed. Reassess the saved evidence first.")
    if row["classification"]=="not_eligible":
        raise HTTPException(409,"A confirmed mandatory mismatch cannot be overridden by an applicant review.")
    from resume_job_matcher import MatchResult,apply_safety_rules
    try:original=MatchResult.model_validate_json(row["result_json"])
    except Exception as error:raise HTTPException(409,"The saved result is invalid and must be reassessed.") from error
    checked=apply_safety_rules(original,profile,profile_status,prefs,opening)
    if checked.classification=="not_eligible":
        raise HTTPException(409,"Verified evidence confirms a mandatory mismatch; review cannot override it.")
    if _unresolved_mandatory(checked.model_dump_json(),opening.get("description") or ""):
        raise HTTPException(409,"A mandatory fact remains unresolved. Confirm it in Candidate Profile, then use deterministic reassessment.")
    qualitative_entries=[item for item in checked.technical_qualifications if _is_qualitative_employer_expectation(item.requirement)]
    qualitative_terms=[item.requirement.casefold() for item in qualitative_entries]
    qualitative_terms.extend(item.missing_question.casefold() for item in qualitative_entries if item.missing_question)
    def is_qualitative_review_text(value):
        text=str(value or "").casefold()
        return _is_qualitative_employer_expectation(text) or any(term and term in text for term in qualitative_terms)
    checked.gaps=[value for value in checked.gaps if not is_qualitative_review_text(value)]
    checked.missing_information=[value for value in checked.missing_information if not is_qualitative_review_text(value)]
    unmapped_required=_unmapped_required_posting_items(opening.get("description") or "",checked.model_dump())
    if unmapped_required:
        raise HTTPException(409,"The saved assessment did not evaluate every explicit mandatory qualification. Reassess the posting and candidate evidence before reviewing it.")
    unknown_employment=not str(opening.get("employment_type") or "").strip()
    if unknown_employment and not request.accept_unspecified_employment_type:
        raise HTTPException(422,"Confirm whether you want to consider this job while its employment type remains unknown.")
    role_level_unknown=_review_experience_level(opening,checked.model_dump())=="Ambiguous" and any(
        re.search(r"experience level.{0,60}(?:not clear|unclear)",str(item or ""),re.I)
        for item in checked.missing_information)
    with connect() as source_db:source_verified=matcher._verified_saved_employer_source(source_db,opening)
    if role_level_unknown and (opening.get("availability_status")!="current" or not opening.get("active") or
        not opening.get("last_verified_at") or not source_verified):
        raise HTTPException(409,"Role-seniority acknowledgment requires a currently verified posting and healthy official employer source.")
    if request.accept_unclear_role_seniority and not role_level_unknown:
        raise HTTPException(422,"This posting has no unresolved role-seniority uncertainty to acknowledge.")
    if role_level_unknown and not request.accept_unclear_role_seniority:
        raise HTTPException(422,"The posting does not state a clear role level. Confirm that you want to consider the job with seniority still unknown.")
    if checked.ai_evidence_diagnostics and not request.acknowledge_ai_diagnostics:
        raise HTTPException(422,"Review the AI-authored diagnostics and confirm they do not establish a mandatory conflict.")
    known_employment_notice=re.compile(r"employment type.{0,90}(?:not provided|not specified|unknown)",re.I)
    unknown_seniority_notice=re.compile(r"experience level.{0,60}(?:not clear|unclear)",re.I)
    unresolved=[item for item in checked.missing_information if not (
        (unknown_employment and known_employment_notice.search(str(item))) or
        (role_level_unknown and request.accept_unclear_role_seniority and unknown_seniority_notice.search(str(item))))]
    if unresolved or checked.gaps:
        raise HTTPException(409,"Other unresolved evidence remains. Review the deterministic assessment before proceeding.")
    decisions=[]
    if unknown_employment:decisions.append("Applicant accepted consideration with employment type still unknown for this job.")
    if role_level_unknown:decisions.append("Applicant accepted consideration with role seniority still unknown for this job.")
    if checked.ai_evidence_diagnostics:decisions.append("Applicant acknowledged AI-authored diagnostic statements as non-authoritative.")
    checked.missing_information=[]
    checked.classification="potential_match"
    checked.applicant_review_resolutions=decisions
    uncertainty_text=[]
    if unknown_employment:uncertainty_text.append("employment type remains unknown")
    if role_level_unknown:uncertainty_text.append("role seniority remains unknown")
    checked.explanation=("Deterministic mandatory requirements are met. The applicant reviewed the remaining uncertainty; " +
        (", ".join(uncertainty_text)+". " if uncertainty_text else "")+
        "AI-authored diagnostics are not qualification evidence.")[:2000]
    stamp=now()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current=db.execute("SELECT * FROM job_match_results WHERE user_id=? AND job_key=? AND resume_id=?",(USER,job_key,resume_id)).fetchone()
        if not current or str(current["updated_at"] or "")!=request.match_updated_at or current["matching_model_version"]!=current_version or current["freshness_status"]!="current":
            raise HTTPException(409,"The saved assessment changed while you were reviewing it. Refresh and try again.")
        matcher._archive_match_result(db,current,"applicant_evidence_review_resolution",stamp)
        db.execute("""UPDATE job_match_results SET classification=?,result_json=?,candidate_fingerprint=?,
            preferences_fingerprint=?,job_fingerprint=?,matching_model_version=?,freshness_status='current',updated_at=?
            WHERE user_id=? AND job_key=? AND resume_id=?""",
            (checked.classification,checked.model_dump_json(),current_profile_fingerprint,current_preferences_fingerprint,
             current_job_fingerprint,current_version,stamp,USER,job_key,resume_id))
        db.execute("""INSERT INTO eligibility_evidence_reviews(user_id,job_key,resume_id,decision,match_updated_at,
            profile_fingerprint,preferences_fingerprint,job_fingerprint,matching_engine_version,details_json,reviewed_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (USER,job_key,resume_id,"accepted_non_mandatory_uncertainty",stamp,current_profile_fingerprint,
             current_preferences_fingerprint,current_job_fingerprint,current_version,
             json.dumps({"accepted_unspecified_employment_type":unknown_employment,
                         "accepted_unclear_role_seniority":role_level_unknown,
                         "acknowledged_ai_diagnostics":bool(checked.ai_evidence_diagnostics)},separators=(",",":")),stamp))
    with connect() as db:
        saved=db.execute("SELECT classification,result_json FROM job_match_results WHERE user_id=? AND job_key=? AND resume_id=?",
            (USER,job_key,resume_id)).fetchone()
        saved_review=db.execute("SELECT decision,reviewed_at FROM eligibility_evidence_reviews WHERE user_id=? AND job_key=? AND resume_id=? ORDER BY review_id DESC LIMIT 1",
            (USER,job_key,resume_id)).fetchone()
    if not saved or saved["classification"]!="potential_match" or not saved_review or saved_review["reviewed_at"]!=stamp:
        raise HTTPException(500,"The review was accepted but could not be verified in saved records. Check the dashboard before proceeding.")
    return {"reviewed":True,"classification":"potential_match","employment_type":"unknown" if unknown_employment else opening.get("employment_type"),
        "role_seniority":"unknown" if role_level_unknown else _review_experience_level(opening,checked.model_dump()),
        "matching_engine_version":current_version,"reviewed_at":stamp,"persisted_verified":True,"application_preparation_authorized":False,
        "message":"Evidence review saved. Employment type remains unknown. Separate job-specific preparation authorization is still required."}


def record_eligibility_decision(job_key:str,decision:str):
    if decision not in {"rejected","deferred"}:raise HTTPException(422,"Unknown eligibility decision.")
    resume,_,_=_candidate()
    with connect() as db:
        classification,_,_=_eligibility_gate(db,job_key,resume["id"])
        if classification in {"rejected","deferred"}:
            current=db.execute("SELECT classification FROM job_match_results WHERE user_id=? AND job_key=? AND resume_id=? ORDER BY created_at DESC LIMIT 1",(USER,job_key,resume["id"])).fetchone()
            classification=current["classification"] if current else classification
        if classification not in {"potential_match","needs_review"}:raise HTTPException(409,"A fresh, eligible or reviewable result is required.")
        match=db.execute("SELECT updated_at FROM job_match_results WHERE user_id=? AND job_key=? AND resume_id=? ORDER BY created_at DESC LIMIT 1",(USER,job_key,resume["id"])).fetchone()
        db.execute("INSERT INTO application_eligibility_reviews(user_id,job_key,resume_id,classification,reviewed_at,match_updated_at,decision) VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,job_key,resume_id) DO UPDATE SET classification=excluded.classification,reviewed_at=excluded.reviewed_at,match_updated_at=excluded.match_updated_at,decision=excluded.decision",(USER,job_key,resume["id"],classification,now(),str(match["updated_at"] or ""),decision))
    return {"decision":decision,"classification":classification,"submission_enabled":False}

@router.post("/api/applications/{application_id}/answers")
def save_missing_application_answer(application_id:str,request:MissingAnswerRequest):
    question=re.sub(r"\s+"," ",request.question.strip())[:300]
    if not question or not request.answer.strip(): raise HTTPException(422,"Enter an answer before saving.")
    if not request.confirmed: raise HTTPException(422,"Confirm that this answer is accurate before saving it.")
    with connect() as db:
        app=db.execute("SELECT state,diagnostic_json,resume_id,job_key FROM applications WHERE id=? AND user_id=?",(application_id,USER)).fetchone()
    if not app: raise HTTPException(404,"Application not found.")
    if app["state"]!="needs_user_input": raise HTTPException(409,"This application is not waiting for answers.")
    try: diagnostic=json.loads(app["diagnostic_json"] or "{}")
    except (TypeError,json.JSONDecodeError): diagnostic={}
    available={re.sub(r"\s+"," ",str(x).strip()).casefold() for x in diagnostic.get("unanswered_fields",[]) if isinstance(x,str)}
    if question.casefold() not in available: raise HTTPException(409,"That question is no longer pending. Refresh the application status.")
    resume,candidate,_=_candidate()
    if resume["id"]!=app["resume_id"]: raise HTTPException(409,"Activate the resume version associated with this attempt before saving answers.")
    sensitive=_sensitive({"label":question}) or _requires_human_confirmation({"label":question})
    key="application-question-"+hashlib.sha256(question.casefold().encode()).hexdigest()[:24]
    reuse_scope=request.reuse_scope or ("matching_jobs" if request.reusable else "none")
    if sensitive and reuse_scope in {"matching_jobs","general_preference"}:raise HTTPException(422,"Sensitive or legal answers cannot be reused across jobs.")
    if sensitive:reuse_scope="job_specific"
    if reuse_scope=="job_specific":scope_job_key=app["job_key"]
    else:scope_job_key=""
    answer_payload=AnswerUpdate(question=question,answer=request.answer[:4000],sensitive=sensitive,confirmed=True,
        answer_type="sensitive" if sensitive else "factual",auto_reuse=reuse_scope!="none",
        reuse_scope=reuse_scope,scope_job_key=scope_job_key)
    save_application_answer(key,answer_payload)
    return {"saved":True,"reusable":bool(reuse_scope!="none" and not sensitive),"reuse_scope":reuse_scope,
        "requires_manual_action":_requires_human_confirmation({"label":question}),"message":"Confirmed answer saved. Resume the dry run when ready."}


@router.get("/api/application-questions/attention")
def grouped_application_questions():
    with connect() as db:
        rows=db.execute("SELECT id,job_key,company,title,diagnostic_json FROM applications WHERE user_id=? AND state='needs_user_input' ORDER BY updated_at DESC",(USER,)).fetchall()
    groups={}
    for row in rows:
        try:diagnostic=json.loads(row["diagnostic_json"] or "{}")
        except (TypeError,json.JSONDecodeError):diagnostic={}
        seen=set()
        for raw in diagnostic.get("unanswered_fields",[]) if isinstance(diagnostic.get("unanswered_fields"),list) else []:
            exact=str(raw or "").strip()[:500];question=_display_question(exact)
            category,context,sensitive=canonical_answer_identity(question)
            sensitive=bool(sensitive or _sensitive({"label":question}) or _requires_human_confirmation({"label":question}))
            identity=(category,context)
            if not question or identity in seen:continue
            seen.add(identity)
            group=groups.setdefault(identity,{"canonical_category":category,"context":context,"question":question,
                "sensitive":sensitive,"jobs":[]})
            group["jobs"].append({"application_id":row["id"],"job_key":row["job_key"],
                "company":row["company"],"title":row["title"],"question":question,
                "diagnostic_question":exact,
                "why_required":"The saved dry-run validation marked this control as unresolved; verify the exact employer wording before answering."})
    result=sorted(groups.values(),key=lambda item:(item["canonical_category"],item["context"],item["question"].casefold()))
    try: active,candidate,answer_bank=_candidate()
    except HTTPException: active,candidate,answer_bank=None,{},{}
    for group in result:
        identity=(group["canonical_category"],group["context"])
        existing=[];invalid_saved=[]
        for key,item in answer_bank.items():
            if not isinstance(item,dict) or not item.get("confirmed") or item.get("revoked_at"):continue
            if canonical_answer_identity(item.get("question",""))[:2]!=identity:continue
            try:validate_application_answer(item.get("question",""),item.get("answer",""))
            except ValueError as error:
                invalid_saved.append({"key":key,"question":item.get("question",""),"reuse_scope":item.get("reuse_scope") or "none",
                    "scope_job_key":item.get("scope_job_key") or "","validation_issue":str(error)})
                continue
            existing.append({"key":key,"answer":str(item.get("answer") or ""),"sensitive":bool(item.get("sensitive")),
                "reuse_scope":item.get("reuse_scope") or "none","scope_job_key":item.get("scope_job_key") or ""})
        values={item["answer"] for item in existing if item["answer"]}
        conflict=len(values)>1
        profile_value=""
        if not group["sensitive"] and not conflict and active and not values:
            value,source=_answer({"label":group["question"]},candidate,answer_bank)
            if value and source not in {"__RESUME__"}:profile_value=str(value)[:1200]
        group["answer_conflict"]=conflict or bool(invalid_saved)
        group["requires_correction"]=bool(invalid_saved)
        group["invalid_saved_answers"]=invalid_saved
        group["existing_answer_available"]=bool(existing or profile_value)
        group["existing_answer_scope"]=(existing[0]["reuse_scope"] if len({item["reuse_scope"] for item in existing})==1 else "multiple" if existing else "")
        group["existing_answers"]=[{"answer":"" if item["sensitive"] else item["answer"][:1200],
            "reuse_scope":item["reuse_scope"],"scope_job_key":item["scope_job_key"],"sensitive":item["sensitive"]} for item in existing]
        group["existing_answer"]="";group["existing_answer_sensitive"]=bool(existing and all(item["sensitive"] for item in existing))
        if not group["sensitive"] and not conflict:
            if len(values)==1:group["existing_answer"]=next(iter(values))[:1200]
            elif profile_value:
                group["existing_answer"]=profile_value
                group["existing_answers"].append({"answer":profile_value,"reuse_scope":"confirmed_profile","scope_job_key":"","sensitive":False})
        group["manual_employer_action"]=group["canonical_category"] in {"employer_confirmation","legal_attestation","human_verification"}
        group["sensitivity_label"]=("Employer-specific confirmation — handle on the employer form" if group["manual_employer_action"]
            else "Sensitive — never automatically reused" if group["sensitive"] else "Ordinary answer")
    technical_groups={}
    eligibility_groups={}
    try:
        from resume_job_matcher import matching_engine_version
        engine=matching_engine_version()
        with connect() as db:
            rows=db.execute("""SELECT m.job_key,m.resume_id,m.updated_at AS match_updated_at,m.matching_model_version,m.freshness_status,
                m.result_json,o.company,o.title,o.description,o.country,o.availability_status,o.last_verified_at FROM job_match_results m JOIN discovered_openings o
                ON o.user_id=m.user_id AND o.job_key=m.job_key WHERE m.user_id=? AND m.resume_id=? AND m.status='completed'""",
                (USER,active["id"] if active else "")).fetchall()
        for row in rows:
            if row["freshness_status"]!="current" or row["matching_model_version"]!=engine:continue
            try:match=json.loads(row["result_json"] or "{}")
            except (TypeError,json.JSONDecodeError):continue
            for qualification in match.get("technical_qualifications",[]) if isinstance(match.get("technical_qualifications"),list) else []:
                question=str(qualification.get("missing_question") or "").strip()
                if (qualification.get("requiredness")!="required" or qualification.get("status")!="needs_review" or
                    _is_qualitative_employer_expectation(qualification.get("requirement")) or not question):continue
                category,context,sensitive=canonical_answer_identity(question)
                if category!="technical_qualification":continue
                identity=(category,context)
                group=technical_groups.setdefault(identity,{"canonical_category":category,"context":context,"question":question,
                    "sensitive":False,"technical":True,"technical_category":qualification.get("category"),"jobs":[],
                    "candidate_evidence":[],"missing_reason":qualification.get("explanation") or "Verified evidence is incomplete."})
                group["jobs"].append({"application_id":"","job_key":row["job_key"],"company":row["company"],"title":row["title"],
                    "question":question,"why_required":qualification.get("explanation") or "This is a mandatory posting requirement."})
                for evidence in qualification.get("candidate_evidence",[]):
                    if evidence not in group["candidate_evidence"]:group["candidate_evidence"].append(evidence)
            availability=match.get("availability_assessment") if isinstance(match.get("availability_assessment"),dict) else {}
            if row["availability_status"]=="current" and availability.get("status")=="needs_review" and availability.get("missing_question"):
                question=str(availability["missing_question"]).strip()
                identity=("start_availability", "earliest_available_date")
                group=eligibility_groups.setdefault(identity,{"canonical_category":"availability","context":"earliest_available_date",
                    "question":question,"expected_answer_type":"specific earliest full-time start date or confirmed range",
                    "reuse_scope":"profile_confirmation","eligibility_effect":"This compares earliest employment availability with the employer's start window. Graduation date and notice period are separate facts.",
                    "why_required":"The official posting states a winter 2026 through summer 2027 start window.","jobs":[]})
                group["jobs"].append({"job_key":row["job_key"],"company":row["company"],"title":row["title"],
                    "requirement":str(availability.get("requirement") or "Employer start window"),"posting_verified_at":row["last_verified_at"] or ""})
            if row["availability_status"]!="current":continue
            for requirement in match.get("required_experience",[]) if isinstance(match.get("required_experience"),list) else []:
                if not isinstance(requirement,dict) or requirement.get("status") not in {"unclear","gap"}:continue
                text=str(requirement.get("requirement") or "").strip()
                if not re.search(r"\b\d+\+?\s+years?\b",text,re.I):continue
                question=_experience_requirement_question(text,row["description"] or "")
                context=re.sub(r"[^a-z0-9]+"," ",question.casefold()).strip()
                identity=("professional_experience_evidence",context)
                group=eligibility_groups.setdefault(identity,{"canonical_category":"professional_experience_evidence",
                    "context":context,"question":question,"expected_answer_type":"confirmed dated employment history",
                    "reuse_scope":"profile_confirmation","eligibility_effect":"Only confirmed employer, role, and date records can resolve this requirement; a yes/no response is not sufficient.",
                    "why_required":"The official saved posting states a mandatory professional experience threshold.","jobs":[]})
                group["jobs"].append({"job_key":row["job_key"],"company":row["company"],"title":row["title"],
                    "requirement":text,"posting_verified_at":row["last_verified_at"] or ""})
            for missing_fact in match.get("missing_information",[]) if isinstance(match.get("missing_information"),list) else []:
                authorization=re.match(r"Work authorization for (.+?) is not confirmed",str(missing_fact or ""),re.I)
                if not authorization:continue
                country=authorization.group(1).strip()
                country_identity=normalize_text(country)
                identity=("work_authorization_country",country_identity)
                question=(f"Do you have current authorization to work in {country}, or would you require employer sponsorship? "
                    "Relocation willingness is not work authorization.")
                group=eligibility_groups.setdefault(identity,{"canonical_category":"work_authorization_country",
                    "context":country_identity,"question":question,"expected_answer_type":"sensitive country-specific legal fact",
                    "reuse_scope":"country_specific_profile_confirmation","sensitive":True,
                    "eligibility_effect":"A confirmed country-specific fact can resolve only this work-authorization question; sponsorship support may remain unknown.",
                    "why_required":"The verified posting location is in this country. Relocation does not establish legal work authorization.","jobs":[]})
                group["jobs"].append({"job_key":row["job_key"],"company":row["company"],"title":row["title"],
                    "requirement":f"Work authorization or sponsorship in {country}","posting_verified_at":row["last_verified_at"] or ""})
        for group in technical_groups.values():
            answers=[]
            invalid_answers=[]
            for answer in answer_bank.values() if isinstance(answer_bank,dict) else []:
                if not isinstance(answer,dict) or not answer.get("confirmed") or answer.get("revoked_at"):continue
                if canonical_answer_identity(answer.get("question",""))[:2]!=(group["canonical_category"],group["context"]):continue
                try:validate_application_answer(answer.get("question",""),answer.get("answer",""))
                except ValueError as error:
                    invalid_answers.append({"question":answer.get("question",""),"validation_issue":str(error),
                        "reuse_scope":answer.get("reuse_scope") or "none"})
                    continue
                answers.append({"answer":str(answer.get("answer") or ""),"reuse_scope":answer.get("reuse_scope") or "none",
                    "scope_job_key":answer.get("scope_job_key") or "","sensitive":False})
            group["existing_answers"]=answers
            group["invalid_saved_answers"]=invalid_answers
            group["requires_correction"]=bool(invalid_answers)
            group["answer_conflict"]=len({item["answer"] for item in answers if item["answer"]})>1 or bool(invalid_answers)
            group["existing_answer"]=answers[-1]["answer"] if answers and not group["answer_conflict"] else ""
    except (HTTPException,sqlite3.OperationalError):
        technical_groups={}
    return {"groups":result,"technical_groups":sorted(technical_groups.values(),key=lambda item:(item["technical_category"] or "",item["question"].casefold())),
        "eligibility_groups":sorted(eligibility_groups.values(),key=lambda item:(item["canonical_category"],item["question"].casefold())),
        "job_count":sum(len(item["jobs"]) for item in result)}


def _grouped_answer_type(question):
    return application_answer_value_kind(question)


def _validate_grouped_answer(question,answer):
    try:return validate_application_answer(question,answer)
    except ValueError as error:raise HTTPException(422,str(error)) from error


@router.post("/api/application-questions/answer")
def save_grouped_application_answer(request:GroupedAnswerRequest):
    question=re.sub(r"\s+"," ",request.question.strip())[:500]
    answer=_validate_grouped_answer(question,request.answer)
    if not request.confirmed:raise HTTPException(422,"Confirm that the answer is accurate before saving it.")
    category,context,sensitive=canonical_answer_identity(question)
    sensitive=bool(sensitive or _sensitive({"label":question}) or _requires_human_confirmation({"label":question}))
    if category in {"employer_confirmation","legal_attestation","human_verification"}:
        raise HTTPException(422,"This is an employer-specific acknowledgment or verification step. Review and complete it directly on the employer form; it cannot be saved or reused as a generic answer.")
    if request.reuse_scope not in {"this_job","matching_jobs","general_preference"}:
        raise HTTPException(422,"Choose how this confirmed answer may be reused.")
    if category=="relocation" and request.reuse_scope!="this_job":
        raise HTTPException(422,"Set relocation anywhere, selected destinations, no relocation, or decide per job in Career Preferences. A job-specific answer cannot become a general relocation preference.")
    if sensitive and request.reuse_scope!="this_job":
        raise HTTPException(422,"Sensitive, legal, and human-verification answers can only be saved for this job and are never automatically reused.")
    ids=list(dict.fromkeys(request.application_ids))
    if not ids:raise HTTPException(422,"Select at least one affected saved application.")
    marks=",".join("?" for _ in ids)
    with connect() as db:
        apps=db.execute(f"SELECT id,job_key,company,title,state,diagnostic_json,resume_id FROM applications WHERE user_id=? AND id IN ({marks})",
                         (USER,*ids)).fetchall()
    if len(apps)!=len(ids) or any(row["state"]!="needs_user_input" for row in apps):
        raise HTTPException(409,"One or more selected applications no longer need answers. Refresh this list.")
    matching=[]
    for row in apps:
        try:diagnostic=json.loads(row["diagnostic_json"] or "{}")
        except (TypeError,json.JSONDecodeError):diagnostic={}
        found=False
        for raw in diagnostic.get("unanswered_fields",[]) if isinstance(diagnostic.get("unanswered_fields"),list) else []:
            if canonical_answer_identity(_display_question(str(raw)) )[:2]==(category,context):found=True;break
        if found:matching.append(row)
    if len(matching)!=len(apps):raise HTTPException(409,"The selected jobs do not share this canonical question. Refresh and answer each distinct question separately.")
    if request.reuse_scope=="this_job":
        target=next((row for row in matching if row["job_key"]==request.job_key),None)
        if not target:raise HTTPException(422,"Choose one of the listed jobs for job-only reuse.")
        active,_,answer_bank=_candidate()
        if active["id"]!=target["resume_id"]:
            raise HTTPException(409,"Activate the resume version associated with this application before saving its answer.")
        reuse_scope="job_specific"
        scope_job=target["job_key"]
        resume_id=target["resume_id"]
        affected=[target]
    else:
        reuse_scope="matching_jobs" if request.reuse_scope=="matching_jobs" else "general_preference"
        scope_job=""
        resume_id=matching[0]["resume_id"]
        active,_,answer_bank=_candidate()
        if active["id"]!=resume_id or any(row["resume_id"]!=resume_id for row in matching):
            raise HTTPException(409,"The grouped jobs use different resume versions. Answer them separately.")
        affected=matching
    if category=="relocation":raise HTTPException(422,"Use structured Career Preferences for relocation decisions.")
    prior_values=set()
    for item in answer_bank.values():
        if isinstance(item,dict) and item.get("confirmed") and not item.get("revoked_at") and canonical_answer_identity(item.get("question",""))[:2]==(category,context):
            old_value=str(item.get("answer") or "").strip()
            if old_value and old_value!=answer:prior_values.add(old_value)
    if prior_values and not request.acknowledge_existing_conflict:
        raise HTTPException(409,"A different confirmed answer exists. Review the displayed conflict and explicitly acknowledge it before saving this scoped answer.")
    key=stable_answer_key(category,context,reuse_scope,scope_job)
    saved=save_application_answer(key,AnswerUpdate(question=question,answer=answer,confirmed=True,
        sensitive=sensitive,answer_type="sensitive" if sensitive else ("preference" if request.reuse_scope=="general_preference" else "factual"),
        auto_reuse=reuse_scope!="none",reuse_scope=reuse_scope,scope_job_key=scope_job))
    if not saved.get("persisted_verified"):
        raise HTTPException(500,"The confirmed answer was not verified after saving. Refresh before continuing.")
    with connect() as db:
        stored=db.execute("SELECT * FROM application_answers WHERE user_id=? AND resume_id=? AND question_key=?",
            (USER,resume_id,saved.get("key") or "custom:"+key)).fetchone()
        stored_answer=_answer_plaintext(stored,db) if stored else ""
    expected_scope_job=scope_job if reuse_scope=="job_specific" else ""
    if not stored or stored_answer!=answer or stored["reuse_scope"]!=reuse_scope or stored["scope_job_key"]!=expected_scope_job or not stored["confirmed"]:
        raise HTTPException(500,"The saved answer or reuse scope could not be verified. Refresh before continuing.")
    return {"saved":True,"persisted_verified":True,"canonical_category":category,"reuse_scope":reuse_scope,
        "conflict_acknowledged":bool(prior_values and request.acknowledge_existing_conflict),
        "affected_jobs":[{"job_key":row["job_key"],"company":row["company"],"title":row["title"]} for row in affected],
        "requires_manual_action":sensitive or category in {"legal_attestation","human_verification"},
        "message":("Sensitive answer stored encrypted for this job. It will not be filled automatically; provide it yourself if the employer asks." if sensitive else "Answer saved. No application was resumed or prepared; return to each dry run when ready.")}

@router.post("/api/application-questions/technical-evidence")
def save_technical_evidence_answer(request:TechnicalEvidenceAnswerRequest):
    question=re.sub(r"\s+"," ",request.question.strip())
    answer=re.sub(r"\s+"," ",request.answer.strip())
    category,context,sensitive=canonical_answer_identity(question)
    if category!="technical_qualification" or sensitive:
        raise HTTPException(422,"Choose a current technical qualification question from the dashboard.")
    if len(answer)<30 or answer.casefold() in {"yes","no","i have","i do","true","false"}:
        raise HTTPException(422,"Provide a specific, confirmed description of the project, coursework, or implementation; a yes/no answer is not evidence.")
    if not re.search(r"\b(?:project|coursework|course|class|implemented|built|developed|designed|created|algorithm|data structures?|software design|frontend|backend|web application|api|service)\b",answer,re.I):
        raise HTTPException(422,"Add a concrete project, course, or implementation detail so the qualification can be assessed as evidence.")
    if not request.confirmed:raise HTTPException(422,"Confirm that this description is accurate before saving it.")
    if request.reuse_scope not in {"this_job","matching_jobs","general_preference"}:
        raise HTTPException(422,"Choose how this technical fact may be reused.")
    selected=list(dict.fromkeys(str(value) for value in request.job_keys if str(value).strip()))
    if not selected:raise HTTPException(422,"Select at least one affected saved job.")
    active,_,answer_bank=_candidate()
    if request.reuse_scope=="this_job":
        target=request.job_key
        if target not in selected:raise HTTPException(422,"Choose one of the listed jobs for job-only reuse.")
        selected=[target]
        reuse_scope="job_specific"
        scope_job=target
    else:
        reuse_scope="matching_jobs" if request.reuse_scope=="matching_jobs" else "general_preference"
        scope_job=""
    from resume_job_matcher import matching_engine_version
    current_engine=matching_engine_version()
    matched=[]
    with connect() as db:
        for key in selected:
            row=db.execute("""SELECT m.status,m.freshness_status,m.matching_model_version,m.result_json,o.company,o.title
                FROM job_match_results m JOIN discovered_openings o ON o.user_id=m.user_id AND o.job_key=m.job_key
                WHERE m.user_id=? AND m.job_key=? AND m.resume_id=?""",(USER,key,active["id"])).fetchone()
            if not row or row["status"]!="completed" or row["freshness_status"]!="current" or row["matching_model_version"]!=current_engine:
                raise HTTPException(409,"A selected job's technical assessment is stale. Reassess saved evidence before answering.")
            try:result=json.loads(row["result_json"] or "{}")
            except (TypeError,json.JSONDecodeError):result={}
            technical_items=result.get("technical_qualifications",[])
            if not isinstance(technical_items,list):technical_items=[]
            qualifies=any(isinstance(item,dict) and item.get("requiredness")=="required" and
                item.get("status")=="needs_review" and canonical_answer_identity(item.get("missing_question",""))[:2]==(category,context)
                for item in technical_items)
            if not qualifies:raise HTTPException(409,"A selected job no longer has this unresolved technical question. Refresh the dashboard.")
            matched.append({"job_key":key,"company":row["company"],"title":row["title"]})
    if request.reuse_scope=="matching_jobs" and len(matched)!=len(selected):
        raise HTTPException(409,"The selected jobs do not share this unresolved technical question.")
    conflicts=set()
    for item in answer_bank.values() if isinstance(answer_bank,dict) else []:
        if not isinstance(item,dict) or not item.get("confirmed") or item.get("revoked_at"):continue
        if canonical_answer_identity(item.get("question",""))[:2]!=(category,context):continue
        old=str(item.get("answer") or "").strip()
        if old and old!=answer:conflicts.add(old)
    if conflicts and not request.acknowledge_existing_conflict:
        raise HTTPException(409,"A different confirmed technical answer already exists. Review the conflict and explicitly acknowledge it before saving this scoped answer.")
    key=stable_answer_key(category,context,reuse_scope,scope_job)
    saved=save_application_answer(key,AnswerUpdate(question=question,answer=answer,confirmed=True,sensitive=False,
        answer_type="factual",auto_reuse=True,reuse_scope=reuse_scope,scope_job_key=scope_job))
    if not saved.get("persisted_verified"):
        raise HTTPException(500,"The technical answer was not verified after saving. Refresh before continuing.")
    with connect() as db:
        stored=db.execute("SELECT * FROM application_answers WHERE user_id=? AND resume_id=? AND question_key=?",
            (USER,active["id"],saved.get("key") or "custom:"+key)).fetchone()
        stored_answer=_answer_plaintext(stored,db) if stored else ""
    if (not stored or stored_answer!=answer or not stored["confirmed"] or stored["reuse_scope"]!=reuse_scope or
        stored["scope_job_key"]!=(scope_job if reuse_scope=="job_specific" else "")):
        raise HTTPException(500,"The saved technical fact or reuse scope could not be verified. Refresh before continuing.")
    reassessed=[]
    import resume_job_matcher as matcher
    for item in matched:
        try:
            result=matcher.reassess_saved_match_result(item["job_key"])
            match_data=result.get("match",{}) if isinstance(result,dict) else {}
            used=False
            for qualification in match_data.get("technical_qualifications",[]) if isinstance(match_data.get("technical_qualifications"),list) else []:
                evidence_rows=qualification.get("candidate_evidence",[]) if isinstance(qualification,dict) else []
                if isinstance(evidence_rows,list) and any(isinstance(evidence,dict) and
                    evidence.get("source_ref")=="application_answers:"+str(saved.get("key") or "") for evidence in evidence_rows):
                    used=True;break
            reassessed.append({"job_key":item["job_key"],"classification":result["classification"],
                "ai_provider_called":False,"evidence_source_verified":used})
        except Exception as error:
            reassessed.append({"job_key":item["job_key"],"error_type":type(error).__name__})
    return {"saved":True,"persisted_verified":True,"answer_key":saved.get("key"),"reuse_scope":reuse_scope,
        "affected_jobs":matched,"reassessed":reassessed,"conflict_acknowledged":bool(conflicts and request.acknowledge_existing_conflict),
        "message":"Technical evidence saved and read back. Selected job assessments were refreshed deterministically; no AI matching or application preparation was started."}

@router.get("/api/applications/{application_id}/questions")
def list_missing_application_questions(application_id:str):
    with connect() as db:
        row=db.execute("SELECT diagnostic_json,state,resume_id FROM applications WHERE id=? AND user_id=?",(application_id,USER)).fetchone()
    if not row: raise HTTPException(404,"Application not found.")
    if row["state"]!="needs_user_input": return {"groups":[]}
    try: diagnostic=json.loads(row["diagnostic_json"] or "{}")
    except (TypeError,json.JSONDecodeError): diagnostic={}
    resume,candidate,answers=_candidate()
    if resume["id"]!=row["resume_id"]: return {"groups":_group_questions(diagnostic.get("unanswered_fields",[])),"profile_version_available":False}
    groups=_group_questions(diagnostic.get("unanswered_fields",[]))
    for group in groups:
        for item in group["questions"]:
            info={"label":item["question"]}
            value,source=_answer(info,candidate,answers)
            item["existing_answer"]=str(value)[:1200] if value and source not in {"__RESUME__"} else ""
            item["can_reuse"]=bool(value and (source in candidate or (isinstance(source,str) and source.startswith("custom:"))) and not item["sensitive"])
            item["human_action_required"]=group["category"] in {"Human verification","Legal or sensitive questions"}
    return {"groups":groups,"profile_version_available":True}

@router.get("/api/applications")
def list_applications():
    with connect() as db:
        rows=db.execute("""SELECT a.id,a.job_key,a.resume_id,a.company,a.title,a.application_url,a.state,a.mode,a.diagnostic_json,a.error,a.created_at,a.updated_at,a.started_at,a.state_changed_at,a.attempt_number,
        (SELECT created_at FROM application_attempts t WHERE t.application_id=a.id AND t.attempt_number=a.attempt_number LIMIT 1) AS last_attempt_created_at,
        (SELECT COUNT(*) FROM application_attempts t WHERE t.application_id=a.id) AS attempt_count,
        (SELECT COALESCE(NULLIF(o.job_url,''),o.url) FROM discovered_openings o WHERE o.user_id=a.user_id AND o.job_key=a.job_key LIMIT 1) AS source_url
        FROM applications a WHERE a.user_id=? ORDER BY a.updated_at DESC LIMIT 250""",(USER,)).fetchall()
        manual_rows=db.execute("SELECT application_id,event_id,attempt_number,outcome,confirmed_by_applicant,recorded_at FROM manual_submission_events WHERE user_id=? ORDER BY recorded_at DESC",
                               (USER,)).fetchall()
    manual_by_app={}
    for event in manual_rows:manual_by_app.setdefault(event["application_id"],[]).append(dict(event))
    try: candidate_state=_candidate()
    except HTTPException: candidate_state=(None,{}, {})
    result=[]
    for row in rows:
        item=dict(row)
        try: item["diagnostic"]=json.loads(item.pop("diagnostic_json"))
        except (TypeError,json.JSONDecodeError): item["diagnostic"]={}
        history=manual_by_app.get(item["id"],[])
        if history:
            item["diagnostic"]["manual_submission_outcome"]=history[0]["outcome"]
            item["manual_submission_history"]=history
        try:item["source_url"]=urlparse(str(item.get("source_url") or ""))._replace(query="",fragment="").geturl()
        except ValueError:item["source_url"]=""
        try:item["application_url"]=urlparse(str(item.get("application_url") or ""))._replace(query="",fragment="").geturl()
        except ValueError:item["application_url"]=""
        item["action_required_groups"]=_group_questions(item["diagnostic"].get("unanswered_fields",[]))
        item["action_required_count"]=sum(len(group["questions"]) for group in item["action_required_groups"])
        item["last_updated_at"]=item.get("updated_at") or ""
        item["last_attempt_at"]=item.get("started_at") or item.get("last_attempt_created_at") or ""
        item["readiness"]=_readiness_for_application(item,candidate_state)
        item["status_label"]=item["readiness"]["status_label"]
        result.append(item)
    return result

@router.get("/api/applications/attention")
def list_application_attention():
    """Return active human handoffs without creating application attempts."""
    with connect() as db:
        rows=db.execute("""SELECT attention_id,job_key,employer,title,posting_url,application_url,blocker_code,reason,
            applicant_action,eligibility_state,form_state,preparation_state,status,created_at,updated_at,last_seen_at
            FROM application_attention_items WHERE user_id=? AND status IN ('open','acknowledged')
            ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END,updated_at DESC LIMIT 200""",(USER,)).fetchall()
    result=[]
    for row in rows:
        item=dict(row)
        for key in ("posting_url","application_url"):
            try:item[key]=urlparse(str(item.get(key) or ""))._replace(query="",fragment="").geturl()
            except ValueError:item[key]=""
        result.append(item)
    return {"items":result,"count":len(result)}


@router.get("/api/applications/{application_id}/attempts")
def list_application_attempts(application_id:str):
    with connect() as db:
        exists=db.execute("SELECT 1 FROM applications WHERE id=? AND user_id=?",(application_id,USER)).fetchone()
        if not exists: raise HTTPException(404,"Application not found.")
        rows=db.execute("SELECT attempt_number,state,started_at,state_changed_at,created_at,updated_at,diagnostic_json,error FROM application_attempts WHERE application_id=? ORDER BY attempt_number DESC",(application_id,)).fetchall()
        manual_rows=db.execute("SELECT event_id,attempt_number,outcome,confirmed_by_applicant,recorded_at FROM manual_submission_events WHERE user_id=? AND application_id=? ORDER BY recorded_at DESC",
                               (USER,application_id)).fetchall()
    manual_by_attempt={}
    for event in manual_rows:manual_by_attempt.setdefault(event["attempt_number"],[]).append(dict(event))
    result=[]
    for row in rows:
        item=dict(row)
        try:item["diagnostic"]=json.loads(item.pop("diagnostic_json"))
        except (TypeError,json.JSONDecodeError):item["diagnostic"]={}
        item["manual_outcomes"]=manual_by_attempt.get(item["attempt_number"],[])
        result.append(item)
    return result


@router.post("/api/applications/{application_id}/manual-outcome")
def record_manual_submission_outcome(application_id:str,request:ManualSubmissionOutcomeRequest):
    """Record an applicant-reported outcome without changing dry-run/submission state."""
    if not request.confirmed:
        raise HTTPException(422,"Confirm that this is your accurate report before saving the manual application outcome.")
    event_id=request.event_id.strip() or str(uuid.uuid4())
    stamp=now()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("SELECT state,attempt_number,diagnostic_json,submitted_at FROM applications WHERE id=? AND user_id=?",
                       (application_id,USER)).fetchone()
        if not row:raise HTTPException(404,"Application not found.")
        try:diagnostic=json.loads(row["diagnostic_json"] or "{}")
        except (TypeError,json.JSONDecodeError):diagnostic={}
        existing=db.execute("SELECT event_id,application_id,user_id,attempt_number,outcome,confirmed_by_applicant,recorded_at FROM manual_submission_events WHERE event_id=?",
                            (event_id,)).fetchone()
        if existing:
            if existing["application_id"]!=application_id or existing["user_id"]!=USER or existing["outcome"]!=request.outcome:
                raise HTTPException(409,"This manual outcome event ID was already used for a different outcome.")
            return {"saved":True,"verified_by_readback":True,"event":dict(existing),"state":row["state"],
                    "submitted_at":row["submitted_at"],"submission_enabled":False}
        event={"event_id":event_id,"application_id":application_id,"user_id":USER,
               "attempt_number":int(row["attempt_number"] or 1),"outcome":request.outcome,
               "confirmed_by_applicant":True,"recorded_at":stamp}
        db.execute("INSERT INTO manual_submission_events(event_id,application_id,user_id,attempt_number,outcome,confirmed_by_applicant,recorded_at) VALUES(?,?,?,?,?,1,?)",
                   (event_id,application_id,USER,event["attempt_number"],request.outcome,stamp))
        diagnostic["manual_submission_outcome"]=request.outcome
        # A manual report is not an automated submission and must never alter
        # application state, submitted_at, or the automated submission flag.
        diagnostic["submission_performed"]=False
        encoded=json.dumps(diagnostic,ensure_ascii=False)[:30000]
        db.execute("UPDATE applications SET diagnostic_json=?,updated_at=? WHERE id=? AND user_id=?",
                   (encoded,stamp,application_id,USER))
        db.execute("UPDATE application_attempts SET diagnostic_json=?,updated_at=? WHERE application_id=? AND attempt_number=?",
                   (encoded,stamp,application_id,row["attempt_number"]))
        persisted=db.execute("SELECT state,submitted_at,diagnostic_json FROM applications WHERE id=? AND user_id=?",
                             (application_id,USER)).fetchone()
        try:readback=json.loads(persisted["diagnostic_json"] or "{}")
        except (TypeError,json.JSONDecodeError):readback={}
        saved_event=db.execute("SELECT outcome,confirmed_by_applicant,attempt_number FROM manual_submission_events WHERE event_id=? AND application_id=? AND user_id=?",
                               (event_id,application_id,USER)).fetchone()
        verified=(persisted["state"]==row["state"] and persisted["submitted_at"]==row["submitted_at"]
                  and readback.get("manual_submission_outcome")==request.outcome and saved_event is not None
                  and saved_event["outcome"]==request.outcome and bool(saved_event["confirmed_by_applicant"]))
        if not verified:raise HTTPException(500,"The manual outcome could not be verified after saving. Refresh application history before trying again.")
    return {"saved":True,"verified_by_readback":True,"event":event,"state":row["state"],
            "submitted_at":row["submitted_at"],"submission_enabled":False}


def _readiness_for_application(app,candidate_state):
    if not isinstance(app,dict): app=dict(app)
    diagnostic=app.get("diagnostic") if isinstance(app.get("diagnostic"),dict) else {}
    if not diagnostic and app.get("diagnostic_json"):
        try:diagnostic=json.loads(app["diagnostic_json"] or "{}")
        except (TypeError,json.JSONDecodeError):diagnostic={}
    state=str(app.get("state") or "")
    manual_outcome=str(diagnostic.get("manual_submission_outcome") or "")
    manual_completion_reported=manual_outcome in {"submitted_manually","employer_confirmation_received"}
    unanswered=diagnostic.get("unanswered_fields") if isinstance(diagnostic.get("unanswered_fields"),list) else []
    human_actions=diagnostic.get("human_actions_required") if isinstance(diagnostic.get("human_actions_required"),list) else []
    local_mock=_mock_allowed(app.get("application_url",""))
    current_resume,candidate,answers=candidate_state
    if current_resume is not None and not isinstance(current_resume,dict): current_resume=dict(current_resume)
    eligibility={"status":"Not applicable (local test)" if local_mock else "Unknown","reason":""}
    authorization={"status":"Not required (local test)" if local_mock else "Missing","reason":""}
    gate_ok=bool(local_mock and current_resume); unresolved=False; scope=None
    if local_mock and not current_resume:
        eligibility.update(status="Unknown",reason="Activate a valid resume before continuing this dry run.")
    if not local_mock:
        if not current_resume:
            eligibility.update(status="Unknown",reason="Activate a valid resume and verified profile.")
            authorization.update(status="Unavailable",reason="Activate a resume before authorizing recovery.")
        elif app.get("resume_id")!=current_resume.get("id"):
            eligibility.update(status="Stale",reason="This attempt uses a different resume version. Activate that version to continue.")
            authorization.update(status="Stale",reason="Authorization is bound to the resume used by this attempt.")
        else:
            try:
                with connect() as db:
                    gate,reviewed,unresolved=_eligibility_gate(db,app.get("job_key"),current_resume["id"])
                if gate in {"potential_match","needs_review"} and not unresolved and (gate!="needs_review" or reviewed):
                    eligibility.update(status="Current",reason="Current matching evidence permits this preparation review.")
                    gate_ok=True
                elif gate=="not_eligible":
                    eligibility.update(status="Conflict",reason="A mandatory eligibility mismatch blocks continuation.")
                elif gate=="needs_review":
                    eligibility.update(status="Needs review",reason="Review or resolve mandatory eligibility evidence first.")
                else:
                    eligibility.update(status="Stale or missing",reason="Refresh matching evidence and resolve mandatory requirements first.")
            except (HTTPException,sqlite3.Error,KeyError):
                eligibility.update(status="Unknown",reason="Eligibility evidence could not be verified.")
            if gate_ok:
                try: scope=_current_preparation_authorization(app.get("job_key"),current_resume["id"],app.get("application_url"))
                except (HTTPException,sqlite3.Error,ValueError): scope=None
            if scope:
                authorization.update(status="Current",reason="Job-specific authorization fingerprints still match.")
            else:
                authorization.update(status="Missing or stale",reason="Review and renew job-specific preparation authorization.")
            gate_ok=bool(gate_ok and scope)

    answer_ready=not unanswered
    answer_reason="No required questions remain."
    if unanswered:
        answer_ready=False
        answer_reason="Resolve the listed required questions before retrying."
        if not local_mock and gate_ok and scope and current_resume:
            try:
                job=_get_job(DryRunRequest(job_key=app.get("job_key")))
                job.update({"job_key":app.get("job_key"),"company":app.get("company"),"title":app.get("title"),"application_url":app.get("application_url")})
                scoped_candidate=_scoped_candidate(candidate,scope)
                scoped_answers=_scoped_answers(_application_context(job,answers),scope)
                answer_ready=True
                for raw in unanswered:
                    question=_display_question(raw)
                    info={"label":question}
                    if _sensitive(info) or _requires_human_confirmation(info):
                        answer_ready=False;answer_reason="A sensitive, legal, or human-confirmation question requires your action.";break
                    value,source=_answer(info,scoped_candidate,scoped_answers)
                    if not value or (source=="resume" and "resume" not in scope.get("document_types",[])):
                        answer_ready=False;answer_reason="A confirmed, authorized answer or document is still missing.";break
            except (HTTPException,KeyError,TypeError,ValueError):
                answer_ready=False;answer_reason="Current confirmed answers could not be verified. Review the question group first."

    question_count=len(unanswered)
    question_groups=_group_questions(unanswered)
    human_action_items=list(dict.fromkeys([str(value) for value in human_actions]+[
        question["question"] for group in question_groups if group["category"] in {"Human verification","Legal or sensitive questions"}
        for question in group["questions"]]))
    if state=="needs_user_input" and human_action_items:status_label="Needs Human Action"
    else:status_label={"queued":"Queued","opening":"Preparing","inspecting":"Preparing","filling":"Preparing","uploading_resume":"Preparing",
        "needs_user_input":"Needs Your Input","ready_for_review":"Ready for Review","failed":"Failed","unsupported":"Unsupported",
        "paused":"Paused","submitted":"Historical Submission Record","skipped_duplicate":"Duplicate Skipped"}.get(state,state.replace("_"," ").title())
    documents_needed=list(dict.fromkeys(str(value)[:240] for value in unanswered if re.search(r"resume|document|transcript|upload|file",str(value),re.I)))
    interrupted=bool(diagnostic.get("interrupted"))
    # Browser contexts are not persisted. A paused attempt is safely resumable
    # only if it stopped before navigation or any field interaction. Later
    # pauses must create a new, explicitly confirmed attempt.
    untouched_checkpoint=(not diagnostic.get("phase") and not diagnostic.get("filled_fields") and not diagnostic.get("steps"))
    safe_resume=state=="paused" and not interrupted and untouched_checkpoint
    retryable_state=state in {"failed","needs_user_input"} or (state=="paused" and not safe_resume)
    resume_allowed=safe_resume and gate_ok and not manual_completion_reported
    retry_allowed=retryable_state and gate_ok and answer_ready and not human_action_items and state!="unsupported" and not manual_completion_reported
    if manual_completion_reported: retry_reason="A manual submission or employer confirmation is recorded. Update that outcome if it was entered in error before starting another preparation."
    elif state=="unsupported": retry_reason="The last attempt found unsupported employer controls. Inspect compatibility before another attempt."
    elif state=="paused" and not safe_resume: retry_reason="This browser checkpoint cannot be restored safely; retry creates a new attempt and preserves this history."
    elif not gate_ok: retry_reason=eligibility.get("reason") or authorization.get("reason")
    elif not answer_ready: retry_reason=answer_reason
    elif human_action_items: retry_reason="Complete the required human action before retrying."
    else: retry_reason="Retry starts a new attempt and preserves earlier attempt history."
    if manual_completion_reported: resume_reason="A manual submission or employer confirmation is recorded. Update that outcome if it was entered in error before resuming preparation."
    elif state=="paused" and interrupted:
        resume_reason="This worker was interrupted and its browser state cannot be restored. Use Retry as a new attempt if current checks pass."
    elif state=="paused" and not untouched_checkpoint:
        resume_reason="The browser session was closed and its in-page checkpoint is not restorable. Use Retry as a new attempt if current checks pass."
    elif state!="paused": resume_reason="Only a verified, non-interrupted paused attempt can resume."
    elif not gate_ok: resume_reason=eligibility.get("reason") or authorization.get("reason")
    else: resume_reason="Resume continues this existing attempt from its last saved checkpoint."
    progress_map={"queued":8,"opening":20,"inspecting":35,"filling":65,"uploading_resume":82,"paused":68,
        "needs_user_input":82,"ready_for_review":100,"failed":100,"unsupported":100,"submitted":100}
    checkpoint=diagnostic.get("last_successful_checkpoint") or "No successful checkpoint recorded"
    uploaded=sum(1 for item in diagnostic.get("filled_fields",[]) if isinstance(item,dict) and item.get("source") in {"private_document","active_resume"} and item.get("result")=="filled")
    return {
        "status_label":status_label,
        "progress_percent":progress_map.get(state,0),"eligibility":eligibility,"authorization":authorization,
        "questions":{"remaining":question_count,"groups":question_groups,"ready_for_retry":answer_ready,"reason":answer_reason},
        "documents":{"needed":documents_needed,"uploaded_controls":uploaded,"status":"Missing" if documents_needed else ("Resume control filled" if uploaded else "No missing document reported")},
        "human_actions":{"required":human_action_items,"count":len(human_action_items)},
        "last_checkpoint":str(checkpoint).replace("_"," ")+((" · "+str(diagnostic.get("steps"))+" step(s)") if diagnostic.get("steps") is not None else ""),
        "browser_compatibility":"Unsupported" if state=="unsupported" else ("Form detected" if diagnostic.get("form_detected") else "Not yet verified"),
        "resume_allowed":resume_allowed,"resume_reason":resume_reason,"retry_allowed":retry_allowed,"retry_reason":retry_reason,
        "retry_creates_new_attempt":retryable_state,"manual_submission_outcome":manual_outcome,
        "manual_completion_reported":manual_completion_reported}


@router.post("/api/applications/{application_id}/drafts/{draft_index}/regenerate")
def regenerate_application_draft(application_id:str,draft_index:int):
    settings=get_authorization()["settings"]
    if not settings.get("allow_ai_drafting"):
        raise HTTPException(403,"Enable AI drafting in Application settings first.")
    with connect() as db:
        row=db.execute("SELECT job_key,company,title,application_url,state,diagnostic_json FROM applications WHERE id=? AND user_id=?",(application_id,USER)).fetchone()
    if not row: raise HTTPException(404,"Application not found.")
    if row["state"]!="needs_user_input": raise HTTPException(409,"Regeneration is available only while an application is waiting for review.")
    try: diagnostic=json.loads(row["diagnostic_json"] or "{}")
    except (TypeError,json.JSONDecodeError): raise HTTPException(409,"Saved application diagnostics are unavailable.")
    drafts=diagnostic.get("draft_answers") or []
    if draft_index<0 or draft_index>=len(drafts): raise HTTPException(404,"Draft answer not found.")
    previous=drafts[draft_index]; question=str(previous.get("question") or "")
    if not question or not ORDINARY_PROMPT.search(question) or SENSITIVE_PROMPT.search(question):
        raise HTTPException(422,"This question cannot be regenerated safely.")
    resume,candidate,answers=_candidate()
    facts=draft_safe_profile(candidate)
    job_record={"job_key":row["job_key"],"company":row["company"],"title":row["title"],"application_url":row["application_url"]}
    answers=_application_context(job_record,answers)
    preferences=answers.get("__preferences",{})
    try: prompt_job,description=answer_assistant_context(row["job_key"])
    except HTTPException as error:
        if error.status_code!=404: raise
        prompt_job={"company":row["company"],"title":row["title"],"source":"Company Careers Page"}; description=""
    previous_answer=str(previous.get("answer") or "")
    # Force a fresh model response while keeping the prior unsupported answer
    # out of the prompt and bypassing its cached result.
    revised=generate_draft(question,facts,preferences,prompt_job,description,
        max_chars=previous.get("max_chars"),max_words=previous.get("max_words"),regenerate=True,avoid_answer=previous_answer)
    drafts[draft_index]={**previous,"answer":revised.get("answer",""),"evidence_used":revised.get("evidence_used",[]),
        "validation":revised.get("validation",{}),"status":"grounded_draft" if not revised.get("needs_user_input") else "ready_to_review",
        "approved":False,"regeneration_count":int(previous.get("regeneration_count",0))+1,
        "previous_answer_replaced":bool(previous_answer)}
    diagnostic["draft_answers"]=drafts
    with connect() as db:
        db.execute("UPDATE applications SET diagnostic_json=?,updated_at=?,state_changed_at=? WHERE id=? AND user_id=?",(json.dumps(diagnostic,ensure_ascii=False)[:30000],now(),now(),application_id,USER))
        db.execute("UPDATE application_attempts SET diagnostic_json=?,updated_at=?,state_changed_at=? WHERE application_id=? AND attempt_number=(SELECT attempt_number FROM applications WHERE id=?)",(json.dumps(diagnostic,ensure_ascii=False)[:30000],now(),now(),application_id,application_id))
    return {"question":question,"answer":drafts[draft_index]["answer"],"needs_user_input":revised.get("needs_user_input",True),
        "validation":drafts[draft_index]["validation"],"evidence_used":drafts[draft_index]["evidence_used"],
        "message":"A fresh draft is saved for review; it has not been inserted into the employer form."}


def answer_assistant_context(job_key):
    from application_answer_assistant import _job_context
    return _job_context(job_key)


@router.get("/api/applications/{application_id}/screenshot")
def application_screenshot(application_id:str):
    with connect() as db:
        row=db.execute("SELECT diagnostic_json FROM applications WHERE id=? AND user_id=?",(application_id,USER)).fetchone()
    if not row: raise HTTPException(404,"Application not found.")
    try: name=json.loads(row["diagnostic_json"] or "{}").get("screenshot")
    except (TypeError,json.JSONDecodeError): name=None
    if not isinstance(name,str) or Path(name).name!=name or not name.endswith(".png"):
        raise HTTPException(404,"No screenshot is available for this attempt.")
    target=(ARTIFACTS/name).resolve()
    if target.parent!=ARTIFACTS.resolve() or not target.is_file(): raise HTTPException(404,"Screenshot is no longer available.")
    return FileResponse(target,media_type="image/png",filename="application-dry-run.png")

@router.post("/api/applications/pause")
def pause_applications():
    with connect() as db:
        db.execute("UPDATE application_controls SET paused=1,updated_at=? WHERE user_id=?",(now(),USER))
        stamp=now(); db.execute("UPDATE applications SET state='paused',updated_at=?,state_changed_at=?,heartbeat_at=NULL WHERE user_id=? AND state='queued'",(stamp,stamp,USER))
        db.execute("UPDATE application_attempts SET state='paused',updated_at=?,state_changed_at=? WHERE state='queued' AND application_id IN (SELECT id FROM applications WHERE user_id=? AND state='paused')",(stamp,stamp,USER))
    return {"paused":True}

@router.post("/api/applications/resume")
def resume_processing():
    with connect() as db:
        db.execute("UPDATE application_controls SET paused=0,updated_at=? WHERE user_id=?",(now(),USER))
        rows=db.execute("SELECT id,resume_id,job_key,company,title,application_url,state,diagnostic_json FROM applications WHERE user_id=? AND state='paused' ORDER BY created_at",(USER,)).fetchall()
    resume,candidate,answers=_candidate() if rows else (None,None,None)
    resumable=[]
    waiting=[]
    for row in rows:
        app=dict(row)
        readiness=_readiness_for_application(app,(resume,candidate,answers))
        if readiness["resume_allowed"]: resumable.append(app)
        else: waiting.append({"application_id":app["id"],"reason":readiness["resume_reason"]})
    stamp=now()
    if resumable:
        with _attempt_lock,connect() as db:
            db.execute("BEGIN IMMEDIATE")
            active=db.execute("SELECT id FROM applications WHERE user_id=? AND state IN ('queued','opening','inspecting','filling','uploading_resume') LIMIT 1",(USER,)).fetchone()
            if active:
                return {"paused":False,"resumed":0,"needs_explicit_retry":0,"waiting_for_original_resume":len(rows),"blocked":[{"application_id":active["id"],"reason":"Another preparation is already active."}]}
            for row in resumable:
                db.execute("UPDATE applications SET state='queued',state_changed_at=?,updated_at=? WHERE id=? AND state='paused'",(stamp,stamp,row["id"]))
                db.execute("UPDATE application_attempts SET state='queued',state_changed_at=?,updated_at=? WHERE application_id=? AND attempt_number=(SELECT attempt_number FROM applications WHERE id=?)",(stamp,stamp,row["id"],row["id"]))
        for row in resumable:
            scoped_candidate,scoped_answers=candidate,answers
            if not _mock_allowed(row["application_url"]):
                scope=_current_preparation_authorization(row["job_key"],resume["id"],row["application_url"])
                if not scope:continue
                job=_get_job(DryRunRequest(job_key=row["job_key"]))
                job.update({"job_key":row["job_key"],"company":row["company"],"title":row["title"],"application_url":row["application_url"]})
                scoped_candidate=_scoped_candidate(candidate,scope);scoped_answers=_scoped_answers(_application_context(job,answers),scope)
            executor.submit(process_application,row["id"],row,resume,scoped_candidate,scoped_answers)
    return {"paused":False,"resumed":len(resumable),"needs_explicit_retry":sum("interrupted" in value["reason"].lower() for value in waiting),"waiting_for_original_resume":len(waiting),"blocked":waiting}

@router.get("/api/applications/progress")
def application_progress():
    recover_stale_applications()
    with connect() as db:
        counts={row["state"]:row["total"] for row in db.execute("SELECT state,COUNT(*) AS total FROM applications WHERE user_id=? GROUP BY state",(USER,)).fetchall()}
        paused=db.execute("SELECT paused FROM application_controls WHERE user_id=?",(USER,)).fetchone()["paused"]
    try: configured=min(200,max(1,int(get_authorization()["settings"].get("max_applications_per_24_hours",20))))
    except (TypeError,ValueError): configured=20
    # Progress polling is a read path. Keep the mirrored controls timestamp
    # stable unless the configured limit or safety flag actually changes.
    with connect() as db: db.execute("UPDATE application_controls SET daily_limit=?,submission_enabled=0,updated_at=? WHERE user_id=? AND (daily_limit<>? OR submission_enabled<>0)",(configured,now(),USER,configured))
    with _attempt_lock: active=list(_active_attempts)
    quota=rolling_submission_quota()
    return {"counts":counts,"paused":bool(paused),"submission_enabled":False,"daily_submission_limit":configured,"active_attempts":len(active),"processing_state":"paused" if paused else ("running" if active else "idle"),"submission_quota":quota}

@router.post("/api/applications/{application_id}/resume")
def resume_one(application_id:str):
    resume,candidate,answers=_candidate()
    with connect() as db:
        row=db.execute("SELECT * FROM applications WHERE id=? AND user_id=?",(application_id,USER)).fetchone()
        if not row: raise HTTPException(404,"Application not found.")
        row=dict(row)
    if row["state"]!="paused": raise HTTPException(409,"Resume is only for a paused attempt. Use Retry Dry Run for a separately approved new attempt.")
    readiness=_readiness_for_application(row,(resume,candidate,answers))
    if not readiness["resume_allowed"]: raise HTTPException(409,readiness["resume_reason"])
    scope=None if _mock_allowed(row["application_url"]) else _current_preparation_authorization(row["job_key"],resume["id"],row["application_url"])
    with _attempt_lock,connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current=db.execute("SELECT state,attempt_number,diagnostic_json FROM applications WHERE id=? AND user_id=?",(application_id,USER)).fetchone()
        if not current or current["state"]!="paused": raise HTTPException(409,"The attempt state changed. Refresh before resuming it.")
        active=db.execute("SELECT id FROM applications WHERE user_id=? AND state IN ('queued','opening','inspecting','filling','uploading_resume') AND id<>? LIMIT 1",(USER,application_id)).fetchone()
        if active or application_id in _active_attempts: raise HTTPException(409,"Another preparation is active. Wait before resuming this attempt.")
        try:
            current_diagnostic=json.loads(current["diagnostic_json"] or "{}")
            interrupted=bool(current_diagnostic.get("interrupted"))
        except (TypeError,json.JSONDecodeError): interrupted=True
        if interrupted or current_diagnostic.get("phase") or current_diagnostic.get("filled_fields") or current_diagnostic.get("steps"):
            raise HTTPException(409,"This browser state cannot be restored safely. Use Retry as a new attempt if current checks pass.")
        stamp=now()
        db.execute("UPDATE application_controls SET paused=0,updated_at=? WHERE user_id=?",(stamp,USER))
        db.execute("UPDATE applications SET state='queued',error=NULL,updated_at=?,state_changed_at=?,heartbeat_at=NULL WHERE id=?",(stamp,stamp,application_id))
        db.execute("UPDATE application_attempts SET state='queued',error=NULL,updated_at=?,state_changed_at=? WHERE application_id=? AND attempt_number=?",(stamp,stamp,application_id,current["attempt_number"]))
    if scope:
        job=_get_job(DryRunRequest(job_key=row["job_key"]))
        job.update({"job_key":row["job_key"],"company":row["company"],"title":row["title"],"application_url":row["application_url"]})
        candidate=_scoped_candidate(candidate,scope);answers=_scoped_answers(_application_context(job,answers),scope)
    executor.submit(process_application,application_id,dict(row),resume,candidate,answers)
    return {"application_id":application_id,"status":"queued","resumed_existing_attempt":True,"new_attempt_created":False}


@router.post("/api/applications/{application_id}/retry")
def retry_one(application_id:str):
    resume,candidate,answers=_candidate()
    with connect() as db:
        row=db.execute("SELECT * FROM applications WHERE id=? AND user_id=?",(application_id,USER)).fetchone()
    if not row: raise HTTPException(404,"Application not found.")
    row=dict(row)
    if row["state"] not in {"failed","needs_user_input","paused"}:
        raise HTTPException(409,"Retry is available only for failed attempts, resolved input requests, or interrupted attempts. Unsupported controls are not retried automatically.")
    readiness=_readiness_for_application(row,(resume,candidate,answers))
    if not readiness["retry_allowed"]:raise HTTPException(409,readiness["retry_reason"])
    if int(row.get("attempt_number") or 1)>=MAX_APPLICATION_ATTEMPTS:
        raise HTTPException(409,"The bounded preparation retry limit was reached. Continue manually at the official employer site.")
    scope=None if _mock_allowed(row["application_url"]) else _current_preparation_authorization(row["job_key"],resume["id"],row["application_url"])
    try:validate_application_url(row["application_url"])
    except ValueError as error:raise HTTPException(400,str(error)) from error
    with _attempt_lock,connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current=db.execute("SELECT state,attempt_number,diagnostic_json FROM applications WHERE id=? AND user_id=?",(application_id,USER)).fetchone()
        if not current or current["state"]!=row["state"]:raise HTTPException(409,"The attempt state changed. Refresh before retrying.")
        active=db.execute("SELECT id FROM applications WHERE user_id=? AND state IN ('queued','opening','inspecting','filling','uploading_resume') LIMIT 1",(USER,)).fetchone()
        if active or application_id in _active_attempts:raise HTTPException(409,"Another preparation is active. Wait before starting a new attempt.")
        stamp=now();number=int(current["attempt_number"] or 0)+1
        try:old_diagnostic=json.loads(current["diagnostic_json"] or "{}")
        except (TypeError,json.JSONDecodeError):old_diagnostic={}
        retained={key:old_diagnostic[key] for key in ("manual_submission_events","manual_submission_outcome") if key in old_diagnostic}
        db.execute("UPDATE application_controls SET paused=0,updated_at=? WHERE user_id=?",(stamp,USER))
        db.execute("UPDATE applications SET state='queued',error=NULL,diagnostic_json=?,updated_at=?,state_changed_at=?,started_at=NULL,heartbeat_at=NULL,attempt_number=? WHERE id=?",(json.dumps(retained,ensure_ascii=False),stamp,stamp,number,application_id))
        db.execute("INSERT INTO application_attempts(id,application_id,attempt_number,state,state_changed_at,diagnostic_json,created_at,updated_at) VALUES(?,?,?,'queued',?,?,?,?)",(str(uuid.uuid4()),application_id,number,stamp,json.dumps(retained,ensure_ascii=False),stamp,stamp))
    if scope:
        job=_get_job(DryRunRequest(job_key=row["job_key"]))
        job.update({"job_key":row["job_key"],"company":row["company"],"title":row["title"],"application_url":row["application_url"]})
        candidate=_scoped_candidate(candidate,scope);answers=_scoped_answers(_application_context(job,answers),scope)
    executor.submit(process_application,application_id,row,resume,candidate,answers)
    return {"application_id":application_id,"status":"queued","new_attempt_created":True,"attempt_number":number}

@router.get("/applications",response_class=HTMLResponse)
def applications_page(): return HTMLResponse(apply_ui_theme(APPLICATIONS_PAGE))

@router.get("/mock-apps/{scenario}",response_class=HTMLResponse)
def mock_application(scenario:str):
    if scenario not in MOCK_PAGES: raise HTTPException(404,"Mock scenario not found.")
    return HTMLResponse(MOCK_PAGES[scenario])

APPLICATIONS_PAGE=r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Applications — BabyStepsAI</title>
<style>
:root{color-scheme:light;--ink:#17263b;--muted:#66758a;--line:#e3eaf2;--brand:#2563eb;--surface:#fff;--wash:#f4f7fb}*{box-sizing:border-box}body{margin:0;background:var(--wash);color:var(--ink);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.top{display:none}.nav{max-width:1120px;margin:auto;padding:16px 22px;display:flex;align-items:center;justify-content:space-between;gap:16px}.brand{font-weight:750;font-size:20px;letter-spacing:-.4px}.brand span{color:var(--brand)}nav{display:flex;gap:20px;flex-wrap:wrap}a{color:#315378;text-decoration:none}a:hover{text-decoration:underline}.wrap{max-width:1120px;margin:30px auto;padding:0 22px}.intro{display:flex;align-items:flex-start;justify-content:space-between;gap:20px}.intro h1{font-size:30px;margin:0 0 5px;letter-spacing:-.7px}.muted{color:var(--muted)}.notice{background:#edf4ff;border:1px solid #d6e5ff;border-radius:12px;padding:14px 16px;margin:20px 0}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));gap:12px;margin:18px 0}.metric,.app,.job{background:var(--surface);border:1px solid var(--line);border-radius:14px;box-shadow:0 2px 8px #102a4308}.metric{padding:16px}.metric strong{display:block;font-size:26px;line-height:1.2}.metric span{font-size:13px;color:var(--muted)}.toolbar{display:flex;gap:9px;flex-wrap:wrap;padding:14px;background:#fff;border:1px solid var(--line);border-radius:14px}.toolbar button,button{font:inherit}button{border:1px solid #d1dbea;border-radius:9px;background:#fff;color:var(--ink);padding:9px 13px;cursor:pointer;font-weight:600}button:hover:not(:disabled){border-color:var(--brand);color:var(--brand)}button.primary{background:var(--brand);border-color:var(--brand);color:white}button:disabled{opacity:.52;cursor:wait}.section-head{margin:27px 0 10px;display:flex;align-items:baseline;justify-content:space-between;gap:12px}.section-head h2{font-size:20px;margin:0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,350px),1fr));gap:13px}.app,.job{padding:17px}.app h3,.job h3{margin:0 0 4px;font-size:17px}.sub{color:var(--muted);font-size:14px}.status{display:inline-flex;align-items:center;gap:7px;font-weight:650;font-size:14px;margin:12px 0 5px}.dot{height:9px;width:9px;border-radius:50%;background:#8995a5}.status[data-state=opening] .dot,.status[data-state=filling] .dot{background:#f59e0b;animation:pulse 1.2s infinite}.status[data-state=ready_for_review] .dot{background:#22a06b}.status[data-state=needs_user_input] .dot{background:#e39a19}.status[data-state=failed] .dot{background:#dc4c4c}.status[data-state=paused] .dot{background:#7486a0}.bar{height:6px;border-radius:9px;background:#edf1f6;overflow:hidden;margin:7px 0 12px}.bar i{display:block;background:var(--brand);height:100%;transition:width .2s}.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}details{border-top:1px solid var(--line);margin-top:13px;padding-top:10px}summary{cursor:pointer;color:#405570;font-size:14px;font-weight:600}details p{font-size:14px;color:#45566d;margin:8px 0;overflow-wrap:anywhere}.empty{background:white;border:1px dashed #cbd6e3;border-radius:12px;padding:22px;color:var(--muted)}#status{min-height:24px;margin:14px 0;color:#315378}.pill{font-size:12px;background:#eef2f7;border-radius:20px;padding:3px 8px;color:#52647a}.loader{display:inline-block;width:13px;height:13px;border:2px solid #cbd7e6;border-top-color:var(--brand);border-radius:50%;animation:spin .7s linear infinite;vertical-align:-2px;margin-right:6px}@keyframes spin{to{transform:rotate(360deg)}}@keyframes pulse{50%{opacity:.4}}@media(max-width:620px){.nav{align-items:flex-start;flex-direction:column}.intro{display:block}.wrap{margin:22px auto;padding:0 14px}}
</style></head><body><header class="top"><div class="nav"><div class="brand">BabySteps<span>AI</span></div><nav aria-label="Main navigation"><a href="/">Home</a><a href="/find-jobs">Find jobs</a><a href="/candidate-profile">Candidate profile</a><a href="/applications" aria-current="page">Applications</a></nav></div></header>
<main class="wrap"><div class="intro"><div><h1>Applications</h1><div class="muted">Review your application activity and safely prepare forms.</div></div><span class="pill">Dry runs only · Submission disabled</span></div>
<div class="notice"><strong>Dry run — No application will be submitted.</strong> Dry runs may fill supported forms and attach your active resume. Legal questions, missing answers, and human verification stop for your review.</div>
<section class="compatibility-tool" aria-labelledby="compatibility-title"><div class="section-head"><h2 id="compatibility-title">Test Employer Form Compatibility</h2><span class="sub">Standalone read-only check</span></div><p class="sub">Paste an official public job URL to inspect its application page. This does not require a saved job, AI match, or candidate profile. BabyStepsAI will not enter information, upload files, accept terms, or submit.</p><form id="standalone-inspection-form"><label for="standalone-inspection-url">Official employer job URL</label><div class="compatibility-input"><input id="standalone-inspection-url" name="application_url" type="url" inputmode="url" autocomplete="url" placeholder="https://jobs.ashbyhq.com/company/job-id" maxlength="2048" required><button id="standalone-inspection-submit" class="primary" type="submit">Inspect public form</button></div></form><p id="standalone-inspection-status" role="status" aria-live="polite"></p><div id="standalone-inspection-result"></div></section>
<div id="summary" class="cards" aria-live="polite"></div><div class="toolbar" aria-label="Application controls"><button id="load-jobs" onclick="loadJobs()">Load saved jobs</button><button id="pause" onclick="action('/api/applications/pause')">Pause processing</button><button id="resume" onclick="action('/api/applications/resume')">Resume processing</button><span class="muted" id="run-state">Checking status…</span></div>
<div class="section-head"><h2>Local test forms</h2><span class="sub">Clearly labeled test workflows</span></div><div class="toolbar"><button onclick="mock('simple')">Test simple form</button><button onclick="mock('multi-step')">Test multi-step form</button><button onclick="mock('uncertain')">Test uncertain questions</button><button onclick="mock('captcha')">Test human-check stop</button></div>
<div class="section-head"><h2>Saved jobs</h2><span class="sub">Prepare one opening or explicitly queue selected openings</span></div><div class="toolbar" aria-label="Saved job filters"><label>Search <input id="job-search" type="search" placeholder="Title, company, or location"></label><label>Eligibility <select id="job-eligibility"><option value="all">All results</option><option value="potential_match">Potential match</option><option value="needs_review">Needs review</option><option value="not_eligible">Not eligible</option><option value="unmatched">Not analyzed</option></select></label><button id="filter-jobs" onclick="loadJobs()">Search saved jobs</button><button id="queue-selected" onclick="queueSelected()">Queue selected dry runs</button></div><div id="jobs" class="grid"><div class="empty">Load saved jobs to choose a dry run.</div></div>
<div class="section-head"><h2>Questions Requiring Your Attention</h2><span class="sub">Confirm once, then choose the reuse scope</span></div><div id="question-attention" class="grid"><div class="empty">Loading unresolved application questions…</div></div>
<div class="section-head"><h2>Technical Qualifications</h2><span class="sub">Verified project, coursework, and experience evidence for mandatory job requirements</span></div><div id="technical-attention" class="grid"><div class="empty">Loading saved technical assessments…</div></div>
<div class="section-head"><h2>Needs Your Attention</h2><span class="sub">Human actions grouped once per application</span></div><div id="attention-inbox" class="grid"><div class="empty">Loading application actions…</div></div>
<div class="section-head"><h2>Application history</h2><span class="sub">Refreshes automatically</span></div><div id="apps" class="grid"></div><p id="status" role="status" aria-live="polite"></p></main>
<script>
function eligibilityDisplay(job){const a=job.eligibility_review_assessment||{};if(job.eligibility_stale)return "Stale · re-analysis required";if(job.preparation_authorized)return "Preparation Authorized";if(job.application_state==="ready_for_review")return "Ready for Review";if(job.classification==="not_eligible"||a.confirmed_conflicts?.length)return "Confirmed Mandatory Conflict";if(job.classification==="potential_match"&&a.preparation_blocked)return "Potential Match — Further Review Required";if(job.classification==="potential_match"&&a.eligibility_resolved)return "Eligible for Preparation Review";if(job.classification==="needs_review")return "Needs Review";return job.classification||"Not analyzed"}
let busy=false,jobsLoaded=false;const labels={queued:'Queued',opening:'Opening employer site',inspecting:'Inspecting application form',filling:'Filling verified information',uploading_resume:'Uploading resume',paused:'Paused',needs_user_input:'Needs your input',ready_for_review:'Ready for review',failed:'Failed',unsupported:'Unsupported website',skipped_duplicate:'Already applied / duplicate skipped',submitted:'Historical submission record'};const progress={queued:8,opening:20,inspecting:35,filling:65,uploading_resume:82,paused:68,needs_user_input:82,ready_for_review:100,failed:100,unsupported:100,skipped_duplicate:100,submitted:100};
function readableApiError(value){if(typeof value==='string')return value;if(Array.isArray(value))return value.map(readableApiError).filter(Boolean).join(' · ');if(value&&typeof value==='object'){for(const key of ['message','detail','msg','error'])if(value[key])return readableApiError(value[key]);try{return JSON.stringify(value)}catch{return 'The request could not be completed.'}}return 'The request could not be completed. Please try again.'}async function req(url,options={}){const response=await fetch(url,options);let data={};try{data=await response.json()}catch{}if(!response.ok)throw Error(readableApiError(data.detail??data.message??data.error));return data}function say(message){document.querySelector('#status').textContent=readableApiError(message)}function escapeText(value){return String(value||'')}
async function runBusy(label,operation){if(busy)return;say(label);busy=true;document.querySelectorAll('button').forEach(b=>b.disabled=true);try{await operation()}catch(e){say(e.message||'Something went wrong.')}finally{busy=false;document.querySelectorAll('button').forEach(b=>b.disabled=false);await refresh()}}
async function mock(scenario){await runBusy('Starting local test dry run…',async()=>{const data=await req('/api/applications/dry-run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({job:{job_key:'local-mock:'+scenario,company:'BabyStepsAI local test',title:'Test '+scenario,application_url:location.origin+'/mock-apps/'+scenario}})});say(data.status==='skipped_duplicate'?'This test was already submitted; duplicate protection prevented another attempt.':'Local test dry run started.');})}
async function renderSavedJobs(silent=false){const q=document.querySelector('#job-search').value,eligibility=document.querySelector('#job-eligibility').value,params=new URLSearchParams({q,eligibility}),data=await req('/api/applications/saved-jobs?'+params.toString()),jobs=data.results||[],box=document.querySelector('#jobs');box.replaceChildren();jobsLoaded=true;if(!jobs.length){box.append(node('div','empty','No saved jobs match these filters. Discover openings from Find Jobs first.'));if(!silent)say('No saved jobs match these filters.');return}for(const job of jobs){const card=node('article','job'),title=node('h3','',job.title||'Untitled role'),company=node('div','sub',(job.company||'Company unknown')+' · '+(job.location||'Location not listed')+' · '+(job.employment_type||'Employment type unknown')),match=node('div','sub','Eligibility: '+(job.eligibility_stale?'Stale · re-analysis required':(job.classification||'Not analyzed'))+(job.match_score===null?'':' · resume relevance reference '+job.match_score+'/100 (not an employer ATS score)')),state=node('div','status',job.application_status_label||'Not prepared');state.dataset.state=job.application_state;card.append(title,company,match,state);if(job.url_syntax_allowed){const link=node('a','sub','View job posting');link.href=job.url;link.target='_blank';link.rel='noopener noreferrer';card.append(link)}else card.append(node('p','sub','Preparation unavailable: the saved URL is not a permitted public HTTPS link.'));const prep=node('button','primary','Prepare Application');prep.disabled=!job.preparation_allowed;prep.title=prep.disabled?'A resume, permitted URL, active posting, or completed workflow is required.':'';prep.onclick=()=>startSaved(job.job_key);card.append(prep);if(job.application_state==='needs_user_input'){const review=node('a','sub','Review missing answers');review.href='/candidate-profile';card.append(review)}box.append(card)}if(!silent)say(jobs.length+' saved openings shown. Preparing an application opens only the selected job and never submits it.')}
const cardStyle=document.createElement('style');cardStyle.textContent='.job{display:flex;flex-direction:column;align-items:flex-start;gap:9px;min-width:0}.job h3{margin:0;font-size:17px;line-height:1.35;overflow-wrap:anywhere}.job .sub{line-height:1.45;overflow-wrap:anywhere}.job .status{margin:0}.job .job-actions{display:flex;gap:9px;flex-wrap:wrap;align-items:center;width:100%;margin-top:2px}.job .job-message{border-left:3px solid #d7e2ef;padding:7px 10px;margin:0;overflow-wrap:anywhere}.job .job-missing{margin:0;padding-left:20px;color:#8b4c14}';document.head.append(cardStyle);
async function renderSavedJobs(silent=false){const q=document.querySelector('#job-search').value,eligibility=document.querySelector('#job-eligibility').value,params=new URLSearchParams({q,eligibility}),data=await req('/api/applications/saved-jobs?'+params.toString()),jobs=data.results||[],box=document.querySelector('#jobs');box.replaceChildren();jobsLoaded=true;if(!jobs.length){box.append(node('div','empty','No saved jobs match these filters. Discover openings from Find Jobs first.'));if(!silent)say('No saved jobs match these filters.');return}for(const job of jobs){const card=node('article','job'),title=node('h3','',job.title||'Untitled role'),company=node('div','sub',(job.company||'Company unknown')+' · '+(job.location||'Location not listed')+' · '+(job.employment_type||'Employment type unknown')),match=node('div','sub','Eligibility: '+(job.classification||'Not analyzed')+(job.match_score===null?'':' · resume relevance reference '+job.match_score+'/100 (not an employer ATS score)')),state=node('div','status',job.application_status_label||'Not prepared');state.dataset.state=job.application_state;card.append(title,company,match,state);if(job.application_message)card.append(node('p','sub job-message',job.application_message));if(job.missing_questions?.length){const list=node('ul','job-missing');for(const question of job.missing_questions)list.append(node('li','',question));card.append(list)}if(job.url_syntax_allowed){const link=node('a','sub','View job posting');link.href=job.url;link.target='_blank';link.rel='noopener noreferrer';card.append(link)}else card.append(node('p','sub','Preparation unavailable: the saved URL is not a permitted public HTTPS link.'));const actions=node('div','job-actions');if(job.application_state==='not_started'&&job.preparation_allowed){const select=document.createElement('label');select.className='queue-select';const check=document.createElement('input');check.type='checkbox';check.dataset.queueJob=job.job_key;select.append(check,document.createTextNode(' Add to dry-run queue'));actions.append(select)}if(job.application_state==='not_started'){const prep=node('button','primary',busy?'Preparing…':'Prepare Application');prep.disabled=busy||!job.preparation_allowed;prep.title=prep.disabled?'A resume, eligible opening, and permitted URL are required.':'';prep.onclick=()=>startSaved(job.job_key);actions.append(prep)}else if(['failed','paused','unsupported'].includes(job.application_state)&&job.application_id){const retryButton=node('button','primary','Retry dry run');retryButton.disabled=busy;retryButton.onclick=()=>retry(job.application_id);actions.append(retryButton)}else if(job.application_state==='needs_user_input'){const link=node('a','sub','Review profile and confirmed answers');link.href='/candidate-profile';actions.append(link)}if(job.application_state==='ready_for_review')actions.append(node('span','pill','Review in Application history'));if(actions.childElementCount)card.append(actions);box.append(card)}if(!silent)say(jobs.length+' saved openings shown. Preparing an application opens only the selected job and never submits it.')}
const phase151RenderSavedJobs=renderSavedJobs;renderSavedJobs=async function(silent=false){await phase151RenderSavedJobs(silent);const result=await req('/api/applications/saved-jobs?'+new URLSearchParams({q:document.querySelector('#job-search').value,eligibility:document.querySelector('#job-eligibility').value}).toString());document.querySelectorAll('#jobs .job').forEach((card,index)=>{const job=result.results?.[index];if(!job)return;const summary=card.querySelectorAll('.sub')[1];if(summary)summary.textContent='Eligibility: '+eligibilityDisplay(job)+(job.match_score===null?'':' · resume relevance reference '+job.match_score+'/100 (not an employer ATS score)')})};
async function loadJobs(){await runBusy('Loading saved jobs…',async()=>renderSavedJobs())}
async function startSaved(key){await runBusy('Preparing application…',async()=>{const d=await req('/api/applications/prepare',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({job_key:key})});say(d.message||'Preparation status updated.');await refresh()})}
async function queueSelected(){const keys=[...document.querySelectorAll('[data-queue-job]:checked')].map(x=>x.dataset.queueJob);if(!keys.length){say('Select at least one eligible saved job to queue.');return}await runBusy('Adding selected jobs to the sequential dry-run queue…',async()=>{const d=await req('/api/applications/queue',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({job_keys:keys})});say(d.queued+' job(s) queued. The worker will continue after a job pauses for your input.'+(d.skipped.length?' '+d.skipped.length+' selection(s) skipped.':''));})}
async function action(url){await runBusy(url.endsWith('/pause')?'Pausing application processing…':'Resuming safe queued work…',async()=>{const d=await req(url,{method:'POST'});if(url.endsWith('/pause'))say('Processing paused. The current form stops at its next safe point.');else say('Processing resumed. '+(d.needs_explicit_retry?'Interrupted attempts require a separate review and retry.':''))})}
function node(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e}
function renderSummary(apps,p){const counts=p.counts||{},quota=p.submission_quota||{},entries=[['Applications',apps.length],['Queued',counts.queued||0],['In progress',(counts.opening||0)+(counts.inspecting||0)+(counts.filling||0)+(counts.uploading_resume||0)],['Paused',counts.paused||0],['Needs your input',counts.needs_user_input||0],['Ready for review',counts.ready_for_review||0],['Failed / unsupported',(counts.failed||0)+(counts.unsupported||0)],['Confirmed submissions · 24h',quota.successful_last_24h||0],['Future capacity · live submission off',quota.remaining??p.daily_submission_limit??200]];const box=document.querySelector('#summary');box.replaceChildren(...entries.map(([label,value])=>{const c=node('div','metric');c.append(node('strong','',value),node('span','',label));return c}));document.querySelector('#run-state').textContent=p.paused?'Processing is paused':p.processing_state==='running'?'Processing is active':'Processing is ready';document.querySelector('#pause').disabled=!!p.paused||busy;document.querySelector('#resume').disabled=!p.paused||busy}
function renderAttentionInbox(apps){const box=document.querySelector('#attention-inbox');if(!box)return;box.replaceChildren();const pending=apps.filter(a=>['needs_user_input','unsupported','failed','ready_for_review'].includes(a.state));if(!pending.length){box.append(node('div','empty','No application needs your attention.'));return}for(const app of pending){const card=node('article','app'),label=app.state==='ready_for_review'?'Ready for your review':app.state==='needs_user_input'?'Missing required information':app.state==='unsupported'?'Human action required':'Preparation failed';card.append(node('h3','',escapeText(app.title)),node('div','sub',escapeText(app.company)),node('div','status',label));const reasons=app.diagnostic?.human_actions_required||app.diagnostic?.unanswered_fields||[];card.append(node('p','sub',reasons.length?reasons.join(' · '):(app.diagnostic?.message||app.error||'Review this preparation before continuing.')));const link=node('a','sub','Open official employer page ↗');link.href=app.source_url||app.application_url;link.target='_blank';link.rel='noopener noreferrer';card.append(link);card.append(node('p','sub',app.diagnostic?.interrupted?'Browser closed; previously entered values may need to be reentered.':app.state==='ready_for_review'?'Review the prepared application and submit manually if you choose.':'No final submission was performed.'));box.append(card)}}
function renderApps(apps){const box=document.querySelector('#apps');box.replaceChildren();if(!apps.length){box.append(node('div','empty','No application attempts yet. Start a dry run from a saved job or choose a local test form.'));return}for(const app of apps){const card=node('article','app'),head=node('h3','',escapeText(app.title)),company=node('div','sub',escapeText(app.company));const test=String(app.job_key||'').startsWith('local-mock:');const badge=node('span','pill',test?'Local test':'Saved job');const state=node('div','status',labels[app.state]||'Needs review');state.dataset.state=app.state;state.prepend(node('i','dot'));const bar=node('div','bar');const fill=node('i');fill.style.width=(progress[app.state]||15)+'%';bar.append(fill);const timing=node('div','sub','Last updated '+new Date(app.updated_at).toLocaleString()+(app.started_at?' · Attempt started '+new Date(app.started_at).toLocaleString():''));card.append(head,company,badge,state,bar,timing);if(app.diagnostic?.message)card.append(node('p','sub',app.diagnostic.message));if(app.diagnostic?.unanswered_fields?.length){card.append(node('p','sub','Needs your input: '+app.diagnostic.unanswered_fields.join(' · ')));const link=node('a','sub','Review candidate profile and answers');link.href='/candidate-profile';card.append(link)}if(['paused','failed','needs_user_input','unsupported'].includes(app.state)){const btn=node('button','',app.diagnostic?.interrupted?'Review and retry':'Retry dry run');btn.onclick=()=>retry(app.id);card.append(btn)}const detail=document.createElement('details'),summary=node('summary','','Diagnostics and attempt history');const body=node('div');if(app.error)body.append(node('p','', 'Technical error: '+app.error));if(app.diagnostic?.steps)body.append(node('p','', 'Form steps visited: '+app.diagnostic.steps));if(app.diagnostic?.filled_fields?.length)body.append(node('p','', 'Fields filled: '+app.diagnostic.filled_fields.filter(x=>x.result==='filled').length));if(app.diagnostic?.screenshot){const shot=node('a','sub','View private dry-run screenshot');shot.href='/api/applications/'+encodeURIComponent(app.id)+'/screenshot';shot.target='_blank';shot.rel='noopener';body.append(shot)}if(app.diagnostic?.interrupted)body.append(node('p','', 'This attempt was interrupted. It was not automatically retried because its outcome may be uncertain.'));detail.append(summary,body);detail.addEventListener('toggle',async()=>{if(!detail.open||detail.dataset.loaded)return;detail.dataset.loaded='1';try{const attempts=await req('/api/applications/'+encodeURIComponent(app.id)+'/attempts');for(const attempt of attempts){const item=node('p','sub','Attempt '+attempt.attempt_number+' · '+(labels[attempt.state]||attempt.state)+' · '+(attempt.started_at?'started '+new Date(attempt.started_at).toLocaleString():'not started'));body.append(item);if(attempt.diagnostic?.message)body.append(node('p','sub',attempt.diagnostic.message));if(attempt.error)body.append(node('p','sub','Technical error type: '+attempt.error))}}catch(e){body.append(node('p','sub',e.message))}});card.append(detail);box.append(card)}}
async function retry(id){await runBusy('Starting the selected retry…',async()=>{await req('/api/applications/'+encodeURIComponent(id)+'/resume',{method:'POST'});say('Retry started. Previously completed attempts and diagnostics remain in history.')})}
async function renderQuestionAttention(){const root=document.getElementById('question-attention');if(!root)return;root.replaceChildren();try{const data=await req('/api/application-questions/attention');if(!data.groups?.length){root.append(node('div','empty','No saved applications currently need additional answers.'));return}for(const group of data.groups){const card=node('article','app'),title=node('h3','',group.question);card.append(title,node('p','sub',(group.jobs.length===1?'1 saved job':'Shared across '+group.jobs.length+' saved jobs')+' · '+group.canonical_category.replaceAll('_',' ')));for(const job of group.jobs){const line=node('p','sub',job.company+' · '+job.title+' — '+job.question);card.append(line)}if(group.sensitive)card.append(node('p','sub','Sensitive or legal answer: stored encrypted, never automatically reused, and the employer may still require your action.'));const answer=document.createElement('textarea');answer.rows=3;answer.maxLength=4000;answer.required=true;answer.setAttribute('aria-label','Confirmed answer for '+group.question);answer.placeholder='Enter only an answer you have confirmed.';const scope=document.createElement('select');scope.setAttribute('aria-label','Answer reuse scope');for(const [value,label] of [['this_job','Use for this job only'],['matching_jobs','Reuse for matching jobs'],['general_preference','Save as a general preference']]){const option=document.createElement('option');option.value=value;option.textContent=label;scope.append(option)}if(group.sensitive||group.canonical_category==='relocation')scope.value='this_job';if(group.sensitive){for(const option of scope.options)if(option.value!=='this_job')option.disabled=true}if(group.canonical_category==='relocation'){for(const option of scope.options)if(option.value!=='this_job')option.disabled=true;card.append(node('p','sub','For future roles, set your structured relocation choice and destinations in Career Preferences.'))}const target=document.createElement('select');target.setAttribute('aria-label','Job for one-job reuse');for(const job of group.jobs){const option=document.createElement('option');option.value=job.job_key;option.textContent=job.company+' · '+job.title;target.append(option)}const confirmLabel=document.createElement('label'),confirm=document.createElement('input');confirm.type='checkbox';confirmLabel.append(confirm,document.createTextNode(' I confirm this answer is accurate.'));const save=node('button','primary','Save confirmed answer');const status=node('p','sub','');status.setAttribute('role','status');save.onclick=async()=>{if(!answer.value.trim()||!confirm.checked){status.textContent='Enter an answer and confirm that it is accurate.';return}save.disabled=true;status.textContent='Saving and verifying…';try{const result=await req('/api/application-questions/answer',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:group.question,answer:answer.value,application_ids:group.jobs.map(x=>x.application_id),reuse_scope:scope.value,job_key:scope.value==='this_job'?target.value:'',confirmed:true})});if(!result.persisted_verified)throw Error('The saved answer could not be verified.');status.textContent=result.requires_manual_action?'Saved encrypted for this job only. It will not be auto-filled; enter it yourself in the employer form when asked.':'Saved. No application was resumed; choose a dry run in Application history when ready.';say(result.message);await renderQuestionAttention()}catch(error){status.textContent=error.message;save.disabled=false}};card.append(answer,scope);if(!group.sensitive&&group.canonical_category!=='relocation'){const profileLink=node('a','sub','Review your profile and permissions');profileLink.href='/candidate-profile';card.append(profileLink)}if(group.jobs.length>1)card.append(target);card.append(confirmLabel,save,status);root.append(card)}}catch(error){root.append(node('p','sub','Could not load grouped application questions: '+error.message))}}
function renderHandoffs(items){const box=document.querySelector('#attention-inbox');if(!box)return;if(items.length)box.querySelector('.empty')?.remove();for(const item of items){const card=node('article','app');card.dataset.attentionId=item.attention_id;card.append(node('h3','',escapeText(item.title)),node('div','sub',escapeText(item.employer)),node('div','status','Needs Your Attention'));card.append(node('p','sub',escapeText(item.reason)));card.append(node('p','sub','Next action: '+escapeText(item.applicant_action)));card.append(node('p','sub','Eligibility: '+escapeText(item.eligibility_state)+' · Form: '+escapeText(item.form_state)+' · Preparation: '+escapeText(item.preparation_state==='not_prepared'?'not started':item.preparation_state.replaceAll('_',' '))));const link=node('a','sub','Open official employer application ↗');link.href=item.application_url;link.target='_blank';link.rel='noopener noreferrer';card.append(link);if(item.posting_url&&item.posting_url!==item.application_url){const posting=node('a','sub','View official posting ↗');posting.href=item.posting_url;posting.target='_blank';posting.rel='noopener noreferrer';posting.style.marginLeft='12px';card.append(posting)}card.append(node('p','sub',item.preparation_state==='not_prepared'?'No application attempt or preparation was created for this handoff.':'Review the existing preparation in Application history; this handoff did not create another attempt.'));box.append(card)}}
async function refresh(){try{const [apps,p,handoffs]=await Promise.all([req('/api/applications'),req('/api/applications/progress'),req('/api/applications/attention')]);renderSummary(apps,p);renderAttentionInbox(apps);renderHandoffs(handoffs.items||[]);renderApps(apps);await renderQuestionAttention()}catch(e){say(e.message)}}
document.querySelectorAll('.toolbar button').forEach(button=>button.addEventListener('click',()=>{if(busy)button.disabled=true}));refresh();setInterval(refresh,15000);const localTestHeading=[...document.querySelectorAll('.section-head')].find(x=>x.querySelector('h2')?.textContent==='Local test forms'),localTestToolbar=localTestHeading?.nextElementSibling;if(localTestToolbar){const answerTest=node('button','','Test AI-written answer');answerTest.onclick=()=>mock('written');localTestToolbar.append(answerTest)}
Object.assign(labels,{opening:'Opening employer website',inspecting:'Finding application form',filling:'Filling candidate details'});if(!document.querySelector('nav a[href="/application-settings"]')){const settingsNav=document.createElement('a');settingsNav.href='/application-settings';settingsNav.textContent='Application settings';document.querySelector('nav')?.append(settingsNav)}
const baseRenderApps=renderApps;renderApps=function(apps){baseRenderApps(apps);const cards=document.querySelectorAll('#apps .app');apps.forEach((app,index)=>{const drafts=app.diagnostic?.draft_answers;if(!Array.isArray(drafts)||!drafts.length)return;const card=cards[index];if(!card)return;const panel=document.createElement('details'),summary=document.createElement('summary');summary.textContent='Review AI-drafted answers';panel.append(summary);for(const draft of drafts){const question=node('p','sub',draft.question||'Application question'),answer=document.createElement('textarea');answer.value=draft.answer||'';answer.rows=4;answer.maxLength=1200;answer.setAttribute('aria-label','Edit AI-drafted answer');const reuse=document.createElement('label'),check=document.createElement('input');check.type='checkbox';reuse.append(check,document.createTextNode(' Allow automatic reuse after confirming'));const save=node('button','','Confirm and save answer');save.onclick=async()=>{try{save.disabled=true;await req('/api/application-answers/ai-'+Date.now().toString(36),{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:draft.question,answer:answer.value,confirmed:true,sensitive:false,answer_type:'generated',auto_reuse:check.checked})});save.textContent='Saved';say('Your reviewed answer was saved to the active profile.')}catch(e){save.disabled=false;say(e.message)}};panel.append(question,answer,reuse,save)}card.append(panel)})};
const previousAnswerReview=renderApps;renderApps=function(apps){previousAnswerReview(apps);const cards=document.querySelectorAll('#apps .app');apps.forEach((app,index)=>{const drafts=app.diagnostic?.draft_answers||[],card=cards[index];if(!card)return;const panel=[...card.querySelectorAll('details')].find(x=>x.querySelector('summary')?.textContent==='Review AI-drafted answers');if(!panel)return;for(const draft of drafts){const question=[...panel.querySelectorAll('p')].find(x=>x.textContent===(draft.question||'Application question'));if(!question)continue;const validation=draft.validation||{},summary=draft.status==='grounded_draft'?'Grounded draft · sources: '+(draft.evidence_used||[]).join(', '):'Review before use · '+(validation.problems||[]).join(' ');question.before(node('p','sub',summary))}})};
const baseDraftReview=renderApps;renderApps=function(apps){baseDraftReview(apps);const cards=document.querySelectorAll('#apps .app');apps.forEach((app,index)=>{const drafts=app.diagnostic?.draft_answers||[],card=cards[index];if(!card||app.state!=='needs_user_input')return;const panel=[...card.querySelectorAll('details')].find(x=>x.querySelector('summary')?.textContent==='Review AI-drafted answers');if(!panel)return;drafts.forEach((draft,draftIndex)=>{if(!(draft.validation?.problems||[]).length)return;const button=node('button','','Regenerate grounded draft');button.onclick=()=>runBusy('Regenerating with confirmed facts…',async()=>{const updated=await req('/api/applications/'+encodeURIComponent(app.id)+'/drafts/'+draftIndex+'/regenerate',{method:'POST'});say(updated.needs_user_input?'The new draft still needs your review: '+(updated.validation?.problems||[]).join(' '):updated.message);await refresh()});panel.append(button)})})};
const phase8SavedJobs=renderSavedJobs;renderSavedJobs=async function(silent=false){await phase8SavedJobs(silent);const result=await req('/api/applications/saved-jobs?'+new URLSearchParams({q:document.querySelector('#job-search').value,eligibility:document.querySelector('#job-eligibility').value}).toString());const cards=document.querySelectorAll('#jobs .job');(result.results||[]).forEach((job,index)=>{const card=cards[index];if(!card)return;card.querySelector('.job-missing')?.remove();if(job.eligibility_explanation)card.append(node('p','sub',job.eligibility_explanation));if(job.preparation_reason)card.append(node('p','sub',job.preparation_reason));const targetedReviewRequired=!!(job.evidence_review?.employment_type_unknown||(job.evidence_review?.ai_diagnostics||[]).length);if(!targetedReviewRequired&&!job.preparation_allowed&&['potential_match','needs_review'].includes(job.classification)&&job.application_state==='not_started'){const agree=document.createElement('input');agree.type='checkbox';agree.dataset.genericReview='true';const consent=document.createElement('label');consent.append(agree,document.createTextNode(' I reviewed this match; it is not a guarantee of eligibility.'));const review=node('button','','Confirm review');review.dataset.genericReviewButton='true';review.disabled=busy||!job.url_syntax_allowed||!agree.checked;agree.onchange=()=>{review.disabled=busy||!job.url_syntax_allowed||!agree.checked};review.onclick=()=>runBusy('Recording your review…',async()=>{const d=await req('/api/applications/saved-jobs/'+encodeURIComponent(job.job_key)+'/review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reviewed:agree.checked})});say(d.message);await renderSavedJobs(true)});card.append(consent,review)}})};
const phase8RenderApps=renderApps;renderApps=function(apps){phase8RenderApps(apps);const cards=document.querySelectorAll('#apps .app');apps.forEach((app,index)=>{if(app.state!=='needs_user_input')return;const card=cards[index];if(!card)return;const old=[...card.querySelectorAll('p')].find(p=>p.textContent.startsWith('Needs your input:'));if(old)old.remove();const questions=app.diagnostic?.unanswered_fields||[];const count=Number.isInteger(app.action_required_count)?app.action_required_count:questions.length;const summary=node('p','sub',count+' question(s) need your attention.');card.append(summary);const panel=document.createElement('details'),title=node('summary','','Resolve Questions');panel.append(title);const body=node('div');panel.append(body);panel.addEventListener('toggle',async()=>{if(!panel.open||panel.dataset.loaded)return;panel.dataset.loaded='1';try{const data=await req('/api/applications/'+encodeURIComponent(app.id)+'/questions');for(const group of data.groups||[]){body.append(node('h4','',group.category));for(const item of group.questions||[]){const question=node('p','sub',item.question);body.append(question);if(item.existing_answer)body.append(node('p','sub','Confirmed profile answer: '+item.existing_answer));const draft=(app.diagnostic?.draft_answers||[]).find(x=>item.question.includes(x.question)||x.question.includes(item.question));const input=document.createElement('textarea');input.rows=3;input.maxLength=4000;input.setAttribute('aria-label','Your answer for '+item.question);if(item.existing_answer)input.value=item.existing_answer;const useDraft=node('button','','Use grounded suggestion');if(draft){useDraft.onclick=()=>{input.value=draft.answer||''};body.append(node('p','sub','Suggested answer · review before saving'));body.append(useDraft)}const reuseLabel=document.createElement('label'),reuse=document.createElement('input');reuse.type='checkbox';reuse.checked=!!item.can_reuse&&!item.sensitive;reuse.disabled=!!item.sensitive;reuseLabel.append(reuse,document.createTextNode(' Save for reuse on similar questions'));const save=node('button','primary','Confirm answer');save.onclick=async()=>{save.disabled=true;try{const result=await req('/api/applications/'+encodeURIComponent(app.id)+'/answers',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:item.diagnostic_question||item.question,answer:input.value,reusable:reuse.checked,confirmed:true})});save.textContent='Saved';say(result.requires_manual_action?'Saved, but this employer still requires a human action.':'Answer confirmed and saved. Resume the dry run when ready.')}catch(e){save.disabled=false;say(e.message)}};body.append(input,reuseLabel,save);if(item.human_action_required)body.append(node('p','sub','This item cannot be completed automatically and may still require action on the employer website.'))}}}catch(e){body.append(node('p','sub',e.message))}});card.append(panel);const resume=node('button','primary','Resume Dry Run');resume.onclick=()=>retry(app.id);card.append(resume)})};
</script></body></html>'''

# Expose provider/form diagnostics in a compact review panel without repeating
# candidate answers, local storage paths, or employer form values.
APPLICATIONS_PAGE = APPLICATIONS_PAGE.replace("link.href=job.url;", "link.href=job.job_posting_url||job.url;")
APPLICATIONS_PAGE = APPLICATIONS_PAGE.replace("refresh();setInterval(refresh,15000);", "setTimeout(()=>refresh(),0);setInterval(()=>refresh(),15000);")
APPLICATIONS_PAGE = APPLICATIONS_PAGE.replace(
    "else if(['failed','paused','unsupported'].includes(job.application_state)&&job.application_id){const retryButton=node('button','primary','Retry dry run');retryButton.disabled=busy;retryButton.onclick=()=>retry(job.application_id);actions.append(retryButton)}",
    "else if(['failed','paused','unsupported'].includes(job.application_state)&&job.application_id){const historyLink=node('a','sub','Review recovery in Application history');historyLink.href='#apps';actions.append(historyLink)}")
APPLICATIONS_PAGE = APPLICATIONS_PAGE.replace("</body></html>", """<script>
const phase122RenderApps=renderApps;
renderApps=function(apps){
  phase122RenderApps(apps);
  const cards=document.querySelectorAll('#apps .app');
  apps.forEach((app,index)=>{
    const diagnostic=app.diagnostic||{},card=cards[index];
    if(!card)return;
    if(app.source_url){const link=node('a','sub','Open official posting');link.href=app.source_url;link.target='_blank';link.rel='noopener noreferrer';card.append(link)}
    if(app.application_url){const link=node('a','sub','Open official application page');link.href=app.application_url;link.target='_blank';link.rel='noopener noreferrer';card.append(link)}
    const panel=document.createElement('details'),summary=node('summary','','Preparation details');
    const body=node('div');
    const provider=diagnostic.provider||{};
    const status=diagnostic.form_detected?'Application form detected':(diagnostic.phase==='navigation'?'Opening application page':'Form not yet verified');
    body.append(node('p','sub','Provider: '+(provider.provider||'Not identified')+' · '+(provider.support_level||'compatibility not assessed')));
    body.append(node('p','sub',status+' · Steps inspected: '+(diagnostic.steps||0)));
    body.append(node('p','sub','Fields completed: '+(diagnostic.filled_fields||[]).filter(x=>x.result==='filled').length+' · Required fields: '+(diagnostic.required_fields||[]).length+' · Required items remaining: '+(diagnostic.unanswered_fields||[]).length));
    const uploaded=(diagnostic.filled_fields||[]).filter(x=>x.source==='private_document'||x.source==='active_resume').length;
    body.append(node('p','sub','Documents accepted by form: '+uploaded+' · AI drafts: '+(diagnostic.draft_answers||[]).length+' · Human actions: '+(diagnostic.human_actions_required||[]).length));
    if(diagnostic.authorization){const auth=diagnostic.authorization;body.append(node('p','sub','Job-specific authorization: '+(auth.status==='current'?'Current':'Needs renewal')+' · Authorized fields: '+(auth.selected_field_count||0)+' · Documents permitted: '+((auth.selected_document_types||[]).join(', ')||'None')+' · AI answers: '+(auth.ai_answers_authorized?'Authorized':'Not authorized')+' · Cloud AI: '+(auth.cloud_ai_authorized?'Authorized':'Not authorized')))}
    if(diagnostic.phase)body.append(node('p','sub','Current stage: '+String(diagnostic.phase).replaceAll('_',' ')+' · Last checkpoint: '+new Date(app.updated_at).toLocaleString()));
    const unresolved=diagnostic.unanswered_fields||[];
    if(unresolved.length){const list=document.createElement('ul');list.className='sub';unresolved.forEach(value=>{const item=document.createElement('li');item.textContent=value;list.append(item)});body.append(list)}
    if(diagnostic.human_actions_required?.length)body.append(node('p','sub','Employer or sensitive questions are waiting for your explicit action.'));
    if(diagnostic.submission_performed)body.append(node('p','sub','Unexpected submission state detected. This run must be reviewed.'));
    else body.append(node('p','sub','Final submission was not performed.'));
    panel.append(summary,body);card.append(panel);
  });
};
</script></body></html>""")





APPLICATIONS_PAGE = APPLICATIONS_PAGE.replace("</body></html>", """<script>
const phase133StableBaseRefresh=refresh;
let phase133SavedJobsSignature=null,phase133SavedJobsRendering=false,phase133SavedJobsPollInFlight=false,phase133SavedJobsEpoch=0;
function phase133HasActiveInteraction(){
  const active=document.activeElement;
  return !!((active&&active!==document.body&&active.closest('#jobs,#question-attention'))||
    document.querySelector('#jobs input:checked,#question-attention input:checked')||
    document.querySelector('#jobs details[open],#apps details[open],#question-attention details[open]'));
}
function phase133JobsSignature(data){return JSON.stringify((data.results||[]).map(job=>({
  job_key:job.job_key,title:job.title,company:job.company,location:job.location,employment_type:job.employment_type,
  url:job.url,application_url:job.application_url,classification:job.classification,match_score:job.match_score,eligibility_stale:job.eligibility_stale,
  match_updated_at:job.match_updated_at,application_state:job.application_state,preparation_allowed:job.preparation_allowed,
  preparation_reason:job.preparation_reason,evidence_review:job.evidence_review,targeted_evidence_review:job.targeted_evidence_review,
  deterministic_reassessment_available:job.deterministic_reassessment_available,application_message:job.application_message,
  form_inspection:job.form_inspection,missing_questions:job.missing_questions,application_status_label:job.application_status_label
})))}
function phase133CaptureSavedJobState(){
  const state=new Map();
  document.querySelectorAll('#jobs .job[data-job-key]').forEach(card=>state.set(card.dataset.jobKey,{
    details:[...card.querySelectorAll('details')].map(item=>item.open),
    checks:[...card.querySelectorAll('input[type=checkbox]')].map(item=>item.checked),
    values:[...card.querySelectorAll('input:not([type=checkbox]),textarea,select')].map(item=>item.value)
  }));
  return state;
}
function phase133RestoreSavedJobState(state){
  document.querySelectorAll('#jobs .job[data-job-key]').forEach(card=>{
    const prior=state.get(card.dataset.jobKey);if(!prior)return;
    card.querySelectorAll('details').forEach((item,index)=>{if(index<prior.details.length)item.open=prior.details[index]});
    card.querySelectorAll('input[type=checkbox]').forEach((item,index)=>{if(index<prior.checks.length){item.checked=prior.checks[index];item.dispatchEvent(new Event('change',{bubbles:true}))}});
    card.querySelectorAll('input:not([type=checkbox]),textarea,select').forEach((item,index)=>{if(index<prior.values.length)item.value=prior.values[index]});
  });
}
refresh=async function(){
  if(busy||phase133HasActiveInteraction())return;
  const loaded=jobsLoaded,scrollY=window.scrollY;jobsLoaded=false;
  try{await phase133StableBaseRefresh()}finally{jobsLoaded=loaded;if(window.scrollY!==scrollY)window.scrollTo(window.scrollX,scrollY)}
};
async function phase133RefreshSavedJobsIfChanged(){
  if(!jobsLoaded||busy||phase133HasActiveInteraction()||phase133SavedJobsRendering||phase133SavedJobsPollInFlight)return;
  phase133SavedJobsPollInFlight=true;const epoch=++phase133SavedJobsEpoch;
  try{
    const params=new URLSearchParams({q:document.querySelector('#job-search').value,eligibility:document.querySelector('#job-eligibility').value});
    const data=await req('/api/applications/saved-jobs?'+params.toString());if(epoch!==phase133SavedJobsEpoch)return;
    const signature=phase133JobsSignature(data);if(phase133SavedJobsSignature===null){phase133SavedJobsSignature=signature;return}
    if(signature!==phase133SavedJobsSignature)await renderSavedJobs(true);
  }catch(error){say('Saved jobs could not refresh: '+(error.message||'Please try again.'))}
  finally{phase133SavedJobsPollInFlight=false}
}
setInterval(phase133RefreshSavedJobsIfChanged,15000);
</script></body></html>""")

# Read-only compatibility inspection is kept separate from dry-run preparation.
APPLICATIONS_PAGE = APPLICATIONS_PAGE.replace("</body></html>", """<script>
const phase123RenderSavedJobs=renderSavedJobs;
function appendInspectionReport(card,report){
  const labels={compatible:'Compatible',partially_compatible:'Partially compatible',needs_human_action:'Needs human action',unsupported:'Unsupported',inaccessible:'Inaccessible',posting_only:'Posting only',inspection_blocked:'Inspection blocked',unresolved:'Unresolved'};
  const effectiveStatus=report.compatibility_status||(report.failure_category?'inspection_blocked':report.page_classification==='job_posting'?'posting_only':report.inspection_outcome);
  const panel=document.createElement('details'),summary=node('summary','', 'Form compatibility · '+(labels[effectiveStatus]||'Not yet inspected'));panel.className='form-inspection-result';
  const body=node('div','sub');
  body.append(node('p','sub','Read-only inspection · no candidate information entered · no application prepared or submitted.'));
  const pageMeta=report.page_metadata||{},titleMatch=String(pageMeta.page_title||'').match(/^Job Application for .+ at (.+)$/i),employer=report.employer||pageMeta.employer||(titleMatch?.[1]||''),jobTitle=report.job_title||pageMeta.job_title||'';
  if(employer||jobTitle){const employerSource=pageMeta.employer_source||(titleMatch?'document_title':'');const source=report.employer?'Saved employer and job':'Public page metadata'+(employerSource||pageMeta.title_source?' · source: '+[employerSource,pageMeta.title_source].filter(Boolean).join(', '):'');body.append(node('p','sub',source+': '+(employer||'Employer not identified')+' · '+(jobTitle||'Job title not identified')))}
  if(report.inspected_at||report.inspection_timestamp)body.append(node('p','sub','Inspected: '+new Date(report.inspected_at||report.inspection_timestamp).toLocaleString()));
  const failureLabels={dns_failure:'DNS lookup failed',timeout:'Request timed out',tls_error:'Secure connection failed',tls_configuration:'TLS trust configuration unavailable',redirect_blocked:'Unsafe redirect blocked',http_status:'Robots server returned an HTTP error',policy_missing:'robots.txt was not found',malformed_policy:'Robots policy could not be parsed',policy_disallowed:'Path disallowed by robots.txt',unsafe_destination:'Unsafe destination blocked',network_error:'Network request failed',navigation_error:'Public page could not be opened',read_only_method_blocked:'A non-read-only navigation was blocked'};
  if(report.failure_category)body.append(node('p','inspection-diagnostic','Diagnostic: '+(failureLabels[report.failure_category]||'Inspection blocked')));
  if(report.robots_policy==='verified_allow')body.append(node('p','inspection-diagnostic','Robots policy verified for this path.'));
  const pageKinds={application_form:'Application form',job_posting:'Job posting only · no application form exposed',posting_redirected:'Job URL redirected · posting not verified',public_form_unverified:'Public form found · application purpose unverified',public_page_unverified:'Public page · application form unverified'};
  body.append(node('p','sub','Page classification: '+(pageKinds[report.page_classification]||'Not verified')));
  body.append(node('p','sub','Provider: '+(report.provider?.provider||'Not identified')+' · '+(report.provider?.support_level||'compatibility unknown')+' · Form detected: '+(report.form_detected?'Yes':'No')));
  const submitStatus={detected_visible:'Detected · visible',detected_hidden:'Detected · hidden',ambiguous_unresolved:'Ambiguous · safety unresolved',not_detected_unresolved:'Not detected · safety unresolved'};
  const controls=report.total_dom_control_count??report.fields?.length??0,unresolvedCount=report.unresolved_visible_control_count??report.unresolved_controls?.length??0;
  body.append(node('p','sub','Questions: '+(report.unique_question_count??(report.questions||[]).length)+' · Required questions: '+(report.unique_required_question_count??(report.required_fields||[]).length)+' · Optional questions: '+(report.unique_optional_question_count??0)+' · Requirement unknown: '+(report.unique_unknown_requirement_question_count??0)));
  body.append(node('p','sub','Visible controls: '+(report.visible_form_control_count??controls)+' · All DOM controls: '+controls+' · Required visible controls: '+(report.required_control_count??(report.required_fields||[]).length)+' · Unresolved controls: '+unresolvedCount));
  const uploadDetails=report.upload_control_details||[];body.append(node('p','sub','Upload questions: '+uploadDetails.length+' · Verified file mechanisms: '+uploadDetails.filter(item=>item.supported).length+' · Unresolved uploads: '+uploadDetails.filter(item=>!item.supported).length+' · Multi-step: '+(report.multi_step_detected?'Detected':'Not detected')+' · Submission control: '+(submitStatus[report.submission_control_status]||'Not verified')));
  if(report.blocked_non_read_only_request_count)body.append(node('p','inspection-diagnostic','Blocked non-read-only background requests: '+report.blocked_non_read_only_request_count+' · Only GET/HEAD/OPTIONS requests were allowed.'));
  if(report.questions?.length){body.append(node('strong','','Applicant questions'));const fields=document.createElement('ul');report.questions.forEach(question=>{const req=question.required===true?'required':question.required===false?'optional':'requirement unknown';const types=(question.controls||[]).map(control=>[control.kind,control.input_type&&control.input_type!==control.kind?'native '+control.input_type:'',control.role?'role '+control.role:'',control.upload_purpose?'purpose '+control.upload_purpose:'',control.accept?'accept '+control.accept:''].filter(Boolean).join(' · ')).filter(Boolean);fields.append(node('li','',(question.question||'Unresolved question')+' · '+req+' · controls: '+(types.length?types.join(', '):(question.control_types||[]).join(', '))+(question.question_id?' · ID: '+question.question_id:'')))});body.append(fields)}
  if(report.unresolved_controls?.length){body.append(node('strong','','Unresolved controls · may block preparation'));const fields=document.createElement('ul');report.unresolved_controls.forEach(item=>fields.append(node('li','',(item.question||'Unlabeled control')+' · '+(item.kind||'unknown')+' · '+(item.required===true?'required':item.required==='unknown'?'required status unknown':'optional')+' · '+(item.reasons||[]).join('; '))));body.append(fields)}
  if(report.ignored_controls?.length){body.append(node('strong','','Hidden or non-visible controls excluded from question counts'));const fields=document.createElement('ul');report.ignored_controls.forEach(item=>fields.append(node('li','',(item.tag||'control')+(item.input_type?' · '+item.input_type:'')+(item.role?' · '+item.role:'')+' · '+(item.reason||'not visible'))));body.append(fields)}
  const uploads=report.upload_control_details||report.upload_prompts||[];
  if(uploads.length){body.append(node('strong','','Upload controls'));const fields=document.createElement('ul');uploads.forEach(item=>{const status=item.supported?'supported native input':item.association_method==='ambiguous'?'ambiguous input mapping':'unresolved input';const types=(item.accepted_types||[]).join(', ')||'file types unspecified';fields.append(node('li','',(item.question||'Unresolved file upload')+' · purpose: '+(item.purpose||'unknown')+' · required: '+(item.required===true?'yes':item.required===false?'no':'unknown')+' · '+status+' · mechanism: '+(item.mechanism||'unknown')+' · accepted: '+types+(item.input_id?' · input id: '+item.input_id:'')+(item.association_method?' · associated by: '+item.association_method:'')+(item.unresolved_reason?' · '+item.unresolved_reason:'')))});body.append(fields)}
  if(report.submission_controls?.length){body.append(node('strong','','Submission boundary controls'));const fields=document.createElement('ul');report.submission_controls.forEach(item=>fields.append(node('li','',(item.label||item.aria_label||'Unlabeled control')+' · '+(item.action==='final_submission'?'final submission candidate':'unresolved action')+' · '+(item.type||'type unknown')+' · '+(item.enabled?'enabled':'disabled')+' · '+(item.inside_form?'inside form':'form association unknown')+(item.form_id?' · form id: '+item.form_id:'')+(item.form_action_path?' · form action path: '+item.form_action_path:''))));body.append(fields)}
  if(report.reason)body.append(node('p','sub',report.reason));
  for(const [title,items] of [['File upload questions',report.file_upload_controls],['Supported fields',report.supported_fields],['Unsupported fields',report.unsupported_fields],['Human actions required',report.human_actions_required]])if(items?.length){body.append(node('strong','',title));const list=document.createElement('ul');items.forEach(x=>list.append(node('li','',x)));body.append(list)}
  if(report.posting_url){const link=node('a','sub','Open official posting');link.href=report.posting_url;link.target='_blank';link.rel='noopener noreferrer';body.append(link)}
  if(report.canonical_application_url||report.application_url){const link=node('a','sub','Open inspected application page');link.href=report.canonical_application_url||report.application_url;link.target='_blank';link.rel='noopener noreferrer';body.append(link)}
  panel.append(summary,body);card.append(panel);
}
const standaloneForm=document.querySelector('#standalone-inspection-form');
standaloneForm?.addEventListener('submit',async event=>{event.preventDefault();const input=document.querySelector('#standalone-inspection-url'),button=document.querySelector('#standalone-inspection-submit'),status=document.querySelector('#standalone-inspection-status'),result=document.querySelector('#standalone-inspection-result');if(!input.reportValidity())return;button.disabled=true;button.setAttribute('aria-busy','true');status.textContent='Checking robots.txt and inspecting the public page in read-only mode…';result.replaceChildren();try{const report=await req('/api/applications/form-compatibility-tests',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({application_url:input.value.trim()})});const card=node('article','job');appendInspectionReport(card,report);result.append(card);status.textContent='Compatibility report saved. No candidate data was used and no application was prepared or submitted.'}catch(error){status.textContent=error.message||'Compatibility inspection failed. Check the public URL and try again.'}finally{button.disabled=false;button.removeAttribute('aria-busy')}});
renderSavedJobs=async function(silent=false){
  await phase123RenderSavedJobs(silent);
  const data=await req('/api/applications/saved-jobs?'+new URLSearchParams({q:document.querySelector('#job-search').value,eligibility:document.querySelector('#job-eligibility').value}).toString());
  const cards=document.querySelectorAll('#jobs .job');
    (data.results||[]).forEach((job,index)=>{
    const card=cards[index];if(!card)return;card.dataset.jobKey=job.job_key;
    if(job.targeted_evidence_review?.recorded){card.append(node('p','sub job-message','Job-specific eligibility review saved '+(job.targeted_evidence_review.reviewed_at?'on '+new Date(job.targeted_evidence_review.reviewed_at).toLocaleString():'')+'. Application preparation still requires separate job-specific authorization.'));return}
    if(job.form_inspection)appendInspectionReport(card,job.form_inspection);
    if(job.classification!=='potential_match'||job.eligibility_stale||job.match_status!=='completed'||job.freshness_status!=='current'||!job.active||!job.url_syntax_allowed)return;
    if(!job.form_inspection)card.append(node('div','sub','Compatibility: Not yet inspected'));
    const button=node('button','primary form-inspect-action', 'Inspect Employer Form');button.setAttribute('aria-label','Inspect Employer Form for '+(job.title||'saved job'));
    window.bsaInspectionPending=window.bsaInspectionPending||new Set();
    const pending=window.bsaInspectionPending;
    button.title='Read-only inspection. This will not enter candidate information or prepare or submit an application.';
    button.disabled=busy||pending.size>0;
    if(pending.has(job.job_key)){button.textContent='Inspecting public form…';button.setAttribute('aria-busy','true')}
    button.onclick=async()=>{if(pending.size)return;pending.add(job.job_key);button.disabled=true;button.textContent='Inspecting public form…';button.setAttribute('aria-busy','true');try{const report=await req('/api/applications/form-inspections',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({job_key:job.job_key})});card.querySelector('details.form-inspection-result')?.remove();appendInspectionReport(card,report);say('Form inspection finished: '+(report.inspection_outcome||'result available')+'. No application was prepared or submitted.')}catch(e){say('Form inspection could not start: '+e.message)}finally{pending.delete(job.job_key);await renderSavedJobs(true)}};
    card.append(button);
  });
};
</script></body></html>""")

APPLICATIONS_PAGE = APPLICATIONS_PAGE.replace("</head>", """<style>
.form-inspection-result{overflow-wrap:anywhere}.form-inspection-result summary{color:#405570}.form-inspect-action:focus-visible,.form-inspection-result summary:focus-visible{outline:3px solid #2563eb;outline-offset:2px}
.compatibility-tool{margin:22px 0;padding:18px;background:var(--bsa-surface);color:var(--bsa-ink);border:1px solid var(--bsa-line);border-radius:14px;box-shadow:0 2px 8px #102a4308}.compatibility-tool label{display:block;color:var(--bsa-ink);font-weight:650;margin:8px 0}.compatibility-input{display:flex;gap:9px;flex-wrap:wrap}.compatibility-input input{flex:1 1 360px;min-width:0;padding:10px 12px;border:1px solid var(--bsa-line);border-radius:8px;background:var(--bsa-surface);color:var(--bsa-ink);font:inherit}.compatibility-tool button{background:var(--bsa-surface);color:var(--bsa-ink);border-color:var(--bsa-line)}.compatibility-tool button.primary{background:var(--brand);color:#fff;border-color:var(--brand)}.compatibility-tool button:focus-visible,.compatibility-input input:focus-visible{outline:3px solid #2563eb;outline-offset:2px}.inspection-diagnostic{font-weight:650}
html[data-bsa-theme="dark"] .form-inspection-result summary{color:#e7edf6}
</style></head>""")

APPLICATIONS_PAGE = APPLICATIONS_PAGE.replace("</body></html>", """<script>
const phase124RenderSavedJobs=renderSavedJobs;
function addAuthorizationControl(card,job){
  if(job.preparation_authorized){
    card.append(node('span','pill','Job-specific authorization current'));
    const revoke=node('button','','Revoke this job authorization');
    revoke.onclick=async()=>{revoke.disabled=true;try{const result=await req('/api/applications/saved-jobs/'+encodeURIComponent(job.job_key)+'/preparation-authorization',{method:'DELETE'});say(result.message);await renderSavedJobs(true)}catch(error){revoke.disabled=false;say(error.message)}};
    card.append(revoke);return;
  }
  if(!job.preparation_authorization_required)return;
  const open=node('button','primary','Authorize Preparation');
  open.title='Review employer, exact URL, selected candidate fields, documents, and AI processing before authorizing.';
  open.onclick=async()=>{
    open.disabled=true;open.textContent='Loading authorization plan…';
    try{
      const plan=await req('/api/applications/saved-jobs/'+encodeURIComponent(job.job_key)+'/preparation-authorization');
      const panel=node('section','authorization-panel');panel.setAttribute('aria-label','Job-specific preparation authorization');
      panel.append(node('h4','', 'Authorize preparation for this job'));
      panel.append(node('p','sub','Employer: '+plan.employer+' · Job: '+plan.title));
      panel.append(node('p','sub','Exact application URL: '+plan.application_url));
      panel.append(node('p','sub','Provider: '+(plan.provider?.provider||'Employer site')+' · Eligibility: '+plan.eligibility));
      if(plan.inspection)appendInspectionReport(panel,plan.inspection);
      if(plan.inspection_blocker){panel.append(node('p','authorization-error',plan.inspection_blocker));card.append(panel);open.remove();return}
      panel.append(node('p','sub','Choose the confirmed profile fields BabyStepsAI may enter. Values are not displayed here.'));
      const fields=node('div','authorization-options');
      for(const item of plan.candidate_fields||[]){const label=document.createElement('label'),check=document.createElement('input');check.type='checkbox';check.dataset.fieldKey=item.key;label.append(check,document.createTextNode(' '+item.label+(item.sensitive?' · sensitive':'') ));fields.append(label)}
      panel.append(fields);
      panel.append(node('p','sub','Choose which authentic files may be uploaded. Resume and additional documents require separate selection.'));
      const docs=node('div','authorization-options');
      for(const item of plan.available_documents||[]){const label=document.createElement('label'),check=document.createElement('input');check.type='checkbox';check.dataset.documentType=item.type;label.append(check,document.createTextNode(' '+item.label));docs.append(label)}
      panel.append(docs);
      const aiLabel=document.createElement('label'),ai=document.createElement('input');ai.type='checkbox';ai.disabled=!plan.ai_drafting_enabled;aiLabel.append(ai,document.createTextNode(' Allow grounded AI answers to be inserted for ordinary written questions'+(plan.ai_drafting_enabled?'':' · enable AI drafting in Application Settings first')));panel.append(aiLabel);
      if(['gemini','groq'].includes(plan.provider_selected)){
        const cloudLabel=document.createElement('label'),cloud=document.createElement('input');cloud.type='checkbox';cloud.dataset.cloudAi='true';
        cloud.disabled=!plan.cloud_provider_configured||(plan.provider_selected==='groq'&&!plan.groq_cloud_permission_available);
        const providerLabel=plan.provider_selected==='groq'?'Groq':'Google Gemini';
        const unavailable=!plan.cloud_provider_configured?' · '+providerLabel+' is not configured':(plan.provider_selected==='groq'&&!plan.groq_cloud_permission_available?' · separately enable Groq cloud permission in Application Settings first':'');
        cloudLabel.append(cloud,document.createTextNode(' Separately authorize sending the selected facts and this job description to '+providerLabel+unavailable));panel.append(cloudLabel);
      }
      const confirmLabel=document.createElement('label'),confirm=document.createElement('input');confirm.type='checkbox';confirmLabel.append(confirm,document.createTextNode(' I authorize BabyStepsAI to use only the selected fields and files for this employer and exact URL, and to stop before final submission. I will handle legal acknowledgments and human verification.'));panel.append(confirmLabel);
      const status=node('p','sub','This authorization expires when the job URL, match, resume/profile, confirmed answers, documents, or settings version changes.');panel.append(status);
      const save=node('button','primary','Save Job-Specific Authorization');save.disabled=true;confirm.onchange=()=>{save.disabled=!confirm.checked};
      save.onclick=async()=>{save.disabled=true;try{
        const field_keys=[...fields.querySelectorAll('[data-field-key]:checked')].map(x=>x.dataset.fieldKey);
        const document_types=[...docs.querySelectorAll('[data-document-type]:checked')].map(x=>x.dataset.documentType);
        await req('/api/applications/saved-jobs/'+encodeURIComponent(job.job_key)+'/preparation-authorization',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirmed:confirm.checked,confirmed_employer:plan.employer,confirmed_title:plan.title,confirmed_application_url:plan.application_url,field_keys,document_types,allow_ai_answers:ai.checked,allow_cloud_ai:!!panel.querySelector('input[type=checkbox][data-cloud-ai]:checked')})});
        say('Job-specific authorization saved. Preparation remains a dry run and will stop before submission.');await renderSavedJobs(true);
      }catch(error){save.disabled=false;status.textContent=error.message}}
      panel.append(save);card.append(panel);open.remove();
    }catch(error){say(error.message);open.disabled=false;open.textContent='Authorize Preparation'}
  };
  card.append(open);
}
renderSavedJobs=async function(silent=false){
  await phase124RenderSavedJobs(silent);
  const data=await req('/api/applications/saved-jobs?'+new URLSearchParams({q:document.querySelector('#job-search').value,eligibility:document.querySelector('#job-eligibility').value}).toString());
  const cards=document.querySelectorAll('#jobs .job');
  (data.results||[]).forEach((job,index)=>{const card=cards[index];if(card)addAuthorizationControl(card,job)});
};
</script></body></html>""")

APPLICATIONS_PAGE = APPLICATIONS_PAGE.replace("check.type='checkbox';cloudLabel.append(check,document.createTextNode(","check.type='checkbox';check.dataset.cloudAi='true';cloudLabel.append(check,document.createTextNode(")
APPLICATIONS_PAGE = APPLICATIONS_PAGE.replace(".form-inspection-result{overflow-wrap:anywhere}",".form-inspection-result{overflow-wrap:anywhere}.authorization-panel{width:100%;padding:14px;border:1px solid var(--bsa-line);border-radius:10px;background:var(--bsa-surface);color:var(--bsa-ink)}.authorization-panel h4{margin:4px 0}.authorization-panel label{display:block;margin:8px 0;color:var(--bsa-ink)}.authorization-options{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,230px),1fr));gap:4px}.authorization-error{color:#a12b32;font-weight:650}")

APPLICATIONS_PAGE = APPLICATIONS_PAGE.replace("</body></html>", """<script>
const phase133PriorSavedJobs=renderSavedJobs;
renderSavedJobs=async function(silent=false){
  await phase133PriorSavedJobs(silent);
  const data=await req('/api/applications/saved-jobs?'+new URLSearchParams({q:document.querySelector('#job-search').value,eligibility:document.querySelector('#job-eligibility').value}).toString());
  const cards=document.querySelectorAll('#jobs .job');
  (data.results||[]).forEach((job,index)=>{
    const card=cards[index];if(!card)return;
    if(job.deterministic_reassessment_available){
      const refresh=node('button','','Reassess saved evidence without AI');
      refresh.setAttribute('aria-label','Deterministically reassess '+(job.company||'')+' '+(job.title||'')+' without an AI call');
      refresh.onclick=()=>runBusy('Reassessing confirmed facts…',async()=>{
        const result=await req('/api/applications/saved-jobs/'+encodeURIComponent(job.job_key)+'/deterministic-reassessment',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({match_updated_at:job.evidence_review?.snapshot?.match_updated_at||job.match_updated_at})});
        say(result.message);await renderSavedJobs(true);
      });card.append(refresh);
    }
    const review=job.evidence_review;if(!review)return;
    const diagnostics=review.ai_diagnostics||[];
    const targetedRequired=!!(review.employment_type_unknown||review.role_seniority_unknown||diagnostics.length);
    if(!review.available){if(targetedRequired){const blocked=node('p','sub job-message',review.blocked_reason||'This job-specific review is not available yet.');blocked.setAttribute('role','status');card.append(blocked)}return}
    const panel=document.createElement('details');panel.className='evidence-review';
    const summary=node('summary','','Resolve eligibility evidence review');panel.append(summary);
    panel.append(node('p','sub','This review does not change job facts or authorize application preparation. Confirm only that you accept the listed uncertainty for this job.'));
    if(review.employment_type_unknown){
      panel.append(node('p','sub','The official posting does not state an employment type. It remains unknown; the FTE benefits language is not being used to infer full-time status.'));
    }
    if(review.role_seniority_unknown){
      panel.append(node('p','sub','The verified posting does not state a clear seniority level. This choice will not assert that the role is entry-level; it only records whether you want to consider the job while level remains unknown.'));
    }
    if(diagnostics.length){
      panel.append(node('p','sub','AI-authored diagnostics (not posting evidence):'));
      const list=document.createElement('ul');diagnostics.forEach(value=>list.append(node('li','sub',value)));panel.append(list);
    }
    const checks=[],decision={employment:!review.employment_type_unknown,seniority:!review.role_seniority_unknown,diagnostics:diagnostics.length===0};
    if(review.employment_type_unknown){const input=document.createElement('input');input.type='checkbox';const label=document.createElement('label');label.append(input,document.createTextNode(' I accept considering this job while its employment type remains unknown.'));panel.append(label);checks.push(input);input.onchange=()=>{decision.employment=input.checked;update()}}
    if(review.role_seniority_unknown){const input=document.createElement('input');input.type='checkbox';const label=document.createElement('label');label.append(input,document.createTextNode(' I want to consider this job while its seniority remains unknown; I am not confirming it is entry-level.'));panel.append(label);checks.push(input);input.onchange=()=>{decision.seniority=input.checked;update()}}
    if(diagnostics.length){const input=document.createElement('input');input.type='checkbox';const label=document.createElement('label');label.append(input,document.createTextNode(' I reviewed these AI diagnostics and understand they do not establish a mandatory qualification conflict.'));panel.append(label);checks.push(input);input.onchange=()=>{decision.diagnostics=input.checked;update()}}
    const status=node('p','sub','No application will be prepared by resolving this review.');
    const confirm=node('button','primary','Save this job-specific review');confirm.disabled=true;
    const update=()=>confirm.disabled=checks.some(input=>!input.checked);
    checks.forEach(input=>input.addEventListener('change',update));
    confirm.onclick=()=>runBusy('Saving your review…',async()=>{
      confirm.disabled=true;
      try{const result=await req('/api/applications/saved-jobs/'+encodeURIComponent(job.job_key)+'/resolve-evidence-review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...review.snapshot,accept_unspecified_employment_type:decision.employment,accept_unclear_role_seniority:decision.seniority,acknowledge_ai_diagnostics:decision.diagnostics})});
        status.textContent='Saved. Verifying the decision from the server…';
        const persisted=await req('/api/applications/saved-jobs?'+new URLSearchParams({q:job.company||'',eligibility:'all'}).toString());
        const saved=(persisted.results||[]).find(item=>item.job_key===job.job_key);
        if(!saved?.targeted_evidence_review?.recorded||saved.classification!=='potential_match')throw Error('The review response was received, but the saved decision could not be verified. Refresh the page and check the job state.');
        status.textContent=result.message;say(result.message);await renderSavedJobs(true);
      }catch(error){status.textContent=error.message;confirm.disabled=false}
    });
    panel.append(confirm,status);card.append(panel);
  });
};
</script></body></html>""")

APPLICATIONS_PAGE = APPLICATIONS_PAGE.replace("</body></html>", """<script>
const phase133FinalSavedJobsRenderer=renderSavedJobs;
renderSavedJobs=async function(silent=false){
  if(phase133SavedJobsRendering)return;
  if(phase133HasActiveInteraction()&&!busy)return;
  phase133SavedJobsRendering=true;
  const epoch=++phase133SavedJobsEpoch,box=document.querySelector('#jobs'),scrollY=window.scrollY,state=phase133CaptureSavedJobState();
  try{
    const params=new URLSearchParams({q:document.querySelector('#job-search').value,eligibility:document.querySelector('#job-eligibility').value});
    const data=await req('/api/applications/saved-jobs?'+params.toString());if(epoch!==phase133SavedJobsEpoch)return;
    const signature=phase133JobsSignature(data);
    if(signature===phase133SavedJobsSignature&&box.children.length){if(!silent)say((data.results||[]).length+' saved openings shown.');return}
    await phase133FinalSavedJobsRenderer(silent);if(epoch!==phase133SavedJobsEpoch)return;
    const cards=box.querySelectorAll('.job');(data.results||[]).forEach((job,index)=>{
      const card=cards[index];if(!card)return;card.dataset.jobKey=job.job_key;
      const submit=card.querySelector('button');
      if(submit?.textContent==='Save this job-specific review'&&!submit.dataset.stableWrapped){
        const original=submit.onclick;submit.dataset.stableWrapped='true';
        submit.onclick=async event=>{const result=original?.call(submit,event);if(result&&typeof result.then==='function')await result};
      }
    });
    phase133RestoreSavedJobState(state);phase133SavedJobsSignature=signature;window.scrollTo(window.scrollX,scrollY);
  }catch(error){say('Saved jobs could not refresh: '+(error.message||'Please try again.'))}
  finally{phase133SavedJobsRendering=false}
};
</script></body></html>""")

MOCK_PAGES={
"simple":'''<!doctype html><title>Mock simple</title><h1>Software Developer Application</h1><form><label>Full Name <input name="name" required></label><label>Email <input name="email" type="email" required></label><label>Phone <input name="phone" required></label><label>LinkedIn URL <input name="linkedin"></label><label>GitHub URL <input name="github"></label><label>Resume <input name="resume" type="file" required></label><button type="submit">Submit application</button></form>''',
"multi-step":'''<!doctype html><title>Mock multi step</title><h1>Application</h1><section id="one"><label>Full name <input name="name" required></label><label>Email <input name="email" type="email" required></label><button type="button" onclick="one.hidden=true;two.hidden=false">Next</button></section><section id="two" hidden><label>University <input name="university" required></label><label>Degree <input name="degree" required></label><label>Graduation year <input name="graduation_year" required></label><button type="button" onclick="two.hidden=true;three.hidden=false">Continue to work experience</button></section><section id="three" hidden><label>Work experience <textarea name="experience" required></textarea></label><button type="button">Review application</button><button type="submit">Submit application</button></section>''',
"uncertain":'''<!doctype html><title>Mock uncertain questions</title><h1>Application questions</h1><label>Are you legally authorized to work in the United States? <select required><option value="">Choose</option><option>Yes</option><option>No</option></select></label><label>Will you require sponsorship? <select required><option value="">Choose</option><option>Yes</option><option>No</option></select></label><label>Are you willing to relocate? <input required></label><label>Salary expectations <input required></label><label>Why do you want to work here? <textarea required></textarea></label><button type="submit">Submit</button>''',
"validation":'''<!doctype html><title>Mock validation</title><h1>Validation test</h1><label>Email <input name="email" type="email" required></label><label>Required custom question <input required></label><label>Resume (.docx only) <input name="resume" type="file" accept=".docx" required></label><label>Conditional response <input required></label><button type="submit">Submit</button>''',
"captcha":'''<!doctype html><title>Mock human verification</title><h1>Application</h1><p>Human verification required: CAPTCHA challenge simulation.</p><div role="img" aria-label="CAPTCHA">Mock CAPTCHA</div><button>Submit</button>''',
"job-page":'''<!doctype html><title>Mock job posting</title><h1>Software Developer</h1><p>Job description.</p><a href="/mock-apps/simple">Apply Now</a>''',
"apply-button":'''<!doctype html><title>Mock job posting</title><h1>Software Developer</h1><p>Job description.</p><button type="button" onclick="location.href='/mock-apps/simple'">Apply Now</button>''',
"weird-submit":'''<!doctype html><title>Mock ambiguous application</title><h1>Application</h1><label>Full Name <input name="name" required></label><button type="submit">Continue</button>''',
"choices":'''<!doctype html><title>Mock choices</title><h1>Application questions</h1><label>Technical skills <input type="checkbox" name="technical_skills" value="Python">Python</label><fieldset><legend>Are you willing to relocate?</legend><label><input type="radio" name="relocation" value="yes" required>Yes</label><label><input type="radio" name="relocation" value="no" required>No</label></fieldset>''',
"auth":'''<!doctype html><title>Mock sign in</title><h1>Sign in to continue your application</h1><form><label>Email <input type="email"></label><label>Password <input type="password"></label></form>''',
"expired":'''<!doctype html><title>Mock expired listing</title><h1>Job expired</h1><p>This position is no longer accepting applications.</p>''',
"apply-loop":'''<!doctype html><title>Mock apply loop</title><h1>Job posting</h1><a href="/mock-apps/apply-loop">Apply</a>''',
"written":'''<!doctype html><title>Mock AI-written answer</title><h1>Application questions</h1><form><label>Describe a relevant technical project and the technologies used (maximum 120 words) <textarea name="project" maxlength="600" required></textarea></label><button type="submit">Submit application</button></form>''',
}

APPLICATIONS_PAGE = APPLICATIONS_PAGE.replace("</body></html>", """<style>
.application-card{display:flex;flex-direction:column;align-items:stretch;gap:12px;min-width:0;padding:20px;border-color:var(--bsa-line);background:var(--bsa-surface);color:var(--bsa-ink)}
.application-card h3{font-size:19px;line-height:1.32;overflow-wrap:anywhere;word-break:normal;margin:0}.application-card .app-company{font-weight:600;color:var(--bsa-muted)}
.application-card .status{display:flex;width:max-content;max-width:100%;padding:5px 10px;border:1px solid var(--bsa-line);border-radius:999px;margin:0;color:var(--bsa-ink)}
.application-card .status[data-state=ready_for_review]{border-color:#16804a}.application-card .status[data-state=failed],.application-card .status[data-state=unsupported]{border-color:#b33b43}
.application-card .bar{width:100%;height:8px}.application-card .app-timing,.application-card .app-checkpoint{font-size:13px;color:var(--bsa-muted);margin:0}
.application-readiness{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:2px 0}
.readiness-item{min-width:0;border:1px solid var(--bsa-line);border-radius:10px;padding:9px 10px;background:var(--bsa-wash);overflow-wrap:anywhere}
.readiness-item strong{display:block;font-size:12px;color:var(--bsa-muted);font-weight:650}.readiness-item span{display:block;font-size:14px;font-weight:650;color:var(--bsa-ink)}
.application-next-action{display:flex;flex-direction:column;align-items:flex-start;gap:8px;border-top:1px solid var(--bsa-line);padding-top:12px}
.application-next-action .action-buttons{display:flex;gap:10px;flex-wrap:wrap;width:100%;align-items:center}.application-next-action button,.application-next-action a{min-height:42px;display:inline-flex;align-items:center;justify-content:center}
.application-next-action .action-reason{font-size:14px;color:var(--bsa-muted);margin:0;overflow-wrap:anywhere}.application-card details{width:100%;padding-top:11px}.application-card details>summary{padding:5px 0;color:var(--bsa-link)}
.application-card .manual-outcome{display:grid;gap:9px;border-top:1px solid var(--bsa-line);margin-top:10px;padding-top:10px}.application-card .manual-outcome select,.application-card .manual-outcome input[type=text]{width:100%;max-width:520px;min-height:40px;padding:8px 10px;border:1px solid var(--bsa-line);border-radius:8px;background:var(--bsa-surface);color:var(--bsa-ink);font:inherit}.application-card .manual-outcome label{display:flex;gap:8px;align-items:flex-start}.application-card .manual-outcome button:disabled{opacity:.55;cursor:not-allowed}
.application-card a,.application-card button{max-width:100%;overflow-wrap:anywhere}.application-card button:focus-visible,.application-card a:focus-visible,.application-card summary:focus-visible,.question-card input:focus-visible,.question-card textarea:focus-visible,.question-card select:focus-visible{outline:3px solid #4b8dff;outline-offset:2px}
.question-card{min-width:0;display:flex;flex-direction:column;gap:10px}.question-card h3{overflow-wrap:anywhere}.question-card .question-jobs{display:grid;gap:7px}.question-card .question-job{border-left:3px solid var(--bsa-line);padding:7px 10px;margin:0;overflow-wrap:anywhere}.question-card .question-answer{width:100%;min-height:88px;border:1px solid var(--bsa-line);border-radius:8px;padding:9px;background:var(--bsa-surface);color:var(--bsa-ink);font:inherit}
.question-card .question-controls{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.question-card .question-feedback{min-height:22px;margin:0;color:var(--bsa-muted)}.question-card .conflict{border-left:4px solid #a96508;padding:9px 12px;background:var(--bsa-wash)}
#question-attention{grid-template-columns:minmax(0,1fr);gap:16px}.question-card{width:100%;padding:20px}.question-card .question-jobs{grid-template-columns:repeat(auto-fit,minmax(min(100%,300px),1fr))}.question-card .question-job{display:grid;gap:2px;min-width:0}.question-card .question-job strong,.question-card .question-job span{overflow-wrap:anywhere;word-break:normal}.question-card .job-select-list{display:grid;gap:7px;width:100%;max-height:260px;overflow:auto;padding:2px}.question-card .job-choice{display:flex;align-items:flex-start;gap:9px;min-width:0;border:1px solid var(--bsa-line);border-radius:9px;padding:9px 11px;background:var(--bsa-wash);color:var(--bsa-ink);cursor:pointer}.question-card .job-choice input{flex:0 0 auto;margin-top:4px}.question-card .job-choice span{min-width:0;overflow-wrap:anywhere}.question-card [hidden]{display:none!important}
.app a,.app button,.question-card button{margin:2px 0}.app .status[data-state=needs_user_input]{border-color:#9b6500}
@media(max-width:760px){.application-readiness{grid-template-columns:repeat(2,minmax(0,1fr))}.application-card{padding:15px}.application-next-action .action-buttons{display:grid;grid-template-columns:1fr}.application-next-action button,.application-next-action a{width:100%}}
@media(max-width:420px){.application-readiness{grid-template-columns:1fr}.wrap{padding-left:12px;padding-right:12px}.application-card h3{font-size:18px}.application-card .app-timing{font-size:12px}}
</style><script>
const phase134OriginalRenderApps=renderApps,phase134AppSignatures=new Map();
function phase134ApplicationSignature(app){return JSON.stringify(app)}
function phase134ReadinessItem(label,value,title){const item=node('div','readiness-item');item.setAttribute('role','listitem');item.title=title||'';item.append(node('strong','',label),node('span','',value||'Unknown'));return item}
async function phase134ResumeApplication(id){await runBusy('Revalidating and resuming the saved attempt…',async()=>{const result=await req('/api/applications/'+encodeURIComponent(id)+'/resume',{method:'POST'});if(!result.resumed_existing_attempt||result.new_attempt_created)throw Error('The server did not confirm a safe resume. Refresh the application history.');say('Existing attempt resumed from its saved checkpoint. No new attempt was created.')})}
retry=async function(id){await runBusy('Rechecking eligibility and authorization before retry…',async()=>{const result=await req('/api/applications/'+encodeURIComponent(id)+'/retry',{method:'POST'});if(!result.new_attempt_created)throw Error('The server did not confirm a new retry attempt. Refresh the application history.');const attempts=await req('/api/applications/'+encodeURIComponent(id)+'/attempts');if(!(attempts||[]).some(item=>item.attempt_number===result.attempt_number))throw Error('Retry started, but its attempt record could not be verified. Check application history.');say('Retry attempt '+result.attempt_number+' saved. Earlier attempt history is preserved; submission remains disabled.')})};
function phase134DecorateApplicationCard(card,app){
  card.classList.add('application-card');card.dataset.applicationId=app.id;
  const heading=card.querySelector('h3');if(heading)heading.title=heading.textContent;
  const company=card.querySelector('.sub');if(company)company.classList.add('app-company');
  const readiness=app.readiness||{},status=card.querySelector('.status');
  if(status){status.replaceChildren(node('i','dot'));status.append(document.createTextNode(readiness.status_label||app.state));status.dataset.state=app.state;status.setAttribute('role','status');status.setAttribute('aria-label','Preparation status: '+(readiness.status_label||app.state))}
  const bar=card.querySelector('.bar'),fill=bar?.querySelector('i');if(bar){bar.setAttribute('role','progressbar');bar.setAttribute('aria-label','Preparation progress');bar.setAttribute('aria-valuemin','0');bar.setAttribute('aria-valuemax','100');bar.setAttribute('aria-valuenow',String(readiness.progress_percent||0));if(fill)fill.style.width=(readiness.progress_percent||0)+'%'}
  const timing=[...card.querySelectorAll('.sub')].find(item=>item.textContent.startsWith('Last updated '));
  if(timing){timing.classList.add('app-timing');timing.textContent='Updated '+(app.last_updated_at?new Date(app.last_updated_at).toLocaleString():'Unknown')+' · Attempt '+(app.attempt_number||1)+' of '+(app.attempt_count||1)+' · Last attempt '+(app.last_attempt_at?new Date(app.last_attempt_at).toLocaleString():'Not started')}
  const oldCheckpoint=card.querySelector('.app-checkpoint');oldCheckpoint?.remove();const checkpoint=node('p','app-checkpoint','Last successful checkpoint: '+(readiness.last_checkpoint||'None recorded'));card.append(checkpoint);
  const oldReadiness=card.querySelector('.application-readiness');oldReadiness?.remove();const grid=node('div','application-readiness');grid.setAttribute('role','list');
  const questions=readiness.questions||{},documents=readiness.documents||{},actions=readiness.human_actions||{};
  grid.append(phase134ReadinessItem('Eligibility freshness',readiness.eligibility?.status,readiness.eligibility?.reason));
  grid.append(phase134ReadinessItem('Authorization freshness',readiness.authorization?.status,readiness.authorization?.reason));
  grid.append(phase134ReadinessItem('Required questions',String(questions.remaining??app.action_required_count??0)+' remaining',questions.reason));
  grid.append(phase134ReadinessItem('Documents',documents.needed?.length?documents.needed.length+' needed':documents.status,documents.needed?.join(' · ')));
  grid.append(phase134ReadinessItem('Human action',actions.count?actions.count+' required':'None reported',actions.required?.join(' · ')));
  grid.append(phase134ReadinessItem('Browser compatibility',readiness.browser_compatibility||'Not yet verified'));
  const detail=card.querySelector('details');if(detail)card.insertBefore(grid,detail);else card.append(grid);
  const oldActions=card.querySelector('.application-next-action');oldActions?.remove();
  for(const button of [...card.querySelectorAll('button')])if(!button.closest('details')&&/^(resume|retry)( dry run)?$/i.test(button.textContent.trim()))button.remove();
  const actionSection=node('section','application-next-action');actionSection.setAttribute('aria-label','Next safe action');
  const buttons=node('div','action-buttons'),state=app.state,recoveryReason=readiness.retry_reason||readiness.resume_reason||'Current eligibility and authorization are required.';
  const addButton=(label,enabled,reason,handler,primary=false)=>{const button=node('button',primary?'primary':'',label);button.type='button';button.disabled=!enabled;button.title=enabled?'':reason;button.setAttribute('aria-label',enabled?label:label+' unavailable: '+reason);if(enabled)button.onclick=handler;buttons.append(button)};
  if(readiness.resume_allowed)addButton('Resume Dry Run',true,'',()=>phase134ResumeApplication(app.id),true);
  else if(readiness.retry_allowed)addButton('Retry Dry Run',true,'',()=>retry(app.id),true);
  else if(state==='failed')addButton('Retry Dry Run',false,recoveryReason,null,true);
  else if(state==='paused'&&readiness.retry_creates_new_attempt)addButton('Retry as New Attempt',false,recoveryReason,null,true);
  else if(state==='paused')addButton('Resume Dry Run',false,readiness.resume_reason||recoveryReason,null,true);
  if(state==='needs_user_input'){
    const resolve=node('a','primary','Resolve Questions');resolve.href='#question-attention';resolve.setAttribute('aria-label','Resolve questions shared across application attempts');buttons.append(resolve);
    const perJob=[...card.querySelectorAll('details')].find(x=>x.querySelector('summary')?.textContent==='Resolve Questions');
    if(perJob){const detailsButton=node('button','','Review This Application’s Questions');detailsButton.type='button';detailsButton.onclick=()=>{perJob.open=true;perJob.querySelector('summary')?.focus()};buttons.append(detailsButton)}
    if(readiness.retry_allowed)addButton('Retry Dry Run',true,'',()=>retry(app.id));
    else if((questions.remaining||0)===0)addButton('Retry Dry Run',false,recoveryReason,null);
  }
  if(state==='unsupported')actionSection.append(node('p','action-reason','No retry is offered: '+(readiness.retry_reason||'The last attempt found unsupported controls. Inspect compatibility first.')));
  else if(state==='failed'||state==='paused'||state==='needs_user_input')actionSection.append(node('p','action-reason',state==='needs_user_input'?(actions.count?'Human action is required before safe retry.':questions.reason||'Save confirmed answers before retrying.'):recoveryReason));
  if(actions.count){
    actionSection.append(node('p','action-reason','Automation stopped for a human action. Its isolated browser session is closed, so entered form values are not carried over. Complete verification or employer-specific declarations on the official form, then submit it yourself.'));
    if(app.application_url){const employerLink=node('a','', 'Open official employer form');employerLink.href=app.application_url;employerLink.target='_blank';employerLink.rel='noopener noreferrer';actionSection.append(employerLink)}
  }
  if(buttons.childElementCount)actionSection.append(buttons);
  if(state==='ready_for_review')actionSection.append(node('p','action-reason','Preparation is complete for review. BabyStepsAI did not submit the application.'));
  card.append(actionSection);
  const manual=node('details','manual-outcome'),manualTitle=node('summary','','Record a manual application outcome');
  manual.append(manualTitle,node('p','sub','Use this only after you act on the employer site. This records your report; it does not submit an application or change the preparation status.'));
  const outcome=document.createElement('select');outcome.setAttribute('aria-label','Manual application outcome');
  [['','Choose an outcome'],['submitted_manually','Submitted manually'],['not_submitted','Not submitted'],['needs_follow_up','Needs follow-up'],['employer_confirmation_received','Employer confirmation received'],['outcome_unknown','Outcome unknown']].forEach(([value,label])=>{const option=document.createElement('option');option.value=value;option.textContent=label;outcome.append(option)});
  outcome.value=readiness.manual_submission_outcome||'';
  const confirmLabel=document.createElement('label'),confirm=document.createElement('input');confirm.type='checkbox';confirmLabel.append(confirm,document.createTextNode(' I confirm this report is accurate.'));
  const record=node('button','','Save manual outcome');record.type='button';record.disabled=true;confirm.onchange=()=>{record.disabled=!confirm.checked||!outcome.value};outcome.onchange=()=>{record.disabled=!confirm.checked||!outcome.value};
  const feedback=node('p','sub','');feedback.setAttribute('role','status');feedback.setAttribute('aria-live','polite');
  record.onclick=async()=>{if(record.dataset.saving==='true'||!confirm.checked||!outcome.value)return;record.dataset.saving='true';record.disabled=true;feedback.textContent='Saving and verifying…';try{const saved=await req('/api/applications/'+encodeURIComponent(app.id)+'/manual-outcome',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({outcome:outcome.value,confirmed:true,event_id:crypto.randomUUID()})});if(!saved.saved||!saved.verified_by_readback)throw Error('The manual outcome was not verified after saving.');feedback.textContent='Saved and verified. Preparation remains separate from your reported outcome.';say('Manual outcome saved and read back. No application was submitted by BabyStepsAI.');setTimeout(()=>refresh(),0)}catch(error){feedback.textContent=error.message||'Could not verify the saved outcome.';confirm.checked=false;record.disabled=false}finally{delete record.dataset.saving}};
  manual.append(outcome,confirmLabel,record,feedback);if(readiness.manual_submission_outcome)manual.append(node('p','sub','Latest applicant-reported outcome: '+readiness.manual_submission_outcome.replaceAll('_',' ')));card.append(manual);
}
renderApps=function(apps){
  const target=document.getElementById('apps');if(!target)return;
  const records=Array.isArray(apps)?apps:[],ids=records.map((app,index)=>String(app.id||'application-'+index));
  const changed=records.some((app,index)=>phase134AppSignatures.get(ids[index])!==phase134ApplicationSignature(app))||target.querySelectorAll('.app').length!==records.length;
  if(!changed)return;
  const stage=document.createElement('div');stage.id='apps';stage.className=target.className;target.replaceWith(stage);
  try{phase134OriginalRenderApps(records)}finally{stage.id='apps-stage';target.id='apps';stage.replaceWith(target)}
  const staged=[...stage.children],old=new Map([...target.querySelectorAll('.app[data-application-id]')].map(card=>[card.dataset.applicationId,card]));
  const next=[];
  records.forEach((app,index)=>{
    const id=ids[index],signature=phase134ApplicationSignature(app),prior=old.get(id);
    if(prior&&phase134AppSignatures.get(id)===signature){next.push(prior);return}
    const fresh=staged[index];if(!fresh)return;
    phase134DecorateApplicationCard(fresh,app);fresh.dataset.applicationId=id;phase134AppSignatures.set(id,signature);next.push(fresh);
  });
  if(!records.length&&staged[0])next.push(staged[0]);
  const y=window.scrollY;target.replaceChildren(...next);for(const id of [...phase134AppSignatures.keys()])if(!ids.includes(id))phase134AppSignatures.delete(id);window.scrollTo(window.scrollX,y);
};
let phase134QuestionSignature=null,phase134QuestionRequest=null,phase134QuestionDirty=false;
renderQuestionAttention=async function(force=false){
  const root=document.getElementById('question-attention');if(!root)return;
  if(phase134QuestionRequest)return phase134QuestionRequest;
  phase134QuestionRequest=(async()=>{
    try{
      const data=await req('/api/application-questions/attention'),groups=data.groups||[],signature=JSON.stringify(groups);
      if(signature===phase134QuestionSignature&&root.children.length)return;
      if(!force&&phase134QuestionDirty)return;
      const active=document.activeElement;
      if(!force&&active&&active!==document.body&&active.closest('#question-attention'))return;
      const cards=[];
      for(const group of groups){
        const card=node('article','app question-card');card.dataset.questionCategory=group.canonical_category;card.dataset.questionContext=group.context||'';
        card.append(node('h3','',group.question));
        card.append(node('p','sub',(group.jobs.length===1?'1 application':'Shared across '+group.jobs.length+' applications')+' · '+String(group.canonical_category||'question').replaceAll('_',' ')));
        const jobList=node('div','question-jobs');for(const job of group.jobs){const row=node('div','question-job');row.append(node('strong','',job.company||'Employer not listed'),node('span','',job.title||'Untitled role'));row.title=job.diagnostic_question||job.question;jobList.append(row)}card.append(jobList);
        card.append(node('p','sub',group.sensitivity_label|| (group.sensitive?'Sensitive · manual entry required':'Ordinary answer')));
        const existing=group.existing_answers||[];
        if(group.requires_correction){const warning=node('p','conflict','A previously saved answer fails the question’s type check and will not be reused. Correct it in Candidate Profile; this new answer can replace it only after you review the conflict.');warning.setAttribute('role','alert');card.append(warning);const edit=node('a','sub','Review saved answers');edit.href='/candidate-profile';card.append(edit)}
        if(group.answer_conflict){const alert=node('p','conflict','Conflicting confirmed answers exist for this question. Review each scope before saving a new answer.');alert.setAttribute('role','alert');card.append(alert)}
        if(existing.length){const details=document.createElement('details'),summary=node('summary','','Existing confirmed answers and reuse scope');details.append(summary);for(const value of existing){const scope=value.reuse_scope==='job_specific'?'This application only':value.reuse_scope==='matching_jobs'?'Matching applications':value.reuse_scope==='general_preference'?'General preference':'Confirmed profile';details.append(node('p','sub',(value.sensitive?'Sensitive answer stored encrypted; value hidden.':value.answer||'Confirmed answer available')+' · '+scope))}card.append(details)}
        if(group.sensitive)card.append(node('p','sub','Sensitive, legal, or human-verification answers are never auto-filled. Provide the answer directly in the employer form when required.'));
        if(group.manual_employer_action){const note=node('p','conflict','This acknowledgment or verification is specific to the employer. Review the exact wording and complete it directly on that employer form. BabyStepsAI will not save, reuse, or answer it.');note.setAttribute('role','note');card.append(note);cards.push(card);continue}
        if(group.canonical_category==='relocation'){card.append(node('p','sub','Relocation is a structured preference. Update it in Career Preferences; this answer cannot override location or work-authorization checks.'));const pref=node('a','', 'Open Career Preferences');pref.href='/career-preferences';card.append(pref);cards.push(card);continue}
        const answer=document.createElement('textarea');answer.className='question-answer';answer.rows=3;answer.maxLength=4000;answer.required=true;answer.setAttribute('aria-label','Confirmed answer for '+group.question);answer.placeholder=group.sensitive?'Enter only the answer you intend to provide manually.':'Enter only an answer you have confirmed.';if(!group.sensitive&&group.existing_answer)answer.value=group.existing_answer;card.append(answer);
        const scope=document.createElement('select');scope.setAttribute('aria-label','Answer reuse scope');
        for(const [value,label] of [['this_job','This application only'],['matching_jobs','Matching applications'],['general_preference','General preference']]){const option=document.createElement('option');option.value=value;option.textContent=label;option.disabled=!!group.sensitive&&value!=='this_job';scope.append(option)}
        if(group.sensitive)scope.value='this_job';else if(group.existing_answer_scope==='matching_jobs')scope.value='matching_jobs';else if(group.existing_answer_scope==='general_preference')scope.value='general_preference';
        const target=node('div','job-select-list');target.setAttribute('role','radiogroup');target.setAttribute('aria-label','Application for job-only reuse');for(const [index,job] of group.jobs.entries()){const label=node('label','job-choice'),radio=document.createElement('input');radio.type='radio';radio.name='question-target-'+group.canonical_category+'-'+(group.context||'all');radio.value=job.application_id;radio.dataset.jobKey=job.job_key;radio.checked=index===0;const description=node('span','');description.append(node('strong','',job.company||'Employer not listed'),document.createElement('br'),document.createTextNode(job.title||'Untitled role'));label.append(radio,description);target.append(label)}
        const confirmLabel=document.createElement('label'),confirm=document.createElement('input');confirm.type='checkbox';confirmLabel.append(confirm,document.createTextNode(' I confirm this answer is accurate.'));card.append(confirmLabel);
        let acknowledge=null;if(group.answer_conflict){const label=document.createElement('label');acknowledge=document.createElement('input');acknowledge.type='checkbox';label.append(acknowledge,document.createTextNode(' I reviewed the conflicts and intentionally chose this answer and scope.'));card.append(label)}
        const controls=node('div','question-controls');controls.append(scope);target.hidden=scope.value!=='this_job';if(group.jobs.length>1)controls.append(target);const save=node('button','primary','Save and verify answer');save.type='button';controls.append(save);card.append(controls);
        const feedback=node('p','question-feedback','');feedback.setAttribute('role','status');feedback.setAttribute('aria-live','polite');card.append(feedback);
        answer.addEventListener('input',()=>{phase134QuestionDirty=true});scope.addEventListener('change',()=>{phase134QuestionDirty=true;target.hidden=scope.value!=='this_job'});confirm.addEventListener('change',()=>{phase134QuestionDirty=true});target.addEventListener('change',()=>{phase134QuestionDirty=true});acknowledge?.addEventListener('change',()=>{phase134QuestionDirty=true});
        save.onclick=async()=>{
          if(save.dataset.saving==='true')return;
          if(!answer.value.trim()||!confirm.checked){feedback.textContent='Enter an answer and confirm that it is accurate.';return}
          if(acknowledge&&!acknowledge.checked){feedback.textContent='Review and acknowledge the existing conflicting answers before saving.';return}
          save.dataset.saving='true';save.disabled=true;feedback.textContent='Saving and verifying answer and reuse scope…';
          const chosen=target.querySelector('input[type=radio]:checked'),selectedJob=group.jobs.find(item=>item.application_id===chosen?.value)||group.jobs[0];const applicationIds=scope.value==='this_job'?[selectedJob.application_id]:group.jobs.map(item=>item.application_id);
          const request={question:group.question,answer:answer.value.trim(),application_ids:applicationIds,reuse_scope:scope.value,job_key:scope.value==='this_job'?selectedJob.job_key:'',confirmed:true,acknowledge_existing_conflict:!!acknowledge?.checked};
          try{
            const saved=await req('/api/application-questions/answer',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(request)});
            if(!saved.persisted_verified)throw Error('The answer was not verified in storage. Refresh before continuing.');
            const verify=await req('/api/application-questions/attention');const persisted=(verify.groups||[]).find(item=>item.canonical_category===group.canonical_category&&item.context===group.context);
            if(!persisted)throw Error('The server saved the answer, but the grouped question was not present in readback. Refresh the dashboard before relying on it.');
            const scopes=persisted.existing_answers||[];const scopeMatch=scopes.find(item=>item.reuse_scope===saved.reuse_scope&&item.sensitive===!!group.sensitive&&(item.reuse_scope!=='job_specific'||item.scope_job_key===request.job_key));
            if(!scopeMatch)throw Error('The answer was saved, but readback did not confirm the selected reuse scope for this job. Review the saved answer before retrying.');
            if(scopeMatch.answer!==(group.sensitive?'':request.answer))throw Error('The reuse scope was confirmed, but the saved answer text did not match the confirmed value. Review the stored answer before continuing.');
            feedback.textContent=saved.requires_manual_action?'Saved and verified for this application only. It will not be auto-filled; answer directly on the employer form.':`Saved and verified for ${scope.value==='this_job'?'this application':scope.value==='matching_jobs'?'matching applications':'your general preference'}. No dry run was resumed.`;
            say(feedback.textContent);phase134QuestionDirty=false;await renderQuestionAttention(true);await refresh();
          }catch(error){feedback.textContent=readableApiError(error.message);save.disabled=false;save.dataset.saving='false'}
        };
        cards.push(card);
      }
      if(!cards.length)cards.push(node('div','empty','No saved applications currently need additional answers.'));
      root.replaceChildren(...cards);phase134QuestionSignature=signature;
    }catch(error){if(!root.children.length)root.append(node('p','sub','Could not load grouped questions: '+readableApiError(error.message)))}
    finally{phase134QuestionRequest=null}
  })();return phase134QuestionRequest;
};
let phase134RefreshInFlight=false;
refresh=async function(){
  if(phase134RefreshInFlight||busy)return;phase134RefreshInFlight=true;const y=window.scrollY;
  try{
    const [apps,progress]=await Promise.all([req('/api/applications'),req('/api/applications/progress')]);
    renderSummary(apps,progress);
    const appActive=!!(document.activeElement?.closest('#apps')||document.querySelector('#apps details[open]'));
    if(!appActive)renderApps(apps);
    const questionActive=!!(document.activeElement?.closest('#question-attention'));
    if(!questionActive)await renderQuestionAttention();
    if(typeof phase133RefreshSavedJobsIfChanged==='function')await phase133RefreshSavedJobsIfChanged();
  }catch(error){say(readableApiError(error.message))}
  finally{phase134RefreshInFlight=false;if(window.scrollY!==y)window.scrollTo(window.scrollX,y)}
};
</script></body></html>""")

APPLICATIONS_PAGE = APPLICATIONS_PAGE.replace("</body></html>", """<style>
#technical-attention{grid-template-columns:minmax(0,1fr);gap:16px}.technical-card{width:100%;min-width:0;padding:20px;overflow-wrap:anywhere}.technical-card textarea,.technical-card select{width:100%;max-width:100%;box-sizing:border-box;background:var(--bsa-surface);color:var(--bsa-ink);border:1px solid var(--bsa-line);border-radius:8px;padding:10px;font:inherit}.technical-card label{display:block;margin:10px 0;color:var(--bsa-ink)}.technical-card details{margin:10px 0}.technical-qualification-row{border-top:1px solid var(--bsa-line);padding:10px 0}.technical-status{font-weight:700}.technical-status[data-status="met"]{color:#16703d}.technical-status[data-status="needs_review"]{color:#8a5600}.technical-status[data-status="confirmed_mismatch"]{color:#a12b32}.technical-card :focus-visible{outline:3px solid #2563eb;outline-offset:2px}
html[data-bsa-theme="dark"] .technical-status[data-status="met"]{color:#66d69a}html[data-bsa-theme="dark"] .technical-status[data-status="needs_review"]{color:#ffd166}html[data-bsa-theme="dark"] .technical-status[data-status="confirmed_mismatch"]{color:#ff8585}
</style><script>
const phase136SavedJobRenderer=renderSavedJobs;
renderSavedJobs=async function(silent=false){
  await phase136SavedJobRenderer(silent);
  try{
    const data=await req('/api/applications/saved-jobs?'+new URLSearchParams({q:document.querySelector('#job-search').value,eligibility:document.querySelector('#job-eligibility').value}).toString());
    const cards=document.querySelectorAll('#jobs .job');
    (data.results||[]).forEach((job,index)=>{
      const card=cards[index];if(!card)return;
      const qualifications=job.technical_qualifications||[],signature=JSON.stringify(qualifications);
      const assessment=job.eligibility_review_assessment||{};
      const reviewSignature=JSON.stringify(assessment);
      let reviewPanel=card.querySelector(':scope > details.eligibility-review-findings');
      if(card.dataset.eligibilityReviewSignature!==reviewSignature){
        reviewPanel?.remove();card.dataset.eligibilityReviewSignature=reviewSignature;
        if(assessment.blockers?.length||assessment.employer_uncertainties?.length||assessment.unsupported_ai_evidence?.length||assessment.mandatory_met?.length){
          reviewPanel=document.createElement('details');reviewPanel.className='eligibility-review-findings';
          reviewPanel.append(node('summary','','Eligibility evidence review'));
          if(assessment.mandatory_met?.length){reviewPanel.append(node('h4','','Mandatory requirements supported'));const list=document.createElement('ul');assessment.mandatory_met.forEach(value=>list.append(node('li','',value)));reviewPanel.append(list)}
          if(assessment.mandatory_unresolved?.length){reviewPanel.append(node('h4','','Mandatory evidence still unresolved'));const list=document.createElement('ul');assessment.mandatory_unresolved.forEach(value=>list.append(node('li','',value)));reviewPanel.append(list)}
          if(assessment.confirmed_conflicts?.length){reviewPanel.append(node('h4','','Confirmed conflicts'));const list=document.createElement('ul');assessment.confirmed_conflicts.forEach(value=>list.append(node('li','',value)));reviewPanel.append(list)}
          for(const [heading,items] of [['Employer information not stated',assessment.employer_uncertainties],['Preferred qualifications',assessment.preferred_qualifications],['AI diagnostics (not posting evidence)',assessment.unsupported_ai_evidence]]){
            if(!items?.length)continue;reviewPanel.append(node('h4','',heading));const list=document.createElement('ul');
            items.forEach(item=>{const text=typeof item==='string'?item:(item.requirement?item.requirement+' — ':'')+(item.explanation||item.status||'');list.append(node('li','',text))});reviewPanel.append(list)
          }
          reviewPanel.append(node('p','sub','Next action: '+(assessment.next_action||'Review the missing evidence.')));
          reviewPanel.append(node('p','sub','Match score is a ranking aid only. Eligibility and job-specific authorization are checked separately.'));
          if(assessment.preparation_blocked&&assessment.preparation_block_reason)reviewPanel.append(node('p','sub','Preparation blocked: '+assessment.preparation_block_reason));
          card.append(reviewPanel);
        }
      }
      let panel=card.querySelector(':scope > details.technical-qualifications');
      if(card.dataset.technicalSignature===signature)return;
      panel?.remove();card.dataset.technicalSignature=signature;
      if(!qualifications.length)return;
      panel=document.createElement('details');panel.className='technical-qualifications';
      const summary=node('summary','','Technical Qualifications · '+qualifications.length+' posting item(s)');panel.append(summary);
      for(const item of qualifications){
        const row=node('div','technical-qualification-row');
        row.append(node('strong','',item.requirement||'Technical qualification'));
        const status=node('p','technical-status',({met:'Met',needs_review:'Needs review',confirmed_mismatch:'Confirmed mismatch',not_applicable:'Not applicable'})[item.status]||'Needs review');status.dataset.status=item.status||'needs_review';row.append(status);
        row.append(node('p','sub',(item.requiredness==='required'?'Mandatory':'Preferred')+' · '+(item.category||'technical requirement').replaceAll('_',' ')));
        if(item.explanation)row.append(node('p','sub',item.explanation));
        if(item.candidate_evidence?.length){const evidence=node('ul','sub');for(const ev of item.candidate_evidence){const source=(ev.source_type||'confirmed source').replaceAll('_',' '),ref=String(ev.source_ref||'').split(':');evidence.append(node('li','',source+(ref.length>1?' · entry '+(Number(ref[1])+1):'')+' · confirmed'+(ev.supports?.length?' · supports '+ev.supports.join(', ').replaceAll('_',' '):'')))}row.append(evidence)}
        if(item.missing_question)row.append(node('p','sub','Question: '+item.missing_question));panel.append(row);
      }
      card.append(panel);
    });
  }catch(error){say('Technical qualification details could not be refreshed: '+readableApiError(error.message))}
};
let phase136TechnicalSignature='',phase136TechnicalRequest=null;
async function renderTechnicalAttention(force=false){
  const root=document.getElementById('technical-attention');if(!root||phase136TechnicalRequest)return;
  let persistentFeedback=document.getElementById('technical-attention-status');
  if(!persistentFeedback){persistentFeedback=document.createElement('p');persistentFeedback.id='technical-attention-status';persistentFeedback.setAttribute('role','status');persistentFeedback.setAttribute('aria-live','polite');root.parentNode.insertBefore(persistentFeedback,root)}
  const active=document.activeElement;
  if(!force&&active&&active!==document.body&&active.closest('#technical-attention'))return;
  phase136TechnicalRequest=(async()=>{
    try{
      const data=await req('/api/application-questions/attention');const groups=data.technical_groups||[],eligibilityGroups=data.eligibility_groups||[];
      const signature=JSON.stringify([groups,eligibilityGroups]);
      if(signature===phase136TechnicalSignature&&!force)return;
      const cards=[];
      for(const group of groups){
        const card=node('article','question-card technical-card');card.append(node('h3','',group.question));
        card.append(node('p','sub','Shared across '+group.jobs.length+' saved job(s) · '+String(group.technical_category||'technical').replaceAll('_',' ')));
        if(group.missing_reason)card.append(node('p','sub',group.missing_reason));
        for(const job of group.jobs){const line=node('p','sub',job.company+' · '+job.title);card.append(line)}
        if(group.candidate_evidence?.length){const details=document.createElement('details'),summary=node('summary','','Evidence already assessed');details.append(summary);const list=node('ul','sub');for(const ev of group.candidate_evidence){const ref=String(ev.source_ref||'').split(':');list.append(node('li','',String(ev.source_type||'confirmed source').replaceAll('_',' ')+(ref.length>1?' · entry '+(Number(ref[1])+1):'')+' · '+(ev.supports||[]).join(', ').replaceAll('_',' ')))}details.append(list);card.append(details)}
        if(group.existing_answers?.length){const details=document.createElement('details'),summary=node('summary','','Previously confirmed answers and scope');details.append(summary);for(const saved of group.existing_answers){const line=node('p','sub',saved.answer+' · '+({job_specific:'This job',matching_jobs:'Matching jobs',general_preference:'General preference'}[saved.reuse_scope]||saved.reuse_scope));details.append(line)}card.append(details)}
        if(group.answer_conflict){const label=document.createElement('label'),ack=document.createElement('input');ack.type='checkbox';ack.dataset.conflictAck='true';label.append(ack,document.createTextNode(' I reviewed the conflicting confirmed answers and authorize this new scoped answer.'));card.append(label)}
        const answer=document.createElement('textarea');answer.rows=4;answer.maxLength=4000;answer.required=true;answer.setAttribute('aria-label','Confirmed technical evidence for '+group.question);answer.placeholder='Describe only work or coursework you personally completed. Include what you implemented and where.';card.append(answer);
        const scope=document.createElement('select');scope.setAttribute('aria-label','Technical answer reuse scope');for(const [value,label] of [['this_job','Use for one selected job'],['matching_jobs','Reuse for these matching jobs'],['general_preference','Save as a general technical fact']]){const option=document.createElement('option');option.value=value;option.textContent=label;scope.append(option)}card.append(scope);
        const target=document.createElement('select');target.setAttribute('aria-label','Job for job-only technical answer');for(const job of group.jobs){const option=document.createElement('option');option.value=job.job_key;option.textContent=job.company+' · '+job.title;target.append(option)}target.hidden=scope.value!=='this_job';scope.onchange=()=>{target.hidden=scope.value!=='this_job'};card.append(target);
        const label=document.createElement('label'),confirm=document.createElement('input');confirm.type='checkbox';label.append(confirm,document.createTextNode(' I confirm this project/coursework description is accurate.'));card.append(label);
        const save=node('button','primary','Save confirmed technical evidence');save.type='button';const status=node('p','question-feedback','');status.setAttribute('role','status');status.setAttribute('aria-live','polite');card.append(save,status);
        save.onclick=async()=>{
          if(save.dataset.saving==='true')return;
          if(answer.value.trim().length<30){status.textContent='Add a specific description of your implementation or coursework (at least 30 characters).';return}
          if(!confirm.checked){status.textContent='Confirm that this description is accurate before saving.';return}
          if(group.answer_conflict&&!card.querySelector('[data-conflict-ack]')?.checked){status.textContent='Review the prior answers and confirm the conflict before replacing this scoped evidence.';return}
          save.dataset.saving='true';save.disabled=true;status.textContent='Saving answer and verifying reuse scope…';
          const jobKeys=scope.value==='this_job'?[target.value]:group.jobs.map(item=>item.job_key);
          try{
            const saved=await req('/api/application-questions/technical-evidence',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:group.question,answer:answer.value.trim(),job_keys:jobKeys,job_key:target.value,reuse_scope:scope.value,confirmed:true,acknowledge_existing_conflict:!!card.querySelector('[data-conflict-ack]')?.checked})});
            if(!saved.persisted_verified)throw Error('The technical answer could not be verified in the information bank.');
            const readback=await req('/api/application-questions/attention');
            const persisted=(readback.technical_groups||[]).find(item=>item.canonical_category===group.canonical_category&&item.context===group.context);
            if(persisted&&!(persisted.existing_answers||[]).some(item=>item.answer===answer.value.trim()&&item.reuse_scope===saved.reuse_scope&&(saved.reuse_scope!=='job_specific'||item.scope_job_key===target.value)))throw Error('The answer was saved, but readback did not confirm its exact reuse scope. Refresh before relying on it.');
            const outcomes=(saved.reassessed||[]).map(item=>item.classification||item.error_type||'unknown').join(', ');
            status.textContent='Saved and read back. Deterministic reassessment completed without AI. Eligibility remains governed by all mandatory requirements.'+(outcomes?' Current result: '+outcomes+'.':'');
            persistentFeedback.textContent=status.textContent;
            say(status.textContent);
            save.dataset.saving='false';save.disabled=false;save.blur();phase136TechnicalSignature='';await renderTechnicalAttention(true);await refresh();
          }catch(error){status.textContent=readableApiError(error.message);save.dataset.saving='false';save.disabled=false}
        };
        cards.push(card);
      }
      for(const group of eligibilityGroups){
        const card=node('article','question-card eligibility-question-card');
        card.append(node('h3','',group.question));
        if(group.sensitive){
          card.append(node('p','status','Sensitive · country-specific · applicant confirmation required'));
          card.append(node('p','sub',group.why_required||'Confirm this legal fact in your profile.'));
          card.append(node('p','sub',group.eligibility_effect||'This answer will not be reused globally.'));
          for(const job of group.jobs)card.append(node('p','sub',job.company+' · '+job.title+' · '+job.requirement));
          const link=node('a','sub','Confirm country-specific work authorization in Candidate Profile');link.href='/candidate-profile';card.append(link);
          card.append(node('p','sub','Do not enter a generic yes/no here. The confirmed country and sponsorship context must be preserved; any sponsorship requirement still needs employer-policy evidence.'));
          cards.push(card);continue;
        }
        if(group.canonical_category==='availability'){
          card.append(node('p','status','Mandatory start availability · applicant confirmation required'));
          card.append(node('p','sub',group.why_required||'The employer gave a start window.'));
          card.append(node('p','sub',group.eligibility_effect||'Graduation date does not establish employment availability.'));
          for(const job of group.jobs)card.append(node('p','sub',job.company+' · '+job.title+' · '+job.requirement));
          const link=node('a','sub','Add earliest available date in Candidate Profile');link.href='/candidate-profile';card.append(link);
          card.append(node('p','sub','Enter a specific date or range only if confirmed. Do not substitute graduation date or an assumed notice period.'));
          cards.push(card);continue;
        }
        card.append(node('p','sub','Mandatory experience evidence · affects '+group.jobs.length+' saved job(s)'));
        card.append(node('p','sub',group.why_required||'This requirement appears in the verified posting.'));
        card.append(node('p','sub',group.eligibility_effect||'Eligibility changes only after verified employment evidence is recorded.'));
        for(const job of group.jobs)card.append(node('p','sub',job.company+' · '+job.title+' · '+job.requirement));
        const link=node('a','sub','Review dated employment evidence in Candidate Profile');link.href='/candidate-profile';card.append(link);
        card.append(node('p','sub','Do not answer yes/no here. Confirm employer, role, and dates in your profile; projects and coursework are not industry employment years.'));
        cards.push(card);
      }
      if(!cards.length)cards.push(node('div','empty','No saved jobs currently need mandatory qualification evidence.'));
      root.replaceChildren(...cards);phase136TechnicalSignature=signature;
    }catch(error){if(!root.children.length)root.append(node('p','sub','Could not load technical qualification questions: '+readableApiError(error.message)))}
    finally{phase136TechnicalRequest=null}
  })();return phase136TechnicalRequest;
}
const phase136BaseRefresh=refresh;
refresh=async function(){await phase136BaseRefresh();const active=document.activeElement;if(!(active&&active!==document.body&&active.closest('#technical-attention'))){await renderTechnicalAttention()}};
document.addEventListener('DOMContentLoaded',()=>{renderTechnicalAttention()});
</script></body></html>""")

APPLICATIONS_PAGE = APPLICATIONS_PAGE.replace("</body></html>", """<script>
const phase151ReadinessPriorRenderer=renderSavedJobs;
renderSavedJobs=async function(silent=false){
  await phase151ReadinessPriorRenderer(silent);
  try{
    const data=await req('/api/applications/saved-jobs?'+new URLSearchParams({q:document.querySelector('#job-search').value,eligibility:document.querySelector('#job-eligibility').value}).toString());
    const cards=document.querySelectorAll('#jobs .job');
    (data.results||[]).forEach((job,index)=>{
      const card=cards[index];if(!card||!job.preparation_plan_available||job.preparation_authorized||job.preparation_authorization_required)return;
      if(card.querySelector('[data-readiness-plan]'))return;
      const open=node('button','','View Preparation Readiness');open.dataset.readinessPlan='true';
      open.title='Read-only summary of confirmed fields, missing requirements, document options, and preparation blockers.';
      open.onclick=async()=>{
        open.disabled=true;open.textContent='Loading readiness…';
        try{
          const plan=await req('/api/applications/saved-jobs/'+encodeURIComponent(job.job_key)+'/preparation-authorization');
          const panel=node('section','authorization-panel');panel.setAttribute('aria-label','Read-only preparation readiness plan');
          panel.append(node('h4','','Preparation readiness · read only'));
          panel.append(node('p','sub','Employer: '+plan.employer+' · '+plan.title));
          panel.append(node('p','sub','Official application URL: '+plan.application_url));
          panel.append(node('p','sub','Eligibility: '+plan.eligibility+' · '+(plan.preparation_ready?'Evidence resolved; authorization remains separate.':'Preparation is blocked.')));
          if(plan.posting_verified_at)panel.append(node('p','sub','Posting verification: '+plan.posting_verified_at));
          panel.append(node('p','sub','Next action: '+(plan.next_action||'Review the listed blockers.')));
          const blockers=plan.preparation_blockers||[];
          if(blockers.length){const details=document.createElement('details');details.append(node('summary','','Preparation blockers · '+blockers.length));const list=node('ul','sub');for(const item of blockers)list.append(node('li','',(item.requirement?item.requirement+' — ':'')+(item.explanation||'')));details.append(list);panel.append(details)}
          if(plan.missing_requirements?.length){const details=document.createElement('details');details.append(node('summary','','Requirements needing resolution · '+plan.missing_requirements.length));const list=node('ul','sub');for(const item of plan.missing_requirements)list.append(node('li','',(item.requirement||'Requirement')+' · '+(item.status||'needs review')+(item.missing_question?' — '+item.missing_question:'')));details.append(list);panel.append(details)}
          const fields=(plan.candidate_fields||[]).map(item=>item.label+(item.sensitive?' · sensitive':''));panel.append(node('p','sub','Confirmed applicant fields available: '+(fields.join(', ')||'None listed.')));
          const docs=(plan.available_documents||[]).map(item=>item.label);panel.append(node('p','sub','Document options: '+(docs.join(', ')||'None listed.')));
          panel.append(node('p','sub','AI answers proposed: '+((plan.ai_answers_proposed||[]).length?'Review listed proposals':'None. Local AI availability does not authorize answer insertion.')));
          panel.append(node('p','sub',plan.cloud_processing_permission||'Cloud processing requires separate job-specific authorization.'));
          if(plan.human_only_actions?.length){const list=node('ul','sub');for(const item of plan.human_only_actions)list.append(node('li','',item));panel.append(node('p','sub','Human-only actions'));panel.append(list)}
          if(plan.inspection)appendInspectionReport(panel,plan.inspection);
          panel.append(node('p','authorization-error',plan.inspection_blocker||'No form-inspection blocker is recorded.'));
          panel.append(node('p','sub','No authorization was created. Application attempts: '+plan.attempt_history_count+'. Live submission: disabled.'));
          card.append(panel);open.remove();
        }catch(error){say(readableApiError(error.message));open.disabled=false;open.textContent='View Preparation Readiness'}
      };
      card.append(open);
    });
  }catch(error){say('Preparation readiness could not be refreshed: '+readableApiError(error.message))}
};
</script></body></html>""")

# Keep handoff cards wired through the final stable-polling wrapper. Later
# dashboard additions wrap ``refresh`` to preserve open forms and scroll; this
# final adapter refreshes only the read-only inbox after those guards run.
APPLICATIONS_PAGE = APPLICATIONS_PAGE.replace("</body></html>", """<script>
const phase151StableRefresh=refresh;
refresh=async function(){
  await phase151StableRefresh();if(busy)return;
  try{const [apps,handoffs]=await Promise.all([req('/api/applications'),req('/api/applications/attention')]);
    const active=document.activeElement;if(!(active&&active!==document.body&&active.closest('#attention-inbox'))){renderAttentionInbox(apps);renderHandoffs(handoffs.items||[])}
  }catch(error){say(readableApiError(error.message))}
};
</script></body></html>""")
