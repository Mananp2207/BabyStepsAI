"""Persistent, authorization-gated orchestration of local job search workflows.

This module coordinates the existing discovery, matching, and application
preparation components. It never submits an application.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

import application_agent
import application_answer_assistant
import career_preferences
import candidate_verification
import job_search_agent
import resume_job_matcher
import job_description_enricher
from ui_theme import apply_ui_theme

router = APIRouter()
DB = Path(__file__).resolve().parent / "babysteps.db"
USER = job_search_agent.USER
POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="user-job-workflow")
_worker_lock = threading.Lock()
_running_workflows=set()


def now():
    return datetime.now(timezone.utc).isoformat()


def connect():
    db=sqlite3.connect(DB,timeout=30)
    db.row_factory=sqlite3.Row
    return db


@contextmanager
def database():
    db=connect()
    try:
        yield db
        db.commit()
    finally:
        db.close()


def initialize():
    """Add workflow state without changing or replacing user data."""
    with database() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS user_workflows(
            workflow_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,prompt TEXT NOT NULL,
            status TEXT NOT NULL,current_stage TEXT NOT NULL,
            resume_id TEXT NOT NULL DEFAULT '',resume_version TEXT NOT NULL DEFAULT '',
            profile_version TEXT NOT NULL DEFAULT '',preferences_version INTEGER NOT NULL DEFAULT 0,
            job_keys_json TEXT NOT NULL DEFAULT '[]',diagnostics_json TEXT NOT NULL DEFAULT '{}',
            discovered_count INTEGER NOT NULL DEFAULT 0,matched_count INTEGER NOT NULL DEFAULT 0,
            potential_match_count INTEGER NOT NULL DEFAULT 0,
            queued_count INTEGER NOT NULL DEFAULT 0,prepared_count INTEGER NOT NULL DEFAULT 0,
            needs_review_count INTEGER NOT NULL DEFAULT 0,failed_count INTEGER NOT NULL DEFAULT 0,
            pause_requested INTEGER NOT NULL DEFAULT 0,cancel_requested INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,updated_at TEXT NOT NULL,completed_at TEXT)""")
        columns={row[1] for row in db.execute("PRAGMA table_info(user_workflows)")}
        if "potential_match_count" not in columns:
            db.execute("ALTER TABLE user_workflows ADD COLUMN potential_match_count INTEGER NOT NULL DEFAULT 0")
        if "discovery_run_id" not in columns:
            db.execute("ALTER TABLE user_workflows ADD COLUMN discovery_run_id TEXT NOT NULL DEFAULT ''")
        if "matching_session_id" not in columns:
            db.execute("ALTER TABLE user_workflows ADD COLUMN matching_session_id TEXT NOT NULL DEFAULT ''")
        # Backfill only exact, persisted associations. Diagnostics from older
        # versions sometimes recorded a proposed run ID that was never inserted.
        has_discovery_table=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_discovery_runs'").fetchone()
        if has_discovery_table:
            for row in db.execute("SELECT workflow_id,diagnostics_json FROM user_workflows WHERE discovery_run_id='' ").fetchall():
                try:run_id=json.loads(row["diagnostics_json"] or "{}").get("discovery_run_id")
                except (TypeError,json.JSONDecodeError):run_id=None
                if run_id:
                    found=db.execute("SELECT workflow_id FROM job_discovery_runs WHERE run_id=?",(run_id,)).fetchone()
                    if found and found["workflow_id"] in {"",row["workflow_id"]}:
                        db.execute("UPDATE user_workflows SET discovery_run_id=? WHERE workflow_id=?",
                                   (run_id,row["workflow_id"]))
                        if not found["workflow_id"]:
                            db.execute("UPDATE job_discovery_runs SET workflow_id=? WHERE run_id=? AND workflow_id=''",
                                       (row["workflow_id"],run_id))
        db.execute("CREATE INDEX IF NOT EXISTS idx_user_workflows_user_updated ON user_workflows(user_id,updated_at DESC)")
        db.execute("""CREATE TABLE IF NOT EXISTS eligibility_review_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,user_id TEXT NOT NULL,workflow_id TEXT NOT NULL,
            job_key TEXT NOT NULL,action TEXT NOT NULL,classification TEXT NOT NULL DEFAULT '',
            resume_id TEXT NOT NULL DEFAULT '',profile_fingerprint TEXT NOT NULL DEFAULT '',
            preferences_fingerprint TEXT NOT NULL DEFAULT '',job_fingerprint TEXT NOT NULL DEFAULT '',
            matching_engine_version TEXT NOT NULL DEFAULT '',match_updated_at TEXT NOT NULL DEFAULT '',
            details_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL)""")
        db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_user_workflow
            ON user_workflows(user_id) WHERE status IN ('queued','running','paused','needs_clarification')""")
        # Never change running workflow state merely because another app
        # process imported this module; discovery leases govern recovery.


initialize()


class WorkflowStart(BaseModel):
    prompt: str = Field(min_length=3,max_length=1500)


class DiscoveryRecovery(BaseModel):
    confirm_abandoned: bool = False


class MatchingRecovery(BaseModel):
    confirm_abandoned: bool = False


class EnrichmentRetry(BaseModel):
    job_keys: list[str] = Field(min_length=1,max_length=12)


class EligibilityAction(BaseModel):
    job_key: str = Field(min_length=1,max_length=250)
    action: str = Field(pattern="^(confirm|reassess|approve_dry_run|reject|defer)$")
    question_key: str = Field(default="",max_length=80)
    question: str = Field(default="",max_length=300)
    answer: str = Field(default="",max_length=4000)
    reusable: bool = False
    confirmed: bool = False


def _local_only(request: Request):
    host=getattr(getattr(request,"client",None),"host",None)
    try:
        import ipaddress
        if not host or not ipaddress.ip_address(host).is_loopback:
            raise HTTPException(403,"Workflow controls are available only from this computer.")
    except ValueError as error:
        raise HTTPException(403,"Workflow controls are available only from this computer.") from error


def _authorization():
    settings=application_answer_assistant.get_authorization()["settings"]
    if not settings.get("allow_automatic_discovery"):
        raise HTTPException(403,"Enable automatic job discovery in Application settings first.")
    if not settings.get("allow_ai_matching"):
        raise HTTPException(403,"Enable AI job matching in Application settings first.")
    return settings


def _versions():
    # Validate the active PDF and use only explicitly confirmed profile facts.
    resume,candidate,_answers=application_agent._candidate()
    completion=candidate_verification.profile_completeness()
    if not completion.get("ready_for_dry_runs"):
        raise HTTPException(409,"Confirm your name and email in Candidate Profile before starting an end-to-end workflow.")
    resume_id=resume["id"]
    profile_status=resume_job_matcher.active_candidate()[1]
    if profile_status not in {"user_reviewed","field_verified"} or not candidate:
        raise HTTPException(409,"Review and confirm candidate profile information before starting a workflow.")
    with database() as db:
        resume_row=db.execute("SELECT created_at FROM resumes WHERE id=? AND user_id=?",(resume_id,USER)).fetchone()
        verification=db.execute("SELECT updated_at FROM candidate_verifications WHERE resume_id=? AND user_id=?",(resume_id,USER)).fetchone()
        # career_preferences is versioned by preference_version/fingerprint; the
        # table intentionally has no updated_at column. Querying one here made
        # every otherwise valid Start Workflow request fail before insertion.
        pref=db.execute("SELECT preference_version FROM career_preferences WHERE user_id=?",(USER,)).fetchone()
    if not pref:
        raise HTTPException(409,"Save career preferences before starting a workflow.")
    preferences=career_preferences.get_preferences()
    if not preferences or not (preferences.get("roles") or preferences.get("categories")):
        raise HTTPException(409,"Select at least one career category or job role first.")
    return resume_id,str(resume_row["created_at"] if resume_row else ""),str(verification["updated_at"] if verification else ""),int(pref["preference_version"] or 0),preferences


def _ensure_snapshot(workflow_id):
    row=_row(workflow_id)
    resume_id,resume_version,profile_version,pref_version,_prefs=_versions()
    if (row["resume_id"]!=resume_id or row["resume_version"]!=resume_version or
        row["profile_version"]!=profile_version or row["preferences_version"]!=pref_version):
        raise HTTPException(409,"Resume, verified profile, or career preferences changed during this workflow. Start a new workflow to refresh matching safely.")


def prompt_conflicts(prompt: str, preferences: dict) -> list[str]:
    """Report direct prompt/preference contradictions; never rewrite saved choices."""
    text=prompt.casefold()
    conflicts=[]
    saved_location=str(preferences.get("location") or "").strip()
    if saved_location and saved_location.casefold() not in {"any","anywhere","global"}:
        from job_relevance import COUNTRY_ALIASES, US_STATES
        requested=[]
        for country,aliases in COUNTRY_ALIASES.items():
            if any(re_search(r"(?<!\w)"+re_escape(alias)+r"(?!\w)",text) for alias in aliases):
                requested.append(country)
        for state in US_STATES:
            if re_search(r"(?<!\w)"+re_escape(state)+r"(?!\w)",text): requested.append(state)
        saved_canonical={country for country,aliases in COUNTRY_ALIASES.items()
                         if any(re_search(r"(?<!\w)"+re_escape(alias)+r"(?!\w)",saved_location.casefold()) for alias in aliases)}
        if any(re_search(r"(?<!\w)"+re_escape(state)+r"(?!\w)",saved_location.casefold()) for state in US_STATES):
            saved_canonical.add("united states")
        if re_search(r"(?:^|[,\s])(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)(?:$|[,\s])",saved_location):
            saved_canonical.add("united states")
        requested=list(dict.fromkeys(requested))
        if requested and (not saved_canonical or not set(requested).intersection(saved_canonical)):
            conflicts.append(f"Prompt location ({', '.join(dict.fromkeys(requested))}) conflicts with saved location ({saved_location}).")
    types={str(value).casefold() for value in preferences.get("employment_types",[])}
    if re_search(r"\b(include|only|looking for|find)\s+(?:paid\s+)?internships?\b",text) and types and not any("intern" in value for value in types):
        conflicts.append("Prompt asks to include internships, but saved employment preferences exclude them.")
    if re_search(r"\b(exclude|no|without|skip)\s+internships?\b",text) and types and types<= {"internship","intern","internships"}:
        conflicts.append("Prompt excludes internships, but saved employment preferences allow only internships.")
    levels={str(value).casefold() for value in preferences.get("levels",[])}
    asks_senior=bool(re_search(r"\b(senior|staff|principal|lead)\b",text))
    excludes_senior=bool(re_search(r"\b(exclude|no|without|skip)\s+(?:senior|staff|principal|lead)\b",text))
    if asks_senior and levels and not any(any(term in level for term in ("senior","staff","principal","lead")) for level in levels):
        conflicts.append("Prompt asks for senior-level roles, but saved experience preferences exclude them.")
    if excludes_senior and levels and all(any(term in level for term in ("senior","staff","principal","lead")) for level in levels):
        conflicts.append("Prompt excludes senior roles, but saved experience preferences allow only senior roles.")
    return conflicts


def re_search(pattern,text):
    import re
    return re.search(pattern,text,re.I)


def re_escape(value):
    import re
    return re.escape(value)


def _write(workflow_id,**changes):
    allowed={"status","current_stage","resume_id","resume_version","profile_version","preferences_version",
             "job_keys_json","diagnostics_json","discovered_count","matched_count","potential_match_count","queued_count","prepared_count",
             "needs_review_count","failed_count","pause_requested","cancel_requested","completed_at","matching_session_id"}
    changes={key:value for key,value in changes.items() if key in allowed}
    if not changes:return
    if "status" in changes or "current_stage" in changes:
        previous=_row(workflow_id)
        if previous:
            try:diagnostics=json.loads(changes.get("diagnostics_json",previous["diagnostics_json"]) or "{}")
            except (TypeError,json.JSONDecodeError):diagnostics={}
            events=diagnostics.setdefault("activity",[])
            event={"at":now(),"status":changes.get("status",previous["status"]),
                   "stage":changes.get("current_stage",previous["current_stage"])}
            if not events or any(events[-1].get(key)!=event[key] for key in ("status","stage")):
                events.append(event)
            diagnostics["activity"]=events[-20:]
            changes["diagnostics_json"]=json.dumps(diagnostics,sort_keys=True)
    changes["updated_at"]=now()
    clause=",".join(f"{key}=?" for key in changes)
    with database() as db: db.execute(f"UPDATE user_workflows SET {clause} WHERE workflow_id=? AND user_id=?",(*changes.values(),workflow_id,USER))


def _row(workflow_id):
    with database() as db: row=db.execute("SELECT * FROM user_workflows WHERE workflow_id=? AND user_id=?",(workflow_id,USER)).fetchone()
    return dict(row) if row else None


def _is_stopped(workflow_id):
    row=_row(workflow_id)
    return not row or bool(row["pause_requested"] or row["cancel_requested"])


def _checkpoint(workflow_id,stage,**extra):
    # Check and transition under one write lock. A Resume request can clear
    # pause_requested between a worker's read and write; never let that stale
    # read put the workflow back into paused state.
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("SELECT pause_requested,cancel_requested FROM user_workflows WHERE workflow_id=? AND user_id=?",
                        (workflow_id,USER)).fetchone()
        if not row:return False
        stamp=now()
        if row["cancel_requested"]:
            db.execute("UPDATE user_workflows SET status='cancelled',current_stage='cancelled',completed_at=?,updated_at=? WHERE workflow_id=? AND user_id=? AND cancel_requested=1",
                       (stamp,stamp,workflow_id,USER))
            return False
        if row["pause_requested"]:
            db.execute("UPDATE user_workflows SET status='paused',current_stage=?,updated_at=? WHERE workflow_id=? AND user_id=? AND pause_requested=1 AND cancel_requested=0",
                       (stage,stamp,workflow_id,USER))
            return False
        db.execute("UPDATE user_workflows SET status='running',current_stage=?,updated_at=? WHERE workflow_id=? AND user_id=? AND pause_requested=0 AND cancel_requested=0",
                   (stage,stamp,workflow_id,USER))
        return True


def _pause_if_requested(workflow_id,stage):
    """Persist a pause only while the same pause request is still outstanding."""
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        changed=db.execute("UPDATE user_workflows SET status='paused',current_stage=?,updated_at=? WHERE workflow_id=? AND user_id=? AND pause_requested=1 AND cancel_requested=0",
                           (stage,now(),workflow_id,USER)).rowcount
    return bool(changed)


def _save_diagnostics(workflow_id,**updates):
    row=_row(workflow_id)
    data=json.loads(row["diagnostics_json"] or "{}") if row else {}
    data.update(updates)
    _write(workflow_id,diagnostics_json=json.dumps(data,sort_keys=True))


def run_workflow(workflow_id,*,discover_fn=None,match_fn=None,queue_fn=None,poll_interval=.2,match_timeout=900):
    """Resume at the persisted stage and continue safely through the pipeline."""
    discover_fn=discover_fn or job_search_agent.discover
    match_fn=match_fn or resume_job_matcher.analyze_matches
    queue_fn=queue_fn or application_agent.enqueue_saved_jobs
    row=_row(workflow_id)
    if not row:return
    try:
        if row["current_stage"] in {"queued","authorizing","discovering"}:
            if not _checkpoint(workflow_id,"authorizing"):return
            settings=_authorization()
            resume_id,version,profile_version,pref_version,prefs=_versions()
            _write(workflow_id,resume_id=resume_id,resume_version=version,profile_version=profile_version,
                   preferences_version=pref_version)
            prompt=row["prompt"]
            conflicts=prompt_conflicts(prompt,prefs)
            if conflicts:
                _save_diagnostics(workflow_id,clarification_required=conflicts)
                _write(workflow_id,status="needs_clarification",current_stage="needs_clarification")
                return
            if not _checkpoint(workflow_id,"discovering"):return
            # Reuse the persisted discovery run when restarting from its
            # discovering checkpoint. job_search_agent resumes only sources
            # that lack a durable completed-source marker.
            prior_diagnostics=json.loads(row.get("diagnostics_json") or "{}")
            discovery_run_id=(row.get("discovery_run_id") or
                              (prior_diagnostics.get("discovery_run_id") if row.get("current_stage")=="discovering" else None)
                              or str(uuid.uuid4()))
            _write(workflow_id,discovery_run_id=discovery_run_id)
            _save_diagnostics(workflow_id,discovery_run_id=discovery_run_id)
            result=discover_fn(prompt,prefs,run_id=discovery_run_id,workflow_id=workflow_id)
            if result.get("status") in {"cancelled","interrupted"}:
                current=_row(workflow_id)
                if current and current["cancel_requested"]:
                    _write(workflow_id,status="cancelled",current_stage="cancelled",completed_at=now())
                else:
                    _write(workflow_id,status="paused",current_stage="discovering")
                return
            keys=list(dict.fromkeys(item.get("job_key") for item in result.get("results",[]) if item.get("job_key")))
            diagnostics=result.get("diagnostics",{})
            source_note=("No active, healthy, live-verified employer sources are currently eligible for discovery."
                         if diagnostics.get("companies_checked",diagnostics.get("sources_checked",0))==0 else "")
            with database() as db:
                state={"discovery":diagnostics,"discovery_status":result.get("status"),"source_note":source_note}
                prior=_row(workflow_id)
                try:state["activity"]=json.loads(prior["diagnostics_json"] or "{}").get("activity",[])
                except (TypeError,json.JSONDecodeError):pass
                db.execute("UPDATE user_workflows SET job_keys_json=?,discovered_count=?,diagnostics_json=?,current_stage='matching',status='running',updated_at=? WHERE workflow_id=?",
                           (json.dumps(keys),len(keys),json.dumps(state),now(),workflow_id))
            row=_row(workflow_id)

        _ensure_snapshot(workflow_id)
        if not _checkpoint(workflow_id,"matching"):return
        row=_row(workflow_id);keys=json.loads(row["job_keys_json"] or "[]")
        if keys:
            # Enrich only these already-saved selected openings. This never
            # repeats employer discovery and failure remains per-job.
            try:
                with job_description_enricher.connect() as db:
                    placeholders=",".join("?" for _ in keys)
                    missing_rows=db.execute(f"SELECT job_key,description FROM discovered_openings WHERE user_id=? AND job_key IN ({placeholders})",
                                            (USER,*keys)).fetchall()
                missing=[item["job_key"] for item in missing_rows
                         if not job_description_enricher.usable_description(item["description"])]
                enrichment=job_description_enricher.enrich_saved_jobs(missing,limit=12) if missing else {
                    "total":0,"attempted":0,"succeeded":0,"failed":0,"available":0,"results":[]}
                _save_diagnostics(workflow_id,enrichment=enrichment)
            except Exception as error:
                # Matching continues; absent descriptions stay explicitly
                # excluded/unresolved by the matcher with its source reason.
                _save_diagnostics(workflow_id,enrichment={"total":0,"attempted":0,"succeeded":0,
                    "failed":0,"available":0,"error":f"Description enrichment could not run ({type(error).__name__})."})
            settings=_authorization()
            request=resume_job_matcher.MatchRequest(job_keys=keys,batch_size=3)
            _save_diagnostics(workflow_id,matching={"status":"running","total":len(keys),"completed":0})
            if match_fn is resume_job_matcher.analyze_matches:
                match_response=match_fn(request,workflow_id=workflow_id)
            else:
                match_response=match_fn(request)
            matching_session_id=match_response.get("session_id") if isinstance(match_response,dict) else None
            if matching_session_id:
                _write(workflow_id,matching_session_id=matching_session_id)
                _save_diagnostics(workflow_id,matching_session_id=matching_session_id)
            if isinstance(match_response,dict) and match_response.get("status")=="paused":
                resume_job_matcher.resume_matches(session_id=matching_session_id,workflow_id=workflow_id)
            if isinstance(match_response,dict) and match_response.get("status") in {"queued","running","paused"}:
                deadline=time.monotonic()+match_timeout
                while time.monotonic()<deadline:
                    current=_row(workflow_id)
                    if not current or current["cancel_requested"]:
                        _write(workflow_id,status="cancelled",current_stage="cancelled",completed_at=now());return
                    if current["pause_requested"]:
                        _pause_if_requested(workflow_id,"matching");return
                    progress=(resume_job_matcher._progress(matching_session_id,keys)
                              if matching_session_id else resume_job_matcher._progress(job_keys=keys))
                    with resume_job_matcher.database() as db:
                        placeholders=",".join("?" for _ in keys)
                        rows=db.execute(f"SELECT job_key,status,freshness_status,classification FROM job_match_results WHERE user_id=? AND resume_id=? AND job_key IN ({placeholders})",
                                        (USER,row["resume_id"],*keys)).fetchall()
                    completed={item["job_key"] for item in rows if item["status"] in {"completed","excluded","failed"}}
                    matches=sum(item["status"]=="completed" and item["freshness_status"]=="current" for item in rows)
                    _save_diagnostics(workflow_id,matching={"status":"running","total":len(keys),"completed":len(completed),"fresh_completed":matches})
                    if progress:
                        _save_diagnostics(workflow_id,matching={"status":progress.get("status"),"total":progress.get("total",len(keys)),
                            "completed":progress.get("completed",len(completed)),"fresh_completed":matches,
                            "failed":progress.get("failed",0),"pending":progress.get("pending",0),
                            "provider":progress.get("provider"),"provider_model":progress.get("provider_model"),
                            "owner_state":progress.get("owner_state"),
                            "current_job":progress.get("current_job"),"last_successful_checkpoint":progress.get("last_successful_checkpoint"),
                            "error":progress.get("error")})
                    _write(workflow_id,matched_count=matches,failed_count=sum(item["status"]=="failed" for item in rows))
                    if progress.get("status")=="paused" and progress.get("pending",0)>0:
                        _write(workflow_id,status="paused",current_stage="matching",pause_requested=1)
                        return
                    if len(completed)>=len(keys):break
                    time.sleep(poll_interval)
                else:
                    raise TimeoutError("AI matching is still running; workflow can be resumed safely.")
            with resume_job_matcher.database() as db:
                placeholders=",".join("?" for _ in keys)
                rows=db.execute(f"SELECT job_key,status,classification,freshness_status FROM job_match_results WHERE user_id=? AND resume_id=? AND job_key IN ({placeholders})",
                                (USER,row["resume_id"],*keys)).fetchall()
            persisted=_row(workflow_id)
            persisted_item=_serialize(persisted) or {}
            outcome_counts=persisted_item.get("outcome_counts",{})
            if outcome_counts.get("pending",0):
                _save_diagnostics(workflow_id,matching={"status":"incomplete","total":len(keys),
                    "completed":len(keys)-outcome_counts["pending"],"pending":outcome_counts["pending"],
                    "outcome_counts":outcome_counts,
                    "error":"Matching returned before every selected job had a current result or an explicit excluded/failed outcome."})
                # This is an incomplete checkpoint, rather than a user pause.
                _write(workflow_id,status="paused",current_stage="matching",pause_requested=0)
                return
            fresh=[item for item in rows if item["status"]=="completed" and item["freshness_status"]=="current"]
            _write(workflow_id,matched_count=len(fresh),potential_match_count=sum(item["classification"]=="potential_match" for item in fresh),
                   needs_review_count=sum(item["classification"]=="needs_review" for item in fresh))
            _save_diagnostics(workflow_id,matching={"status":"completed","total":len(keys),"completed":len(keys),
                "fresh_completed":len(fresh),"pending":0,"outcome_counts":outcome_counts})
        else:
            fresh=[]
        _ensure_snapshot(workflow_id)
        if not _checkpoint(workflow_id,"queueing"):return
        settings=_authorization()
        queue_result={"queued":0,"skipped":[]}
        potential=[]
        for item in fresh:
            if item["classification"]!="potential_match":continue
            # The queue endpoint repeats all freshness, mandatory-requirement,
            # URL, duplicate, resume, and authorization checks before enqueue.
            potential.append(item["job_key"])
        if settings.get("allow_automated_form_preparation"):
            for start in range(0,len(potential),20):
                response=queue_fn(application_agent.ApplicationQueueRequest(job_keys=potential[start:start+20]))
                queue_result["queued"]+=int(response.get("queued",0))
                queue_result["skipped"].extend(response.get("skipped",[]))
        with application_agent.connect() as db:
            apps=db.execute("""SELECT state,count(*) n FROM applications WHERE user_id=? AND job_key IN
                (SELECT value FROM json_each(?)) GROUP BY state""",(USER,json.dumps(keys))).fetchall()
        app_counts={item["state"]:item["n"] for item in apps}
        _save_diagnostics(workflow_id,queue=queue_result,application_states=app_counts,
                          submission_enabled=False,eligible_sources=len(job_search_agent._employer_sources()))
        if not _checkpoint(workflow_id,"completed"):return
        _write(workflow_id,status="completed",current_stage="completed",queued_count=queue_result["queued"],
               prepared_count=app_counts.get("ready_for_review",0)+app_counts.get("needs_user_input",0),
               completed_at=now())
    except HTTPException as error:
        _save_diagnostics(workflow_id,failure=str(error.detail)[:500])
        detail=str(error.detail)
        conflict_code=error.detail.get("code","") if isinstance(error.detail,dict) else ""
        current=_row(workflow_id) or {}
        stage=current.get("current_stage","unknown")
        if error.status_code==409 and stage=="matching" and conflict_code in {
                "active_worker","legacy_unowned","stale_lease_owner_alive","stale_lease_owner_unknown","legacy_checkpoint_overlap"}:
            if error.detail.get("session_id"):
                _write(workflow_id,matching_session_id=error.detail["session_id"])
            _save_diagnostics(workflow_id,matching={"status":"blocked","error":error.detail.get("message"),
                "session_id":error.detail.get("session_id"),"recovery_available":bool(error.detail.get("recovery_available"))},
                recovery="Discovery completed and its saved jobs are preserved. Resolve the matching session, then resume this workflow without repeating discovery.")
            _write(workflow_id,status="paused",current_stage="matching",pause_requested=0)
        elif error.status_code==409 and any(code in detail for code in
                ("active_worker:","legacy_unleased:","stale_lease_owner_alive:","stale_lease:")):
            _save_diagnostics(workflow_id,recovery="Discovery is owned by another or abandoned worker. View that run or recover it, then resume this workflow.")
            _write(workflow_id,status="paused",current_stage="discovering",pause_requested=0)
        else:
            _write(workflow_id,status="failed",current_stage="failed",failed_count=1,completed_at=now())
    except Exception as error:
        current=_row(workflow_id)
        stage=(current or {}).get("current_stage","unknown")
        _save_diagnostics(workflow_id,failure=f"{type(error).__name__}: workflow stopped safely; resume may continue from its checkpoint.",
                          failure_stage=stage,failure_type=type(error).__name__)
        _write(workflow_id,status="paused",current_stage=stage,pause_requested=0)


def _worker_entry(workflow_id):
    try:
        run_workflow(workflow_id)
    finally:
        with _worker_lock:
            _running_workflows.discard(workflow_id)
        # A Resume can arrive while this worker is unwinding from a pause.
        # _submit intentionally deduplicates, so hand off queued work after
        # releasing the old worker's slot to avoid losing that request.
        current=_row(workflow_id)
        if current and current["status"]=="queued" and not current["pause_requested"] and not current["cancel_requested"]:
            try:_submit(workflow_id)
            except Exception:pass


def _submit(workflow_id):
    with _worker_lock:
        if workflow_id in _running_workflows:
            return False
        _running_workflows.add(workflow_id)
        try:
            POOL.submit(_worker_entry,workflow_id)
        except Exception:
            _running_workflows.discard(workflow_id)
            raise
        return True


def _serialize(row):
    if not row:return None
    item=dict(row)
    for key in ("job_keys_json","diagnostics_json"):
        try:item[key.removesuffix("_json")]=json.loads(item.pop(key) or ("[]" if key=="job_keys_json" else "{}"))
        except (TypeError,json.JSONDecodeError):item[key.removesuffix("_json")]=[] if key=="job_keys_json" else {}
    item["submission_enabled"]=False
    item["job_outcomes"]=_job_outcomes(item)
    item["outcome_counts"]={key:sum(job.get(key,False) for job in item["job_outcomes"])
                            for key in ("analyzed","reused","excluded","needs_review","not_eligible","failed","pending","retryable")}
    selected=item.get("job_keys") or []
    item["application_counts"]={}
    item["review_counts"]={"awaiting_reassessment":0,"approved_for_dry_run":0}
    if selected:
        with database() as db:
            marks=",".join("?" for _ in selected)
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "applications" in tables:
                item["application_counts"]={r["state"]:r["n"] for r in db.execute(
                    f"SELECT state,COUNT(*) AS n FROM applications WHERE user_id=? AND job_key IN ({marks}) GROUP BY state",(USER,*selected))}
            if "job_match_results" in tables:
                item["review_counts"]["awaiting_reassessment"]=db.execute(
                    f"SELECT COUNT(*) FROM job_match_results WHERE user_id=? AND resume_id=? AND job_key IN ({marks}) AND status='completed' AND freshness_status='stale'",
                    (USER,item.get("resume_id"),*selected)).fetchone()[0]
            if "application_eligibility_reviews" in tables and "job_match_results" in tables:
                item["review_counts"]["approved_for_dry_run"]=db.execute(f"""SELECT COUNT(*) FROM application_eligibility_reviews r
                    JOIN job_match_results m ON m.user_id=r.user_id AND m.job_key=r.job_key AND m.resume_id=r.resume_id
                    WHERE r.user_id=? AND r.resume_id=? AND r.job_key IN ({marks}) AND r.classification=m.classification
                    AND r.match_updated_at=m.updated_at AND m.freshness_status='current'""",(USER,item.get("resume_id"),*selected)).fetchone()[0]
    session_id=item.get("matching_session_id") or item.get("diagnostics",{}).get("matching_session_id")
    progress=None
    if session_id:
        try:progress=resume_job_matcher._progress(session_id,item.get("job_keys") or [])
        except (sqlite3.OperationalError,TypeError,ValueError):pass
    if progress:
        item["matching_progress"]=progress
        item["matching_worker_state"]=("running" if item.get("status")=="running" and
            progress.get("status")=="running" and progress.get("owner_state")=="owned" and
            progress.get("workflow_id")==item.get("workflow_id") else
            "stopping" if item.get("status")=="paused" and progress.get("owner_state")=="owned" else
            "resume_requested" if item.get("status")=="queued" else
            "stopped" if item.get("status") in {"paused","failed","completed","cancelled"} else "starting")
        if progress.get("workflow_id") and progress.get("workflow_id")!=item.get("workflow_id"):
            item["matching_checkpoint_mismatch"]=True
    else:
        item["matching_worker_state"]=("resume_requested" if item.get("status")=="queued" else
            "stopped" if item.get("status") in {"paused","failed","completed","cancelled"} else "starting")
    return item


def _job_outcomes(workflow):
    """Classify every selected key from its persisted result without mutating history."""
    keys=list(dict.fromkeys(workflow.get("job_keys") or []))
    if not keys:return []
    resume_id=workflow.get("resume_id")
    placeholders=",".join("?" for _ in keys)
    try:
        with database() as db:
            session=None
            session_id=workflow.get("matching_session_id") or (workflow.get("diagnostics") or {}).get("matching_session_id")
            if session_id:
                session=db.execute("SELECT created_at,workflow_id FROM job_match_sessions WHERE session_id=? AND user_id=?",
                                   (session_id,USER)).fetchone()
            if session and session["workflow_id"] and session["workflow_id"]!=workflow.get("workflow_id"):
                session=None
            opening_columns={row[1] for row in db.execute("PRAGMA table_info(discovered_openings)")}
            description_column="o.description" if "description" in opening_columns else "'' AS description"
            description_source="o.description_source" if "description_source" in opening_columns else "'' AS description_source"
            description_error="o.description_error" if "description_error" in opening_columns else "'' AS description_error"
            rows=db.execute(f"""SELECT o.job_key,o.company,o.title,{description_column},{description_source},{description_error},m.status,m.classification,m.freshness_status,
                    m.matching_model_version,m.updated_at,m.error,m.result_json
                FROM discovered_openings o LEFT JOIN job_match_results m
                  ON m.user_id=o.user_id AND m.job_key=o.job_key AND m.resume_id=?
                WHERE o.user_id=? AND o.job_key IN ({placeholders})""",(resume_id,USER,*keys)).fetchall()
    except sqlite3.OperationalError:
        return [{"job_key":key,"status":"pending","pending":True} for key in keys]
    by_key={row["job_key"]:dict(row) for row in rows}
    # A stale/cross-linked session must not make results from another workflow
    # look reused. _attach_workflow_checkpoint repairs this relationship, but
    # dashboard rendering remains conservative for older records.
    session_created=session["created_at"] if session else workflow.get("created_at")
    outcomes=[]
    for key in keys:
        row=by_key.get(key,{})
        status=row.get("status")
        classification=row.get("classification")
        fresh=(row.get("freshness_status")=="current")
        explanation=""
        if row.get("result_json"):
            try:explanation=(json.loads(row["result_json"]).get("explanation") or "")
            except (TypeError,json.JSONDecodeError,AttributeError):pass
        deterministic=bool(explanation and ("not assessed by Ollama" in explanation.lower() or
                                             "not sent to ollama" in explanation.lower()))
        description_available=job_description_enricher.usable_description(row.get("description") or "")
        reason=row.get("error") or explanation
        missing_content_exclusion=(status=="excluded" and not description_available and
            any(phrase in str(reason).casefold() for phrase in
                ("description", "job content", "posting details")) and not deterministic)
        # Legacy matcher rows used `excluded` for a missing feed description.
        # That is not a terminal eligibility decision: the job cannot be
        # assessed until usable source content is available.
        pending=(status not in {"excluded","failed"} and not (status=="completed" and fresh)) or missing_content_exclusion
        reused=bool(not pending and status=="completed" and fresh and row.get("matching_model_version") and
                    session_created and row.get("updated_at") and row["updated_at"]<=session_created)
        analyzed=bool(not pending and status=="completed" and fresh and row.get("matching_model_version") and not reused)
        outcomes.append({"job_key":key,"company":row.get("company"),"title":row.get("title"),
            "status":status or "pending","classification":classification,"reason":reason,
            "description_available":description_available,
            "description_error":row.get("description_error") or "","description_source":row.get("description_source") or "",
            "analyzed":analyzed,"reused":reused,"excluded":status=="excluded" and not missing_content_exclusion,
            "unresolved_description":missing_content_exclusion,
            "needs_review":not pending and classification=="needs_review",
            "not_eligible":not pending and classification=="not_eligible",
            "failed":status=="failed","pending":pending,
            "retryable":status=="failed" and description_available,
            "deterministic_filter":deterministic})
    return outcomes


@router.post("/api/workflows",status_code=202)
def start_workflow(payload:WorkflowStart,request:Request):
    _local_only(request);settings=_authorization()
    resume_id,resume_version,profile_version,pref_version,prefs=_versions()
    conflicts=prompt_conflicts(payload.prompt,prefs)
    active_discovery=job_search_agent.active_discovery()
    if active_discovery:
        associated=None
        associated_id=active_discovery.get("workflow_id")
        if not associated_id:
            with database() as db:
                match=db.execute("SELECT workflow_id FROM user_workflows WHERE user_id=? AND discovery_run_id=? ORDER BY created_at DESC LIMIT 1",
                    (USER,active_discovery["run_id"])).fetchone()
                associated_id=match["workflow_id"] if match else None
        if associated_id:
            with database() as db:
                associated=db.execute("SELECT * FROM user_workflows WHERE workflow_id=? AND user_id=?",
                    (associated_id,USER)).fetchone()
        if associated:
            item=_serialize(associated)
            action="resume" if item["status"]=="paused" else "view"
            raise HTTPException(409,{"code":"existing_workflow","message":"A workflow already owns the active discovery run.",
                "workflow":item,"action":action,"discovery_run_id":active_discovery["run_id"]})
        raise HTTPException(409,{"code":active_discovery.get("ownership_state","active_worker"),
            "message":"An unassociated discovery run owns this user's active slot. Inspect it or use the explicit abandoned-run recovery action.",
            "discovery_run_id":active_discovery["run_id"],"cancel_requested":bool(active_discovery.get("cancel_requested")),
            "recovery_available":active_discovery.get("ownership_state") in {"legacy_unleased","lease_expired"}})
    with database() as db:
        prior=db.execute("""SELECT * FROM user_workflows WHERE user_id=? AND status='failed'
            AND current_stage='failed' AND discovered_count>0 ORDER BY updated_at DESC LIMIT 1""",(USER,)).fetchone()
    if prior and prior["prompt"]==payload.prompt:
        try:prior_diagnostics=json.loads(prior["diagnostics_json"] or "{}")
        except (TypeError,json.JSONDecodeError):prior_diagnostics={}
        if "Another match analysis is already active" in str(prior_diagnostics.get("failure","")):
            raise HTTPException(409,{"code":"existing_workflow","message":"This prompt already discovered jobs and is ready to resume matching without repeating discovery.",
                "workflow":_serialize(prior),"action":"resume"})
    stamp=now();workflow_id=str(uuid.uuid4())
    discovery_run_id=str(uuid.uuid4())
    try:
        with database() as db:
            db.execute("BEGIN IMMEDIATE")
            active=db.execute("SELECT * FROM user_workflows WHERE user_id=? AND status IN ('queued','running','paused','needs_clarification') LIMIT 1",(USER,)).fetchone()
            if active:
                item=_serialize(active)
                action=("clarify" if item["status"]=="needs_clarification" else
                        "resume" if item["status"]=="paused" else "view")
                raise HTTPException(409,{"code":"existing_workflow","message":"An active workflow already exists; continue it instead of creating a competing run.",
                    "workflow":item,"action":action})
            db.execute("""INSERT INTO user_workflows(workflow_id,user_id,prompt,status,current_stage,resume_id,resume_version,
                profile_version,preferences_version,diagnostics_json,discovery_run_id,created_at,updated_at)
                VALUES(?,?,?,'needs_clarification','needs_clarification',?,?,?,?,?,?,?,?)""",
                (workflow_id,USER,payload.prompt,resume_id,resume_version,profile_version,pref_version,
                 json.dumps({"clarification_required":conflicts,"discovery_run_id":discovery_run_id}) if conflicts else
                 json.dumps({"discovery_run_id":discovery_run_id}),discovery_run_id,stamp,stamp))
    except sqlite3.IntegrityError as error:
        with database() as db:
            active=db.execute("SELECT * FROM user_workflows WHERE user_id=? AND status IN ('queued','running','paused','needs_clarification') LIMIT 1",(USER,)).fetchone()
        if active:
            item=_serialize(active);action="resume" if item["status"]=="paused" else "view"
            raise HTTPException(409,{"code":"concurrent_workflow_start","message":"Another request created the active workflow first.",
                "workflow":item,"action":action}) from error
        raise
    if not conflicts:
        _write(workflow_id,status="queued",current_stage="queued")
        try:
            _submit(workflow_id)
        except Exception as error:
            detail=f"Workflow could not start its background worker ({type(error).__name__}). Try again."
            _save_diagnostics(workflow_id,failure=detail,failure_stage="worker_startup",failure_type=type(error).__name__)
            _write(workflow_id,status="failed",current_stage="failed",failed_count=1,completed_at=now())
            raise HTTPException(503,detail) from error
    return {**(_serialize(_row(workflow_id)) or {}),"clarification_required":conflicts,
            "automatic_preparation_authorized":bool(settings.get("allow_automated_form_preparation"))}


def _review_question_kind(text):
    value=str(text or "").casefold()
    for kind,terms in (("sponsorship",("sponsor","visa")),("work_authorization",("work authorization","work authorisation","authorized to work","authorised to work")),
        ("graduation_date",("graduat","class of")),("education",("degree","education")),("professional_experience",("experience","years")),
        ("technical_skills",("skill","technology")),("location",("location","country","city","geographic")),
        ("availability",("availability","start date","notice period")),("work_arrangement",("work arrangement","remote","hybrid")),
        ("employment_type",("employment type","internship"))):
        if any(term in value for term in terms):return kind
    return "other"


def _eligibility_review_rows(workflow):
    saved_keys=workflow.get("job_keys")
    if saved_keys is None:
        try:saved_keys=json.loads(workflow.get("job_keys_json") or "[]")
        except (TypeError,json.JSONDecodeError):saved_keys=[]
    keys=list(dict.fromkeys(saved_keys or []))
    matches=resume_job_matcher._match_rows(keys,workflow.get("resume_id")) if keys else []
    by_key={item.get("job_key"):item for item in matches}
    jobs=[];groups={};seen=set()
    try:_,profile_status,profile=resume_job_matcher.active_candidate()
    except HTTPException:profile_status,profile="unverified",{}
    verified_evidence=[]
    if profile_status in {"user_reviewed","field_verified"} and isinstance(profile,dict):
        for key,label in (("technical_skills","Confirmed technical skills"),("skills","Confirmed skills"),
                          ("education_entries","Confirmed education"),("education","Confirmed education"),
                          ("experience_entries","Confirmed professional experience"),("work_experience","Confirmed professional experience"),
                          ("internship_entries","Confirmed internships"),("current_location","Confirmed location")):
            value=profile.get(key)
            if value not in (None,"",[],{}):verified_evidence.append(label)
    verified_evidence=list(dict.fromkeys(verified_evidence))
    with database() as db:
        for key in keys:
            match=by_key.get(key) or {}
            if match.get("classification")!="needs_review":continue
            opening=db.execute("SELECT company,title,location,description FROM discovered_openings WHERE user_id=? AND job_key=?",(USER,key)).fetchone()
            description_text=" ".join(str(opening["description"] or "").casefold().split()) if opening else ""
            data=match.get("match") if isinstance(match.get("match"),dict) else {}
            missing=list(dict.fromkeys(str(x) for x in data.get("missing_information",[]) if str(x).strip()))
            requirements=[]
            for group in ("required_skills","required_experience","education_requirements","preferred_skills","preferred_experience"):
                for entry in data.get(group,[]) if isinstance(data.get(group),list) else []:
                    if isinstance(entry,dict):
                        requirement=str(entry.get("requirement") or "").strip()
                        # Models sometimes repeat candidate facts as job
                        # requirements. Show a qualification as source-backed
                        # only when its wording appears in the saved posting.
                        if requirement and " ".join(requirement.casefold().split()) in description_text:
                            requirements.append({"requirement":requirement,"status":str(entry.get("status") or "unclear"),"preferred":group.startswith("preferred_"),"candidate_evidence":entry.get("candidate_evidence",[])})
            try:
                from job_relevance import extract_experience_requirements
                extracted=extract_experience_requirements(opening["description"] if opening else "")
                for category,items in extracted.items():
                    for item in items:
                        phrase=item.get("requirement") or ""
                        if phrase and not any(existing["requirement"].casefold()==phrase.casefold() for existing in requirements):
                            requirements.append({"requirement":phrase,"status":category,"preferred":category=="preferred",
                                                 "candidate_evidence":[],"source":"job description"})
            except (TypeError,AttributeError):
                pass
            unique_requirements=[];requirement_keys=set()
            for entry in requirements:
                identity=" ".join(entry["requirement"].casefold().split())
                if identity and identity not in requirement_keys:
                    requirement_keys.add(identity);unique_requirements.append(entry)
            requirements=unique_requirements
            for question in missing:
                kind=_review_question_kind(question)
                if kind=="other":continue
                if kind not in groups:groups[kind]={"key":kind,"question":question,"jobs":[]}
                groups[kind]["jobs"].append({"job_key":key,"title":opening["title"] if opening else "Opening"})
            conflicts=[e["requirement"] for e in requirements if e["status"]=="confirmed_mismatch"]
            jobs.append({"job_key":key,"employer":opening["company"] if opening else "Employer","title":opening["title"] if opening else "Opening",
                "location":opening["location"] if opening else "Location not listed","classification":match.get("classification"),
                "fresh":not bool(match.get("stale")),"required_qualifications":[x for x in requirements if not x["preferred"]],
                "preferred_qualifications":[x for x in requirements if x["preferred"]],
                "verified_candidate_evidence":verified_evidence,
                "ai_cited_candidate_evidence":list(dict.fromkeys(v for x in requirements for v in x["candidate_evidence"] if isinstance(v,str))),
                "missing_candidate_evidence":missing,"confirmed_conflicts":conflicts,
                "uncertain_requirements":[x["requirement"] for x in requirements if x["status"] in {"gap","unclear","unknown"}],
                "ai_explanation":data.get("explanation") or "Eligibility evidence is incomplete.",
                "recommended_next_action":"Confirm missing facts in Candidate Profile, then reassess this saved job.",
                "review_status":"open","reassessment_status":"current" if not match.get("stale") else "awaiting reassessment",
                "application_preparation_status":"not queued"})
            app=db.execute("SELECT state FROM applications WHERE user_id=? AND job_key=?",(USER,key)).fetchone()
            if app:jobs[-1]["application_preparation_status"]=app["state"].replace("_"," ")
            events=db.execute("SELECT action,profile_fingerprint,preferences_fingerprint,job_fingerprint,matching_engine_version,match_updated_at FROM eligibility_review_events WHERE user_id=? AND workflow_id=? AND job_key=? ORDER BY id DESC LIMIT 1",(USER,workflow["workflow_id"],key)).fetchone()
            if events:
                stale=(events["profile_fingerprint"]!=match.get("candidate_fingerprint") or events["preferences_fingerprint"]!=match.get("preferences_fingerprint") or events["job_fingerprint"]!=match.get("job_fingerprint") or events["matching_engine_version"]!=match.get("matching_model_version") or events["match_updated_at"]!=match.get("updated_at"))
                jobs[-1]["review_status"]=("open" if stale and events["action"] in {"confirm","reassess"} and not match.get("stale")
                                            else "stale" if stale else events["action"])
    prompts={"sponsorship":"For which country should your sponsorship answer be recorded?",
        "work_authorization":"Confirm the country specific work authorization in Candidate Profile.",
        "graduation_date":"What is your confirmed graduation month and year?",
        "education":"Which education entry or degree should be confirmed in Candidate Profile?",
        "professional_experience":"What professional experience and dates can you confirm for your profile?",
        "technical_skills":"Which technical skills can you confirm for your profile?",
        "location":"What current city and country should be recorded?",
        "availability":"What is your earliest confirmed availability?",
        "work_arrangement":"Which work arrangements should be set in Career Preferences?",
        "employment_type":"Which employment types are you available for?"}
    for group in groups.values():group["question"]=prompts.get(group["key"],group["question"])
    return {"jobs":jobs,"questions":list(groups.values())}


@router.get("/api/workflows/{workflow_id}/eligibility-reviews")
def eligibility_reviews(workflow_id:str,request:Request):
    _local_only(request)
    workflow=_row(workflow_id)
    if not workflow or workflow.get("user_id")!=USER:raise HTTPException(404,"Workflow not found.")
    return _eligibility_review_rows(workflow)


@router.post("/api/workflows/{workflow_id}/eligibility-review")
def eligibility_review_action(workflow_id:str,payload:EligibilityAction,request:Request):
    _local_only(request)
    workflow=_row(workflow_id)
    if not workflow or workflow.get("user_id")!=USER:raise HTTPException(404,"Workflow not found.")
    selected=workflow.get("job_keys")
    if selected is None:
        try:selected=json.loads(workflow.get("job_keys_json") or "[]")
        except (TypeError,json.JSONDecodeError):selected=[]
    if payload.job_key not in (selected or []):raise HTTPException(404,"Opening is not selected in this workflow.")
    resume_id,_,_=resume_job_matcher.active_candidate()
    rows=resume_job_matcher._match_rows([payload.job_key],resume_id)
    match=rows[0] if rows else None
    if not match:raise HTTPException(409,"Analyze this saved opening before reviewing eligibility.")
    result=match.get("match") or {}
    if match.get("classification")=="not_eligible":raise HTTPException(409,"A confirmed mandatory conflict blocks review and application preparation.")
    affected_keys=[payload.job_key]
    related_matches=[match]
    if payload.action=="confirm":
        if not payload.confirmed or not payload.answer.strip():raise HTTPException(422,"Confirm that the answer is accurate before saving it.")
        allowed={"graduation_date","technical_skills","professional_experience","location","availability","employment_type"}
        if payload.question_key not in allowed:raise HTTPException(422,"Confirm work authorization and other sensitive facts in Candidate Profile, where they receive the required protection.")
        try:
            review_data=_eligibility_review_rows(workflow)
            affected_keys=list(dict.fromkeys(job["job_key"] for group in review_data["questions"]
                if group["key"]==payload.question_key for job in group["jobs"])) or [payload.job_key]
        except (sqlite3.OperationalError,HTTPException,KeyError,TypeError):
            affected_keys=[payload.job_key]
        related_matches=resume_job_matcher._match_rows(affected_keys,resume_id) or [match]
        candidate_verification.save_application_answer(payload.question_key,candidate_verification.AnswerUpdate(question=payload.question.strip() or payload.question_key.replace("_"," ").title(),answer=payload.answer,confirmed=True,auto_reuse=payload.reusable))
    elif payload.action=="approve_dry_run":
        if match.get("stale") or match.get("freshness_status")!="current":raise HTTPException(409,"Reassess this opening after candidate or job changes before approval.")
        application_agent.review_saved_job_eligibility(payload.job_key,application_agent.EligibilityReviewRequest(reviewed=True))
    elif payload.action in {"reject","defer"}:
        application_agent.record_eligibility_decision(payload.job_key,"rejected" if payload.action=="reject" else "deferred")
    elif payload.action!="reassess":raise HTTPException(422,"Unknown review action.")
    stamp=now()
    with database() as db:
        audit_matches=related_matches if payload.action=="confirm" else [match]
        for prior in audit_matches:
            previous_result=prior.get("match") if isinstance(prior.get("match"),dict) else result
            details=({"question_key":payload.question_key,"previous_match_result":previous_result}
                     if payload.action=="confirm" else
                     {"previous_match_result":previous_result} if payload.action=="reassess" else {})
            db.execute("INSERT INTO eligibility_review_events(user_id,workflow_id,job_key,action,classification,resume_id,profile_fingerprint,preferences_fingerprint,job_fingerprint,matching_engine_version,match_updated_at,details_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (USER,workflow_id,prior.get("job_key") or payload.job_key,payload.action,prior.get("classification") or "",resume_id,prior.get("candidate_fingerprint") or "",prior.get("preferences_fingerprint") or "",prior.get("job_fingerprint") or "",prior.get("matching_model_version") or "",prior.get("updated_at") or "",json.dumps(details),stamp))
            if payload.action in {"confirm","reassess"}:
                db.execute("UPDATE job_match_results SET freshness_status='stale' WHERE user_id=? AND resume_id=? AND job_key=? AND status='completed'",(USER,resume_id,prior.get("job_key") or payload.job_key))
    if payload.action=="reassess":
        response=resume_job_matcher.analyze_matches(resume_job_matcher.MatchRequest(job_keys=[payload.job_key]),workflow_id=workflow_id)
        return {"action":"reassess","matching":response,"message":"Reassessment started for this saved job; employer discovery was not repeated."}
    if payload.action=="confirm":
        try:
            response=resume_job_matcher.analyze_matches(resume_job_matcher.MatchRequest(job_keys=affected_keys))
            return {"action":"confirm","saved":True,"matching":response,"submission_enabled":False,
                    "affected_jobs":affected_keys,"message":f"Confirmed answer saved. Targeted reassessment started for {len(affected_keys)} related saved job(s) without employer rediscovery."}
        except HTTPException as error:
            return {"action":"confirm","saved":True,"reassessment_error":str(error.detail),"submission_enabled":False,
                    "message":"Confirmed answer was saved. Reassessment could not start: "+str(error.detail)}
    return {"action":payload.action,"saved":True,"submission_enabled":False,
        "message":"Review decision saved."}


@router.get("/api/workflows/current")
def current_workflow(request:Request):
    _local_only(request)
    # Reconcile only provably dead, expired worker leases before reporting
    # persisted state; active workers are never changed on module import/poll.
    job_search_agent.active_discovery()
    resume_job_matcher.reconcile_matching_sessions()
    with database() as db:row=db.execute("SELECT * FROM user_workflows WHERE user_id=? ORDER BY created_at DESC LIMIT 1",(USER,)).fetchone()
    if not row:return {"workflow":None,"submission_enabled":False}
    item=_serialize(row)
    keys=item.get("job_keys",[])
    if (item.get("status")=="failed" and item.get("discovered_count",0)>0 and
            "Another match analysis is already active" in str((item.get("diagnostics") or {}).get("failure",""))):
        item.setdefault("diagnostics",{})["matching"]={"status":"ready_to_resume","total":item["discovered_count"],
            "completed":item.get("matched_count",0),"fresh_completed":item.get("matched_count",0),
            "pending":max(0,item["discovered_count"]-item.get("matched_count",0)),"failed":0,
            "error":"The earlier matching request was blocked by a paused legacy session. Its discovered jobs are preserved."}
        item["diagnostics"]["recovery"]="Choose Resume matching to continue with saved jobs; discovery will not run again."
    run_id=(item.get("diagnostics") or {}).get("discovery_run_id")
    if run_id:
        with job_search_agent.database() as db:
            run=db.execute("SELECT status,total_sources,sources_checked,postings_retrieved,diagnostics_json FROM job_discovery_runs WHERE run_id=? AND user_id=?",
                           (run_id,USER)).fetchone()
        if run:
            try:details=json.loads(run["diagnostics_json"] or "{}")
            except (TypeError,json.JSONDecodeError):details={}
            item.setdefault("diagnostics",{})["discovery"]={**details,"status":run["status"],
                "total_sources":run["total_sources"],"sources_checked":run["sources_checked"],
                "postings_retrieved":run["postings_retrieved"]}
    matching_session_id=item.get("matching_session_id") or (item.get("diagnostics") or {}).get("matching_session_id")
    if matching_session_id:
        try:
            progress=resume_job_matcher._progress(matching_session_id,keys)
            item.setdefault("diagnostics",{})["matching"]={**(item.get("diagnostics",{}).get("matching") or {}),**progress}
        except Exception:pass
    if keys:
        with application_agent.connect() as db:
            apps=db.execute("SELECT state,count(*) n FROM applications WHERE user_id=? AND job_key IN (SELECT value FROM json_each(?)) GROUP BY state",(USER,json.dumps(keys))).fetchall()
        item["application_counts"]={row["state"]:row["n"] for row in apps}
    else:item["application_counts"]={}
    item["prepared_count"]=item["application_counts"].get("ready_for_review",0)+item["application_counts"].get("needs_user_input",0)
    item["queued_count"]=max(item.get("queued_count",0),item["application_counts"].get("queued",0))
    return {"workflow":item,"submission_enabled":False}


def _control(request,workflow_id,action):
    _local_only(request)
    with database() as db:row=db.execute("SELECT * FROM user_workflows WHERE workflow_id=? AND user_id=?",(workflow_id,USER)).fetchone()
    if not row:raise HTTPException(404,"Workflow not found.")
    worker_state=None
    if action=="pause":
        if row["status"] not in {"queued","running"}:raise HTTPException(409,"Workflow is not running.")
        _write(workflow_id,pause_requested=1,status="paused")
        if row["current_stage"]=="discovering":
            try:
                discovery_id=row["discovery_run_id"] or json.loads(row["diagnostics_json"] or "{}").get("discovery_run_id")
                if discovery_id:job_search_agent.pause_discovery(discovery_id)
            except Exception:pass
        elif row["current_stage"]=="matching":
            try:resume_job_matcher.pause_matches(session_id=row["matching_session_id"] or None)
            except Exception:pass
        try:application_agent.pause_applications()
        except Exception:pass
    elif action=="resume":
        try:existing_diagnostics=json.loads(row["diagnostics_json"] or "{}")
        except (TypeError,json.JSONDecodeError):existing_diagnostics={}
        resumable_match_failure=(row["status"]=="failed" and row["current_stage"]=="failed" and
            row["discovered_count"]>0 and "Another match analysis is already active" in str(existing_diagnostics.get("failure","")))
        if row["status"] not in {"paused","needs_clarification"} and not resumable_match_failure:
            raise HTTPException(409,"Workflow cannot be resumed in its current state.")
        _authorization()
        try:_ensure_snapshot(workflow_id)
        except HTTPException as error:
            _save_diagnostics(workflow_id,failure=str(error.detail)[:500])
            _write(workflow_id,status="failed",current_stage="failed",completed_at=now())
            raise
        if row["status"]=="needs_clarification":raise HTTPException(409,"Update the saved preferences or start a new prompt after resolving the conflict.")
        with database() as db:
            db.execute("BEGIN IMMEDIATE")
            fresh=db.execute("SELECT status,current_stage FROM user_workflows WHERE workflow_id=? AND user_id=?",
                              (workflow_id,USER)).fetchone()
            can_recover_failed=(fresh and fresh["status"]=="failed" and resumable_match_failure)
            if not fresh or (fresh["status"]!="paused" and not can_recover_failed):
                raise HTTPException(409,"Another request already resumed or changed this workflow; view its current status.")
            target_stage="matching" if can_recover_failed else fresh["current_stage"]
            changed=db.execute("UPDATE user_workflows SET pause_requested=0,status='queued',current_stage=?,updated_at=? WHERE workflow_id=? AND user_id=? AND status=?",
                               (target_stage,now(),workflow_id,USER,fresh["status"])).rowcount
            if not changed:raise HTTPException(409,"Another request already resumed this workflow; view its current status.")
        try:application_agent.resume_processing()
        except Exception:pass
        try:
            submitted=_submit(workflow_id)
            worker_state="starting" if submitted else "finishing_previous_worker"
        except Exception as error:
            _save_diagnostics(workflow_id,failure=f"{type(error).__name__}: the workflow worker could not start. Retry Resume; saved jobs and matching results were preserved.",
                              failure_type=type(error).__name__)
            _write(workflow_id,status="paused",current_stage=target_stage,pause_requested=0)
            raise HTTPException(503,"The workflow worker could not start. Saved jobs and matching results are preserved; retry Resume.") from error
    else:
        if row["status"] in {"completed","cancelled","failed"}:raise HTTPException(409,"Workflow is already finished.")
        _write(workflow_id,cancel_requested=1,pause_requested=0,status="cancelled",current_stage="cancelled",completed_at=now())
        try:
            discovery_id=row["discovery_run_id"] or json.loads(row["diagnostics_json"] or "{}").get("discovery_run_id")
            if discovery_id:job_search_agent.cancel_discovery(discovery_id)
        except Exception:pass
        try:
            if row["matching_session_id"]:resume_job_matcher.cancel_matches(session_id=row["matching_session_id"])
        except Exception:pass
        try:application_agent.pause_applications()
        except Exception:pass
    response={"workflow":_serialize(_row(workflow_id)),"submission_enabled":False}
    if worker_state:response["worker_state"]=worker_state
    return response


@router.post("/api/workflows/{workflow_id}/pause")
def pause_workflow(workflow_id:str,request:Request):return _control(request,workflow_id,"pause")


@router.post("/api/workflows/{workflow_id}/resume")
def resume_workflow(workflow_id:str,request:Request):return _control(request,workflow_id,"resume")


@router.post("/api/workflows/{workflow_id}/cancel")
def cancel_workflow(workflow_id:str,request:Request):return _control(request,workflow_id,"cancel")


@router.post("/api/workflows/discovery/{run_id}/recover")
def recover_discovery(run_id:str,payload:DiscoveryRecovery,request:Request):
    _local_only(request)
    result=job_search_agent.recover_abandoned_discovery(run_id,payload.confirm_abandoned)
    return {"recovery":result,"submission_enabled":False}


@router.post("/api/workflows/matching/{session_id}/recover")
def recover_matching(session_id:str,payload:MatchingRecovery,request:Request):
    _local_only(request)
    result=resume_job_matcher.recover_match_session(session_id,payload.confirm_abandoned)
    return {"recovery":result,"submission_enabled":False}


@router.post("/api/workflows/{workflow_id}/enrich")
def retry_workflow_enrichment(workflow_id:str,payload:EnrichmentRetry,request:Request):
    """Retry selected saved jobs and start a matching-only child workflow."""
    _local_only(request)
    parent=_row(workflow_id)
    if not parent:raise HTTPException(404,"Workflow was not found.")
    selected=list(dict.fromkeys(payload.job_keys))
    original=set(json.loads(parent["job_keys_json"] or "[]"))
    if not set(selected).issubset(original):raise HTTPException(400,"Select jobs from this workflow only.")
    if parent["status"] in {"running","paused"}:
        parent_state=_serialize(parent) or {}
        if (parent_state.get("outcome_counts") or {}).get("pending",0):
            raise HTTPException(409,"Finish or pause the current matching checkpoint before starting a matching-only retry.")
        _write(workflow_id,status="completed",current_stage="completed",completed_at=now(),pause_requested=0)
        parent=_row(workflow_id)
    result=job_description_enricher.enrich_saved_jobs(selected,limit=12)
    if not (result["succeeded"] or result.get("available")):
        return {"enrichment":result,"matching_workflow_id":None}
    child_id=str(uuid.uuid4());stamp=now()
    with database() as db:
        db.execute("""INSERT INTO user_workflows(workflow_id,user_id,prompt,status,current_stage,resume_id,resume_version,
            profile_version,preferences_version,job_keys_json,diagnostics_json,discovered_count,created_at,updated_at)
            VALUES(?,?,?,'queued','matching',?,?,?,?,?,?,?, ?,?)""",
            (child_id,USER,parent["prompt"],parent["resume_id"],parent["resume_version"],parent["profile_version"],
             parent["preferences_version"],json.dumps(selected),json.dumps({"parent_workflow_id":workflow_id,
             "enrichment":result,"discovery_reused":True}),len(selected),stamp,stamp))
    if not _submit(child_id):raise HTTPException(503,"Could not start matching for the enriched saved jobs.")
    return {"enrichment":result,"matching_workflow_id":child_id,"discovery_reused":True,"submission_enabled":False}


@router.get("/workflow",response_class=HTMLResponse)
def workflow_page():return HTMLResponse(apply_ui_theme(WORKFLOW_PAGE))


WORKFLOW_PAGE=r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Job search workflow — BabyStepsAI</title>
<style>:root{--ink:#17263b;--muted:#66758a;--line:#dfe7f1;--surface:#fff;--wash:#f4f7fb;--brand:#2563eb}*{box-sizing:border-box}body{margin:0;background:var(--wash);color:var(--ink);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.wrap{max-width:1060px;margin:26px auto;padding:0 18px}h1{font-size:clamp(25px,4vw,36px);line-height:1.2;margin:10px 0}.muted{color:var(--muted)}.panel,.metric{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:18px;margin:14px 0}.prompt{width:100%;min-height:105px;font:inherit;border:1px solid var(--line);border-radius:10px;padding:12px;background:var(--surface);color:var(--ink);resize:vertical}.actions{display:flex;gap:9px;flex-wrap:wrap;margin-top:12px}button{font:inherit;font-weight:650;padding:9px 14px;border:1px solid var(--line);border-radius:9px;background:var(--surface);color:var(--ink);cursor:pointer}button.primary{background:var(--brand);color:white;border-color:var(--brand)}button:disabled{opacity:.5;cursor:not-allowed}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}.metric strong{display:block;font-size:24px}.metric span{color:var(--muted);font-size:14px}.stage{font-weight:700;text-transform:capitalize}.bar{height:8px;background:#e7edf5;border-radius:99px;overflow:hidden;margin:9px 0}.bar i{display:block;height:100%;background:var(--brand);width:0;transition:width .2s}.notice{padding:11px;border-radius:9px;background:#eef4ff}.outcomes{display:grid;gap:10px}.outcome{border:1px solid var(--line);border-radius:12px;padding:13px}.outcome-head{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}.badge{font-size:12px;background:var(--wash);border-radius:99px;padding:4px 9px;white-space:nowrap}.outcome p{margin:7px 0;font-size:13px}.outcome button{margin-top:4px}.recent{margin:0;padding-left:20px}.error{color:#a22}@media(max-width:600px){.wrap{padding:0 12px}.panel{padding:14px}}</style></head><body><main class="wrap"><p class="muted">One prompt · verified employer feeds · eligibility checks · application dry runs</p><h1>Your job search workflow</h1><p class="muted">Search and review suitable roles in one flow. Application preparation requires separate authorization. BabyStepsAI stops before final submission.</p>
<section class="panel"><label for="prompt"><strong>What kind of work are you looking for?</strong></label><textarea id="prompt" class="prompt" maxlength="1500" placeholder="Find entry-level software engineering jobs in the United States. Exclude internships and senior positions."></textarea><div class="actions"><button id="start" class="primary">Start workflow</button><button id="pause" disabled>Pause</button><button id="resume" disabled>Resume</button><button id="cancel" disabled>Cancel</button><button id="recover" hidden>Recover abandoned search</button><button id="recoverMatch" hidden>Recover abandoned matching</button></div><p id="message" class="muted" role="status" aria-live="polite"></p></section>
<section class="panel"><h2>Progress</h2><div class="stage" id="stage">No workflow started</div><div class="bar"><i id="progress"></i></div><div class="grid" id="metrics"></div><p id="diagnostics" class="notice" hidden></p></section><section class="panel"><h2>Matching Details</h2><p class="muted">Every selected opening shows its matching status, eligibility classification, and any recorded failure. Retries use saved jobs and do not repeat employer discovery.</p><button id="retry-failed" hidden>Retry failed analyses</button><div id="job-outcomes" class="outcomes"></div></section><section class="panel"><h2>Recent activity</h2><ol id="activity" class="recent"></ol></section><section class="panel"><h2>Application preparation</h2><p class="muted">Queued jobs are prepared one at a time using confirmed profile facts and your active resume. Missing or sensitive questions pause that job while the queue continues. Review prepared applications before any future submission functionality.</p><div id="application-state" class="grid"></div><p class="muted"><a href="/applications">Open application history</a> · <a href="/application-settings">Review permissions</a> · <a href="/candidate-profile">Verify your profile</a></p></section></main>
<script>
const $=s=>document.querySelector(s);let current=null,resumePending=false;
const labels={analyzed:'Analyzed by AI',reused:'Current results reused',excluded:'Excluded by filters or missing content',needs_review:'Needs review',not_eligible:'Not eligible',failed:'Failed jobs',pending:'Pending jobs',retryable:'Retryable analyses',discovered:'Jobs discovered',potential_match:'Potential matches',queued:'Applications queued',prepared:'Ready for review',failed_steps:'Failed steps'};
function errorText(value){if(typeof value==='string')return value;if(value&&typeof value==='object'&&!Array.isArray(value)){const action=value.action==='resume'?' Use Resume to continue the existing workflow.':value.action==='view'?' Open the current workflow to view progress.':'';return (value.message||'The server could not complete this request.')+action}if(Array.isArray(value))return value.map(item=>{const where=Array.isArray(item.loc)?item.loc.filter(part=>part!=='body').join(' → '):'';return (where?where+': ':'')+(item.msg||'Invalid request')}).join(' ');return 'The server could not complete this request.'}
async function call(url,opt={}){let r;try{r=await fetch(url,{...opt,headers:{Accept:'application/json','Content-Type':'application/json',...(opt.headers||{})}})}catch(_e){throw Error('Could not reach BabyStepsAI. Check that the local server is running and try again.')}const type=r.headers.get('content-type')||'';let d;if(type.includes('json')){try{d=await r.json()}catch(_e){throw Error('BabyStepsAI returned an unreadable response. Check the server log and try again.')}}else{const body=await r.text();if(!r.ok)throw Error('Request failed (HTTP '+r.status+'). '+(body.slice(0,180)||'Check the server log.'));throw Error('BabyStepsAI returned an unexpected response. Check the server log and try again.')}if(!r.ok){const error=Error(errorText(d.detail)||('Request failed (HTTP '+r.status+').'));error.detail=d.detail;throw error}return d}
function metric(name,value){const d=document.createElement('div');d.className='metric';const strong=document.createElement('strong');strong.textContent=value??0;const span=document.createElement('span');span.textContent=name;d.append(strong,span);return d}
function outcomeLabel(j){if(j.analyzed)return 'Analyzed by AI';if(j.reused)return 'Current matching result reused';if(j.pending)return j.unresolved_description?'Description missing — unresolved':'Pending matching';if(j.failed)return 'Failed';if(j.needs_review)return 'Needs review';if(j.not_eligible)return 'Not eligible';if(j.excluded)return j.description_available?'Excluded by preferences or relevance':'Description missing — unresolved';return 'Needs review'}
function renderOutcomes(w){const root=$('#job-outcomes'),rows=w.job_outcomes||[];if(!rows.length){root.textContent='No saved jobs in this workflow yet.';return}root.replaceChildren(...rows.map(j=>{const card=document.createElement('article');card.className='outcome';const head=document.createElement('div');head.className='outcome-head';const title=document.createElement('strong');title.textContent=(j.company||'Employer')+' · '+(j.title||j.job_key);const badge=document.createElement('span');badge.className='badge';badge.textContent=outcomeLabel(j);head.append(title,badge);const classification=document.createElement('p');classification.textContent='Eligibility: '+(j.classification||'Unclassified');const desc=document.createElement('p');desc.textContent=j.description_available?'Description available'+(j.description_source?' · Source: '+j.description_source:''):'Description unavailable'+(j.description_error?' · '+j.description_error:'');const reason=document.createElement('p');reason.className='muted';reason.textContent=j.reason||'';card.append(head,classification,desc);if(j.reason)card.append(reason);if((!j.description_available||j.failed)&&w.workflow_id){const retry=document.createElement('button');retry.textContent=j.failed?'Retry analysis':(j.description_available?'Retry matching':'Retry description + matching');retry.onclick=async()=>{retry.disabled=true;try{const d=await call('/api/workflows/'+encodeURIComponent(w.workflow_id)+'/enrich',{method:'POST',body:JSON.stringify({job_keys:[j.job_key]})});const success=(d.enrichment?.succeeded||0)+(d.enrichment?.available||0);$('#message').textContent=success?(j.description_available?'Matching-only workflow started; discovery was not repeated.':'Description retrieved. A matching-only workflow started; discovery was not repeated.'):'Description retry finished: '+(d.enrichment?.results?.[0]?.reason||'no public description was available.');refresh()}catch(e){$('#message').textContent=e.message;retry.disabled=false}};card.append(retry)}return card}))}
function render(w){current=w;if(!w){$('#stage').textContent='No workflow started';$('#metrics').replaceChildren();return}$('#prompt').value=w.prompt||'';$('#stage').textContent=(w.status||'').replaceAll('_',' ')+' · '+(w.current_stage||'').replaceAll('_',' ');const order=['queued','authorizing','discovering','matching','queueing','completed'];$('#progress').style.width=Math.max(4,100*Math.max(0,order.indexOf(w.current_stage))/(order.length-1))+'%';const counts=w.outcome_counts||{},outcomes=w.job_outcomes||[],enrichment=w.diagnostics?.enrichment||{};const retryFailed=$('#retry-failed'),failedKeys=outcomes.filter(j=>j.failed&&j.description_available).map(j=>j.job_key);retryFailed.hidden=!failedKeys.length||!['completed','paused','failed'].includes(w.status);retryFailed.textContent='Retry '+failedKeys.length+' failed matching job'+(failedKeys.length===1?'':'s')+' without rediscovery';retryFailed.disabled=false;retryFailed.onclick=async()=>{retryFailed.disabled=true;try{const result=await call('/api/workflows/'+encodeURIComponent(w.workflow_id)+'/enrich',{method:'POST',body:JSON.stringify({job_keys:failedKeys})});$('#message').textContent=result.matching_workflow_id?'Matching-only retry started for '+failedKeys.length+' saved jobs. Employer discovery was not repeated.':'No saved descriptions were ready to retry.';refresh()}catch(error){$('#message').textContent=error.message;retryFailed.disabled=false}};const values=[['Employers searched',w.diagnostics?.discovery?.sources_checked||w.diagnostics?.eligible_sources||0],['Descriptions available',outcomes.filter(j=>j.description_available).length],['Descriptions missing',outcomes.filter(j=>!j.description_available).length],['Enrichment attempted',enrichment.attempted||0],['Enrichment succeeded',enrichment.succeeded||0],['Enrichment failed',enrichment.failed||0],['Awaiting reassessment',w.review_counts?.awaiting_reassessment||0],['Approved for dry run',w.review_counts?.approved_for_dry_run||0],['Applications needing input',w.application_counts?.needs_user_input||0],['Failed or unsupported applications',Number(w.application_counts?.failed||0)+Number(w.application_counts?.unsupported||0)],...Object.entries(labels).map(([key,label])=>[label,key==='discovered'?w.discovered_count:key==='potential_match'?w.potential_match_count:key==='queued'?w.queued_count:key==='prepared'?w.prepared_count:key==='failed_steps'?w.failed_count:counts[key]||0])];$('#metrics').replaceChildren(...values.map(([k,v])=>metric(k,v)));renderOutcomes(w);renderEligibilityReviews(w);const app=w.application_counts||w.diagnostics?.application_states||{};$('#application-state').replaceChildren(...[['Queued',app.queued||0],['Preparing',Number(app.opening||0)+Number(app.inspecting||0)+Number(app.filling||0)+Number(app.uploading_resume||0)],['Needs your input',app.needs_user_input||0],['Ready for review',app.ready_for_review||0]].map(([k,v])=>metric(k,v)));const d=w.diagnostics||{},liveMatching={...(d.matching||{}),...(w.matching_progress||{})},events=d.activity||[];$('#activity').replaceChildren(...events.slice(-8).reverse().map(e=>{const li=document.createElement('li'),date=new Date(e.at);li.textContent=(Number.isNaN(date.getTime())?'':date.toLocaleString()+' · ')+(e.stage||e.status||'Workflow update').replaceAll('_',' ');return li}));const discovery=d.discovery||{},feeds=(discovery.feed_failures||[]).map(f=>`${f.company} (${f.source_platform||'source'}${f.stage?' · '+f.stage:''}): ${f.reason}`).join(' '),details=[];if(discovery.current_source?.company)details.push('Checking '+discovery.current_source.company);if(discovery.last_successful_checkpoint?.company)details.push('Last successful source: '+discovery.last_successful_checkpoint.company);if(liveMatching?.provider)details.push('Provider: '+liveMatching.provider+(liveMatching.provider_model?' · '+liveMatching.provider_model:''));if(w.matching_worker_state==='resume_requested')details.push('Resume requested · waiting for a worker to claim the saved checkpoint');else if(w.matching_worker_state==='running')details.push('Matching worker running');else if(w.matching_worker_state==='stopping')details.push('Pause requested · matching worker is stopping');if(w.matching_checkpoint_mismatch)details.push('The linked matching checkpoint belongs to another workflow. Its history is preserved; Resume will use an independent checkpoint for these saved jobs.');if(liveMatching?.status)details.push('AI matching '+liveMatching.status+' ('+(liveMatching.fresh_completed||liveMatching.completed||0)+'/'+(liveMatching.total||0)+')');if(liveMatching&&Number.isFinite(Number(liveMatching.pending)))details.push(liveMatching.pending+' jobs pending analysis');if(w.job_outcomes?.length)details.push(...w.job_outcomes.filter(j=>j.excluded||j.not_eligible||j.failed||j.pending||j.needs_review).map(j=>`${j.company||'Employer'} — ${j.title||j.job_key}: ${j.status==='excluded'?'Excluded: '+(j.reason||'filtered'):j.failed?'Failed: '+(j.reason||'analysis failed'):j.pending?(j.unresolved_description?'Description missing — unresolved':'Pending analysis'):j.not_eligible?'Not eligible':j.needs_review?'Needs review':''}`));if(liveMatching?.current_job?.title)details.push('Analyzing '+liveMatching.current_job.title+(liveMatching.current_job.company?' at '+liveMatching.current_job.company:''));if(liveMatching?.last_successful_checkpoint?.title)details.push('Last matching checkpoint: '+liveMatching.last_successful_checkpoint.title+(liveMatching.last_successful_checkpoint.company?' at '+liveMatching.last_successful_checkpoint.company:''));if(liveMatching?.error)details.push(liveMatching.error);if(feeds)details.push(feeds);const primary=d.clarification_required?.join(' ')||d.failure||d.recovery||d.source_note||'';if(primary)details.unshift(primary);const text=details.join(' · ');$('#diagnostics').hidden=!text;$('#diagnostics').textContent=text;$('#pause').disabled=!['queued','running'].includes(w.status);const resumableMatchFailure=w.status==='failed'&&Number(w.discovered_count||0)>0&&String(d.failure||'').includes('Another match analysis is already active');const matcherOwned=w.matching_worker_state==='running';if(resumePending&&matcherOwned){resumePending=false;$('#message').textContent='Matching worker running.'}else if(resumePending&&w.status==='paused'&&!w.pause_requested){resumePending=false;$('#message').textContent=liveMatching?.error||d.failure||'Resume did not start matching. Review the Matching Details and retry.'}$('#resume').disabled=resumePending||!(w.status==='paused'||resumableMatchFailure);$('#resume').textContent=resumePending?'Resume requested…':(resumableMatchFailure?'Resume matching':'Resume');$('#recoverMatch').hidden=!(liveMatching?.recovery_available&&liveMatching?.session_id);$('#recoverMatch').dataset.sessionId=liveMatching?.session_id||'';$('#cancel').disabled=['completed','cancelled','failed'].includes(w.status);$('#start').disabled=['queued','running','paused'].includes(w.status)}
async function renderEligibilityReviews(w){let section=document.getElementById('eligibility-reviews');if(!section){section=document.createElement('section');section.className='panel';section.id='eligibility-reviews';section.innerHTML='<h2>Jobs Requiring Your Review</h2><p class="muted">Confirm factual information once, then reassess saved openings. Confirmed mandatory conflicts stay blocked.</p><div id="eligibility-questions"></div><div id="eligibility-jobs" class="outcomes"></div>';document.getElementById('job-outcomes').parentElement.after(section)}if(!w?.workflow_id){section.hidden=true;return}section.hidden=false;try{const data=await call('/api/workflows/'+encodeURIComponent(w.workflow_id)+'/eligibility-reviews'),questions=document.getElementById('eligibility-questions'),root=document.getElementById('eligibility-jobs');questions.replaceChildren();root.replaceChildren();for(const q of data.questions||[]){const box=document.createElement('div');box.className='outcome';const title=document.createElement('strong');title.textContent='Confirm once · '+q.key.replaceAll('_',' ');const prompt=document.createElement('p');prompt.textContent=q.question;box.append(title,prompt);if(['work_authorization','sponsorship','education','work_arrangement'].includes(q.key)){const note=document.createElement('p');note.textContent=['work_authorization','sponsorship'].includes(q.key)?'Sensitive facts must be confirmed in Candidate Profile and are never copied to an AI prompt by this form.':'Update this structured fact in the matching profile or career preferences page.';const link=document.createElement('a');link.href=q.key==='work_arrangement'?'/careers':'/candidate-profile';link.textContent=q.key==='work_arrangement'?'Open Career Preferences':'Open Candidate Profile';box.append(note,link);questions.append(box);continue}const input=document.createElement('input');input.type='text';input.maxLength=4000;input.placeholder='Your confirmed answer';input.setAttribute('aria-label',q.question);const reuse=document.createElement('input');reuse.type='checkbox';const reuseLabel=document.createElement('label');reuseLabel.append(reuse,document.createTextNode(' Allow reuse in future applications'));const save=document.createElement('button');save.textContent='Confirm Information';save.onclick=async()=>{const job=q.jobs?.[0]?.job_key;if(!job||!input.value.trim()){document.getElementById('message').textContent='Enter and confirm an answer before saving.';return}save.disabled=true;try{const result=await call('/api/workflows/'+encodeURIComponent(w.workflow_id)+'/eligibility-review',{method:'POST',body:JSON.stringify({job_key:job,action:'confirm',question_key:q.key,question:q.question,answer:input.value,reusable:reuse.checked,confirmed:true})});document.getElementById('message').textContent=result.message;renderEligibilityReviews(w)}catch(e){document.getElementById('message').textContent=e.message;save.disabled=false}};box.append(input,reuseLabel,save);questions.append(box)}for(const job of data.jobs||[]){const card=document.createElement('article');card.className='outcome';const head=document.createElement('div');head.className='outcome-head';const title=document.createElement('strong');title.textContent=job.employer+' · '+job.title;const badge=document.createElement('span');badge.className='badge';badge.textContent=job.review_status+' · '+job.reassessment_status;head.append(title,badge);card.append(head);const meta=document.createElement('p');meta.textContent='Location: '+job.location+' · Eligibility: '+job.classification+' · Preparation: '+job.application_preparation_status;card.append(meta);for(const [label,items] of [['Required qualifications',job.required_qualifications?.map(x=>x.requirement+' — '+x.status)],['Preferred qualifications',job.preferred_qualifications?.map(x=>x.requirement+' — '+x.status)],['Verified candidate evidence',job.verified_candidate_evidence],['Missing candidate evidence',job.missing_candidate_evidence],['Confirmed conflicts',job.confirmed_conflicts],['Uncertain requirements',job.uncertain_requirements]])if(items?.length){const p=document.createElement('p');p.textContent=label+': '+items.join('; ');card.append(p)}const explanation=document.createElement('p');explanation.className='muted';explanation.textContent=job.ai_explanation;card.append(explanation);const actions=document.createElement('div');actions.className='actions';const details=document.createElement('button');details.textContent='Review Job';details.onclick=()=>{const expanded=card.dataset.expanded==='true';card.dataset.expanded=String(!expanded);explanation.hidden=expanded};actions.append(details);for(const [label,action] of [['Reassess','reassess'],['Approve Dry Run','approve_dry_run'],['Reject','reject'],['Defer','defer']]){const button=document.createElement('button');button.textContent=label;button.disabled=false;button.onclick=async()=>{button.disabled=true;try{const result=await call('/api/workflows/'+encodeURIComponent(w.workflow_id)+'/eligibility-review',{method:'POST',body:JSON.stringify({job_key:job.job_key,action})});document.getElementById('message').textContent=result.message||'Review action saved.';if(action==='approve_dry_run'){const queued=await call('/api/applications/queue',{method:'POST',body:JSON.stringify({job_keys:[job.job_key]})});document.getElementById('message').textContent=queued.queued?'Eligible opening entered the dry-run queue.':'Queue blocked: '+(queued.skipped?.[0]?.reason||'review eligibility and preparation permissions.')}await renderEligibilityReviews(w);refresh()}catch(e){document.getElementById('message').textContent=e.message;button.disabled=false}};actions.append(button)}card.append(actions);root.append(card)}}catch(e){section.hidden=false;document.getElementById('eligibility-jobs').textContent='Eligibility review details are temporarily unavailable: '+e.message}}
async function refresh(){try{const d=await call('/api/workflows/current');render(d.workflow);if(d.workflow&&(['queued','running'].includes(d.workflow.status)||Object.keys(d.workflow.application_counts||{}).some(k=>['queued','opening','inspecting','filling','uploading_resume'].includes(k))))setTimeout(refresh,1200)}catch(e){$('#message').textContent=e.message}}
$('#start').onclick=async()=>{if($('#start').disabled)return;$('#start').disabled=true;try{$('#message').textContent='Checking permissions and starting…';const d=await call('/api/workflows',{method:'POST',body:JSON.stringify({prompt:$('#prompt').value})});render(d);$('#message').textContent=d.clarification_required?.length?'Resolve the prompt and saved preference conflict before starting.':(d.automatic_preparation_authorized?'Workflow started.':'Workflow started; application preparation is not authorized, so eligible jobs will remain for review.');refresh()}catch(e){$('#message').textContent=e.message;$('#start').disabled=false;const detail=e.detail||{};if(detail.recovery_available&&detail.discovery_run_id){$('#recover').hidden=false;$('#recover').dataset.runId=detail.discovery_run_id}refresh()}};
for(const action of ['pause','resume','cancel'])$('#'+action).onclick=async()=>{if(!current||(action==='resume'&&resumePending))return;if(action==='resume'){resumePending=true;$('#resume').disabled=true;$('#resume').textContent='Resume requested…';$('#message').textContent='Resume requested. Waiting for the matching worker to claim the saved checkpoint…'}try{const d=await call('/api/workflows/'+encodeURIComponent(current.workflow_id)+'/'+action,{method:'POST'});render(d.workflow);if(action==='resume'){const state=d.worker_state||'starting';$('#message').textContent=state==='finishing_previous_worker'?'Resume queued; the previous worker is finishing before the checkpoint is claimed.':'Resume requested; waiting for the matching worker to claim the saved checkpoint.'}else $('#message').textContent='Workflow '+action+' requested.';refresh()}catch(e){if(action==='resume')resumePending=false;$('#message').textContent=e.message;$('#resume').disabled=false;$('#resume').textContent='Resume'}};
$('#recover').onclick=async()=>{const runId=$('#recover').dataset.runId;if(!runId||!window.confirm('Confirm this discovery worker is abandoned. BabyStepsAI will preserve its history and checkpoint.'))return;try{const d=await call('/api/workflows/discovery/'+encodeURIComponent(runId)+'/recover',{method:'POST',body:JSON.stringify({confirm_abandoned:true})});$('#recover').hidden=true;$('#message').textContent=d.recovery.recovered?'Abandoned run recovered. You can start or resume discovery.':'This run is already '+d.recovery.status+'.';refresh()}catch(e){$('#message').textContent=e.message}};
$('#recoverMatch').onclick=async()=>{const sessionId=$('#recoverMatch').dataset.sessionId;if(!sessionId||!window.confirm('Confirm this matching worker is abandoned. Completed results and queued job checkpoints will be preserved.'))return;try{const d=await call('/api/workflows/matching/'+encodeURIComponent(sessionId)+'/recover',{method:'POST',body:JSON.stringify({confirm_abandoned:true})});$('#recoverMatch').hidden=true;$('#message').textContent=d.recovery.recovered?'Abandoned matching worker recovered. Resume this workflow to continue analysis.':'Matching session is '+d.recovery.status+'.';refresh()}catch(e){$('#message').textContent=e.message}};
refresh();
</script></body></html>'''
