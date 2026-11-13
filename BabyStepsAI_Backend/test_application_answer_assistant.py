import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import application_answer_assistant as assistant
import candidate_verification as profile
from job_search_agent import USER


class FakeResponse:
    def __init__(self, value): self.value=value
    def raise_for_status(self): pass
    def json(self): return {"response":json.dumps(self.value)}

class FakeGeminiResponse:
    status_code=200
    def __init__(self,value): self.value=value
    def raise_for_status(self): pass
    def json(self): return {"candidates":[{"content":{"parts":[{"text":json.dumps(self.value)}]}}]}

class FakeGroqResponse:
    status_code=200
    def __init__(self,value): self.value=value
    def raise_for_status(self): pass
    def json(self): return self.value


class ApplicationAnswerAssistantTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.db=Path(self.temp.name)/"answers.sqlite3"
        self.old_key_path=os.environ.get("BABYSTEPSAI_KEY_PATH"); os.environ["BABYSTEPSAI_KEY_PATH"]=str(Path(self.temp.name)/"encryption.key")
        self.old_assistant_db,self.old_profile_db=assistant.DB,profile.DB
        assistant.DB=self.db; profile.DB=self.db
        with sqlite3.connect(self.db) as db:
            db.execute("CREATE TABLE resumes(id TEXT PRIMARY KEY,user_id TEXT,stored_name TEXT,original_name TEXT,created_at TEXT,active INTEGER,extracted_text TEXT)")
            db.execute("INSERT INTO resumes VALUES('r1',?,'r1.pdf','resume.pdf','2026-01-01',1,'')",(USER,))
            db.execute("CREATE TABLE resume_profiles(resume_id TEXT PRIMARY KEY,status TEXT,profile_json TEXT,error TEXT,updated_at TEXT)")
            db.execute("INSERT INTO resume_profiles VALUES('r1','completed_unverified','{}',NULL,'2026-01-01')")
            db.execute("CREATE TABLE career_preferences(user_id TEXT PRIMARY KEY,preferences_json TEXT)")
            db.execute("INSERT INTO career_preferences VALUES(?,?)",(USER,json.dumps({"career_categories":["Software Engineering"],"instructions":"I enjoy backend systems"})))
            db.execute("CREATE TABLE discovered_openings(user_id TEXT,job_key TEXT,company TEXT,title TEXT,location TEXT,url TEXT,discovered_at TEXT,employment_type TEXT,location_status TEXT,location_reason TEXT,search_prompt TEXT,discovery_source TEXT,PRIMARY KEY(user_id,job_key))")
            db.execute("INSERT INTO discovered_openings VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(USER,"greenhouse:cloudflare:10","Example Systems","Backend Engineer","United States","https://example.test/job","2026-01-01","Full time","eligible","","backend","Company Careers Page"))
        profile.initialize(); assistant.initialize()
        profile.update_verification(profile.VerificationUpdate(values={
            "full_name":"Example Candidate","email":"private@example.test","work_authorization":"United States",
            "project_entries":[{"name":"Telemetry API","description":"Built a small telemetry service","technologies":["Python","FastAPI"],"accomplishments":["Added request aggregation"]}],
            "skills":["Python","FastAPI"]},confirmed_fields=["full_name","email","work_authorization","skills","project_entries:0"]))
        assistant.update_authorization(assistant.AuthorizationUpdate(allow_ai_drafting=True))

    def tearDown(self):
        assistant.DB=self.old_assistant_db; profile.DB=self.old_profile_db; self.temp.cleanup()
        if self.old_key_path is None: os.environ.pop("BABYSTEPSAI_KEY_PATH",None)
        else: os.environ["BABYSTEPSAI_KEY_PATH"]=self.old_key_path

    def test_authorization_is_versioned_and_submission_cannot_be_enabled(self):
        initial=assistant.get_authorization()
        self.assertEqual(initial["settings"]["live_submission_enabled"],False)
        updated=assistant.update_authorization(assistant.AuthorizationUpdate(
            allow_ai_drafting=True,allow_auto_use_confirmed_answers=True,max_applications_per_24_hours=7))
        self.assertEqual(updated["version"],initial["version"]+1)
        self.assertEqual(updated["settings"]["max_applications_per_24_hours"],7)
        self.assertFalse(updated["settings"]["live_submission_enabled"])
        with assistant.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM application_authorization_history WHERE user_id=?",(USER,)).fetchone()[0],3)

    def test_matching_provider_is_versioned_and_gemini_key_is_not_persisted(self):
        with patch.dict(os.environ,{"GEMINI_API_KEY":"optional-test-key"}):
            saved=assistant.update_authorization(assistant.AuthorizationUpdate(
                allow_ai_matching=True,matching_provider="gemini"))
            self.assertEqual(saved["settings"]["matching_provider"],"gemini")
            self.assertTrue(saved["settings"]["gemini_configured"])
            status=assistant.get_authorization()
        self.assertEqual(status["settings"]["matching_provider"],"gemini")
        self.assertTrue(status["settings"]["gemini_configured"])
        with assistant.connect() as db:
            stored=db.execute("SELECT settings_json FROM application_authorizations WHERE user_id=?",(USER,)).fetchone()[0]
        self.assertNotIn("optional-test-key",stored)
        self.assertNotIn("gemini_configured",stored)
        self.assertFalse(status["settings"]["live_submission_enabled"])

    def test_standing_policy_is_bounded_versioned_revocable_and_default_off(self):
        from fastapi import HTTPException
        self.assertFalse(assistant.get_authorization()["settings"]["standing_policy_enabled"])
        scope={
            "allow_automated_form_preparation":True,"allow_auto_use_confirmed_answers":True,
            "standing_policy_enabled":True,"standing_policy_categories":["Software Engineering"],
            "standing_policy_employment_types":["Full-time"],"standing_policy_countries":["United States"],
            "standing_policy_locations":["Austin"],
            "standing_policy_work_arrangements":["Remote"],"standing_policy_experience_levels":["New Graduate"],
            "standing_policy_daily_limit":2,"standing_policy_field_keys":["full_name","email","education","projects"],
            "standing_policy_document_types":["resume"],"standing_policy_reuse_confirmed_answers":True,
        }
        with patch("career_preferences.get_preferences",return_value={"categories":["Software Engineering"],"roles":["Software Engineer"]}):
            saved=assistant.update_authorization(assistant.AuthorizationUpdate(**scope))
        self.assertTrue(saved["settings"]["standing_policy_enabled"])
        self.assertTrue(saved["settings"]["allow_automatic_discovery"])
        self.assertTrue(saved["settings"]["allow_ai_matching"])
        self.assertTrue(saved["settings"]["allow_automated_form_preparation"])
        self.assertEqual(saved["settings"]["standing_policy_daily_limit"],2)
        self.assertTrue(saved["settings"]["standing_policy_preferences_fingerprint"])
        self.assertFalse(saved["settings"]["live_submission_enabled"])
        page=assistant.application_settings_page().body.decode()
        self.assertNotIn('name="allow_automatic_discovery"',page)
        self.assertNotIn('name="allow_ai_matching"',page)
        self.assertNotIn('name="allow_automated_form_preparation"',page)
        self.assertNotIn('name="allow_auto_use_confirmed_answers"',page)
        self.assertNotIn('name="allow_ai_drafting"',page)
        with patch("career_preferences.get_preferences",return_value={"categories":["Software Engineering"],"roles":["Software Engineer"]}):
            with self.assertRaises(HTTPException) as caught:
                assistant.update_authorization(assistant.AuthorizationUpdate(**{**scope,"standing_policy_field_keys":["work_authorization"]}))
        self.assertEqual(caught.exception.status_code,422)
        revoked=assistant.update_authorization(assistant.AuthorizationUpdate())
        self.assertFalse(revoked["settings"]["standing_policy_enabled"])
        self.assertGreater(revoked["version"],saved["version"])
        with assistant.connect() as db:
            history=db.execute("SELECT COUNT(*) FROM application_authorization_history WHERE user_id=?",(USER,)).fetchone()[0]
        self.assertGreaterEqual(history,4)

    def test_standing_policy_requires_complete_scope_and_separate_cloud_permission(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as caught:
            assistant.update_authorization(assistant.AuthorizationUpdate(
                allow_automated_form_preparation=True,standing_policy_enabled=True))
        self.assertEqual(caught.exception.status_code,422)

    def test_standing_policy_missing_location_returns_field_specific_422_without_saving(self):
        from fastapi import HTTPException
        scope={"standing_policy_enabled":True,"standing_policy_categories":["Software Development"],
            "standing_policy_employment_types":["Full-time"],"standing_policy_countries":["United States"],
            "standing_policy_work_arrangements":["Remote"],"standing_policy_experience_levels":["New Graduate"],
            "standing_policy_field_keys":["full_name","email"],"standing_policy_document_types":["resume"]}
        before=assistant.get_authorization()["version"]
        with patch("career_preferences.get_preferences",return_value={"categories":["Software Development"],"roles":["Software Engineer"]}):
            with self.assertRaises(HTTPException) as caught:
                assistant.update_authorization(assistant.AuthorizationUpdate(**scope))
        self.assertEqual(caught.exception.status_code,422)
        self.assertEqual(caught.exception.detail,{"errors":[{"field":"standing_policy_locations",
            "message":"Enter ‘Anywhere’ to allow any location within the selected countries, or choose at least one city or region.",
            "label":"Allowed locations"}]})
        self.assertEqual(assistant.get_authorization()["version"],before)

    def test_settings_page_serializes_policy_and_prevents_duplicate_saves_in_chromium(self):
        if os.getenv("BABYSTEPS_RUN_REAL_CHROMIUM")!="1":
            self.skipTest("Set BABYSTEPS_RUN_REAL_CHROMIUM=1 to run Chromium settings verification.")
        from urllib.parse import urlparse
        from playwright.sync_api import sync_playwright
        from fastapi import HTTPException
        requests={"get":0,"put":0}
        page_html=assistant.application_settings_page().body.decode()
        def transport(route):
            parsed=urlparse(route.request.url)
            if parsed.path=="/application-settings":
                route.fulfill(status=200,content_type="text/html",body=page_html)
                return
            if parsed.path!="/api/application-authorization":
                route.abort();return
            if route.request.method=="GET":
                requests["get"]+=1
                route.fulfill(status=200,json=assistant.authorization_status())
                return
            requests["put"]+=1
            try:
                payload=route.request.post_data_json
                saved=assistant.update_authorization(assistant.AuthorizationUpdate(**payload))
                route.fulfill(status=200,json=saved)
            except HTTPException as error:
                route.fulfill(status=error.status_code,json={"detail":error.detail})
        with patch("career_preferences.get_preferences",return_value={"categories":["Software Development"],"roles":["Software Engineer"]}):
            with sync_playwright() as playwright:
                browser=playwright.chromium.launch(headless=True)
                page=browser.new_page()
                page.route("**/*",transport)
                page.goto("http://app.test/application-settings",wait_until="domcontentloaded")
                for key,value in {
                    "standing_policy_categories":"Software Development",
                    "standing_policy_employment_types":"Full-time",
                    "standing_policy_countries":"United States",
                    "standing_policy_locations":"Anywhere",
                    "standing_policy_work_arrangements":"Remote, Hybrid, Onsite",
                    "standing_policy_experience_levels":"New Graduate, Early Career",
                    "standing_policy_field_keys":"full_name, email, phone",
                    "standing_policy_document_types":"resume",
                    "standing_policy_daily_limit":"1",
                    "max_applications_per_24_hours":"7",
                }.items():
                    page.locator(f'[name="{key}"]').fill(value)
                page.locator('[name="standing_policy_enabled"]').check()
                page.locator("#settings").evaluate("form=>{const e=new SubmitEvent('submit',{bubbles:true,cancelable:true});form.dispatchEvent(e);form.dispatchEvent(new SubmitEvent('submit',{bubbles:true,cancelable:true}))}")
                page.get_by_text("Saved and read back authorization version",exact=False).wait_for(timeout=5000)
                self.assertEqual(requests["put"],1)
                saved=assistant.get_authorization()
                self.assertTrue(saved["settings"]["standing_policy_enabled"])
                self.assertEqual(saved["settings"]["standing_policy_locations"],["Anywhere"])
                self.assertEqual(saved["settings"]["standing_policy_field_keys"],["full_name","email","phone"])
                self.assertEqual(saved["settings"]["standing_policy_document_types"],["resume"])
                self.assertEqual(saved["settings"]["standing_policy_daily_limit"],1)
                self.assertEqual(saved["settings"]["max_applications_per_24_hours"],7)
                self.assertFalse(saved["settings"]["live_submission_enabled"])
                page.reload(wait_until="domcontentloaded")
                page.wait_for_function("document.querySelector('#status').textContent.includes('Authorization version')")
                self.assertTrue(page.locator('[name="standing_policy_enabled"]').is_checked())
                self.assertEqual(page.locator('[name="standing_policy_locations"]').input_value(),"Anywhere")
                page.locator('[name="standing_policy_locations"]').fill("")
                page.locator("#settings").evaluate("form=>form.dispatchEvent(new SubmitEvent('submit',{bubbles:true,cancelable:true}))")
                page.get_by_text("Allowed locations",exact=False).wait_for(timeout=3000)
                self.assertEqual(requests["put"],1,"The browser must block incomplete policy scope before sending a save.")
                page.locator('[name="standing_policy_enabled"]').uncheck()
                page.locator("#settings").evaluate("form=>form.dispatchEvent(new SubmitEvent('submit',{bubbles:true,cancelable:true}))")
                page.get_by_text("Saved and read back authorization version",exact=False).wait_for(timeout=5000)
                self.assertFalse(assistant.get_authorization()["settings"]["standing_policy_enabled"])
                browser.close()
        scope={"allow_automated_form_preparation":True,"allow_ai_drafting":True,"matching_provider":"ollama",
            "standing_policy_enabled":True,"standing_policy_categories":["Software Engineering"],
            "standing_policy_employment_types":["Full-time"],"standing_policy_countries":["United States"],
            "standing_policy_locations":["Anywhere"],
            "standing_policy_work_arrangements":["Remote"],"standing_policy_experience_levels":["New Graduate"],
            "standing_policy_field_keys":["full_name"],"standing_policy_document_types":["resume"],
            "standing_policy_allow_ai_answers":True,"standing_policy_allow_cloud_ai":True}
        with patch("career_preferences.get_preferences",return_value={"categories":["Software Engineering"],"roles":["Software Engineer"]}):
            with self.assertRaises(HTTPException) as caught:
                assistant.update_authorization(assistant.AuthorizationUpdate(**scope))
        self.assertEqual(caught.exception.status_code,422)

    def test_sensitive_and_referral_questions_are_never_ai_drafted(self):
        from fastapi import HTTPException
        for question in ("Are you authorized to work in the US?","Do you require visa sponsorship?","Did someone refer you?"):
            with self.assertRaises(HTTPException) as caught:
                assistant.draft_application_answer(assistant.DraftRequest(question=question))
            self.assertEqual(caught.exception.status_code,422)

    def test_invalid_historical_answer_is_excluded_from_ai_reusable_context(self):
        saved=profile.save_application_answer("saved-city",profile.AnswerUpdate(question="Location (City)*",
            answer="San Francisco",confirmed=True,reuse_scope="matching_jobs"))
        with profile.connect() as db:
            db.execute("UPDATE application_answers SET answer='yes' WHERE question_key=?",(saved["key"],))
        _,_,reusable=assistant._verified_context()
        self.assertFalse(any(item["question"]=="Location (City)*" for item in reusable))

    def test_cover_letter_text_field_is_ordinary_but_sensitive_questions_stay_blocked(self):
        self.assertTrue(assistant.ORDINARY_PROMPT.search("Please provide a cover letter (maximum 400 words)."))
        self.assertFalse(assistant.SENSITIVE_PROMPT.search("Please provide a cover letter."))
        self.assertTrue(assistant.SENSITIVE_PROMPT.search("Are you authorized to work?"))

    def test_successful_draft_uses_confirmed_facts_and_excludes_private_or_sensitive_facts(self):
        def mocked_post(_url,**kwargs):
            prompt=kwargs["json"]["prompt"]
            self.assertIn("Telemetry API",prompt)
            self.assertIn("Python",prompt)
            self.assertIn("Example Systems",prompt)
            self.assertIn("The team builds developer observability tools.",prompt)
            self.assertNotIn("private@example.test",prompt)
            self.assertNotIn("United States",prompt)
            return FakeResponse({"answer":"I am interested in contributing Python API experience to the backend role.","needs_user_input":False,"evidence_used":["verified candidate profile","job posting"]})
        with patch.object(assistant,"fetch_board",return_value=([{"id":10,"content":"<p>The team builds developer observability tools.</p>"}],None)), \
             patch.object(assistant.requests,"post",side_effect=mocked_post):
            result=assistant.draft_application_answer(assistant.DraftRequest(question="Why are you interested in this position?",job_key="greenhouse:cloudflare:10"))
        self.assertFalse(result["needs_user_input"])
        self.assertLessEqual(len(result["answer"]),assistant.MAX_ANSWER_CHARS)
        self.assertIn("Review and edit",result["disclaimer"])

    def test_model_can_decline_when_evidence_is_insufficient(self):
        with patch.object(assistant.requests,"post",return_value=FakeResponse({"answer":"","needs_user_input":True,"evidence_used":[]})):
            result=assistant.draft_application_answer(assistant.DraftRequest(question="Describe a challenge you solved."))
        self.assertTrue(result["needs_user_input"])
        self.assertEqual(result["answer"],"")

    def test_answer_validation_rejects_unsupported_named_facts_and_limits(self):
        facts={"technical_skills":["Python"],"projects":[{"name":"Telemetry API"}]}
        answer="Northstar is revolutionizing the industry, and I offer Python experience."
        checked=assistant.validate_generated_answer("Why are you interested in this position?",answer,facts,{},
            {"company":"Example Systems","title":"Backend Engineer"},"",120,8)
        self.assertFalse(checked["valid"])
        self.assertTrue(any("Unsupported named claim" in x for x in checked["problems"]))
        too_long=assistant.validate_generated_answer("Why Python?","Python services work well.",facts,{}, {},"",12,2)
        self.assertTrue(any("character field limit" in x for x in too_long["problems"]))
        self.assertTrue(any("word field limit" in x for x in too_long["problems"]))

    def test_grounding_accepts_verified_project_technology_aliases_and_paraphrase(self):
        facts={"project_entries":[{"name":"Telemetry API","description":"Built request aggregation services","technologies":["FastAPI","JavaScript"]}],"skills":["FastAPI","JavaScript"]}
        answer="I built a telemetry service for request aggregation with Fast API and JavaScript. This technical project used verified API tools."
        checked=assistant.validate_generated_answer("Describe a technical project and technologies used.",answer,facts,{}, {},"",500,120)
        self.assertTrue(checked["valid"],checked["problems"])

    def test_grounding_reports_specific_unsupported_organization_claim(self):
        facts={"skills":["Python"],"project_entries":[{"name":"Telemetry API","technologies":["Python"]}]}
        checked=assistant.validate_generated_answer("Describe a technical project.","I built Telemetry API with Python at Northstar Labs.",facts,{}, {},"",500,120)
        self.assertTrue(any("Northstar" in problem or "Labs" in problem for problem in checked["problems"]))

    def test_explicit_answer_word_and_character_limits_are_parsed(self):
        self.assertEqual(assistant.parse_answer_limits("Describe a project (maximum 120 words)."),(None,120))
        self.assertEqual(assistant.parse_answer_limits("Why role? Max 450 characters"),(450,None))

    def test_identical_grounded_drafts_are_cached_by_context(self):
        assistant._DRAFT_CACHE.clear()
        facts={"technical_skills":["Python"],"projects":[{"name":"Telemetry API"}]}
        answer={"answer":"I enjoy building Python services, and the backend role fits my project experience.",
                "needs_user_input":False,"evidence_used":[]}
        with patch.object(assistant.requests,"post",return_value=FakeResponse(answer)) as post:
            first=assistant.generate_draft("Why are you interested in backend engineering?",facts,{},
                {"title":"Backend Engineer"},"",max_chars=160)
            second=assistant.generate_draft("Why are you interested in backend engineering?",facts,{},
                {"title":"Backend Engineer"},"",max_chars=160)
        self.assertFalse(first["needs_user_input"]); self.assertEqual(first,second); post.assert_called_once()

    def test_malformed_ollama_response_is_reported_without_fabricating_answer(self):
        class BadResponse:
            def raise_for_status(self): pass
            def json(self): return {"response":"not json"}
        with patch.object(assistant.requests,"post",return_value=BadResponse()):
            with self.assertRaises(Exception) as caught:
                assistant.draft_application_answer(assistant.DraftRequest(question="Why do you want this company?"))
        self.assertIn("answer assistant is unavailable",str(caught.exception))

    def test_selected_gemini_provider_uses_structured_output_and_confirmed_context(self):
        assistant._DRAFT_CACHE.clear()
        with patch.dict(os.environ,{"GEMINI_API_KEY":"test-only-key"}):
            assistant.update_authorization(assistant.AuthorizationUpdate(allow_ai_drafting=True,matching_provider="gemini"))
            def post(url,**kwargs):
                self.assertIn("generateContent",url)
                self.assertEqual(kwargs["headers"]["x-goog-api-key"],"test-only-key")
                self.assertEqual(kwargs["json"]["generationConfig"]["responseMimeType"],"application/json")
                self.assertIn("Telemetry API",kwargs["json"]["contents"][0]["parts"][0]["text"])
                return FakeGeminiResponse({"answer":"I built a Python telemetry API and am interested in this backend role.",
                    "needs_user_input":False,"evidence_used":["confirmed candidate project","saved job posting"]})
            with patch.object(assistant.requests,"post",side_effect=post) as mocked:
                result=assistant.generate_draft("Why are you interested in this backend role?",
                    {"projects":[{"name":"Telemetry API","technologies":["Python"]}]},{},
                    {"title":"Backend Engineer"},"The backend team builds APIs.")
        self.assertFalse(result["needs_user_input"])
        mocked.assert_called_once()

    def test_selected_gemini_without_key_fails_clearly_without_fallback(self):
        from fastapi import HTTPException
        assistant.update_authorization(assistant.AuthorizationUpdate(allow_ai_drafting=True,matching_provider="gemini"))
        with patch.dict(os.environ,{"GEMINI_API_KEY":""}):
            with patch.object(assistant.requests,"post") as mocked:
                with self.assertRaises(HTTPException) as caught:
                    assistant.generate_draft("Why this position?",{"technical_skills":["Python"]},{},
                        {"title":"Engineer"},"Python software role.")
        self.assertIn("GEMINI_API_KEY",str(caught.exception.detail))
        mocked.assert_not_called()

    def test_groq_provider_health_uses_models_endpoint_without_candidate_data(self):
        payload={"data":[{"id":"openai/gpt-oss-20b"}]}
        with patch.dict(os.environ,{"GROQ_API_KEY":"synthetic-groq-secret","GROQ_MODEL":"openai/gpt-oss-20b"}), \
             patch.object(assistant.requests,"get",return_value=FakeGroqResponse(payload)) as get:
            result=assistant.groq_provider_health()
        self.assertEqual(result,{"provider":"groq","configured":True,"model":"openai/gpt-oss-20b","available":True,"reason":"ready"})
        self.assertEqual(get.call_args.args[0],"https://api.groq.com/openai/v1/models")
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"],"Bearer synthetic-groq-secret")
        self.assertNotIn("synthetic-groq-secret",json.dumps(result))
        self.assertNotIn("synthetic-groq-secret",json.dumps(get.call_args.kwargs.get("json",{})))

    def test_groq_structured_json_response_uses_allowlisted_model_and_server_key(self):
        assistant._DRAFT_CACHE.clear()
        answer={"answer":"","needs_user_input":True,"evidence_used":[]}
        with patch.dict(os.environ,{"GROQ_API_KEY":"synthetic-groq-secret","GROQ_MODEL":"openai/gpt-oss-20b"}):
            saved=assistant.update_authorization(assistant.AuthorizationUpdate(allow_ai_drafting=True,
                matching_provider="groq",groq_model="openai/gpt-oss-20b",allow_groq_cloud_ai=True))
            with patch.object(assistant.requests,"post",return_value=FakeGroqResponse({
                    "choices":[{"message":{"content":json.dumps(answer)}}]})) as post:
                result=assistant.generate_draft("What additional detail is needed?",{"technical_skills":["Python"]},
                    {},{"title":"Synthetic Engineer"},"Synthetic job description.",regenerate=True)
        self.assertTrue(saved["settings"]["groq_configured"])
        self.assertTrue(result["needs_user_input"])
        self.assertEqual(post.call_args.args[0],"https://api.groq.com/openai/v1/chat/completions")
        self.assertEqual(post.call_args.kwargs["json"]["model"],"openai/gpt-oss-20b")
        self.assertEqual(post.call_args.kwargs["json"]["response_format"],{"type":"json_object"})
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"],"Bearer synthetic-groq-secret")
        self.assertNotIn("synthetic-groq-secret",json.dumps(saved))
        self.assertNotIn("synthetic-groq-secret",json.dumps(assistant.authorization_status()))
        with assistant.connect() as db:
            persisted=db.execute("SELECT settings_json FROM application_authorizations WHERE user_id=?",(USER,)).fetchone()[0]
        self.assertNotIn("synthetic-groq-secret",persisted)

    def test_groq_cloud_consent_is_separate_and_invalid_model_is_rejected(self):
        from fastapi import HTTPException
        with patch.dict(os.environ,{"GROQ_API_KEY":""}):
            assistant.update_authorization(assistant.AuthorizationUpdate(allow_ai_drafting=True,matching_provider="groq"))
            with patch.object(assistant.requests,"post") as post:
                with self.assertRaises(HTTPException) as caught:
                    assistant.generate_draft("Why this role?",{"technical_skills":["Python"]},{},
                        {"title":"Synthetic Engineer"},"Synthetic description.")
            self.assertEqual(caught.exception.status_code,403)
            post.assert_not_called()
        with self.assertRaises(HTTPException) as invalid:
            assistant.update_authorization(assistant.AuthorizationUpdate(matching_provider="groq",groq_model="paid/model"))
        self.assertEqual(invalid.exception.status_code,422)
        self.assertEqual(invalid.exception.detail["field"],"groq_model")

    def test_ollama_connection_failure_returns_clear_local_service_error(self):
        import requests
        with patch.object(assistant.requests,"post",side_effect=requests.Timeout("timeout")):
            with self.assertRaises(Exception) as caught:
                assistant.draft_application_answer(assistant.DraftRequest(question="What interests you in this role?"))
        self.assertIn("local answer assistant is unavailable",str(caught.exception))

    def test_country_and_date_profile_entries_are_saved_as_confirmed_values(self):
        profile.update_verification(profile.VerificationUpdate(values={
            "country":"United States","state_province":"California","city":"Oakland","postal_code":"94612",
            "earliest_available_date":"2026-11-01","country_work_authorizations":[{"country":"United States","status":"Authorized"}],
            "sponsorship_by_country":[{"country":"United States","requires_sponsorship":"No"}]},
            confirmed_fields=["country","state_province","city","postal_code","earliest_available_date","country_work_authorizations:0","sponsorship_by_country:0"]))
        with assistant.connect() as db:
            row=db.execute("SELECT values_json FROM candidate_verifications WHERE resume_id='r1'").fetchone()
        self.assertIn('"city": "Oakland"',row[0])
        with assistant.connect() as db:
            saved=db.execute("SELECT values_json,confirmed_json FROM candidate_verifications WHERE resume_id='r1'").fetchone()
        facts=profile.verified_profile_facts(profile.verified_values_from_json(saved["values_json"]),json.loads(saved["confirmed_json"]))
        self.assertEqual(facts["city"],"Oakland")
        self.assertEqual(facts["country_work_authorizations"][0]["status"],"Authorized")


if __name__=="__main__": unittest.main()
