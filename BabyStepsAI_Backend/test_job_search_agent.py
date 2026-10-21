import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import HTTPException

import job_search_agent as jobs
from job_connectors import EmployerSource
from job_relevance import evaluate_location, extract_experience_requirements, infer_prompt_preferences


def posting(job_id, title, **overrides):
    value = {
        "id": job_id,
        "title": title,
        "absolute_url": f"https://boards.greenhouse.io/example/jobs/{job_id}",
        "location": {"name": "San Francisco, CA, United States"},
        "metadata": [{"name": "Employment Type", "value": "Full-time"}],
        "content": "",
    }
    value.update(overrides)
    return value


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_db = jobs.DB
        jobs.DB = Path(self.tempdir.name) / "test.sqlite3"
        jobs.initialize()
        with jobs.database() as db:
            db.execute("CREATE TABLE career_preferences (user_id TEXT PRIMARY KEY, preferences_json TEXT NOT NULL)")
            db.execute("INSERT INTO career_preferences VALUES (?, ?)", (jobs.USER, json.dumps(self.prefs())))
            stamp=jobs.datetime.now(jobs.timezone.utc).isoformat()
            db.execute("UPDATE employers SET connector_status='healthy',discovery_status='verified',last_success_at=? WHERE source_type='greenhouse'",(stamp,))
            for row in db.execute("SELECT * FROM employers WHERE source_type='greenhouse'").fetchall():
                db.execute("UPDATE employers SET source_fingerprint=? WHERE employer_id=?",(jobs._source_fingerprint(dict(row)),row["employer_id"]))

    def tearDown(self):
        jobs.DB = self.old_db
        self.tempdir.cleanup()

    @staticmethod
    def prefs(**changes):
        result = {
            "roles": ["Software Engineer"], "categories": [],
            "levels": ["New Graduate", "Entry Level", "Junior", "Associate"],
            "employment_types": ["Full-time"], "location": "United States",
            "work_arrangement": "Any", "instructions": "",
        }
        result.update(changes)
        return result

    def _mock_feeds(self, items_by_board):
        def fetch(company, board):
            value = items_by_board.get(board, [])
            if isinstance(value, Exception):
                return [], str(value)
            return value, None
        return patch.object(jobs, "fetch_board", side_effect=fetch)

    def test_related_software_titles_are_kept_for_review(self):
        prefs = jobs.preferences()
        terms = jobs.search_terms("new grad, entry-level, junior, associate software engineering positions", prefs)
        for title in ("Software Engineer", "Backend Engineer", "Full Stack Engineer", "Software Developer"):
            accepted, reason, review_reasons = jobs.assess_job(posting(title, title), prefs, terms)
            self.assertTrue(accepted, title)
            self.assertIsNone(reason)
            self.assertTrue(review_reasons, title)

    def test_saved_job_discovery_requires_explicit_authorization(self):
        with self.assertRaises(HTTPException) as caught:
            jobs.search_jobs(jobs.SearchRequest(prompt="Software Engineer"))
        self.assertEqual(caught.exception.status_code,403)
        self.assertIn("Application settings",caught.exception.detail)

    def test_business_development_is_rejected_even_when_language_contains_swe(self):
        prefs = self.prefs()
        terms = jobs.search_terms("all software engineering roles", prefs)
        accepted, reason, _ = jobs.assess_job(posting(71, "Business Development Representative - Danish, Norwegian or Swedish Speaking"), prefs, terms)
        self.assertFalse(accepted)
        self.assertEqual(reason, "role")

    def test_sales_engineer_requires_an_explicit_selected_title(self):
        prefs = self.prefs()
        accepted, reason, _ = jobs.assess_job(posting(73, "Sales Engineer"), prefs, jobs.search_terms("", prefs))
        self.assertFalse(accepted)
        self.assertEqual(reason, "role")
        explicit = self.prefs(roles=["Sales Engineer"])
        accepted, _, _ = jobs.assess_job(posting(74, "Sales Engineer"), explicit, jobs.search_terms("", explicit))
        self.assertTrue(accepted)
        restricted = self.prefs(roles=["Sales Engineer"], instructions="No sales roles")
        accepted, reason, _ = jobs.assess_job(posting(76, "Sales Engineer"), restricted,
                                              jobs.search_terms("", restricted))
        self.assertFalse(accepted)
        self.assertEqual(reason, "instructions")

    def test_customer_solutions_and_security_research_titles_do_not_leak_into_software(self):
        prefs = self.prefs()
        terms = jobs.search_terms("software engineering jobs in United States", prefs)
        for title in ("Customer Engineer", "Solutions Engineer", "AI Security Research & Red Team Engineer"):
            accepted, reason, _ = jobs.assess_job(posting(75, title, content="Python and cloud experience preferred."), prefs, terms)
            self.assertFalse(accepted, title)
            self.assertEqual(reason, "role")

    def test_engineer_keyword_alone_does_not_match_jobs(self):
        from job_relevance import relevance_for_job
        relevant, _, _ = relevance_for_job({"title": "Systems Engineer", "description": ""}, {"roles": ["Engineer"], "categories": []})
        self.assertFalse(relevant)

    def test_multiple_selected_categories_are_combined(self):
        prefs = self.prefs(roles=[], categories=["Software Development", "Cybersecurity", "Artificial Intelligence"])
        terms = jobs.search_terms("roles", prefs)
        for title in ("Backend Engineer", "Cybersecurity Analyst", "Machine Learning Engineer", "AI Security Research & Red Team Engineer"):
            accepted, _, _ = jobs.assess_job(posting(72, title), prefs, terms)
            self.assertTrue(accepted, title)

    def test_us_geography_accepts_us_cities_remote_and_multilocation(self):
        prefs = self.prefs()
        terms = jobs.search_terms("", prefs)
        for loc in ("Austin, Texas", "Remote US", "London, UK; Austin, TX"):
            posting_value = posting(80, "Software Engineer", location={"name": loc})
            self.assertEqual(evaluate_location(posting_value, "United States")[0], "eligible", loc)
            self.assertTrue(jobs.assess_job(posting_value, prefs, terms)[0], loc)

    def test_us_geography_rejects_korea_and_london_even_when_role_is_explicit(self):
        customer_prefs = self.prefs(roles=["Customer Engineer"])
        accepted, reason, _ = jobs.assess_job(posting(81, "Customer Engineer, Korea", location={"name": "Seoul, Korea"}),
                                              customer_prefs, jobs.search_terms("", customer_prefs))
        self.assertFalse(accepted)
        self.assertEqual(reason, "location")
        software_prefs = self.prefs()
        accepted, reason, _ = jobs.assess_job(posting(82, "Software Engineer, London, United Kingdom",
                                                      location={"name": "London, United Kingdom"}),
                                              software_prefs, jobs.search_terms("", software_prefs))
        self.assertFalse(accepted)
        self.assertEqual(reason, "location")

    def test_arrangement_only_location_is_review_not_rejection(self):
        prefs = self.prefs()
        accepted, reason, notes = jobs.assess_job(posting(82, "Software Engineer", location={"name": "Hybrid"}), prefs,
                                                  jobs.search_terms("", prefs))
        self.assertTrue(accepted)
        self.assertIsNone(reason)
        self.assertTrue(any("Country cannot be verified" in note for note in notes))
        self.assertEqual(evaluate_location({"location": "In-Office", "title": "Software Engineer"}, "United States")[0], "unknown")

    def test_structured_metadata_and_description_location_are_used(self):
        metadata_job = posting(83, "Software Engineer", location={"name": "Hybrid"},
                              metadata=[{"name": "Country", "value": "United States"}])
        description_job = posting(84, "Software Engineer", location={"name": "Hybrid"},
                                  content="Work location: Austin, Texas. Hybrid role.")
        self.assertEqual(evaluate_location(metadata_job, "United States")[0], "eligible")
        self.assertEqual(evaluate_location(description_job, "United States")[0], "eligible")

    def test_saved_location_and_level_restrictions_override_prompt_hints(self):
        explicit = self.prefs(location="United States", levels=["Senior"])
        effective = infer_prompt_preferences("new grad roles in Korea", explicit)
        self.assertEqual(effective["location"], "United States")
        self.assertEqual(effective["levels"], ["Senior"])
        inferred = infer_prompt_preferences("software engineering jobs for new grads 2026 in United States", self.prefs(location="Any", levels=[]))
        self.assertEqual(inferred["location"], "United States")
        self.assertIn("New Graduate", inferred["levels"])

    def test_experience_extractor_separates_required_preferred_and_unrelated_years(self):
        extracted = extract_experience_requirements(
            "Must have 3 years of professional experience. 2 years of experience is preferred. The office has served Austin for 15 years.")
        self.assertEqual(len(extracted["required"]), 1)
        self.assertEqual(extracted["required"][0]["years"], "3")
        self.assertEqual(len(extracted["preferred"]), 1)
        self.assertEqual(extracted["preferred"][0]["years"], "2")

    def test_experience_extractor_uses_requirement_section_and_not_following_ideally_modifier(self):
        extracted=extract_experience_requirements(
            "THE SKILLSET YOU'LL BRING 3+ years experience in software development, ideally some of which at startups. "
            "NICE TO HAVE 2 years of experience with cloud systems.")
        self.assertEqual([item["years"] for item in extracted["required"]],["3+"])
        self.assertEqual([item["years"] for item in extracted["preferred"]],["2"])
        enterprise=extract_experience_requirements("THE SKILLSET YOU'LL BRING 2–8 years of professional software engineering experience on high impact teams.")
        self.assertEqual([item["years"] for item in enterprise["required"]],["2–8"])

    def test_required_years_without_confirmed_experience_need_review_not_automatic_rejection(self):
        prefs = self.prefs()
        terms = jobs.search_terms("", prefs)
        required = posting(85, "Software Engineer", content="Must have at least 8 years of professional experience.")
        preferred = posting(86, "Software Engineer", content="8 years of professional experience preferred.")
        accepted, reason, reasons = jobs.assess_job(required, prefs, terms)
        self.assertTrue(accepted)
        self.assertIsNone(reason)
        self.assertTrue(any("confirmed professional experience is unavailable" in item for item in reasons))
        self.assertTrue(jobs.assess_job(preferred, prefs, terms)[0])

    def test_confirmed_experience_conflict_is_excluded_but_matching_experience_is_not(self):
        prefs = self.prefs()
        terms = jobs.search_terms("", prefs)
        required = posting(85, "Software Engineer", content="Must have at least 3 years of professional experience.")
        self.assertEqual(jobs.assess_job(required, prefs, terms, confirmed_experience_years=2)[1], "experience_level")
        accepted, reason, _ = jobs.assess_job(required, prefs, terms, confirmed_experience_years=4)
        self.assertTrue(accepted)
        self.assertIsNone(reason)

    def test_experience_classification_graduation_window_and_ranking_are_explained(self):
        from job_relevance import (classify_experience_level, classify_career_category,
                                   discovery_relevance_score, extract_graduation_windows,
                                   extract_education_requirements)
        self.assertEqual(classify_experience_level("Software Engineer, New Grad")[0], "New Graduate")
        self.assertEqual(classify_experience_level("Software Engineer", "Requires 6+ years of professional experience.")[0], "Experienced")
        self.assertEqual(classify_experience_level("Software Engineer", "Requires 2-8 years of professional experience.")[0], "Ambiguous")
        self.assertEqual(extract_graduation_windows("Must graduate between 2025 and 2027.")[0]["end_year"], 2027)
        self.assertEqual(extract_graduation_windows("Class of 2026 graduates may apply.")[0]["start_year"], 2026)
        education = extract_education_requirements("Requirements. Bachelor's degree in computer science required. Nice to have a master's degree.")
        self.assertTrue(any("bachelor" in item.casefold() for item in education["required"]))
        self.assertTrue(any("master" in item.casefold() for item in education["preferred"]))
        self.assertEqual(classify_career_category("Software Engineer", {"categories": ["software development"]}), "Software Development")
        prefs = self.prefs(levels=["New Graduate"])
        early_score, explanation, _, _ = discovery_relevance_score(
            {"title": "Software Engineer, New Grad", "description": "Build products with Python.", "location_status": "eligible"}, prefs)
        vague_score, _, _, _ = discovery_relevance_score(
            {"title": "Software Engineer", "description": "", "location_status": "eligible"}, prefs)
        self.assertGreater(early_score, vague_score)
        self.assertTrue(any("matches a saved experience-level preference" in item for item in explanation))
        skill_score, skill_explanation, _, _ = discovery_relevance_score(
            {"title": "Software Engineer", "description": "Python experience preferred.", "location_status": "eligible"},
            prefs, ["Python"])
        self.assertGreater(skill_score, vague_score)
        self.assertTrue(any("confirmed candidate skill" in item for item in skill_explanation))

    def test_senior_level_is_supported_when_user_selects_it(self):
        prefs = self.prefs(levels=["Senior"])
        accepted, reason, _ = jobs.assess_job(posting(87, "Senior Software Engineer"), prefs, jobs.search_terms("", prefs))
        self.assertTrue(accepted)
        self.assertIsNone(reason)

    def test_resume_upload_and_career_preference_routes_remain_registered(self):
        import main
        paths = main.app.openapi()["paths"]
        self.assertIn("post", paths["/resumes"])
        self.assertIn("get", paths["/api/career-preferences"])
        self.assertIn("put", paths["/api/career-preferences"])

    def test_career_preferences_can_still_be_saved_and_retrieved(self):
        import career_preferences as career
        previous = career.DB
        try:
            career.DB = Path(self.tempdir.name) / "career-preferences.sqlite3"
            with career.connect() as db:
                db.execute("CREATE TABLE career_preferences (user_id TEXT PRIMARY KEY, preferences_json TEXT NOT NULL)")
            chosen = career.Preferences(categories=["Software Development", "Cybersecurity"],
                roles=["Backend Engineer"], levels=["New Graduate"], employment_types=["Full-time"],
                location="United States", work_arrangement="Remote", instructions="No sales roles")
            result = career.save_preferences(chosen)
            self.assertTrue(result["saved"])
            restored = career.get_preferences()
            self.assertEqual(restored["roles"], ["Backend Engineer"])
            self.assertEqual(restored["categories"], ["Software Development", "Cybersecurity"])
            self.assertEqual(restored["location"], "United States")
        finally:
            career.DB = previous

    def test_unclear_experience_is_needs_review_and_clear_conflict_rejected(self):
        prefs = jobs.preferences()
        terms = jobs.search_terms("software roles", prefs)
        accepted, _, reasons = jobs.assess_job(posting(1, "Software Engineer"), prefs, terms)
        self.assertTrue(accepted)
        self.assertTrue(any("Experience level" in item for item in reasons))
        accepted, reason, _ = jobs.assess_job(posting(2, "Senior Software Engineer"), prefs, terms)
        self.assertFalse(accepted)
        self.assertEqual(reason, "experience_level")

    def test_unknown_employment_metadata_is_reviewable_and_known_conflict_rejected(self):
        prefs = jobs.preferences()
        terms = jobs.search_terms("software roles", prefs)
        unknown = posting(1, "Software Engineer", metadata=[])
        accepted, _, reasons = jobs.assess_job(unknown, prefs, terms)
        self.assertTrue(accepted)
        self.assertTrue(any("Employment type" in item for item in reasons))
        contract = posting(2, "Software Engineer", metadata=[{"name": "Employment Type", "value": "Contract"}])
        accepted, reason, _ = jobs.assess_job(contract, prefs, terms)
        self.assertFalse(accepted)
        self.assertEqual(reason, "employment_type")

    def test_saved_preferences_outweigh_prompt_inference(self):
        items = [posting(1, "Senior Software Engineer"), posting(2, "Software Engineer Intern"), posting(3, "Software Engineer")]
        with self._mock_feeds({"cloudflare": items, "datadog": [], "figma": []}):
            result = jobs.discover("Senior internship software engineering jobs", jobs.preferences())
        self.assertEqual(result["status"], "preliminary_matches")
        self.assertEqual([row["title"] for row in result["results"]], ["Software Engineer"])

    def test_saved_location_and_instruction_restrictions_are_hard_filters(self):
        prefs = self.prefs(instructions="remote only")
        terms = jobs.search_terms("software roles", prefs)
        london = posting(10, "Software Engineer", location={"name": "London, England"})
        accepted, reason, _ = jobs.assess_job(london, prefs, terms)
        self.assertFalse(accepted)
        self.assertEqual(reason, "location")
        onsite = posting(11, "Software Engineer", location={"name": "In-Office, San Francisco, CA"})
        accepted, reason, _ = jobs.assess_job(onsite, prefs, terms)
        self.assertFalse(accepted)
        self.assertEqual(reason, "instructions")
        unknown_location = posting(12, "Software Engineer", location={"name": "San Francisco, CA"})
        accepted, _, review_reasons = jobs.assess_job(unknown_location, prefs, terms)
        self.assertTrue(accepted)
        self.assertTrue(any("Remote-only" in item for item in review_reasons))

    def test_all_sources_unavailable_has_reasoned_diagnostics(self):
        with self._mock_feeds({board: RuntimeError("DNS lookup failed") for board in jobs.BOARDS.values()}):
            result = jobs.discover("software roles", jobs.preferences())
        self.assertEqual(result["status"], "all_sources_unavailable")
        self.assertEqual(result["diagnostics"]["companies_checked"], 3)
        self.assertEqual(len(result["diagnostics"]["feed_failures"]), 3)
        self.assertIn("DNS lookup failed", result["diagnostics"]["feed_failures"][0]["reason"])

    def test_no_matching_openings_when_all_feeds_are_empty(self):
        with self._mock_feeds({}):
            result = jobs.discover("software roles", jobs.preferences())
        self.assertEqual(result["status"], "no_matching_openings")
        self.assertEqual(result["diagnostics"]["postings_retrieved"], 0)

    def test_restrictive_filters_report_rejection_counts(self):
        unrelated = [posting(i, "Product Manager") for i in range(1, 4)]
        with self._mock_feeds({"cloudflare": unrelated}):
            result = jobs.discover("software roles", jobs.preferences())
        self.assertEqual(result["status"], "filters_too_restrictive")
        self.assertEqual(result["diagnostics"]["rejected_by_filter"]["role"], 3)
        self.assertEqual(result["diagnostics"]["preliminary_matches"], 0)

    def test_successful_search_reports_counts_and_needs_review(self):
        with self._mock_feeds({"cloudflare": [posting(123, "Full Stack Engineer")]}):
            result = jobs.discover("software roles", jobs.preferences())
        self.assertEqual(result["status"], "preliminary_matches")
        self.assertEqual(result["diagnostics"]["companies_checked"], 3)
        self.assertEqual(result["diagnostics"]["postings_retrieved"], 1)
        self.assertEqual(result["diagnostics"]["preliminary_matches"], 1)
        self.assertEqual(result["results"][0]["review_status"], "needs_review")

    def test_type_error_in_one_employer_does_not_stop_remaining_feeds(self):
        sources=[EmployerSource(name,"greenhouse",token,f"https://boards.greenhouse.io/{token}")
                 for name,token in (("Broken Feed Co","broken"),("Healthy Feed One","healthy-one"),("Healthy Feed Two","healthy-two"))]
        board_jobs={
            "broken":[posting(1,"Software Engineer Broken")],
            "healthy-one":[posting(2,"Software Engineer One")],
            "healthy-two":[posting(3,"Software Engineer Two")],
        }
        original=jobs.assess_job
        def assess(job,prefs,terms):
            if job["title"]=="Software Engineer Broken":
                raise TypeError("malformed public feed field")
            return original(job,prefs,terms)
        with patch.object(jobs,"_employer_sources",return_value=sources), \
             patch.object(jobs,"fetch_board",side_effect=lambda _company,token:(board_jobs[token],None)), \
             patch.object(jobs,"assess_job",side_effect=assess):
            result=jobs.discover("software engineering jobs",self.prefs())
        self.assertEqual(result["status"],"preliminary_matches")
        self.assertEqual(result["diagnostics"]["sources_checked"],3)
        self.assertEqual(result["diagnostics"]["postings_retrieved"],3)
        self.assertEqual(result["count"],2)
        self.assertEqual({item["company"] for item in result["results"]},{"Healthy Feed One","Healthy Feed Two"})
        self.assertEqual(result["diagnostics"]["feed_failures"][0]["stage"],"job_normalization_and_filtering")
        self.assertIn("TypeError",result["diagnostics"]["feed_failures"][0]["reason"])
        saved=jobs.saved_jobs()
        self.assertEqual(saved["count"],2)

    def test_public_html_feed_fetch_type_error_is_diagnostic_and_isolated(self):
        sources=[EmployerSource("Etleap","public_html","","https://www.etleap.com/careers"),
                 EmployerSource("Healthy Feed","greenhouse","healthy","https://boards.greenhouse.io/healthy")]
        def fetch(source):
            if source.name=="Etleap":raise TypeError("malformed adapter value")
            return [posting(209,"Software Engineer")],None
        with patch.object(jobs,"_employer_sources",return_value=sources),patch.object(jobs,"fetch_source",side_effect=fetch):
            result=jobs.discover("software engineering jobs",self.prefs())
        self.assertEqual(result["status"],"preliminary_matches")
        self.assertEqual(result["diagnostics"]["sources_checked"],2)
        self.assertEqual(result["count"],1)
        failure=result["diagnostics"]["feed_failures"][0]
        self.assertEqual(failure["company"],"Etleap")
        self.assertEqual(failure["stage"],"feed_fetch")
        self.assertIn("TypeError during feed_fetch",failure["reason"])
        self.assertTrue(failure["diagnostic_ref"])

    def test_discovery_run_resumes_from_completed_source_checkpoint(self):
        first=EmployerSource("First Employer","greenhouse","first","https://boards.greenhouse.io/first")
        second=EmployerSource("Second Employer","greenhouse","second","https://boards.greenhouse.io/second")
        with patch.object(jobs,"_employer_sources",return_value=[first]), \
             patch.object(jobs,"fetch_board",return_value=([posting(10,"Software Engineer First")],None)):
            initial=jobs.discover("software engineering jobs",self.prefs(),run_id="resume-discovery")
        self.assertEqual(initial["count"],1)
        with jobs.database() as db:
            db.execute("UPDATE job_discovery_runs SET status='interrupted',total_sources=2 WHERE run_id='resume-discovery'")
        with patch.object(jobs,"_employer_sources",return_value=[first,second]), \
             patch.object(jobs,"fetch_board",return_value=([posting(11,"Software Engineer Second")],None)) as fetch:
            resumed=jobs.discover("software engineering jobs",self.prefs(),run_id="resume-discovery")
        self.assertEqual(fetch.call_count,1)
        self.assertEqual(fetch.call_args.args,("Second Employer","second"))
        self.assertEqual(resumed["count"],2)
        self.assertEqual(resumed["diagnostics"]["sources_checked"],2)
        self.assertEqual(resumed["diagnostics"]["postings_retrieved"],2)
        self.assertEqual({item["company"] for item in resumed["results"]},{"First Employer","Second Employer"})

    def test_discovery_run_recovers_after_application_restart(self):
        source=EmployerSource("Restart Employer","greenhouse","restart","https://boards.greenhouse.io/restart")
        with patch.object(jobs,"_employer_sources",return_value=[source]), \
             patch.object(jobs,"fetch_board",return_value=([posting(30,"Software Engineer Restart")],None)) as fetch:
            first=jobs.discover("software engineering jobs",self.prefs(),run_id="restart-discovery")
            self.assertEqual(first["count"],1)
            with jobs.database() as db:
                db.execute("UPDATE job_discovery_runs SET status='interrupted' WHERE run_id='restart-discovery'")
            resumed=jobs.discover("software engineering jobs",self.prefs(),run_id="restart-discovery")
        self.assertEqual(fetch.call_count,1)
        self.assertEqual(resumed["count"],1)
        self.assertEqual(resumed["diagnostics"]["sources_checked"],1)
        with jobs.database() as db:
            self.assertEqual(db.execute("SELECT status FROM job_discovery_runs WHERE run_id='restart-discovery'").fetchone()[0],"completed")

    def test_discovery_returns_actionable_conflict_for_another_active_run(self):
        stamp=jobs.datetime.now(jobs.timezone.utc).isoformat()
        with jobs.database() as db:
            db.execute("INSERT INTO job_discovery_runs(run_id,user_id,prompt,status,created_at,updated_at) VALUES('already-running',?,?,'running',?,?)",
                       (jobs.USER,"private prompt is not returned",stamp,stamp))
        with self.assertRaises(HTTPException) as caught:
            jobs.discover("software engineering jobs",self.prefs(),run_id="new-discovery")
        self.assertEqual(caught.exception.status_code,409)
        self.assertIn("legacy_unleased",caught.exception.detail)
        with jobs.database() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM job_discovery_runs WHERE run_id='new-discovery'").fetchone())

    def test_pause_stops_at_source_boundary_and_resumes_remaining_sources(self):
        first=EmployerSource("First Employer","greenhouse","pause-first","https://boards.greenhouse.io/pause-first")
        second=EmployerSource("Second Employer","greenhouse","pause-second","https://boards.greenhouse.io/pause-second")
        calls=[]
        def fetch(_company,token):
            calls.append(token)
            if token=="pause-first":
                jobs.pause_discovery("pause-discovery")
                return [posting(40,"Software Engineer First")],None
            return [posting(41,"Software Engineer Second")],None
        with patch.object(jobs,"_employer_sources",return_value=[first,second]), \
             patch.object(jobs,"fetch_board",side_effect=fetch):
            paused=jobs.discover("software engineering jobs",self.prefs(),run_id="pause-discovery")
            self.assertEqual(paused["status"],"interrupted")
            self.assertEqual(calls,["pause-first"])
            resumed=jobs.discover("software engineering jobs",self.prefs(),run_id="pause-discovery")
        self.assertEqual(calls,["pause-first","pause-second"])
        self.assertEqual(resumed["count"],2)
        self.assertEqual(resumed["diagnostics"]["sources_checked"],2)
        self.assertEqual(resumed["diagnostics"]["postings_retrieved"],2)

    def test_cancellation_releases_run_lease_and_next_workflow_can_start(self):
        source=EmployerSource("Cancel Employer","greenhouse","cancel-board","https://boards.greenhouse.io/cancel-board")
        def cancel_during_fetch(_company,_token):
            jobs.cancel_discovery("cancel-this-run")
            return [posting(42,"Software Engineer")],None
        with patch.object(jobs,"_employer_sources",return_value=[source]), \
             patch.object(jobs,"fetch_board",side_effect=cancel_during_fetch):
            result=jobs.discover("software engineering jobs",self.prefs(),run_id="cancel-this-run",workflow_id="cancelled-workflow")
        self.assertEqual(result["status"],"cancelled")
        with jobs.database() as db:
            row=db.execute("SELECT status,owner_token,lease_expires_at FROM job_discovery_runs WHERE run_id='cancel-this-run'").fetchone()
        self.assertEqual(row["status"],"cancelled")
        self.assertEqual(row["owner_token"],"")
        self.assertIsNone(row["lease_expires_at"])
        with patch.object(jobs,"_employer_sources",return_value=[]):
            next_run=jobs.discover("software engineering jobs",self.prefs(),run_id="next-after-cancel")
        self.assertEqual(next_run["status"],"no_matching_openings")

    def test_failed_source_can_retry_without_double_counting_after_checkpoint(self):
        first=EmployerSource("Flaky Employer","greenhouse","flaky","https://boards.greenhouse.io/flaky")
        second=EmployerSource("Healthy Employer","greenhouse","healthy","https://boards.greenhouse.io/healthy")
        board={"flaky":[posting(50,"Software Engineer Flaky")],"healthy":[posting(51,"Software Engineer Healthy")]}
        original=jobs.assess_job
        def fail_first(job,prefs,terms):
            if job["title"].endswith("Flaky"):
                raise TypeError("transient malformed field")
            return original(job,prefs,terms)
        with patch.object(jobs,"_employer_sources",return_value=[first,second]), \
             patch.object(jobs,"fetch_board",side_effect=lambda _company,token:(board[token],None)), \
             patch.object(jobs,"assess_job",side_effect=fail_first):
            first_result=jobs.discover("software engineering jobs",self.prefs(),run_id="retry-discovery")
        self.assertEqual(first_result["count"],1)
        with jobs.database() as db:db.execute("UPDATE job_discovery_runs SET status='interrupted' WHERE run_id='retry-discovery'")
        with patch.object(jobs,"_employer_sources",return_value=[first,second]), \
             patch.object(jobs,"fetch_board",side_effect=lambda _company,token:(board[token],None)):
            resumed=jobs.discover("software engineering jobs",self.prefs(),run_id="retry-discovery")
        self.assertEqual(resumed["count"],2)
        self.assertEqual(resumed["diagnostics"]["sources_checked"],2)
        self.assertEqual(resumed["diagnostics"]["postings_retrieved"],2)
        self.assertEqual(resumed["diagnostics"]["feed_failures"],[])

    def test_legacy_interrupted_run_restarts_counters_without_overcounting(self):
        first=EmployerSource("First Employer","greenhouse","first","https://boards.greenhouse.io/first")
        second=EmployerSource("Second Employer","greenhouse","second","https://boards.greenhouse.io/second")
        with patch.object(jobs,"_employer_sources",return_value=[first]), \
             patch.object(jobs,"fetch_board",return_value=([posting(20,"Software Engineer First")],None)):
            jobs.discover("software engineering jobs",self.prefs(),run_id="legacy-discovery")
        with jobs.database() as db:
            row=db.execute("SELECT diagnostics_json FROM job_discovery_runs WHERE run_id='legacy-discovery'").fetchone()
            legacy=json.loads(row[0]);legacy.pop("completed_sources",None);legacy.pop("found_job_keys",None)
            legacy["sources_checked"]=5;legacy["postings_retrieved"]=20
            db.execute("UPDATE job_discovery_runs SET status='interrupted',diagnostics_json=? WHERE run_id='legacy-discovery'",
                       (json.dumps(legacy),))
        with patch.object(jobs,"_employer_sources",return_value=[first,second]), \
             patch.object(jobs,"fetch_board",side_effect=[([posting(20,"Software Engineer First")],None),
                 ([posting(21,"Software Engineer Second")],None)]):
            resumed=jobs.discover("software engineering jobs",self.prefs(),run_id="legacy-discovery")
        self.assertEqual(resumed["diagnostics"]["sources_checked"],2)
        self.assertEqual(resumed["diagnostics"]["postings_retrieved"],2)
        self.assertEqual(resumed["count"],2)

    def test_discovery_diagnostics_separate_geo_conflicts_and_unknown_locations(self):
        items = [posting(501, "Software Engineer", location={"name": "London, United Kingdom"}),
                 posting(502, "Software Engineer", location={"name": "Hybrid"}),
                 posting(503, "Software Engineer", location={"name": "Remote US"})]
        with self._mock_feeds({"cloudflare": items}):
            result = jobs.discover("software engineering jobs in United States", jobs.preferences())
        self.assertEqual(result["diagnostics"]["geographic_mismatches"], 1)
        self.assertEqual(result["diagnostics"]["location_needs_review"], 1)
        self.assertEqual({job["location_status"] for job in result["results"]}, {"unknown", "eligible"})
        self.assertGreaterEqual(result["results"][0]["relevance_score"], result["results"][1]["relevance_score"])

    def test_duplicate_prevention_survives_repeated_search(self):
        with self._mock_feeds({"cloudflare": [posting(123, "Software Engineer")]}):
            first = jobs.discover("software roles", jobs.preferences())
            second = jobs.discover("software roles", jobs.preferences())
        self.assertEqual(first["count"], 1)
        self.assertEqual(second["count"], 1)
        self.assertEqual(jobs.saved_jobs()["count"], 1)

    def test_invalid_application_link_is_counted(self):
        invalid = posting(9, "Software Engineer", absolute_url="https://example.com/apply")
        with self._mock_feeds({"cloudflare": [invalid]}):
            result = jobs.discover("software roles", jobs.preferences())
        self.assertEqual(result["diagnostics"]["rejected_by_filter"]["invalid_application_url"], 1)

    def test_malformed_feed_response_is_reported(self):
        response = unittest.mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"unexpected": []}
        with patch("job_connectors.requests.get", return_value=response):
            found, error = jobs.fetch_board("Example", "example")
        self.assertEqual(found, [])
        self.assertIn("Malformed response", error)


if __name__ == "__main__":
    unittest.main()
