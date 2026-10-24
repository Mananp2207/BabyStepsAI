import json
import os
import sqlite3
import sys
import concurrent.futures
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import HTTPException

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
    def evaluate(self, script):
        if "el.files?.length" in script:
            return {"count":1 if self.file else 0,"name":Path(self.file).name if self.file else ""}
        if "selectedOptions" in script: return getattr(self,"selected","")
        return self.info
    def fill(self, value): self.value = value
    def input_value(self): return self.value
    def is_checked(self): return self.checked
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
        if self.info.get("action")=="safe_next" and self.page.scenario!="stalled-step": self.page.stage+=1
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
    @property
    def scenario(self): return self.url.rstrip("/").rsplit("/",1)[-1]
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
                "stalled-step": [
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
        if scenario in {"multi-step","stalled-step"} and self.stage<2:
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
    def evaluate(self, _script): return []

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
        if path=="/api/applications/form-compatibility-tests": return app_agent.inspect_standalone_application_form(app_agent.StandaloneFormCompatibilityRequest(**json))

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
        self.assertTrue(app["diagnostic"]["form_detected"])
        self.assertEqual(app["diagnostic"]["provider"]["adapter"],"shared_semantic_form")
        self.assertEqual(app["diagnostic"]["filled_fields"][-1]["source"],"active_resume")

    def test_field_readback_mismatch_is_not_reported_as_filled(self):
        class RejectingField(FakeField):
            def fill(self, _value): pass
        class Page:
            def locator(self, _selector): return FakeFields([RejectingField({"tag":"input","type":"text","name":"name","label":"Full Name","required":True})])
        candidate={"full_name":"A Candidate"}
        answers={"__settings":{"allow_auto_use_confirmed_answers":True},"__is_local_mock":True}
        filled,missing,_=app_agent._fill_page(Page(),candidate,answers,self.storage/"r1.pdf")
        self.assertEqual(filled[0]["result"],"verification_failed")
        self.assertTrue(any("could not be verified" in item for item in missing))

    def test_required_conditional_controls_are_checked_after_field_scan(self):
        class Body:
            def evaluate(self,_script): return ["Additional eligibility question"]
        class Page:
            def locator(self,_selector): return Body()
        self.assertEqual(app_agent._required_field_issues(Page()),["Additional eligibility question"])

    def test_read_only_inspection_persists_report_without_creating_application_attempt(self):
        from unittest.mock import patch
        report={"provider":{"provider":"ashby","support_level":"accessibility_first"},
                "form_detected":True,"supported_fields":["Email"],"unsupported_fields":[],
                "required_fields":["Email"],"file_upload_controls":[],"human_actions_required":[],
                "multi_step_detected":False,"submission_control_detected":True,
                "inspection_outcome":"compatible","reason":"Public controls inspected."}
        before=app_agent.list_applications()
        with patch.object(app_agent,"inspect_public_application_form",return_value=dict(report)) as inspect:
            result=app_agent.inspect_saved_application_form(app_agent.FormInspectionRequest(job_key="saved-simple"))
        inspect.assert_called_once()
        self.assertEqual(result["inspection_outcome"],"compatible")
        self.assertEqual(result["employer"],"Example Co")
        self.assertNotIn("test-secret",json.dumps(result))
        self.assertEqual(app_agent.list_applications(),before)
        with app_agent.connect() as db:
            row=db.execute("SELECT report_json FROM application_form_inspections WHERE user_id=? AND job_key=?",(USER,"saved-simple")).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(json.loads(row["report_json"])["provider"]["provider"],"ashby")

    def test_standalone_compatibility_check_needs_no_saved_match_or_candidate_data(self):
        report={"application_url":"https://user:password@jobs.lever.co/example/role?session=secret","provider":{"provider":"lever"},
                "form_detected":False,"page_classification":"job_posting","inspection_outcome":"unsupported",
                "robots_policy":"verified_allow",
                "fields":[],"supported_fields":[],"unsupported_fields":[],"required_fields":[],
                "file_upload_controls":[],"human_actions_required":[],"multi_step_detected":False,
                "submission_control_detected":False,"reason":"A job posting was reached; no application form was exposed."}
        before=app_agent.list_applications()
        with patch.object(app_agent,"inspect_public_application_form",return_value=dict(report)) as inspect, \
             patch.object(profile,"verified_profile_facts",side_effect=AssertionError("candidate facts must not be read")):
            result=self.post("/api/applications/form-compatibility-tests",json={"application_url":"https://user:password@jobs.lever.co/example/role?session=secret"})
        inspect.assert_called_once_with("https://user:password@jobs.lever.co/example/role?session=secret")
        self.assertEqual(result["inspection_mode"],"standalone_read_only")
        self.assertEqual(result["page_classification"],"job_posting")
        self.assertNotIn("session=secret",json.dumps(result)); self.assertNotIn("password",json.dumps(result))
        self.assertEqual(result["source_url"],"https://jobs.lever.co/example/role")
        self.assertTrue(result.get("inspection_timestamp"))
        self.assertTrue(result.get("canonical_application_url"))
        self.assertTrue(result.get("form_fingerprint"))
        self.assertEqual(result.get("compatibility_status"),"posting_only")
        self.assertEqual(app_agent.list_applications(),before)
        with app_agent.connect() as db:
            row=db.execute("SELECT job_key,report_json FROM application_form_inspections WHERE id=?",(result["inspection_id"],)).fetchone()
        self.assertTrue(row["job_key"].startswith("standalone:"))
        self.assertNotIn("session=secret",row["report_json"])

    def test_standalone_endpoint_persists_blocked_robots_diagnostics(self):
        blocked={"application_url":"https://jobs.ashbyhq.com/example/job","inspection_outcome":"inaccessible",
                "provider":{"provider":"ashby"},"form_detected":False,"page_classification":"not_inspected",
                "robots_policy":"not_verified","failure_category":"policy_disallowed",
                "reason":"The employer's robots.txt explicitly disallows this read-only inspection path."}
        with patch.object(app_agent,"inspect_public_application_form",return_value=blocked):
            result=self.post("/api/applications/form-compatibility-tests",json={"application_url":"https://jobs.ashbyhq.com/example/job"})
        self.assertEqual(result["inspection_outcome"],"inaccessible")
        self.assertEqual(result["failure_category"],"policy_disallowed")
        with app_agent.connect() as db:
            saved=json.loads(db.execute("SELECT report_json FROM application_form_inspections WHERE id=?",(result["inspection_id"],)).fetchone()[0])
        self.assertEqual(saved["robots_policy"],"not_verified")

    def test_application_page_classification_distinguishes_posting_from_form(self):
        posting_controls=[{"action":"apply_link","label":"Apply Now"}]
        posting,kind=app_agent._classify_inspected_page("https://jobs.greenhouse.io/company/1",[],posting_controls,"Job description and qualifications")
        self.assertFalse(posting); self.assertEqual(kind,"job_posting")
        newsletter,kind=app_agent._classify_inspected_page("https://company.example/jobs/1",[
            {"label":"Email","kind":"email","inside_form":True}],posting_controls,"Job description and qualifications")
        self.assertFalse(newsletter); self.assertEqual(kind,"job_posting")
        application,kind=app_agent._classify_inspected_page("https://jobs.ashbyhq.com/company/1/application",[
            {"label":"Email","kind":"email","inside_form":True},{"label":"Resume","kind":"file","inside_form":True}],[],"Candidate application")
        self.assertTrue(application); self.assertEqual(kind,"application_form")

    def test_job_specific_url_redirecting_to_employer_root_is_not_reported_as_a_posting(self):
        self.assertTrue(app_agent._posting_redirected_away(
            "https://job-boards.greenhouse.io/planetlabs/jobs/8096079",
            "https://job-boards.greenhouse.io/planetlabs"))
        self.assertFalse(app_agent._posting_redirected_away(
            "https://job-boards.greenhouse.io/planetlabs/jobs/8096079",
            "https://job-boards.greenhouse.io/planetlabs/jobs/8096079"))
        self.assertFalse(app_agent._posting_redirected_away(
            "https://jobs.ashbyhq.com/notion/e32799d2-8ef8-4803-8189-c72514afa816/",
            "https://jobs.ashbyhq.com/notion/e32799d2-8ef8-4803-8189-c72514afa816/application"))

    def test_form_inspection_rejects_ineligible_or_stale_jobs(self):
        with self.assertRaises(HTTPException) as caught:
            app_agent.inspect_saved_application_form(app_agent.FormInspectionRequest(job_key="saved-ineligible"))
        self.assertEqual(caught.exception.status_code,409)
        with app_agent.connect() as db:
            db.execute("UPDATE job_match_results SET freshness_status='stale' WHERE user_id=? AND job_key='saved-simple'",(USER,))
        with self.assertRaises(HTTPException) as stale:
            app_agent.inspect_saved_application_form(app_agent.FormInspectionRequest(job_key="saved-simple"))
        self.assertEqual(stale.exception.status_code,409)

    def test_inspection_browser_path_only_navigates_and_reads_metadata(self):
        from unittest.mock import patch
        calls=[]
        class Response: status=200
        class Page:
            url="https://jobs.ashbyhq.com/example/job"
            def route(self,*args): calls.append("route")
            def goto(self,url,**kwargs): calls.append(("goto",url,kwargs)); return Response()
            def locator(self,*_args): return self
            def inner_text(self,**_kwargs): return "Public job application"
        class Context:
            def new_page(self): return Page()
            def close(self): pass
        class Browser:
            def new_context(self): return Context()
            def close(self): pass
        class Chromium:
            def launch(self,**_kwargs): return Browser()
        class Manager:
            chromium=Chromium()
            def __enter__(self): return self
            def __exit__(self,*_args): pass
        module=types.ModuleType("playwright.sync_api");module.sync_playwright=Manager
        with patch.dict(sys.modules,{"playwright.sync_api":module}), patch.object(app_agent,"validate_application_url"), patch.object(app_agent,"_robots_permission",return_value=(True,"","policy_verified")), patch.object(app_agent,"_visible_form_metadata",return_value=[{"label":"Email","kind":"email","required":True},{"label":"Resume","kind":"file","required":True}]), patch.object(app_agent,"_inspect_public_controls",return_value=[{"label":"Submit application","tag":"button","type":"submit","inside_form":True,"action":"final_submission"}]), patch.object(app_agent,"_interruption",return_value=None):
            report=app_agent.inspect_public_application_form("https://jobs.ashbyhq.com/example/job")
        self.assertEqual(report["inspection_outcome"],"compatible")
        self.assertEqual(calls[0],"route")
        self.assertEqual(calls[1][0],"goto")
        self.assertEqual([call[0] for call in calls if isinstance(call,tuple)], ["goto"])
        self.assertTrue(report["submission_control_detected"])
        self.assertEqual(report["page_classification"],"application_form")

    def test_inspection_stops_for_unsafe_urls_and_disallowed_robots_policy(self):
        with patch.object(app_agent,"validate_application_url",side_effect=ValueError("blocked")):
            unsafe=app_agent.inspect_public_application_form("http://127.0.0.1/private")
        self.assertEqual(unsafe["inspection_outcome"],"inaccessible")
        with patch.object(app_agent,"validate_application_url"), patch.object(app_agent,"_robots_permission",return_value=(False,"robots policy disallows inspection","policy_disallowed")):
            blocked=app_agent.inspect_public_application_form("https://example.org/jobs/1")
        self.assertEqual(blocked["inspection_outcome"],"inaccessible")
        self.assertIn("robots",blocked["reason"])
        with patch.object(app_agent,"_robots_permission",return_value=(False,"The employer's robots.txt host could not be resolved by this computer. Check DNS or network access and retry.","dns_failure")):
            dns=app_agent.inspect_public_application_form("https://unresolved.example/jobs/1")
        self.assertEqual(dns["failure_category"],"dns_failure"); self.assertIn("DNS",dns["reason"])

    def test_unresolved_required_greenhouse_controls_or_missing_submit_block_preparation(self):
        base={"robots_policy":"verified_allow","form_detected":True,"page_classification":"application_form",
              "inspection_outcome":"compatible","human_actions_required":[],"unsupported_fields":[]}
        missing_submit={**base,"submission_control_status":"not_detected_unresolved"}
        self.assertIn("submission control could not be identified",app_agent._preparation_inspection_blocker(missing_submit))
        unresolved={**base,"submission_control_status":"detected_visible",
                    "unresolved_controls":[{"question":"Are you legally authorized?","required":True,"reasons":["Unknown type"]}]}
        self.assertIn("Required form controls could not be identified reliably",app_agent._preparation_inspection_blocker(unresolved))

    def test_required_marker_recognizes_suffix_asterisk_used_by_greenhouse(self):
        self.assertTrue(app_agent._required_marker("Resume/CV*"))
        self.assertTrue(app_agent._required_marker("Email ✱"))
        self.assertFalse(app_agent._required_marker("Gender"))

    def test_robots_policy_is_parsed_for_each_requested_path(self):
        class Response:
            status=200
            def __enter__(self): return self
            def __exit__(self,*_args): pass
            def read(self,_limit): return b"User-agent: *\nDisallow: /apply\nAllow: /jobs\n"
        class Opener:
            def open(self,*_args,**_kwargs): return Response()
        with patch.object(app_agent,"validate_application_url"), patch.object(app_agent.urllib.request,"build_opener",return_value=Opener()):
            app_agent._robots_cache.clear()
            denied=app_agent._robots_permission("https://employer.example/apply")
            allowed=app_agent._robots_permission("https://employer.example/jobs/1")
            self.assertFalse(denied[0]); self.assertEqual(denied[2],"policy_disallowed")
            self.assertTrue(allowed[0]); self.assertEqual(allowed[2],"policy_verified")

    def test_robots_policy_retries_transient_timeout_and_caches_verified_policy(self):
        class Response:
            status=200
            def __enter__(self): return self
            def __exit__(self,*_args): pass
            def read(self,_limit): return b"User-agent: *\nAllow: /\nDisallow: /private\n"
        class Opener:
            calls=0
            requests=[]
            def open(self,request,*args,**kwargs):
                self.calls+=1
                self.requests.append((request.full_url,request.get_header("User-agent"),kwargs.get("timeout")))
                if self.calls==1: raise TimeoutError("temporary")
                return Response()
        opener=Opener()
        with patch.object(app_agent,"validate_application_url"), patch.object(app_agent.urllib.request,"build_opener",return_value=opener), patch.object(app_agent.time,"sleep"):
            app_agent._robots_cache.clear()
            self.assertTrue(app_agent._robots_permission("https://cache.example/jobs/1")[0])
            self.assertFalse(app_agent._robots_permission("https://cache.example/private/1")[0])
            self.assertEqual(opener.calls,2)
            self.assertEqual(opener.requests[0],("https://cache.example/robots.txt","BabyStepsAI-ReadOnlyInspector/1.0",5))

    def test_robots_policy_reports_exhausted_dns_and_tls_without_permission(self):
        from unittest.mock import Mock
        app_agent._robots_cache.clear()
        with patch.object(app_agent,"validate_application_url",side_effect=app_agent.ApplicationURLValidationError("dns_failure","dns")), patch.object(app_agent.urllib.request,"build_opener") as build, patch.object(app_agent.time,"sleep"):
            denied=app_agent._robots_permission("https://dns.example/job")
        self.assertFalse(denied[0]); self.assertEqual(denied[2],"dns_failure"); self.assertEqual(build.call_count,1)
        app_agent._robots_cache.clear()
        unsafe_validate=Mock(side_effect=app_agent.ApplicationURLValidationError("private_destination","private"))
        with patch.object(app_agent,"validate_application_url",unsafe_validate), patch.object(app_agent.urllib.request,"build_opener") as unsafe_build:
            unsafe=app_agent._robots_permission("https://private.example/job")
        self.assertFalse(unsafe[0]); self.assertEqual(unsafe[2],"unsafe_destination"); self.assertEqual(unsafe_validate.call_count,1); unsafe_build.assert_called_once()
        app_agent._robots_cache.clear()
        class Opener:
            calls=0
            def open(self,*_args,**_kwargs): self.calls+=1; raise app_agent.ssl.SSLError("certificate")
        opener=Opener()
        with patch.object(app_agent,"validate_application_url"), patch.object(app_agent.urllib.request,"build_opener",return_value=opener):
            tls=app_agent._robots_permission("https://tls.example/job")
        self.assertFalse(tls[0]); self.assertEqual(tls[2],"tls_error"); self.assertEqual(opener.calls,1)

    def test_robots_tls_context_uses_verified_system_or_ca_bundle_context(self):
        context=app_agent._robots_ssl_context()
        self.assertEqual(context.verify_mode,app_agent.ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        class Response:
            status=200
            def __enter__(self): return self
            def __exit__(self,*_args): pass
            def read(self,_limit): return b"User-agent: *\nAllow: /\n"
        class Opener:
            def open(self,*_args,**_kwargs): return Response()
        handlers=[]
        def build(*items): handlers.extend(items); return Opener()
        with patch.object(app_agent,"validate_application_url"), patch.object(app_agent.urllib.request,"build_opener",side_effect=build):
            app_agent._robots_cache.clear()
            allowed=app_agent._robots_permission("https://trusted.example/job")
        self.assertTrue(allowed[0]); self.assertTrue(any(isinstance(item,app_agent.urllib.request.HTTPSHandler) for item in handlers))
        https=next(item for item in handlers if isinstance(item,app_agent.urllib.request.HTTPSHandler))
        self.assertEqual(https._context.verify_mode,app_agent.ssl.CERT_REQUIRED)
        self.assertTrue(https._context.check_hostname)

    def test_robots_tls_invalid_certificate_and_missing_ca_fail_closed_with_sanitized_reason(self):
        class Opener:
            calls=0
            def open(self,*_args,**_kwargs):
                self.calls+=1
                raise app_agent.urllib.error.URLError(app_agent.ssl.SSLCertVerificationError(1,"invalid certificate details"))
        app_agent._robots_cache.clear(); opener=Opener()
        with patch.object(app_agent,"validate_application_url"), patch.object(app_agent.urllib.request,"build_opener",return_value=opener):
            invalid=app_agent._robots_permission("https://invalid-cert.example/job")
        self.assertFalse(invalid[0]); self.assertEqual(invalid[2],"tls_error")
        self.assertIn("SSLCertVerificationError",invalid[1]); self.assertNotIn("invalid certificate details",invalid[1]); self.assertEqual(opener.calls,1)
        app_agent._robots_cache.clear()
        with patch.dict(os.environ,{"SSL_CERT_FILE":"/definitely/missing/babysteps-test-ca.pem","SSL_CERT_DIR":"","REQUESTS_CA_BUNDLE":"","CURL_CA_BUNDLE":""}), \
             patch.object(app_agent.urllib.request,"build_opener") as build:
            missing=app_agent._robots_permission("https://missing-ca.example/job")
        self.assertFalse(missing[0]); self.assertEqual(missing[2],"tls_configuration"); build.assert_not_called()

    def test_explicit_ca_environment_is_used_by_verified_context(self):
        context=types.SimpleNamespace(verify_mode=None,check_hostname=False)
        with patch.dict(os.environ,{"SSL_CERT_FILE":"/configured/ca.pem","SSL_CERT_DIR":"","REQUESTS_CA_BUNDLE":"","CURL_CA_BUNDLE":""}), \
             patch.object(app_agent.ssl,"create_default_context",return_value=context) as create:
            result=app_agent._robots_ssl_context()
        create.assert_called_once_with(cafile="/configured/ca.pem",capath=None)
        self.assertIs(result,context); self.assertEqual(context.verify_mode,app_agent.ssl.CERT_REQUIRED); self.assertTrue(context.check_hostname)

    def test_robots_policy_bounds_retries_for_timeout_and_network_failures(self):
        class Opener:
            def __init__(self,exc): self.calls=0; self.exc=exc
            def open(self,*_args,**_kwargs): self.calls+=1; raise self.exc
        for exc,category in ((TimeoutError("slow"),"timeout"),(app_agent.urllib.error.URLError(OSError("offline")),"network_error")):
            app_agent._robots_cache.clear(); opener=Opener(exc)
            with patch.object(app_agent,"validate_application_url"), patch.object(app_agent.urllib.request,"build_opener",return_value=opener), patch.object(app_agent.time,"sleep"):
                result=app_agent._robots_permission("https://transient.example/job")
            self.assertFalse(result[0]); self.assertEqual(result[2],category); self.assertEqual(opener.calls,app_agent.ROBOTS_MAX_ATTEMPTS)

    def test_robots_policy_rejects_malformed_policy_and_unsafe_redirect(self):
        class Response:
            status=200
            def __enter__(self): return self
            def __exit__(self,*_args): pass
            def read(self,_limit): return b"<html>blocked</html>"
        class Opener:
            def open(self,*_args,**_kwargs): return Response()
        app_agent._robots_cache.clear()
        with patch.object(app_agent,"validate_application_url"), patch.object(app_agent.urllib.request,"build_opener",return_value=Opener()):
            malformed=app_agent._robots_permission("https://malformed.example/job")
        self.assertFalse(malformed[0]); self.assertEqual(malformed[2],"malformed_policy")
        handler=app_agent.SafeRobotsRedirectHandler()
        with patch.object(app_agent,"validate_application_url") as validate:
            redirected=handler.redirect_request(app_agent.urllib.request.Request("https://public.example/robots.txt"),None,302,"Found",{},"https://cdn.public.example/policy")
        self.assertEqual(redirected.full_url,"https://cdn.public.example/policy"); validate.assert_called_once_with("https://cdn.public.example/policy")
        with patch.object(app_agent,"validate_application_url",side_effect=app_agent.ApplicationURLValidationError("private_destination","private")):
            with self.assertRaises(app_agent.RobotsRedirectBlocked):
                handler.redirect_request(types.SimpleNamespace(full_url="https://public.example/robots.txt"),None,302,"Found",{},"http://127.0.0.1/robots.txt")

    def test_robots_http_404_and_permanent_http_errors_fail_closed(self):
        class Opener:
            calls=0
            def __init__(self,code): self.code=code
            def open(self,*_args,**_kwargs):
                self.calls+=1
                if self.code: raise app_agent.urllib.error.HTTPError("https://example.test/robots.txt",self.code,"",{},None)
                raise AssertionError("not expected")
        app_agent._robots_cache.clear(); opener=Opener(404)
        with patch.object(app_agent,"validate_application_url"), patch.object(app_agent.urllib.request,"build_opener",return_value=opener):
            missing=app_agent._robots_permission("https://example.test/open")
            self.assertFalse(missing[0]); self.assertEqual(missing[2],"policy_missing")
            app_agent._robots_permission("https://example.test/other")
        self.assertEqual(opener.calls,2)
        app_agent._robots_cache.clear(); opener=Opener(403)
        with patch.object(app_agent,"validate_application_url"), patch.object(app_agent.urllib.request,"build_opener",return_value=opener):
            forbidden=app_agent._robots_permission("https://forbidden.example/open")
        self.assertFalse(forbidden[0]); self.assertEqual(forbidden[2],"http_status"); self.assertEqual(opener.calls,1)

    @unittest.skipUnless(os.getenv("BABYSTEPS_RUN_REAL_CHROMIUM")=="1","Set BABYSTEPS_RUN_REAL_CHROMIUM=1 to launch installed Chromium.")
    def test_real_chromium_local_mock_reaches_ready_for_review_without_submission(self):
        submitted=[]
        html=b'''<!doctype html><html><head><title>Job Application for Synthetic Engineer at Synthetic Employer</title></head><body><h1>Local synthetic application</h1><form>
          <label>Full Name <input name="name" required></label>
          <label>Email <input name="email" type="email" required></label>
          <label>Phone <input name="phone" required></label>
          <label>Resume <input name="resume" type="file" accept=".pdf" required></label>
          <button type="submit">Submit application</button></form></body></html>'''
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/mock-apps/"):
                    self.send_response(200);self.send_header("Content-Type","text/html");self.end_headers();self.wfile.write(html)
                else:self.send_error(404)
            def do_POST(self):
                submitted.append(self.path);self.send_error(405)
            def log_message(self,*_args): pass
        server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
        port=server.server_address[1]
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        old_artifacts=app_agent.ARTIFACTS
        app_agent.ARTIFACTS=Path(self.temp.name)/"artifacts"
        app_agent.ARTIFACTS.mkdir()
        self.patch_playwright.stop()
        try:
            original_mock_allowed=app_agent._mock_allowed
            with patch.object(app_agent,"_mock_allowed",side_effect=lambda url:original_mock_allowed(url) or
                              (url.startswith(f"http://127.0.0.1:{port}/mock-apps/"))):
                result=app_agent.start_dry_run(app_agent.DryRunRequest(job={"job_key":"real-chromium-local","company":"Synthetic Employer","title":"Synthetic Engineer","application_url":f"http://127.0.0.1:{port}/mock-apps/application"}))
                with patch.object(app_agent,"_robots_permission",return_value=(True,"","policy_verified")):
                    inspection=app_agent.inspect_public_application_form(f"http://127.0.0.1:{port}/mock-apps/application")
            app=next(item for item in app_agent.list_applications() if item["id"]==result["application_id"])
            self.assertEqual(app["state"],"ready_for_review")
            self.assertTrue(app["diagnostic"]["form_detected"])
            self.assertEqual(app["diagnostic"]["provider"]["provider"],"employer_site")
            self.assertIn("active_resume",{item.get("source") for item in app["diagnostic"]["filled_fields"]})
            self.assertEqual(len(app["diagnostic"]["required_fields"]),4,app["diagnostic"])
            self.assertFalse(app["diagnostic"]["submission_performed"])
            self.assertEqual(inspection["inspection_outcome"],"compatible",json.dumps(inspection))
            self.assertTrue(inspection["submission_control_detected"])
            self.assertEqual(inspection["required_fields"],["Full Name","Email","Phone","Resume"])
            self.assertEqual(inspection["file_upload_controls"],["Resume"])
            self.assertEqual(inspection["page_metadata"]["job_title"],"Local synthetic application")
            self.assertEqual(inspection["page_metadata"]["employer"],"Synthetic Employer")
            self.assertEqual(inspection["page_metadata"]["employer_source"],"document_title")
            self.assertNotIn("candidate@example.test",json.dumps(inspection))
            self.assertEqual(submitted,[])
        finally:
            server.shutdown();server.server_close();thread.join(timeout=2);app_agent.ARTIFACTS=old_artifacts

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

    def _make_external_eligible_job(self):
        key="saved-external"
        url="https://jobs.ashbyhq.com/example/software-engineer"
        stamp=app_agent.now()
        with app_agent.connect() as db:
            db.execute("INSERT INTO discovered_openings(user_id,job_key,company,title,location,url,job_url,discovered_at,employment_type,location_status,location_reason,search_prompt,discovery_source,active) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                (USER,key,"Example Employer","Software Engineer I","New York, NY",url,"https://jobs.ashbyhq.com/example/software-engineer",stamp,"Full time","eligible","","","Company Careers Page"))
            db.execute("INSERT INTO job_match_results(user_id,job_key,resume_id,status,classification,match_score,result_json,error,created_at,updated_at,freshness_status,matching_model_version) VALUES(?,?,?,'completed','potential_match',90,'{}',NULL,?,?,'current',?)",
                (USER,key,"r1",stamp,stamp,matcher.MATCHING_ENGINE_VERSION))
            report={"provider":{"provider":"ashby","support_level":"accessibility_first"},"form_detected":True,
                "page_classification":"application_form","inspection_outcome":"compatible","robots_policy":"verified_allow",
                "requested_application_url":url,"required_fields":["Full name"],"unsupported_fields":[],
                "submission_control_status":"detected_visible","submission_control_detected":True,
                "fields":[{"label":"Full name","kind":"text","required":True}],"human_actions_required":[]}
            db.execute("INSERT INTO application_form_inspections(id,user_id,job_key,provider,outcome,report_json,inspected_at) VALUES(?,?,?,?,?,?,?)",
                ("inspection-external",USER,key,"ashby","compatible",json.dumps(report),stamp))
        return key,url

    def test_job_specific_authorization_is_required_for_real_employers(self):
        key,url=self._make_external_eligible_job()
        with patch.object(app_agent,"validate_application_url"):
            with self.assertRaises(HTTPException) as caught:
                app_agent.start_dry_run(app_agent.DryRunRequest(job_key=key))
        self.assertEqual(caught.exception.status_code,403)
        self.assertEqual(app_agent.list_applications(),[])
        item=next(row for row in app_agent.list_saved_application_jobs()["results"] if row["job_key"]==key)
        self.assertTrue(item["preparation_authorization_required"])
        self.assertFalse(item["preparation_allowed"])

    def test_authorization_plan_lists_only_field_names_and_binds_exact_job_versions(self):
        key,url=self._make_external_eligible_job()
        with patch.object(app_agent,"validate_application_url"):
            plan=app_agent.preparation_authorization_plan(key)
            self.assertNotIn("candidate@example.test",json.dumps(plan))
            available={item["key"] for item in plan["candidate_fields"]}
            self.assertIn("full_name",available)
            saved=app_agent.authorize_saved_job_preparation(key,app_agent.PreparationAuthorizationRequest(
                confirmed=True,confirmed_employer=plan["employer"],confirmed_title=plan["title"],
                confirmed_application_url=plan["application_url"],field_keys=["full_name","email"],
                document_types=["resume"],allow_ai_answers=False,allow_cloud_ai=False))
        self.assertTrue(saved["authorized"])
        self.assertTrue(app_agent._current_preparation_authorization(key,"r1",url))
        item=next(row for row in app_agent.list_saved_application_jobs()["results"] if row["job_key"]==key)
        self.assertTrue(item["preparation_allowed"])
        scoped=app_agent._scoped_candidate(app_agent._candidate()[1],app_agent._current_preparation_authorization(key,"r1",url))
        self.assertEqual(set(scoped)-{k for k in scoped if k.startswith("__")},{"full_name","email"})
        self.assertNotIn("phone",scoped)

    def test_authorization_is_invalidated_by_profile_or_settings_change_and_revocable(self):
        key,url=self._make_external_eligible_job()
        with patch.object(app_agent,"validate_application_url"):
            plan=app_agent.preparation_authorization_plan(key)
            app_agent.authorize_saved_job_preparation(key,app_agent.PreparationAuthorizationRequest(
                confirmed=True,confirmed_employer=plan["employer"],confirmed_title=plan["title"],
                confirmed_application_url=plan["application_url"],field_keys=["full_name"],document_types=["resume"]))
        self.assertTrue(app_agent._current_preparation_authorization(key,"r1",url))
        profile.update_verification(profile.VerificationUpdate(values={"full_name":"Candidate Updated","email":"candidate@example.test"},confirmed_fields=["full_name","email"]))
        self.assertIsNone(app_agent._current_preparation_authorization(key,"r1",url))

    def test_inspection_report_is_not_reused_after_application_url_changes(self):
        key,url=self._make_external_eligible_job()
        with patch.object(app_agent,"validate_application_url"):
            _,_,_,_,_,_,report,_=app_agent._saved_preparation_context(key)
            self.assertIsNotNone(report)
            with app_agent.connect() as db:
                db.execute("UPDATE discovered_openings SET url=? WHERE user_id=? AND job_key=?",
                    ("https://jobs.ashbyhq.com/example/changed/application",USER,key))
            _,_,_,_,_,_,stale_report,_=app_agent._saved_preparation_context(key)
        self.assertIsNone(stale_report)

    def test_authorization_blocks_gemini_without_separate_cloud_consent(self):
        key,url=self._make_external_eligible_job()
        answer_assistant.update_authorization(answer_assistant.AuthorizationUpdate(
            allow_automated_form_preparation=True,allow_ai_drafting=True,matching_provider="gemini"))
        with patch.object(app_agent,"validate_application_url"),patch.dict(os.environ,{"GEMINI_API_KEY":"test-key"}):
            plan=app_agent.preparation_authorization_plan(key)
            with self.assertRaises(HTTPException) as caught:
                app_agent.authorize_saved_job_preparation(key,app_agent.PreparationAuthorizationRequest(
                    confirmed=True,confirmed_employer=plan["employer"],confirmed_title=plan["title"],
                    confirmed_application_url=plan["application_url"],field_keys=["full_name"],document_types=["resume"],
                    allow_ai_answers=True,allow_cloud_ai=False))
        self.assertEqual(caught.exception.status_code,409)

    def test_authorized_real_job_runs_a_fresh_read_only_preflight_before_filling(self):
        key,url=self._make_external_eligible_job()
        with patch.object(app_agent,"validate_application_url"):
            plan=app_agent.preparation_authorization_plan(key)
            app_agent.authorize_saved_job_preparation(key,app_agent.PreparationAuthorizationRequest(
                confirmed=True,confirmed_employer=plan["employer"],confirmed_title=plan["title"],
                confirmed_application_url=plan["application_url"],field_keys=["full_name"],document_types=["resume"]))
        report={"provider":{"provider":"ashby"},"form_detected":True,"page_classification":"application_form",
            "inspection_outcome":"needs_human_action","robots_policy":"verified_allow",
            "required_fields":["Full name"],"unsupported_fields":[],"human_actions_required":["CAPTCHA or human verification"],
            "fields":[{"label":"Full name","kind":"text","required":True}]}
        with patch.object(app_agent,"validate_application_url"),patch.object(app_agent,"inspect_public_application_form",return_value=report):
            response=app_agent.start_dry_run(app_agent.DryRunRequest(job_key=key))
        saved=next(row for row in app_agent.list_applications() if row["job_key"]==key)
        self.assertEqual(response["mode"],"dry_run")
        self.assertEqual(saved["state"],"needs_user_input")
        self.assertTrue(saved["diagnostic"]["human_actions_required"])
        self.assertEqual(saved["diagnostic"]["filled_fields"],[])
        self.assertEqual(saved["diagnostic"]["authorization"]["status"],"current")
        self.assertEqual(saved["diagnostic"]["authorization"]["selected_field_count"],1)
        self.assertFalse(saved["diagnostic"]["submission_performed"])

    def test_scope_filters_confirmed_reusable_answers_and_document_types(self):
        candidate={"full_name":"Synthetic","skills":["Python"],"__application_documents":[
            {"document_type":"transcript","path":"/private/transcript.pdf"},
            {"document_type":"certificate","path":"/private/certificate.pdf"}]}
        answers={"custom:project":{"answer":"Synthetic project","question":"Describe a project","confirmed":True,"auto_reuse":True},
            "custom:location":{"answer":"Unknown place","question":"Where do you live?","confirmed":True,"auto_reuse":True},
            "__settings":{"matching_provider":"ollama"}}
        scope={"field_keys":["full_name","custom:project"],"document_types":["transcript"],
            "authorization_id":"auth-test","allow_ai_answers":False,"allow_cloud_ai":False,"authorized_at":"2026-01-01"}
        filtered_candidate=app_agent._scoped_candidate(candidate,scope)
        filtered_answers=app_agent._scoped_answers(answers,scope)
        self.assertEqual(filtered_candidate["full_name"],"Synthetic")
        self.assertEqual([x["document_type"] for x in filtered_candidate["__application_documents"]],["transcript"])
        self.assertIn("custom:project",filtered_answers)
        self.assertNotIn("custom:location",filtered_answers)

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

    def test_browser_route_cleanup_tolerates_playwright_cancellation(self):
        class Request: method="GET"; url="about:blank"
        class Route:
            request=Request()
            def continue_(self):raise app_agent.asyncio.CancelledError()
        app_agent._guard_browser_request(Route(),{})

    def test_inspection_aborts_background_post_without_treating_it_as_page_navigation(self):
        class Request:
            method="POST";url="https://jobs.ashbyhq.com/telemetry"
            def is_navigation_request(self):return False
        class Route:
            request=Request();aborted=False
            def abort(self):self.aborted=True
        state={};route=Route();app_agent._guard_browser_request(route,state)
        self.assertTrue(route.aborted)
        self.assertEqual(state,{"blocked_non_read_only_request_count":1})

    def test_inspection_stops_when_non_read_only_request_is_top_level_navigation(self):
        class Request:
            method="POST";url="https://jobs.lever.co/example/apply"
            def is_navigation_request(self):return True
        class Route:
            request=Request();aborted=False
            def abort(self):self.aborted=True
        state={};route=Route();app_agent._guard_browser_request(route,state)
        self.assertTrue(route.aborted)
        self.assertEqual(state["category"],"read_only_method_blocked")

    def test_explicit_visual_required_markers_are_recognized_without_matching_question_wording(self):
        self.assertTrue(app_agent._required_marker("Resume/CV ✱ ATTACH RESUME/CV"))
        self.assertTrue(app_agent._required_marker("Email *"))
        self.assertFalse(app_agent._required_marker("Do you require sponsorship?"))

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
        self.assertIn("Authorize Preparation",page)
        self.assertIn("Job-specific authorization current",page)
        self.assertIn("Open official application page",page)
        self.assertIn("View private dry-run screenshot",page)
        self.assertIn("/screenshot",page)

    def test_dashboard_exposes_separate_read_only_employer_inspection(self):
        page=app_agent.APPLICATIONS_PAGE
        self.assertIn("Inspect Employer Form",page)
        self.assertIn("Compatibility: Not yet inspected",page)
        self.assertIn("/api/applications/form-inspections",page)
        self.assertIn("no candidate information entered",page)
        self.assertIn("Form compatibility",page)

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

    def test_preferred_name_uses_only_separate_confirmed_profile_fact(self):
        value,source=app_agent._answer({"label":"Preferred name","type":"text"},{"preferred_name":"Alex"}, {})
        self.assertEqual((value,source),("Alex","preferred_name"))
        value,source=app_agent._answer({"label":"Preferred name","type":"text"},{"full_name":"Alex Example"}, {})
        self.assertIsNone(value)
        self.assertIsNone(source)

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

    def test_safe_next_control_must_advance_before_form_can_be_marked_ready(self):
        app_agent.MOCK_PAGES["stalled-step"]=app_agent.MOCK_PAGES["multi-step"]
        try:
            self.post("/api/applications/dry-run",json={"job":{"job_key":"mock-stalled-step","company":"Test Co","title":"Engineer","application_url":"http://127.0.0.1:8000/mock-apps/stalled-step"}})
            app=self.get("/api/applications")[0]
            self.assertEqual(app["state"],"needs_user_input")
            self.assertTrue(any("did not advance" in x for x in app["diagnostic"]["unanswered_fields"]))
            self.assertFalse(app["diagnostic"]["submission_performed"])
        finally:
            app_agent.MOCK_PAGES.pop("stalled-step",None)

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
                     "/api/applications/form-inspections",
                     "/api/applications/pause","/mock-apps/{scenario}"):
            self.assertTrue(path in paths or path in documented,path)


if __name__=="__main__": unittest.main()
