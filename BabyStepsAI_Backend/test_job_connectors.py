import json
import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, patch

import job_connectors as connectors
import job_search_agent as discovery


class ConnectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db = discovery.DB
        discovery.DB = Path(self.temp.name) / "sources.sqlite3"
        discovery.initialize()
        with discovery.database() as db:
            db.execute("CREATE TABLE career_preferences(user_id TEXT PRIMARY KEY,preferences_json TEXT NOT NULL)")
            db.execute("CREATE TABLE application_authorizations(user_id TEXT PRIMARY KEY,settings_json TEXT)")
            db.execute("INSERT INTO career_preferences VALUES(?,?)", (discovery.USER, json.dumps({
                "roles": ["Software Engineer"], "categories": [], "levels": [], "employment_types": [],
                "location": "United States", "work_arrangement": "Any", "instructions": ""})))
            db.execute("INSERT INTO application_authorizations VALUES(?,?)", (discovery.USER, json.dumps({"allow_automatic_discovery": True})))
        connectors.clear_cache()

    @staticmethod
    def local_request(host="127.0.0.1"):
        return SimpleNamespace(client=SimpleNamespace(host=host))

    def register_and_activate(self, sources):
        discovery.import_employers(discovery.EmployerImportRequest(sources=sources), self.local_request())
        with discovery.database() as db:
            db.execute("UPDATE employers SET active=1,connector_status='healthy' WHERE active=0 AND source_type!='workday'")

    def tearDown(self):
        discovery.DB = self.old_db
        self.temp.cleanup()
        connectors.clear_cache()

    def test_all_documented_feed_adapters_normalize_records(self):
        cases = [
            ("greenhouse", "board", {"id": 4, "title": "Software Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/4", "content": "Python", "location": {"name": "Austin, Texas, United States"}}),
            ("lever", "board", {"id": "l4", "text": "Software Engineer", "applyUrl": "https://jobs.lever.co/acme/l4", "descriptionPlain": "Python", "categories": {"location": "Austin, Texas, United States", "commitment": "Full-time"}}),
            ("ashby", "board", {"id": "a4", "title": "Software Engineer", "applyUrl": "https://jobs.ashbyhq.com/acme/a4", "descriptionPlain": "Python", "location": "Austin, Texas, United States", "isListed": True}),
            ("smartrecruiters", "acme", {"id": "s4", "name": "Software Engineer", "applyUrl": "https://www.smartrecruiters.com/acme/4", "location": {"city": "Austin", "region": "Texas", "country": "United States"}}),
        ]
        for kind, token, raw in cases:
            source = connectors.EmployerSource("Acme", kind, token)
            record = connectors.normalize_job(kind, source, raw)
            self.assertEqual(record["title"], "Software Engineer")
            self.assertTrue(record["job_key"].startswith(kind + ":"))
            self.assertIn("active", record)

    def test_fetch_adapters_use_documented_read_only_urls_and_payloads(self):
        fixtures = {
            "greenhouse": {"jobs": [{"id": 1, "title": "SE"}]},
            "lever": [{"id": "2", "text": "SE"}],
            "ashby": {"apiVersion": "1", "jobs": [{"id": "3", "title": "SE", "isListed": True}]},
            "smartrecruiters": {"content": [{"id": "4", "name": "SE"}], "totalFound": 1},
        }
        expected = {"greenhouse": "boards-api.greenhouse.io", "lever": "api.lever.co", "ashby": "api.ashbyhq.com", "smartrecruiters": "api.smartrecruiters.com"}
        for kind, payload in fixtures.items():
            response = Mock()
            response.content = json.dumps(payload).encode()
            response.json.return_value = payload
            response.raise_for_status.return_value = None
            getter = Mock(return_value=response)
            records, error = connectors.fetch_source(connectors.EmployerSource("Acme", kind, "board"), getter=getter)
            self.assertIsNone(error, kind)
            self.assertEqual(len(records), 1, kind)
            self.assertIn(expected[kind], getter.call_args.args[0])
            self.assertNotEqual(getter.call_args.args[0].split("/")[2], "")

    def test_workday_is_reported_as_unsupported_not_fake_supported(self):
        records, error = connectors.fetch_source(connectors.EmployerSource("Tenant", "workday", "tenant"))
        self.assertEqual(records, [])
        self.assertIn("universal", error)

    def test_live_source_registration_requires_successful_validation_before_activation(self):
        response = discovery.import_employers(discovery.EmployerImportRequest(sources=[
            discovery.EmployerImportItem(name="Pending", source_type="ashby", source_token="verified-token")]),
            self.local_request())
        self.assertEqual(response["active"], 0)
        with discovery.database() as db:
            row = db.execute("SELECT employer_id,active,connector_status FROM employers WHERE source_token='verified-token'").fetchone()
        self.assertEqual((row["active"], row["connector_status"]), (0, "needs_verification"))
        valid_posting = {"id": "x", "title": "Software Engineer", "applyUrl": "https://jobs.ashbyhq.com/pending/x",
                         "jobUrl": "https://jobs.ashbyhq.com/pending/x", "location": "Austin, TX"}
        with patch.object(discovery, "fetch_source", return_value=([valid_posting], None)), \
             patch.object(discovery, "validate_public_https_url", side_effect=lambda value, **_kwargs: value):
            result = discovery.validate_employer_source(row["employer_id"], self.local_request())
        self.assertTrue(result["valid"])
        with discovery.database() as db:
            state = db.execute("SELECT active,connector_status,last_success_at FROM employers WHERE employer_id=?", (row["employer_id"],)).fetchone()
        self.assertEqual((state["active"], state["connector_status"]), (1, "healthy"))
        self.assertTrue(state["last_success_at"])

    def test_temporary_validation_failure_keeps_previously_active_source_active(self):
        with patch.object(discovery, "fetch_source", return_value=([], "HTTP 429 rate limited; Retry-After 30s")):
            result = discovery.validate_employer_source("greenhouse:cloudflare", self.local_request())
        self.assertFalse(result["valid"])
        self.assertTrue(result["active"])
        self.assertEqual(result["status"], "rate_limited")
        with discovery.database() as db:
            active = db.execute("SELECT active FROM employers WHERE employer_id='greenhouse:cloudflare'").fetchone()[0]
        self.assertEqual(active, 1)

    def test_health_classification_distinguishes_parser_access_and_transient_failures(self):
        cases={
            "Unsupported career page layout: no JSON-LD": "unsupported_structure",
            "JavaScript-rendered career page: no static jobs": "javascript_rendered",
            "Career page disallowed by robots.txt.": "robots_disallowed",
            "HTTP 403 access restricted": "access_restricted",
            "Feed request timed out.": "network_timeout",
            "HTTP 429 rate limited; Retry-After 30s": "rate_limited",
            "Malformed response: expected jobs collection": "malformed_response",
            "HTTP 410 source not found": "permanently_unavailable",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(discovery._source_health(message),expected)

    def test_valid_empty_source_is_active_and_retries_are_deferred_until_due(self):
        with patch.object(discovery,"validate_public_https_url",side_effect=lambda url,**_kw:url):
            discovery.register_employer_sources(discovery.EmployerImportRequest(sources=[
                discovery.EmployerImportItem(name="Empty but valid",source_type="public_html",careers_url="https://example.test/careers")]))
        with patch.object(discovery,"fetch_source",return_value=([],None)):
            result=discovery.verify_employer_source_record("public_html:"+__import__('hashlib').sha256(b"https://example.test/careers").hexdigest()[:24])
        self.assertTrue(result["valid"])
        self.assertEqual(result["health_reason"],"valid_zero_postings")
        self.assertTrue(result["active"])
        with discovery.database() as db:
            row=dict(db.execute("SELECT * FROM employers WHERE name='Empty but valid'").fetchone())
        self.assertFalse(discovery.employer_retry_eligible(row))
        self.assertTrue(discovery.employer_retry_eligible(row,force=True))
        changed={**row,"careers_url":row["careers_url"]+"?source_revision=2"}
        self.assertTrue(discovery.employer_retry_eligible(changed))

    def test_unsupported_source_is_scheduled_and_temporary_failure_preserves_active_source(self):
        source_id="public_html:"+__import__('hashlib').sha256(b"https://unsupported.example/jobs").hexdigest()[:24]
        with patch.object(discovery,"validate_public_https_url",side_effect=lambda url,**_kw:url):
            discovery.register_employer_sources(discovery.EmployerImportRequest(sources=[
                discovery.EmployerImportItem(name="Unsupported",source_type="public_html",careers_url="https://unsupported.example/jobs")]))
        with patch.object(discovery,"fetch_source",return_value=([],"Unsupported career page layout: no public links")):
            result=discovery.verify_employer_source_record(source_id)
        self.assertEqual(result["status"],"unsupported_structure")
        with discovery.database() as db:
            unsupported=dict(db.execute("SELECT * FROM employers WHERE employer_id=?",(source_id,)).fetchone())
        self.assertFalse(discovery.employer_retry_eligible(unsupported))
        self.assertNotIn(source_id,discovery.pending_candidate_employer_ids(None,100))

        fetcher=Mock(return_value=([],"Feed request timed out."))
        with patch.object(discovery,"fetch_source",fetcher):
            result=discovery.verify_employer_source_record("greenhouse:cloudflare")
        self.assertTrue(result["active"])
        self.assertEqual(result["status"],"network_timeout")
        deferred=discovery.verify_employer_source_record("greenhouse:cloudflare",fetcher=fetcher)
        self.assertEqual(deferred["status"],"retry_deferred")
        forced=discovery.verify_employer_source_record("greenhouse:cloudflare",fetcher=fetcher,force=True)
        self.assertEqual(forced["status"],"network_timeout")
        self.assertEqual(fetcher.call_count,2)

    def test_additive_health_migration_schedules_prior_generic_unsupported_rows(self):
        stamp=discovery.datetime.now(discovery.timezone.utc).isoformat()
        with discovery.database() as db:
            db.execute("""INSERT INTO employers(employer_id,name,careers_url,source_type,source_token,connector_status,
                error_details,active,created_at,updated_at,last_attempt_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                ("public_html:migrated","Migrated","https://migrated.example/jobs","public_html","migrated",
                 "unsupported","Unsupported career page layout: no JobPosting JSON-LD or recognizable public job links found.",0,stamp,stamp,stamp))
        discovery.initialize()
        with discovery.database() as db:
            row=dict(db.execute("SELECT * FROM employers WHERE employer_id='public_html:migrated'").fetchone())
        self.assertEqual(row["connector_status"],"unsupported_structure")
        self.assertEqual(row["health_reason"],"unsupported_structure")
        self.assertFalse(discovery.employer_retry_eligible(row))

    def test_employer_admin_routes_are_loopback_only(self):
        with self.assertRaises(Exception) as error:
            discovery.import_employers(discovery.EmployerImportRequest(sources=[
                discovery.EmployerImportItem(name="Nope", source_type="ashby", source_token="nope")]),
                self.local_request("203.0.113.4"))
        from fastapi import HTTPException
        self.assertIsInstance(error.exception, HTTPException)
        self.assertEqual(error.exception.status_code, 403)

    def test_rate_limit_retry_after_is_honored_without_early_retry(self):
        connectors.clear_cache()
        response = Mock(status_code=429, headers={"Retry-After": "60"}, content=b"{}")
        getter = Mock(return_value=response)
        jobs, error = connectors._get_json("https://api.example.test/jobs", getter=getter)
        self.assertEqual(jobs, [])
        self.assertIn("429", error)
        self.assertIn("60s", error)
        self.assertEqual(getter.call_count, 1)
        self.assertEqual(discovery._source_health(error), "rate_limited")

    def test_temporary_feed_failure_does_not_expire_saved_postings(self):
        with discovery.database() as db:
            db.execute("INSERT INTO discovered_openings(user_id,job_key,company,title,location,url,discovered_at,active) VALUES(?,?,?,?,?,?,?,1)",
                       (discovery.USER,"greenhouse:cloudflare:77","Cloudflare","Software Engineer","Remote","https://boards.greenhouse.io/cloudflare/jobs/77","2026-10-01"))
        def response(_company, board):
            if board == "cloudflare":
                return [], "HTTP 503 temporary source failure"
            return [], None
        with patch.object(discovery,"fetch_board",side_effect=response):
            discovery.discover("software engineering jobs",discovery.preferences())
        with discovery.database() as db:
            active = db.execute("SELECT active FROM discovered_openings WHERE job_key='greenhouse:cloudflare:77'").fetchone()[0]
            status = db.execute("SELECT connector_status,last_success_at FROM employers WHERE source_token='cloudflare'").fetchone()
        self.assertEqual(active,1)
        self.assertEqual(status["connector_status"],"temporarily_unavailable")
        self.assertIsNone(status["last_success_at"])

    def test_server_error_gets_bounded_retry_and_recovers(self):
        import requests
        connectors.clear_cache()
        failing = Mock(status_code=503, content=b"{}")
        failing.raise_for_status.side_effect = requests.HTTPError(response=failing)
        success = Mock(status_code=200, content=b'{"jobs":[{"id":"1"}]}')
        success.json.return_value = {"jobs": [{"id": "1"}]}
        success.raise_for_status.return_value = None
        getter = Mock(side_effect=[failing, success])
        with patch.object(connectors.time, "sleep"):
            jobs, error = connectors._get_json("https://api.example.test/retry", getter=getter)
        self.assertIsNone(error)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(getter.call_count, 2)

    def test_redirect_destination_is_revalidated(self):
        redirect = Mock(status_code=302, headers={"Location": "http://127.0.0.1/private"})
        with patch.object(connectors, "validate_public_https_url", side_effect=lambda url, **kwargs: url if "127.0.0.1" not in url else (_ for _ in ()).throw(ValueError("private"))):
            with self.assertRaises(ValueError):
                connectors._safe_get("https://public.example.test/start", getter=Mock(return_value=redirect), trusted_initial=True)

    def test_rss_atom_parsing_normalizes_public_job_entries(self):
        source = connectors.EmployerSource("Example", "rss", "feed", "https://jobs.example.org/feed.xml")
        response = Mock(status_code=200, content=b"<rss><channel><item><title>Software Engineer</title><link>https://jobs.example.org/jobs/1</link><pubDate>2026-10-01</pubDate></item></channel></rss>")
        response.raise_for_status.return_value = None
        with patch.object(connectors, "validate_public_https_url", side_effect=lambda value, **_kw: value):
            jobs, error = connectors.fetch_rss(source, getter=Mock(return_value=response))
        self.assertIsNone(error)
        normalized = connectors.normalize_job("rss", source, jobs[0])
        self.assertEqual(normalized["title"], "Software Engineer")
        self.assertEqual(normalized["application_url"], "https://jobs.example.org/jobs/1")

    def test_public_html_fallback_extracts_only_job_like_links(self):
        source = connectors.EmployerSource("Example", "public_html", "careers", "https://example.org/careers")
        robots = Mock(status_code=404, content=b"", text="")
        page_text = '<a href="/jobs/123">Backend Engineer</a><a href="/about">About</a>'
        page = Mock(status_code=200, content=page_text.encode(), text=page_text)
        page.raise_for_status.return_value = None
        with patch.object(connectors, "validate_public_https_url", side_effect=lambda value, **_kw: value):
            jobs, error = connectors.fetch_public_career_page(source, getter=Mock(side_effect=[robots, page]))
        self.assertIsNone(error)
        self.assertEqual([job["title"] for job in jobs], ["Backend Engineer"])
        self.assertEqual(jobs[0]["url"], "https://example.org/jobs/123")

    def test_javascript_career_shell_gets_specific_non_activation_diagnostic(self):
        robots=Mock(status_code=404,content=b"",text="")
        text='<html><h1>Careers</h1><div id="root"></div><script src="bundle.js"></script></html>'
        page=Mock(status_code=200,content=text.encode(),text=text)
        page.raise_for_status.return_value=None
        source=connectors.EmployerSource("Example","public_html","", "https://example.org/careers")
        with patch.object(connectors,"validate_public_https_url",side_effect=lambda value,**_kwargs:value):
            records,error=connectors.fetch_public_career_page(source,getter=Mock(side_effect=[robots,page]))
        self.assertEqual(records,[])
        self.assertIn("JavaScript-rendered",error)
        self.assertEqual(discovery._source_health(error),"javascript_rendered")

    def test_career_redirect_to_recognized_ats_uses_public_feed(self):
        robots=Mock(status_code=404,content=b"",text="")
        page=Mock(status_code=200,content=b"{}",text="{}",url="https://boards.greenhouse.io/acme")
        page.raise_for_status.return_value=None
        feed=Mock(status_code=200,content=b'{"jobs":[]}',headers={})
        feed.json.return_value={"jobs":[]};feed.raise_for_status.return_value=None
        source=connectors.EmployerSource("Acme","public_html","","https://acme.example/careers")
        getter=Mock(side_effect=[robots,page,feed])
        with patch.object(connectors,"validate_public_https_url",side_effect=lambda value,**_kwargs:value):
            jobs,error=connectors.fetch_public_career_page(source,getter=getter)
        self.assertEqual((jobs,error),([],None))
        self.assertIn("boards-api.greenhouse.io",getter.call_args_list[-1].args[0])

    def test_public_html_follows_only_bounded_explicit_next_page(self):
        robots=Mock(status_code=404,content=b"",text="")
        first_text='<a href="/jobs/1">Software Engineer</a><a rel="next" href="/careers?page=2">Next</a>'
        second_text='<a href="/jobs/2">Data Engineer</a>'
        first=Mock(status_code=200,content=first_text.encode(),text=first_text);first.raise_for_status.return_value=None
        second=Mock(status_code=200,content=second_text.encode(),text=second_text);second.raise_for_status.return_value=None
        source=connectors.EmployerSource("Example","public_html","","https://example.org/careers")
        getter=Mock(side_effect=[robots,first,robots,second])
        with patch.object(connectors,"validate_public_https_url",side_effect=lambda value,**_kwargs:value):
            jobs,error=connectors.fetch_public_career_page(source,getter=getter)
        self.assertIsNone(error)
        self.assertEqual({job["title"] for job in jobs},{"Software Engineer","Data Engineer"})
        self.assertEqual(getter.call_count,4)

    def test_employer_source_discovery_uses_explicit_ats_links_without_guessing(self):
        robots = Mock(status_code=404, content=b"", text="")
        page_text = '<a href="https://jobs.ashbyhq.com/verified-board">Open roles</a>'
        page = Mock(status_code=200, content=page_text.encode(), text=page_text)
        page.raise_for_status.return_value = None
        with patch.object(connectors, "validate_public_https_url", side_effect=lambda value, **_kw: value):
            candidates, error = connectors.discover_source_candidates(
                "Example", "https://example.org/careers", getter=Mock(side_effect=[robots, page]))
        self.assertIsNone(error)
        self.assertEqual(candidates[0]["source_type"], "ashby")
        self.assertEqual(candidates[0]["source_token"], "verified-board")
        self.assertEqual(candidates[0]["status"], "needs_validation")

    def test_official_website_careers_link_is_followed_and_provenance_returned(self):
        robots = Mock(status_code=404, content=b"", text="")
        home_text = '<a href="/careers">Careers</a>'
        careers_text = '<a href="https://jobs.lever.co/acme-public">Open jobs</a>'
        home = Mock(status_code=200, content=home_text.encode(), text=home_text)
        careers = Mock(status_code=200, content=careers_text.encode(), text=careers_text)
        home.raise_for_status.return_value = careers.raise_for_status.return_value = None
        with patch.object(connectors, "validate_public_https_url", side_effect=lambda value, **_kw: value):
            candidates, error = connectors.discover_source_candidates(
                "Acme", "https://acme.example", getter=Mock(side_effect=[robots, home, robots, careers]))
        self.assertIsNone(error)
        self.assertEqual(candidates[0]["source_type"], "lever")
        self.assertEqual(candidates[0]["source_token"], "acme-public")
        self.assertEqual(candidates[0]["website_url"], "https://acme.example")
        self.assertEqual(candidates[0]["evidence_url"], "https://acme.example/careers")

    def test_employer_source_discovery_marks_workday_unsupported(self):
        robots = Mock(status_code=404, content=b"", text="")
        page_text = '<a href="https://acme.wd5.myworkdayjobs.com/en-US/External">Careers</a>'
        page = Mock(status_code=200, content=page_text.encode(), text=page_text)
        page.raise_for_status.return_value = None
        with patch.object(connectors, "validate_public_https_url", side_effect=lambda value, **_kw: value):
            candidates, error = connectors.discover_source_candidates(
                "Acme", "https://acme.example/careers", getter=Mock(side_effect=[robots, page]))
        self.assertIsNone(error)
        self.assertEqual(candidates[0]["status"], "unsupported")
        self.assertIn("tenant-specific", candidates[0]["reason"])

    def test_robots_disallowed_career_page_is_not_fetched(self):
        source = connectors.EmployerSource("Example", "career_page", "careers", "https://example.org/careers")
        robots = Mock(status_code=200, content=b"User-agent: BabyStepsAI\nDisallow: /careers", text="User-agent: BabyStepsAI\nDisallow: /careers")
        robots.raise_for_status.return_value = None
        getter = Mock(return_value=robots)
        with patch.object(connectors, "validate_public_https_url", side_effect=lambda value, **_kw: value):
            jobs, error = connectors.fetch_public_career_page(source, getter=getter)
        self.assertEqual(jobs, [])
        self.assertIn("robots.txt", error)
        self.assertEqual(getter.call_count, 1)

    def test_smartrecruiters_public_posting_feed_paginates_with_safety_cap(self):
        calls=[]
        def response_for_page(url, **kwargs):
            calls.append(kwargs["params"]["offset"])
            offset=kwargs["params"]["offset"]
            size=100 if offset<200 else 1
            payload={"content":[{"id":str(offset+i),"name":"Software Engineer"} for i in range(size)]}
            response=Mock();response.content=json.dumps(payload).encode();response.json.return_value=payload;response.raise_for_status.return_value=None
            return response
        jobs,error=connectors.fetch_smartrecruiters(connectors.EmployerSource("Example","smartrecruiters","example"),getter=response_for_page)
        self.assertIsNone(error)
        self.assertEqual(len(jobs),201)
        self.assertEqual(calls,[0,100,200])

    def test_jsonld_parser_accepts_job_postings_only_and_expiration(self):
        source = connectors.EmployerSource("Example", "career_page", "careers", "https://careers.example.org")
        expired = {"@context": "https://schema.org", "@type": "JobPosting", "title": "Old Software Engineer",
                   "url": "/jobs/old", "validThrough": "2000-01-01", "description": "Expired"}
        fresh = {"@type": "JobPosting", "title": "Software Engineer", "url": "/jobs/new", "description": "Python"}
        response = Mock()
        response.content = b"<html>"
        response.text = '<script type="application/ld+json">' + json.dumps([expired, fresh]) + "</script>"
        response.raise_for_status.return_value = None
        with patch.object(connectors, "validate_public_https_url", side_effect=lambda value, **_kwargs: value):
            jobs, error = connectors.fetch_jsonld_career_page(source, getter=Mock(return_value=response))
        self.assertIsNone(error)
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["url"], "https://careers.example.org/jobs/old")
        self.assertFalse(connectors.normalize_job("career_page", source, jobs[0])["active"])
        self.assertTrue(connectors.normalize_job("career_page", source, jobs[1])["active"])

    def test_directory_import_and_multisource_discovery(self):
        self.register_and_activate([
            discovery.EmployerImportItem(name="Lever Demo", source_type="lever", source_token="demo"),
            discovery.EmployerImportItem(name="Ashby Demo", source_type="ashby", source_token="demo"),
        ])
        lever = [{"id": "l1", "text": "Software Engineer", "applyUrl": "https://jobs.lever.co/demo/l1",
                  "descriptionPlain": "Python", "categories": {"location": "Austin, Texas, United States", "commitment": "Full-time"}}]
        ashby = [{"id": "a1", "title": "Backend Engineer", "applyUrl": "https://jobs.ashbyhq.com/demo/a1",
                  "descriptionPlain": "Python", "location": "Austin, Texas, United States", "isListed": True, "employmentType": "FullTime"}]
        def source_response(source):
            return (lever, None) if source.source_type == "lever" else (ashby, None)
        with patch.object(discovery, "fetch_board", return_value=([], None)), patch.object(discovery, "fetch_source", side_effect=source_response):
            result = discovery.discover("software jobs", discovery.preferences())
        self.assertGreaterEqual(result["diagnostics"]["sources_checked"], 5)
        self.assertIn("lever", result["diagnostics"]["source_platforms"])
        self.assertIn("ashby", result["diagnostics"]["source_platforms"])
        self.assertEqual({job["source_platform"] for job in result["results"]}, {"lever", "ashby"})
        self.assertTrue(all(job["description"] for job in result["results"]))

    def test_same_canonical_url_is_deduplicated_and_provenance_retained(self):
        self.register_and_activate([
            discovery.EmployerImportItem(name="Lever Demo", source_type="lever", source_token="demo")])
        gh = [{"id": 1, "title": "Software Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
               "content": "Python", "location": {"name": "Austin, Texas, United States"}}]
        lever = [{"id": "l1", "text": "Software Engineer", "applyUrl": "https://boards.greenhouse.io/acme/jobs/1",
                  "descriptionPlain": "Python", "categories": {"location": "Austin, Texas, United States"}}]
        def board_response(_company, board):
            return (gh, None) if board == "cloudflare" else ([], None)
        with patch.object(discovery, "fetch_board", side_effect=board_response), patch.object(discovery, "fetch_source", return_value=(lever, None)):
            result = discovery.discover("software jobs", discovery.preferences())
            repeated = discovery.discover("software jobs", discovery.preferences())
        self.assertEqual(result["diagnostics"]["duplicate_listings_removed"], 1)
        self.assertEqual(repeated["diagnostics"]["duplicate_listings_removed"], 1)
        self.assertEqual(repeated["count"], 1)
        with discovery.database() as db:
            count = db.execute("SELECT count(*) FROM job_source_provenance").fetchone()[0]
        self.assertEqual(count, 2)

    def test_ssrf_destinations_rejected(self):
        for url in ("http://example.org", "https://127.0.0.1/private", "https://metadata.google.internal/latest"):
            with self.assertRaises(ValueError):
                connectors.validate_public_https_url(url, resolve=False)

    def test_job_url_canonicalization_strips_tracking_but_preserves_job_path(self):
        self.assertEqual(connectors.canonicalize_job_url(
            "https://EXAMPLE.org/jobs/ABC/?utm_source=board&ref=feed#apply"),
            "https://example.org/jobs/ABC")
        self.assertNotEqual(connectors.canonicalize_job_url("https://example.org/jobs/ABC"),
                            connectors.canonicalize_job_url("https://example.org/jobs/XYZ"))

    def test_discovery_run_progress_and_safe_cancellation_persist(self):
        run_id = "phase9-cancel-test"
        def cancel_first(_company, _board):
            discovery.cancel_discovery(run_id)
            return [], None
        with patch.object(discovery, "fetch_board", side_effect=cancel_first):
            result = discovery.discover("software jobs", discovery.preferences(), run_id=run_id)
        self.assertEqual(result["status"], "cancelled")
        progress = discovery.discovery_progress(run_id)
        self.assertEqual(progress["status"], "cancelled")
        self.assertTrue(progress["cancel_requested"])
        self.assertEqual(progress["sources_checked"], 1)

    def test_discovery_routes_work_through_asgi(self):
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(discovery.router)
        async def request(method, path, body=b""):
            sent=[]; delivered=False
            async def receive():
                nonlocal delivered
                if not delivered:
                    delivered=True
                    return {"type":"http.request","body":body,"more_body":False}
                return {"type":"http.disconnect"}
            async def send(message): sent.append(message)
            scope={"type":"http","asgi":{"version":"3.0"},"http_version":"1.1","method":method,
                   "scheme":"http","path":path,"raw_path":path.encode(),"query_string":b"","root_path":"",
                   "headers":[(b"content-type",b"application/json")],"client":("127.0.0.1",1234),"server":("127.0.0.1",8000)}
            await app(scope,receive,send)
            start=next(item for item in sent if item["type"]=="http.response.start")
            body=b"".join(item.get("body",b"") for item in sent if item["type"]=="http.response.body")
            return start["status"],body
        status,body=asyncio.run(request("GET","/api/employers"))
        self.assertEqual(status,200,body)
        self.assertEqual(json.loads(body)["count"],3)
        import_body=json.dumps({"sources":[{"name":"ASGI Source","source_type":"ashby","source_token":"asgi-demo"}]}).encode()
        status,body=asyncio.run(request("POST","/api/employers/import",import_body))
        self.assertEqual(status,200,body)
        self.assertEqual(json.loads(body)["active"],0)
        valid_posting = {"id": "1", "title": "Software Engineer", "applyUrl": "https://jobs.ashbyhq.com/asgi/1",
                         "jobUrl": "https://jobs.ashbyhq.com/asgi/1", "location": "Austin, TX"}
        with patch.object(discovery,"fetch_source",return_value=([valid_posting],None)), \
             patch.object(discovery,"validate_public_https_url",side_effect=lambda value, **_kwargs: value):
            status,body=asyncio.run(request("POST","/api/employers/ashby:asgi-demo/validate"))
        self.assertEqual(status,200)
        self.assertTrue(json.loads(body)["valid"])
        status,_=asyncio.run(request("GET","/find-jobs"))
        self.assertEqual(status,200)

    def test_career_preference_changes_version_and_invalidate_completed_match_rows(self):
        import career_preferences as career
        previous=career.DB
        try:
            career.DB=discovery.DB
            with discovery.database() as db:
                db.execute("CREATE TABLE job_match_results(user_id TEXT,job_key TEXT,status TEXT,freshness_status TEXT)")
                db.execute("INSERT INTO job_match_results VALUES(?,?,?,?)",(discovery.USER,"greenhouse:cloudflare:7","completed","current"))
            new=career.Preferences(roles=["Backend Engineer"],categories=["Software Development"],levels=["Entry Level"],
                employment_types=["Full-time"],location="United States",work_arrangement="Remote",instructions="No sales roles")
            saved=career.save_preferences(new)
            self.assertEqual(saved["preference_version"],2)
            self.assertGreater(career.get_preferences()["preference_version"],1)
            with discovery.database() as db:
                state=db.execute("SELECT freshness_status FROM job_match_results WHERE job_key='greenhouse:cloudflare:7'").fetchone()[0]
            self.assertEqual(state,"stale")
        finally:
            career.DB=previous

    def test_mocked_discovery_matching_to_explicit_application_queue_end_to_end(self):
        with discovery.database() as db:
            columns={row[1] for row in db.execute("PRAGMA table_info(application_authorizations)")}
            if "version" not in columns:db.execute("ALTER TABLE application_authorizations ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
            if "updated_at" not in columns:db.execute("ALTER TABLE application_authorizations ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
        import application_answer_assistant as answers
        import candidate_verification as verification
        import resume_job_matcher as matcher
        from test_resume_job_matcher import valid_result
        old_paths={module:module.DB for module in (discovery,answers,verification)}
        old_storage=None
        applications=None
        storage=Path(self.temp.name)/"resumes";storage.mkdir()
        try:
            for module in old_paths: module.DB=discovery.DB
            with discovery.database() as db:
                columns={row[1] for row in db.execute("PRAGMA table_info(application_authorizations)")}
                if "version" not in columns:db.execute("ALTER TABLE application_authorizations ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
                if "updated_at" not in columns:db.execute("ALTER TABLE application_authorizations ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
                db.execute("CREATE TABLE resumes(id TEXT PRIMARY KEY,user_id TEXT,stored_name TEXT,original_name TEXT,created_at TEXT,active INTEGER,extracted_text TEXT)")
                db.execute("CREATE TABLE resume_profiles(resume_id TEXT PRIMARY KEY,status TEXT,profile_json TEXT,error TEXT,updated_at TEXT)")
                db.execute("INSERT INTO resumes VALUES('e2e-resume',?,'resume.pdf','Resume.pdf',?,1,'')",(discovery.USER,discovery.datetime.now(discovery.timezone.utc).isoformat()))
                db.execute("INSERT INTO resume_profiles VALUES('e2e-resume','user_reviewed',?,NULL,?)",(json.dumps({"technical_skills":["Python"],"education":[{"degree":"B.S."}]}),discovery.datetime.now(discovery.timezone.utc).isoformat()))
                db.execute("UPDATE career_preferences SET preferences_json=? WHERE user_id=?",(json.dumps({"roles":["Software Engineer"],"categories":[],"levels":[],"employment_types":[],"location":"United States","work_arrangement":"Any","instructions":""}),discovery.USER))
                db.execute("UPDATE application_authorizations SET settings_json=? WHERE user_id=?",(json.dumps({"allow_automatic_discovery":True,"allow_ai_matching":True,"allow_automated_form_preparation":True}),discovery.USER))
            verification.initialize()
            with discovery.database() as db:
                db.execute("INSERT INTO candidate_verifications VALUES('e2e-resume',?,?,?,?)",(discovery.USER,json.dumps({"full_name":"A Candidate","email":"a@example.test","technical_skills":["Python"]}),json.dumps({"full_name":True,"email":True,"technical_skills":True}),discovery.datetime.now(discovery.timezone.utc).isoformat()))
            import application_agent as applications
            old_storage=applications.STORAGE
            applications.DB=discovery.DB
            applications.STORAGE=storage
            storage.joinpath("resume.pdf").write_bytes(b"local test resume")
            answers.initialize();matcher.initialize();applications.initialize()
            answers.update_authorization(answers.AuthorizationUpdate(allow_automatic_discovery=True,allow_ai_matching=True,
                allow_automated_form_preparation=True,allow_auto_use_confirmed_answers=True))
            self.register_and_activate([
                discovery.EmployerImportItem(name="E2E Employer",source_type="ashby",source_token="e2e-board")])
            feed=[{"id":"job-42","title":"Software Engineer","location":"Austin, Texas, United States",
                   "employmentType":"FullTime","descriptionPlain":"Python experience and a B.S. degree.",
                   "applyUrl":"https://jobs.ashbyhq.com/e2e-board/job-42","jobUrl":"https://jobs.ashbyhq.com/e2e-board/job-42","isListed":True}]
            with patch.object(discovery,"fetch_board",return_value=([],None)),patch.object(discovery,"fetch_source",return_value=(feed,None)):
                found=discovery.discover("software engineering jobs",discovery.preferences())
            self.assertEqual(found["count"],1)
            self.assertTrue(found["results"][0]["description"])
            with patch.object(matcher,"analyze_with_ollama",return_value=valid_result()), \
                 patch.object(matcher.executor,"submit",side_effect=lambda fn,*args:fn(*args)):
                matcher.analyze_matches(matcher.MatchRequest(job_keys=[found["results"][0]["job_key"]]))
            match=matcher._match_rows([found["results"][0]["job_key"]],"e2e-resume")[0]
            self.assertEqual(match["classification"],"needs_review")
            self.assertTrue(any("Experience level" in item for item in match["match"]["missing_information"]))
            applications.review_saved_job_eligibility(found["results"][0]["job_key"],
                applications.EligibilityReviewRequest(reviewed=True))
            with patch.object(applications,"validate_application_url",side_effect=lambda url:url),patch.object(applications,"_schedule_application_queue",return_value=False):
                queue=applications.enqueue_saved_jobs(applications.ApplicationQueueRequest(job_keys=[found["results"][0]["job_key"]]))
            self.assertEqual(queue["queued"],1)
            self.assertFalse(queue["submission_enabled"])
            with discovery.database() as db:
                state=db.execute("SELECT state FROM applications WHERE user_id=? AND job_key=?",(discovery.USER,found["results"][0]["job_key"])).fetchone()[0]
            self.assertEqual(state,"queued")
        finally:
            for module,path in old_paths.items():module.DB=path
            if applications is not None:
                applications.DB=self.old_db
                applications.STORAGE=old_storage


if __name__ == "__main__":
    unittest.main()
