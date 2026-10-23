import json
import os
import sqlite3
import sys
import concurrent.futures
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import main
import application_agent as app_agent
import candidate_verification as profile
import application_answer_assistant as answer_assistant
import resume_job_matcher as matcher
from job_search_agent import USER


class FakeField:
    def __init__(self, info): self.info, self.value, self.file, self.checked = info, "", None, False
    def is_visible(self): return True
    def is_enabled(self): return True
    def evaluate(self, _script): return self.info
    def fill(self, value): self.value = value
    def set_input_files(self, value): self.file = value
    def check(self): self.checked=True
    def uncheck(self): self.checked=False
    def select_option(self,**kwargs): self.checked=True; self.selected=kwargs.get("label")
    def locator(self, _selector): return FakeOptions(self.info.get("options",[]))

class FakeOptions:
    def __init__(self, values): self.values = values
    def all_text_contents(self): return self.values
    def count(self): return len(self.values)
    def nth(self, index): return self.values[index]

class FakeFields:
    def __init__(self, fields): self.fields = fields
    def count(self): return len(self.fields)
    def nth(self, index): return self.fields[index]

class FakeQuestionPage:
    def __init__(self, info): self.fields=FakeFields([FakeField(info)])
    def locator(self, _selector): return self.fields

class FakeControl(FakeField):
    def __init__(self,page,info): super().__init__(info); self.page=page
    def click(self,**_kwargs):
        if self.info.get("action")=="safe_next": self.page.stage+=1
        elif self.info.get("action")=="apply_button": self.page.url=self.page.url.rsplit("/",1)[0]+"/simple"
        elif self.info.get("type")=="submit": self.page.submission_count+=1

class FakeControls(FakeFields):
    def __init__(self,page,infos): super().__init__([FakeControl(page,item) for item in infos])

class FakeButton:
    def __init__(self, page, available): self.page, self.available = page, available
    @property
    def first(self): return self
    def count(self): return int(self.available)
    def is_visible(self): return self.available
    def click(self, **_kwargs): self.page.stage += 1

class FakePage:
    def __init__(self, client): self.client, self.stage, self.url = client, 0, ""; self.submission_count=0
    def set_default_timeout(self, _value): pass
    def route(self, *_args): pass
    def goto(self, url, **_kwargs):
        self.url = url
        from urllib.parse import urlparse
        response = app_agent.mock_application(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1])
        self.body = response.body.decode("utf-8")
    def wait_for_timeout(self, _value): pass
    def locator(self, selector):
        if selector == "body": return FakeBody(self.body)
        if selector=='input[type="password"]': return FakeFields([])
        if selector=="form": return FakeForms()
        if selector=="a[href]": return FakeLinks(self)
        if selector=="button,a[href],input[type=button],input[type=submit]": return FakeControls(self,self.control_infos())
        if selector.startswith("input"):
            from urllib.parse import urlparse
            scenario = urlparse(self.url).path.rstrip("/").rsplit("/",1)[-1]
            stages = {
                "simple": [[
                    {"tag":"input","type":"text","name":"name","label":"Full Name","required":True},
                    {"tag":"input","type":"email","name":"email","label":"Email","required":True},
                    {"tag":"input","type":"text","name":"phone","label":"Phone","required":True},
                    {"tag":"input","type":"text","name":"linkedin","label":"LinkedIn URL","required":False},
                    {"tag":"input","type":"text","name":"github","label":"GitHub URL","required":False},
                    {"tag":"input","type":"file","name":"resume","label":"Resume","required":True},]],
                "multi-step": [
                    [{"tag":"input","type":"text","name":"name","label":"Full name","required":True},{"tag":"input","type":"email","name":"email","label":"Email","required":True}],
                    [{"tag":"input","type":"text","name":"university","label":"University","required":True},{"tag":"input","type":"text","name":"degree","label":"Degree","required":True},{"tag":"input","type":"text","name":"graduation_year","label":"Graduation year","required":True}],
                    [{"tag":"textarea","type":"textarea","name":"experience","label":"Work experience","required":True}],],
                "uncertain": [[{"tag":"select","type":"select-one","name":"authorized","label":"Are you legally authorized to work?","required":True},{"tag":"textarea","type":"textarea","name":"why","label":"Why do you want this job?","required":True}]],
                "written": [[{"tag":"textarea","type":"textarea","name":"project","label":"Describe a relevant technical project and the technologies used (maximum 120 words)","required":True,"max_length":600}]],
                "validation": [[{"tag":"input","type":"email","name":"email","label":"Email","required":True},{"tag":"input","type":"text","name":"custom","label":"Required custom question","required":True},{"tag":"input","type":"file","name":"resume","label":"Resume (.docx only)","required":True,"accept":".docx"}]],
                "weird-submit": [[{"tag":"input","type":"text","name":"name","label":"Full Name","required":True}]],
                "choices": [[{"tag":"input","type":"checkbox","name":"technical_skills","value":"Python","label":"Python","required":False},
                             {"tag":"input","type":"radio","name":"relocation","value":"yes","label":"Are you willing to relocate? Yes","required":True},
                             {"tag":"input","type":"radio","name":"relocation","value":"no","label":"Are you willing to relocate? No","required":True}]],
                "captcha": [[]],
            }
            values=stages.get(scenario,[[]])[min(self.stage,len(stages.get(scenario,[[]]))-1)]
            return FakeFields([FakeField(x) for x in values])
        return FakeOptions([])
    def control_infos(self):
        from urllib.parse import urlparse
        scenario=urlparse(self.url).path.rstrip("/").rsplit("/",1)[-1]
        if scenario=="multi-step" and self.stage<2:
            label="Next" if self.stage==0 else "Continue to work experience"
            return [{"tag":"button","type":"button","label":label,"inside_form":False,"action":"safe_next"}]
        if scenario=="weird-submit": return [{"tag":"button","type":"submit","label":"Continue","inside_form":True,"action":"final_submission"}]
        if scenario=="job-page": return [{"tag":"a","type":"","label":"Apply Now","href":"http://127.0.0.1:8000/mock-apps/simple","inside_form":False,"action":"apply_link"}]
        if scenario=="apply-button": return [{"tag":"button","type":"button","label":"Apply Now","inside_form":False,"action":"apply_button"}]
        if scenario in {"simple","uncertain","validation","captcha"}: return [{"tag":"button","type":"submit","label":"Submit application","inside_form":True,"action":"final_submission"}]
        return []
    def get_by_role(self, role, name=None):
        from urllib.parse import urlparse
        scenario=urlparse(self.url).path.rstrip("/").rsplit("/",1)[-1]
        return FakeButton(self, role=="button" and scenario=="multi-step" and self.stage<2)
    def screenshot(self, path, **_kwargs): Path(path).write_bytes(b"mock screenshot")

class FakeBody:
    def __init__(self, text): self.text=text
    def inner_text(self, **_kwargs): return self.text

class FakeForms:
    def evaluate_all(self,_script): return []

class FakeLinks:
    def __init__(self,page): self.page=page
    def evaluate_all(self,_script):
        if self.page.url.endswith("/mock-apps/job-page"):
            return [{"label":"Apply Now","href":"http://127.0.0.1:8000/mock-apps/simple"}]
        if self.page.url.endswith("/mock-apps/apply-loop"):
            return [{"label":"Apply","href":"http://127.0.0.1:8000/mock-apps/apply-loop"}]
        return []

class FakeBrowser:
    def __init__(self, client): self.client=client
    def new_context(self, **_kwargs): return FakeContext(self.client)
    def close(self): pass

class FakeContext:
    def __init__(self, client): self.client=client
    def new_page(self): self.client.last_page=FakePage(self.client); return self.client.last_page

class FakeChromium:
    def __init__(self, client): self.client=client
    def launch(self, **kwargs): self.client.launch_options.append(kwargs); return FakeBrowser(self.client)

class FakePlaywrightContext:
    def __init__(self, client): self.chromium=FakeChromium(client)
    def __enter__(self): return self
    def __exit__(self, *_args): pass


class ApplicationAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); root=Path(self.temp.name)
        self.old_key_path=os.environ.get("BABYSTEPSAI_KEY_PATH"); os.environ["BABYSTEPSAI_KEY_PATH"]=str(root/"encryption.key")
        self.db=root/"application.sqlite3"; self.storage=root/"resumes"; self.storage.mkdir()
        self.old_db,self.old_storage=app_agent.DB,app_agent.STORAGE
        self.old_profile_db=profile.DB
        self.old_assistant_db=answer_assistant.DB
        app_agent.DB=self.db; app_agent.STORAGE=self.storage; profile.DB=self.db; answer_assistant.DB=self.db
        with app_agent.connect() as db:
            db.execute("CREATE TABLE resumes(id TEXT PRIMARY KEY,user_id TEXT,stored_name TEXT,original_name TEXT,created_at TEXT,active INTEGER,extracted_text TEXT)")
            db.execute("INSERT INTO resumes VALUES('r1',?,'r1.pdf','Resume.pdf','2026-01-01',1,'')",(USER,))
            db.execute("CREATE TABLE resume_profiles(resume_id TEXT PRIMARY KEY,status TEXT,profile_json TEXT,error TEXT,updated_at TEXT)")
            db.execute("INSERT INTO resume_profiles VALUES('r1','completed_unverified','{}',NULL,'2026-01-01')")
            db.execute("""CREATE TABLE discovered_openings(user_id TEXT,job_key TEXT,company TEXT,title TEXT,location TEXT,url TEXT,discovered_at TEXT,employment_type TEXT,location_status TEXT,location_reason TEXT,search_prompt TEXT,discovery_source TEXT DEFAULT 'Company Careers Page',PRIMARY KEY(user_id,job_key))""")
            opening_cols="user_id,job_key,company,title,location,url,discovered_at,employment_type,location_status,location_reason,search_prompt,discovery_source"
            db.execute(f"INSERT INTO discovered_openings({opening_cols}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(USER,"saved-simple","Example Co","Software Developer","Austin, TX","http://127.0.0.1:8000/mock-apps/simple?access_token=test-secret","2026-01-01","Full time","eligible","","","Company Careers Page"))
            db.execute(f"INSERT INTO discovered_openings({opening_cols}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(USER,"saved-invalid","Example Co","Invalid Link Job","Remote","file:///tmp/app","2026-01-02","","unknown","","","Company Careers Page"))
            db.execute(f"INSERT INTO discovered_openings({opening_cols}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(USER,"saved-ineligible","Example Co","Ineligible Job","Austin, TX","http://127.0.0.1:8000/mock-apps/simple","2026-01-03","Full time","eligible","","","Company Careers Page"))
            db.execute("""CREATE TABLE job_match_results(user_id TEXT,job_key TEXT,resume_id TEXT,status TEXT,classification TEXT,match_score INTEGER,result_json TEXT,error TEXT,created_at TEXT,updated_at TEXT,PRIMARY KEY(user_id,job_key,resume_id))""")
            db.execute("INSERT INTO job_match_results(user_id,job_key,resume_id,status,classification,match_score,result_json,error,created_at,updated_at) VALUES(?,?,?,'completed','potential_match',88,'{}',NULL,'2026-01-01','2026-01-01')",(USER,"saved-simple","r1"))
            db.execute("INSERT INTO job_match_results(user_id,job_key,resume_id,status,classification,match_score,result_json,error,created_at,updated_at) VALUES(?,?,?,'completed','not_eligible',10,'{}',NULL,'2026-01-01','2026-01-01')",(USER,"saved-ineligible","r1"))
        profile.initialize(); answer_assistant.initialize(); app_agent.initialize()
        answer_assistant.update_authorization(answer_assistant.AuthorizationUpdate(
            allow_automated_form_preparation=True,allow_auto_use_confirmed_answers=True,allow_ai_drafting=True))
        (self.storage/"r1.pdf").write_bytes(b"PDF mock")
        profile.update_verification(profile.VerificationUpdate(values={
            "full_name":"A Candidate","email":"candidate@example.test","phone":"555-0100",
            "linkedin":"https://linkedin.test/candidate","github":"https://github.test/candidate",
            "university":"Example University","degree":"B.S.","graduation_year":"2026",
            "work_experience":["Intern, Example Co."],"technical_skills":["Python"],"relocation_willingness":"Yes",
        },confirmed_fields=["full_name","email","phone","linkedin","github","university","degree","graduation_year","work_experience","technical_skills","relocation_willingness"]))
        with app_agent.connect() as db: db.execute("UPDATE job_match_results SET updated_at=?,freshness_status='current',matching_model_version=? WHERE user_id=?",(app_agent.now(),matcher.MATCHING_ENGINE_VERSION,USER))
        self.client=self
        self.launch_options=[]
        self.sync_api=types.ModuleType("playwright.sync_api")
        self.sync_api.sync_playwright=lambda:FakePlaywrightContext(self.client)
        self.patch_playwright=patch.dict(sys.modules,{"playwright.sync_api":self.sync_api})
        self.patch_playwright.start()
        self.submit_patch=patch.object(app_agent.executor,"submit",side_effect=lambda fn,*args:fn(*args))
        self.submit_patch.start()

    def tearDown(self):
        self.submit_patch.stop(); self.patch_playwright.stop()
        app_agent.DB=self.old_db; app_agent.STORAGE=self.old_storage; profile.DB=self.old_profile_db; answer_assistant.DB=self.old_assistant_db
        if self.old_key_path is None: os.environ.pop("BABYSTEPSAI_KEY_PATH",None)
        else: os.environ["BABYSTEPSAI_KEY_PATH"]=self.old_key_path
        self.temp.cleanup()

    def post(self, path, json=None):
        if path=="/api/applications/pause": return app_agent.pause_applications()
        if path=="/api/applications/resume": return app_agent.resume_processing()
        if path=="/api/applications/dry-run": return app_agent.start_dry_run(app_agent.DryRunRequest(**json))
        if path=="/api/applications/queue": return app_agent.enqueue_saved_jobs(app_agent.ApplicationQueueRequest(**json))

    def get(self, path):
        if path=="/api/applications": return app_agent.list_applications()
        if path=="/api/applications/progress": return app_agent.application_progress()
        if path=="/api/applications/saved-jobs": return app_agent.list_saved_application_jobs()

    def test_end_to_end_local_simple_mock_dry_run_fills_uploads_and_never_submits(self):
        response=self.post("/api/applications/dry-run",json={"job":{"job_key":"mock-simple","company":"Test Co","title":"Software Developer","application_url":"http://127.0.0.1:8000/mock-apps/simple"}})
        app=self.get("/api/applications")[0]
        self.assertEqual(app["state"],"ready_for_review")
        self.assertFalse(app["diagnostic"]["submission_performed"])
        self.assertEqual(app["diagnostic"]["steps"],1)
        self.assertEqual(sum(x["result"]=="filled" for x in app["diagnostic"]["filled_fields"]),6)

    def test_imported_official_job_can_reach_existing_mock_dry_run(self):
        import job_search_agent as jobs
        import career_preferences
        old_jobs_db,old_preferences_db=jobs.DB,career_preferences.DB
        jobs.DB=career_preferences.DB=app_agent.DB
        try:
            jobs.initialize();matcher.initialize()
            prefs={"roles":["Software Engineer"],"categories":["Software Development"],
                   "levels":["New Graduate","Entry Level"],"employment_types":["Full-time"],
                   "location":"United States","work_arrangement":"Any","instructions":""}
            with jobs.database() as db:
                db.execute("CREATE TABLE IF NOT EXISTS career_preferences(user_id TEXT PRIMARY KEY,preferences_json TEXT NOT NULL,preference_version INTEGER NOT NULL DEFAULT 1,preferences_fingerprint TEXT NOT NULL DEFAULT '')")
                db.execute("INSERT OR REPLACE INTO career_preferences(user_id,preferences_json,preference_version,preferences_fingerprint) VALUES(?,?,1,'phase12-test')",(USER,json.dumps(prefs)))
                row=db.execute("SELECT settings_json FROM application_authorizations WHERE user_id=?",(USER,)).fetchone()
                settings=json.loads(row["settings_json"])
                settings.update(allow_automatic_discovery=True,allow_automated_form_preparation=True)
                db.execute("UPDATE application_authorizations SET settings_json=? WHERE user_id=?",(json.dumps(settings),USER))
            text=("Build and test software with a collaborative engineering team. Required qualifications: "
                  "Bachelor's degree in Computer Science; new graduates are welcome. Preferred qualifications include Python.")
            detail={"provider":"lever","source_token":"local-acme","id":"direct-42","employer":"Acme Systems",
                    "title":"New Graduate Software Engineer","description":text,"location":"Austin, Texas, United States",
                    "country":"United States","employment_type":"Full-time","work_arrangement":"",
                    "url":"https://jobs.lever.co/local-acme/direct-42","application_url":"https://jobs.lever.co/local-acme/direct-42",
                    "active":True,"source_url":"https://jobs.lever.co/local-acme/direct-42"}
            with patch.object(jobs,"fetch_public_job_url",return_value=(detail,None)):
                imported=jobs.import_job_url(jobs.JobUrlImportRequest(url=detail["url"]),types.SimpleNamespace(client=types.SimpleNamespace(host="127.0.0.1")))
            key=imported["job_key"]
            with jobs.database() as db:
                db.execute("UPDATE discovered_openings SET url=? WHERE user_id=? AND job_key=?",("http://127.0.0.1:8000/mock-apps/simple",USER,key))
                opening=dict(db.execute("SELECT * FROM discovered_openings WHERE user_id=? AND job_key=?",(USER,key)).fetchone())
            resume_id,_,candidate=matcher.active_candidate()
            stamp=matcher.now()
            result={"classification":"potential_match","missing_information":[],"required_skills":[],"required_experience":[],"education_requirements":[]}
            with jobs.database() as db:
                db.execute("""INSERT OR REPLACE INTO job_match_results(user_id,job_key,resume_id,status,classification,match_score,result_json,created_at,updated_at,candidate_fingerprint,preferences_fingerprint,job_fingerprint,matching_model_version,freshness_status)
                    VALUES(?,?,?,'completed','potential_match',90,?,?,?,?,?,?,?,'current')""",
                    (USER,key,resume_id,json.dumps(result),stamp,stamp,matcher.profile_fingerprint(candidate,resume_id),
                     matcher.stable_fingerprint(prefs),matcher.job_fingerprint(opening),matcher.MATCHING_ENGINE_VERSION))
            prepared=app_agent.prepare_saved_job(app_agent.DryRunRequest(job_key=key))
            self.assertEqual(imported["experience_level"],"New Graduate")
            self.assertEqual(prepared["mode"],"dry_run")
            app=next(item for item in app_agent.list_applications() if item["job_key"]==key)
            self.assertEqual(app["state"],"ready_for_review")
            self.assertFalse(app["diagnostic"]["submission_performed"])
        finally:
            jobs.DB,career_preferences.DB=old_jobs_db,old_preferences_db

    def test_private_transcript_is_selected_only_for_matching_upload_question(self):
        candidate={"__application_documents":[{"document_type":"transcript","path":"/private/transcript.pdf"}]}
        self.assertEqual(app_agent._answer({"type":"file","label":"Upload your academic transcript"},candidate,{}),
                         ("__DOCUMENT__:/private/transcript.pdf","document"))
        ambiguous={"__application_documents":[*candidate["__application_documents"],
            {"document_type":"transcript","path":"/private/transcript-2.pdf"}]}
        self.assertEqual(app_agent._answer({"type":"file","label":"Upload your academic transcript"},ambiguous,{}),(None,None))

    def test_workflow_discovery_matching_queue_and_mock_preparation_end_to_end(self):
        import workflow_orchestrator as workflow
        import job_search_agent as jobs
        import resume_job_matcher as matcher
        import career_preferences
        old_workflow_db,old_jobs_db,old_preferences_db=workflow.DB,jobs.DB,career_preferences.DB
        workflow.DB=jobs.DB=career_preferences.DB=app_agent.DB
        try:
            jobs.initialize();matcher.initialize();workflow.initialize()
            prefs={"roles":["Software Engineer"],"categories":["Software Development"],
                   "levels":["New Graduate","Entry Level"],"employment_types":["Full-time"],
                   "location":"United States","work_arrangement":"Any","instructions":""}
            with jobs.database() as db:
                db.execute("CREATE TABLE IF NOT EXISTS career_preferences(user_id TEXT PRIMARY KEY,preferences_json TEXT NOT NULL,preference_version INTEGER NOT NULL DEFAULT 1,preferences_fingerprint TEXT NOT NULL DEFAULT '')")
                db.execute("INSERT OR REPLACE INTO career_preferences(user_id,preferences_json,preference_version,preferences_fingerprint) VALUES(?,?,1,'test')",(USER,json.dumps(prefs)))
                stamp=jobs.datetime.now(jobs.timezone.utc).isoformat()
                db.execute("INSERT OR IGNORE INTO employers(employer_id,name,careers_url,source_type,source_token,connector_status,active,created_at,updated_at,last_success_at,discovery_status) VALUES('greenhouse:workflow-mock','Workflow Mock','https://boards.greenhouse.io/workflow-mock','greenhouse','workflow-mock','healthy',1,?,?,?,'verified')",(stamp,stamp,stamp))
                row=db.execute("SELECT * FROM employers WHERE employer_id='greenhouse:workflow-mock'").fetchone()
                db.execute("UPDATE employers SET source_fingerprint=? WHERE employer_id='greenhouse:workflow-mock'",(jobs._source_fingerprint(dict(row)),))
            with workflow.database() as db:
                db.execute("INSERT OR REPLACE INTO user_workflows(workflow_id,user_id,prompt,status,current_stage,resume_id,resume_version,profile_version,preferences_version,created_at,updated_at) VALUES('wf-e2e',?,'software engineering new grad jobs','queued','queued','r1','rv1','pv1',1,?,?)",(USER,app_agent.now(),app_agent.now()))
            posting={"id":9001,"title":"Software Engineer New Grad","absolute_url":"https://boards.greenhouse.io/workflow-mock/jobs/9001",
                     "location":{"name":"United States"},"metadata":[{"name":"Employment Type","value":"Full-time"}],
                     "content":"Build software with Python. New graduates are welcome."}
            original_discover=jobs.discover
            def discover_and_route_to_local_mock(prompt,saved_prefs,run_id=None,workflow_id=None):
                result=original_discover(prompt,saved_prefs,run_id=run_id,workflow_id=workflow_id)
                with jobs.database() as db:
                    db.execute("UPDATE discovered_openings SET url='http://127.0.0.1:8000/mock-apps/simple' WHERE job_key='greenhouse:workflow-mock:9001'")
                return result
            def mocked_ollama_result(request):
                for key in request.job_keys:
                    with jobs.database() as db:
                        opening=dict(db.execute("SELECT * FROM discovered_openings WHERE user_id=? AND job_key=?",(USER,key)).fetchone())
                    resume_id,_,candidate=matcher.active_candidate()
                    stamp=matcher.now()
                    result={"classification":"potential_match","missing_information":[],"required_skills":[],"required_experience":[],"education_requirements":[]}
                    with jobs.database() as db:
                        db.execute("""INSERT OR REPLACE INTO job_match_results(user_id,job_key,resume_id,status,classification,match_score,result_json,created_at,updated_at,candidate_fingerprint,preferences_fingerprint,job_fingerprint,matching_model_version,freshness_status)
                            VALUES(?,?,?,'completed','potential_match',90,?,?,?,?,?,?,?,'current')""",
                            (USER,key,resume_id,json.dumps(result),stamp,stamp,matcher.profile_fingerprint(candidate,resume_id),
                             matcher.stable_fingerprint(prefs),matcher.job_fingerprint(opening),matcher.MATCHING_ENGINE_VERSION))
                return {"status":"completed"}
            queue_calls=[]
            def queue_request(request):
                result=app_agent.enqueue_saved_jobs(request)
                queue_calls.append((request.job_keys,result))
                return result
            with patch.object(jobs,"_employer_sources",return_value=[jobs.EmployerSource("Workflow Mock","greenhouse","workflow-mock","https://boards.greenhouse.io/workflow-mock")]), \
                 patch.object(jobs,"fetch_board",return_value=([posting],None)), \
                 patch.object(workflow,"_authorization",return_value={"allow_automated_form_preparation":True}), \
                 patch.object(workflow,"_versions",return_value=("r1","rv1","pv1",1,prefs)):
                workflow.run_workflow("wf-e2e",discover_fn=discover_and_route_to_local_mock,match_fn=mocked_ollama_result,
                                      queue_fn=queue_request)
            state=workflow._row("wf-e2e")
            applications=app_agent.list_applications()
            self.assertEqual(state["status"],"completed")
            self.assertEqual(state["discovered_count"],1)
            self.assertEqual(state["potential_match_count"],1)
            self.assertEqual(state["queued_count"],1,json.dumps({"state":state,"applications":applications,"queue_calls":queue_calls},default=str))
            self.assertEqual(state["prepared_count"],1)
            self.assertEqual(len(applications),1)
            self.assertEqual(applications[0]["state"],"ready_for_review")
            self.assertFalse(applications[0]["diagnostic"]["submission_performed"])
            self.assertFalse(json.loads(state["diagnostics_json"])["submission_enabled"])
        finally:
            workflow.DB, jobs.DB, career_preferences.DB=old_workflow_db,old_jobs_db,old_preferences_db

    def test_queue_continues_after_one_application_needs_user_input(self):
        with app_agent.connect() as db:
            db.execute("INSERT INTO discovered_openings(user_id,job_key,company,title,location,url,discovered_at,employment_type,location_status,location_reason,search_prompt,discovery_source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(USER,"saved-uncertain","Example Co","Engineer","Austin, TX","http://127.0.0.1:8000/mock-apps/uncertain","2026-01-04","Full time","eligible","","","Company Careers Page"))
            stamp=app_agent.now()
            db.execute("INSERT INTO job_match_results(user_id,job_key,resume_id,status,classification,match_score,result_json,error,created_at,updated_at,freshness_status,matching_model_version) VALUES(?,?,?,'completed','potential_match',99,'{}',NULL,?,?,'current',?)",(USER,"saved-uncertain","r1",stamp,stamp,matcher.MATCHING_ENGINE_VERSION))
        result=self.post("/api/applications/queue",{"job_keys":["saved-uncertain","saved-simple"]})
        self.assertEqual(result["queued"],2)
        with app_agent.connect() as db:
            states={r["job_key"]:r["state"] for r in db.execute("SELECT job_key,state FROM applications WHERE user_id=?",(USER,))}
        self.assertEqual(states["saved-uncertain"],"needs_user_input")
        self.assertEqual(states["saved-simple"],"ready_for_review")
        self.assertFalse(result["submission_enabled"])

    def test_queue_skips_unmatched_ineligible_and_invalid_jobs(self):
        result=self.post("/api/applications/queue",{"job_keys":["saved-invalid","saved-ineligible"]})
        self.assertEqual(result["queued"],0)
        self.assertEqual(len(result["skipped"]),2)
        self.assertTrue(all(item.get("reason") for item in result["skipped"]))

    def test_authorized_rolling_limit_remains_source_of_truth_on_dashboard(self):
        answer_assistant.update_authorization(answer_assistant.AuthorizationUpdate(max_applications_per_24_hours=7))
        progress=app_agent.application_progress()
        self.assertEqual(progress["daily_submission_limit"],7)
        self.assertEqual(app_agent.rolling_submission_quota()["limit"],7)

    def test_rolling_quota_is_atomic_and_uses_confirmed_successes_only(self):
        with app_agent.connect() as db:
            db.execute("UPDATE application_controls SET daily_limit=2,submission_enabled=1 WHERE user_id=?",(USER,))
            for i in range(6):
                db.execute("INSERT INTO applications(id,user_id,resume_id,job_key,company,title,application_url,state,mode,created_at,updated_at,state_changed_at,attempt_number) VALUES(?,?,? ,?,? ,? ,?,'ready_for_review','dry_run','2026-01-01','2026-01-01','2026-01-01',1)",('quota-app-'+str(i),USER,'r1','quota-job-'+str(i),'Example Co','Software Engineer','http://127.0.0.1:8000/mock-apps/simple'))
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            reservations=list(pool.map(lambda i:app_agent.reserve_submission_capacity('quota-app-'+str(i)),range(6)))
        held=[x for x in reservations if x["reserved"]]
        self.assertEqual(len(held),2)
        self.assertEqual(app_agent.rolling_submission_quota()["successful_last_24h"],0)
        app_agent.finalize_submission_reservation(held[0]["reservation_id"],"succeeded")
        app_agent.finalize_submission_reservation(held[1]["reservation_id"],"failed")
        quota=app_agent.rolling_submission_quota()
        self.assertEqual(quota["successful_last_24h"],1)
        self.assertEqual(quota["capacity_held"],0)
        self.assertEqual(quota["remaining"],1)
        held=app_agent.reserve_submission_capacity("quota-app-2"); self.assertTrue(held["reserved"])
        app_agent.finalize_submission_reservation(held["reservation_id"],"uncertain")
        self.assertEqual(app_agent.rolling_submission_quota()["capacity_held"],1)
        app_agent.resolve_uncertain_submission(held["reservation_id"],False)
        self.assertEqual(app_agent.rolling_submission_quota()["capacity_held"],0)
        with app_agent.connect() as db: db.execute("UPDATE application_controls SET daily_limit=1 WHERE user_id=?",(USER,))
        self.assertFalse(app_agent.reserve_submission_capacity("quota-app-3")["reserved"])
        with app_agent.connect() as db: db.execute("UPDATE application_controls SET submission_enabled=0 WHERE user_id=?",(USER,))

    def test_failed_grounding_draft_can_be_regenerated_without_losing_history(self):
        resume,_,_=app_agent._candidate()
        app_id,_,_=app_agent._create({"job_key":"draft-review","company":"Example Co","title":"Engineer","application_url":"http://127.0.0.1:8000/mock-apps/simple"},resume)
        app_agent._update(app_id,"needs_user_input",{"draft_answers":[{"question":"What programming languages have you used?","answer":"Unsupported fictional claim","max_chars":300,"validation":{"problems":["Unsupported named claim(s): FictionalCorp."]}}],"unanswered_fields":["Review the answer"]})
        class Response:
            def raise_for_status(self): pass
            def json(self): return {"response":json.dumps({"answer":"I used Python for backend software and technical work.","needs_user_input":False,"evidence_used":[]})}
        with patch.object(answer_assistant.requests,"post",return_value=Response()):
            result=app_agent.regenerate_application_draft(app_id,0)
        self.assertFalse(result["needs_user_input"])
        self.assertIn("Python",result["answer"])
        with app_agent.connect() as db:
            saved=json.loads(db.execute("SELECT diagnostic_json FROM applications WHERE id=?",(app_id,)).fetchone()[0])
            history=db.execute("SELECT COUNT(*) FROM application_attempts WHERE application_id=?",(app_id,)).fetchone()[0]
        self.assertEqual(saved["draft_answers"][0]["regeneration_count"],1)
        self.assertEqual(history,1)

    def test_saved_jobs_are_enriched_filtered_and_only_permitted_urls_preparable(self):
        data=app_agent.list_saved_application_jobs()
        self.assertEqual(data["count"],3)
        good=next(item for item in data["results"] if item["job_key"]=="saved-simple")
        bad=next(item for item in data["results"] if item["job_key"]=="saved-invalid")
        self.assertEqual(good["classification"],"potential_match")
        self.assertEqual(good["match_score"],88)
        self.assertTrue(good["preparation_allowed"])
        self.assertNotIn("access_token",good["url"])
        self.assertFalse(bad["preparation_allowed"])
        no_match=next(item for item in data["results"] if item["job_key"]=="saved-ineligible")
        self.assertFalse(no_match["preparation_allowed"])
        filtered=app_agent.list_saved_application_jobs(q="Austin",eligibility="potential_match")
        self.assertEqual(filtered["count"],1)

    def test_prepare_one_saved_job_reads_record_and_runs_only_selected_dry_run(self):
        response=app_agent.prepare_saved_job(app_agent.DryRunRequest(job_key="saved-simple"))
        self.assertEqual(response["status"],"queued")
        self.assertTrue(response["created"])
        self.assertEqual(len(app_agent.list_applications()),1)
        self.assertEqual(app_agent.list_applications()[0]["state"],"ready_for_review")
        self.assertFalse(app_agent.list_applications()[0]["diagnostic"]["submission_performed"])

    def test_prepare_reused_existing_attempt_reports_real_outcome_without_duplicating(self):
        first=app_agent.prepare_saved_job(app_agent.DryRunRequest(job_key="saved-simple"))
        count=len(app_agent.list_application_attempts(first["application_id"]))
        second=app_agent.prepare_saved_job(app_agent.DryRunRequest(job_key="saved-simple"))
        self.assertEqual(second["status"],"ready_for_review")
        self.assertFalse(second["created"])
        self.assertIn("already ready",second["message"])
        self.assertEqual(len(app_agent.list_application_attempts(first["application_id"])),count)

    def test_existing_needs_input_is_reported_and_requires_explicit_retry(self):
        resume,_,_=app_agent._candidate()
        job={"job_key":"saved-simple","company":"Example Co","title":"Software Developer","application_url":"http://127.0.0.1:8000/mock-apps/simple"}
        app_id,_,_=app_agent._create(job,resume)
        app_agent._update(app_id,"opening",{})
        app_agent._update(app_id,"needs_user_input",{"message":"Missing location.","unanswered_fields":["Location is required."]})
        response=app_agent.prepare_saved_job(app_agent.DryRunRequest(job_key="saved-simple"))
        self.assertEqual(response["status"],"needs_user_input")
        self.assertFalse(response["created"])
        item=next(x for x in app_agent.list_saved_application_jobs()["results"] if x["job_key"]=="saved-simple")
        self.assertEqual(item["missing_questions"],["Location is required."])
        self.assertFalse(item["preparation_allowed"])

    def test_browser_headless_environment_flag(self):
        import os
        self.assertTrue(app_agent.browser_headless())
        with patch.dict(os.environ,{"BABYSTEPS_BROWSER_HEADLESS":"false"}):
            self.assertFalse(app_agent.browser_headless())
            app_agent.start_dry_run(app_agent.DryRunRequest(job={"job_key":"visible-browser","company":"Test","title":"Developer","application_url":"http://127.0.0.1:8000/mock-apps/simple"}))
        self.assertFalse(self.client.launch_options[-1]["headless"])

    def test_browser_launch_failure_has_specific_safe_diagnostic(self):
        resume,candidate,answers=app_agent._candidate()
        job={"job_key":"launch-failure","company":"Test","title":"Developer","application_url":"http://127.0.0.1:8000/mock-apps/simple"}
        app_id,_,_=app_agent._create(job,resume)
        with patch.object(FakeChromium,"launch",side_effect=RuntimeError("browser not installed")):
            app_agent.process_application(app_id,job,resume,candidate,answers)
        item=next(x for x in app_agent.list_applications() if x["id"]==app_id)
        self.assertEqual(item["state"],"failed")
        self.assertEqual(item["diagnostic"]["failure_code"],"browser_launch_failed")
        self.assertIn("Chromium could not start",item["diagnostic"]["message"])
        self.assertNotIn("browser not installed",str(item["diagnostic"]))

    def test_dashboard_exposes_preparation_feedback_and_readable_controls(self):
        page=app_agent.applications_page().body.decode("utf-8")
        self.assertIn("Preparing application…",page)
        self.assertIn("job-actions",page)
        self.assertIn("Review profile and confirmed answers",page)
        self.assertIn("Failed",page)
        self.assertIn("Test AI-written answer",page)
        written=app_agent.mock_application("written").body.decode("utf-8")
        self.assertIn("relevant technical project",written)
        self.assertIn('maxlength="600"',written)

    def test_prepare_rejects_invalid_saved_url_before_browser(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as caught:
            app_agent.prepare_saved_job(app_agent.DryRunRequest(job_key="saved-invalid"))
        self.assertEqual(caught.exception.status_code,400)
        self.assertEqual(app_agent.list_applications(),[])

    def test_prepare_rejects_ai_classified_not_eligible_job(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as caught:
            app_agent.prepare_saved_job(app_agent.DryRunRequest(job_key="saved-ineligible"))
        self.assertEqual(caught.exception.status_code,409)
        self.assertEqual(app_agent.list_applications(),[])

    def test_saved_job_city_country_and_combined_location_use_confirmed_residence(self):
        profile.update_verification(profile.VerificationUpdate(values={"city":"Toronto","state_province":"Ontario","country":"Canada","preferred_locations":["Seattle, WA"],"address_line1":"10 Example Street"},confirmed_fields=["city","state_province","country","preferred_locations","address_line1"]))
        _,candidate,_=app_agent._candidate()
        self.assertEqual(app_agent._answer({"label":"City"},candidate,{})[0],"Toronto")
        self.assertEqual(app_agent._answer({"label":"Country of residence"},candidate,{})[0],"Canada")
        self.assertEqual(app_agent._answer({"label":"In what city, state, and country do you live?"},candidate,{})[0],"Toronto, Ontario, Canada")
        self.assertEqual(app_agent._answer({"label":"Current location"},candidate,{})[0],"Toronto, Ontario, Canada")
        self.assertEqual(app_agent._answer({"label":"Address Line 1"},candidate,{})[0],"10 Example Street")

    def test_country_and_state_selects_use_exact_confirmed_aliases(self):
        candidate={"country":"United States","state_province":"California"}
        country=FakeQuestionPage({"tag":"select","type":"select-one","label":"Country","required":True,"options":["Choose","United States","Canada"]})
        state=FakeQuestionPage({"tag":"select","type":"select-one","label":"State / Province","required":True,"options":["Choose","CA","NY"]})
        settings={"__settings":{"allow_auto_use_confirmed_answers":True}}
        report,missing,_=app_agent._fill_page(country,candidate,settings,Path(self.temp.name)/"r1.pdf")
        self.assertFalse(missing); self.assertEqual(country.fields.nth(0).selected,"United States")
        report,missing,_=app_agent._fill_page(state,candidate,settings,Path(self.temp.name)/"r1.pdf")
        self.assertFalse(missing); self.assertEqual(state.fields.nth(0).selected,"CA")

    def test_missing_location_is_not_inferred_from_work_preferences(self):
        self.assertEqual(app_agent._answer({"label":"Country"},{"preferred_locations":["Austin, Texas"]},{}),(None,None))

    def test_direct_saved_job_preparation_blocks_unanalyzed_and_needs_review_until_reviewed(self):
        from fastapi import HTTPException
        with app_agent.connect() as db:
            db.execute("INSERT INTO discovered_openings(user_id,job_key,company,title,location,url,discovered_at,employment_type,location_status,location_reason,search_prompt,discovery_source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(USER,"saved-unmatched","Example Co","Unmatched","Austin","http://127.0.0.1:8000/mock-apps/simple","2026-01-05","Full time","eligible","","","Company Careers Page"))
            db.execute("INSERT INTO discovered_openings(user_id,job_key,company,title,location,url,discovered_at,employment_type,location_status,location_reason,search_prompt,discovery_source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(USER,"saved-review","Example Co","Review needed","Austin","http://127.0.0.1:8000/mock-apps/multi-step","2026-01-05","Full time","eligible","","","Company Careers Page"))
            stamp=app_agent.now()
            db.execute("INSERT INTO job_match_results(user_id,job_key,resume_id,status,classification,match_score,result_json,error,created_at,updated_at,freshness_status,matching_model_version) VALUES(?,?,?,'completed','needs_review',91,'{}',NULL,?,?,'current',?)",(USER,"saved-review","r1",stamp,stamp,matcher.MATCHING_ENGINE_VERSION))
        with self.assertRaises(HTTPException) as caught:
            app_agent.prepare_saved_job(app_agent.DryRunRequest(job_key="saved-unmatched"))
        self.assertEqual(caught.exception.status_code,409)
        with self.assertRaises(HTTPException) as caught:
            app_agent.prepare_saved_job(app_agent.DryRunRequest(job_key="saved-review"))
        self.assertEqual(caught.exception.status_code,409)
        queue=app_agent.enqueue_saved_jobs(app_agent.ApplicationQueueRequest(job_keys=["saved-review","saved-unmatched"]))
        self.assertEqual(queue["queued"],0)
        self.assertTrue(app_agent.review_saved_job_eligibility("saved-review",app_agent.EligibilityReviewRequest(reviewed=True))["reviewed"])
        with app_agent.connect() as db: db.execute("UPDATE job_match_results SET updated_at='2099-10-18T12:00:00+00:00' WHERE user_id=? AND job_key='saved-review'",(USER,))
        self.assertFalse(next(x for x in app_agent.list_saved_application_jobs()["results"] if x["job_key"]=="saved-review")["preparation_allowed"])
        app_agent.review_saved_job_eligibility("saved-review",app_agent.EligibilityReviewRequest(reviewed=True))
        self.assertTrue(next(x for x in app_agent.list_saved_application_jobs()["results"] if x["job_key"]=="saved-review")["preparation_allowed"])
        queue=app_agent.enqueue_saved_jobs(app_agent.ApplicationQueueRequest(job_keys=["saved-review"]))
        self.assertEqual(queue["queued"],1)
        app_agent.record_eligibility_decision("saved-review","rejected")
        with app_agent.connect() as db:
            self.assertEqual(app_agent._eligibility_gate(db,"saved-review","r1")[0],"rejected")
        app_agent.review_saved_job_eligibility("saved-review",app_agent.EligibilityReviewRequest(reviewed=True))
        with app_agent.connect() as db:
            self.assertEqual(app_agent._eligibility_gate(db,"saved-review","r1")[0],"needs_review")

    def test_potential_match_with_unresolved_mandatory_evidence_requires_review(self):
        from fastapi import HTTPException
        with app_agent.connect() as db:
            db.execute("UPDATE job_match_results SET result_json=? WHERE user_id=? AND job_key='saved-simple' AND resume_id='r1'",(json.dumps({"required_experience":[{"requirement":"3 years","status":"unclear"}]}),USER))
        with self.assertRaises(HTTPException): app_agent.prepare_saved_job(app_agent.DryRunRequest(job_key="saved-simple"))
        with self.assertRaises(HTTPException):app_agent.review_saved_job_eligibility("saved-simple",app_agent.EligibilityReviewRequest(reviewed=True))
        self.assertFalse(next(x for x in app_agent.list_saved_application_jobs()["results"] if x["job_key"]=="saved-simple")["preparation_allowed"])

    def test_source_backed_year_requirement_cannot_be_reviewed_away_without_matching_evidence(self):
        description="THE SKILLSET YOU'LL BRING 6+ years of professional engineering experience."
        self.assertTrue(app_agent._unresolved_mandatory("{}",description))
        met={"required_experience":[{"requirement":"6+ years professional experience","status":"met","candidate_evidence":["Confirmed work experience dates"]}]}
        self.assertFalse(app_agent._unresolved_mandatory(json.dumps(met),description))

    def test_missing_questions_are_deduplicated_and_confirmed_answers_saved(self):
        resume,_,_=app_agent._candidate()
        job={"job_key":"answers-job","company":"Example Co","title":"Developer","application_url":"http://127.0.0.1:8000/mock-apps/simple"}
        app_id,_,_=app_agent._create(job,resume)
        question="Location (City) is required."
        app_agent._update(app_id,"opening",{})
        app_agent._update(app_id,"needs_user_input",{"unanswered_fields":[question,question,"Country is required."]})
        groups=app_agent.list_missing_application_questions(app_id)["groups"]
        self.assertEqual(sum(len(g["questions"]) for g in groups),2)
        result=app_agent.save_missing_application_answer(app_id,app_agent.MissingAnswerRequest(question=question,answer="Confirmed answer",reusable=True,confirmed=True))
        self.assertTrue(result["saved"])
        _,_,answers=app_agent._candidate()
        custom_key=next(key for key in answers if key.startswith("custom:"))
        self.assertIn("application-question-",custom_key)
        self.assertTrue(answers[custom_key]["auto_reuse"])
        self.assertEqual(app_agent._answer({"label":"City"},{},answers)[0],"Confirmed answer")
        refreshed=app_agent.list_missing_application_questions(app_id)
        self.assertTrue(any(item["existing_answer"]=="Confirmed answer" for group in refreshed["groups"] for item in group["questions"]))

    def test_validation_messages_group_by_actual_field_not_wrapper_text(self):
        groups=app_agent._group_questions(["Required answer is missing for: Country","No matching choice for: Country"])
        self.assertEqual(len(groups),1)
        self.assertEqual(groups[0]["category"],"Location")
        self.assertEqual(groups[0]["questions"][0]["question"],"Country")

    def test_country_and_state_option_aliases_are_localized_not_global_fuzzy_matches(self):
        self.assertEqual(app_agent._option_alias("US","country"),app_agent._option_alias("United States","country"))
        self.assertEqual(app_agent._option_alias("CA","state/province"),app_agent._option_alias("California","state/province"))
        self.assertNotEqual(app_agent._option_alias("Canada","state/province"),app_agent._option_alias("California","state/province"))

    def test_apply_now_link_navigates_to_form_but_never_submits(self):
        app_agent.start_dry_run(app_agent.DryRunRequest(job={"job_key":"mock-apply-link","company":"Mock Co","title":"Developer","application_url":"http://127.0.0.1:8000/mock-apps/job-page"}))
        item=app_agent.list_applications()[0]
        self.assertEqual(item["state"],"ready_for_review")
        self.assertEqual(item["diagnostic"]["steps"],1)
        self.assertEqual(self.client.last_page.submission_count,0)

    def test_apply_now_button_navigation_is_supported_without_submission(self):
        app_agent.start_dry_run(app_agent.DryRunRequest(job={"job_key":"mock-apply-button","company":"Mock Co","title":"Developer","application_url":"http://127.0.0.1:8000/mock-apps/apply-button"}))
        item=app_agent.list_applications()[0]
        self.assertEqual(item["state"],"ready_for_review")
        self.assertEqual(self.client.last_page.url,"http://127.0.0.1:8000/mock-apps/simple")
        self.assertEqual(self.client.last_page.submission_count,0)

    def test_submit_control_with_unusual_continue_label_is_never_clicked(self):
        app_agent.start_dry_run(app_agent.DryRunRequest(job={"job_key":"mock-weird-submit","company":"Mock Co","title":"Developer","application_url":"http://127.0.0.1:8000/mock-apps/weird-submit"}))
        item=app_agent.list_applications()[0]
        self.assertEqual(item["state"],"ready_for_review")
        self.assertFalse(item["diagnostic"]["submission_performed"])
        self.assertIn("Continue",item["diagnostic"]["submission_controls"])
        self.assertEqual(self.client.last_page.submission_count,0)

    def test_checkbox_and_radio_answers_use_confirmed_profile_values(self):
        app_agent.start_dry_run(app_agent.DryRunRequest(job={"job_key":"mock-choice-fields","company":"Mock Co","title":"Developer","application_url":"http://127.0.0.1:8000/mock-apps/choices"}))
        item=app_agent.list_applications()[0]
        self.assertEqual(item["state"],"ready_for_review")
        filled=item["diagnostic"]["filled_fields"]
        self.assertEqual(sum(x["result"]=="filled" for x in filled),2)

    def test_authentication_expired_listing_and_apply_loop_stop_safely(self):
        for scenario,expected in (("auth","needs_user_input"),("expired","unsupported"),("apply-loop","unsupported")):
            app_agent.start_dry_run(app_agent.DryRunRequest(job={"job_key":"mock-"+scenario,"company":"Mock Co","title":"Developer","application_url":"http://127.0.0.1:8000/mock-apps/"+scenario}))
            item=app_agent.list_applications()[0]
            self.assertEqual(item["state"],expected)

    def test_multi_step_form_visits_all_three_steps_before_review(self):
        response=self.post("/api/applications/dry-run",json={"job":{"job_key":"mock-multi","company":"Test Co","title":"Engineer","application_url":"http://127.0.0.1:8000/mock-apps/multi-step"}})
        app=next(x for x in self.get("/api/applications") if x["title"]=="Engineer")
        self.assertEqual(app["state"],"ready_for_review")
        self.assertEqual(app["diagnostic"]["steps"],3)

    def test_unknown_required_question_and_sensitive_authorization_pause(self):
        response=self.post("/api/applications/dry-run",json={"job":{"job_key":"mock-uncertain","company":"Test Co","title":"Engineer","application_url":"http://127.0.0.1:8000/mock-apps/uncertain"}})
        app=next(x for x in self.get("/api/applications") if x["title"]=="Engineer")
        self.assertEqual(app["state"],"needs_user_input")
        self.assertTrue(any("Human confirmation" in x for x in app["diagnostic"]["unanswered_fields"]))

    def test_human_challenge_is_unsupported_and_url_guard_blocks_private_targets(self):
        response=self.post("/api/applications/dry-run",json={"job":{"job_key":"mock-captcha","company":"Test Co","title":"Engineer","application_url":"http://127.0.0.1:8000/mock-apps/captcha"}})
        app=next(x for x in self.get("/api/applications") if x["title"]=="Engineer")
        self.assertEqual(app["state"],"unsupported")
        for url in ("http://127.0.0.1/private","https://127.0.0.1/","file:///etc/passwd"):
            with self.assertRaises(ValueError): app_agent.validate_application_url(url)

    def test_required_unknown_fields_and_unsupported_resume_type_pause_safely(self):
        profile.update_verification(profile.VerificationUpdate(values={"email":""},confirmed_fields=["email"]))
        self.post("/api/applications/dry-run",json={"job":{"job_key":"mock-validation","company":"Test Co","title":"Engineer","application_url":"http://127.0.0.1:8000/mock-apps/validation"}})
        app=next(x for x in self.get("/api/applications") if x["title"]=="Engineer")
        self.assertEqual(app["state"],"needs_user_input")
        self.assertTrue(any(x["result"]=="unsupported_file_type" for x in app["diagnostic"]["filled_fields"]))
        self.assertTrue(any("Required answer is missing" in x for x in app["diagnostic"]["unanswered_fields"]))

    def test_duplicate_url_does_not_create_a_second_history_record(self):
        payload={"job":{"job_key":"one","company":"Test Co","title":"Engineer","application_url":"http://127.0.0.1:8000/mock-apps/simple"}}
        first=self.post("/api/applications/dry-run",json=payload)
        payload["job"]["job_key"]="different-source-key"
        second=self.post("/api/applications/dry-run",json=payload)
        self.assertEqual(first["application_id"],second["application_id"])
        self.assertEqual(len(self.get("/api/applications")),1)

    def test_pause_resume_and_submission_settings(self):
        self.post("/api/applications/pause")
        self.assertTrue(self.get("/api/applications/progress")["paused"])
        self.post("/api/applications/resume")
        progress=self.get("/api/applications/progress")
        self.assertFalse(progress["submission_enabled"])
        self.assertLessEqual(progress["daily_submission_limit"],200)

    def test_queued_attempt_can_pause_and_resume_without_losing_attempt_record(self):
        resume,_,_=app_agent._candidate()
        job={"job_key":"pause-resume-real","company":"Test Co","title":"Software Developer","application_url":"http://127.0.0.1:8000/mock-apps/simple"}
        app_id,_,_=app_agent._create(job,resume)
        self.post("/api/applications/pause")
        self.assertEqual(next(x for x in self.get("/api/applications") if x["id"]==app_id)["state"],"paused")
        self.post("/api/applications/resume")
        item=next(x for x in self.get("/api/applications") if x["id"]==app_id)
        self.assertEqual(item["state"],"ready_for_review")
        self.assertEqual(len(app_agent.list_application_attempts(app_id)),1)

    def test_attempt_timestamps_and_transition_history_are_persisted(self):
        response=self.post("/api/applications/dry-run",json={"job":{"job_key":"timestamped","company":"Test Co","title":"Software Developer","application_url":"http://127.0.0.1:8000/mock-apps/simple"}})
        item=self.get("/api/applications")[0]
        self.assertEqual(item["state"],"ready_for_review")
        self.assertIsNotNone(item["started_at"]); self.assertIsNotNone(item["state_changed_at"])
        attempts=app_agent.list_application_attempts(item["id"])
        self.assertEqual(len(attempts),1); self.assertEqual(attempts[0]["state"],"ready_for_review")
        self.assertIsNotNone(attempts[0]["started_at"]); self.assertIsNotNone(attempts[0]["state_changed_at"])

    def test_stale_inflight_attempt_is_marked_interrupted_and_never_auto_retried(self):
        resume,_,_=app_agent._candidate()
        job={"job_key":"stale-job","company":"Test Co","title":"Software Developer","application_url":"http://127.0.0.1:8000/mock-apps/simple"}
        app_id,_,_=app_agent._create(job,resume)
        app_agent._update(app_id,"opening",{"message":"Opening"})
        with app_agent.connect() as db:
            db.execute("UPDATE applications SET heartbeat_at='2020-01-01T00:00:00+00:00' WHERE id=?",(app_id,))
            db.execute("UPDATE application_attempts SET heartbeat_at='2020-01-01T00:00:00+00:00' WHERE application_id=?",(app_id,))
        progress=self.get("/api/applications/progress")
        item=next(x for x in self.get("/api/applications") if x["id"]==app_id)
        self.assertEqual(item["state"],"paused"); self.assertTrue(item["diagnostic"]["interrupted"])
        result=self.post("/api/applications/resume")
        self.assertEqual(result["resumed"],0); self.assertEqual(result["needs_explicit_retry"],1)
        self.assertEqual(next(x for x in self.get("/api/applications") if x["id"]==app_id)["state"],"paused")

    def test_orphaned_queued_attempt_is_recovered_as_interrupted(self):
        resume,_,_=app_agent._candidate()
        job={"job_key":"orphaned-queue","company":"Test Co","title":"Software Developer","application_url":"http://127.0.0.1:8000/mock-apps/simple"}
        app_id,_,_=app_agent._create(job,resume)
        with app_agent.connect() as db:
            recovered=app_agent._recover_stale_in_db(db,startup=True)
        self.assertEqual(recovered,1)
        item=next(x for x in self.get("/api/applications") if x["id"]==app_id)
        self.assertEqual(item["state"],"paused")
        self.assertTrue(item["diagnostic"]["interrupted"])

    def test_duplicate_active_attempt_is_rejected(self):
        from fastapi import HTTPException
        resume,_,_=app_agent._candidate()
        job={"job_key":"active-job","company":"Test Co","title":"Software Developer","application_url":"http://127.0.0.1:8000/mock-apps/simple"}
        app_id,_,_=app_agent._create(job,resume)
        app_agent._update(app_id,"opening",{})
        with self.assertRaises(HTTPException) as caught: app_agent._create(job,resume)
        self.assertEqual(caught.exception.status_code,409)

    def test_only_one_application_dry_run_can_be_active_per_user(self):
        from fastapi import HTTPException
        resume,_,_=app_agent._candidate()
        first={"job_key":"active-1","company":"Test Co","title":"One","application_url":"http://127.0.0.1:8000/mock-apps/simple"}
        second={"job_key":"active-2","company":"Test Co","title":"Two","application_url":"http://127.0.0.1:8000/mock-apps/job-page"}
        app_id,_,_=app_agent._create(first,resume); app_agent._update(app_id,"opening",{})
        with self.assertRaises(HTTPException) as caught: app_agent._create(second,resume)
        self.assertEqual(caught.exception.status_code,409)

    def test_mutating_employer_requests_are_aborted_by_backend_guard(self):
        class Request: method="POST"; url="https://employer.example/apply"
        class Route:
            request=Request(); aborted=False; continued=False
            def abort(self): self.aborted=True
            def continue_(self): self.continued=True
        route=Route(); app_agent._guard_browser_request(route)
        self.assertTrue(route.aborted); self.assertFalse(route.continued)

    def test_retry_keeps_previous_attempt_diagnostics(self):
        response=self.post("/api/applications/dry-run",json={"job":{"job_key":"preserve-history","company":"Test Co","title":"Software Developer","application_url":"http://127.0.0.1:8000/mock-apps/simple"}})
        app_id=response["application_id"]
        first=app_agent.list_application_attempts(app_id)[0]
        self.assertTrue(first["diagnostic"]["filled_fields"])
        with app_agent.connect() as db:
            db.execute("UPDATE applications SET state='failed' WHERE id=?",(app_id,))
            db.execute("UPDATE application_attempts SET state='failed' WHERE application_id=?",(app_id,))
        app_agent.resume_one(app_id)
        attempts=app_agent.list_application_attempts(app_id)
        self.assertEqual(len(attempts),2)
        original=next(x for x in attempts if x["attempt_number"]==1)
        self.assertEqual(original["diagnostic"]["filled_fields"],first["diagnostic"]["filled_fields"])

    def test_dashboard_uses_human_statuses_without_raw_counts_or_ids(self):
        page=app_agent.APPLICATIONS_PAGE
        self.assertIn("Opening employer site",page); self.assertIn("Inspecting application form",page); self.assertIn("Needs your input",page)
        self.assertIn("Local test",page); self.assertIn("Diagnostics and attempt history",page)
        self.assertNotIn("JSON.stringify(p.counts)",page)
        self.assertNotIn("application_id</",page)

    def test_dashboard_links_missing_answers_and_private_screenshots(self):
        page=app_agent.APPLICATIONS_PAGE
        self.assertIn("Review candidate profile and answers",page)
        self.assertIn("View private dry-run screenshot",page)
        self.assertIn("/screenshot",page)

    def test_structured_profile_answers_cover_location_education_employment_and_projects(self):
        profile.update_verification(profile.VerificationUpdate(values={
            "location":"Austin, TX",
            "education_entries":[{"institution":"Example University","degree":"B.S.","major":"Computer Science","graduation_date":"2026-12","gpa":"3.8","currently_enrolled":False}],
            "experience_entries":[{"job_title":"Software Intern","employer":"Example Co.","location":"Austin","employment_type":"Internship","start_date":"2025-05","end_date":"2025-08","currently_working":False,"responsibilities":["Built APIs"],"technologies":["Python"]}],
            "project_entries":[{"name":"Portfolio App","description":"A tested demo","technologies":["React"],"github_url":"https://github.test/app","demo_url":"https://demo.test"}]},
            confirmed_fields=["location","education_entries:0","experience_entries:0","project_entries:0"]))
        _,candidate,answers=app_agent._candidate()
        value,_=app_agent._answer({"label":"Current location"},candidate,answers)
        self.assertEqual(value,"Austin, TX")
        value,_=app_agent._answer({"label":"Education history"},candidate,answers)
        self.assertIn("Computer Science",value); self.assertIn("December 2026",value)
        value,_=app_agent._answer({"label":"Employment history"},candidate,answers)
        self.assertIn("Example Co.",value); self.assertIn("Python",value)
        value,_=app_agent._answer({"label":"Projects description"},candidate,answers)
        self.assertIn("Portfolio App",value); self.assertIn("https://github.test/app",value)

    def test_confirmed_month_picker_value_reaches_application_answers(self):
        profile.update_verification(profile.VerificationUpdate(values={"graduation_date":"2026-12"},confirmed_fields=["graduation_date"]))
        _,facts,_=app_agent._candidate()
        self.assertEqual(facts["graduation_year"],"2026"); self.assertEqual(facts["graduation_month"],"12")
        value,key=app_agent._answer({"label":"Graduation month and year"},facts,{})
        self.assertEqual((value,key),("December 2026","graduation_date"))

    def test_job_discovery_source_answer_uses_the_recorded_source(self):
        _,candidate,answers=app_agent._candidate()
        job={"job_key":"saved-simple","company":"Example Co","title":"Software Developer","location":"Austin, TX"}
        app_agent._application_context(job,answers)
        value,source=app_agent._answer({"label":"How did you hear about this job?","tag":"select","type":"select"},candidate,answers)
        self.assertEqual((value,source),("Company Careers Page","discovery_source"))

    def test_unavailable_source_dropdown_choice_pauses_and_hidden_conditional_field_is_skipped(self):
        _,candidate,answers=app_agent._candidate()
        answers["__job_source"]="Company Careers Page"
        source_page=FakeQuestionPage({"tag":"select","type":"select-one","label":"How did you hear about this job?","required":True})
        _,missing,_=app_agent._fill_page(source_page,candidate,answers,Path(self.temp.name)/"r1.pdf")
        self.assertIn("No matching choice",missing[0])
        hidden_page=FakeQuestionPage({"tag":"textarea","type":"textarea","label":"Why are you interested in this role?","required":True,"hidden":True})
        _,missing,drafts=app_agent._fill_page(hidden_page,candidate,answers,Path(self.temp.name)/"r1.pdf")
        self.assertFalse(missing); self.assertFalse(drafts)

    def test_grounded_ai_answer_is_filled_and_persisted_with_validation_sources(self):
        _,candidate,answers=app_agent._candidate()
        candidate.update({"technical_skills":["Python"],"projects":[{"name":"Telemetry API"}]})
        answers["__settings"]["allow_ai_drafting"]=True
        answers["__job_context"]={"company":"Example Co","title":"Backend Engineer"}
        answers["__job_description"]="The team builds backend APIs."
        page=FakeQuestionPage({"tag":"textarea","type":"textarea","label":"Why are you interested in this position?","required":True,"max_length":160})
        draft={"answer":"I enjoy building Python services, and the backend role fits my project experience.","needs_user_input":False,
               "evidence_used":["confirmed candidate profile","saved job posting"],"validation":{"valid":True,"problems":[],"sources":["confirmed candidate profile","saved job posting"]}}
        with patch.object(app_agent,"generate_draft",return_value=draft):
            report,missing,drafts=app_agent._fill_page(page,candidate,answers,Path(self.temp.name)/"r1.pdf")
        self.assertEqual(page.fields.nth(0).value,draft["answer"])
        self.assertFalse(missing); self.assertEqual(drafts[0]["status"],"grounded_draft")
        self.assertEqual(drafts[0]["evidence_used"],draft["evidence_used"])
        self.assertEqual(report[0]["source"],"ai_draft")

    def test_written_question_dry_run_persists_grounded_answer_without_submission(self):
        draft={"answer":"I built a Python telemetry service and used FastAPI to aggregate requests.","needs_user_input":False,
               "evidence_used":["confirmed candidate profile"],"validation":{"valid":True,"problems":[],"sources":["confirmed candidate profile"]}}
        with patch.object(app_agent,"generate_draft",return_value=draft):
            result=self.post("/api/applications/dry-run",json={"job":{"job_key":"local-mock:written","company":"BabyStepsAI local test","title":"Test written","application_url":"http://127.0.0.1:8000/mock-apps/written"}})
        app=next(x for x in self.get("/api/applications") if x["id"]==result["application_id"])
        self.assertEqual(app["state"],"ready_for_review")
        saved=app["diagnostic"]["draft_answers"][0]
        self.assertEqual(saved["answer"],draft["answer"])
        self.assertEqual(saved["status"],"grounded_draft")
        self.assertFalse(saved["approved"])
        self.assertEqual(self.client.last_page.submission_count,0)

    def test_unvalidated_ai_answer_is_not_inserted_and_requests_review(self):
        _,candidate,answers=app_agent._candidate()
        answers["__settings"]["allow_ai_drafting"]=True
        page=FakeQuestionPage({"tag":"textarea","type":"textarea","label":"Why are you interested in this position?","required":True,"max_length":80})
        draft={"answer":"A generated answer that could not be grounded.","needs_user_input":True,"evidence_used":[],
               "validation":{"valid":False,"problems":["Not enough evidence."],"sources":[]}}
        with patch.object(app_agent,"generate_draft",return_value=draft):
            _,missing,drafts=app_agent._fill_page(page,candidate,answers,Path(self.temp.name)/"r1.pdf")
        self.assertEqual(page.fields.nth(0).value,"")
        self.assertIn("needs your review",missing[0]); self.assertEqual(drafts[0]["status"],"ready_to_review")

    def test_routes_are_registered_using_safe_route_path_inspection(self):
        paths={getattr(route,"path",None) for route in main.app.routes}
        documented=set(main.app.openapi()["paths"])
        for path in ("/candidate-profile","/applications","/api/candidate-profile/verification",
                     "/api/applications/dry-run","/api/applications/prepare","/api/applications/saved-jobs",
                     "/api/applications/pause","/mock-apps/{scenario}"):
            self.assertTrue(path in paths or path in documented,path)


if __name__=="__main__": unittest.main()
