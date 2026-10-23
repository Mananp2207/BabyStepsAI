"""Playwright dry-run application agent; real submissions are disabled."""
import ipaddress
import json
import os
import re
import socket
import sqlite3
import threading
import uuid
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
import hashlib

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from job_search_agent import USER, DB as JOB_DB, fetch_board
from candidate_verification import (verified_profile_facts, save_application_answer, AnswerUpdate,
                                    verified_values_from_json, _answer_plaintext)
from application_browser import apply_targets, classify_control, detect_application_provider, page_interruption, submission_controls
from application_answer_assistant import get_authorization, generate_draft, draft_safe_profile, ORDINARY_PROMPT, SENSITIVE_PROMPT, MAX_ANSWER_CHARS
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
MAX_STEPS = 6
MOCK_PATH_PREFIX = "/mock-apps/"

def browser_headless():
    """Default to the safe background mode; opt into a visible local browser."""
    return os.getenv("BABYSTEPS_BROWSER_HEADLESS", "true").strip().casefold() not in {"0", "false", "no", "off"}

def now(): return datetime.now(timezone.utc).isoformat()
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
                                     ("career_category","TEXT"),("experience_level","TEXT"),("salary_json","TEXT"),
                                     ("required_qualifications_json","TEXT"),("preferred_qualifications_json","TEXT")):
                if name not in opening_columns: db.execute(f"ALTER TABLE discovered_openings ADD COLUMN {name} {declaration}")
        db.execute("""CREATE TABLE IF NOT EXISTS applications (
            id TEXT PRIMARY KEY,user_id TEXT NOT NULL,resume_id TEXT NOT NULL,job_key TEXT NOT NULL,
            company TEXT NOT NULL,title TEXT NOT NULL,application_url TEXT NOT NULL,state TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'dry_run',diagnostic_json TEXT NOT NULL DEFAULT '{}',error TEXT,
            created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(user_id,job_key),
            FOREIGN KEY(resume_id) REFERENCES resumes(id))""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_applications_user_state ON applications(user_id,state)")
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
        db.execute("INSERT OR IGNORE INTO application_controls VALUES(?,0,200,0,?)", (USER, now()))
        auth_table=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='application_authorizations'").fetchone()
        if auth_table:
            auth=db.execute("SELECT settings_json FROM application_authorizations WHERE user_id=?",(USER,)).fetchone()
            try: configured=min(200,max(1,int(json.loads(auth["settings_json"] or "{}").get("max_applications_per_24_hours",20)))) if auth else 20
            except (TypeError,ValueError,AttributeError,json.JSONDecodeError): configured=20
            db.execute("UPDATE application_controls SET daily_limit=?,submission_enabled=0,updated_at=? WHERE user_id=?",(configured,now(),USER))
        db.execute("""CREATE TABLE IF NOT EXISTS submission_quota_reservations (
            id TEXT PRIMARY KEY,user_id TEXT NOT NULL,application_id TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('reserved','succeeded','failed','uncertain')),
            created_at TEXT NOT NULL,updated_at TEXT NOT NULL,confirmed_at TEXT)""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_submission_quota_user_time ON submission_quota_reservations(user_id,state,confirmed_at)")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_submission_quota_app ON submission_quota_reservations(user_id,application_id)")
        db.execute("INSERT OR IGNORE INTO submission_quota_reservations(id,user_id,application_id,state,created_at,updated_at,confirmed_at) SELECT 'historic:'||id,user_id,id,'succeeded',created_at,updated_at,COALESCE(submitted_at,updated_at) FROM applications WHERE state='submitted'")
        # Older versions left in-flight records without heartbeat timestamps.
        # Recovery is explicit and conservative: never retry automatically.
        _recover_stale_in_db(db, startup=True)
        # Upgrade existing application rows into a one-attempt history without
        # dropping or altering their saved diagnostics.
        db.execute("UPDATE applications SET state_changed_at=COALESCE(state_changed_at,updated_at)")
        rows=db.execute("SELECT id,state,diagnostic_json,error,created_at,updated_at,started_at,state_changed_at,heartbeat_at,attempt_number FROM applications").fetchall()
        for row in rows:
            number=max(1,int(row["attempt_number"] or 0))
            db.execute("UPDATE applications SET attempt_number=? WHERE id=?",(number,row["id"]))
            db.execute("INSERT OR IGNORE INTO application_attempts(id,application_id,attempt_number,state,started_at,state_changed_at,heartbeat_at,diagnostic_json,error,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (str(uuid.uuid4()),row["id"],number,row["state"],row["started_at"],row["state_changed_at"] or row["updated_at"],row["heartbeat_at"],row["diagnostic_json"] or "{}",row["error"],row["created_at"],row["updated_at"]))
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

def validate_application_url(url):
    if not isinstance(url,str) or len(url)>2048: raise ValueError("Application URL is missing or too long.")
    p=urlparse(url)
    if _mock_allowed(url): return url
    if p.scheme!="https" or not p.hostname or p.username or p.password: raise ValueError("Application links must use public HTTPS or configured local mock pages.")
    host=p.hostname.rstrip(".").casefold()
    if host in {"localhost","localhost.localdomain","metadata.google.internal"} or host.endswith(".localhost"): raise ValueError("Private and localhost application destinations are blocked.")
    try: addresses={x[4][0] for x in socket.getaddrinfo(host,p.port or 443,type=socket.SOCK_STREAM)}
    except OSError as e: raise ValueError("Application host could not be resolved.") from e
    if any(not ipaddress.ip_address(a).is_global for a in addresses): raise ValueError("Private, loopback, and metadata-service destinations are blocked.")
    return url

def _safe_request(url):
    try: validate_application_url(url); return True
    except ValueError: return False


def _guard_browser_request(route):
    request=route.request
    if request.method.upper() not in {"GET","HEAD","OPTIONS"}:
        route.abort()
        return
    if not _safe_request(request.url) and not request.url.startswith(("about:","data:")):
        route.abort()
        return
    route.continue_()

class DryRunRequest(BaseModel):
    job_key: str|None=None
    job: dict|None=None
    application_url: str|None=None


class ApplicationQueueRequest(BaseModel):
    job_keys: list[str]

class EligibilityReviewRequest(BaseModel):
    reviewed: bool = False

class MissingAnswerRequest(BaseModel):
    question: str
    answer: str
    reusable: bool = False
    confirmed: bool = False

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


def _url_syntax_allowed(url):
    if _mock_allowed(url): return True
    parsed=urlparse(str(url or ""))
    if parsed.scheme!="https" or not parsed.hostname or parsed.username or parsed.password: return False
    host=parsed.hostname.rstrip(".").casefold()
    if host in {"localhost","localhost.localdomain","metadata.google.internal"} or host.endswith(".localhost"): return False
    try:
        address=ipaddress.ip_address(host)
        if not address.is_global: return False
    except ValueError:
        pass
    return True


@router.get("/api/applications/saved-jobs")
def list_saved_application_jobs(q:str="",eligibility:str="all"):
    """Saved openings enriched with the active resume's match and application state."""
    query=(q or "").strip().casefold()[:200]
    if eligibility not in {"all","potential_match","needs_review","not_eligible","unmatched"}:
        raise HTTPException(422,"Unknown eligibility filter.")
    with connect() as db:
        rows=db.execute("""SELECT o.company,o.title,o.location,o.url,o.job_url,o.job_key,o.discovered_at,
                o.employment_type,o.location_status,o.location_reason,o.active,o.source_platform,o.posting_date,
                m.classification,m.match_score,m.status AS match_status,m.result_json AS match_result_json,m.freshness_status,m.matching_model_version,
                a.id AS application_id,a.state AS application_state,a.updated_at AS application_updated_at,
                a.diagnostic_json AS application_diagnostic,
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
        item["application_status_label"]={"not_started":"Not prepared","queued":"Queued","opening":"Opening employer site","inspecting":"Inspecting form","filling":"Preparing form","uploading_resume":"Uploading resume","needs_user_input":"Needs your input","ready_for_review":"Ready for review","failed":"Failed","unsupported":"Unsupported website","paused":"Paused","submitted":"Submitted","skipped_duplicate":"Duplicate skipped"}.get(item["application_state"],"In progress")
        try:item["job_posting_url"]=urlparse(str(item.get("job_url") or item.get("url") or ""))._replace(query="",fragment="").geturl()
        except ValueError:item["job_posting_url"]=""
        item.pop("job_url",None)
        try:
            app_diagnostic=json.loads(item.pop("application_diagnostic",None) or "{}")
        except (TypeError,json.JSONDecodeError):
            app_diagnostic={}
        item["application_message"]=str(app_diagnostic.get("message") or "")[:500]
        item["question_groups"]=_group_questions(app_diagnostic.get("unanswered_fields",[]))
        item["missing_questions"]=[entry["question"] for group in item["question_groups"] for entry in group["questions"]]
        item["url_syntax_allowed"]=_url_syntax_allowed(item.get("url"))
        resume_name=Path(str(item.get("active_resume_stored_name") or ""))
        resume_path=(STORAGE/resume_name.name).resolve()
        resume_available=bool(item["has_active_resume"] and resume_name.name==str(item.get("active_resume_stored_name") or "") and resume_path.parent==STORAGE.resolve() and resume_path.is_file())
        with connect() as db:
            gate_class,reviewed,unresolved=_eligibility_gate(db,item.get("job_key"),item.get("active_resume_id")) if item.get("active_resume_id") else ("not_analyzed",False,True)
        item["eligibility_reviewed"]=reviewed
        try:
            from resume_job_matcher import matching_engine_version
            expected_version=matching_engine_version()
        except Exception:expected_version=""
        item["eligibility_stale"]=bool(item.get("freshness_status")=="stale" or
            (item.get("matching_model_version") not in (None,"") and item.get("matching_model_version")!=expected_version))
        item["preparation_reason"]=("AI matching is required before preparation." if gate_class=="not_analyzed" else
            "This opening is not eligible according to the latest match." if gate_class=="not_eligible" else
            "Resolve mandatory requirements before preparing; a review cannot override them." if unresolved else
            "Review this match before preparation." if gate_class=="needs_review" and not reviewed else "")
        item["preparation_allowed"]=bool(authorized and item["url_syntax_allowed"] and resume_available and item["application_state"]=="not_started" and
            item.get("active",1) and
            gate_class not in {"not_eligible","not_analyzed","rejected","deferred"} and not unresolved and (gate_class=="potential_match" or (gate_class=="needs_review" and reviewed)))
        # Query strings and fragments may contain signed or session-like tokens.
        # Keep the original private in SQLite for preparation, but don't return it to the dashboard.
        try: item["url"]=urlparse(str(item.get("url") or ""))._replace(query="",fragment="").geturl()
        except ValueError: item["url"]=""
        item.pop("has_active_resume",None); item.pop("active_resume_stored_name",None); item.pop("active_resume_id",None)
        results.append(item)
    return {"results":results,"count":len(results)}

def _candidate():
    with connect() as db:
        resume=db.execute("SELECT r.id,r.stored_name,r.original_name FROM resumes r WHERE r.user_id=? AND r.active=1 LIMIT 1",(USER,)).fetchone()
        if not resume: raise HTTPException(409,"Upload and activate a resume before starting a dry run.")
        v=db.execute("SELECT values_json,confirmed_json FROM candidate_verifications WHERE resume_id=? AND user_id=?",(resume["id"],USER)).fetchone()
        ans=db.execute("SELECT id,question_key,question_text,answer,answer_encrypted,sensitive,auto_reuse,answer_type,jurisdiction FROM application_answers WHERE user_id=? AND resume_id=? AND confirmed=1 AND revoked_at IS NULL",(USER,resume["id"])).fetchall()
        answers_by_key={}
        for item in ans:
            answers_by_key[item["question_key"]]={"answer":_answer_plaintext(item,db),"question":item["question_text"],
                "sensitive":bool(item["sensitive"]),"auto_reuse":bool(item["auto_reuse"]),
                "answer_type":item["answer_type"],"jurisdiction":item["jurisdiction"]}
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
        old=db.execute("SELECT id,state FROM applications WHERE user_id=? AND job_key=?",(USER,key)).fetchone()
        active=db.execute("SELECT id FROM applications WHERE user_id=? AND state IN ('queued','opening','inspecting','filling','uploading_resume') LIMIT 1",(USER,)).fetchone()
        if active and (not old or active["id"]!=old["id"]) and not allow_queue:
            raise HTTPException(409,"Another application dry run is active. Wait for it to finish before preparing another job.")
        normalized_url=str(job["application_url"]).split("?",1)[0].rstrip("/").casefold()
        if not old:
            previous=db.execute("SELECT id,state,application_url FROM applications WHERE user_id=?",(USER,)).fetchall()
            old=next((row for row in previous if str(row["application_url"]).split("?",1)[0].rstrip("/").casefold()==normalized_url),None)
        if old and old["state"]=="submitted": return old["id"],"skipped_duplicate",False
        if old and old["state"] in {"queued","opening","inspecting","filling","uploading_resume"}:
            if allow_queue and old["state"]=="queued": return old["id"],old["state"],False
            raise HTTPException(409,"This application is already being processed.")
        if old and old["state"] not in {"failed","paused","needs_user_input","unsupported"}:
            return old["id"],old["state"],False
        if old:
            app_id=old["id"]; db.execute("UPDATE applications SET resume_id=?,company=?,title=?,application_url=?,state='queued',queue_priority=?,diagnostic_json='{}',error=NULL,updated_at=?,state_changed_at=?,started_at=NULL,heartbeat_at=NULL,attempt_number=attempt_number+1 WHERE id=?",(resume["id"],str(job.get("company",""))[:200],str(job.get("title",""))[:300],job["application_url"],int(priority),stamp,stamp,app_id))
            number=db.execute("SELECT attempt_number FROM applications WHERE id=?",(app_id,)).fetchone()[0]
        else:
            app_id=str(uuid.uuid4()); db.execute("INSERT INTO applications(id,user_id,resume_id,job_key,company,title,application_url,state,mode,created_at,updated_at,state_changed_at,attempt_number,queue_priority) VALUES(?,?,?,?,?,?,?,'queued','dry_run',?,?,?,1,?)",(app_id,USER,resume["id"],key,str(job.get("company",""))[:200],str(job.get("title",""))[:300],job["application_url"],stamp,stamp,stamp,int(priority)))
            number=1
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
        heartbeat=stamp if state in {"opening","filling"} else None
        db.execute("UPDATE applications SET state=?,diagnostic_json=?,error=?,updated_at=?,state_changed_at=CASE WHEN state<>? THEN ? ELSE state_changed_at END,started_at=COALESCE(started_at,?),heartbeat_at=? WHERE id=? AND user_id=?",
                   (state,json.dumps(diagnostic or {},ensure_ascii=False)[:30000],(error or "")[:300] or None,stamp,state,stamp,started,heartbeat,app_id,USER))
        db.execute("UPDATE application_attempts SET state=?,diagnostic_json=?,error=?,updated_at=?,state_changed_at=CASE WHEN state<>? THEN ? ELSE state_changed_at END,started_at=COALESCE(started_at,?),heartbeat_at=? WHERE application_id=? AND attempt_number=?",
                   (state,json.dumps(diagnostic or {},ensure_ascii=False)[:30000],(error or "")[:300] or None,stamp,state,stamp,started,heartbeat,app_id,row["attempt_number"]))

def _paused():
    with connect() as db: row=db.execute("SELECT paused FROM application_controls WHERE user_id=?",(USER,)).fetchone()
    return bool(row and row["paused"])

def _label(info): return re.sub(r"[^a-z0-9]+"," "," ".join(str(info.get(k) or "") for k in ("label","name","id","placeholder","aria_label")).casefold()).strip()

def _application_context(job,answers):
    context={"company":job.get("company",""),"title":job.get("title",""),"location":job.get("location",""),"source":""}
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
    answers["__job_description"]=str(job.get("description") or "")[:12000]
    answers["__preferences"]=preferences
    preferences["_reusable_answers"]=[{"question":v.get("question",""),"answer":v.get("answer",""),"type":v.get("answer_type","factual")} for k,v in answers.items() if k.startswith("custom:") and isinstance(v,dict) and v.get("auto_reuse") and not v.get("sensitive")]
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

def _unresolved_mandatory(result_json,job_description=""):
    try: result=json.loads(result_json or "{}")
    except (TypeError,json.JSONDecodeError): return True
    for key in ("required_skills","required_experience","education_requirements"):
        entries=result.get(key,[])
        if not isinstance(entries,list): continue
        for entry in entries:
            if isinstance(entry,dict) and str(entry.get("status","")).casefold() in {"gap","unclear","confirmed_mismatch","unknown"}:
                return True
    missing=result.get("missing_information",[])
    if isinstance(missing,list) and any(re.search(r"work authorization|sponsor|visa|location|graduat|education|professional experience|years of experience",str(item),re.I) for item in missing):
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
      (r"authorization|authorisation|authorized to work|authorised to work","work_authorization"),(r"sponsorship|sponsor|visa","requires_sponsorship"),(r"relocat","relocation_willingness"),(r"salary|compensation expectation","salary_expectations"),(r"salary currency|currency","salary_currency"),(r"earliest available|earliest start|available to start","earliest_available_date"),(r"availability|start date|notice period","availability"),(r"travel|willingness to travel","willingness_to_travel"),
      (r"remote|hybrid|on site|on-site|work arrangement","work_arrangement_preferences"),
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
            if isinstance(saved,dict) and saved.get("answer") and saved.get("auto_reuse"): return saved["answer"],key
    # A user-confirmed reusable answer can support a custom question only when
    # the field text substantially matches the question they saved.
    tokens=set(re.findall(r"[a-z0-9]{4,}",label))-{"your","what","when","where","please","provide","question"}
    for key,item in answers.items():
        if not key.startswith("custom:") or item.get("sensitive") or not item.get("auto_reuse"): continue
        known=set(re.findall(r"[a-z0-9]{4,}",str(item.get("question","")).casefold()))-{"your","what","when","where","please","provide","question"}
        shared=len(tokens & known); required_shared=1 if min(len(tokens),len(known))<=2 else 2
        if tokens and known and shared>=required_shared and shared/max(1,min(len(tokens),len(known)))>=0.6:
            return item["answer"],key
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
    return bool(re.search(r"captcha|human verification|verification code|attest|certify|legal declaration|terms and conditions|gender|race|ethnic|disability|veteran|criminal background|did you use (?:ai|automation)|have you used (?:ai|automation)|do not use automation|no automation",_label(info)))

def _ordinary_open_question(info):
    label=_label(info)
    return (info.get("tag")=="textarea" or info.get("type") in {"text","textarea"}) and bool(ORDINARY_PROMPT.search(label)) and not SENSITIVE_PROMPT.search(label)

def _fill_page(page,candidate,answers,resume_path,on_upload=None):
    fields=page.locator("input:not([type=hidden]),textarea,select,[role=textbox],[role=checkbox],[role=radio],[contenteditable=true]"); report=[]; missing=[]; drafts=[]; processed_radios=set()
    for i in range(min(fields.count(),150)):
        field=fields.nth(i); info={}
        try:
            if not field.is_visible() or not field.is_enabled(): continue
            info=field.evaluate("""el=>{let labels=[...(el.labels||[])].map(x=>x.innerText);let near=el.closest('label')?.innerText||el.parentElement?.innerText||'';let role=el.getAttribute('role')||'';return {tag:el.tagName.toLowerCase(),type:(el.type||(role==='textbox'||el.isContentEditable?'text':role==='checkbox'?'checkbox':role==='radio'?'radio':'')).toLowerCase(),name:el.name||el.getAttribute('name')||'',id:el.id||'',value:el.value||el.getAttribute('value')||el.innerText||'',placeholder:el.placeholder||el.getAttribute('placeholder')||'',aria_label:el.getAttribute('aria-label')||'',accept:el.getAttribute('accept')||'',label:(labels.join(' ')||el.getAttribute('aria-label')||near).slice(0,240),required:el.required||el.getAttribute('aria-required')==='true'||el.getAttribute('aria-required')==='1',max_length:el.maxLength>0?el.maxLength:null,disabled:el.disabled,hidden:el.type==='hidden'||el.getAttribute('aria-hidden')==='true'}}""")
            if info["type"] in {"submit","button","hidden"} or info.get("hidden"): continue
            matching_answer_key=None
            value,source=_answer(info,candidate,answers)
            settings=answers.get("__settings",{})
            local_mock=bool(answers.get("__is_local_mock"))
            if source in candidate and not settings.get("allow_auto_use_confirmed_answers",False) and not local_mock:
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
                    chosen.check(); report.append({"field":info.get("label") or "Selection","result":"filled","source":"verified_profile" if source in candidate else "confirmed_answer","required":bool(info.get("required"))})
                elif info["required"]: missing.append("No confirmed choice matches: "+(info.get("label") or info.get("name") or "required selection"))
                continue
            if value is None and _ordinary_open_question(info) and settings.get("allow_ai_drafting"):
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
                if choice: field.select_option(label=choice)
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
                if (source in candidate or (isinstance(source,str) and source.startswith('custom:'))) and not settings.get('allow_auto_use_confirmed_answers',False) and not answers.get('__is_local_mock'):
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
            info=field.evaluate("""el=>({required:!!el.required||el.getAttribute('aria-required')==='true'||el.getAttribute('aria-required')==='1',label:((el.labels||[]).map(x=>x.innerText).join(' ')||el.getAttribute('aria-label')||el.placeholder||el.name||el.id||el.parentElement?.innerText||'Required field').trim().slice(0,160)})""")
            if info.get("required"):
                result.append(info.get("label") or "Required field")
    except Exception:
        return list(dict.fromkeys(result))
    return list(dict.fromkeys(result))


def _has_password(page):
    try:
        fields=page.locator('input[type="password"]')
        return any(fields.nth(i).is_visible() for i in range(min(fields.count(),10)))
    except Exception: return False


def _interruption(page):
    try: text=page.locator("body").inner_text(timeout=2000)
    except Exception: text=""
    try:
        if re.search(r"/(?:login|log-in|signin|sign-in|auth|sso)(?:/|$)",urlparse(page.url).path,re.I):
            return "authentication_required"
    except (TypeError,ValueError): pass
    return page_interruption(text,_has_password(page))

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
    _update(app_id,"opening",diagnostic)
    try:
        validate_application_url(job["application_url"])
        from playwright.sync_api import sync_playwright
        resume_path=STORAGE/resume["stored_name"]
        if not resume_path.is_file(): raise RuntimeError("The active resume file is unavailable.")
        with sync_playwright() as p:
            diagnostic["phase"]="browser_launch"
            browser=p.chromium.launch(headless=browser_headless()); context=browser.new_context(accept_downloads=False); page=context.new_page()
            page.set_default_timeout(20000)
            page.route("**/*",_guard_browser_request)
            target=job["application_url"]; visited=set()
            for _ in range(4):
                if target in visited:
                    diagnostic["message"]="The employer page redirected repeatedly. Review the job link manually."; diagnostic["screenshot"]=_screenshot(page,app_id)
                    browser.close(); _update(app_id,"unsupported",diagnostic); return
                visited.add(target); validate_application_url(target)
                diagnostic["phase"]="navigation"
                page.goto(target,wait_until="domcontentloaded",timeout=25000); page.wait_for_timeout(100)
                _update(app_id,"inspecting",diagnostic)
                interruption=_interruption(page)
                if interruption:
                    diagnostic["message"]={"human_verification":"Human verification detected. No bypass was attempted.","authentication_required":"The employer requires sign-in. Complete authentication manually before retrying.","expired_listing":"This job listing appears expired or closed.","automation_restricted":"This employer page says automated application preparation is not permitted. The dry run stopped."}[interruption]
                    diagnostic["screenshot"]=_screenshot(page,app_id)
                    browser.close(); _update(app_id,"unsupported" if interruption in {"human_verification","expired_listing","automation_restricted"} else "needs_user_input",diagnostic); return
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
                                browser.close(); _update(app_id,"unsupported",diagnostic); return
                            target=page.url
                            continue
                        except Exception:
                            diagnostic["message"]="The Apply button could not be followed safely."; diagnostic["screenshot"]=_screenshot(page,app_id)
                            browser.close(); _update(app_id,"unsupported",diagnostic); return
                    diagnostic["message"]="No supported application form or clearly labeled Apply link was found."; diagnostic["screenshot"]=_screenshot(page,app_id)
                    browser.close(); _update(app_id,"unsupported",diagnostic); return
                next_target=links[0]
                if next_target in visited:
                    diagnostic["message"]="The Apply link loops back to a page already visited."; diagnostic["screenshot"]=_screenshot(page,app_id)
                    browser.close(); _update(app_id,"unsupported",diagnostic); return
                validate_application_url(next_target)
                target=next_target
            else:
                diagnostic["message"]="The application page could not be reached after several navigation steps."; diagnostic["screenshot"]=_screenshot(page,app_id)
                browser.close(); _update(app_id,"unsupported",diagnostic); return
            diagnostic["form_detected"]=bool(_has_form_fields(page))
            if not diagnostic["form_detected"]:
                diagnostic["message"]="The application page opened, but no supported form controls were detected. No application was submitted."
                diagnostic["screenshot"]=_screenshot(page,app_id)
                browser.close(); _update(app_id,"unsupported",diagnostic); return
            diagnostic["phase"]="form_filling"
            _update(app_id,"filling",diagnostic); missing=[]
            for step in range(MAX_STEPS):
                if _paused():
                    diagnostic["message"]="Paused by user; prior field entries in this browser attempt are retained."; diagnostic["screenshot"]=_screenshot(page,app_id)
                    browser.close(); _update(app_id,"paused",diagnostic); return
                if not answers.get("__is_local_mock") and not get_authorization()["settings"].get("allow_automated_form_preparation"):
                    diagnostic["message"]="Automated form preparation permission was revoked. The dry run stopped safely."; diagnostic["unanswered_fields"].append("Re-enable form preparation in Application settings to continue.")
                    diagnostic["screenshot"]=_screenshot(page,app_id); browser.close(); _update(app_id,"needs_user_input",diagnostic); return
                def mark_upload(): _update(app_id,"uploading_resume",diagnostic)
                filled,unanswered,drafts=_fill_page(page,candidate,answers,resume_path,on_upload=mark_upload)
                diagnostic["filled_fields"].extend(filled); missing.extend(unanswered); diagnostic["draft_answers"].extend(drafts); diagnostic["steps"]=step+1
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
            if missing:
                diagnostic["message"]="Required or sensitive questions need your input. The form was not submitted."; state="needs_user_input"
            else:
                diagnostic["message"]="Dry run complete. Review the form; final submission was not performed."; state="ready_for_review"
            browser.close(); _update(app_id,state,diagnostic)
    except Exception as error:
        name=type(error).__name__
        phase=diagnostic.get("phase")
        if phase=="browser_launch":
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
                resume,candidate,answers=_candidate()
                if row["resume_id"]!=resume["id"]:
                    _update(row["id"],"needs_user_input",{"mode":"dry_run","submission_performed":False,"message":"Activate the resume version associated with this queued application before continuing.","unanswered_fields":["The active resume differs from the version selected when this job was queued."]})
                    continue
                job={"job_key":row["job_key"],"company":row["company"],"title":row["title"],"application_url":row["application_url"]}
                if not _mock_allowed(row["application_url"]):
                    with connect() as db:
                        saved=db.execute("SELECT 1 FROM discovered_openings WHERE user_id=? AND job_key=?",(USER,row["job_key"])).fetchone()
                        gate= _eligibility_gate(db,row["job_key"],resume["id"]) if saved else ("potential_match",True,False)
                    if gate[0]=="not_eligible" or gate[0]=="not_analyzed" or (gate[0] not in {"potential_match","needs_review"}) or ((gate[0]=="needs_review" or gate[2]) and not gate[1]):
                        _update(row["id"],"needs_user_input",{"mode":"dry_run","submission_performed":False,"message":"Eligibility must be analyzed or reviewed again before preparation can continue.","unanswered_fields":["Review the latest AI eligibility result and any unresolved mandatory requirements."]})
                        continue
                answers=_application_context(job,answers)
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
        except HTTPException as error: skipped.append({"job_key":key,"reason":str(error.detail)[:300]}); continue
        try: validate_application_url(job.get("application_url"))
        except ValueError as error: skipped.append({"job_key":key,"reason":str(error)}); continue
        with connect() as db:
            match=db.execute("SELECT classification,match_score,result_json FROM job_match_results WHERE user_id=? AND job_key=? AND resume_id=? ORDER BY created_at DESC LIMIT 1",(USER,key,resume["id"])).fetchone()
            posting=db.execute("SELECT discovered_at,location_status FROM discovered_openings WHERE user_id=? AND job_key=?",(USER,key)).fetchone()
            gate_class,reviewed,unresolved=_eligibility_gate(db,key,resume["id"])
        if gate_class=="not_eligible" or gate_class=="not_analyzed" or gate_class in {"rejected","deferred"} or gate_class not in {"potential_match","needs_review"} or unresolved or (gate_class=="needs_review" and not reviewed):
            reason="AI matching marked this opening not eligible." if gate_class=="not_eligible" else "You rejected this opening." if gate_class=="rejected" else "You deferred this opening's review." if gate_class=="deferred" else "Run AI matching first." if gate_class=="not_analyzed" else "Resolve mandatory requirements before queueing." if unresolved else "Review this match before queueing."
            skipped.append({"job_key":key,"reason":reason}); continue
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
            else: skipped.append({"job_key":key,"reason":"Existing application state: "+state.replace("_"," ")+"."})
        except HTTPException as error: skipped.append({"job_key":key,"reason":str(error.detail)[:300]})
    scheduled=_schedule_application_queue() if prepared else False
    return {"queued":len(prepared),"scheduled":scheduled,"items":prepared,"skipped":skipped,"mode":"dry_run","submission_enabled":False}

@router.post("/api/applications/dry-run")
def start_dry_run(request:DryRunRequest):
    job=_get_job(request); resume,candidate,answers=_candidate()
    is_local=_mock_allowed(job.get("application_url",""))
    with connect() as db:
        saved=db.execute("SELECT 1 FROM discovered_openings WHERE user_id=? AND job_key=?",(USER,job.get("job_key"))).fetchone()
        if saved:
            gate_class,reviewed,unresolved=_eligibility_gate(db,job["job_key"],resume["id"])
            if gate_class=="not_eligible": raise HTTPException(409,"AI matching marked this opening not eligible; preparation is blocked.")
            if gate_class=="not_analyzed": raise HTTPException(409,"Run AI matching before preparing this saved opening.")
            if gate_class not in {"potential_match","needs_review"} or unresolved or (gate_class=="needs_review" and not reviewed):
                raise HTTPException(409,"Review the eligibility result and unresolved mandatory requirements before preparing this opening.")
    if not is_local and not get_authorization()["settings"].get("allow_automated_form_preparation"):
        raise HTTPException(403,"Enable automated form preparation in Application settings before preparing an employer application.")
    answers=_application_context(job,answers)
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
        gate_class,reviewed,unresolved=_eligibility_gate(db,request.job_key,resume["id"])
    if gate_class=="not_eligible": raise HTTPException(409,"AI matching marked this opening not eligible. Preparation is blocked.")
    if gate_class=="not_analyzed": raise HTTPException(409,"Run AI matching before preparing this saved opening.")
    if gate_class not in {"potential_match","needs_review"} or unresolved or (gate_class=="needs_review" and not reviewed):
        raise HTTPException(409,"Review this match and unresolved mandatory requirements before preparing this opening.")
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
        match=db.execute("SELECT updated_at FROM job_match_results WHERE user_id=? AND job_key=? AND resume_id=? ORDER BY created_at DESC LIMIT 1",(USER,job_key,resume["id"])).fetchone()
        db.execute("INSERT INTO application_eligibility_reviews(user_id,job_key,resume_id,classification,reviewed_at,match_updated_at,decision) VALUES(?,?,?,?,?,?,'approved') ON CONFLICT(user_id,job_key,resume_id) DO UPDATE SET classification=excluded.classification,reviewed_at=excluded.reviewed_at,match_updated_at=excluded.match_updated_at,decision='approved'",(USER,job_key,resume["id"],classification,now(),str(match["updated_at"] or "")))
    return {"reviewed":True,"classification":classification,"mandatory_requirements_need_review":unresolved,"message":"Review recorded for this resume version. This does not guarantee eligibility or authorize submission."}


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
        app=db.execute("SELECT state,diagnostic_json,resume_id FROM applications WHERE id=? AND user_id=?",(application_id,USER)).fetchone()
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
    answer_payload=AnswerUpdate(question=question,answer=request.answer[:4000],sensitive=sensitive,confirmed=True,
        answer_type="sensitive" if sensitive else "factual",auto_reuse=bool(request.reusable and not sensitive))
    save_application_answer(key,answer_payload)
    return {"saved":True,"reusable":bool(request.reusable and not sensitive),"requires_manual_action":_requires_human_confirmation({"label":question}),"message":"Confirmed answer saved. Resume the dry run when ready."}

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
    with connect() as db: rows=db.execute("""SELECT a.id,a.job_key,a.company,a.title,a.state,a.mode,a.diagnostic_json,a.error,a.created_at,a.updated_at,a.started_at,a.state_changed_at,a.attempt_number,
        (SELECT COALESCE(NULLIF(o.job_url,''),o.url) FROM discovered_openings o WHERE o.user_id=a.user_id AND o.job_key=a.job_key LIMIT 1) AS source_url
        FROM applications a WHERE a.user_id=? ORDER BY a.updated_at DESC LIMIT 250""",(USER,)).fetchall()
    result=[]
    for row in rows:
        item=dict(row)
        try: item["diagnostic"]=json.loads(item.pop("diagnostic_json"))
        except (TypeError,json.JSONDecodeError): item["diagnostic"]={}
        try:item["source_url"]=urlparse(str(item.get("source_url") or ""))._replace(query="",fragment="").geturl()
        except ValueError:item["source_url"]=""
        item["action_required_groups"]=_group_questions(item["diagnostic"].get("unanswered_fields",[]))
        item["action_required_count"]=sum(len(group["questions"]) for group in item["action_required_groups"])
        result.append(item)
    return result


@router.get("/api/applications/{application_id}/attempts")
def list_application_attempts(application_id:str):
    with connect() as db:
        exists=db.execute("SELECT 1 FROM applications WHERE id=? AND user_id=?",(application_id,USER)).fetchone()
        if not exists: raise HTTPException(404,"Application not found.")
        rows=db.execute("SELECT attempt_number,state,started_at,state_changed_at,created_at,updated_at,diagnostic_json,error FROM application_attempts WHERE application_id=? ORDER BY attempt_number DESC",(application_id,)).fetchall()
    result=[]
    for row in rows:
        item=dict(row)
        try:item["diagnostic"]=json.loads(item.pop("diagnostic_json"))
        except (TypeError,json.JSONDecodeError):item["diagnostic"]={}
        result.append(item)
    return result


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
        rows=db.execute("SELECT id,resume_id,job_key,company,title,application_url,diagnostic_json FROM applications WHERE user_id=? AND state='paused' ORDER BY created_at",(USER,)).fetchall()
    resume,candidate,answers=_candidate() if rows else (None,None,None)
    # Interrupted in-flight workflows may have an uncertain remote state; only
    # an explicit per-application retry can start them again.
    resumable=[]
    for row in rows:
        try: interrupted=bool(json.loads(row["diagnostic_json"] or "{}").get("interrupted"))
        except (TypeError,json.JSONDecodeError): interrupted=False
        if row["resume_id"]==resume["id"] and not interrupted: resumable.append(row)
    stamp=now()
    for row in resumable:
        with connect() as db:
            db.execute("UPDATE applications SET state='queued',state_changed_at=?,updated_at=? WHERE id=? AND state='paused'",(stamp,stamp,row["id"]))
            db.execute("UPDATE application_attempts SET state='queued',state_changed_at=?,updated_at=? WHERE application_id=? AND attempt_number=(SELECT attempt_number FROM applications WHERE id=?)",(stamp,stamp,row["id"],row["id"]))
        executor.submit(process_application,row["id"],dict(row),resume,candidate,answers)
    _schedule_application_queue()
    return {"paused":False,"resumed":len(resumable),"needs_explicit_retry":sum(bool(json.loads(r["diagnostic_json"] or "{}").get("interrupted")) for r in rows),"waiting_for_original_resume":len(rows)-len(resumable)}

@router.get("/api/applications/progress")
def application_progress():
    recover_stale_applications()
    with connect() as db:
        counts={row["state"]:row["total"] for row in db.execute("SELECT state,COUNT(*) AS total FROM applications WHERE user_id=? GROUP BY state",(USER,)).fetchall()}
        paused=db.execute("SELECT paused FROM application_controls WHERE user_id=?",(USER,)).fetchone()["paused"]
    try: configured=min(200,max(1,int(get_authorization()["settings"].get("max_applications_per_24_hours",20))))
    except (TypeError,ValueError): configured=20
    with connect() as db: db.execute("UPDATE application_controls SET daily_limit=?,submission_enabled=0,updated_at=? WHERE user_id=?",(configured,now(),USER))
    with _attempt_lock: active=list(_active_attempts)
    quota=rolling_submission_quota()
    return {"counts":counts,"paused":bool(paused),"submission_enabled":False,"daily_submission_limit":configured,"active_attempts":len(active),"processing_state":"paused" if paused else ("running" if active else "idle"),"submission_quota":quota}

@router.post("/api/applications/{application_id}/resume")
def resume_one(application_id:str):
    resume,candidate,answers=_candidate()
    with connect() as db:
        row=db.execute("SELECT * FROM applications WHERE id=? AND user_id=?",(application_id,USER)).fetchone()
        if not row: raise HTTPException(404,"Application not found.")
        if row["resume_id"]!=resume["id"]: raise HTTPException(409,"Activate the resume version used for this application before resuming it.")
        if not _mock_allowed(row["application_url"]):
            saved=db.execute("SELECT 1 FROM discovered_openings WHERE user_id=? AND job_key=?",(USER,row["job_key"])).fetchone()
            if saved:
                gate_class,reviewed,unresolved=_eligibility_gate(db,row["job_key"],resume["id"])
                if gate_class=="not_eligible": raise HTTPException(409,"AI matching marked this opening not eligible; retry is blocked.")
                if gate_class=="not_analyzed": raise HTTPException(409,"Run AI matching before retrying this saved opening.")
                if gate_class not in {"potential_match","needs_review"} or unresolved or (gate_class=="needs_review" and not reviewed):
                    raise HTTPException(409,"Review the eligibility result and unresolved mandatory requirements before retrying.")
        if row["state"] not in {"paused","failed","needs_user_input","unsupported"}: raise HTTPException(409,"Application is not resumable.")
        if row["state"] in {"opening","filling","queued"}:
            raise HTTPException(409,"This application already has an active attempt.")
        with _attempt_lock:
            if application_id in _active_attempts: raise HTTPException(409,"This application already has an active attempt.")
        db.execute("UPDATE application_controls SET paused=0,updated_at=? WHERE user_id=?",(now(),USER))
        stamp=now()
        try: interrupted=bool(json.loads(row["diagnostic_json"] or "{}").get("interrupted"))
        except (TypeError,json.JSONDecodeError): interrupted=False
        new_attempt=interrupted or row["state"]!="paused"
        if new_attempt:
            number=int(row["attempt_number"] or 0)+1
            db.execute("UPDATE applications SET state='queued',error=NULL,diagnostic_json='{}',updated_at=?,state_changed_at=?,started_at=NULL,heartbeat_at=NULL,attempt_number=? WHERE id=?",(stamp,stamp,number,application_id))
            db.execute("INSERT INTO application_attempts(id,application_id,attempt_number,state,state_changed_at,diagnostic_json,created_at,updated_at) VALUES(?,?,?,'queued',?,'{}',?,?)",(str(uuid.uuid4()),application_id,number,stamp,stamp,stamp))
        else:
            db.execute("UPDATE applications SET state='queued',error=NULL,updated_at=?,state_changed_at=?,heartbeat_at=NULL WHERE id=?",(stamp,stamp,application_id))
            db.execute("UPDATE application_attempts SET state='queued',error=NULL,updated_at=?,state_changed_at=? WHERE application_id=? AND attempt_number=?",(stamp,stamp,application_id,row["attempt_number"]))
    executor.submit(process_application,application_id,dict(row),resume,candidate,answers)
    return {"application_id":application_id,"status":"queued"}

@router.get("/applications",response_class=HTMLResponse)
def applications_page(): return HTMLResponse(apply_ui_theme(APPLICATIONS_PAGE))

@router.get("/mock-apps/{scenario}",response_class=HTMLResponse)
def mock_application(scenario:str):
    if scenario not in MOCK_PAGES: raise HTTPException(404,"Mock scenario not found.")
    return HTMLResponse(MOCK_PAGES[scenario])

APPLICATIONS_PAGE=r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Applications — BabyStepsAI</title>
<style>
:root{color-scheme:light;--ink:#17263b;--muted:#66758a;--line:#e3eaf2;--brand:#2563eb;--surface:#fff;--wash:#f4f7fb}*{box-sizing:border-box}body{margin:0;background:var(--wash);color:var(--ink);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.top{background:#fff;border-bottom:1px solid var(--line)}.nav{max-width:1120px;margin:auto;padding:16px 22px;display:flex;align-items:center;justify-content:space-between;gap:16px}.brand{font-weight:750;font-size:20px;letter-spacing:-.4px}.brand span{color:var(--brand)}nav{display:flex;gap:20px;flex-wrap:wrap}a{color:#315378;text-decoration:none}a:hover{text-decoration:underline}.wrap{max-width:1120px;margin:30px auto;padding:0 22px}.intro{display:flex;align-items:flex-start;justify-content:space-between;gap:20px}.intro h1{font-size:30px;margin:0 0 5px;letter-spacing:-.7px}.muted{color:var(--muted)}.notice{background:#edf4ff;border:1px solid #d6e5ff;border-radius:12px;padding:14px 16px;margin:20px 0}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));gap:12px;margin:18px 0}.metric,.app,.job{background:var(--surface);border:1px solid var(--line);border-radius:14px;box-shadow:0 2px 8px #102a4308}.metric{padding:16px}.metric strong{display:block;font-size:26px;line-height:1.2}.metric span{font-size:13px;color:var(--muted)}.toolbar{display:flex;gap:9px;flex-wrap:wrap;padding:14px;background:#fff;border:1px solid var(--line);border-radius:14px}.toolbar button,button{font:inherit}button{border:1px solid #d1dbea;border-radius:9px;background:#fff;color:var(--ink);padding:9px 13px;cursor:pointer;font-weight:600}button:hover:not(:disabled){border-color:var(--brand);color:var(--brand)}button.primary{background:var(--brand);border-color:var(--brand);color:white}button:disabled{opacity:.52;cursor:wait}.section-head{margin:27px 0 10px;display:flex;align-items:baseline;justify-content:space-between;gap:12px}.section-head h2{font-size:20px;margin:0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,350px),1fr));gap:13px}.app,.job{padding:17px}.app h3,.job h3{margin:0 0 4px;font-size:17px}.sub{color:var(--muted);font-size:14px}.status{display:inline-flex;align-items:center;gap:7px;font-weight:650;font-size:14px;margin:12px 0 5px}.dot{height:9px;width:9px;border-radius:50%;background:#8995a5}.status[data-state=opening] .dot,.status[data-state=filling] .dot{background:#f59e0b;animation:pulse 1.2s infinite}.status[data-state=ready_for_review] .dot{background:#22a06b}.status[data-state=needs_user_input] .dot{background:#e39a19}.status[data-state=failed] .dot{background:#dc4c4c}.status[data-state=paused] .dot{background:#7486a0}.bar{height:6px;border-radius:9px;background:#edf1f6;overflow:hidden;margin:7px 0 12px}.bar i{display:block;background:var(--brand);height:100%;transition:width .2s}.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}details{border-top:1px solid var(--line);margin-top:13px;padding-top:10px}summary{cursor:pointer;color:#405570;font-size:14px;font-weight:600}details p{font-size:14px;color:#45566d;margin:8px 0;overflow-wrap:anywhere}.empty{background:white;border:1px dashed #cbd6e3;border-radius:12px;padding:22px;color:var(--muted)}#status{min-height:24px;margin:14px 0;color:#315378}.pill{font-size:12px;background:#eef2f7;border-radius:20px;padding:3px 8px;color:#52647a}.loader{display:inline-block;width:13px;height:13px;border:2px solid #cbd7e6;border-top-color:var(--brand);border-radius:50%;animation:spin .7s linear infinite;vertical-align:-2px;margin-right:6px}@keyframes spin{to{transform:rotate(360deg)}}@keyframes pulse{50%{opacity:.4}}@media(max-width:620px){.nav{align-items:flex-start;flex-direction:column}.intro{display:block}.wrap{margin:22px auto;padding:0 14px}}
</style></head><body><header class="top"><div class="nav"><div class="brand">BabySteps<span>AI</span></div><nav aria-label="Main navigation"><a href="/">Home</a><a href="/find-jobs">Find jobs</a><a href="/candidate-profile">Candidate profile</a><a href="/applications" aria-current="page">Applications</a></nav></div></header>
<main class="wrap"><div class="intro"><div><h1>Applications</h1><div class="muted">Review your application activity and safely prepare forms.</div></div><span class="pill">Dry runs only · Submission disabled</span></div>
<div class="notice"><strong>Dry run — No application will be submitted.</strong> Dry runs may fill supported forms and attach your active resume. Legal questions, missing answers, and human verification stop for your review.</div>
<div id="summary" class="cards" aria-live="polite"></div><div class="toolbar" aria-label="Application controls"><button id="load-jobs" onclick="loadJobs()">Load saved jobs</button><button id="pause" onclick="action('/api/applications/pause')">Pause processing</button><button id="resume" onclick="action('/api/applications/resume')">Resume processing</button><button type="button" id="theme-toggle" aria-label="Toggle dark mode">Dark mode</button><span class="muted" id="run-state">Checking status…</span></div>
<div class="section-head"><h2>Local test forms</h2><span class="sub">Clearly labeled test workflows</span></div><div class="toolbar"><button onclick="mock('simple')">Test simple form</button><button onclick="mock('multi-step')">Test multi-step form</button><button onclick="mock('uncertain')">Test uncertain questions</button><button onclick="mock('captcha')">Test human-check stop</button></div>
<div class="section-head"><h2>Saved jobs</h2><span class="sub">Prepare one opening or explicitly queue selected openings</span></div><div class="toolbar" aria-label="Saved job filters"><label>Search <input id="job-search" type="search" placeholder="Title, company, or location"></label><label>Eligibility <select id="job-eligibility"><option value="all">All results</option><option value="potential_match">Potential match</option><option value="needs_review">Needs review</option><option value="not_eligible">Not eligible</option><option value="unmatched">Not analyzed</option></select></label><button id="filter-jobs" onclick="loadJobs()">Search saved jobs</button><button id="queue-selected" onclick="queueSelected()">Queue selected dry runs</button></div><div id="jobs" class="grid"><div class="empty">Load saved jobs to choose a dry run.</div></div>
<div class="section-head"><h2>Application history</h2><span class="sub">Refreshes automatically</span></div><div id="apps" class="grid"></div><p id="status" role="status" aria-live="polite"></p></main>
<script>
let busy=false,jobsLoaded=false;const labels={queued:'Queued',opening:'Opening employer site',inspecting:'Inspecting application form',filling:'Filling verified information',uploading_resume:'Uploading resume',paused:'Paused',needs_user_input:'Needs your input',ready_for_review:'Ready for review',failed:'Failed',unsupported:'Unsupported website',skipped_duplicate:'Already applied / duplicate skipped',submitted:'Submitted'};const progress={queued:8,opening:20,inspecting:35,filling:65,uploading_resume:82,paused:68,needs_user_input:82,ready_for_review:100,failed:100,unsupported:100,skipped_duplicate:100,submitted:100};
async function req(url,options={}){const response=await fetch(url,options);let data={};try{data=await response.json()}catch{}if(!response.ok)throw Error(data.detail||'The request could not be completed. Please try again.');return data}function say(message){document.querySelector('#status').textContent=message}function escapeText(value){return String(value||'')}
async function runBusy(label,operation){if(busy)return;say(label);busy=true;document.querySelectorAll('button').forEach(b=>b.disabled=true);try{await operation()}catch(e){say(e.message||'Something went wrong.')}finally{busy=false;document.querySelectorAll('button').forEach(b=>b.disabled=false);await refresh()}}
async function mock(scenario){await runBusy('Starting local test dry run…',async()=>{const data=await req('/api/applications/dry-run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({job:{job_key:'local-mock:'+scenario,company:'BabyStepsAI local test',title:'Test '+scenario,application_url:location.origin+'/mock-apps/'+scenario}})});say(data.status==='skipped_duplicate'?'This test was already submitted; duplicate protection prevented another attempt.':'Local test dry run started.');})}
async function renderSavedJobs(silent=false){const q=document.querySelector('#job-search').value,eligibility=document.querySelector('#job-eligibility').value,params=new URLSearchParams({q,eligibility}),data=await req('/api/applications/saved-jobs?'+params.toString()),jobs=data.results||[],box=document.querySelector('#jobs');box.replaceChildren();jobsLoaded=true;if(!jobs.length){box.append(node('div','empty','No saved jobs match these filters. Discover openings from Find Jobs first.'));if(!silent)say('No saved jobs match these filters.');return}for(const job of jobs){const card=node('article','job'),title=node('h3','',job.title||'Untitled role'),company=node('div','sub',(job.company||'Company unknown')+' · '+(job.location||'Location not listed')+' · '+(job.employment_type||'Employment type unknown')),match=node('div','sub','Eligibility: '+(job.eligibility_stale?'Stale · re-analysis required':(job.classification||'Not analyzed'))+(job.match_score===null?'':' · score '+job.match_score)),state=node('div','status',job.application_status_label||'Not prepared');state.dataset.state=job.application_state;card.append(title,company,match,state);if(job.url_syntax_allowed){const link=node('a','sub','View job posting');link.href=job.url;link.target='_blank';link.rel='noopener noreferrer';card.append(link)}else card.append(node('p','sub','Preparation unavailable: the saved URL is not a permitted public HTTPS link.'));const prep=node('button','primary','Prepare Application');prep.disabled=!job.preparation_allowed;prep.title=prep.disabled?'A resume, permitted URL, active posting, or completed workflow is required.':'';prep.onclick=()=>startSaved(job.job_key);card.append(prep);if(job.application_state==='needs_user_input'){const review=node('a','sub','Review missing answers');review.href='/candidate-profile';card.append(review)}box.append(card)}if(!silent)say(jobs.length+' saved openings shown. Preparing an application opens only the selected job and never submits it.')}
const cardStyle=document.createElement('style');cardStyle.textContent='.job{display:flex;flex-direction:column;align-items:flex-start;gap:9px;min-width:0}.job h3{margin:0;font-size:17px;line-height:1.35;overflow-wrap:anywhere}.job .sub{line-height:1.45;overflow-wrap:anywhere}.job .status{margin:0}.job .job-actions{display:flex;gap:9px;flex-wrap:wrap;align-items:center;width:100%;margin-top:2px}.job .job-message{border-left:3px solid #d7e2ef;padding:7px 10px;margin:0;overflow-wrap:anywhere}.job .job-missing{margin:0;padding-left:20px;color:#8b4c14}';document.head.append(cardStyle);
async function renderSavedJobs(silent=false){const q=document.querySelector('#job-search').value,eligibility=document.querySelector('#job-eligibility').value,params=new URLSearchParams({q,eligibility}),data=await req('/api/applications/saved-jobs?'+params.toString()),jobs=data.results||[],box=document.querySelector('#jobs');box.replaceChildren();jobsLoaded=true;if(!jobs.length){box.append(node('div','empty','No saved jobs match these filters. Discover openings from Find Jobs first.'));if(!silent)say('No saved jobs match these filters.');return}for(const job of jobs){const card=node('article','job'),title=node('h3','',job.title||'Untitled role'),company=node('div','sub',(job.company||'Company unknown')+' · '+(job.location||'Location not listed')+' · '+(job.employment_type||'Employment type unknown')),match=node('div','sub','Eligibility: '+(job.classification||'Not analyzed')+(job.match_score===null?'':' · score '+job.match_score)),state=node('div','status',job.application_status_label||'Not prepared');state.dataset.state=job.application_state;card.append(title,company,match,state);if(job.application_message)card.append(node('p','sub job-message',job.application_message));if(job.missing_questions?.length){const list=node('ul','job-missing');for(const question of job.missing_questions)list.append(node('li','',question));card.append(list)}if(job.url_syntax_allowed){const link=node('a','sub','View job posting');link.href=job.url;link.target='_blank';link.rel='noopener noreferrer';card.append(link)}else card.append(node('p','sub','Preparation unavailable: the saved URL is not a permitted public HTTPS link.'));const actions=node('div','job-actions');if(job.application_state==='not_started'&&job.preparation_allowed){const select=document.createElement('label');select.className='queue-select';const check=document.createElement('input');check.type='checkbox';check.dataset.queueJob=job.job_key;select.append(check,document.createTextNode(' Add to dry-run queue'));actions.append(select)}if(job.application_state==='not_started'){const prep=node('button','primary',busy?'Preparing…':'Prepare Application');prep.disabled=busy||!job.preparation_allowed;prep.title=prep.disabled?'A resume, eligible opening, and permitted URL are required.':'';prep.onclick=()=>startSaved(job.job_key);actions.append(prep)}else if(['failed','paused','unsupported'].includes(job.application_state)&&job.application_id){const retryButton=node('button','primary','Retry dry run');retryButton.disabled=busy;retryButton.onclick=()=>retry(job.application_id);actions.append(retryButton)}else if(job.application_state==='needs_user_input'){const link=node('a','sub','Review profile and confirmed answers');link.href='/candidate-profile';actions.append(link)}if(job.application_state==='ready_for_review')actions.append(node('span','pill','Review in Application history'));if(actions.childElementCount)card.append(actions);box.append(card)}if(!silent)say(jobs.length+' saved openings shown. Preparing an application opens only the selected job and never submits it.')}
async function loadJobs(){await runBusy('Loading saved jobs…',async()=>renderSavedJobs())}
async function startSaved(key){await runBusy('Preparing application…',async()=>{const d=await req('/api/applications/prepare',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({job_key:key})});say(d.message||'Preparation status updated.');await refresh()})}
async function queueSelected(){const keys=[...document.querySelectorAll('[data-queue-job]:checked')].map(x=>x.dataset.queueJob);if(!keys.length){say('Select at least one eligible saved job to queue.');return}await runBusy('Adding selected jobs to the sequential dry-run queue…',async()=>{const d=await req('/api/applications/queue',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({job_keys:keys})});say(d.queued+' job(s) queued. The worker will continue after a job pauses for your input.'+(d.skipped.length?' '+d.skipped.length+' selection(s) skipped.':''));})}
async function action(url){await runBusy(url.endsWith('/pause')?'Pausing application processing…':'Resuming safe queued work…',async()=>{const d=await req(url,{method:'POST'});if(url.endsWith('/pause'))say('Processing paused. The current form stops at its next safe point.');else say('Processing resumed. '+(d.needs_explicit_retry?'Interrupted attempts require a separate review and retry.':''))})}
function node(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e}
function renderSummary(apps,p){const counts=p.counts||{},quota=p.submission_quota||{},entries=[['Applications',apps.length],['Queued',counts.queued||0],['In progress',(counts.opening||0)+(counts.inspecting||0)+(counts.filling||0)+(counts.uploading_resume||0)],['Paused',counts.paused||0],['Needs your input',counts.needs_user_input||0],['Ready for review',counts.ready_for_review||0],['Failed / unsupported',(counts.failed||0)+(counts.unsupported||0)],['Confirmed submissions · 24h',quota.successful_last_24h||0],['Future capacity · live submission off',quota.remaining??p.daily_submission_limit??200]];const box=document.querySelector('#summary');box.replaceChildren(...entries.map(([label,value])=>{const c=node('div','metric');c.append(node('strong','',value),node('span','',label));return c}));document.querySelector('#run-state').textContent=p.paused?'Processing is paused':p.processing_state==='running'?'Processing is active':'Processing is ready';document.querySelector('#pause').disabled=!!p.paused||busy;document.querySelector('#resume').disabled=!p.paused||busy}
function renderApps(apps){const box=document.querySelector('#apps');box.replaceChildren();if(!apps.length){box.append(node('div','empty','No application attempts yet. Start a dry run from a saved job or choose a local test form.'));return}for(const app of apps){const card=node('article','app'),head=node('h3','',escapeText(app.title)),company=node('div','sub',escapeText(app.company));const test=String(app.job_key||'').startsWith('local-mock:');const badge=node('span','pill',test?'Local test':'Saved job');const state=node('div','status',labels[app.state]||'Needs review');state.dataset.state=app.state;state.prepend(node('i','dot'));const bar=node('div','bar');const fill=node('i');fill.style.width=(progress[app.state]||15)+'%';bar.append(fill);const timing=node('div','sub','Last updated '+new Date(app.updated_at).toLocaleString()+(app.started_at?' · Attempt started '+new Date(app.started_at).toLocaleString():''));card.append(head,company,badge,state,bar,timing);if(app.diagnostic?.message)card.append(node('p','sub',app.diagnostic.message));if(app.diagnostic?.unanswered_fields?.length){card.append(node('p','sub','Needs your input: '+app.diagnostic.unanswered_fields.join(' · ')));const link=node('a','sub','Review candidate profile and answers');link.href='/candidate-profile';card.append(link)}if(['paused','failed','needs_user_input','unsupported'].includes(app.state)){const btn=node('button','',app.diagnostic?.interrupted?'Review and retry':'Retry dry run');btn.onclick=()=>retry(app.id);card.append(btn)}const detail=document.createElement('details'),summary=node('summary','','Diagnostics and attempt history');const body=node('div');if(app.error)body.append(node('p','', 'Technical error: '+app.error));if(app.diagnostic?.steps)body.append(node('p','', 'Form steps visited: '+app.diagnostic.steps));if(app.diagnostic?.filled_fields?.length)body.append(node('p','', 'Fields filled: '+app.diagnostic.filled_fields.filter(x=>x.result==='filled').length));if(app.diagnostic?.screenshot){const shot=node('a','sub','View private dry-run screenshot');shot.href='/api/applications/'+encodeURIComponent(app.id)+'/screenshot';shot.target='_blank';shot.rel='noopener';body.append(shot)}if(app.diagnostic?.interrupted)body.append(node('p','', 'This attempt was interrupted. It was not automatically retried because its outcome may be uncertain.'));detail.append(summary,body);detail.addEventListener('toggle',async()=>{if(!detail.open||detail.dataset.loaded)return;detail.dataset.loaded='1';try{const attempts=await req('/api/applications/'+encodeURIComponent(app.id)+'/attempts');for(const attempt of attempts){const item=node('p','sub','Attempt '+attempt.attempt_number+' · '+(labels[attempt.state]||attempt.state)+' · '+(attempt.started_at?'started '+new Date(attempt.started_at).toLocaleString():'not started'));body.append(item);if(attempt.diagnostic?.message)body.append(node('p','sub',attempt.diagnostic.message));if(attempt.error)body.append(node('p','sub','Technical error type: '+attempt.error))}}catch(e){body.append(node('p','sub',e.message))}});card.append(detail);box.append(card)}}
async function retry(id){await runBusy('Starting the selected retry…',async()=>{await req('/api/applications/'+encodeURIComponent(id)+'/resume',{method:'POST'});say('Retry started. Previously completed attempts and diagnostics remain in history.')})}
async function refresh(){try{const [apps,p]=await Promise.all([req('/api/applications'),req('/api/applications/progress')]);renderSummary(apps,p);renderApps(apps);if(jobsLoaded)await renderSavedJobs(true)}catch(e){say(e.message)}}
document.querySelectorAll('.toolbar button').forEach(button=>button.addEventListener('click',()=>{if(busy)button.disabled=true}));refresh();setInterval(refresh,3000);const localTestHeading=[...document.querySelectorAll('.section-head')].find(x=>x.querySelector('h2')?.textContent==='Local test forms'),localTestToolbar=localTestHeading?.nextElementSibling;if(localTestToolbar){const answerTest=node('button','','Test AI-written answer');answerTest.onclick=()=>mock('written');localTestToolbar.append(answerTest)}
Object.assign(labels,{opening:'Opening employer website',inspecting:'Finding application form',filling:'Filling candidate details'});if(!document.querySelector('nav a[href="/application-settings"]')){const settingsNav=document.createElement('a');settingsNav.href='/application-settings';settingsNav.textContent='Application settings';document.querySelector('nav')?.append(settingsNav)}
const baseRenderApps=renderApps;renderApps=function(apps){baseRenderApps(apps);const cards=document.querySelectorAll('#apps .app');apps.forEach((app,index)=>{const drafts=app.diagnostic?.draft_answers;if(!Array.isArray(drafts)||!drafts.length)return;const card=cards[index];if(!card)return;const panel=document.createElement('details'),summary=document.createElement('summary');summary.textContent='Review AI-drafted answers';panel.append(summary);for(const draft of drafts){const question=node('p','sub',draft.question||'Application question'),answer=document.createElement('textarea');answer.value=draft.answer||'';answer.rows=4;answer.maxLength=1200;answer.setAttribute('aria-label','Edit AI-drafted answer');const reuse=document.createElement('label'),check=document.createElement('input');check.type='checkbox';reuse.append(check,document.createTextNode(' Allow automatic reuse after confirming'));const save=node('button','','Confirm and save answer');save.onclick=async()=>{try{save.disabled=true;await req('/api/application-answers/ai-'+Date.now().toString(36),{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:draft.question,answer:answer.value,confirmed:true,sensitive:false,answer_type:'generated',auto_reuse:check.checked})});save.textContent='Saved';say('Your reviewed answer was saved to the active profile.')}catch(e){save.disabled=false;say(e.message)}};panel.append(question,answer,reuse,save)}card.append(panel)})};
const previousAnswerReview=renderApps;renderApps=function(apps){previousAnswerReview(apps);const cards=document.querySelectorAll('#apps .app');apps.forEach((app,index)=>{const drafts=app.diagnostic?.draft_answers||[],card=cards[index];if(!card)return;const panel=[...card.querySelectorAll('details')].find(x=>x.querySelector('summary')?.textContent==='Review AI-drafted answers');if(!panel)return;for(const draft of drafts){const question=[...panel.querySelectorAll('p')].find(x=>x.textContent===(draft.question||'Application question'));if(!question)continue;const validation=draft.validation||{},summary=draft.status==='grounded_draft'?'Grounded draft · sources: '+(draft.evidence_used||[]).join(', '):'Review before use · '+(validation.problems||[]).join(' ');question.before(node('p','sub',summary))}})};
const baseDraftReview=renderApps;renderApps=function(apps){baseDraftReview(apps);const cards=document.querySelectorAll('#apps .app');apps.forEach((app,index)=>{const drafts=app.diagnostic?.draft_answers||[],card=cards[index];if(!card||app.state!=='needs_user_input')return;const panel=[...card.querySelectorAll('details')].find(x=>x.querySelector('summary')?.textContent==='Review AI-drafted answers');if(!panel)return;drafts.forEach((draft,draftIndex)=>{if(!(draft.validation?.problems||[]).length)return;const button=node('button','','Regenerate grounded draft');button.onclick=()=>runBusy('Regenerating with confirmed facts…',async()=>{const updated=await req('/api/applications/'+encodeURIComponent(app.id)+'/drafts/'+draftIndex+'/regenerate',{method:'POST'});say(updated.needs_user_input?'The new draft still needs your review: '+(updated.validation?.problems||[]).join(' '):updated.message);await refresh()});panel.append(button)})})};
const phase8SavedJobs=renderSavedJobs;renderSavedJobs=async function(silent=false){await phase8SavedJobs(silent);const result=await req('/api/applications/saved-jobs?'+new URLSearchParams({q:document.querySelector('#job-search').value,eligibility:document.querySelector('#job-eligibility').value}).toString());const cards=document.querySelectorAll('#jobs .job');(result.results||[]).forEach((job,index)=>{const card=cards[index];if(!card)return;card.querySelector('.job-missing')?.remove();if(job.eligibility_explanation)card.append(node('p','sub',job.eligibility_explanation));if(job.preparation_reason)card.append(node('p','sub',job.preparation_reason));if(!job.preparation_allowed&&['potential_match','needs_review'].includes(job.classification)&&job.application_state==='not_started'){const agree=document.createElement('input');agree.type='checkbox';const consent=document.createElement('label');consent.append(agree,document.createTextNode(' I reviewed this match; it is not a guarantee of eligibility.'));const review=node('button','','Confirm review');review.disabled=busy||!job.url_syntax_allowed||!agree.checked;agree.onchange=()=>{review.disabled=busy||!job.url_syntax_allowed||!agree.checked};review.onclick=()=>runBusy('Recording your review…',async()=>{const d=await req('/api/applications/saved-jobs/'+encodeURIComponent(job.job_key)+'/review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reviewed:agree.checked})});say(d.message);await renderSavedJobs(true)});card.append(consent,review)}})};
const phase8RenderApps=renderApps;renderApps=function(apps){phase8RenderApps(apps);const cards=document.querySelectorAll('#apps .app');apps.forEach((app,index)=>{if(app.state!=='needs_user_input')return;const card=cards[index];if(!card)return;const old=[...card.querySelectorAll('p')].find(p=>p.textContent.startsWith('Needs your input:'));if(old)old.remove();const questions=app.diagnostic?.unanswered_fields||[];const count=Number.isInteger(app.action_required_count)?app.action_required_count:questions.length;const summary=node('p','sub',count+' question(s) need your attention.');card.append(summary);const panel=document.createElement('details'),title=node('summary','','Resolve Questions');panel.append(title);const body=node('div');panel.append(body);panel.addEventListener('toggle',async()=>{if(!panel.open||panel.dataset.loaded)return;panel.dataset.loaded='1';try{const data=await req('/api/applications/'+encodeURIComponent(app.id)+'/questions');for(const group of data.groups||[]){body.append(node('h4','',group.category));for(const item of group.questions||[]){const question=node('p','sub',item.question);body.append(question);if(item.existing_answer)body.append(node('p','sub','Confirmed profile answer: '+item.existing_answer));const draft=(app.diagnostic?.draft_answers||[]).find(x=>item.question.includes(x.question)||x.question.includes(item.question));const input=document.createElement('textarea');input.rows=3;input.maxLength=4000;input.setAttribute('aria-label','Your answer for '+item.question);if(item.existing_answer)input.value=item.existing_answer;const useDraft=node('button','','Use grounded suggestion');if(draft){useDraft.onclick=()=>{input.value=draft.answer||''};body.append(node('p','sub','Suggested answer · review before saving'));body.append(useDraft)}const reuseLabel=document.createElement('label'),reuse=document.createElement('input');reuse.type='checkbox';reuse.checked=!!item.can_reuse&&!item.sensitive;reuse.disabled=!!item.sensitive;reuseLabel.append(reuse,document.createTextNode(' Save for reuse on similar questions'));const save=node('button','primary','Confirm answer');save.onclick=async()=>{save.disabled=true;try{const result=await req('/api/applications/'+encodeURIComponent(app.id)+'/answers',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:item.diagnostic_question||item.question,answer:input.value,reusable:reuse.checked,confirmed:true})});save.textContent='Saved';say(result.requires_manual_action?'Saved, but this employer still requires a human action.':'Answer confirmed and saved. Resume the dry run when ready.')}catch(e){save.disabled=false;say(e.message)}};body.append(input,reuseLabel,save);if(item.human_action_required)body.append(node('p','sub','This item cannot be completed automatically and may still require action on the employer website.'))}}}catch(e){body.append(node('p','sub',e.message))}});card.append(panel);const resume=node('button','primary','Resume Dry Run');resume.onclick=()=>retry(app.id);card.append(resume)})};
</script></body></html>'''

# Expose provider/form diagnostics in a compact review panel without repeating
# candidate answers, local storage paths, or employer form values.
APPLICATIONS_PAGE = APPLICATIONS_PAGE.replace("link.href=job.url;", "link.href=job.job_posting_url||job.url;")
APPLICATIONS_PAGE = APPLICATIONS_PAGE.replace("</body></html>", """<script>
const phase122RenderApps=renderApps;
renderApps=function(apps){
  phase122RenderApps(apps);
  const cards=document.querySelectorAll('#apps .app');
  apps.forEach((app,index)=>{
    const diagnostic=app.diagnostic||{},card=cards[index];
    if(!card)return;
    if(app.source_url){const link=node('a','sub','Open official posting');link.href=app.source_url;link.target='_blank';link.rel='noopener noreferrer';card.append(link)}
    const panel=document.createElement('details'),summary=node('summary','','Preparation details');
    const body=node('div');
    const provider=diagnostic.provider||{};
    const status=diagnostic.form_detected?'Application form detected':(diagnostic.phase==='navigation'?'Opening application page':'Form not yet verified');
    body.append(node('p','sub','Provider: '+(provider.provider||'Not identified')+' · '+(provider.support_level||'compatibility not assessed')));
    body.append(node('p','sub',status+' · Steps inspected: '+(diagnostic.steps||0)));
    body.append(node('p','sub','Fields completed: '+(diagnostic.filled_fields||[]).filter(x=>x.result==='filled').length+' · Required fields: '+(diagnostic.required_fields||[]).length+' · Required items remaining: '+(diagnostic.unanswered_fields||[]).length));
    const uploaded=(diagnostic.filled_fields||[]).filter(x=>x.source==='private_document'||x.source==='active_resume').length;
    body.append(node('p','sub','Documents accepted by form: '+uploaded+' · AI drafts: '+(diagnostic.draft_answers||[]).length+' · Human actions: '+(diagnostic.human_actions_required||[]).length));
    const unresolved=diagnostic.unanswered_fields||[];
    if(unresolved.length){const list=document.createElement('ul');list.className='sub';unresolved.forEach(value=>{const item=document.createElement('li');item.textContent=value;list.append(item)});body.append(list)}
    if(diagnostic.human_actions_required?.length)body.append(node('p','sub','Employer or sensitive questions are waiting for your explicit action.'));
    if(diagnostic.submission_performed)body.append(node('p','sub','Unexpected submission state detected. This run must be reviewed.'));
    else body.append(node('p','sub','Final submission was not performed.'));
    panel.append(summary,body);card.append(panel);
  });
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
