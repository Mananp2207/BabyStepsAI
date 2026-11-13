"""Isolated workflow-to-matching-to-application-queue integration coverage."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import application_agent
import application_answer_assistant
import candidate_verification
import career_preferences
import job_search_agent
import main
import resume_job_matcher
import workflow_orchestrator


class Phase11MatchingWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=Path(self.temp.name)/"workflow.sqlite3"
        self.old={module:module.DB for module in (job_search_agent,workflow_orchestrator,
            candidate_verification,career_preferences,application_answer_assistant,application_agent,main)}
        for module in self.old:module.DB=self.db
        self.old_storage=application_agent.STORAGE
        application_agent.STORAGE=Path(self.temp.name)/"resumes"
        application_agent.STORAGE.mkdir()
        (application_agent.STORAGE/"resume.pdf").write_bytes(b"test-only PDF placeholder")
        job_search_agent.initialize()
        with job_search_agent.database() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS resumes(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,
                original_name TEXT NOT NULL,stored_name TEXT NOT NULL,created_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 0,extracted_text TEXT NOT NULL DEFAULT '')""")
            db.execute("""CREATE TABLE IF NOT EXISTS resume_profiles(resume_id TEXT PRIMARY KEY,status TEXT NOT NULL,
                profile_json TEXT,error TEXT,updated_at TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS career_preferences(user_id TEXT PRIMARY KEY,
                preferences_json TEXT NOT NULL,preference_version INTEGER NOT NULL DEFAULT 1,
                preferences_fingerprint TEXT NOT NULL DEFAULT '')""")
        candidate_verification.initialize()
        application_answer_assistant.initialize()
        with job_search_agent.database() as db:
            db.execute("INSERT INTO resumes VALUES('resume-test',?,'resume.pdf','resume.pdf','resume-v1',1,'')",
                       (job_search_agent.USER,))
            db.execute("INSERT INTO resume_profiles VALUES('resume-test','user_reviewed',?,NULL,'profile-v1')",
                       (json.dumps({"technical_skills":["Python"],"education":[{"degree":"B.S."}]}),))
            db.execute("INSERT INTO candidate_verifications VALUES('resume-test',?,?,?,'2026-10-20T12:00:00+00:00')",
                       (job_search_agent.USER,json.dumps({"name":"Test Candidate","email":"test@example.invalid","skills":["Python"],"work_authorization":"United States"}),
                        json.dumps({"name":True,"email":True,"skills":True,"work_authorization":True})))
            prefs={"roles":["Software Engineer"],"categories":["Computer and Information Technology"],
                   "levels":["Entry Level"],"employment_types":["Full-time"],"location":"United States",
                   "work_arrangement":"Any","instructions":""}
            db.execute("INSERT INTO career_preferences VALUES(?,?,1,'test-pref')",(job_search_agent.USER,json.dumps(prefs)))
            settings={"allow_automatic_discovery":True,"allow_ai_matching":True,
                      "allow_automated_form_preparation":True,"live_submission_enabled":False}
            db.execute("INSERT OR REPLACE INTO application_authorizations VALUES(?,1,?,'auth-v1')",
                       (job_search_agent.USER,json.dumps(settings)))
        application_agent.initialize()
        resume_job_matcher.initialize()
        workflow_orchestrator.initialize()
        self.keys=[f"greenhouse:test-board:job-{index}" for index in range(12)]
        with job_search_agent.database() as db:
            for index,key in enumerate(self.keys):
                db.execute("""INSERT INTO discovered_openings(user_id,job_key,company,title,location,url,
                    discovered_at,employment_type,location_status,search_prompt,source_platform,source_id,country,
                    work_arrangement,description,active,job_url) VALUES(?,?,'Test Employer','Entry Level Software Engineer',
                    'San Francisco, CA, United States',?, '2026-10-20T12:00:00+00:00','Full-time','eligible',
                    'software engineering jobs','greenhouse',?,'United States','remote',
                    'Python experience required for this software engineering role.',1,?)""",
                    (job_search_agent.USER,key,f"https://boards.greenhouse.io/test-board/jobs/{index}",
                     str(index),f"https://boards.greenhouse.io/test-board/jobs/{index}"))
        with workflow_orchestrator.database() as db:
            db.execute("""INSERT INTO user_workflows(workflow_id,user_id,prompt,status,current_stage,resume_id,
                resume_version,profile_version,preferences_version,job_keys_json,discovered_count,created_at,updated_at)
                VALUES('workflow-test',?,'software engineering jobs','running','matching','resume-test',
                'resume-v1','profile-v1',1,?,12,'workflow-v1','workflow-v1')""",
                (job_search_agent.USER,json.dumps(self.keys)))

    def tearDown(self):
        for module,old in self.old.items():module.DB=old
        application_agent.STORAGE=self.old_storage
        self.temp.cleanup()

    def test_twelve_saved_jobs_match_and_only_fresh_eligible_jobs_enter_dry_queue(self):
        match=resume_job_matcher.MatchResult.model_validate({
            "classification":"potential_match","match_score":86,
            "explanation":"Confirmed Python experience is relevant; the user should review the posting.",
            "strengths":["Confirmed Python skill"],"gaps":[],"missing_information":[],
            "required_skills":[{"requirement":"Python","status":"met","candidate_evidence":["skills: Python"]}],
            "preferred_skills":[],"required_experience":[],"preferred_experience":[],"education_requirements":[],
            "education_assessment":"No mandatory degree conflict found.","experience_assessment":"No mandatory experience conflict found.",
            "experience_level_assessment":"Consistent with entry-level preference.","location_assessment":"United States location verified.",
            "employment_type_assessment":"Full-time matches saved preference."})
        def no_discovery(*_args,**_kwargs):self.fail("matching resume must not repeat completed discovery")
        with patch.object(workflow_orchestrator,"_versions",return_value=("resume-test","resume-v1","profile-v1",1,
                    {"roles":["Software Engineer"],"levels":["Entry Level"],"employment_types":["Full-time"],
                     "location":"United States","work_arrangement":"Any","instructions":""})), \
             patch.object(resume_job_matcher,"analyze_with_ollama",return_value=match) as ollama, \
             patch.object(application_agent,"validate_application_url",return_value=None), \
             patch.object(application_agent,"_schedule_application_queue",return_value=False), \
             patch.object(job_search_agent,"_employer_sources",return_value=[]):
            workflow_orchestrator.run_workflow("workflow-test",discover_fn=no_discovery,poll_interval=.01,match_timeout=20)
        row=workflow_orchestrator._row("workflow-test")
        diagnostics=json.loads(row["diagnostics_json"])
        self.assertEqual(row["status"],"completed",diagnostics)
        self.assertEqual(row["discovered_count"],12)
        self.assertEqual(len(json.loads(row["job_keys_json"])),12)
        self.assertTrue(row["matching_session_id"])
        self.assertEqual(ollama.call_count,12)
        rendered=workflow_orchestrator._serialize(workflow_orchestrator._row("workflow-test"))
        self.assertEqual(rendered["outcome_counts"]["analyzed"],12)
        self.assertEqual(rendered["outcome_counts"]["pending"],0)
        # Workflow matching must not silently treat global preparation settings
        # as consent to enter real employer forms. Authorize each exact job
        # after a fresh read-only inspection, then queue the selected dry runs.
        with patch.object(application_agent,"validate_application_url",return_value=None), \
             patch.object(application_agent,"_schedule_application_queue",return_value=False):
            for key in self.keys:
                job=application_agent._get_job(application_agent.DryRunRequest(job_key=key))
                report={"provider":{"provider":"greenhouse"},"form_detected":True,
                    "page_classification":"application_form","inspection_outcome":"compatible",
                    "robots_policy":"verified_allow","required_fields":[],"unsupported_fields":[],
                    "fields":[],"human_actions_required":[],"submission_control_detected":True,
                    "submission_control_status":"detected_visible",
                    "submission_controls":[{"action":"final_submission","label":"Submit application","visible":True}]}
                application_agent._save_form_inspection(job,report)
                plan=application_agent.preparation_authorization_plan(key)
                application_agent.authorize_saved_job_preparation(key,application_agent.PreparationAuthorizationRequest(
                    confirmed=True,confirmed_employer=plan["employer"],confirmed_title=plan["title"],
                    confirmed_application_url=plan["application_url"],
                    field_keys=[item["key"] for item in plan["candidate_fields"] if item["key"] in {"full_name","email"}],
                    document_types=["resume"]))
            queued=application_agent.enqueue_saved_jobs(application_agent.ApplicationQueueRequest(job_keys=self.keys))
        self.assertEqual(queued["queued"],12)
        with job_search_agent.database() as db:
            results=db.execute("SELECT status,classification,freshness_status FROM job_match_results WHERE user_id=? AND job_key IN (SELECT value FROM json_each(?))",
                               (job_search_agent.USER,json.dumps(self.keys))).fetchall()
            apps=db.execute("SELECT state,mode,count(*) n FROM applications WHERE user_id=? AND job_key IN (SELECT value FROM json_each(?)) GROUP BY state,mode",
                            (job_search_agent.USER,json.dumps(self.keys))).fetchall()
            submission=db.execute("SELECT submission_enabled FROM application_controls WHERE user_id=?",(job_search_agent.USER,)).fetchone()
        self.assertEqual(len(results),12)
        self.assertTrue(all(item["status"]=="completed" and item["freshness_status"]=="current" for item in results))
        self.assertEqual(sum(item["n"] for item in apps),12)
        self.assertTrue(all(item["state"]=="queued" and item["mode"]=="dry_run" for item in apps))
        self.assertEqual(submission["submission_enabled"],0)
        self.assertFalse(diagnostics["submission_enabled"])

    def test_completed_matcher_response_cannot_complete_with_missing_job_results(self):
        def no_discovery(*_args,**_kwargs):self.fail("saved matching must not repeat discovery")
        with patch.object(workflow_orchestrator,"_versions",return_value=("resume-test","resume-v1","profile-v1",1,{})), \
             patch.object(workflow_orchestrator,"_authorization",return_value={"allow_automated_form_preparation":False}), \
             patch.object(resume_job_matcher,"_progress",return_value={"status":"completed","total":12,"completed":0,"pending":0}):
            workflow_orchestrator.run_workflow("workflow-test",discover_fn=no_discovery,
                match_fn=lambda _request:{"status":"completed"},poll_interval=.01,match_timeout=.2)
        row=workflow_orchestrator._row("workflow-test")
        self.assertEqual(row["status"],"paused")
        self.assertEqual(row["current_stage"],"matching")
        self.assertEqual(json.loads(row["diagnostics_json"])["matching"]["pending"],12)

    def test_enabled_synthetic_policy_scheduler_starts_once_and_preserves_paused_sessions(self):
        prefs=career_preferences.get_preferences()
        scope={"standing_policy_enabled":True,"standing_policy_categories":["Computer and Information Technology"],
            "standing_policy_employment_types":["Full-time"],"standing_policy_countries":["United States"],
            "standing_policy_locations":["Anywhere"],"standing_policy_work_arrangements":["Remote"],
            "standing_policy_experience_levels":["Entry Level"],"standing_policy_field_keys":["full_name","email"],
            "standing_policy_document_types":["resume"],"standing_policy_daily_limit":2,
            "standing_policy_reuse_confirmed_answers":False,"standing_policy_allow_ai_answers":False}
        policy=application_answer_assistant.update_authorization(application_answer_assistant.AuthorizationUpdate(**scope))
        self.assertTrue(policy["settings"]["standing_policy_enabled"])
        self.assertTrue(workflow_orchestrator._standing_policy_current())
        stamp=workflow_orchestrator.now()
        with job_search_agent.database() as db:
            db.execute("INSERT INTO job_match_sessions(session_id,user_id,resume_id,status,job_keys_json,batch_size,total_jobs,created_at,updated_at) VALUES('paused-unrelated',?,'resume-test','paused','[]',1,0,?,?)",
                (job_search_agent.USER,stamp,stamp))
        with workflow_orchestrator.database() as db:
            db.execute("UPDATE user_workflows SET status='completed',current_stage='completed' WHERE workflow_id='workflow-test'")
        eligible="local-mock:scheduler-eligible";ineligible="local-mock:scheduler-ineligible"
        def synthetic_discover(_prompt,_prefs,**_kwargs):
            stamp=workflow_orchestrator.now()
            with job_search_agent.database() as db:
                for key,title,url in ((eligible,"Entry Level Software Engineer","http://127.0.0.1:8000/mock-apps/simple"),
                                      (ineligible,"Entry Level Software Engineer","http://127.0.0.1:8000/mock-apps/uncertain")):
                    db.execute("""INSERT OR REPLACE INTO discovered_openings(user_id,job_key,company,title,location,url,discovered_at,
                        employment_type,location_status,search_prompt,discovery_source,country,work_arrangement,description,
                        last_verified_at,active,content_fingerprint,job_url,career_category,experience_level,availability_status)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (job_search_agent.USER,key,"Synthetic Employer",title,"San Francisco, CA",url,stamp,"Full-time","eligible",
                         "Software Engineer","Synthetic verified source","United States","Remote","Python software role "*10,
                         stamp,1,"synthetic-fingerprint",url,"Computer and Information Technology","Entry Level","current"))
            return {"status":"completed","results":[{"job_key":eligible},{"job_key":ineligible}],"diagnostics":{"companies_checked":1}}
        def synthetic_match(request,**_kwargs):
            stamp=resume_job_matcher.now()
            resume_id,candidate_status,candidate=resume_job_matcher.active_candidate()
            with job_search_agent.database() as db:
                for key,classification in ((eligible,"potential_match"),(ineligible,"not_eligible")):
                    opening=dict(db.execute("SELECT * FROM discovered_openings WHERE user_id=? AND job_key=?",(job_search_agent.USER,key)).fetchone())
                    db.execute("""INSERT OR REPLACE INTO job_match_results(user_id,job_key,resume_id,status,classification,match_score,
                        result_json,created_at,updated_at,candidate_fingerprint,preferences_fingerprint,job_fingerprint,
                        matching_model_version,freshness_status) VALUES(?,?,?,'completed',?,72,?,?,?,?,?,?,?,'current')""",
                        (job_search_agent.USER,key,resume_id,classification,json.dumps({"classification":classification,"missing_information":[]}),
                         stamp,stamp,resume_job_matcher.profile_fingerprint(candidate,resume_id),
                         resume_job_matcher.stable_fingerprint(prefs),resume_job_matcher.job_fingerprint(opening),
                         resume_job_matcher.MATCHING_ENGINE_VERSION))
            return {"status":"completed"}
        def submit_and_run(workflow_id):
            workflow_orchestrator.run_workflow(workflow_id,discover_fn=synthetic_discover,match_fn=synthetic_match,
                queue_fn=application_agent.enqueue_saved_jobs,poll_interval=.01,match_timeout=2)
            return True
        with patch.object(workflow_orchestrator,"_versions",return_value=("resume-test","resume-v1","profile-v1",1,prefs)), \
             patch.object(workflow_orchestrator,"_submit",side_effect=submit_and_run) as submit, \
             patch.object(application_agent,"_schedule_application_queue",return_value=False):
            self.assertEqual(workflow_orchestrator.run_auto_apply_scheduler_cycle(),"started")
            with workflow_orchestrator.database() as db:
                workflow_row=db.execute("SELECT workflow_id,status,diagnostics_json,queued_count FROM user_workflows WHERE user_id=? AND workflow_id<>'workflow-test'",(job_search_agent.USER,)).fetchone()
                all_rows=[tuple(r) for r in db.execute("SELECT workflow_id,status,queued_count,diagnostics_json FROM user_workflows ORDER BY created_at")]
            self.assertEqual(workflow_row["status"],"completed")
            self.assertEqual(workflow_row["queued_count"],1,all_rows)
            self.assertEqual(json.loads(workflow_row["diagnostics_json"])["trigger"],"approved_auto_apply_scheduler")
            self.assertEqual(workflow_orchestrator.run_auto_apply_scheduler_cycle(),"cooldown")
            self.assertEqual(submit.call_count,1)
        with application_agent.connect() as db:
            states={row["job_key"]:row["state"] for row in db.execute("SELECT job_key,state FROM applications WHERE user_id=?",(job_search_agent.USER,))}
        self.assertEqual(states,{eligible:"queued"})
        with job_search_agent.database() as db:
            paused=db.execute("SELECT status,job_keys_json FROM job_match_sessions WHERE session_id='paused-unrelated'").fetchone()
        self.assertEqual((paused["status"],paused["job_keys_json"]),("paused","[]"))
        application_answer_assistant.update_authorization(application_answer_assistant.AuthorizationUpdate())
        self.assertEqual(workflow_orchestrator.run_auto_apply_scheduler_cycle(),"disabled_or_stale")


if __name__=="__main__":unittest.main()
