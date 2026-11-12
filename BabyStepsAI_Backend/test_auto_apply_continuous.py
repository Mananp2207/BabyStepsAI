"""One continuous, isolated Auto Apply journey through real routes and Chromium.

The transport adapter below is intentionally test-only.  It is constructed
inside this module, accepts one exact loopback origin and path, and patches
transport dependencies only for the duration of a single test.  Production
URL, robots, and browser guards are not changed.
"""
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import asyncio
from io import BytesIO
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

from fastapi import UploadFile, HTTPException
from starlette.requests import Request

import application_agent as app_agent
import application_answer_assistant as answer_assistant
import candidate_verification as verification
import career_preferences
import job_description_enricher
import job_search_agent as jobs
import main
import resume_job_matcher as matcher
import workflow_orchestrator as workflows


class ExactSyntheticTransport:
    """Allow one exact local form target; reject every other destination."""
    def __init__(self, origin, path):
        self.origin = origin.rstrip("/")
        self.path = path
        self.url = self.origin + path

    def permits(self, url):
        parsed = urlparse(str(url or ""))
        return (parsed.scheme == "http" and
                f"{parsed.scheme}://{parsed.netloc}" == self.origin and
                parsed.path == self.path and not parsed.query and
                not parsed.fragment and not parsed.username and not parsed.password)

    def activate(self, stack):
        original_validate = app_agent.validate_application_url
        original_robots = app_agent._robots_permission
        original_guard = app_agent._guard_browser_request
        original_source_url = jobs.source_application_url

        def validate(url):
            if self.permits(url):
                return None
            return original_validate(url)

        def robots(url):
            if self.permits(url):
                return True, "Synthetic form is restricted to this isolated test origin.", "test_allow"
            return original_robots(url)

        def guard(route, policy_state=None):
            request_url = getattr(getattr(route, "request", None), "url", "")
            if self.permits(request_url):
                return route.continue_()
            return original_guard(route, policy_state)

        def map_official_feed_url(value, source):
            trusted = original_source_url(value, source)
            parsed = urlparse(trusted or "")
            if (trusted and parsed.hostname == "jobs.ashbyhq.com" and
                    parsed.path.rstrip("/") == "/synthetic-board/req-42"):
                return self.url
            return trusted

        stack.enter_context(patch.object(app_agent, "validate_application_url", side_effect=validate))
        stack.enter_context(patch.object(app_agent, "_robots_permission", side_effect=robots))
        stack.enter_context(patch.object(app_agent, "_guard_browser_request", side_effect=guard))
        stack.enter_context(patch.object(jobs, "source_application_url", side_effect=map_official_feed_url))
        return self


@unittest.skipUnless(os.getenv("BABYSTEPS_RUN_REAL_CHROMIUM") == "1",
                     "Set BABYSTEPS_RUN_REAL_CHROMIUM=1 to run the continuous Chromium integration.")
class ContinuousAutoApplyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db = root / "synthetic.sqlite3"
        self.storage = root / "resumes"
        self.storage.mkdir()
        self.artifacts = root / "artifacts"
        self.artifacts.mkdir()
        self.modules = (main, jobs, career_preferences, verification, answer_assistant,
                        app_agent, workflows, job_description_enricher)
        self.old_dbs = {module: module.DB for module in self.modules}
        for module in self.modules:
            module.DB = self.db
        self.old_storage = (main.STORAGE, app_agent.STORAGE, app_agent.ARTIFACTS)
        main.STORAGE = app_agent.STORAGE = self.storage
        app_agent.ARTIFACTS = self.artifacts
        self.old_verification_file = verification.__file__
        verification.__file__ = str(root / "candidate_verification.py")
        (root / "private_resumes").mkdir()
        self.old_key = os.environ.get("BABYSTEPSAI_KEY_PATH")
        os.environ["BABYSTEPSAI_KEY_PATH"] = str(root / "synthetic-encryption.key")

        with sqlite3.connect(self.db) as db:
            db.executescript("""
                CREATE TABLE resumes(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,original_name TEXT NOT NULL,
                    stored_name TEXT NOT NULL,created_at TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 0,
                    extracted_text TEXT NOT NULL DEFAULT '');
                CREATE TABLE resume_profiles(resume_id TEXT PRIMARY KEY,status TEXT NOT NULL,
                    profile_json TEXT,error TEXT,updated_at TEXT NOT NULL);
                CREATE TABLE career_preferences(user_id TEXT PRIMARY KEY,preferences_json TEXT NOT NULL,
                    preference_version INTEGER NOT NULL DEFAULT 1,preferences_fingerprint TEXT NOT NULL DEFAULT '');
            """)
        jobs.initialize()
        verification.initialize()
        answer_assistant.initialize()
        app_agent.initialize()
        matcher.initialize()
        workflows.initialize()

    def tearDown(self):
        for module, db_path in self.old_dbs.items():
            module.DB = db_path
        main.STORAGE, app_agent.STORAGE, app_agent.ARTIFACTS = self.old_storage
        verification.__file__ = self.old_verification_file
        if self.old_key is None:
            os.environ.pop("BABYSTEPSAI_KEY_PATH", None)
        else:
            os.environ["BABYSTEPSAI_KEY_PATH"] = self.old_key
        self.temp.cleanup()

    def test_continuous_synthetic_auto_apply_journey(self):
        posts = []
        redirect_served = []
        form = b'''<!doctype html><html><head><title>Software Engineer Application</title></head>
        <body><h1>Synthetic Employer</h1><form method="post">
        <label>Full Name <input name="name" required></label>
        <label>Email <input type="email" name="email" required></label>
        <label>Phone <input name="phone" required></label>
        <label>Resume <input type="file" name="resume" accept=".pdf" required></label>
        <button type="submit">Submit application</button></form></body></html>'''

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/synthetic-employer/apply/req-42":
                    if self.headers.get("X-Synthetic-Redirect") == "1":
                        redirect_served.append(self.path)
                        self.send_response(302)
                        self.send_header("Location", "http://127.0.0.1:9/private")
                        self.end_headers()
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(form)
                else:
                    self.send_error(404)

            def do_POST(self):
                posts.append(self.path)
                self.send_error(405)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        adapter = ExactSyntheticTransport(f"http://127.0.0.1:{port}",
                                          "/synthetic-employer/apply/req-42")
        try:
            # Verify unchanged production guards before injecting the narrow test transport.
            with self.assertRaises(ValueError):
                app_agent.validate_application_url(adapter.url)
            self.assertFalse(adapter.permits(f"http://127.0.0.1:{port + 1}{adapter.path}"))
            self.assertFalse(adapter.permits("http://10.0.0.8/synthetic-employer/apply/req-42"))
            self.assertFalse(adapter.permits(f"http://127.0.0.1:{port}/other"))

            with ExitStack() as stack:
                adapter.activate(stack)
                with patch.object(main, "extract_pdf_text", return_value="Synthetic Candidate\nPython developer"), \
                     patch.object(main, "enqueue", return_value={"status": "queued"}):
                    uploaded = asyncio.run(main.upload_resume(UploadFile(filename="synthetic.pdf",
                                                                          file=BytesIO(b"synthetic-pdf-bytes"))))
                resume_id = uploaded["id"]
                with jobs.database() as db:
                    row = db.execute("SELECT active,stored_name FROM resumes WHERE id=?", (resume_id,)).fetchone()
                self.assertEqual(row["active"], 1)
                self.assertTrue((self.storage / row["stored_name"]).is_file())
                (Path(verification.__file__).resolve().parent / "private_resumes" / row["stored_name"]).write_bytes(
                    (self.storage / row["stored_name"]).read_bytes())

                profile_payload = {key: [] for key in main.PROFILE_FIELDS}
                profile_payload.update({"education": [{"degree": "B.S.", "major": "Computer Science"}],
                                        "technical_skills": ["Python"]})
                saved_profile = main.save_profile(resume_id, profile_payload)
                self.assertEqual(saved_profile["status"], "user_reviewed")
                with jobs.database() as db:
                    row = db.execute("SELECT status,profile_json FROM resume_profiles WHERE resume_id=?", (resume_id,)).fetchone()
                self.assertEqual(row["status"], "user_reviewed")
                self.assertTrue(json.loads(row["profile_json"])["technical_skills"])

                confirmed = verification.update_verification(verification.VerificationUpdate(values={
                    "full_name": "Synthetic Candidate", "email": "synthetic@example.test",
                    "phone": "555-0102", "university": "Synthetic University",
                    "degree": "B.S.", "major": "Computer Science", "skills": ["Python"]},
                    confirmed_fields=["full_name", "email", "phone", "university", "degree", "major", "skills"]))
                self.assertTrue(confirmed.get("saved", True))
                with jobs.database() as db:
                    row = db.execute("SELECT confirmed_json FROM candidate_verifications WHERE resume_id=?", (resume_id,)).fetchone()
                self.assertTrue(json.loads(row["confirmed_json"])["full_name"])
                _active, verified_candidate, _answers = app_agent._candidate()
                self.assertEqual(verified_candidate.get("full_name"), "Synthetic Candidate")
                self.assertEqual(verified_candidate.get("email"), "synthetic@example.test")

                pref_data = {"categories": ["Software Development"], "roles": ["Software Engineer"],
                             "levels": ["Entry Level"], "employment_types": ["Full-time"],
                             "location": "United States", "work_arrangement": "Remote",
                             "relocation_preference": "anywhere"}
                pref_response = career_preferences.save_preferences(career_preferences.Preferences(**pref_data))
                self.assertTrue(pref_response["saved"])
                self.assertEqual(career_preferences.get_preferences()["roles"], ["Software Engineer"])

                policy_data = {"standing_policy_enabled": True,
                    "standing_policy_categories": ["Software Development"],
                    "standing_policy_employment_types": ["Full-time"],
                    "standing_policy_countries": ["United States"],
                    "standing_policy_locations": ["Anywhere"],
                    "standing_policy_work_arrangements": ["Remote"],
                    "standing_policy_experience_levels": ["Entry Level"],
                    "standing_policy_daily_limit": 1,
                    "standing_policy_field_keys": ["full_name", "email", "phone"],
                    "standing_policy_document_types": ["resume"],
                    "standing_policy_reuse_confirmed_answers": False,
                    "standing_policy_allow_ai_answers": False,
                    "standing_policy_allow_cloud_ai": False}
                policy_response = answer_assistant.update_authorization(answer_assistant.AuthorizationUpdate(**policy_data))
                self.assertTrue(policy_response["settings"]["standing_policy_enabled"])
                self.assertFalse(policy_response["settings"]["live_submission_enabled"])

                stamp = app_agent.now()
                with jobs.database() as db:
                    # This is a synthetic, explicitly verified Ashby-style board record.
                    values = ("ashby:synthetic-board", "Synthetic Employer",
                        "https://jobs.ashbyhq.com/synthetic-board", "ashby", "synthetic-board",
                        "https://api.ashbyhq.com/posting-api/job-board/synthetic-board",
                        "United States", stamp, stamp, "healthy", "", 1, stamp, stamp,
                        "https://synthetic.example", "https://synthetic.example/careers",
                        "synthetic_fixture", "https://synthetic.example/careers", "", stamp,
                        "verified", "", 0, None, "")
                    db.execute("""INSERT INTO employers(employer_id,name,careers_url,source_type,source_token,
                        public_feed_url,geographic_coverage,last_success_at,last_attempt_at,connector_status,
                        error_details,active,created_at,updated_at,website_url,evidence_url,candidate_source,
                        candidate_record_url,candidate_license,discovered_at,discovery_status,health_reason,
                        consecutive_failures,next_retry_at,source_fingerprint)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", values)
                    source = db.execute("SELECT * FROM employers WHERE employer_id='ashby:synthetic-board'").fetchone()
                    db.execute("UPDATE employers SET source_fingerprint=? WHERE employer_id=?",
                               (jobs._source_fingerprint(dict(source)), "ashby:synthetic-board"))

                raw_job = {"id": "req-42", "title": "Entry Level Software Engineer",
                    "location": "Remote, United States", "country": "United States",
                    "employmentType": "Full-time", "workplaceType": "Remote", "isListed": True,
                    "descriptionHtml": "Entry level full-time software engineer. Build and maintain web software using Python. Apply to this current role.",
                    "jobUrl": "https://jobs.ashbyhq.com/synthetic-board/req-42",
                    "applyUrl": "https://jobs.ashbyhq.com/synthetic-board/req-42"}
                feed_fetch = patch.object(jobs, "fetch_source", return_value=([raw_job], None))
                stack.enter_context(feed_fetch)

                def synthetic_match(request, **_kwargs):
                    resume_key, _status, candidate = matcher.active_candidate()
                    prefs = career_preferences.get_preferences()
                    stamp = matcher.now()
                    with matcher.database() as db:
                        for key in request.job_keys:
                            opening = dict(db.execute("SELECT * FROM discovered_openings WHERE user_id=? AND job_key=?",
                                                      (matcher.USER, key)).fetchone())
                            result = {"classification": "potential_match", "missing_information": [],
                                      "required_skills": [], "required_experience": [],
                                      "education_requirements": [], "technical_qualifications": []}
                            db.execute("""INSERT OR REPLACE INTO job_match_results(user_id,job_key,resume_id,status,
                                classification,match_score,result_json,created_at,updated_at,candidate_fingerprint,
                                preferences_fingerprint,job_fingerprint,matching_model_version,freshness_status)
                                VALUES(?,?,?,'completed','potential_match',0,?,?,?,?,?,?,?,'current')""",
                                (matcher.USER, key, resume_key, json.dumps(result), stamp, stamp,
                                 matcher.profile_fingerprint(candidate, resume_key),
                                 matcher.stable_fingerprint(prefs), matcher.job_fingerprint(opening),
                                 matcher.MATCHING_ENGINE_VERSION))
                    return {"status": "completed", "total": len(request.job_keys)}

                stack.enter_context(patch.object(matcher, "analyze_matches", side_effect=synthetic_match))
                # Keep the actual worker pool so ownership locks and lifecycle behave as in the app.
                stack.enter_context(patch.object(app_agent.executor, "submit", side_effect=lambda fn, *args: fn(*args)))
                browser_inputs = []
                original_fill_page = app_agent._fill_page
                def observe_fill(page, candidate, answers, resume_path, **kwargs):
                    observation={"candidate": dict(candidate),
                        "answers": {"name": app_agent._answer({"label":"Full Name"},candidate,answers),
                                    "email": app_agent._answer({"label":"Email"},candidate,answers),
                                    "phone": app_agent._answer({"label":"Phone"},candidate,answers)}}
                    browser_inputs.append(observation)
                    result=original_fill_page(page,candidate,answers,resume_path,**kwargs)
                    observation["readback"]={name:page.locator(f"input[name='{name}']").input_value()
                                              for name in ("name","email","phone")}
                    observation["resume_upload_count"]=page.locator("input[type=file]").evaluate("el=>el.files?.length||0")
                    return result
                stack.enter_context(patch.object(app_agent, "_fill_page", side_effect=observe_fill))

                self.assertTrue(workflows._standing_policy_current())
                workflows._versions()
                scheduler_status = workflows.run_auto_apply_scheduler_cycle()
                deadline = time.monotonic() + 30
                workflow_rows = []
                while time.monotonic() < deadline:
                    with workflows.database() as db:
                        workflow_rows = [dict(row) for row in db.execute(
                            "SELECT workflow_id,status,current_stage,diagnostics_json FROM user_workflows ORDER BY created_at DESC")]
                    if workflow_rows and workflow_rows[0]["status"] not in {"queued", "running"}:
                        break
                    time.sleep(.05)
                with workflows.database() as db:
                    workflow_rows = [dict(row) for row in db.execute("SELECT workflow_id,status,current_stage,diagnostics_json FROM user_workflows")]
                self.assertEqual(scheduler_status, "started", json.dumps(workflow_rows))
                self.assertTrue(workflow_rows, "scheduler created no persisted workflow")
                self.assertEqual(workflow_rows[-1]["status"], "completed", json.dumps(workflow_rows))
                self.assertEqual(workflows.run_auto_apply_scheduler_cycle(), "cooldown")
                with jobs.database() as db:
                    opening = db.execute("SELECT * FROM discovered_openings WHERE job_key='ashby:synthetic-board:req-42'").fetchone()
                    match = db.execute("SELECT status,classification,freshness_status,matching_model_version FROM job_match_results WHERE job_key=?",
                                        ("ashby:synthetic-board:req-42",)).fetchone()
                    app = db.execute("SELECT id,state,attempt_number,diagnostic_json FROM applications WHERE job_key=?",
                                     ("ashby:synthetic-board:req-42",)).fetchone()
                    if not app:
                        self.fail("Scheduler workflow did not create an application: " + json.dumps(workflow_rows))
                    grant = db.execute("SELECT authorization_source,field_keys_json,document_types_json FROM application_preparation_authorizations WHERE job_key=? AND revoked_at IS NULL",
                                       ("ashby:synthetic-board:req-42",)).fetchone()
                    reservation_count = db.execute("SELECT COUNT(*) FROM application_opening_reservations WHERE application_id=?",
                                                    (app["id"],)).fetchone()[0]
                    policy_job_count = db.execute("SELECT COUNT(DISTINCT job_key) FROM standing_policy_events WHERE user_id=?",
                                                  (app_agent.USER,)).fetchone()[0]
                self.assertIsNotNone(opening)
                self.assertEqual(opening["availability_status"], "current")
                self.assertEqual(opening["url"], adapter.url)
                self.assertEqual(match["status"], "completed")
                self.assertEqual(match["classification"], "potential_match")
                self.assertEqual(match["freshness_status"], "current")
                self.assertEqual(match["matching_model_version"], matcher.MATCHING_ENGINE_VERSION)
                current_scope = app_agent._current_preparation_authorization("ashby:synthetic-board:req-42", resume_id, adapter.url)
                scoped = app_agent._scoped_candidate(verified_candidate, current_scope or {})
                self.assertEqual(app["state"], "ready_for_review", json.dumps({"diagnostic":json.loads(app["diagnostic_json"]),
                    "grant":current_scope,"scoped_candidate":scoped,"browser_inputs":browser_inputs}))
                self.assertEqual(app["attempt_number"], 1)
                self.assertEqual(reservation_count, 1)
                self.assertEqual(policy_job_count, 1)
                self.assertEqual(grant["authorization_source"], "standing_policy")
                self.assertEqual(json.loads(grant["field_keys_json"]), ["email", "full_name", "phone"])
                self.assertEqual(json.loads(grant["document_types_json"]), ["resume"])
                diagnostic = json.loads(app["diagnostic_json"])
                self.assertFalse(diagnostic["submission_performed"])
                self.assertEqual({x["source"] for x in diagnostic["filled_fields"] if x["result"] == "filled"},
                                 {"verified_profile", "active_resume"})
                self.assertEqual(browser_inputs[0]["readback"], {"name":"Synthetic Candidate",
                    "email":"synthetic@example.test","phone":"555-0102"})
                self.assertEqual(browser_inputs[0]["resume_upload_count"], 1)
                self.assertFalse(posts)

                # Rediscovery and a second scheduler pass cannot create another reservation or attempt.
                with workflows.database() as db:
                    db.execute("UPDATE auto_apply_scheduler_state SET last_started_at='2000-01-01T00:00:00+00:00' WHERE user_id=?",
                               (workflows.USER,))
                self.assertEqual(workflows.run_auto_apply_scheduler_cycle(), "started")
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    with workflows.database() as db:
                        latest = db.execute("SELECT status FROM user_workflows ORDER BY created_at DESC LIMIT 1").fetchone()
                    if latest and latest["status"] not in {"queued", "running"}:
                        break
                    time.sleep(.05)
                with jobs.database() as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM applications WHERE job_key=?",
                                                ("ashby:synthetic-board:req-42",)).fetchone()[0], 1)
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM application_attempts a JOIN applications x ON x.id=a.application_id WHERE x.job_key=?",
                                                ("ashby:synthetic-board:req-42",)).fetchone()[0], 1)

                app_id = app["id"]
                outcome = app_agent.record_manual_submission_outcome(app_id,
                    app_agent.ManualSubmissionOutcomeRequest(outcome="not_submitted", confirmed=True,
                                                             event_id="synthetic-outcome-1"))
                self.assertEqual(outcome["state"], "ready_for_review")
                with jobs.database() as db:
                    ledger = db.execute("SELECT outcome,confirmed_by_applicant FROM manual_submission_events WHERE event_id='synthetic-outcome-1'").fetchone()
                    state = db.execute("SELECT state,submitted_at,diagnostic_json FROM applications WHERE id=?", (app_id,)).fetchone()
                self.assertEqual(ledger["outcome"], "not_submitted")
                self.assertEqual(ledger["confirmed_by_applicant"], 1)
                self.assertEqual(state["state"], "ready_for_review")
                self.assertIsNone(state["submitted_at"])
                self.assertFalse(json.loads(state["diagnostic_json"])["submission_performed"])
                self.assertFalse(posts)

                # Policy revocation blocks subsequent automatic queueing and scheduler cycles.
                revoked = answer_assistant.update_authorization(answer_assistant.AuthorizationUpdate())
                self.assertFalse(revoked["settings"]["standing_policy_enabled"])
                with self.assertRaises(HTTPException) as blocked:
                    app_agent.enqueue_saved_jobs(app_agent.ApplicationQueueRequest(job_keys=["ashby:synthetic-board:req-42"]))
                self.assertEqual(blocked.exception.status_code, 403)
                self.assertEqual(workflows.run_auto_apply_scheduler_cycle(), "disabled_or_stale")

                # A redirect is observed by Chromium's route guard and denied outside this exact path.
                from playwright.sync_api import sync_playwright
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    context = browser.new_context(extra_http_headers={"X-Synthetic-Redirect":"1"})
                    page = context.new_page()
                    guarded_requests = []
                    def guard_redirect(route):
                        guarded_requests.append(route.request.url)
                        return app_agent._guard_browser_request(route, {})
                    page.route("**/*", guard_redirect)
                    with self.assertRaises(Exception):
                        page.goto(adapter.url, wait_until="domcontentloaded", timeout=3000)
                    context.close()
                    browser.close()
                self.assertTrue(redirect_served)
                self.assertEqual(guarded_requests, [adapter.url])

            # After adapter destruction the production guard still rejects the synthetic loopback form.
            with self.assertRaises(ValueError):
                app_agent.validate_application_url(adapter.url)
            self.assertFalse(posts)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        # Playwright follows HTTP redirects inside Chromium without invoking
        # the route callback again for every redirect hop. Exercise the actual
        # production request guard against the redirected URL independently;
        # it must abort before any page code could run there.
        class RedirectRequest:
            method = "GET"
            url = "http://127.0.0.1:9/private"
            def is_navigation_request(self): return True
        class RedirectRoute:
            request = RedirectRequest()
            aborted = False
            def abort(self): self.aborted = True
        redirect_route = RedirectRoute()
        app_agent._guard_browser_request(redirect_route, {})
        self.assertTrue(redirect_route.aborted)


if __name__ == "__main__":
    unittest.main()
