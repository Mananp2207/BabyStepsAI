import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import HTTPException

import job_search_agent as jobs
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

    def test_explicit_senior_required_years_rejected_for_new_grads_but_preferred_is_not(self):
        prefs = self.prefs()
        terms = jobs.search_terms("", prefs)
        required = posting(85, "Software Engineer", content="Must have at least 8 years of professional experience.")
        preferred = posting(86, "Software Engineer", content="8 years of professional experience preferred.")
        self.assertEqual(jobs.assess_job(required, prefs, terms)[1], "experience_level")
        self.assertTrue(jobs.assess_job(preferred, prefs, terms)[0])

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

    def test_discovery_diagnostics_separate_geo_conflicts_and_unknown_locations(self):
        items = [posting(501, "Software Engineer", location={"name": "London, United Kingdom"}),
                 posting(502, "Software Engineer", location={"name": "Hybrid"}),
                 posting(503, "Software Engineer", location={"name": "Remote US"})]
        with self._mock_feeds({"cloudflare": items}):
            result = jobs.discover("software engineering jobs in United States", jobs.preferences())
        self.assertEqual(result["diagnostics"]["geographic_mismatches"], 1)
        self.assertEqual(result["diagnostics"]["location_needs_review"], 1)
        self.assertEqual([job["location_status"] for job in result["results"]], ["unknown", "eligible"])

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
