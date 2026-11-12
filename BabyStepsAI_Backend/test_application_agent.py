import json
import inspect
import os
import sqlite3
import sys
import concurrent.futures
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import tempfile
import time
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

    def _save_evidence_review_fixture(self, description, *, years=0):
        key="review-authentic-"+str(time.time_ns())
        with app_agent.connect() as db:
            db.execute("INSERT INTO discovered_openings(user_id,job_key,company,title,location,url,discovered_at,employment_type,location_status,location_reason,search_prompt,discovery_source,description) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (USER,key,"Authentic","New Grad Software Engineer","New York, New York, United States",
                 "https://job-boards.greenhouse.io/authenticinsurance/jobs/4114318009","2026-02-16","","eligible","","",
                 "Official company posting",description))
            opening=dict(db.execute("SELECT * FROM discovered_openings WHERE user_id=? AND job_key=?",(USER,key)).fetchone())
        profile_data={"education":[{"degree":"B.S.","major":"Computer Science","graduation_date":"2026-12"}],
            "graduation_date":"2026-12","technical_skills":["Python"],
            "projects":[{"name":"Verified web application","description":"Built a web application with a React frontend and implemented backend API services."}],
            "dated_software_experience_months":years}
        prefs={"roles":["Software Engineer"],"levels":["New Graduate"],"employment_types":["Full-time"],
            "location":"","relocation_preference":"anywhere","work_arrangement":"Any"}
        result=matcher.MatchResult.model_validate({
            "classification":"needs_review","match_score":70,"explanation":"Candidate facts need review.",
            "strengths":[],"gaps":[],"missing_information":["Employment type is not provided in Greenhouse metadata."],
            "required_skills":[],
            "preferred_skills":[],"required_experience":[],"preferred_experience":[],
            "education_requirements":[],"preferred_education_requirements":[],
            "education_assessment":"Degree evidence is available.","gpa_assessment":"No GPA stated.","graduation_assessment":"Graduation evidence is available.",
            "experience_assessment":"No years threshold stated.","experience_level_assessment":"New graduate role.",
            "location_assessment":"New York is compatible.",
            "employment_type_assessment":"The posting does not specify employment type; the saved full-time preference remains unverified.",
            "evidence_consistency":["Bachelors degree in Computer Science, graduation date 2026-12",
                "Experience in software engineering through projects and team project"]})
        stamp=matcher.now()
        with app_agent.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS job_match_result_history(history_id INTEGER PRIMARY KEY AUTOINCREMENT,user_id TEXT,job_key TEXT,resume_id TEXT,archived_at TEXT,reason TEXT,snapshot_json TEXT)")
            db.execute("""INSERT INTO job_match_results(user_id,job_key,resume_id,status,classification,match_score,result_json,error,
                created_at,updated_at,candidate_fingerprint,preferences_fingerprint,job_fingerprint,matching_model_version,freshness_status)
                VALUES(?,?,?,'completed','needs_review',70,?,NULL,?,?,?,?,?,?,'current')""",
                (USER,key,"r1",result.model_dump_json(),stamp,stamp,matcher.profile_fingerprint(profile_data,"r1"),
                 matcher.stable_fingerprint(prefs),matcher.job_fingerprint(opening),matcher.MATCHING_ENGINE_VERSION))
        return key,profile_data,prefs

    def _evidence_review_request(self,key,profile_data,prefs,**changes):
        with app_agent.connect() as db:
            row=db.execute("SELECT * FROM job_match_results WHERE user_id=? AND job_key=? AND resume_id='r1'",(USER,key)).fetchone()
            opening=dict(db.execute("SELECT * FROM discovered_openings WHERE user_id=? AND job_key=?",(USER,key)).fetchone())
        request={"match_updated_at":row["updated_at"],"profile_fingerprint":matcher.profile_fingerprint(profile_data,"r1"),
            "preferences_fingerprint":matcher.stable_fingerprint(prefs),"job_fingerprint":matcher.job_fingerprint(opening),
            "matching_engine_version":matcher.MATCHING_ENGINE_VERSION,"accept_unspecified_employment_type":True,
            "acknowledge_ai_diagnostics":True}
        request.update(changes)
        return app_agent.TargetedEligibilityReviewRequest(**request)

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

    def test_current_needs_review_job_allows_read_only_inspection_but_not_preparation(self):
        key="saved-simple"
        with app_agent.connect() as db:
            db.execute("UPDATE job_match_results SET classification='needs_review',result_json=? WHERE user_id=? AND job_key=?",
                (json.dumps({"classification":"needs_review","match_score":58}),USER,key))
        report={"application_url":"http://127.0.0.1:8000/mock-apps/simple","inspection_outcome":"partially_compatible",
            "robots_policy":"verified_allow","form_detected":True,"page_classification":"application_form",
            "submission_control_status":"not_detected_unresolved","human_actions_required":[],
            "unsupported_fields":[],"unresolved_controls":[],"fields":[]}
        with patch.object(app_agent,"inspect_public_application_form",return_value=report) as inspect:
            saved=app_agent.inspect_saved_application_form(app_agent.FormInspectionRequest(job_key=key))
        self.assertEqual(inspect.call_count,1)
        self.assertEqual(inspect.call_args.args[0].split("?",1)[0],"http://127.0.0.1:8000/mock-apps/simple")
        self.assertEqual(saved["compatibility_status"],"unresolved")
        self.assertFalse(saved["preparation_preflight"])
        with app_agent.connect() as db:
            application_count=db.execute("SELECT COUNT(*) FROM applications WHERE user_id=? AND job_key=?",(USER,key)).fetchone()[0]
            authorization_count=db.execute("SELECT COUNT(*) FROM application_preparation_authorizations WHERE user_id=? AND job_key=? AND revoked_at IS NULL",(USER,key)).fetchone()[0]
        self.assertEqual(application_count,0)
        self.assertEqual(authorization_count,0)
        with self.assertRaises(HTTPException) as blocked:
            app_agent.preparation_authorization_plan(key)
        self.assertEqual(blocked.exception.status_code,409)

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
        unknown={**base,"submission_control_status":"detected_visible",
                 "unknown_requirement_questions":[{"question":"Upload supporting document"}]}
        self.assertIn("Required status is unknown",app_agent._preparation_inspection_blocker(unknown))
        upload={**base,"submission_control_status":"detected_visible",
                "unresolved_controls":[{"question":"Resume/CV*","kind":"file_upload_prompt","required":True}]}
        self.assertIn("Resume/CV*",app_agent._preparation_inspection_blocker(upload))

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
          <div class="application--field"><label for="resume">Resume*</label><button type="button" aria-controls="resume">Attach</button><input id="resume" name="resume" type="file" accept=".pdf" required style="display:none"></div>
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
            self.assertEqual(inspection["required_fields"],["Full Name","Email","Phone","Resume*"])
            self.assertEqual(inspection["file_upload_controls"],["Resume*"])
            self.assertTrue(inspection["upload_control_details"][0]["supported"])
            self.assertEqual(inspection["upload_control_details"][0]["mechanism"],"hidden_native_input")
            self.assertEqual(inspection["upload_control_details"][0]["association_method"],"accessible_label")
            self.assertEqual(inspection["page_metadata"]["job_title"],"Local synthetic application")
            self.assertEqual(inspection["page_metadata"]["employer"],"Synthetic Employer")
            self.assertEqual(inspection["page_metadata"]["employer_source"],"document_title")
            self.assertNotIn("candidate@example.test",json.dumps(inspection))
            self.assertEqual(submitted,[])
        finally:
            server.shutdown();server.server_close();thread.join(timeout=2);app_agent.ARTIFACTS=old_artifacts

    @unittest.skipUnless(os.getenv("BABYSTEPS_RUN_REAL_CHROMIUM")=="1","Set BABYSTEPS_RUN_REAL_CHROMIUM=1 to launch installed Chromium.")
    def test_real_chromium_manual_outcome_ui_uses_synthetic_api_and_never_submits(self):
        synthetic={"id":"synthetic-app-1","job_key":"synthetic:employer:job-1","resume_id":"synthetic-resume",
            "company":"Synthetic Employer","title":"Synthetic New Grad Engineer","application_url":"https://example.invalid/apply",
            "source_url":"https://example.invalid/jobs/1","state":"needs_user_input","mode":"dry_run","diagnostic":{"submission_performed":False},
            "error":None,"created_at":"2026-10-01T00:00:00Z","updated_at":"2026-10-01T00:00:00Z",
            "last_updated_at":"2026-10-01T00:00:00Z","last_attempt_at":"2026-10-01T00:00:00Z","attempt_number":1,"attempt_count":1,
            "action_required_count":0,"action_required_groups":[],"readiness":{"status_label":"Needs Your Input","progress_percent":50,
                "eligibility":{"status":"Current","reason":"Synthetic only"},"authorization":{"status":"Current","reason":"Synthetic only"},
                "questions":{"remaining":0,"reason":"No required questions remain."},"documents":{"needed":[],"status":"Resume ready"},
                "human_actions":{"required":[],"count":0},"browser_compatibility":"Supported","last_checkpoint":"Synthetic checkpoint",
                "resume_allowed":False,"resume_reason":"Not resumable","retry_allowed":False,"retry_reason":"No retry required",
                "retry_creates_new_attempt":False,"manual_submission_outcome":"","manual_completion_reported":False}}
        saved_job={"job_key":"synthetic:netic:job-1","company":"Synthetic Employer","title":"Synthetic New Grad Engineer",
            "location":"San Francisco","employment_type":"Full-time","classification":"potential_match","match_score":68,
            "eligibility_stale":False,"preparation_authorized":False,"application_state":"not_started",
            "application_status_label":"Not prepared","preparation_allowed":False,"preparation_authorization_required":False,
            "preparation_plan_available":True,"url_syntax_allowed":True,"url":"https://example.invalid/apply",
            "eligibility_review_assessment":{"classification":"potential_match","preparation_blocked":True,
                "eligibility_resolved":False,"mandatory_unresolved":["Release cycles"],"confirmed_conflicts":[],
                "next_action":"Review the synthetic release-cycle evidence."}}
        readiness_plan={"job_key":saved_job["job_key"],"employer":"Synthetic Employer","title":saved_job["title"],
            "application_url":"https://example.invalid/apply","eligibility":"needs_review","preparation_ready":False,
            "preparation_blockers":[{"requirement":"Release cycles","explanation":"Synthetic evidence is unresolved."}],
            "missing_requirements":[{"requirement":"Multiple release cycles","status":"needs_review","missing_question":"Describe the confirmed release sequence."}],
            "missing_applicant_information":["Describe the confirmed release sequence."],"candidate_fields":[{"key":"full_name","label":"Full name","sensitive":False}],
            "available_documents":[{"type":"resume","label":"Active resume"}],"inspection_blocker":"Synthetic form is unverified.",
            "human_only_actions":["Synthetic form controls remain unverified."],"attempt_history_count":0,
            "live_submission_enabled":False,"ai_answers_proposed":[],"cloud_processing_permission":"No cloud permission is granted.",
            "ai_drafting_enabled":True,"local_ai_answer_permission_available":True,"cloud_ai_authorized":False,
            "preparation_authorized":False,"authorization_current":False,"posting_verified_at":"2026-10-01T00:00:00Z",
            "provider":{"provider":"ashby"}}
        state={"posts":[],"authorization_posts":[],"submission_enabled":False,"requests":[]};errors=[];browser=None
        self.patch_playwright.stop()
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={"width":390,"height":844})
                page.on("pageerror",lambda error:errors.append(str(error)))
                page.on("request",lambda req:(state["requests"].append(req.url),state["authorization_posts"].append(req.url) if req.method=="POST" and req.url.endswith("/preparation-authorization") else None))
                def route_api(route):
                    path=route.request.url.split("?",1)[0]
                    if path.endswith("/api/applications"):
                        body=[synthetic]
                    elif path.endswith("/api/applications/progress"):
                        body={"counts":{"needs_user_input":1},"paused":False,"submission_enabled":False,"processing_state":"idle","submission_quota":{"remaining":0}}
                    elif path.endswith("/api/application-questions/attention"):
                        body={"groups":[],"technical_groups":[]}
                    elif path.endswith("/api/applications/saved-jobs"):
                        body={"results":[saved_job]}
                    elif path.endswith("/preparation-authorization"):
                        body=readiness_plan
                    elif path.endswith("/manual-outcome"):
                        payload=json.loads(route.request.post_data or "{}")
                        state["posts"].append(payload);synthetic["readiness"]["manual_submission_outcome"]=payload.get("outcome","")
                        body={"saved":True,"verified_by_readback":True,"state":"needs_user_input","submitted_at":None,"submission_enabled":False}
                    else:body={}
                    route.fulfill(status=200,content_type="application/json",body=json.dumps(body))
                page.route("**/api/**",route_api)
                page.goto("http://127.0.0.1:8000/applications",wait_until="domcontentloaded")
                page.locator("#load-jobs").click()
                page.get_by_role("button",name="View Preparation Readiness").click()
                readiness=page.get_by_role("region",name="Read-only preparation readiness plan")
                self.assertIn("Preparation readiness · read only",readiness.inner_text())
                readiness.locator("details summary").nth(1).click()
                self.assertIn("Multiple release cycles",readiness.inner_text())
                self.assertIn("No authorization was created",readiness.inner_text())
                self.assertIn("Live submission: disabled",readiness.inner_text())
                self.assertEqual(state["authorization_posts"],[])
                self.assertEqual(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"),True)
                page.locator(".bsa-mobile-bar .bsa-theme-toggle").click()
                self.assertEqual(page.locator("html").get_attribute("data-bsa-theme"),"dark")
                for width,height in ((390,844),(768,1024),(1440,950)):
                    page.set_viewport_size({"width":width,"height":height})
                    self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"),f"horizontal overflow at {width}px")
                page.locator(".bsa-sidebar .bsa-theme-toggle").click()
                self.assertEqual(page.locator("html").get_attribute("data-bsa-theme"),"light")
                page.set_viewport_size({"width":390,"height":844})
                card=page.locator(".application-card");card.wait_for()
                self.assertIn("Synthetic New Grad Engineer",card.inner_text())
                page.get_by_text("Record a manual application outcome").click()
                page.get_by_label("Manual application outcome").select_option("submitted_manually")
                page.get_by_role("checkbox",name="I confirm this report is accurate.").check()
                page.get_by_role("button",name="Save manual outcome").click()
                page.get_by_text("Saved and verified.").wait_for()
                self.assertEqual(state["posts"],[{"outcome":"submitted_manually","confirmed":True,"event_id":state["posts"][0]["event_id"]}])
                self.assertFalse(state["submission_enabled"])
                self.assertEqual(synthetic["state"],"needs_user_input")
                self.assertFalse(any("example.invalid" in url for url in state["requests"]))
                self.assertEqual(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"),True)
                self.assertFalse(errors,errors)
        finally:
            if browser is not None:
                try:browser.close()
                except Exception:pass  # The Playwright context manager already closed it on a failed assertion.

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

    def test_standing_policy_derives_a_job_scoped_grant_before_automatic_queueing(self):
        key="standing-policy-synthetic"
        job={"job_key":key,"company":"Policy Example","title":"New Graduate Software Engineer",
            "application_url":"https://jobs.example.test/apply/123","job_url":"https://jobs.example.test/jobs/123",
            "location":"Austin, Texas, United States","employment_type":"Full time","career_category":"Software Engineering",
            "country":"United States","work_arrangement":"Remote","last_verified_at":app_agent.now(),
            "description":"New graduate full-time software engineer"}
        with app_agent.connect() as db:
            db.execute("INSERT INTO discovered_openings(user_id,job_key,company,title,location,url,discovered_at,employment_type,location_status,location_reason,search_prompt,discovery_source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (USER,key,job["company"],job["title"],job["location"],job["application_url"],app_agent.now(),job["employment_type"],"eligible","","","Synthetic official source"))
            db.execute("INSERT INTO job_match_results(user_id,job_key,resume_id,status,classification,match_score,result_json,error,created_at,updated_at,freshness_status,matching_model_version) VALUES(?,?,?,'completed','potential_match',0,'{}',NULL,?,?,'current',?)",
                (USER,key,"r1",app_agent.now(),app_agent.now(),matcher.MATCHING_ENGINE_VERSION))
            setting_version=int(db.execute("SELECT version FROM application_authorizations WHERE user_id=?",(USER,)).fetchone()[0])
        snapshot={"resume_id":"r1","application_url_hash":"url-hash","match_updated_at":"match-stamp",
            "matching_engine_version":matcher.MATCHING_ENGINE_VERSION,"profile_fingerprint":"profile-hash",
            "answers_fingerprint":"answers-hash","documents_fingerprint":"documents-hash","settings_version":setting_version,
            "match_status":"completed","freshness_status":"current","classification":"potential_match"}
        settings={"allow_automated_form_preparation":True,"standing_policy_enabled":True,"standing_policy_field_keys":["full_name"],
            "standing_policy_document_types":["resume"],"standing_policy_reuse_confirmed_answers":False,
            "allow_auto_use_confirmed_answers":False,"standing_policy_allow_ai_answers":False,"allow_ai_drafting":False,
            "standing_policy_allow_cloud_ai":False,"matching_provider":"ollama","standing_policy_daily_limit":1}
        context=(job,{"id":"r1"},{"full_name":"A Candidate"},{},[{"key":"full_name"}],[{"type":"resume"}],
            {"compatibility_status":"compatible","requested_application_url":job["application_url"]},snapshot)
        with (patch.object(app_agent,"get_authorization",return_value={"version":setting_version,"settings":settings}),
              patch.object(app_agent,"_get_job",return_value=job), patch.object(app_agent,"validate_application_url"),
              patch.object(app_agent,"_saved_preparation_context",return_value=context),
              patch.object(app_agent,"_preparation_inspection_blocker",return_value=""),
              patch.object(app_agent,"_unmapped_requirements_blocker",return_value=""),
              patch.object(app_agent,"_standing_policy_blocker",return_value=""),
              patch.object(app_agent,"_preparation_snapshot",return_value=snapshot),
              patch.object(app_agent,"_standing_policy_still_current",return_value=True),
              patch.object(app_agent,"_schedule_application_queue",return_value=True)):
            result=app_agent.enqueue_saved_jobs(app_agent.ApplicationQueueRequest(job_keys=[key]))
            scope=app_agent._current_preparation_authorization(key,"r1",job["application_url"])
            scoped=app_agent._scoped_candidate({"full_name":"A Candidate","work_authorization":"US authorized",
                "__application_documents":[{"document_type":"resume"},{"document_type":"transcript"}]},scope)
            scoped_answers=app_agent._scoped_answers({"custom:private":{"answer":"private answer"},"__settings":{}},scope)
            job2={**job,"job_key":key+"-2"};context2=(*context[:1],*context[1:7],{**snapshot,"application_url_hash":"url-hash-2"})
            with patch.object(app_agent,"_saved_preparation_context",return_value=context2):
                granted_again,reason=app_agent._derive_standing_policy_authorization(job2["job_key"],"r1",job2["application_url"])
        self.assertEqual(result["queued"],1)
        self.assertFalse(result["submission_enabled"])
        self.assertEqual(scoped,{"full_name":"A Candidate","__application_documents":[{"document_type":"resume"}]})
        self.assertNotIn("custom:private",scoped_answers)
        self.assertFalse(scoped_answers["__settings"]["allow_auto_use_confirmed_answers"])
        self.assertFalse(granted_again)
        self.assertIn("daily preparation limit",reason)
        with app_agent.connect() as db:
            authorization=db.execute("SELECT authorization_source,field_keys_json,document_types_json FROM application_preparation_authorizations WHERE user_id=? AND job_key=? AND revoked_at IS NULL",(USER,key)).fetchone()
            application=db.execute("SELECT state FROM applications WHERE user_id=? AND job_key=?",(USER,key)).fetchone()
        self.assertEqual(authorization["authorization_source"],"standing_policy")
        self.assertEqual(json.loads(authorization["field_keys_json"]),["full_name"])
        self.assertEqual(json.loads(authorization["document_types_json"]),["resume"])
        self.assertEqual(application["state"],"queued")

    def test_persistent_opening_identity_blocks_cross_source_duplicates_and_auto_retries(self):
        resume={"id":"r1"}
        first={"job_key":"ashby:board-one:req-742","company":"Acme Systems","title":"Software Engineer I",
            "application_url":"http://127.0.0.1:8000/mock-apps/simple?source=one"}
        second={**first,"job_key":"ashby:board-two:req-742","application_url":"http://127.0.0.1:8000/mock-apps/simple?source=two"}
        mirrored={**first,"job_key":"greenhouse:acme:req-742","application_url":"http://127.0.0.1:8000/mock-apps/simple?utm_source=import"}
        changed_req={**first,"job_key":"ashby:board-one:req-743","title":"Software Engineer I — Platform"}
        app_id,state,created=app_agent._create(first,resume,allow_queue=True)
        duplicate_id,duplicate_state,duplicate_created=app_agent._create(second,resume,allow_queue=True)
        mirrored_id,mirrored_state,mirrored_created=app_agent._create(mirrored,resume,allow_queue=True)
        new_id,new_state,new_created=app_agent._create(changed_req,resume,allow_queue=True)
        self.assertTrue(created);self.assertEqual(state,"queued")
        self.assertEqual(duplicate_id,app_id);self.assertEqual(duplicate_state,"skipped_duplicate");self.assertFalse(duplicate_created)
        self.assertEqual(mirrored_id,app_id);self.assertEqual(mirrored_state,"skipped_duplicate");self.assertFalse(mirrored_created)
        self.assertTrue(new_created);self.assertNotEqual(new_id,app_id);self.assertEqual(new_state,"queued")
        app_agent._update(app_id,"opening")
        app_agent._update(app_id,"failed",error="synthetic interruption")
        retry_id,retry_state,retry_created=app_agent._create(first,resume,allow_queue=True)
        self.assertEqual(retry_id,app_id);self.assertEqual(retry_state,"failed");self.assertFalse(retry_created)
        with app_agent.connect() as db:db.execute("UPDATE applications SET attempt_number=? WHERE id=?",(app_agent.MAX_APPLICATION_ATTEMPTS,app_id))
        with self.assertRaises(HTTPException) as retry_limit:app_agent.retry_one(app_id)
        self.assertEqual(retry_limit.exception.status_code,409)
        racing=[{**first,"job_key":f"ashby:race-{n}:req-800","application_url":f"http://127.0.0.1:8000/mock-apps/simple?race={n}"} for n in (1,2)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            outcomes=list(pool.map(lambda job:app_agent._create(job,resume,allow_queue=True),racing))
        self.assertEqual(sum(1 for item in outcomes if item[2]),1)
        self.assertEqual(len({item[0] for item in outcomes}),1)
        unknown={**first,"job_key":"ashby:acme:req-900"}
        unknown_id,_,_=app_agent._create(unknown,resume,allow_queue=True)
        with app_agent.connect() as db:
            db.execute("INSERT INTO manual_submission_events(event_id,application_id,user_id,attempt_number,outcome,confirmed_by_applicant,recorded_at) VALUES(?,?,?,1,'outcome_unknown',1,?)",
                ("synthetic-unknown-event",unknown_id,USER,app_agent.now()))
            db.execute("UPDATE applications SET state='failed' WHERE id=?",(unknown_id,))
        blocked_id,blocked_state,blocked_created=app_agent._create(unknown,resume,allow_queue=True)
        self.assertEqual(blocked_id,unknown_id);self.assertEqual(blocked_state,"failed");self.assertFalse(blocked_created)
        mirror={**unknown,"job_key":"greenhouse:another-board:req-900"}
        mirror_id,mirror_state,mirror_created=app_agent._create(mirror,resume,allow_queue=True)
        self.assertEqual(mirror_id,unknown_id);self.assertEqual(mirror_state,"skipped_duplicate");self.assertFalse(mirror_created)
        with app_agent.connect() as db:
            count=db.execute("SELECT COUNT(*) FROM applications WHERE user_id=?",(USER,)).fetchone()[0]
            reservations=db.execute("SELECT COUNT(*) FROM application_opening_reservations WHERE user_id=?",(USER,)).fetchone()[0]
        self.assertEqual(count,4);self.assertEqual(reservations,4)

    def test_reopened_requisition_is_human_review_only_and_never_queued(self):
        with app_agent.connect() as db:
            db.execute("UPDATE discovered_openings SET active=1,availability_status='current',reopened_review_required=1 WHERE user_id=? AND job_key='saved-simple'",(USER,))
        queued=app_agent.enqueue_saved_jobs(app_agent.ApplicationQueueRequest(job_keys=["saved-simple"]))
        self.assertEqual(queued["queued"],0)
        self.assertIn("closed and reopened",queued["skipped"][0]["reason"])
        card=next(item for item in app_agent.list_saved_application_jobs()["results"] if item["job_key"]=="saved-simple")
        self.assertEqual(card["application_status_label"],"Needs Human Review · Reopened requisition")
        self.assertFalse(card["preparation_allowed"])

    def test_attention_inbox_is_application_grouped_and_manual_submission_stays_clear(self):
        self.assertIn('id="attention-inbox"',app_agent.APPLICATIONS_PAGE)
        self.assertIn("function renderAttentionInbox(apps)",app_agent.APPLICATIONS_PAGE)
        self.assertIn("Human actions grouped once per application",app_agent.APPLICATIONS_PAGE)
        self.assertIn("submit manually if you choose",app_agent.APPLICATIONS_PAGE)

    def test_standing_policy_scope_fails_closed_for_stale_or_unclassified_postings(self):
        settings={"standing_policy_enabled":True,"standing_policy_excluded_employers":[],
            "standing_policy_categories":["Software Engineering"],"standing_policy_employment_types":["Full-time"],
            "standing_policy_countries":["United States"],"standing_policy_locations":["Anywhere"],
            "standing_policy_work_arrangements":["Remote"],"standing_policy_experience_levels":["New Graduate"]}
        with (patch.object(app_agent,"get_authorization",return_value={"settings":settings}),
              patch.object(app_agent,"_standing_policy_still_current",return_value=True)):
            stale=app_agent._standing_policy_blocker({"active":False,"availability_status":"unknown"})
            incomplete=app_agent._standing_policy_blocker({"active":True,"availability_status":"current","career_category":""})
        self.assertIn("inactive or is not verified",stale)
        self.assertIn("category is outside or missing",incomplete)

    def test_authorized_rolling_limit_remains_source_of_truth_on_dashboard(self):
        answer_assistant.update_authorization(answer_assistant.AuthorizationUpdate(max_applications_per_24_hours=7))
        progress=app_agent.application_progress()
        self.assertEqual(progress["daily_submission_limit"],7)
        self.assertEqual(app_agent.rolling_submission_quota()["limit"],7)

    def test_progress_poll_does_not_rewrite_unchanged_control_timestamp(self):
        with app_agent.connect() as db:
            before=db.execute("SELECT paused,daily_limit,submission_enabled,updated_at FROM application_controls WHERE user_id=?",(USER,)).fetchone()
        app_agent.application_progress()
        with app_agent.connect() as db:
            after=db.execute("SELECT paused,daily_limit,submission_enabled,updated_at FROM application_controls WHERE user_id=?",(USER,)).fetchone()
        self.assertEqual(tuple(after),tuple(before))

    def test_initialize_does_not_rewrite_unchanged_application_control_timestamp(self):
        with app_agent.connect() as db:
            before=db.execute("SELECT paused,daily_limit,submission_enabled,updated_at FROM application_controls WHERE user_id=?",(USER,)).fetchone()
        app_agent.initialize()
        with app_agent.connect() as db:
            after=db.execute("SELECT paused,daily_limit,submission_enabled,updated_at FROM application_controls WHERE user_id=?",(USER,)).fetchone()
        self.assertEqual(tuple(after),tuple(before))

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

    def _make_external_eligible_job(self,description=""):
        key="saved-external"
        url="https://jobs.ashbyhq.com/example/software-engineer"
        stamp=app_agent.now()
        with app_agent.connect() as db:
            db.execute("INSERT INTO discovered_openings(user_id,job_key,company,title,location,url,job_url,discovered_at,employment_type,location_status,location_reason,search_prompt,discovery_source,active,description) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)",
                (USER,key,"Example Employer","Software Engineer I","New York, NY",url,"https://jobs.ashbyhq.com/example/software-engineer",stamp,"Full time","eligible","","","Company Careers Page",description))
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

    def test_unassessed_plaintext_requirements_block_authorization_and_preparation(self):
        description=("What You'll Bring: Proven product builder able to ship software at scale. "
            "Full-stack fluency with React, TypeScript, and Python. AI experience (nice-to-have). "
            "We need to hire graduates available to start between winter 2026 and summer 2027.")
        key,url=self._make_external_eligible_job(description)
        with patch.object(app_agent,"validate_application_url"):
            plan=app_agent.preparation_authorization_plan(key)
            self.assertIn("mandatory posting requirements",plan["inspection_blocker"])
            self.assertFalse(plan["preparation_ready"])
            self.assertFalse(plan["preparation_authorized"])
            self.assertFalse(plan["live_submission_enabled"])
            self.assertFalse(plan["ai_answers_authorized"])
            self.assertTrue(plan["missing_requirements"])
            self.assertEqual(plan["attempt_history_count"],0)
            request=app_agent.PreparationAuthorizationRequest(confirmed=True,confirmed_employer=plan["employer"],
                confirmed_title=plan["title"],confirmed_application_url=plan["application_url"],
                field_keys=["full_name"],document_types=["resume"])
            with self.assertRaises(HTTPException) as authorization_error:
                app_agent.authorize_saved_job_preparation(key,request)
            self.assertEqual(authorization_error.exception.status_code,409)
            with self.assertRaises(HTTPException) as preparation_error:
                app_agent.start_dry_run(app_agent.DryRunRequest(job_key=key))
            self.assertEqual(preparation_error.exception.status_code,409)
        self.assertEqual(app_agent.list_applications(),[])

    def test_read_only_authorization_proposal_explains_unresolved_eligibility_without_granting_access(self):
        description="Requirements: Experience delivering user-facing software through multiple release cycles."
        key,url=self._make_external_eligible_job(description)
        pending={"classification":"needs_review","match_score":68,"missing_information":["Confirm the requested release-cycle example."],
            "technical_qualifications":[{"requirement":"multiple release cycles","category":"product_release_cycles",
                "requiredness":"required","status":"needs_review","missing_question":"Technical evidence (release cycles): Give the sequence and your contribution."}],
            "required_skills":[],"required_experience":[],"education_requirements":[]}
        with app_agent.connect() as db:
            db.execute("UPDATE job_match_results SET classification='needs_review',result_json=? WHERE user_id=? AND job_key=?",
                (json.dumps(pending),USER,key))
        plan=app_agent.preparation_authorization_plan(key)
        self.assertEqual(plan["eligibility"],"needs_review")
        self.assertFalse(plan["preparation_ready"])
        self.assertFalse(plan["authorization_current"])
        self.assertEqual(plan["available_documents"],[{"type":"resume","label":"Active resume"}])
        self.assertTrue(plan["missing_requirements"])
        self.assertTrue(any("release-cycle" in item.casefold() for item in plan["missing_applicant_information"]))
        self.assertFalse(plan["live_submission_enabled"])
        self.assertEqual(plan["attempt_history_count"],0)
        with app_agent.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM application_preparation_authorizations WHERE user_id=? AND job_key=?",(USER,key)).fetchone()[0],0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM applications WHERE user_id=? AND job_key=?",(USER,key)).fetchone()[0],0)

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

    def test_phase134_dashboard_has_readiness_recovery_and_stable_grouped_question_ui(self):
        page=app_agent.applications_page().body.decode("utf-8")
        for text in ("Eligibility freshness","Authorization freshness","Required questions","Documents","Human action",
                     "Browser compatibility","Resume Dry Run","Retry Dry Run","retry_allowed","resume_allowed",
                     "application-readiness","max-width:420px","phase134RefreshInFlight","phase134AppSignatures",
                     "phase134QuestionSignature","Saving and verifying answer and reuse scope",
                     "persisted.existing_answers","No dry run was resumed","phase134QuestionDirty",
                     "if(!force&&phase134QuestionDirty)return","setTimeout(()=>refresh(),0);setInterval(()=>refresh(),15000)",
                     "answer:answer.value.trim()","dataset.saving==='true'","manual_employer_action",
                     "question-job","job-select-list","This acknowledgment or verification is specific to the employer",
                     "entered form values are not carried over","Open official employer form","Save manual outcome"):
            self.assertIn(text,page)
        self.assertIn("details[open]",page)
        self.assertIn("document.activeElement?.closest('#question-attention')",page)
        self.assertIn("Historical submission record",page)
        self.assertNotIn("submitted:'Submitted'",page)
        self.assertIn("Review recovery in Application history",page)
        self.assertNotIn("retryButton=node('button','primary','Retry dry run')",page)

    def test_manual_outcome_is_confirmed_read_back_and_never_changes_submission_state(self):
        resume,_,_=app_agent._candidate()
        job={"job_key":"local-mock:manual-outcome","company":"Synthetic Employer","title":"Synthetic Engineer",
             "application_url":"http://127.0.0.1:8000/mock-apps/simple"}
        app_id,_,_=app_agent._create(job,resume)
        app_agent._update(app_id,"failed",{"message":"Synthetic failure","submission_performed":False})
        before=next(item for item in app_agent.list_applications() if item["id"]==app_id)
        with self.assertRaises(HTTPException) as missing_confirmation:
            app_agent.record_manual_submission_outcome(app_id,app_agent.ManualSubmissionOutcomeRequest(
                outcome="submitted_manually",confirmed=False,event_id="event-1"))
        self.assertEqual(missing_confirmation.exception.status_code,422)
        result=app_agent.record_manual_submission_outcome(app_id,app_agent.ManualSubmissionOutcomeRequest(
            outcome="submitted_manually",confirmed=True,event_id="event-1"))
        self.assertTrue(result["verified_by_readback"])
        self.assertFalse(result["submission_enabled"])
        repeated=app_agent.record_manual_submission_outcome(app_id,app_agent.ManualSubmissionOutcomeRequest(
            outcome="submitted_manually",confirmed=True,event_id="event-1"))
        self.assertTrue(repeated["verified_by_readback"])
        with self.assertRaises(HTTPException) as conflicting_event:
            app_agent.record_manual_submission_outcome(app_id,app_agent.ManualSubmissionOutcomeRequest(
                outcome="not_submitted",confirmed=True,event_id="event-1"))
        self.assertEqual(conflicting_event.exception.status_code,409)
        with app_agent.connect() as db:
            stored=db.execute("SELECT state,submitted_at,diagnostic_json FROM applications WHERE id=?",(app_id,)).fetchone()
            attempt=db.execute("SELECT diagnostic_json FROM application_attempts WHERE application_id=?",(app_id,)).fetchone()
            event=db.execute("SELECT outcome,confirmed_by_applicant,attempt_number FROM manual_submission_events WHERE event_id='event-1'").fetchone()
        self.assertEqual(stored["state"],before["state"])
        self.assertEqual(stored["submitted_at"],before.get("submitted_at"))
        self.assertFalse(json.loads(stored["diagnostic_json"])["submission_performed"])
        self.assertEqual(event["outcome"],"submitted_manually")
        self.assertTrue(event["confirmed_by_applicant"])
        self.assertEqual(event["attempt_number"],1)
        readiness=app_agent._readiness_for_application({**before,"diagnostic":json.loads(stored["diagnostic_json"])},app_agent._candidate())
        self.assertTrue(readiness["manual_completion_reported"])
        self.assertFalse(readiness["retry_allowed"])

    def test_manual_outcome_survives_dry_run_diagnostic_updates(self):
        resume,_,_=app_agent._candidate()
        job={"job_key":"local-mock:manual-preserved","company":"Synthetic Employer","title":"Synthetic Engineer",
             "application_url":"http://127.0.0.1:8000/mock-apps/simple"}
        app_id,_,_=app_agent._create(job,resume)
        app_agent.record_manual_submission_outcome(app_id,app_agent.ManualSubmissionOutcomeRequest(
            outcome="needs_follow_up",confirmed=True,event_id="event-preserve"))
        app_agent._update(app_id,"opening",{"phase":"navigation","submission_performed":False})
        item=next(item for item in app_agent.list_applications() if item["id"]==app_id)
        self.assertEqual(item["diagnostic"]["manual_submission_outcome"],"needs_follow_up")
        self.assertEqual(len(item["manual_submission_history"]),1)

    def test_manual_outcome_history_survives_a_new_retry_attempt(self):
        resume,_,_=app_agent._candidate()
        job={"job_key":"local-mock:manual-retry","company":"Synthetic Employer","title":"Synthetic Engineer",
             "application_url":"http://127.0.0.1:8000/mock-apps/simple"}
        app_id,_,_=app_agent._create(job,resume)
        app_agent._update(app_id,"failed",{"message":"Synthetic prior failure","submission_performed":False})
        app_agent.record_manual_submission_outcome(app_id,app_agent.ManualSubmissionOutcomeRequest(
            outcome="needs_follow_up",confirmed=True,event_id="event-retry"))
        retried=app_agent.retry_one(app_id)
        self.assertTrue(retried["new_attempt_created"])
        item=next(item for item in app_agent.list_applications() if item["id"]==app_id)
        self.assertEqual(item["diagnostic"]["manual_submission_outcome"],"needs_follow_up")
        attempts=app_agent.list_application_attempts(app_id)
        self.assertEqual(len(attempts),2)
        self.assertTrue(any(event["event_id"]=="event-retry" for event in attempts[1]["manual_outcomes"]))

    def test_safe_resume_reuses_existing_attempt_and_never_calls_retry(self):
        resume,_,_=app_agent._candidate()
        job={"job_key":"local-mock:safe-resume","company":"BabyStepsAI local test","title":"Synthetic Engineer",
             "application_url":"http://127.0.0.1:8000/mock-apps/simple"}
        app_id,_,_=app_agent._create(job,resume)
        app_agent._update(app_id,"paused",{})
        before=len(app_agent.list_application_attempts(app_id))
        result=app_agent.resume_one(app_id)
        self.assertTrue(result["resumed_existing_attempt"])
        self.assertFalse(result["new_attempt_created"])
        self.assertEqual(len(app_agent.list_application_attempts(app_id)),before)
        self.assertEqual(result["status"],"queued")

    def test_application_api_readiness_is_derived_from_saved_attempt(self):
        resume,_,_=app_agent._candidate()
        job={"job_key":"local-mock:readiness","company":"BabyStepsAI local test","title":"Synthetic Engineer",
             "application_url":"http://127.0.0.1:8000/mock-apps/simple"}
        app_id,_,_=app_agent._create(job,resume)
        app_agent._update(app_id,"needs_user_input",{"unanswered_fields":["When can you start?"]})
        item=next(row for row in app_agent.list_applications() if row["id"]==app_id)
        self.assertEqual(item["status_label"],"Needs Your Input")
        self.assertEqual(item["readiness"]["questions"]["remaining"],1)
        self.assertFalse(item["readiness"]["resume_allowed"])
        self.assertFalse(item["readiness"]["retry_allowed"])
        self.assertIn("Resolve",item["readiness"]["retry_reason"])

    def test_application_recovery_readiness_distinguishes_resume_retry_and_human_actions(self):
        resume,candidate,answers=app_agent._candidate()
        local_url="http://127.0.0.1:8000/mock-apps/simple"
        common={"id":"recovery-test","job_key":"local-mock:recovery-test","resume_id":resume["id"],
            "application_url":local_url,"diagnostic":{}}
        paused=app_agent._readiness_for_application({**common,"state":"paused"},(resume,candidate,answers))
        self.assertTrue(paused["resume_allowed"]);self.assertFalse(paused["retry_allowed"])
        progressed=app_agent._readiness_for_application({**common,"state":"paused","diagnostic":{"phase":"form_filling","steps":1,"filled_fields":[{"field":"Email"}]}},(resume,candidate,answers))
        self.assertFalse(progressed["resume_allowed"]);self.assertTrue(progressed["retry_allowed"])
        self.assertIn("new attempt",progressed["resume_reason"])
        interrupted=app_agent._readiness_for_application({**common,"state":"paused","diagnostic":{"interrupted":True}},(resume,candidate,answers))
        self.assertFalse(interrupted["resume_allowed"]);self.assertTrue(interrupted["retry_allowed"])
        unresolved=app_agent._readiness_for_application({**common,"state":"needs_user_input","diagnostic":{"unanswered_fields":["Country is required."]}},(resume,candidate,answers))
        self.assertFalse(unresolved["resume_allowed"]);self.assertFalse(unresolved["retry_allowed"])
        unsupported=app_agent._readiness_for_application({**common,"state":"unsupported"},(resume,candidate,answers))
        self.assertFalse(unsupported["retry_allowed"])

    def test_unsupported_application_cannot_be_retried_without_new_compatibility_evidence(self):
        resume,_,_=app_agent._candidate()
        app_id,_,_=app_agent._create({"job_key":"unsupported-no-retry","company":"Test","title":"Role",
            "application_url":"http://127.0.0.1:8000/mock-apps/simple"},resume)
        app_agent._update(app_id,"unsupported",{"message":"Unsupported control"})
        with self.assertRaises(HTTPException) as blocked:app_agent.retry_one(app_id)
        self.assertEqual(blocked.exception.status_code,409)
        self.assertEqual(len(app_agent.list_application_attempts(app_id)),1)

    def test_dashboard_exposes_targeted_evidence_review_and_no_ai_reassessment(self):
        page=app_agent.APPLICATIONS_PAGE
        self.assertIn("Resolve eligibility evidence review",page)
        self.assertIn("employment type remains unknown",page)
        self.assertIn("AI-authored diagnostics (not posting evidence)",page)
        self.assertIn("Reassess saved evidence without AI",page)
        self.assertIn("/resolve-evidence-review",page)
        self.assertIn("/deterministic-reassessment",page)
        self.assertIn("phase133HasActiveInteraction",page)
        self.assertIn("phase133SavedJobsSignature",page)
        self.assertIn("phase133SavedJobsEpoch",page)
        self.assertIn("phase133RestoreSavedJobState",page)
        self.assertIn("if(phase133SavedJobsRendering)return",page)
        self.assertGreaterEqual(page.count("epoch!==phase133SavedJobsEpoch"),2)
        self.assertIn("signature===phase133SavedJobsSignature",page)
        self.assertIn("function readableApiError",page)
        self.assertIn("details[open]",page)
        self.assertIn("Confirm review",page)
        self.assertIn("targetedReviewRequired",page)
        self.assertIn("persisted_verified",page)
        self.assertNotIn("setInterval(refresh,3000)",page)

    def test_dashboard_backend_exposes_stale_review_blocker_and_saved_review_status(self):
        source=inspect.getsource(app_agent.list_saved_application_jobs)
        self.assertIn("The saved assessment is stale. Reassess saved evidence without AI before continuing.",source)
        self.assertIn('"targeted_evidence_review"',source)

    def test_review_assessment_separates_missing_mandatory_evidence_from_conflicts(self):
        result={"classification":"needs_review","required_experience":[
            {"requirement":"3+ years of experience in an industry role","status":"unclear","candidate_evidence":[]}],
            "experience_level_assessment":"The candidate has 3+ years of experience, which is relevant to the job."}
        assessment=app_agent._eligibility_review_assessment({"employment_type":"full-time","work_arrangement":"Remote"},result)
        self.assertIn("3+ years of experience in an industry role",assessment["mandatory_unresolved"])
        self.assertFalse(assessment["confirmed_conflicts"])
        self.assertTrue(any(item["category"]=="unsupported_ai_evidence" for item in assessment["unsupported_ai_evidence"]))
        self.assertIn("mandatory evidence",assessment["next_action"].casefold())

    def test_review_assessment_keeps_confirmed_mismatch_blocking(self):
        result={"classification":"not_eligible","required_skills":[
            {"requirement":"Required Rust experience","status":"confirmed_mismatch","candidate_evidence":["Confirmed profile evidence"]}]}
        assessment=app_agent._eligibility_review_assessment({"employment_type":"full-time","work_arrangement":"Remote"},result)
        self.assertEqual(assessment["confirmed_conflicts"],["Required Rust experience"])
        self.assertTrue(assessment["preparation_blocked"])
        self.assertIn("cannot override",assessment["next_action"])

    def test_review_assessment_keeps_employer_unknowns_separate_and_nonblocking(self):
        result={"classification":"needs_review","preferred_experience":[{"requirement":"Internship preferred","status":"preferred"}]}
        assessment=app_agent._eligibility_review_assessment({"employment_type":"","work_arrangement":""},result)
        self.assertFalse(assessment["mandatory_unresolved"])
        self.assertFalse(assessment["confirmed_conflicts"])
        self.assertEqual(len(assessment["employer_uncertainties"]),2)
        self.assertEqual(assessment["preferred_qualifications"][0]["requirement"],"Internship preferred")
        self.assertTrue(assessment["employer_uncertainties"])

    def test_review_assessment_flags_ai_absence_claim_against_confirmed_projects(self):
        result={"classification":"needs_review","experience_level_assessment":
            "The candidate profile does not mention any relevant projects and does not have experience with Haskell or other programming languages."}
        assessment=app_agent._eligibility_review_assessment({"employment_type":"full-time","work_arrangement":"Remote"},result,
            confirmed_projects_available=True)
        diagnostics=" ".join(item["explanation"] for item in assessment["unsupported_ai_evidence"])
        self.assertIn("confirmed project evidence",diagnostics)
        self.assertIn("does not establish Haskell proficiency",diagnostics)
        self.assertFalse(assessment["confirmed_conflicts"])

    def test_experience_evidence_question_is_specific_and_requires_dated_employment(self):
        question=app_agent._experience_requirement_question("3+ years of experience",
            "It's important that you have 3+ years of experience in an industry role. Programming experience required.")
        self.assertIn("3+ years of experience in an industry role",question)
        self.assertNotIn("Programming experience",question)
        self.assertIn("employer, role, and dates",question)
        self.assertIn("Projects and coursework do not count",question)

    def test_grouped_mandatory_experience_questions_merge_only_equivalent_requirements(self):
        stamp=matcher.now();engine=matcher.matching_engine_version()
        requirements=[("deepgram-a","Deepgram","Backend Engineer A","3+ years of experience",
            "You need 3+ years of experience in an industry role."),
            ("deepgram-b","Deepgram","Backend Engineer B","3+ years of experience",
            "You need 3+ years of experience in an industry role."),
            ("other-role","Other Employer","Engineer","5+ years of experience",
            "You need 5+ years of experience in a professional role.")]
        with app_agent.connect() as db:
            opening_cols={row[1] for row in db.execute("PRAGMA table_info(discovered_openings)")}
            if "availability_status" not in opening_cols:db.execute("ALTER TABLE discovered_openings ADD COLUMN availability_status TEXT DEFAULT 'current'")
            if "last_verified_at" not in opening_cols:db.execute("ALTER TABLE discovered_openings ADD COLUMN last_verified_at TEXT")
            for key,company,title,requirement,description in requirements:
                db.execute("INSERT INTO discovered_openings(user_id,job_key,company,title,location,url,discovered_at,description,availability_status,last_verified_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (USER,key,company,title,"United States","https://example.test/"+key,stamp,description,"current",stamp))
                result={"classification":"needs_review","required_experience":[{"requirement":requirement,"status":"unclear","candidate_evidence":[]}],"technical_qualifications":[]}
                db.execute("""INSERT INTO job_match_results(user_id,job_key,resume_id,status,classification,match_score,result_json,
                    created_at,updated_at,matching_model_version,freshness_status) VALUES(?,?,?,'completed','needs_review',55,?,?,?,?,?)""",
                    (USER,key,"r1",json.dumps(result),stamp,stamp,engine,"current"))
            db.execute("INSERT INTO discovered_openings(user_id,job_key,company,title,location,url,discovered_at,description,country,availability_status,last_verified_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (USER,"mytos-uk","Mytos","Junior Software Engineer","London","https://jobs.example/uk",stamp,
                 "Build software in the London office.","GB","current",stamp))
            uk_result={"classification":"needs_review","missing_information":[
                "Work authorization for United Kingdom is not confirmed; confirm whether you are authorized there or would require employer sponsorship."],
                "required_experience":[],"technical_qualifications":[]}
            db.execute("""INSERT INTO job_match_results(user_id,job_key,resume_id,status,classification,match_score,result_json,
                created_at,updated_at,matching_model_version,freshness_status) VALUES(?,?,?,'completed','needs_review',55,?,?,?,?,?)""",
                (USER,"mytos-uk","r1",json.dumps(uk_result),stamp,stamp,engine,"current"))
        groups=app_agent.grouped_application_questions()["eligibility_groups"]
        grouped=next(group for group in groups if len(group["jobs"])==2)
        self.assertEqual({job["job_key"] for job in grouped["jobs"]},{"deepgram-a","deepgram-b"})
        self.assertEqual(grouped["expected_answer_type"],"confirmed dated employment history")
        self.assertIn("a yes/no response is not sufficient",grouped["eligibility_effect"])
        self.assertTrue(any(len(group["jobs"])==1 and group["jobs"][0]["job_key"]=="other-role" for group in groups))
        country_group=next(group for group in groups if group["canonical_category"]=="work_authorization_country")
        self.assertEqual(country_group["context"],"united kingdom")
        self.assertTrue(country_group["sensitive"])
        self.assertEqual(country_group["reuse_scope"],"country_specific_profile_confirmation")
        self.assertEqual(country_group["jobs"][0]["job_key"],"mytos-uk")

    def test_dashboard_exposes_blocker_categories_and_grouped_mandatory_questions(self):
        page=app_agent.APPLICATIONS_PAGE
        self.assertIn("Eligibility evidence review",page)
        self.assertIn("eligibility_groups",page)
        self.assertIn("Review dated employment evidence in Candidate Profile",page)
        self.assertIn("Do not answer yes/no here",page)
        self.assertIn("Sensitive · country-specific · applicant confirmation required",page)
        self.assertIn("Confirm country-specific work authorization in Candidate Profile",page)
        self.assertIn("Potential Match — Further Review Required",page)
        self.assertIn("Eligible for Preparation Review",page)
        self.assertIn("Confirmed Mandatory Conflict",page)
        self.assertIn("Ready for Review",page)
        self.assertIn("Add earliest available date in Candidate Profile",page)
        self.assertIn("preparation_plan_available",page)
        self.assertIn("View Preparation Readiness",page)
        self.assertIn("Read-only preparation readiness plan",page)

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
        settings={"__settings":{"allow_auto_use_confirmed_answers":True,"allow_confirmed_profile_fields":True}}
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
        saved_review=app_agent.review_saved_job_eligibility("saved-review",app_agent.EligibilityReviewRequest(reviewed=True))
        self.assertTrue(saved_review["reviewed"])
        self.assertTrue(saved_review["persisted_verified"])
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

    def test_targeted_evidence_review_resolves_only_acknowledged_non_mandatory_uncertainty(self):
        description=("Requirements: B.S. or M.S. in Computer Science or a related field (recently graduated or graduating "
            "Fall 2026/Spring 2027). Experience building web applications through personal projects, coursework, or internships. "
            "Ability to work on-site in our New York office. Nice to Haves: Prior internship experience in fast-paced environments. "
            "Our (FTE) benefits include health coverage and 401(k).")
        key,profile_data,prefs=self._save_evidence_review_fixture(description)
        request=self._evidence_review_request(key,profile_data,prefs)
        with patch.object(matcher,"active_candidate",return_value=("r1","user_reviewed",profile_data)), \
             patch.object(matcher,"preferences",return_value=prefs), \
             patch.object(matcher,"assess_job",return_value=(True,"",["Employment type is not provided in Greenhouse metadata."])), \
             patch.object(matcher,"analyze_with_ollama",side_effect=AssertionError("review must not call Ollama")):
            with self.assertRaises(HTTPException) as missing:
                app_agent.resolve_saved_job_evidence_review(key,app_agent.TargetedEligibilityReviewRequest(**{
                    **request.model_dump(),"accept_unspecified_employment_type":False}))
            self.assertEqual(missing.exception.status_code,422)
            self.assertIn("employment type remains unknown",missing.exception.detail)
            resolved=app_agent.resolve_saved_job_evidence_review(key,request)
            with app_agent.connect() as db:
                gate=app_agent._eligibility_gate(db,key,"r1")
            listed=next(item for item in app_agent.list_saved_application_jobs(q="Authentic")["results"] if item["job_key"]==key)
            self.assertEqual(gate[0],"potential_match")
            self.assertTrue(listed["targeted_evidence_review"]["recorded"])
            self.assertFalse(listed["preparation_allowed"])
            changed_profile={**profile_data,"graduation_date":"2025-06"}
            with patch.object(matcher,"active_candidate",return_value=("r1","user_reviewed",changed_profile)):
                with app_agent.connect() as db: stale_gate=app_agent._eligibility_gate(db,key,"r1")
            self.assertEqual(stale_gate[0],"not_analyzed")
        self.assertEqual(resolved["classification"],"potential_match")
        self.assertEqual(resolved["employment_type"],"unknown")
        self.assertFalse(resolved["application_preparation_authorized"])
        self.assertTrue(resolved["persisted_verified"])
        with app_agent.connect() as db:
            saved=db.execute("SELECT classification,result_json FROM job_match_results WHERE user_id=? AND job_key=? AND resume_id='r1'",(USER,key)).fetchone()
            event=db.execute("SELECT decision,profile_fingerprint,preferences_fingerprint,job_fingerprint,matching_engine_version,details_json FROM eligibility_evidence_reviews WHERE user_id=? AND job_key=?",(USER,key)).fetchone()
            apps=db.execute("SELECT COUNT(*) FROM applications WHERE user_id=? AND job_key=?",(USER,key)).fetchone()[0]
            history=db.execute("SELECT COUNT(*) FROM job_match_result_history WHERE user_id=? AND job_key=?",(USER,key)).fetchone()[0]
        data=json.loads(saved["result_json"])
        self.assertEqual(saved["classification"],"potential_match")
        self.assertIn("does not specify employment type",data["employment_type_assessment"])
        self.assertTrue(data["ai_evidence_diagnostics"])
        self.assertEqual(event["decision"],"accepted_non_mandatory_uncertainty")
        self.assertEqual(event["matching_engine_version"],matcher.MATCHING_ENGINE_VERSION)
        self.assertEqual(apps,0)
        self.assertEqual(history,1)

    def test_role_seniority_uncertainty_requires_job_specific_ack_and_verified_source(self):
        from fastapi import HTTPException
        description=("Requirements: B.S. in Computer Science or equivalent. Build and maintain software using "
            "Haskell and related tools. The posting does not specify a seniority level.")
        key,profile_data,prefs=self._save_evidence_review_fixture(description)
        with app_agent.connect() as db:
            columns={row[1] for row in db.execute("PRAGMA table_info(discovered_openings)")}
            if "availability_status" not in columns:db.execute("ALTER TABLE discovered_openings ADD COLUMN availability_status TEXT NOT NULL DEFAULT ''")
            db.execute("UPDATE discovered_openings SET title='Software Engineer',employment_type='Full-time',active=1,availability_status='current',last_verified_at=? WHERE user_id=? AND job_key=?",
                (matcher.now(),USER,key))
            opening=dict(db.execute("SELECT * FROM discovered_openings WHERE user_id=? AND job_key=?",(USER,key)).fetchone())
            row=db.execute("SELECT result_json FROM job_match_results WHERE user_id=? AND job_key=? AND resume_id='r1'",(USER,key)).fetchone()
            result=matcher.MatchResult.model_validate_json(row["result_json"])
            result.missing_information=["Experience level is not clear from the posting title."]
            db.execute("UPDATE job_match_results SET result_json=?,job_fingerprint=? WHERE user_id=? AND job_key=? AND resume_id='r1'",
                (result.model_dump_json(),matcher.job_fingerprint(opening),USER,key))
        request=self._evidence_review_request(key,profile_data,prefs,accept_unspecified_employment_type=False,
            accept_unclear_role_seniority=False)
        review_result=matcher.MatchResult.model_validate_json(result.model_dump_json())
        with patch.object(matcher,"active_candidate",return_value=("r1","user_reviewed",profile_data)), \
             patch.object(matcher,"preferences",return_value=prefs), \
             patch.object(matcher,"apply_safety_rules",return_value=review_result), \
             patch.object(matcher,"_verified_saved_employer_source",return_value=True), \
             patch.object(matcher,"analyze_with_ollama",side_effect=AssertionError("review must not call AI")):
            with self.assertRaises(HTTPException) as missing:
                app_agent.resolve_saved_job_evidence_review(key,request)
            self.assertEqual(missing.exception.status_code,422)
            self.assertIn("seniority still unknown",missing.exception.detail)
            request=app_agent.TargetedEligibilityReviewRequest(**{**request.model_dump(),"accept_unclear_role_seniority":True})
            with patch.object(matcher,"_verified_saved_employer_source",return_value=False):
                with self.assertRaises(HTTPException) as unverified:
                    app_agent.resolve_saved_job_evidence_review(key,request)
            self.assertEqual(unverified.exception.status_code,409)
            self.assertIn("healthy official employer source",unverified.exception.detail)
            resolved=app_agent.resolve_saved_job_evidence_review(key,request)
        self.assertEqual(resolved["classification"],"potential_match")
        self.assertEqual(resolved["role_seniority"],"unknown")
        self.assertFalse(resolved["application_preparation_authorized"])
        with app_agent.connect() as db:
            saved=json.loads(db.execute("SELECT result_json FROM job_match_results WHERE user_id=? AND job_key=? AND resume_id='r1'",(USER,key)).fetchone()[0])
            review=json.loads(db.execute("SELECT details_json FROM eligibility_evidence_reviews WHERE user_id=? AND job_key=? ORDER BY review_id DESC LIMIT 1",(USER,key)).fetchone()[0])
        self.assertIn("role seniority remains unknown",saved["explanation"])
        self.assertTrue(review["accepted_unclear_role_seniority"])

    def test_needs_review_preparation_reason_does_not_hide_review_blocker_with_authorization(self):
        source=inspect.getsource(app_agent.list_saved_application_jobs)
        self.assertIn("Confirm the missing mandatory evidence in Candidate Profile",source)
        self.assertIn("Review the employer-side uncertainty for this job",source)

    def test_targeted_review_rejects_stale_fingerprints(self):
        key,profile_data,prefs=self._save_evidence_review_fixture("Requirements: a relevant degree.")
        request=self._evidence_review_request(key,profile_data,prefs,profile_fingerprint="f"*64)
        with patch.object(matcher,"active_candidate",return_value=("r1","user_reviewed",profile_data)), \
             patch.object(matcher,"preferences",return_value=prefs):
            with self.assertRaises(HTTPException) as caught:
                app_agent.resolve_saved_job_evidence_review(key,request)
        self.assertEqual(caught.exception.status_code,409)
        self.assertIn("stale",caught.exception.detail)

    def test_targeted_review_cannot_resolve_unmapped_mandatory_posting_items(self):
        description=("<p><strong>Requirements</strong></p><ul>"
            "<li>B.S. or M.S. in Computer Science or a related field (recently graduated or graduating Fall 2026/Spring 2027)</li>"
            "<li>Experience building web applications (personal projects, coursework, or internships)</li>"
            "<li>Comfort working across the stack; curious about frontend and backend</li>"
            "<li>Strong fundamentals in data structures, algorithms, and software design</li>"
            "<li>Ability to work on-site in our New York office</li></ul>")
        key,profile_data,prefs=self._save_evidence_review_fixture(description)
        request=self._evidence_review_request(key,profile_data,prefs)
        with app_agent.connect() as db:
            result=json.loads(db.execute(
                "SELECT result_json FROM job_match_results WHERE user_id=? AND job_key=?",(USER,key)).fetchone()[0])
        unmapped=app_agent._unmapped_required_posting_items(description,result)
        self.assertEqual(len(unmapped),4)
        self.assertTrue(any("web applications" in item for item in unmapped))
        with patch.object(matcher,"active_candidate",return_value=("r1","user_reviewed",profile_data)), \
             patch.object(matcher,"preferences",return_value=prefs):
            with self.assertRaises(HTTPException) as caught:
                app_agent.resolve_saved_job_evidence_review(key,request)
        self.assertEqual(caught.exception.status_code,409)
        self.assertIn("mandatory fact remains unresolved",caught.exception.detail)
        with app_agent.connect() as db:
            saved=db.execute("SELECT classification FROM job_match_results WHERE user_id=? AND job_key=?",(USER,key)).fetchone()
            review_count=db.execute("SELECT COUNT(*) FROM eligibility_evidence_reviews WHERE user_id=? AND job_key=?",(USER,key)).fetchone()[0]
            application_count=db.execute("SELECT COUNT(*) FROM applications WHERE user_id=? AND job_key=?",(USER,key)).fetchone()[0]
        self.assertEqual(saved["classification"],"needs_review")
        self.assertEqual(review_count,0)
        self.assertEqual(application_count,0)

    def test_flattened_ashby_new_grad_requirements_block_until_assessed(self):
        description=("About the role\nBuild and ship full-stack product features.\n"
            "What You'll Bring\nA product builder able to deliver user-facing software at scale.\n"
            "Full-stack fluency with React, TypeScript, Python, databases, and cloud systems.\n"
            "AI experience with LLM APIs, embeddings, and RAG is a nice-to-have.\n"
            "What You'll Do\nPartner with customers to deliver high-quality features.\n"
            "We are hiring new graduates available to start between winter 2026 and summer 2027.")
        unmapped=app_agent._unmapped_required_posting_items(description,{})
        self.assertEqual(len(unmapped),3)
        self.assertTrue(any("product builder" in item for item in unmapped))
        self.assertTrue(any("available to start" in item for item in unmapped))
        self.assertFalse(any("nice-to-have" in item for item in unmapped))
        self.assertFalse(any("Partner with customers" in item for item in unmapped))

    def test_start_window_is_considered_assessed_only_with_explicit_availability_assessment(self):
        description="We need to hire new graduates available to start between winter of 2026 and summer of 2027."
        missing=app_agent._unmapped_required_posting_items(description,{})
        self.assertTrue(any("available to start" in item for item in missing))
        assessed={"availability_assessment":{"status":"needs_review","requirement":"available to start between winter of 2026 and summer of 2027"}}
        self.assertEqual(app_agent._unmapped_required_posting_items(description,assessed),[])

    def test_technical_evidence_answer_requires_specific_confirmation_and_reassesses_without_ai(self):
        key,profile_data,prefs=self._save_evidence_review_fixture(
            "Requirements: Strong fundamentals in data structures, algorithms, and software design.")
        question=("Technical evidence: Have you completed coursework or implemented projects covering data structures, "
                  "algorithms, and software design? Name the courses or describe the specific work.")
        with app_agent.connect() as db:
            opening=dict(db.execute("SELECT * FROM discovered_openings WHERE user_id=? AND job_key=?",(USER,key)).fetchone())
            result=json.loads(db.execute("SELECT result_json FROM job_match_results WHERE user_id=? AND job_key=?",(USER,key)).fetchone()[0])
            result["technical_qualifications"]=[{"requirement":"Strong fundamentals in data structures, algorithms, and software design",
                "category":"data_structures_algorithms_design","requiredness":"required","status":"needs_review",
                "missing_question":question,"candidate_evidence":[],"posting_evidence":["Strong fundamentals in data structures, algorithms, and software design"]}]
            result["missing_information"]=[question]
            db.execute("UPDATE discovered_openings SET description=? WHERE user_id=? AND job_key=?",
                ("Requirements: Strong fundamentals in data structures, algorithms, and software design.",USER,key))
            opening=dict(db.execute("SELECT * FROM discovered_openings WHERE user_id=? AND job_key=?",(USER,key)).fetchone())
            db.execute("UPDATE job_match_results SET result_json=?,job_fingerprint=? WHERE user_id=? AND job_key=?",
                (json.dumps(result),matcher.job_fingerprint(opening),USER,key))
        request=app_agent.TechnicalEvidenceAnswerRequest(question=question,
            answer="In coursework I implemented graph traversal algorithms and documented their design tradeoffs.",
            job_keys=[key],job_key=key,reuse_scope="this_job",confirmed=False)
        with patch.object(app_agent,"_candidate",return_value=({"id":"r1"},profile_data,{})):
            with self.assertRaises(HTTPException) as unconfirmed:
                app_agent.save_technical_evidence_answer(request)
            self.assertEqual(unconfirmed.exception.status_code,422)
            with self.assertRaises(HTTPException) as yes_no:
                app_agent.save_technical_evidence_answer(request.model_copy(update={"answer":"Yes, I have completed these requirements."*2,"confirmed":True}))
            self.assertEqual(yes_no.exception.status_code,422)
            with patch.object(matcher,"reassess_saved_match_result",return_value={"classification":"needs_review",
                    "match":{"technical_qualifications":[{"candidate_evidence":[{"source_ref":"application_answers:custom:technical_dsa"}]}]}}) as reassess:
                saved=app_agent.save_technical_evidence_answer(request.model_copy(update={"confirmed":True}))
        self.assertTrue(saved["persisted_verified"])
        self.assertEqual(saved["reuse_scope"],"job_specific")
        self.assertFalse(saved["reassessed"][0]["ai_provider_called"])
        self.assertEqual(reassess.call_count,1)
        with app_agent.connect() as db:
            answer=db.execute("SELECT answer,confirmed,reuse_scope,scope_job_key FROM application_answers WHERE question_category='technical_qualification'").fetchone()
            app_count=db.execute("SELECT COUNT(*) FROM applications WHERE user_id=? AND job_key=?",(USER,key)).fetchone()[0]
            auth_count=db.execute("SELECT COUNT(*) FROM application_preparation_authorizations WHERE user_id=? AND job_key=? AND revoked_at IS NULL",(USER,key)).fetchone()[0]
        self.assertEqual(answer["answer"],"In coursework I implemented graph traversal algorithms and documented their design tradeoffs.")
        self.assertEqual((answer["confirmed"],answer["reuse_scope"],answer["scope_job_key"]),(1,"job_specific",key))
        self.assertEqual(app_count,0);self.assertEqual(auth_count,0)

    def test_targeted_review_cannot_override_missing_gpa_or_confirmed_degree_mismatch(self):
        base=("Requirements: B.S. or M.S. in Computer Science or related field (graduating Fall 2026/Spring 2027). "
              "Ability to work on-site in New York. ")
        for suffix,expected in (("Minimum GPA 3.7/4.0 required.","unresolved"),
                                ("Basic Qualifications: Master's degree in Computer Science required.","mismatch")):
            with self.subTest(kind=expected):
                key,profile_data,prefs=self._save_evidence_review_fixture(base+suffix+" Nice to Haves: Prior internship experience.")
                request=self._evidence_review_request(key,profile_data,prefs)
                with patch.object(matcher,"active_candidate",return_value=("r1","user_reviewed",profile_data)), \
                     patch.object(matcher,"preferences",return_value=prefs), \
                     patch.object(matcher,"assess_job",return_value=(True,"",["Employment type is not provided in Greenhouse metadata."])):
                    with self.assertRaises(HTTPException) as caught:
                        app_agent.resolve_saved_job_evidence_review(key,request)
                self.assertEqual(caught.exception.status_code,409)
                if expected=="mismatch":self.assertIn("mandatory mismatch",caught.exception.detail)
                else:self.assertIn("mandatory fact remains unresolved",caught.exception.detail)

    def test_generic_eligibility_review_cannot_bypass_targeted_unknown_type_review(self):
        key,profile_data,prefs=self._save_evidence_review_fixture("Requirements: a relevant degree.")
        with app_agent.connect() as db:
            db.execute("UPDATE job_match_results SET result_json=? WHERE user_id=? AND job_key=?",(json.dumps({"ai_evidence_diagnostics":["Model statement"]}),USER,key))
        with self.assertRaises(HTTPException) as caught:
            app_agent.review_saved_job_eligibility(key,app_agent.EligibilityReviewRequest(reviewed=True))
        self.assertEqual(caught.exception.status_code,409)
        self.assertIn("targeted evidence review",caught.exception.detail)

    def test_deterministic_reassessment_endpoint_never_calls_ai(self):
        key,_,_=self._save_evidence_review_fixture("Requirements: a relevant degree.")
        with app_agent.connect() as db:
            stamp=db.execute("SELECT updated_at FROM job_match_results WHERE user_id=? AND job_key=?",(USER,key)).fetchone()["updated_at"]
        result={"classification":"needs_review","matching_model_version":matcher.MATCHING_ENGINE_VERSION}
        with patch.object(matcher,"reassess_saved_match_result",return_value=result) as reassess, \
             patch.object(matcher,"analyze_with_ollama",side_effect=AssertionError("must not call Ollama")):
            response=app_agent.deterministic_reassess_saved_job(key,app_agent.DeterministicReassessmentRequest(match_updated_at=stamp))
        reassess.assert_called_once_with(key)
        self.assertFalse(response["ai_provider_called"])
        self.assertEqual(response["classification"],"needs_review")

    def test_potential_match_with_unresolved_mandatory_evidence_requires_review(self):
        from fastapi import HTTPException
        with app_agent.connect() as db:
            db.execute("UPDATE job_match_results SET result_json=? WHERE user_id=? AND job_key='saved-simple' AND resume_id='r1'",(json.dumps({"required_experience":[{"requirement":"3 years","status":"unclear"}]}),USER))
        with self.assertRaises(HTTPException): app_agent.prepare_saved_job(app_agent.DryRunRequest(job_key="saved-simple"))
        with self.assertRaises(HTTPException):app_agent.review_saved_job_eligibility("saved-simple",app_agent.EligibilityReviewRequest(reviewed=True))
        self.assertFalse(next(x for x in app_agent.list_saved_application_jobs()["results"] if x["job_key"]=="saved-simple")["preparation_allowed"])

    def test_missing_confirmed_gpa_blocks_dry_run_preparation(self):
        from fastapi import HTTPException
        result={"education_requirements":[{"requirement":"Minimum GPA 3.7/4.0","status":"unclear",
            "candidate_evidence":[]}],"missing_information":["Confirmed candidate GPA evidence is missing."]}
        with app_agent.connect() as db:
            db.execute("UPDATE job_match_results SET result_json=?,classification='needs_review' WHERE user_id=? AND job_key='saved-simple' AND resume_id='r1'",
                       (json.dumps(result),USER))
        self.assertTrue(app_agent._unresolved_mandatory(json.dumps(result),"Basic Qualifications: GPA minimum 3.7/4.0."))
        with self.assertRaises(HTTPException) as caught:
            app_agent.prepare_saved_job(app_agent.DryRunRequest(job_key="saved-simple"))
        self.assertEqual(caught.exception.status_code,409)
        self.assertFalse(next(x for x in app_agent.list_saved_application_jobs()["results"]
                              if x["job_key"]=="saved-simple")["preparation_allowed"])

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
        self.assertEqual(item["state"],"needs_user_input")
        self.assertFalse(item["diagnostic"]["submission_performed"])
        self.assertIn("Continue",item["diagnostic"]["submission_controls"])
        self.assertNotEqual(item["state"],"ready_for_review")
        self.assertEqual(self.client.last_page.submission_count,0)

    def test_checkbox_and_radio_answers_use_confirmed_profile_values(self):
        with app_agent.connect() as db:
            db.execute("CREATE TABLE career_preferences(user_id TEXT PRIMARY KEY,preferences_json TEXT NOT NULL)")
            db.execute("INSERT INTO career_preferences VALUES(?,?)",(USER,json.dumps({"relocation_preference":"anywhere","relocation_destinations":[]})))
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
        with self.assertRaises(HTTPException) as wrong_action:
            app_agent.resume_one(app_id)
        self.assertEqual(wrong_action.exception.status_code,409)
        app_agent.retry_one(app_id)
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
        self.assertIn("Upload controls",page)
        self.assertIn("accepted:",page)
        self.assertIn("Submission boundary controls",page)
        self.assertIn("form action path:",page)

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

    def test_reusable_answer_matching_obeys_global_and_job_specific_scope(self):
        candidate={}
        question={"label":"When can you start?","type":"text"}
        scoped={"__job_key":"saved-simple","custom:answer":{"question":"What is your earliest available start date?","answer":"June 1","confirmed":True,"auto_reuse":True,"sensitive":False,"reuse_scope":"job_specific","scope_job_key":"saved-simple"}}
        self.assertEqual(app_agent._answer(question,candidate,scoped)[0],"June 1")
        scoped["__job_key"]="another-job"
        self.assertIsNone(app_agent._answer(question,candidate,scoped)[0])
        scoped["custom:answer"]["reuse_scope"]="general_preference"
        self.assertEqual(app_agent._answer(question,candidate,scoped)[0],"June 1")
        legacy,source=app_agent._answer({"label":"What work arrangement do you prefer?"},
            {"work_arrangement_preferences":["Remote"]},{})
        self.assertIsNone(legacy); self.assertIsNone(source)

    def test_grouped_missing_question_answer_preserves_scope_and_never_resumes_apps(self):
        with app_agent.connect() as db:
            for app_id,job_key,company,title in (("a1","saved-simple","Example Co","First Engineer"),("a2","saved-second","Other Co","Second Engineer")):
                db.execute("INSERT INTO applications(id,user_id,resume_id,job_key,company,title,application_url,state,diagnostic_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'needs_user_input',?,?,?)",
                           (app_id,USER,"r1",job_key,company,title,"https://example.test/apply",json.dumps({"unanswered_fields":["When can you start?"]}),"2026-01-01","2026-01-01"))
        grouped=app_agent.grouped_application_questions()
        self.assertEqual(grouped["job_count"],2)
        self.assertEqual(len(grouped["groups"]),1)
        saved=app_agent.save_grouped_application_answer(app_agent.GroupedAnswerRequest(
            question="When can you start?",answer="  June 1  \n",application_ids=["a1","a2"],
            reuse_scope="general_preference",confirmed=True))
        self.assertTrue(saved["persisted_verified"])
        self.assertEqual(saved["reuse_scope"],"general_preference")
        with app_agent.connect() as db:
            states=[row[0] for row in db.execute("SELECT state FROM applications WHERE id IN ('a1','a2') ORDER BY id")]
            answer=db.execute("SELECT reuse_scope,answer,auto_reuse FROM application_answers WHERE question_category='availability'").fetchone()
        self.assertEqual(states,["needs_user_input","needs_user_input"])
        self.assertEqual(tuple(answer),("general_preference","June 1",1))
        refreshed=app_agent.grouped_application_questions()["groups"][0]
        self.assertEqual(refreshed["existing_answer"],"June 1")
        self.assertEqual(refreshed["existing_answer_scope"],"general_preference")

    def test_grouped_answer_typed_validation_covers_location_contact_date_url_and_boolean(self):
        invalid=[
            ("Location (City)","yes"),("Country of residence","no"),
            ("What is your start date?","sometime soon"),("LinkedIn URL","javascript:alert(1)"),
            ("Email address","not-an-email"),("Phone number","123"),
            ("Are you willing to work onsite?","sometimes"),
        ]
        for question,value in invalid:
            with self.subTest(question=question,value=value),self.assertRaises(HTTPException) as caught:
                app_agent._validate_grouped_answer(question,value)
            self.assertEqual(caught.exception.status_code,422)
        valid=[
            ("Location (City)","San Francisco"),("Country of residence","United States"),
            ("What is your start date?","2026-06-15"),("LinkedIn URL","https://linkedin.com/in/candidate"),
            ("Email address","person@example.com"),("Phone number","+1 (415) 555-0199"),
            ("Are you willing to work onsite?","No"),
        ]
        for question,value in valid:
            with self.subTest(question=question,value=value):
                self.assertEqual(app_agent._validate_grouped_answer(question,value),value)
        city_identity=app_agent.canonical_answer_identity("Location (City)")
        country_identity=app_agent.canonical_answer_identity("Location (Country)")
        self.assertNotEqual(city_identity[:2],country_identity[:2])

    def test_netic_technical_prompts_group_by_the_actual_requirement(self):
        prompts={
            "Technical evidence (release cycles): Which project did you carry through multiple cycles?":"product_release_cycles",
            "Technical evidence (full-stack): Describe your React and backend work.":"full_stack_fluency",
            "Technical evidence (customer work): Describe a customer or stakeholder need and product change.":"customer_product_translation",
            "Technical evidence (ownership): Describe an ambiguous feature you owned.":"founder_level_ownership",
            "Technical evidence (communication): Describe an architectural trade-off and business outcome.":"technical_business_communication",
        }
        for question,expected in prompts.items():
            category,context,_=profile.canonical_answer_identity(question)
            with self.subTest(question=question):
                self.assertEqual(category,"technical_qualification")
                self.assertEqual(context,expected)

    def test_invalid_historical_city_answer_is_never_reused_by_browser_filler(self):
        answers={"custom:old-city":{"question":"Location (City)*","answer":"yes","confirmed":True,
            "auto_reuse":True,"sensitive":False,"category":"location","context":"city",
            "reuse_scope":"matching_jobs","scope_job_key":""}}
        value,source=app_agent._answer({"label":"Location (City)*"},{},answers)
        self.assertIsNone(value);self.assertIsNone(source)

    def test_grouped_dashboard_marks_invalid_legacy_answer_for_correction(self):
        profile.save_application_answer("legacy-city",profile.AnswerUpdate(question="Location (City)*",
            answer="San Francisco",confirmed=True,reuse_scope="matching_jobs"))
        with app_agent.connect() as db:
            db.execute("UPDATE application_answers SET answer='yes' WHERE question_key='custom:legacy-city'")
            db.execute("INSERT INTO applications(id,user_id,resume_id,job_key,company,title,application_url,state,diagnostic_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'needs_user_input',?,?,?)",
                ("legacy-city-app",USER,"r1","saved-simple","Example Co","Engineer","https://example.test/apply",
                 json.dumps({"unanswered_fields":["Location (City)*"]}),"2026-10-29","2026-10-29"))
        _,_,candidate_answers=app_agent._candidate()
        self.assertFalse(candidate_answers["custom:legacy-city"]["answer_valid"])
        self.assertFalse(candidate_answers["custom:legacy-city"]["auto_reuse"])
        group=next(item for item in app_agent.grouped_application_questions()["groups"] if item["canonical_category"]=="location")
        self.assertTrue(group["requires_correction"])
        self.assertTrue(group["answer_conflict"])
        self.assertEqual(group["existing_answers"],[])
        self.assertEqual(group["existing_answer"],"")
        self.assertIn("not a yes/no response",group["invalid_saved_answers"][0]["validation_issue"])

    def test_employer_acknowledgments_are_manual_and_never_saved_as_reusable_answers(self):
        question="Please acknowledge that all information provided is accurate."
        with app_agent.connect() as db:
            db.execute("INSERT INTO applications(id,user_id,resume_id,job_key,company,title,application_url,state,diagnostic_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'needs_user_input',?,?,?)",
                       ("ack-1",USER,"r1","ack-job","Employer","Engineer","https://example.test/apply",json.dumps({"unanswered_fields":[question]}),"2026-01-01","2026-01-01"))
        group=app_agent.grouped_application_questions()["groups"][0]
        self.assertEqual(group["canonical_category"],"employer_confirmation")
        self.assertTrue(group["manual_employer_action"])
        self.assertIn("employer form",group["sensitivity_label"])
        with self.assertRaises(HTTPException) as rejected:
            app_agent.save_grouped_application_answer(app_agent.GroupedAnswerRequest(
                question=question,answer="I acknowledge",application_ids=["ack-1"],
                reuse_scope="this_job",job_key="ack-job",confirmed=True))
        self.assertEqual(rejected.exception.status_code,422)
        with app_agent.connect() as db:
            count=db.execute("SELECT COUNT(*) FROM application_answers WHERE question_category='employer_confirmation'").fetchone()[0]
        self.assertEqual(count,0)

    def test_grouped_answer_rejects_sensitive_global_reuse_and_conflicting_questions(self):
        from fastapi import HTTPException
        with app_agent.connect() as db:
            for app_id,job_key,question in (("a1","saved-simple","Are you legally authorized to work in the United States?"),("a2","saved-second","How much notice do you need?")):
                db.execute("INSERT INTO applications(id,user_id,resume_id,job_key,company,title,application_url,state,diagnostic_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'needs_user_input',?,?,?)",
                           (app_id,USER,"r1",job_key,"Example Co","Engineer","https://example.test/apply",json.dumps({"unanswered_fields":[question]}),"2026-01-01","2026-01-01"))
        with self.assertRaises(HTTPException) as sensitive:
            app_agent.save_grouped_application_answer(app_agent.GroupedAnswerRequest(question="Are you legally authorized to work in the United States?",answer="Yes",application_ids=["a1"],reuse_scope="matching_jobs",confirmed=True))
        self.assertEqual(sensitive.exception.status_code,422)
        scoped=app_agent.save_grouped_application_answer(app_agent.GroupedAnswerRequest(
            question="Are you legally authorized to work in the United States?",answer="Yes",application_ids=["a1"],
            reuse_scope="this_job",job_key="saved-simple",confirmed=True))
        self.assertEqual(scoped["reuse_scope"],"job_specific")
        with app_agent.connect() as db:
            sensitive_row=db.execute("SELECT answer,answer_encrypted,reuse_scope,scope_job_key,auto_reuse FROM application_answers WHERE sensitive=1 AND question_category='work_authorization'").fetchone()
        self.assertEqual(sensitive_row["answer"],""); self.assertTrue(sensitive_row["answer_encrypted"])
        self.assertEqual((sensitive_row["reuse_scope"],sensitive_row["scope_job_key"],sensitive_row["auto_reuse"]),("job_specific","saved-simple",0))
        sensitive_group=next(g for g in app_agent.grouped_application_questions()["groups"] if g["canonical_category"]=="work_authorization")
        self.assertTrue(sensitive_group["sensitive"]); self.assertEqual(sensitive_group["existing_answer"],"")
        self.assertTrue(sensitive_group["existing_answer_sensitive"])
        with self.assertRaises(HTTPException) as conflict:
            app_agent.save_grouped_application_answer(app_agent.GroupedAnswerRequest(question="When can you start?",answer="June 1",application_ids=["a1","a2"],reuse_scope="matching_jobs",confirmed=True))
        self.assertEqual(conflict.exception.status_code,409)

    def test_grouped_job_only_answer_is_bound_to_one_selected_job(self):
        with app_agent.connect() as db:
            for app_id,job_key in (("a1","saved-simple"),("a2","saved-second")):
                db.execute("INSERT INTO applications(id,user_id,resume_id,job_key,company,title,application_url,state,diagnostic_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'needs_user_input',?,?,?)",
                           (app_id,USER,"r1",job_key,"Example Co","Engineer","https://example.test/apply",json.dumps({"unanswered_fields":["How much notice do you need?"]}),"2026-01-01","2026-01-01"))
        saved=app_agent.save_grouped_application_answer(app_agent.GroupedAnswerRequest(
            question="How much notice do you need?",answer="Two weeks",application_ids=["a1","a2"],
            reuse_scope="this_job",job_key="saved-simple",confirmed=True))
        self.assertEqual(saved["reuse_scope"],"job_specific")
        self.assertEqual([job["job_key"] for job in saved["affected_jobs"]],["saved-simple"])
        with app_agent.connect() as db:
            answer=db.execute("SELECT reuse_scope,scope_job_key,auto_reuse FROM application_answers WHERE question_category='availability'").fetchone()
            states=[row[0] for row in db.execute("SELECT state FROM applications WHERE id IN ('a1','a2') ORDER BY id")]
        self.assertEqual(tuple(answer),("job_specific","saved-simple",1))
        self.assertEqual(states,["needs_user_input","needs_user_input"])

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
