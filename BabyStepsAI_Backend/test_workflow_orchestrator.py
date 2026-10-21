import json
import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import workflow_orchestrator as workflow
import job_search_agent


class WorkflowOrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.old_db=workflow.DB
        workflow.DB=Path(self.temp.name)/"workflow.sqlite3"
        workflow.initialize()
        with workflow.database() as db:
            db.execute("""INSERT INTO user_workflows(workflow_id,user_id,prompt,status,current_stage,resume_id,resume_version,
                profile_version,preferences_version,created_at,updated_at)
                VALUES('wf-test',?,'software engineering jobs','queued','queued','resume-1','rv1','pv1',3,'now','now')""",(workflow.USER,))

    def tearDown(self):
        workflow.DB=self.old_db
        self.temp.cleanup()

    def asgi_request(self, method, path, body=None):
        from main import app
        payload=json.dumps(body).encode() if body is not None else b""
        messages=[]
        sent=False
        async def receive():
            nonlocal sent
            if not sent:
                sent=True
                return {"type":"http.request","body":payload,"more_body":False}
            return {"type":"http.disconnect"}
        async def send(message):messages.append(message)
        scope={"type":"http","asgi":{"version":"3.0","spec_version":"2.3"},
               "http_version":"1.1","method":method,"scheme":"http","path":path,
               "raw_path":path.encode(),"query_string":b"","root_path":"",
               "headers":[(b"host",b"127.0.0.1:8000"),(b"content-type",b"application/json")],
               "client":("127.0.0.1",43210),"server":("127.0.0.1",8000)}
        asyncio.run(app(scope,receive,send))
        start=next(message for message in messages if message["type"]=="http.response.start")
        content=b"".join(message.get("body",b"") for message in messages if message["type"]=="http.response.body")
        return start["status"],json.loads(content) if content else {}

    def test_start_workflow_asgi_persists_using_real_preferences_schema(self):
        # Mirror the actual career_preferences schema: preference_version exists,
        # updated_at does not. This exercises the exact failure in the user's DB.
        with workflow.database() as db:
            db.execute("UPDATE user_workflows SET status='completed',current_stage='completed',created_at='2000-01-01',updated_at='2000-01-01' WHERE workflow_id='wf-test'")
            db.executescript("""
                CREATE TABLE resumes(id TEXT,user_id TEXT,created_at TEXT);
                CREATE TABLE candidate_verifications(resume_id TEXT,user_id TEXT,updated_at TEXT);
                CREATE TABLE career_preferences(user_id TEXT,preferences_json TEXT,
                    preference_version INTEGER,preferences_fingerprint TEXT);
            """)
            db.execute("INSERT INTO resumes VALUES('resume-1',?,'resume-created')",(workflow.USER,))
            db.execute("INSERT INTO candidate_verifications VALUES('resume-1',?,'profile-confirmed')",(workflow.USER,))
            db.execute("INSERT INTO career_preferences VALUES(?,?,4,'fingerprint')",
                       (workflow.USER,json.dumps({"roles":["Software Engineer"]})))
        with patch.object(workflow.application_answer_assistant,"get_authorization",return_value={"settings":{
                 "allow_automatic_discovery":True,"allow_ai_matching":True,
                 "allow_automated_form_preparation":False}}), \
             patch.object(workflow.application_agent,"_candidate",return_value=({"id":"resume-1"},{"name":"Confirmed"},{})), \
             patch.object(workflow.candidate_verification,"profile_completeness",return_value={"ready_for_dry_runs":True}), \
             patch.object(workflow.resume_job_matcher,"active_candidate",return_value=({},"user_reviewed")), \
             patch.object(workflow.career_preferences,"get_preferences",return_value={"roles":["Software Engineer"]}), \
             patch.object(workflow.job_search_agent,"active_discovery",return_value=None), \
             patch.object(workflow,"_submit"):
            status,data=self.asgi_request("POST","/api/workflows",{"prompt":"Find software engineering jobs"})
            self.assertEqual(status,202,data)
            self.assertEqual(data["status"],"queued")
            self.assertTrue(data["workflow_id"])
            self.assertTrue(data["discovery_run_id"])
            self.assertEqual(data["preferences_version"],4)
            row=workflow._row(data["workflow_id"])
            self.assertIsNotNone(row)
            self.assertEqual(row["prompt"],"Find software engineering jobs")
            status,current=self.asgi_request("GET","/api/workflows/current")
            self.assertEqual(status,200)
            self.assertEqual(current["workflow"]["workflow_id"],data["workflow_id"])
            self.assertFalse(current["submission_enabled"])

    def test_eligibility_review_groups_shared_facts_and_exposes_structured_evidence(self):
        keys=["job-one","job-two"]
        with workflow.database() as db:
            db.execute("UPDATE user_workflows SET job_keys_json=? WHERE workflow_id='wf-test'",(json.dumps(keys),))
            db.execute("CREATE TABLE discovered_openings(user_id TEXT,job_key TEXT,company TEXT,title TEXT,location TEXT,description TEXT)")
            db.executemany("INSERT INTO discovered_openings VALUES(?,?,?,?,?,?)",[
                (workflow.USER,"job-one","Example","Engineer I","Remote","Python is a required qualification; Cloud experience is preferred."),
                (workflow.USER,"job-two","Example","Engineer II","Remote","Python is a required qualification; Cloud experience is preferred.")])
            db.execute("CREATE TABLE applications(user_id TEXT,job_key TEXT,state TEXT)")
            db.execute("CREATE TABLE job_match_results(user_id TEXT,job_key TEXT,resume_id TEXT,status TEXT,freshness_status TEXT)")
        sample=[{"job_key":key,"classification":"needs_review","freshness_status":"current","stale":False,
            "candidate_fingerprint":"candidate","preferences_fingerprint":"preferences","job_fingerprint":key,
            "matching_model_version":"ollama:test","updated_at":"2026-01-01T00:00:00+00:00","match":{
                "missing_information":["Confirm expected graduation date","Professional experience is unclear"],
                "required_skills":[{"requirement":"Python","status":"met","candidate_evidence":["Confirmed skills profile"]}],
                "preferred_skills":[{"requirement":"Cloud experience","status":"preferred","candidate_evidence":[] }],
                "explanation":"Evidence needs confirmation."}} for key in keys]
        with patch.object(workflow.resume_job_matcher,"_match_rows",return_value=sample):
            data=workflow._eligibility_review_rows(workflow._row("wf-test"))
        self.assertEqual(len(data["jobs"]),2)
        graduation=next(q for q in data["questions"] if q["key"]=="graduation_date")
        self.assertEqual(len(graduation["jobs"]),2)
        self.assertEqual(data["jobs"][0]["required_qualifications"][0]["status"],"met")
        self.assertEqual(data["jobs"][0]["preferred_qualifications"][0]["requirement"],"Cloud experience")

    def test_confirmed_review_action_is_versioned_and_never_stores_answer_in_event_details(self):
        with workflow.database() as db:
            db.execute("UPDATE user_workflows SET job_keys_json='[\"job-one\"]' WHERE workflow_id='wf-test'")
            db.execute("CREATE TABLE job_match_results(user_id TEXT,job_key TEXT,resume_id TEXT,status TEXT,freshness_status TEXT)")
        match={"job_key":"job-one","classification":"needs_review","freshness_status":"current","stale":False,
            "candidate_fingerprint":"candidate-v1","preferences_fingerprint":"prefs-v1","job_fingerprint":"job-v1",
            "matching_model_version":"ollama:test-v1","updated_at":"match-v1","result_json":"{}","match":{}}
        with patch.object(workflow.resume_job_matcher,"active_candidate",return_value=("resume-1","field_verified",{})), \
             patch.object(workflow.resume_job_matcher,"_match_rows",return_value=[match]), \
             patch.object(workflow.candidate_verification,"save_application_answer") as save, \
             patch.object(workflow.resume_job_matcher,"analyze_matches",return_value={"status":"queued"}):
            status,data=self.asgi_request("POST","/api/workflows/wf-test/eligibility-review",{
                "job_key":"job-one","action":"confirm","question_key":"technical_skills",
                "question":"Confirm your technical skills","answer":"Python","reusable":True,"confirmed":True})
        self.assertEqual(status,200,data)
        self.assertTrue(save.called)
        with workflow.database() as db:
            event=db.execute("SELECT action,resume_id,profile_fingerprint,matching_engine_version,details_json FROM eligibility_review_events").fetchone()
        self.assertEqual(event["action"],"confirm")
        self.assertEqual(event["resume_id"],"resume-1")
        self.assertEqual(event["profile_fingerprint"],"candidate-v1")
        self.assertNotIn("Python",event["details_json"])

    def test_start_rejects_active_discovery_before_creating_a_workflow(self):
        with workflow.database() as db:
            db.execute("UPDATE user_workflows SET status='completed',current_stage='completed' WHERE workflow_id='wf-test'")
        with patch.object(workflow.application_answer_assistant,"get_authorization",return_value={"settings":{
                 "allow_automatic_discovery":True,"allow_ai_matching":True}}), \
             patch.object(workflow,"_versions",return_value=("resume-1","rv1","pv1",3,{"roles":["Software Engineer"]})), \
             patch.object(workflow.job_search_agent,"active_discovery",return_value={"run_id":"prior","cancel_requested":1,"ownership_state":"legacy_unleased"}), \
             patch.object(workflow,"_submit") as submit:
            status,data=self.asgi_request("POST","/api/workflows",{"prompt":"Find software engineering jobs"})
        self.assertEqual(status,409)
        self.assertEqual(data["detail"]["code"],"legacy_unleased")
        self.assertTrue(data["detail"]["recovery_available"])
        submit.assert_not_called()
        with workflow.database() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM user_workflows WHERE workflow_id!='wf-test'").fetchone()[0],0)

    def test_start_returns_resume_action_for_associated_active_workflow(self):
        with workflow.database() as db:
            db.execute("UPDATE user_workflows SET status='paused',current_stage='discovering',discovery_run_id='owned-run' WHERE workflow_id='wf-test'")
        with patch.object(workflow.application_answer_assistant,"get_authorization",return_value={"settings":{
                 "allow_automatic_discovery":True,"allow_ai_matching":True}}), \
             patch.object(workflow,"_versions",return_value=("resume-1","rv1","pv1",3,{"roles":["Software Engineer"]})), \
             patch.object(workflow.job_search_agent,"active_discovery",return_value={"run_id":"owned-run","workflow_id":"wf-test","cancel_requested":False,"ownership_state":"lease_valid"}):
            status,data=self.asgi_request("POST","/api/workflows",{"prompt":"Find software engineering jobs"})
        self.assertEqual(status,409)
        self.assertEqual(data["detail"]["action"],"resume")
        self.assertEqual(data["detail"]["workflow"]["workflow_id"],"wf-test")

    def test_failed_matching_conflict_can_resume_saved_jobs_without_discovery(self):
        keys=[f"job:{i}" for i in range(12)]
        with workflow.database() as db:
            db.execute("""UPDATE user_workflows SET status='failed',current_stage='failed',discovered_count=12,
                job_keys_json=?,diagnostics_json=? WHERE workflow_id='wf-test'""",
                (json.dumps(keys),json.dumps({"failure":"Another match analysis is already active for this user. Pause or finish it first."})))
        status,current=self.asgi_request("GET","/api/workflows/current")
        self.assertEqual(status,200)
        self.assertEqual(current["workflow"]["diagnostics"]["matching"]["status"],"ready_to_resume")
        self.assertEqual(current["workflow"]["diagnostics"]["matching"]["pending"],12)
        with patch.object(workflow,"_authorization",return_value={}), \
             patch.object(workflow,"_ensure_snapshot"),patch.object(workflow,"_submit") as submit, \
             patch.object(workflow.application_agent,"resume_processing"):
            status,data=self.asgi_request("POST","/api/workflows/wf-test/resume")
        self.assertEqual(status,200,data)
        self.assertEqual(data["workflow"]["status"],"queued")
        self.assertEqual(data["workflow"]["current_stage"],"matching")
        self.assertEqual(data["workflow"]["job_keys"],keys)
        submit.assert_called_once_with("wf-test")

    def test_start_same_prompt_offers_saved_matching_workflow_instead_of_rediscovery(self):
        keys=[f"job:{i}" for i in range(12)]
        with workflow.database() as db:
            db.execute("""UPDATE user_workflows SET status='failed',current_stage='failed',discovered_count=12,
                job_keys_json=?,diagnostics_json=? WHERE workflow_id='wf-test'""",
                (json.dumps(keys),json.dumps({"failure":"Another match analysis is already active for this user. Pause or finish it first."})))
        with patch.object(workflow.application_answer_assistant,"get_authorization",return_value={"settings":{
                 "allow_automatic_discovery":True,"allow_ai_matching":True}}), \
             patch.object(workflow,"_versions",return_value=("resume-1","rv1","pv1",3,{"roles":["Software Engineer"]})), \
             patch.object(workflow.job_search_agent,"active_discovery",return_value=None),patch.object(workflow,"_submit") as submit:
            status,data=self.asgi_request("POST","/api/workflows",{"prompt":"software engineering jobs"})
        self.assertEqual(status,409)
        self.assertEqual(data["detail"]["action"],"resume")
        self.assertEqual(data["detail"]["workflow"]["job_keys"],keys)
        submit.assert_not_called()
        with workflow.database() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM user_workflows").fetchone()[0],1)

    def test_matching_progress_is_returned_for_workflow_session(self):
        with workflow.database() as db:
            db.execute("UPDATE user_workflows SET status='running',current_stage='matching',matching_session_id='match-owned' WHERE workflow_id='wf-test'")
        progress={"session_id":"match-owned","status":"running","total":12,"completed":4,"pending":8,
                  "failed":0,"current_job":{"company":"Example","title":"Engineer"},
                  "last_successful_checkpoint":{"company":"Example","title":"Engineer","at":"now"}}
        with patch.object(workflow.resume_job_matcher,"_progress",return_value=progress):
            status,data=self.asgi_request("GET","/api/workflows/current")
        self.assertEqual(status,200)
        self.assertEqual(data["workflow"]["diagnostics"]["matching"]["completed"],4)
        self.assertEqual(data["workflow"]["diagnostics"]["matching"]["pending"],8)
        self.assertEqual(data["workflow"]["diagnostics"]["matching"]["current_job"]["title"],"Engineer")

    def test_matching_recovery_route_is_loopback_gated_and_explicit(self):
        with patch.object(workflow.resume_job_matcher,"recover_match_session",return_value={"recovered":True,"status":"interrupted"}) as recover:
            status,data=self.asgi_request("POST","/api/workflows/matching/legacy-session/recover",{"confirm_abandoned":True})
        self.assertEqual(status,200,data)
        self.assertTrue(data["recovery"]["recovered"])
        recover.assert_called_once_with("legacy-session",True)

    def test_duplicate_worker_submission_for_same_workflow_is_suppressed(self):
        class FakePool:
            def __init__(self):self.submitted=[]
            def submit(self,*args):self.submitted.append(args)
        old_pool=workflow.POOL;pool=FakePool();workflow.POOL=pool
        workflow._running_workflows.discard("wf-test")
        try:
            self.assertTrue(workflow._submit("wf-test"))
            self.assertFalse(workflow._submit("wf-test"))
            self.assertEqual(len(pool.submitted),1)
        finally:
            workflow._running_workflows.discard("wf-test")
            workflow.POOL=old_pool

    def test_two_simultaneous_starts_return_one_persisted_workflow(self):
        from concurrent.futures import ThreadPoolExecutor
        with workflow.database() as db:
            db.execute("UPDATE user_workflows SET status='completed',current_stage='completed' WHERE workflow_id='wf-test'")
        request=SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
        def start():
            try:return workflow.start_workflow(workflow.WorkflowStart(prompt="Find software engineering jobs"),request)
            except Exception as error:return error
        with patch.object(workflow.application_answer_assistant,"get_authorization",return_value={"settings":{
                 "allow_automatic_discovery":True,"allow_ai_matching":True,"allow_automated_form_preparation":False}}), \
             patch.object(workflow,"_versions",return_value=("resume-1","rv1","pv1",3,{"roles":["Software Engineer"]})), \
             patch.object(workflow.job_search_agent,"active_discovery",return_value=None), \
             patch.object(workflow,"_submit"):
            with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _i:start(),range(2)))
        self.assertEqual(sum(isinstance(item,dict) for item in results),1)
        conflict=next(item for item in results if not isinstance(item,dict))
        self.assertEqual(conflict.status_code,409)
        self.assertIn(conflict.detail["code"],{"existing_workflow","concurrent_workflow_start"})
        with workflow.database() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM user_workflows WHERE workflow_id!='wf-test'").fetchone()[0],1)

    def test_new_workflow_can_start_after_prior_workflow_cancellation(self):
        with workflow.database() as db:
            db.execute("UPDATE user_workflows SET status='cancelled',current_stage='cancelled',cancel_requested=1 WHERE workflow_id='wf-test'")
        with patch.object(workflow.application_answer_assistant,"get_authorization",return_value={"settings":{
                 "allow_automatic_discovery":True,"allow_ai_matching":True,"allow_automated_form_preparation":False}}), \
             patch.object(workflow,"_versions",return_value=("resume-1","rv1","pv1",3,{"roles":["Software Engineer"]})), \
             patch.object(workflow.job_search_agent,"active_discovery",return_value=None),patch.object(workflow,"_submit"):
            status,data=self.asgi_request("POST","/api/workflows",{"prompt":"Find software engineering jobs"})
        self.assertEqual(status,202)
        self.assertTrue(data["discovery_run_id"])
        self.assertNotEqual(data["discovery_run_id"],"owned-run")

    def test_start_invalid_prompt_returns_actionable_validation_json_without_row(self):
        status,data=self.asgi_request("POST","/api/workflows",{"prompt":"x"})
        self.assertEqual(status,422)
        self.assertIn("detail",data)
        self.assertTrue(any(item.get("loc",[])[-1]=="prompt" for item in data["detail"]))
        with workflow.database() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM user_workflows WHERE workflow_id!='wf-test'").fetchone()[0],0)

    def test_frontend_start_action_renders_progress_and_explains_transport_or_validation_errors(self):
        page=workflow.WORKFLOW_PAGE
        script=page.split("<script>",1)[1].split("</script>",1)[0]
        self.assertIn("$('#start').onclick=async()=>",script)
        self.assertIn("await call('/api/workflows'",script)
        self.assertIn("render(d);",script)
        self.assertIn("refresh()",script)
        self.assertIn("Could not reach BabyStepsAI",script)
        self.assertIn("item.msg||'Invalid request'",script)
        self.assertIn("unexpected response",script)
        self.assertIn("discovery.feed_failures",script)
        self.assertIn("f.stage",script)
        self.assertIn("discovery.current_source",script)
        self.assertIn("last_successful_checkpoint",script)
        self.assertIn("AI matching ",script)
        self.assertNotIn("),d=await r.json()",script)
        self.assertIn("try{d=await r.json()}catch",script)
        self.assertIn("Recover abandoned search",page)
        self.assertIn("Recover abandoned matching",page)
        self.assertIn("confirm_abandoned:true",script)
        self.assertIn("/api/workflows/matching/",script)
        self.assertIn("/api/workflows/discovery/",script)
        self.assertIn("Analyzed by AI",script)
        self.assertIn("Current results reused",script)
        self.assertIn("Excluded by filters or missing content",script)
        self.assertIn("Pending jobs",script)
        self.assertIn("Matching Details",page)
        self.assertIn("Eligibility: ",script)
        self.assertIn("Resume requested",script)
        self.assertIn("Matching worker running",script)

    def test_lifecycle_controls_and_refresh_recovery_over_asgi(self):
        with workflow.database() as db:
            db.execute("UPDATE user_workflows SET status='running',current_stage='matching',job_keys_json='[\"job:one\"]' WHERE workflow_id='wf-test'")
        with patch.object(workflow,"_authorization",return_value={}), \
             patch.object(workflow,"_versions",return_value=("resume-1","rv1","pv1",3,{})), \
             patch.object(workflow,"_submit"):
            status,paused=self.asgi_request("POST","/api/workflows/wf-test/pause")
            self.assertEqual(status,200)
            self.assertEqual(paused["workflow"]["status"],"paused")
            status,resumed=self.asgi_request("POST","/api/workflows/wf-test/resume")
            self.assertEqual(status,200)
            self.assertEqual(resumed["workflow"]["status"],"queued")
            status,cancelled=self.asgi_request("POST","/api/workflows/wf-test/cancel")
            self.assertEqual(status,200)
            self.assertEqual(cancelled["workflow"]["status"],"cancelled")
            status,current=self.asgi_request("GET","/api/workflows/current")
            self.assertEqual(status,200)
            self.assertEqual(current["workflow"]["job_keys"],["job:one"] if current["workflow"]["workflow_id"]=="wf-test" else [])
            self.assertEqual(current["workflow"]["status"],"cancelled")

    def test_prompt_conflicts_are_reported_without_overriding_saved_preferences(self):
        prefs={"location":"Canada","employment_types":["Full-time"],"levels":["Entry Level"]}
        conflicts=workflow.prompt_conflicts(
            "Find senior software jobs in the United States and include internships",prefs)
        self.assertEqual(len(conflicts),3)
        self.assertIn("Canada",conflicts[0])
        self.assertEqual(prefs["location"],"Canada")

    def test_discovery_matching_and_authorized_dry_run_queue_are_orchestrated(self):
        class MatchDatabase:
            def __enter__(self):return self
            def __exit__(self,*_args):pass
            def execute(self,_query,_params):
                return SimpleNamespace(fetchall=lambda:[
                    {"job_key":"job:eligible","status":"completed","freshness_status":"current","classification":"potential_match"},
                    {"job_key":"job:review","status":"completed","freshness_status":"current","classification":"needs_review"},
                    {"job_key":"job:stale","status":"completed","freshness_status":"stale","classification":"potential_match"},
                    {"job_key":"job:no","status":"completed","freshness_status":"current","classification":"not_eligible"},
                ])
        class AppDatabase:
            def __enter__(self):return self
            def __exit__(self,*_args):pass
            def execute(self,*_args):return SimpleNamespace(fetchall=lambda:[])
        queued=[]
        def queue(payload):
            queued.extend(payload.job_keys)
            return {"queued":len(payload.job_keys),"skipped":[]}
        discover=lambda _prompt,_prefs,**_kw:{"status":"preliminary_matches","results":[
            {"job_key":"job:eligible"},{"job_key":"job:review"},{"job_key":"job:stale"},{"job_key":"job:no"}],"diagnostics":{"sources_checked":17}}
        def match(_request):return {"status":"completed"}
        outcomes=[{"job_key":key,"status":"completed","pending":False} for key in
                  ("job:eligible","job:review","job:stale","job:no")]
        with patch.object(workflow,"_authorization",return_value={"allow_automated_form_preparation":True}), \
             patch.object(workflow,"_versions",return_value=("resume-1","rv1","pv1",3,{"roles":["SWE"]})), \
             patch.object(workflow,"_job_outcomes",return_value=outcomes), \
             patch.object(workflow.resume_job_matcher,"database",MatchDatabase), \
             patch.object(workflow.application_agent,"connect",AppDatabase), \
             patch.object(workflow.job_search_agent,"_employer_sources",return_value=[object()]*17):
            workflow.run_workflow("wf-test",discover_fn=discover,match_fn=match,queue_fn=queue)
        state=workflow._row("wf-test")
        self.assertEqual(state["status"],"completed")
        self.assertEqual(state["discovered_count"],4)
        self.assertEqual(state["matched_count"],3)
        self.assertEqual(state["potential_match_count"],1)
        self.assertEqual(state["needs_review_count"],1)
        self.assertEqual(queued,["job:eligible"])
        self.assertEqual(state["queued_count"],1)
        self.assertFalse(json.loads(state["diagnostics_json"])["submission_enabled"])

    def test_unverified_and_inactive_employers_are_not_discovery_sources(self):
        original=job_search_agent.DB
        job_search_agent.DB=workflow.DB
        try:
            job_search_agent.initialize()
            with job_search_agent.database() as db:
                stamp=job_search_agent.datetime.now(job_search_agent.timezone.utc).isoformat()
                db.execute("""INSERT INTO employers(employer_id,name,source_type,source_token,connector_status,active,
                    created_at,updated_at,last_success_at,discovery_status) VALUES('good','Good','lever','good','healthy',1,?,?,?,'verified')""",
                    (stamp,stamp,stamp))
                db.execute("""INSERT INTO employers(employer_id,name,source_type,source_token,connector_status,active,
                    created_at,updated_at,last_success_at,discovery_status) VALUES('pending','Pending','ashby','pending','healthy',1,?,?,?,'needs_verification')""",
                    (stamp,stamp,stamp))
                db.execute("""INSERT INTO employers(employer_id,name,source_type,source_token,connector_status,active,
                    created_at,updated_at,last_success_at,discovery_status) VALUES('disabled','Disabled','lever','disabled','healthy',0,?,?,?,'verified')""",
                    (stamp,stamp,stamp))
                row=db.execute("SELECT * FROM employers WHERE employer_id='good'").fetchone()
                db.execute("UPDATE employers SET source_fingerprint=? WHERE employer_id='good'",(job_search_agent._source_fingerprint(dict(row)),))
            self.assertEqual([source.token for source in job_search_agent._employer_sources()], ["good"])
        finally:job_search_agent.DB=original

    def test_pause_resume_and_cancel_persist_without_losing_checkpoints(self):
        with workflow.database() as db:
            db.execute("UPDATE user_workflows SET status='running',current_stage='matching',job_keys_json='[\"job:one\"]' WHERE workflow_id='wf-test'")
        request=SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
        with patch.object(workflow,"_authorization",return_value={}),patch.object(workflow,"_versions",return_value=("resume-1","rv1","pv1",3,{})),patch.object(workflow,"_submit"):
            paused=workflow._control(request,"wf-test","pause")["workflow"]
            self.assertEqual(paused["status"],"paused")
            self.assertEqual(paused["job_keys"],["job:one"])
            resumed=workflow._control(request,"wf-test","resume")["workflow"]
            self.assertEqual(resumed["status"],"queued")
        cancelled=workflow._control(request,"wf-test","cancel")["workflow"]
        self.assertEqual(cancelled["status"],"cancelled")
        self.assertEqual(cancelled["job_keys"],["job:one"])

    def test_resume_reports_worker_handoff_when_previous_worker_is_still_finishing(self):
        with workflow.database() as db:
            db.execute("UPDATE user_workflows SET status='paused',current_stage='matching',pause_requested=1 WHERE workflow_id='wf-test'")
        request=SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
        with patch.object(workflow,"_authorization",return_value={}), \
             patch.object(workflow,"_ensure_snapshot"), \
             patch.object(workflow,"_submit",return_value=False):
            response=workflow._control(request,"wf-test","resume")
        self.assertEqual(response["workflow"]["status"],"queued")
        self.assertEqual(response["worker_state"],"finishing_previous_worker")
        self.assertEqual(workflow._row("wf-test")["pause_requested"],0)

    def test_resume_worker_start_failure_returns_actionable_error_and_restores_paused_state(self):
        from fastapi import HTTPException
        with workflow.database() as db:
            db.execute("UPDATE user_workflows SET status='paused',current_stage='matching',pause_requested=1 WHERE workflow_id='wf-test'")
        request=SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
        with patch.object(workflow,"_authorization",return_value={}), \
             patch.object(workflow,"_ensure_snapshot"), \
             patch.object(workflow,"_submit",side_effect=RuntimeError("executor offline")):
            with self.assertRaises(HTTPException) as caught:workflow._control(request,"wf-test","resume")
        self.assertEqual(caught.exception.status_code,503)
        self.assertIn("retry Resume",caught.exception.detail)
        row=workflow._row("wf-test")
        self.assertEqual(row["status"],"paused")
        self.assertEqual(row["pause_requested"],0)
        self.assertIn("RuntimeError",json.loads(row["diagnostics_json"])["failure"])

    def test_worker_hands_off_resume_queued_while_old_worker_exits(self):
        with workflow.database() as db:
            db.execute("UPDATE user_workflows SET status='queued',current_stage='matching',pause_requested=0,cancel_requested=0 WHERE workflow_id='wf-test'")
        old_pool=workflow.POOL
        class FakePool:
            def __init__(self):self.submitted=[]
            def submit(self,*args):self.submitted.append(args)
        pool=FakePool();workflow.POOL=pool
        try:
            with patch.object(workflow,"run_workflow"):
                workflow._worker_entry("wf-test")
            self.assertEqual(len(pool.submitted),1)
            self.assertEqual(pool.submitted[0][0],workflow._worker_entry)
        finally:workflow.POOL=old_pool

    def test_checkpoint_persists_a_pause_request_atomically(self):
        with workflow.database() as db:
            db.execute("UPDATE user_workflows SET status='running',current_stage='matching',pause_requested=1 WHERE workflow_id='wf-test'")
        self.assertFalse(workflow._checkpoint("wf-test","matching"))
        self.assertEqual(workflow._row("wf-test")["status"],"paused")

    def test_import_does_not_rewrite_another_process_running_workflow(self):
        with workflow.database() as db:db.execute("UPDATE user_workflows SET status='running' WHERE workflow_id='wf-test'")
        workflow.initialize()
        row=workflow._row("wf-test")
        self.assertEqual(row["status"],"running")

    def test_changed_resume_or_preferences_block_stale_workflow_queueing(self):
        with workflow.database() as db:
            db.execute("UPDATE user_workflows SET status='paused',current_stage='matching',job_keys_json='[\"job:one\"]' WHERE workflow_id='wf-test'")
        with patch.object(workflow,"_versions",return_value=("new-resume","new-version","pv1",3,{})):
            workflow.run_workflow("wf-test",match_fn=lambda _request:self.fail("stale workflow must not be matched"))
        row=workflow._row("wf-test")
        self.assertEqual(row["status"],"failed")
        self.assertIn("changed during this workflow",json.loads(row["diagnostics_json"])["failure"])

    def test_workflow_resume_reuses_discovery_run_checkpoint(self):
        with workflow.database() as db:
            db.execute("UPDATE user_workflows SET status='paused',current_stage='discovering',pause_requested=0,diagnostics_json=? WHERE workflow_id='wf-test'",
                       (json.dumps({"discovery_run_id":"persisted-run"}),))
        seen=[]
        class EmptyApps:
            def __enter__(self):return self
            def __exit__(self,*_args):pass
            def execute(self,*_args):return SimpleNamespace(fetchall=lambda:[])
        def discover(_prompt,_prefs,*,run_id,workflow_id=None):
            seen.append(run_id)
            return {"status":"no_matching_openings","results":[],"diagnostics":{"sources_checked":2,"companies_checked":2}}
        with patch.object(workflow,"_authorization",return_value={"allow_automated_form_preparation":False}), \
             patch.object(workflow,"_versions",return_value=("resume-1","rv1","pv1",3,{"roles":["SWE"]})), \
             patch.object(workflow.application_agent,"connect",EmptyApps), \
             patch.object(workflow.job_search_agent,"_employer_sources",return_value=[]):
            workflow.run_workflow("wf-test",discover_fn=discover)
        self.assertEqual(seen,["persisted-run"])
        self.assertEqual(workflow._row("wf-test")["status"],"completed")

    def test_prompt_authorization_and_loopback_only_api_boundary(self):
        request=SimpleNamespace(client=SimpleNamespace(host="192.168.1.30"))
        with self.assertRaises(Exception):workflow._local_only(request)
        with patch.object(workflow.application_answer_assistant,"get_authorization",return_value={"settings":{"allow_automatic_discovery":False}}):
            with self.assertRaises(Exception):workflow._authorization()
            with self.assertRaises(Exception):workflow.start_workflow(workflow.WorkflowStart(prompt="software engineer jobs"),SimpleNamespace(client=SimpleNamespace(host="127.0.0.1")))

    def test_enrichment_retry_creates_matching_only_child_without_discovery(self):
        with workflow.database() as db:
            db.execute("UPDATE user_workflows SET status='completed',current_stage='completed',job_keys_json=? WHERE workflow_id='wf-test'",(json.dumps(["public_html:retool:one"]),))
        request=SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
        result={"total":1,"attempted":1,"succeeded":1,"failed":0,"results":[{"job_key":"public_html:retool:one","status":"succeeded"}]}
        with patch.object(workflow.job_description_enricher,"enrich_saved_jobs",return_value=result), patch.object(workflow,"_submit",return_value=True):
            response=workflow.retry_workflow_enrichment("wf-test",workflow.EnrichmentRetry(job_keys=["public_html:retool:one"]),request)
        self.assertTrue(response["discovery_reused"]);self.assertTrue(response["matching_workflow_id"])
        child=workflow._row(response["matching_workflow_id"])
        self.assertEqual(child["current_stage"],"matching");self.assertEqual(child["status"],"queued")
        self.assertEqual(json.loads(child["job_keys_json"]),["public_html:retool:one"])

    def test_bulk_failed_matching_retry_accepts_eleven_saved_jobs_without_discovery(self):
        keys=[f"public_html:retool:{index}" for index in range(11)]
        with workflow.database() as db:
            db.execute("UPDATE user_workflows SET status='completed',current_stage='completed',job_keys_json=? WHERE workflow_id='wf-test'",(json.dumps(keys),))
        result={"total":11,"attempted":0,"succeeded":0,"failed":0,"available":11,
                "results":[{"job_key":key,"status":"available"} for key in keys]}
        request=SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
        with patch.object(workflow.job_description_enricher,"enrich_saved_jobs",return_value=result) as enrich, \
             patch.object(workflow,"_submit",return_value=True):
            response=workflow.retry_workflow_enrichment("wf-test",workflow.EnrichmentRetry(job_keys=keys),request)
        enrich.assert_called_once_with(keys,limit=12)
        self.assertTrue(response["discovery_reused"])
        child=workflow._row(response["matching_workflow_id"])
        self.assertEqual(child["current_stage"],"matching")
        self.assertEqual(json.loads(child["job_keys_json"]),keys)
        self.assertTrue(json.loads(child["diagnostics_json"])["discovery_reused"])

    def test_missing_description_is_visible_as_unresolved_not_not_eligible(self):
        with workflow.database() as db:
            db.execute("CREATE TABLE discovered_openings(user_id TEXT,job_key TEXT,company TEXT,title TEXT,description TEXT,description_source TEXT,description_error TEXT)")
            db.execute("CREATE TABLE job_match_results(user_id TEXT,job_key TEXT,resume_id TEXT,status TEXT,classification TEXT,freshness_status TEXT,matching_model_version TEXT,updated_at TEXT,error TEXT,result_json TEXT)")
            db.execute("INSERT INTO discovered_openings VALUES(?,?,?,?,?,?,?)",(workflow.USER,"public_html:retool:one","Retool","Engineer","","",""))
            db.execute("INSERT INTO job_match_results VALUES(?,?,?,?,?,?,?,?,?,?)",(workflow.USER,"public_html:retool:one","resume-1","excluded",None,"legacy","","now","The public feed did not include a job description.","{}"))
        outcome=workflow._job_outcomes({"job_keys":["public_html:retool:one"],"resume_id":"resume-1","diagnostics":{}})[0]
        self.assertFalse(outcome["description_available"]);self.assertFalse(outcome["excluded"])
        self.assertFalse(outcome["not_eligible"]);self.assertTrue(outcome["pending"])
        self.assertTrue(outcome["unresolved_description"])
        self.assertIn("did not include a job description",outcome["reason"])

    def test_failed_matching_outcome_keeps_exception_reason_and_retryability(self):
        with workflow.database() as db:
            db.execute("CREATE TABLE discovered_openings(user_id TEXT,job_key TEXT,company TEXT,title TEXT,description TEXT,description_source TEXT,description_error TEXT)")
            db.execute("CREATE TABLE job_match_results(user_id TEXT,job_key TEXT,resume_id TEXT,status TEXT,classification TEXT,freshness_status TEXT,matching_model_version TEXT,updated_at TEXT,error TEXT,result_json TEXT)")
            db.execute("INSERT INTO discovered_openings VALUES(?,?,?,?,?,?,?)",(workflow.USER,"public_html:retool:failed","Retool","Engineer","This complete software engineering opportunity includes detailed role responsibilities, required qualifications, preferred skills, educational expectations, professional experience, collaboration practices, and relevant technical requirements for the position.","company_site",""))
            db.execute("INSERT INTO job_match_results VALUES(?,?,?,?,?,?,?,?,?,?)",(workflow.USER,"public_html:retool:failed","resume-1","failed",None,"stale","ollama:qwen2.5:3b/job-match-v2","now","Match analysis failed: Timeout: model did not respond.",None))
        outcome=workflow._job_outcomes({"job_keys":["public_html:retool:failed"],"resume_id":"resume-1","diagnostics":{}})[0]
        self.assertTrue(outcome["failed"])
        self.assertTrue(outcome["retryable"])
        self.assertIn("Timeout",outcome["reason"])
        self.assertFalse(outcome["pending"])


if __name__=="__main__":unittest.main()
