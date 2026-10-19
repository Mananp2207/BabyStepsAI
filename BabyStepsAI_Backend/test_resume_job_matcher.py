import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import job_search_agent as jobs
import resume_job_matcher as matcher


def valid_result(**changes):
    data = {
        "classification": "potential_match",
        "match_score": 84,
        "explanation": "The profile provides evidence for the required skills; confirm the remaining job details.",
        "strengths": ["Python experience is listed in the reviewed profile."],
        "gaps": [],
        "missing_information": [],
        "required_skills": [{"requirement": "Python", "status": "met", "candidate_evidence": ["technical_skills: Python"]}],
        "preferred_skills": [],
        "education_assessment": "Degree requirement appears met based on the profile.",
        "experience_assessment": "Relevant responsibilities are present.",
        "experience_level_assessment": "The posting level appears consistent.",
        "location_assessment": "The listed location is consistent with the saved preference.",
        "employment_type_assessment": "Full-time is specified.",
    }
    data.update(changes)
    return matcher.MatchResult.model_validate(data)


class ResumeJobMatcherTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_db = jobs.DB
        jobs.DB = Path(self.tempdir.name) / "matcher.sqlite3"
        with jobs.database() as db:
            db.executescript("""
                CREATE TABLE resumes (id TEXT PRIMARY KEY, user_id TEXT, created_at TEXT, active INTEGER);
                CREATE TABLE resume_profiles (resume_id TEXT PRIMARY KEY, status TEXT, profile_json TEXT);
                CREATE TABLE candidate_verifications (resume_id TEXT PRIMARY KEY,user_id TEXT,values_json TEXT,confirmed_json TEXT,updated_at TEXT);
                CREATE TABLE career_preferences (user_id TEXT PRIMARY KEY, preferences_json TEXT NOT NULL);
                CREATE TABLE discovered_openings (user_id TEXT, job_key TEXT, company TEXT, title TEXT, location TEXT, url TEXT, discovered_at TEXT, employment_type TEXT DEFAULT '', location_status TEXT DEFAULT 'unknown', location_reason TEXT DEFAULT '', search_prompt TEXT DEFAULT '', PRIMARY KEY(user_id,job_key));
                CREATE TABLE application_authorizations (user_id TEXT PRIMARY KEY, version INTEGER NOT NULL, settings_json TEXT NOT NULL, updated_at TEXT NOT NULL);
            """)
            db.execute("INSERT INTO application_authorizations VALUES(?,1,?,?)",(jobs.USER,json.dumps({"allow_ai_matching":True}),"2026-01-01"))
            db.execute("INSERT INTO resumes VALUES ('resume-v1', ?, '2026-01-01', 1)", (jobs.USER,))
            db.execute("INSERT INTO resume_profiles VALUES ('resume-v1', 'user_reviewed', ?)", (json.dumps({"technical_skills": ["Python"], "education": [{"degree": "B.S."}]}),))
            db.execute("INSERT INTO career_preferences VALUES (?, ?)", (jobs.USER, json.dumps({
                "roles": ["Software Engineer"], "categories": [], "levels": ["New Graduate", "Entry Level", "Junior"],
                "employment_types": ["Full-time"], "location": "United States", "work_arrangement": "Any", "instructions": "",
            })))
            db.execute("INSERT INTO discovered_openings (user_id,job_key,company,title,location,url,discovered_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (
                jobs.USER, "greenhouse:cloudflare:42", "Cloudflare", "Software Engineer",
                "San Francisco, CA, United States", "https://boards.greenhouse.io/cloudflare/jobs/42", "2026-01-01",
            ))
        matcher.initialize()

    def tearDown(self):
        jobs.DB = self.old_db
        self.tempdir.cleanup()

    def test_active_candidate_reads_active_profile_version(self):
        resume_id, status, profile = matcher.active_candidate()
        self.assertEqual(resume_id, "resume-v1")
        self.assertEqual(status, "user_reviewed")
        self.assertEqual(profile["technical_skills"], ["Python"])

    def test_confirmed_graduation_date_is_merged_for_job_matching(self):
        with jobs.database() as db:
            db.execute("INSERT INTO candidate_verifications VALUES('resume-v1',?,?,?,?)",
                       (jobs.USER,json.dumps({"graduation_date":"2026-12"}),json.dumps({"graduation_date":True}),"2026-02-01"))
        _,_,profile=matcher.active_candidate()
        self.assertEqual(profile["graduation_date"],"2026-12")
        self.assertEqual(profile["graduation_year"],"2026")

    def test_unconfirmed_ai_profile_facts_are_not_sent_to_matcher(self):
        with jobs.database() as db:
            db.execute("UPDATE resume_profiles SET status='completed_unverified' WHERE resume_id='resume-v1'")
            db.execute("INSERT INTO candidate_verifications VALUES('resume-v1',?,?,?,?)",
                       (jobs.USER,json.dumps({"skills":["Go"]}),json.dumps({"skills":False}),"2026-02-01"))
        _,status,profile=matcher.active_candidate()
        self.assertEqual(status,"completed_unverified")
        self.assertEqual(profile,{})

    def test_only_field_confirmed_structured_facts_are_sent_to_matcher(self):
        with jobs.database() as db:
            db.execute("UPDATE resume_profiles SET status='completed_unverified' WHERE resume_id='resume-v1'")
            db.execute("INSERT INTO candidate_verifications VALUES('resume-v1',?,?,?,?)",
                       (jobs.USER,json.dumps({"education_entries":[{"institution":"Example U","degree":"B.S.","graduation_date":"2026-12"}],"experience_entries":[{"job_title":"Backend Engineer","employer":"Acme"}],"project_entries":[{"name":"Portfolio"}],"skills":["Go"]}),json.dumps({"education_entries:0":True,"experience_entries:0":True,"project_entries:0":False,"skills":True}),"2026-02-01"))
        _,status,profile=matcher.active_candidate()
        self.assertEqual(status,"field_verified")
        self.assertEqual(profile["education"][0]["institution"],"Example U")
        self.assertEqual(profile["work_experience"][0]["employer"],"Acme")
        self.assertNotIn("projects",profile)
        self.assertEqual(profile["technical_skills"],["Go"])
        prompt=matcher.build_prompt(profile,status,{}, {"title":"Engineer","description":"3 years experience required"})
        self.assertIn("projects/coursework are not professional years",prompt)

    def test_missing_candidate_information_is_needs_review(self):
        result = matcher.apply_safety_rules(valid_result(), {}, "user_reviewed", {}, {"title": "Software Engineer"})
        self.assertEqual(result.classification, "needs_review")
        self.assertTrue(any("no verified qualifications" in value for value in result.missing_information))

    def test_unverified_profile_is_never_a_potential_match(self):
        profile = {"technical_skills": ["Python"]}
        result = matcher.apply_safety_rules(valid_result(), profile, "completed_unverified", {}, {"title": "Software Engineer"})
        self.assertEqual(result.classification, "needs_review")
        self.assertIn("Candidate profile has not been user verified.", result.missing_information)

    def test_location_conflict_overrides_model_potential_match(self):
        prefs = {"roles": ["Software Engineer"], "levels": [], "employment_types": [], "location": "United States"}
        job = {"title": "Software Engineer", "location": {"name": "London, England"}, "content": ""}
        result = matcher.apply_safety_rules(valid_result(), {"technical_skills": ["Python"]}, "user_reviewed", prefs, job)
        self.assertEqual(result.classification, "not_eligible")
        self.assertTrue(any("location preference" in gap for gap in result.gaps))

    def test_geo_conflict_is_prechecked_without_ollama(self):
        foreign = {"id": 42, "title": "Software Engineer", "content": "Python required.",
                   "location": {"name": "London, United Kingdom"}}
        with patch.object(matcher, "fetch_board", return_value=([foreign], None)), \
             patch.object(matcher, "analyze_with_ollama") as model:
            response = matcher.analyze_matches(matcher.MatchRequest(job_keys=["greenhouse:cloudflare:42"]))
            for _ in range(100):
                rows = matcher._match_rows(["greenhouse:cloudflare:42"], "resume-v1")
                if rows and rows[0]["status"] == "completed":
                    break
                time.sleep(0.01)
        model.assert_not_called()
        self.assertEqual(response["prechecked_not_eligible"], 1)
        self.assertEqual(rows[0]["classification"], "not_eligible")

    def test_completed_result_is_not_overwritten_or_analyzed_again(self):
        existing = valid_result()
        with jobs.database() as db:
            db.execute("""INSERT INTO job_match_results
                (user_id,job_key,resume_id,status,classification,match_score,result_json,created_at,updated_at)
                VALUES(?,?,?,'completed',?,?,?,'2026-01-01','2026-01-01')""",
                       (jobs.USER, "greenhouse:cloudflare:42", "resume-v1", existing.classification,
                        existing.match_score, existing.model_dump_json()))
        fake = {"id": 42, "title": "Software Engineer", "content": "Python required.",
                "location": {"name": "Austin, Texas"}}
        with patch.object(matcher, "fetch_board", return_value=([fake], None)), \
             patch.object(matcher, "analyze_with_ollama") as model:
            matcher.analyze_matches(matcher.MatchRequest(job_keys=["greenhouse:cloudflare:42"]))
            for _ in range(100):
                rows = matcher._match_rows(["greenhouse:cloudflare:42"], "resume-v1")
                progress = matcher.get_match_progress()
                if progress["status"] == "completed":
                    break
                time.sleep(0.01)
        model.assert_not_called()
        self.assertEqual(rows[0]["match_score"], 84)
        self.assertEqual(rows[0]["match"]["explanation"], existing.explanation)

    def test_senior_experience_requirement_is_sent_to_ollama(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"response": valid_result(classification="not_eligible", match_score=35,
            explanation="The posting requires eight years of professional experience, which is not verified in the profile.",
            missing_information=["Total years of professional experience are unclear."]).model_dump_json()}
        job = {"title": "Software Engineer", "content": "Candidates must have 8+ years of professional experience."}
        with patch.object(matcher.requests, "post", return_value=response) as post:
            result = matcher.analyze_with_ollama({"work_experience": []}, "user_reviewed", {"levels": ["New Graduate"]}, job)
        self.assertEqual(result.classification, "not_eligible")
        sent_prompt = post.call_args.kwargs["json"]["prompt"]
        self.assertIn("8+ years of professional experience", sent_prompt)
        self.assertIn("professional experience", sent_prompt)

    def test_required_experience_gap_needs_review_but_preferred_gap_does_not_disqualify(self):
        profile = {"work_experience": [{"title": "Intern"}], "technical_skills": ["Python"]}
        required_gap = valid_result(required_experience=[matcher.SkillEvidence(
            requirement="3 years professional experience", status="gap", candidate_evidence=[])])
        preferred_gap = valid_result(preferred_experience=[matcher.SkillEvidence(
            requirement="Experience with Kubernetes preferred", status="preferred", candidate_evidence=[])])
        required = matcher.apply_safety_rules(required_gap, profile, "user_reviewed", {}, {"title": "Software Engineer"})
        preferred = matcher.apply_safety_rules(preferred_gap, profile, "user_reviewed", {}, {"title": "Software Engineer"})
        self.assertEqual(required.classification, "needs_review")
        self.assertEqual(preferred.classification, "potential_match")

    def test_verified_required_conflict_overrides_high_score(self):
        mismatch = valid_result(match_score=99, required_experience=[matcher.SkillEvidence(
            requirement="8 years professional experience", status="confirmed_mismatch",
            candidate_evidence=["profile states 2 years"] )])
        checked = matcher.apply_safety_rules(mismatch, {"work_experience": ["2 years"]}, "user_reviewed", {},
                                             {"title": "Software Engineer"})
        self.assertEqual(checked.classification, "not_eligible")
        self.assertEqual(checked.match_score, 99)

    def test_missing_required_graduation_and_education_are_needs_review(self):
        profile = {"technical_skills": ["Python"], "education": [{"degree": "B.S."}]}
        result = matcher.apply_safety_rules(valid_result(), profile, "user_reviewed", {},
            {"title": "New Graduate Software Engineer 2026",
             "description": "Candidates must have graduated by 2026 and hold a bachelor's degree."})
        self.assertEqual(result.classification, "needs_review")
        self.assertTrue(any("graduation year" in note for note in result.missing_information))
        no_education = matcher.apply_safety_rules(valid_result(), {"technical_skills": ["Python"]}, "user_reviewed", {},
            {"title": "Software Engineer", "description": "A bachelor's degree is required."})
        self.assertEqual(no_education.classification, "needs_review")
        self.assertTrue(any("education" in note.casefold() for note in no_education.missing_information))

    def test_malformed_ollama_response_is_retried_and_rejected(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"response": "not valid JSON"}
        with patch.object(matcher, "OLLAMA_ATTEMPTS", 2), patch.object(matcher.time, "sleep"), \
             patch.object(matcher.requests, "post", return_value=response) as post:
            with self.assertRaisesRegex(RuntimeError, "malformed match JSON"):
                matcher.analyze_with_ollama({}, "user_reviewed", {}, {"title": "Role", "content": "Description"})
        self.assertEqual(post.call_count, 2)

    def test_ollama_timeout_is_retried(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"response": valid_result().model_dump_json()}
        with patch.object(matcher.time, "sleep"), \
             patch.object(matcher.requests, "post", side_effect=[matcher.requests.Timeout("read timeout"), response]) as post:
            result = matcher.analyze_with_ollama({"technical_skills": ["Python"]}, "user_reviewed", {},
                                                 {"title": "Software Engineer", "content": "Python"})
        self.assertEqual(result.classification, "potential_match")
        self.assertEqual(post.call_count, 2)

    def test_ollama_connection_failure_is_retried_and_saved_as_error(self):
        with patch.object(matcher.time, "sleep"), patch.object(matcher.requests, "post",
                side_effect=matcher.requests.ConnectionError("Ollama unavailable")) as post:
            with self.assertRaisesRegex(RuntimeError, "Ollama matching failed"):
                matcher.analyze_with_ollama({}, "user_reviewed", {}, {"title": "Software Engineer", "content": "Python"})
        self.assertEqual(post.call_count, matcher.OLLAMA_ATTEMPTS)

    def test_successful_verified_match_is_structured_and_grounded(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"response": valid_result().model_dump_json()}
        job = {"title": "Software Engineer", "content": "Python experience required."}
        with patch.object(matcher.requests, "post", return_value=response) as post:
            model_result = matcher.analyze_with_ollama({"technical_skills": ["Python"]}, "user_reviewed", {}, job)
        final = matcher.apply_safety_rules(model_result, {"technical_skills": ["Python"]}, "user_reviewed", {}, job)
        self.assertEqual(final.classification, "potential_match")
        self.assertEqual(final.match_score, 84)
        self.assertIn("Python experience required", post.call_args.kwargs["json"]["prompt"])

    def test_feed_description_cache_reuses_downloaded_payload(self):
        board = "cache-test-resume-matcher"
        matcher_response = Mock()
        matcher_response.raise_for_status.return_value = None
        matcher_response.json.return_value = {"jobs": [{"id": 1, "content": "Full description"}]}
        with patch("job_connectors.requests.get", return_value=matcher_response) as get:
            first = jobs.fetch_board("Test", board)
            second = jobs.fetch_board("Test", board)
        self.assertEqual(first, second)
        self.assertEqual(get.call_count, 1)

    def test_batch_persists_match_with_job_and_resume_version(self):
        fake_job = {"id": 42, "title": "Software Engineer", "content": "Python experience required.",
                    "location": {"name": "San Francisco, CA, United States"}, "metadata": [{"name": "Employment Type", "value": "Full-time"}]}
        with jobs.database() as db:
            db.execute("""INSERT INTO job_match_results
                (user_id,job_key,resume_id,status,created_at,updated_at)
                VALUES(?,?,?,'queued','2026-01-01','2026-01-01')""",
                       (jobs.USER, "greenhouse:cloudflare:42", "resume-v1"))
        with patch.object(matcher, "fetch_board", return_value=([fake_job], None)), \
             patch.object(matcher, "analyze_with_ollama", return_value=valid_result()):
            matcher._process_batch(["greenhouse:cloudflare:42"], "resume-v1", "user_reviewed", {"technical_skills": ["Python"]})
        rows = matcher._match_rows(["greenhouse:cloudflare:42"], "resume-v1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "completed")
        self.assertEqual(rows[0]["resume_id"], "resume-v1")
        self.assertEqual(rows[0]["match"]["classification"], "needs_review")
        public = matcher.get_matches("greenhouse:cloudflare:42")["results"][0]
        self.assertNotIn("resume_id", public)
        self.assertNotIn("job_key", public)

    def test_analyze_matches_queues_only_saved_job_for_active_resume(self):
        fake_job = {"id": 42, "title": "Software Engineer", "content": "Python experience required.",
                    "location": {"name": "San Francisco, CA, United States"}, "metadata": [{"name": "Employment Type", "value": "Full-time"}]}
        with patch.object(matcher, "fetch_board", return_value=([fake_job], None)), \
             patch.object(matcher, "analyze_with_ollama", return_value=valid_result()):
            queued = matcher.analyze_matches(matcher.MatchRequest(job_keys=["greenhouse:cloudflare:42"]))
            self.assertEqual(queued["queued"], 1)
            self.assertNotIn("resume_id", queued)
            rows = []
            for _ in range(100):
                rows = matcher._match_rows(["greenhouse:cloudflare:42"], "resume-v1")
                if rows and rows[0]["status"] in ("completed", "failed"):
                    break
                time.sleep(0.01)
        self.assertEqual(rows[0]["status"], "completed")

    def test_ai_matching_requires_explicit_application_authorization(self):
        from fastapi import HTTPException
        with jobs.database() as db:
            db.execute("UPDATE application_authorizations SET settings_json=? WHERE user_id=?",
                       (json.dumps({"allow_ai_matching":False}),jobs.USER))
        with self.assertRaises(HTTPException) as caught:
            matcher.analyze_matches(matcher.MatchRequest(job_keys=["greenhouse:cloudflare:42"]))
        self.assertEqual(caught.exception.status_code,403)
        self.assertIn("Application settings",caught.exception.detail)

    def test_unrelated_saved_jobs_are_excluded_before_ollama(self):
        with jobs.database() as db:
            db.execute("INSERT INTO discovered_openings (user_id,job_key,company,title,location,url,discovered_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (
                jobs.USER, "greenhouse:cloudflare:99", "Cloudflare",
                "Business Development Representative - Danish, Norwegian or Swedish Speaking",
                "Remote", "https://boards.greenhouse.io/cloudflare/jobs/99", "2026-01-01"))
        with patch.object(matcher, "fetch_board", return_value=([{"id": 99, "title": "Business Development Representative - Danish, Norwegian or Swedish Speaking", "content": "Sell subscriptions"}], None)), \
             patch.object(matcher, "analyze_with_ollama") as ollama:
            result = matcher.analyze_matches(matcher.MatchRequest(job_keys=["greenhouse:cloudflare:99"]))
        self.assertEqual(result["excluded"], 1)
        ollama.assert_not_called()

    def test_session_batches_pause_resume_without_repeating_completed_jobs(self):
        for job_id in (142, 143, 144, 145):
            with jobs.database() as db:
                db.execute("INSERT INTO discovered_openings (user_id,job_key,company,title,location,url,discovered_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (
                    jobs.USER, f"greenhouse:cloudflare:{job_id}", "Cloudflare", "Software Engineer",
                    "San Francisco, CA, United States", f"https://boards.greenhouse.io/cloudflare/jobs/{job_id}", "2026-01-01"))
        feed = [{"id": job_id, "title": "Software Engineer", "content": "Python required."} for job_id in (142, 143, 144, 145)]
        with patch.object(matcher, "fetch_board", return_value=(feed, None)), \
             patch.object(matcher, "analyze_with_ollama", return_value=valid_result()) as model:
            response = matcher.analyze_matches(matcher.MatchRequest(job_keys=[f"greenhouse:cloudflare:{i}" for i in (142, 143, 144, 145)], batch_size=3))
            for _ in range(100):
                progress = matcher.get_match_progress()
                if progress["status"] == "paused":
                    break
                time.sleep(0.01)
            self.assertEqual(progress["completed"], 3)
            self.assertEqual(progress["pending"], 1)
            matcher.resume_matches()
            for _ in range(100):
                progress = matcher.get_match_progress()
                if progress["status"] == "completed":
                    break
                time.sleep(0.01)
            self.assertEqual(model.call_count, 4, progress)
        self.assertEqual(model.call_count, 4)
        self.assertEqual(progress["completed"], 4)

    def test_required_seniority_conflict_overrides_ai_classification(self):
        result = valid_result()
        job = {"title": "Senior Software Engineer", "location": "United States", "description": "Must have 8 years experience"}
        prefs = {"roles": ["Software Engineer"], "levels": ["New Graduate"], "location": "United States"}
        checked = matcher.apply_safety_rules(result, {"work_experience": []}, "user_reviewed", prefs, job)
        self.assertEqual(checked.classification, "not_eligible")

    def test_missing_required_work_authorization_is_needs_review(self):
        result = matcher.apply_safety_rules(
            valid_result(), {"technical_skills": ["Python"]}, "user_reviewed", {},
            {"title": "Software Engineer", "description": "Must be authorized to work in the United States."})
        self.assertEqual(result.classification, "needs_review")
        self.assertTrue(any("Work authorization" in item for item in result.missing_information))


if __name__ == "__main__":
    unittest.main()
