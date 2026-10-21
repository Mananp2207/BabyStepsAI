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

    def test_sensitive_and_referral_questions_are_never_ai_drafted(self):
        from fastapi import HTTPException
        for question in ("Are you authorized to work in the US?","Do you require visa sponsorship?","Did someone refer you?"):
            with self.assertRaises(HTTPException) as caught:
                assistant.draft_application_answer(assistant.DraftRequest(question=question))
            self.assertEqual(caught.exception.status_code,422)

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
