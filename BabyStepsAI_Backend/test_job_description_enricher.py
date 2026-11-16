import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import job_description_enricher as enricher
import job_connectors as connectors
from job_search_agent import USER


class Response:
    def __init__(self,status=200,text="",url="https://jobs.example.test/posting"):
        self.status_code=status;self.text=text;self.content=text.encode();self.url=url;self.headers={}
    def raise_for_status(self):
        if self.status_code>=400:
            import requests
            raise requests.HTTPError(response=self)


class DescriptionEnricherTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.old_db=enricher.DB
        enricher.DB=Path(self.temp.name)/"jobs.sqlite3"
        with enricher.connect() as db:
            db.execute("""CREATE TABLE discovered_openings(
                user_id TEXT,job_key TEXT PRIMARY KEY,company TEXT,title TEXT,url TEXT,job_url TEXT,
                source_platform TEXT,description TEXT,description_source TEXT DEFAULT '',description_retrieved_at TEXT,
                description_error TEXT DEFAULT '',description_version INTEGER DEFAULT 0,content_fingerprint TEXT DEFAULT '',
                last_verified_at TEXT,required_qualifications_json TEXT DEFAULT '{}',preferred_qualifications_json TEXT DEFAULT '{}')""")
            db.execute("CREATE TABLE employers(source_type TEXT,source_token TEXT,careers_url TEXT)")
            db.execute("CREATE TABLE job_match_results(user_id TEXT,job_key TEXT,status TEXT,freshness_status TEXT)")
            db.execute("INSERT INTO discovered_openings(user_id,job_key,company,title,url,job_url,source_platform,description,content_fingerprint) VALUES(?,?,?,?,?,?,?,?,?)",
                (USER,"public_html:retool:test","Retool","Software Engineer","https://jobs.example.test/posting","https://jobs.example.test/posting","public_html","","old"))
            db.execute("INSERT INTO job_match_results VALUES(?,?,?,?)",(USER,"public_html:retool:test","completed","current"))
        enricher.initialize()

    def tearDown(self):enricher.DB=self.old_db;self.temp.cleanup()

    def test_enrichment_persists_provenance_fingerprint_and_stales_only_changed_job(self):
        text="We are looking for an engineer to build reliable software. Required qualifications include strong programming experience, collaboration, testing, system design, and experience shipping production services. You will work with product and design teams to build and maintain customer facing capabilities. "
        page=f"<html><main><h1>Software Engineer</h1><p>{text}</p></main></html>"
        def getter(url,**kwargs):return Response(404 if url.endswith("robots.txt") else 200,"" if url.endswith("robots.txt") else page,url)
        with patch.object(connectors,"validate_public_https_url",side_effect=lambda value,**kwargs:value):
            result=enricher.enrich_one("public_html:retool:test",getter=getter)
        self.assertEqual(result["status"],"succeeded")
        with enricher.connect() as db:
            row=db.execute("SELECT description,description_source,description_retrieved_at,description_version,content_fingerprint FROM discovered_openings WHERE job_key='public_html:retool:test'").fetchone()
            match=db.execute("SELECT freshness_status FROM job_match_results WHERE job_key='public_html:retool:test'").fetchone()
            attempt=db.execute("SELECT status,source_url FROM job_description_retrievals WHERE job_key='public_html:retool:test'").fetchone()
        self.assertGreater(len(row["description"]),180);self.assertEqual(row["description_version"],1)
        self.assertTrue(row["description_fingerprint"] if "description_fingerprint" in row.keys() else row["content_fingerprint"])
        self.assertEqual(match["freshness_status"],"stale");self.assertEqual(attempt["status"],"succeeded")
        self.assertEqual(attempt["source_url"],"https://jobs.example.test/posting")

    def test_robots_denial_records_reason_without_saving_description(self):
        def getter(url,**kwargs):return Response(200,"User-agent: *\nDisallow: /posting",url)
        with patch.object(connectors,"validate_public_https_url",side_effect=lambda value,**kwargs:value):
            result=enricher.enrich_one("public_html:retool:test",getter=getter)
        self.assertEqual(result["status"],"failed");self.assertIn("disallowed",result["reason"])
        with enricher.connect() as db:
            row=db.execute("SELECT description,description_error FROM discovered_openings WHERE job_key='public_html:retool:test'").fetchone()
        self.assertEqual(row["description"],"");self.assertIn("disallowed",row["description_error"])

    def test_usable_description_reused_without_second_fetch(self):
        with enricher.connect() as db:db.execute("UPDATE discovered_openings SET description=? WHERE job_key=?",("Detailed job content. "*20,"public_html:retool:test"))
        result=enricher.enrich_one("public_html:retool:test",getter=lambda *_a,**_k:self.fail("unexpected fetch"))
        self.assertEqual(result["status"],"available")

    def test_visible_text_parser_ignores_aria_hidden_regions_without_losing_main_content(self):
        hidden="<div aria-hidden='true'>decorative navigation</div>"
        content="Build reliable software with a collaborative product team. Required qualifications include professional software development, production systems, strong testing practices, and clear written communication across engineering partners."
        extracted=enricher._structured_payload(f"<html>{hidden}<main><h1>Engineer</h1><p>{content}</p></main></html>","https://jobs.example.test/posting")
        self.assertTrue(extracted);self.assertIn("Required qualifications",extracted["description"])
        self.assertNotIn("decorative navigation",extracted["description"])

    def test_private_and_non_https_saved_urls_are_rejected(self):
        for url in ("http://jobs.example.test/posting","https://127.0.0.1/posting"):
            with enricher.connect() as db:db.execute("UPDATE discovered_openings SET url=? WHERE job_key=?",(url,"public_html:retool:test"))
            result=enricher.enrich_one("public_html:retool:test")
            self.assertEqual(result["status"],"failed")


if __name__=="__main__":unittest.main()
