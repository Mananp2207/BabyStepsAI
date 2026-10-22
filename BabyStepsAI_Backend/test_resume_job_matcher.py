import json
import multiprocessing
import tempfile
import threading
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


def _process_match_claim(db_path,barrier,result_queue):
    jobs.DB=Path(db_path)
    barrier.wait()
    try:
        response=matcher.analyze_matches(matcher.MatchRequest(job_keys=["greenhouse:cloudflare:42"]))
        result_queue.put(("ok",response.get("session_id")))
    except Exception as error:
        result_queue.put(("error",getattr(error,"detail",type(error).__name__)))


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
                CREATE TABLE application_answers(user_id TEXT,resume_id TEXT,question_key TEXT,answer TEXT,sensitive INTEGER,confirmed INTEGER,revoked_at TEXT,auto_reuse INTEGER);
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

    def test_matching_uses_confirmed_ordinary_answer_bank_facts_only(self):
        with jobs.database() as db:
            db.executemany("INSERT INTO application_answers VALUES(?,?,?,?,?,?,?,?)",[
                (jobs.USER,"resume-v1","custom:graduation_date","2026-06",0,1,None,1),
                (jobs.USER,"resume-v1","custom:technical_skills","Python, Rust",0,1,None,1),
                (jobs.USER,"resume-v1","custom:professional_experience","Built verified production APIs",0,1,None,1),
                (jobs.USER,"resume-v1","custom:work_authorization","Sensitive answer",1,1,None,0),
                (jobs.USER,"resume-v1","custom:location","Old answer",0,0,None,1)])
        _,_,profile=matcher.active_candidate()
        self.assertEqual(profile["graduation_year"],"2026")
        self.assertIn("Rust",profile["technical_skills"])
        self.assertEqual(profile["work_experience"][-1]["responsibilities"],["Built verified production APIs"])
        self.assertNotIn("work_authorization",profile)
        self.assertNotIn("Old answer",str(profile))

    def test_match_freshness_detects_engine_profile_preference_and_job_changes(self):
        resume_id,status,profile=matcher.active_candidate()
        self.assertEqual(status,"user_reviewed")
        saved_prefs=jobs.preferences()
        with jobs.database() as db:
            opening=dict(db.execute("SELECT * FROM discovered_openings WHERE job_key='greenhouse:cloudflare:42'").fetchone())
            stamp=matcher.now()
            db.execute("""INSERT INTO job_match_results(user_id,job_key,resume_id,status,classification,match_score,result_json,
                created_at,updated_at,candidate_fingerprint,preferences_fingerprint,job_fingerprint,matching_model_version,freshness_status)
                VALUES(?,?,?,'completed','potential_match',84,?,?,?,?,?,?,?,'current')""",
                (jobs.USER,"greenhouse:cloudflare:42",resume_id,valid_result().model_dump_json(),stamp,stamp,
                 matcher.profile_fingerprint(profile,resume_id),matcher.stable_fingerprint(saved_prefs),
                 matcher.job_fingerprint(opening),matcher.MATCHING_ENGINE_VERSION))
        current=matcher._match_rows(["greenhouse:cloudflare:42"],resume_id)[0]
        self.assertFalse(current["stale"])
        with jobs.database() as db:
            db.execute("UPDATE discovered_openings SET title='Changed Software Engineer' WHERE job_key='greenhouse:cloudflare:42'")
        changed=matcher._match_rows(["greenhouse:cloudflare:42"],resume_id)[0]
        self.assertTrue(changed["stale"])
        self.assertEqual(changed["freshness_status"],"stale")

    def test_legacy_match_without_version_fingerprints_is_stale(self):
        with jobs.database() as db:
            stamp=matcher.now()
            db.execute("""INSERT INTO job_match_results(user_id,job_key,resume_id,status,classification,match_score,result_json,created_at,updated_at)
                VALUES(?,?,'resume-v1','completed','potential_match',80,?,?,?)""",
                (jobs.USER,"greenhouse:cloudflare:42",valid_result().model_dump_json(),stamp,stamp))
        result=matcher._match_rows(["greenhouse:cloudflare:42"],"resume-v1")[0]
        self.assertTrue(result["stale"])

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
        resume_id,_,profile=matcher.active_candidate()
        prefs=jobs.preferences()
        with jobs.database() as db:
            opening=dict(db.execute("SELECT * FROM discovered_openings WHERE job_key='greenhouse:cloudflare:42'").fetchone())
            db.execute("""INSERT INTO job_match_results
                (user_id,job_key,resume_id,status,classification,match_score,result_json,created_at,updated_at,
                 candidate_fingerprint,preferences_fingerprint,job_fingerprint,matching_model_version,freshness_status)
                VALUES(?,?,?,'completed',?,?,?,'2026-01-01','2026-01-01',?,?,?,?,'current')""",
                       (jobs.USER, "greenhouse:cloudflare:42", resume_id, existing.classification,
                        existing.match_score, existing.model_dump_json(),matcher.profile_fingerprint(profile,resume_id),
                        matcher.stable_fingerprint(prefs),matcher.job_fingerprint(opening),matcher.MATCHING_ENGINE_VERSION))
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

    def test_provider_change_marks_saved_match_stale(self):
        resume_id,status,profile=matcher.active_candidate()
        prefs=jobs.preferences();key="greenhouse:cloudflare:42"
        with jobs.database() as db:
            opening=dict(db.execute("SELECT * FROM discovered_openings WHERE job_key=?",(key,)).fetchone())
            db.execute("""INSERT INTO job_match_results(user_id,job_key,resume_id,status,classification,match_score,result_json,
                created_at,updated_at,candidate_fingerprint,preferences_fingerprint,job_fingerprint,matching_model_version,freshness_status)
                VALUES(?,?,?,'completed','potential_match',84,?,?,?,?,?,?,?,'current')""",
                (jobs.USER,key,resume_id,valid_result().model_dump_json(),"2026-01-01","2026-01-01",
                 matcher.profile_fingerprint(profile,resume_id),matcher.stable_fingerprint(prefs),matcher.job_fingerprint(opening),
                 matcher.matching_engine_version("ollama",matcher.MODEL)))
        with patch.object(matcher,"current_provider_config",return_value=("gemini",matcher.GEMINI_MODEL)):
            rows=matcher._match_rows([key],resume_id)
        self.assertTrue(rows[0]["stale"])

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
        with patch.object(matcher, "OLLAMA_ATTEMPTS", 3), patch.object(matcher.time, "sleep"), \
             patch.object(matcher.requests, "post", return_value=response) as post:
            with self.assertRaisesRegex(RuntimeError, "malformed match JSON"):
                matcher.analyze_with_ollama({}, "user_reviewed", {}, {"title": "Role", "content": "Description"})
        self.assertEqual(post.call_count, matcher.OLLAMA_ATTEMPTS)
        self.assertEqual(post.call_args.kwargs["json"]["format"]["type"],"object")

    def test_json_parser_recovers_fenced_object_with_surrounding_text(self):
        parsed=matcher._parse_match_text("Here is the result:\n```json\n"+valid_result().model_dump_json()+"\n```\nDone.")
        self.assertEqual(parsed.classification,"potential_match")

    def test_gemini_structured_response_uses_server_key_and_validates_schema(self):
        response=Mock();response.status_code=200
        response.json.return_value={"candidates":[{"content":{"parts":[{"text":valid_result().model_dump_json()}]}}]}
        with patch.dict("os.environ",{"GEMINI_API_KEY":"test-secret-key"}),patch.object(matcher.requests,"post",return_value=response) as post:
            result=matcher.analyze_with_provider({"technical_skills":["Python"]},"user_reviewed",{},
                {"title":"Software Engineer","content":"Python experience"},provider="gemini")
        self.assertEqual(result.classification,"potential_match")
        self.assertEqual(post.call_args.kwargs["headers"]["x-goog-api-key"],"test-secret-key")
        self.assertNotIn("params",post.call_args.kwargs)
        self.assertEqual(post.call_args.kwargs["json"]["generationConfig"]["responseMimeType"],"application/json")
        self.assertEqual(post.call_args.kwargs["json"]["generationConfig"]["responseSchema"]["type"],"OBJECT")
        self.assertIn("classification",post.call_args.kwargs["json"]["generationConfig"]["responseSchema"]["required"])

    def test_gemini_missing_key_fails_closed_without_request(self):
        with patch.dict("os.environ",{"GEMINI_API_KEY":""}),patch.object(matcher.requests,"post") as post:
            with self.assertRaisesRegex(RuntimeError,"GEMINI_API_KEY is not configured"):
                matcher.analyze_with_provider({},"user_reviewed",{}, {"title":"Role","content":"Description"},provider="gemini")
        post.assert_not_called()

    def test_gemini_retries_malformed_json_with_repair_instruction(self):
        bad=Mock();bad.status_code=200;bad.json.return_value={"candidates":[{"content":{"parts":[{"text":"not json"}]}}]}
        good=Mock();good.status_code=200;good.json.return_value={"candidates":[{"content":{"parts":[{"text":valid_result().model_dump_json()}]}}]}
        with patch.dict("os.environ",{"GEMINI_API_KEY":"test-secret-key"}),patch.object(matcher.time,"sleep"), \
             patch.object(matcher.requests,"post",side_effect=[bad,good]) as post:
            result=matcher.analyze_with_provider({},"user_reviewed",{}, {"title":"Role","content":"Description"},provider="gemini")
        self.assertEqual(result.classification,"potential_match")
        self.assertIn(matcher.MATCHING_JSON_REPAIR_INSTRUCTION,post.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"])

    def test_new_matching_checkpoint_pins_selected_provider_and_model(self):
        with jobs.database() as db:
            db.execute("UPDATE application_authorizations SET settings_json=? WHERE user_id=?",
                (json.dumps({"allow_ai_matching":True,"matching_provider":"gemini"}),jobs.USER))
        fake={"id":42,"title":"Software Engineer","content":"Python experience required.",
              "absolute_url":"https://boards.greenhouse.io/cloudflare/jobs/42",
              "location":{"name":"San Francisco, CA, United States"},"employment_type":"Full-time"}
        with patch.dict("os.environ",{"GEMINI_API_KEY":"test-secret-key"}), \
             patch.object(matcher,"fetch_board",return_value=([fake],None)), \
             patch.object(matcher.executor,"submit",return_value=None):
            result=matcher.analyze_matches(matcher.MatchRequest(job_keys=["greenhouse:cloudflare:42"]))
        with jobs.database() as db:
            session=db.execute("SELECT provider,provider_model FROM job_match_sessions WHERE session_id=?",(result["session_id"],)).fetchone()
        self.assertEqual(session["provider"],"gemini")
        self.assertEqual(session["provider_model"],matcher.GEMINI_MODEL)
        progress=matcher._progress(result["session_id"])
        self.assertEqual(progress["provider"],"gemini")
        self.assertEqual(progress["provider_model"],matcher.GEMINI_MODEL)

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
        self.assertTrue(all(call.kwargs.get("timeout")==matcher.OLLAMA_TIMEOUT for call in post.call_args_list))

    def test_progress_reports_slow_but_live_request_without_recovery(self):
        from datetime import timedelta
        stamp=matcher.datetime.now(matcher.timezone.utc)
        started=(stamp-timedelta(minutes=5)).isoformat()
        heartbeat=(stamp-timedelta(seconds=5)).isoformat()
        lease=(stamp+timedelta(minutes=2)).isoformat()
        with jobs.database() as db:
            db.execute("""INSERT INTO job_match_results(user_id,job_key,resume_id,status,created_at,updated_at)
                VALUES(?,?,'resume-v1','processing',?,?)""",(jobs.USER,"greenhouse:cloudflare:42",started,started))
            db.execute("""INSERT INTO job_match_sessions(session_id,user_id,resume_id,status,job_keys_json,batch_size,
                total_jobs,workflow_id,owner_token,owner_pid,owner_host,heartbeat_at,lease_expires_at,
                current_job_key,current_job_started_at,created_at,updated_at)
                VALUES('slow-live',?,'resume-v1','running',?,3,1,'wf',?,?,?,?,?,?,?, ?,?)""",
                (jobs.USER,json.dumps(["greenhouse:cloudflare:42"]),"live-token",__import__('os').getpid(),
                 matcher.socket.gethostname(),heartbeat,lease,"greenhouse:cloudflare:42",started,started,heartbeat))
        progress=matcher._progress("slow-live")
        self.assertEqual(progress["worker_health"],"slow_processing")
        self.assertFalse(progress["recovery_available"])
        self.assertTrue(progress["owner_pid_alive"])
        self.assertGreaterEqual(progress["current_elapsed_seconds"],300)
        self.assertEqual(progress["current_job"]["title"],"Software Engineer")
        self.assertEqual(progress["last_progress_at"],heartbeat)

    def test_progress_offers_recovery_only_for_expired_dead_local_owner(self):
        from datetime import timedelta
        stale=(matcher.datetime.now(matcher.timezone.utc)-timedelta(minutes=6)).isoformat()
        with jobs.database() as db:
            db.execute("""INSERT INTO job_match_sessions(session_id,user_id,resume_id,status,job_keys_json,batch_size,
                total_jobs,workflow_id,owner_token,owner_pid,owner_host,heartbeat_at,lease_expires_at,created_at,updated_at)
                VALUES('dead-progress',?,'resume-v1','running','[]',3,0,'wf','dead-token',99999999,?,?,?, ?,?)""",
                (jobs.USER,matcher.socket.gethostname(),stale,stale,stale,stale))
        progress=matcher._progress("dead-progress")
        self.assertEqual(progress["worker_health"],"stalled_recoverable")
        self.assertTrue(progress["recovery_available"])
        with patch.object(matcher,"_pid_is_alive",return_value=True):
            active=matcher._progress("dead-progress")
        self.assertFalse(active["recovery_available"])
        self.assertEqual(active["worker_health"],"heartbeat_late")

    def test_terminal_failed_job_does_not_leave_completed_workflow_session_paused(self):
        fake_job={"id":42,"title":"Software Engineer","content":"Python required.",
                  "absolute_url":"https://boards.greenhouse.io/cloudflare/jobs/42",
                  "location":{"name":"San Francisco, CA, United States"},"_source_platform":"greenhouse",
                  "company_name":"Cloudflare","_search_prompt":"software engineering jobs"}
        with patch.object(matcher,"_find_feed_job",return_value=(fake_job,None)), \
             patch.object(matcher,"analyze_with_ollama",side_effect=RuntimeError("local model timed out")):
            response=matcher.analyze_matches(matcher.MatchRequest(job_keys=["greenhouse:cloudflare:42"]))
            for _ in range(200):
                progress=matcher._progress(response["session_id"])
                if progress["status"]=="completed":break
                time.sleep(.01)
        self.assertEqual(progress["status"],"completed")
        self.assertEqual(progress["failed"],1)
        self.assertEqual(progress["pending"],0)
        self.assertIn("failed analyses can be retried",progress["error"])

    def test_recovery_refuses_an_expired_owner_on_an_unverified_remote_host(self):
        from datetime import timedelta
        from fastapi import HTTPException
        stale=(matcher.datetime.now(matcher.timezone.utc)-timedelta(minutes=6)).isoformat()
        with jobs.database() as db:
            db.execute("""INSERT INTO job_match_sessions(session_id,user_id,resume_id,status,job_keys_json,batch_size,
                total_jobs,workflow_id,owner_token,owner_pid,owner_host,heartbeat_at,lease_expires_at,created_at,updated_at)
                VALUES('remote-owner',?,'resume-v1','running','[]',3,0,'wf','remote-token',99999999,'other-host',?,?,?,?)""",
                (jobs.USER,stale,stale,stale,stale))
        with self.assertRaises(HTTPException) as caught:
            matcher.recover_match_session("remote-owner",True)
        self.assertEqual(caught.exception.status_code,409)
        self.assertIn("another host",caught.exception.detail)
        with jobs.database() as db:
            row=db.execute("SELECT status,owner_token FROM job_match_sessions WHERE session_id='remote-owner'").fetchone()
        self.assertEqual(tuple(row),("running","remote-token"))

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

    def test_batch_persists_selected_gemini_engine_version(self):
        fake_job={"id":42,"title":"Software Engineer","content":"Python experience required.",
                  "location":{"name":"San Francisco, CA, United States"},"metadata":[{"name":"Employment Type","value":"Full-time"}]}
        with jobs.database() as db:
            db.execute("INSERT INTO job_match_results(user_id,job_key,resume_id,status,created_at,updated_at) VALUES(?,?,?,'queued','2026-01-01','2026-01-01')",
                       (jobs.USER,"greenhouse:cloudflare:42","resume-v1"))
        with patch.object(matcher,"fetch_board",return_value=([fake_job],None)), \
             patch.object(matcher,"analyze_with_provider",return_value=valid_result()):
            matcher._process_batch(["greenhouse:cloudflare:42"],"resume-v1","user_reviewed",{"technical_skills":["Python"]},
                                   provider="gemini",provider_model=matcher.GEMINI_MODEL)
        with jobs.database() as db:
            result=db.execute("SELECT matching_model_version,freshness_status FROM job_match_results WHERE job_key=?",
                              ("greenhouse:cloudflare:42",)).fetchone()
        self.assertEqual(result["matching_model_version"],matcher.matching_engine_version("gemini",matcher.GEMINI_MODEL))
        self.assertEqual(result["freshness_status"],"current")

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

    def test_unrelated_paused_legacy_session_does_not_block_new_workflow(self):
        with jobs.database() as db:
            old_keys=[f"greenhouse:cloudflare:old-{i}" for i in range(56)]
            db.execute("""INSERT INTO job_match_sessions(session_id,user_id,resume_id,status,job_keys_json,batch_size,
                total_jobs,created_at,updated_at) VALUES('legacy-paused',?,'resume-v1','paused',?,3,56,'old','old')""",
                (jobs.USER,json.dumps(old_keys)))
            db.execute("CREATE TABLE user_workflows(workflow_id TEXT,user_id TEXT,resume_id TEXT,current_stage TEXT)")
            db.execute("INSERT INTO user_workflows VALUES('workflow-new',?,'resume-v1','matching')",(jobs.USER,))
        fake_job={"id":42,"title":"Software Engineer","content":"Python experience required.",
                  "absolute_url":"https://boards.greenhouse.io/cloudflare/jobs/42",
                  "location":{"name":"San Francisco, CA, United States"},"_source_platform":"greenhouse",
                  "company_name":"Cloudflare","_search_prompt":"software engineering jobs"}
        with patch.object(matcher,"_find_feed_job",return_value=(fake_job,None)), \
             patch.object(matcher.executor,"submit",return_value=None) as submit:
            response=matcher.analyze_matches(matcher.MatchRequest(job_keys=["greenhouse:cloudflare:42"]),workflow_id="workflow-new")
        self.assertEqual(response["status"],"queued")
        self.assertTrue(response["session_id"])
        self.assertEqual(submit.call_count,1)
        with jobs.database() as db:
            old=db.execute("SELECT status,owner_token FROM job_match_sessions WHERE session_id='legacy-paused'").fetchone()
            new=db.execute("SELECT status,workflow_id,owner_token FROM job_match_sessions WHERE session_id=?",(response["session_id"],)).fetchone()
        self.assertEqual(tuple(old),("paused",""))
        self.assertEqual(new["status"],"queued")
        self.assertEqual(new["workflow_id"],"workflow-new")
        self.assertTrue(new["owner_token"])

    def test_linked_subset_checkpoint_attaches_to_full_workflow_selection(self):
        with jobs.database() as db:
            db.execute("CREATE TABLE user_workflows(workflow_id TEXT PRIMARY KEY,user_id TEXT,matching_session_id TEXT)")
            db.execute("INSERT INTO user_workflows VALUES('wf-retry',?, 'retry-session')",(jobs.USER,))
            db.execute("""INSERT INTO job_match_sessions(session_id,user_id,resume_id,status,job_keys_json,batch_size,total_jobs,created_at,updated_at)
                VALUES('retry-session',?,'resume-v1','paused',?,3,1,'old','old')""",(jobs.USER,json.dumps(["greenhouse:cloudflare:42"])))
            requested=["greenhouse:cloudflare:42","greenhouse:cloudflare:43"]
            self.assertIsNone(matcher._matching_conflict(db,requested,"wf-retry",matcher.now()))
            attached=matcher._attach_workflow_checkpoint(db,"wf-retry",requested,matcher.now())
            self.assertEqual(attached["workflow_id"],"wf-retry")
            self.assertEqual(json.loads(attached["job_keys_json"]),requested)
            self.assertEqual(attached["total_jobs"],2)

    def test_workflow_never_attaches_a_checkpoint_owned_by_another_workflow(self):
        with jobs.database() as db:
            db.execute("CREATE TABLE user_workflows(workflow_id TEXT PRIMARY KEY,user_id TEXT,matching_session_id TEXT)")
            db.execute("INSERT INTO user_workflows VALUES('wf-new',?, 'session-old')",(jobs.USER,))
            db.execute("INSERT INTO user_workflows VALUES('wf-old',?, '')",(jobs.USER,))
            db.execute("""INSERT INTO job_match_sessions(session_id,user_id,resume_id,status,job_keys_json,batch_size,
                total_jobs,workflow_id,created_at,updated_at) VALUES('session-old',?,'resume-v1','paused',?,3,1,'wf-old','old','old')""",
                (jobs.USER,json.dumps(["greenhouse:cloudflare:42"])))
            attached=matcher._attach_workflow_checkpoint(db,"wf-new",["greenhouse:cloudflare:42"],matcher.now())
            self.assertIsNone(attached)
            owner=db.execute("SELECT workflow_id,status FROM job_match_sessions WHERE session_id='session-old'").fetchone()
            self.assertEqual(tuple(owner),("wf-old","paused"))

    def test_matching_resume_does_not_reclaim_another_workflows_exact_paused_session(self):
        with jobs.database() as db:
            db.execute("CREATE TABLE user_workflows(workflow_id TEXT PRIMARY KEY,user_id TEXT,resume_id TEXT,matching_session_id TEXT,current_stage TEXT)")
            db.execute("INSERT INTO user_workflows VALUES('wf-new',?,'resume-v1','session-old','matching')",(jobs.USER,))
            db.execute("INSERT INTO user_workflows VALUES('wf-old',?,'resume-v1',NULL,'matching')",(jobs.USER,))
            db.execute("""INSERT INTO job_match_sessions(session_id,user_id,resume_id,status,job_keys_json,batch_size,
                total_jobs,workflow_id,created_at,updated_at) VALUES('session-old',?,'resume-v1','paused',?,3,1,'wf-old','old','old')""",
                (jobs.USER,json.dumps(["greenhouse:cloudflare:42"])))
        fake_job={"id":42,"title":"Software Engineer","content":"Python required.",
                  "absolute_url":"https://boards.greenhouse.io/cloudflare/jobs/42",
                  "location":{"name":"San Francisco, CA, United States"},"_source_platform":"greenhouse",
                  "company_name":"Cloudflare","_search_prompt":"software engineering jobs"}
        with patch.object(matcher,"_find_feed_job",return_value=(fake_job,None)), \
             patch.object(matcher.executor,"submit",return_value=None):
            response=matcher.analyze_matches(matcher.MatchRequest(job_keys=["greenhouse:cloudflare:42"]),workflow_id="wf-new")
        self.assertNotEqual(response["session_id"],"session-old")
        with jobs.database() as db:
            old=db.execute("SELECT status,workflow_id,owner_token FROM job_match_sessions WHERE session_id='session-old'").fetchone()
            new=db.execute("SELECT workflow_id,status FROM job_match_sessions WHERE session_id=?",(response["session_id"],)).fetchone()
        self.assertEqual(tuple(old),("paused","wf-old",""))
        self.assertEqual(tuple(new),("wf-new","queued"))

    def test_legacy_active_session_requires_explicit_aged_recovery(self):
        from datetime import timedelta
        from fastapi import HTTPException
        stamp=(matcher.datetime.now(matcher.timezone.utc)-timedelta(minutes=5)).isoformat()
        with jobs.database() as db:
            db.execute("""CREATE TABLE user_workflows(workflow_id TEXT,user_id TEXT,status TEXT,
                current_stage TEXT,pause_requested INTEGER,diagnostics_json TEXT,updated_at TEXT)""")
            db.execute("INSERT INTO user_workflows VALUES('legacy-workflow',?,'running','matching',0,'{}',?)",
                       (jobs.USER,stamp))
            db.execute("""INSERT INTO job_match_sessions(session_id,user_id,resume_id,status,job_keys_json,batch_size,
                total_jobs,workflow_id,created_at,updated_at) VALUES('legacy-running',?,'resume-v1','running',?,3,1,'legacy-workflow',?,?)""",
                (jobs.USER,json.dumps(["greenhouse:cloudflare:42"]),stamp,stamp))
        fake_job={"id":42,"title":"Software Engineer","content":"Python required.",
                  "absolute_url":"https://boards.greenhouse.io/cloudflare/jobs/42",
                  "location":{"name":"San Francisco, CA, United States"},"_source_platform":"greenhouse",
                  "company_name":"Cloudflare","_search_prompt":"software engineering jobs"}
        with patch.object(matcher,"_find_feed_job",return_value=(fake_job,None)), \
             patch.object(matcher.executor,"submit",return_value=None):
            with self.assertRaises(HTTPException) as caught:
                matcher.analyze_matches(matcher.MatchRequest(job_keys=["greenhouse:cloudflare:42"]))
        self.assertEqual(caught.exception.status_code,409)
        self.assertEqual(caught.exception.detail["code"],"legacy_unowned")
        with self.assertRaises(HTTPException) as caught:
            matcher.recover_match_session("legacy-running",False)
        self.assertEqual(caught.exception.status_code,422)
        recovered=matcher.recover_match_session("legacy-running",True)
        self.assertTrue(recovered["recovered"])
        with jobs.database() as db:
            row=db.execute("SELECT status,job_keys_json FROM job_match_sessions WHERE session_id='legacy-running'").fetchone()
            workflow=db.execute("SELECT status,current_stage,pause_requested FROM user_workflows WHERE workflow_id='legacy-workflow'").fetchone()
        self.assertEqual(row["status"],"interrupted")
        self.assertEqual(json.loads(row["job_keys_json"]),["greenhouse:cloudflare:42"])
        self.assertEqual(tuple(workflow),("paused","matching",1))

    def test_expired_dead_owner_is_interrupted_and_processing_checkpoint_requeued(self):
        from datetime import timedelta
        stale=(matcher.datetime.now(matcher.timezone.utc)-timedelta(minutes=6)).isoformat()
        with jobs.database() as db:
            db.execute("""INSERT INTO job_match_results(user_id,job_key,resume_id,status,created_at,updated_at)
                VALUES(?,?,'resume-v1','processing',?,?)""",(jobs.USER,"greenhouse:cloudflare:stale",stale,stale))
            db.execute("""INSERT INTO job_match_sessions(session_id,user_id,resume_id,status,job_keys_json,batch_size,
                total_jobs,workflow_id,owner_token,owner_pid,owner_host,heartbeat_at,lease_expires_at,created_at,updated_at)
                VALUES('dead-owner',?,'resume-v1','running',?,3,1,'','dead-token',99999999,?,?,?, ?,?)""",
                (jobs.USER,json.dumps(["greenhouse:cloudflare:stale"]),matcher.socket.gethostname(),stale,stale,stale,stale))
        fake_job={"id":42,"title":"Software Engineer","content":"Python required.",
                  "absolute_url":"https://boards.greenhouse.io/cloudflare/jobs/42",
                  "location":{"name":"San Francisco, CA, United States"},"_source_platform":"greenhouse",
                  "company_name":"Cloudflare","_search_prompt":"software engineering jobs"}
        with patch.object(matcher,"_find_feed_job",return_value=(fake_job,None)), \
             patch.object(matcher.executor,"submit",return_value=None):
            response=matcher.analyze_matches(matcher.MatchRequest(job_keys=["greenhouse:cloudflare:42"]))
        self.assertEqual(response["status"],"queued")
        with jobs.database() as db:
            session=db.execute("SELECT status,owner_token,job_keys_json FROM job_match_sessions WHERE session_id='dead-owner'").fetchone()
            result=db.execute("SELECT status FROM job_match_results WHERE job_key='greenhouse:cloudflare:stale'").fetchone()
        self.assertEqual(session["status"],"interrupted")
        self.assertEqual(session["owner_token"],"")
        self.assertEqual(result["status"],"queued")
        self.assertEqual(json.loads(session["job_keys_json"]),["greenhouse:cloudflare:stale"])

    def test_concurrent_matching_acquisition_has_one_owner(self):
        import threading
        from fastapi import HTTPException
        gate=threading.Barrier(2);results=[]
        fake_job={"id":42,"title":"Software Engineer","content":"Python required.",
                  "absolute_url":"https://boards.greenhouse.io/cloudflare/jobs/42",
                  "location":{"name":"San Francisco, CA, United States"},"_source_platform":"greenhouse",
                  "company_name":"Cloudflare","_search_prompt":"software engineering jobs"}
        def run():
            gate.wait()
            try:results.append(("ok",matcher.analyze_matches(matcher.MatchRequest(job_keys=["greenhouse:cloudflare:42"]))))
            except HTTPException as error:results.append(("conflict",error.detail.get("code") if isinstance(error.detail,dict) else error.detail))
        with patch.object(matcher,"_find_feed_job",return_value=(fake_job,None)), \
             patch.object(matcher.executor,"submit",return_value=None):
            threads=[threading.Thread(target=run) for _ in range(2)]
            for thread in threads:thread.start()
            for thread in threads:thread.join(5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(sum(item[0]=="ok" for item in results),1)
        self.assertEqual(sum(item[0]=="conflict" for item in results),1)
        with jobs.database() as db:
            owned=db.execute("SELECT COUNT(*) FROM job_match_sessions WHERE user_id=? AND status IN ('queued','running','paused') AND owner_token<>''",(jobs.USER,)).fetchone()[0]
        self.assertEqual(owned,1)

    def test_two_processes_cannot_acquire_distinct_matching_workers(self):
        from fastapi import HTTPException
        ctx=multiprocessing.get_context("fork")
        barrier=ctx.Barrier(2);results=ctx.Queue()
        fake_job={"id":42,"title":"Software Engineer","content":"Python required.",
                  "absolute_url":"https://boards.greenhouse.io/cloudflare/jobs/42",
                  "location":{"name":"San Francisco, CA, United States"},"_source_platform":"greenhouse",
                  "company_name":"Cloudflare","_search_prompt":"software engineering jobs"}
        with patch.object(matcher,"_find_feed_job",return_value=(fake_job,None)), \
             patch.object(matcher.executor,"submit",return_value=None):
            processes=[ctx.Process(target=_process_match_claim,args=(str(jobs.DB),barrier,results)) for _ in range(2)]
            for process in processes:process.start()
            outcomes=[results.get(timeout=10) for _ in processes]
            for process in processes:process.join(10)
        self.assertTrue(all(process.exitcode==0 for process in processes))
        self.assertEqual(sum(result[0]=="ok" for result in outcomes),1)
        self.assertEqual(sum(result[0]=="error" for result in outcomes),1)
        with jobs.database() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM job_match_sessions WHERE status IN ('queued','running','paused') AND owner_token<>''").fetchone()[0],1)

    def test_twelve_discovered_jobs_match_in_one_owned_workflow_session(self):
        keys=[f"greenhouse:cloudflare:batch-{i}" for i in range(12)]
        with jobs.database() as db:
            db.execute("CREATE TABLE user_workflows(workflow_id TEXT,user_id TEXT,resume_id TEXT,current_stage TEXT)")
            db.execute("INSERT INTO user_workflows VALUES('workflow-12',?,'resume-v1','matching')",(jobs.USER,))
            for i,key in enumerate(keys):
                db.execute("""INSERT INTO discovered_openings(user_id,job_key,company,title,location,url,discovered_at)
                    VALUES(?,?,'Cloudflare','Software Engineer','San Francisco, CA, United States',?,?)""",
                    (jobs.USER,key,f"https://boards.greenhouse.io/cloudflare/jobs/{i}","2026-10-20"))
        def fake_find(key,_cache):
            external=key.rsplit(':',1)[-1]
            return ({"id":external,"title":"Software Engineer","content":"Python experience required.",
                "absolute_url":f"https://boards.greenhouse.io/cloudflare/jobs/{external}",
                "location":{"name":"San Francisco, CA, United States"},"employment_type":"Full-time",
                "country":"United States","workplaceType":"On-site","_source_platform":"greenhouse",
                "company_name":"Cloudflare","_search_prompt":"software engineering jobs"},None)
        with patch.object(matcher,"_find_feed_job",side_effect=fake_find), \
             patch.object(matcher,"analyze_with_ollama",return_value=valid_result()) as model:
            response=matcher.analyze_matches(matcher.MatchRequest(job_keys=keys,batch_size=3),workflow_id="workflow-12")
            self.assertEqual(response["status"],"queued")
            for _ in range(500):
                progress=matcher._progress(response["session_id"])
                if progress["status"] in {"completed","paused","interrupted"}:break
                time.sleep(.01)
        self.assertEqual(progress["status"],"completed",progress)
        self.assertEqual(progress["completed"],12)
        self.assertEqual(progress["pending"],0)
        self.assertEqual(model.call_count,12)
        self.assertEqual({item["job_key"] for item in progress["results"]},set(keys))
        retry=matcher.analyze_matches(matcher.MatchRequest(job_keys=keys,batch_size=3),workflow_id="workflow-12")
        self.assertEqual(retry["session_id"],response["session_id"])
        self.assertEqual(retry["status"],"completed")
        self.assertEqual(model.call_count,12)
        with jobs.database() as db:
            row=db.execute("SELECT workflow_id,status FROM job_match_sessions WHERE session_id=?",(response["session_id"],)).fetchone()
            saved=db.execute("SELECT COUNT(*) FROM job_match_results WHERE user_id=? AND job_key IN (SELECT value FROM json_each(?)) AND status='completed'",
                             (jobs.USER,json.dumps(keys))).fetchone()[0]
        self.assertEqual(tuple(row),("workflow-12","completed"))
        self.assertEqual(saved,12)

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

    def test_session_pause_resume_preserves_completed_jobs_and_ownership(self):
        for job_id in (142, 143, 144, 145):
            with jobs.database() as db:
                db.execute("INSERT INTO discovered_openings (user_id,job_key,company,title,location,url,discovered_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (
                    jobs.USER, f"greenhouse:cloudflare:{job_id}", "Cloudflare", "Software Engineer",
                    "San Francisco, CA, United States", f"https://boards.greenhouse.io/cloudflare/jobs/{job_id}", "2026-01-01"))
        feed = [{"id": job_id, "title": "Software Engineer", "content": "Python required."} for job_id in (142, 143, 144, 145)]
        model_started=threading.Event();continue_model=threading.Event();calls=[]
        def model_call(*_args):
            calls.append(1)
            if len(calls)==1:
                model_started.set();continue_model.wait(5)
            return valid_result()
        with patch.object(matcher, "fetch_board", return_value=(feed, None)), \
             patch.object(matcher, "analyze_with_ollama", side_effect=model_call) as model:
            response = matcher.analyze_matches(matcher.MatchRequest(job_keys=[f"greenhouse:cloudflare:{i}" for i in (142, 143, 144, 145)], batch_size=3))
            self.assertTrue(model_started.wait(2))
            matcher.pause_matches(session_id=response["session_id"])
            continue_model.set()
            for _ in range(100):
                progress = matcher._progress(response["session_id"])
                if progress["status"] == "paused" and progress["owner_state"]=="unowned":
                    break
                time.sleep(0.01)
            self.assertEqual(progress["completed"], 1)
            self.assertEqual(progress["pending"], 3)
            matcher.resume_matches(session_id=response["session_id"])
            for _ in range(100):
                progress = matcher._progress(response["session_id"])
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
