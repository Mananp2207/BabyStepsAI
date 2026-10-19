import csv
import json
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse
from pathlib import Path
from unittest.mock import patch

import job_connectors
import job_search_agent as discovery
import verify_employers as cli
import candidate_importers
import expand_employers
from starlette.requests import Request
from fastapi import HTTPException


class CandidateImportTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_db = discovery.DB
        discovery.DB = Path(self.tempdir.name) / "employers.sqlite3"
        discovery.initialize()

    def tearDown(self):
        discovery.DB = self.old_db
        self.tempdir.cleanup()

    def _csv(self, rows, name="candidates.csv"):
        path = Path(self.tempdir.name) / name
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=("name", "source_type", "source_token", "careers_url", "geographic_coverage"))
            writer.writeheader()
            writer.writerows(rows)
        return path

    def test_csv_json_and_invalid_candidate_parsing(self):
        path = self._csv([
            {"name": "Verified Example", "source_type": "greenhouse", "source_token": "example-board", "careers_url": "https://example.com/careers"},
            {"name": "Unsafe", "source_type": "career_page", "source_token": "", "careers_url": "http://127.0.0.1/jobs"},
            {"name": "Duplicate", "source_type": "greenhouse", "source_token": "example-board", "careers_url": "https://example.com/careers"},
        ])
        valid, invalid = cli.parse_candidates(path)
        self.assertEqual(len(valid), 1)
        self.assertEqual([row["status"] for row in invalid], ["invalid_candidate", "duplicate_in_file"])
        json_path = Path(self.tempdir.name) / "candidates.json"
        json_path.write_text(json.dumps({"sources": [{"name": "Example", "source_type": "lever", "source_token": "example", "careers_url": "https://example.com/jobs"}]}))
        self.assertEqual(len(cli.parse_candidates(json_path)[0]), 1)

    def test_import_is_inactive_and_duplicate_does_not_overwrite_existing(self):
        item = discovery.EmployerImportItem(name="Example", source_type="greenhouse", source_token="example", careers_url="https://example.com/careers",
            website_url="https://example.com",evidence_url="https://example.com/careers",
            candidate_source="wikidata",candidate_record_url="https://www.wikidata.org/entity/Q123",candidate_license="CC0-1.0")
        with patch.object(discovery,"validate_public_https_url",side_effect=lambda value,**_kw:value):
            first = discovery.register_employer_sources(discovery.EmployerImportRequest(sources=[item]))
        changed = item.model_copy(update={"name": "Changed Name"})
        with patch.object(discovery,"validate_public_https_url",side_effect=lambda value,**_kw:value):
            second = discovery.register_employer_sources(discovery.EmployerImportRequest(sources=[changed]))
        self.assertEqual(first["imported"], 1)
        self.assertEqual(second["imported"], 0)
        row = next(row for row in cli._existing_sources() if row["employer_id"] == "greenhouse:example")
        self.assertEqual(row["name"], "Example")
        self.assertEqual(row["active"], 0)
        with discovery.database() as db:
            saved=db.execute("SELECT website_url,evidence_url,candidate_source,candidate_record_url,candidate_license,discovered_at,active FROM employers WHERE employer_id='greenhouse:example'").fetchone()
            stages=db.execute("SELECT COUNT(*) FROM employer_discovery_stages WHERE employer_id='greenhouse:example'").fetchone()[0]
        self.assertEqual(saved["candidate_source"],"wikidata")
        self.assertEqual(saved["candidate_record_url"],"https://www.wikidata.org/entity/Q123")
        self.assertEqual(saved["candidate_license"],"CC0-1.0")
        self.assertTrue(saved["discovered_at"])
        self.assertEqual(saved["active"],0)
        self.assertEqual(stages,4)

    def test_wikidata_importer_accepts_official_cc0_records_and_deduplicates(self):
        class Response:
            status_code=200; headers={}; content=b'{"results":{"bindings":[]}}'
            def raise_for_status(self): pass
            def json(self):
                return {"results":{"bindings":[
                    {"company":{"value":"https://www.wikidata.org/entity/Q123"},"companyLabel":{"value":"Example Corp"},"website":{"value":"https://example.com"}},
                    {"company":{"value":"https://www.wikidata.org/entity/Q124"},"companyLabel":{"value":"Example Corp"},"website":{"value":"https://example.com/"}},
                    {"company":{"value":"https://www.wikidata.org/entity/Q125"},"companyLabel":{"value":"Unsafe"},"website":{"value":"http://127.0.0.1/"}},
                ]}}
        original_validate=job_connectors.validate_public_https_url
        with patch.object(candidate_importers.job_connectors,"validate_public_https_url",side_effect=lambda value,**kw:original_validate(value,resolve=False)):
            result=candidate_importers.import_wikidata_candidates(20,getter=lambda *_a,**_kw:Response())
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]["candidate_source"],"wikidata")
        self.assertEqual(result[0]["candidate_license"],"CC0-1.0")
        self.assertEqual(result[0]["candidate_record_url"],"https://www.wikidata.org/entity/Q123")

    def test_wikidata_importer_rejects_private_url_and_bad_limit(self):
        with self.assertRaises(ValueError): candidate_importers.import_wikidata_candidates(0)
        with self.assertRaises(ValueError): candidate_importers.import_wikidata_candidates(501)
        class Response:
            status_code=200; headers={}; content=b'{}'
            def raise_for_status(self): pass
            def json(self): return {"results":{"bindings":[{"company":{"value":"https://www.wikidata.org/entity/Q1"},"companyLabel":{"value":"Bad"},"website":{"value":"https://127.0.0.1/"}}]}}
        diagnostics={}
        # The request is mocked and the unsafe record must be rejected as data,
        # without making the entire directory import fail.
        original_validate=job_connectors.validate_public_https_url
        with patch.object(candidate_importers.job_connectors,"validate_public_https_url",
                          side_effect=lambda value,**kw:original_validate(value,resolve=False)):
            rows,_=candidate_importers.fetch_wikidata_candidate_batch(
                1,getter=lambda *_a,**_kw:Response(),diagnostics=diagnostics)
        self.assertEqual(rows,[])
        self.assertEqual(diagnostics["records_rejected"],1)
        self.assertEqual(diagnostics["rejection_reasons"],{"malformed_or_unsafe_record":1})

    def test_wikidata_candidate_import_rejects_private_ranges_and_localhost(self):
        original_validate=job_connectors.validate_public_https_url
        for website in ("https://127.0.0.1/", "https://10.2.3.4/", "https://192.168.1.10/",
                        "https://[::1]/", "https://localhost/", "https://service.localhost/"):
            with self.subTest(website=website):
                with self.assertRaises(ValueError):
                    original_validate(website,resolve=False)

    def test_wikidata_rate_limit_stops_and_never_treats_records_as_verified(self):
        class Response:
            status_code=429; headers={"Retry-After":"30"}; content=b''
            def raise_for_status(self): raise AssertionError("must stop before interpreting as data")
        with self.assertRaises(Exception): candidate_importers.import_wikidata_candidates(1,getter=lambda *_a,**_kw:Response())

    def test_wikidata_keyset_pages_and_uses_configured_batch_timeout(self):
        def response(qid):
            class Response:
                status_code=200;headers={}
                content=b'{"results":{"bindings":[]}}'
                def raise_for_status(self):pass
                def json(self):return {"results":{"bindings":[{"company":{"value":f"https://www.wikidata.org/entity/{qid}"},
                    "companyLabel":{"value":"Company " + qid},"website":{"value":"https://example.com/"+qid}}]}}
            return Response()
        calls=[]
        def getter(url,**kwargs):calls.append((url,kwargs));return response("Q123" if len(calls)==1 else "Q456")
        with patch.object(candidate_importers.job_connectors,"validate_public_https_url",side_effect=lambda value,**_kw:value):
            first,cursor=candidate_importers.fetch_wikidata_candidate_batch(1,timeout=11,getter=getter,sleeper=lambda _:None,jitter=lambda *_:0)
            second,next_cursor=candidate_importers.fetch_wikidata_candidate_batch(1,cursor=cursor,timeout=11,getter=getter,sleeper=lambda _:None,jitter=lambda *_:0)
        self.assertEqual((len(first),cursor), (1,"Q123"))
        self.assertEqual((len(second),next_cursor),(1,"Q456"))
        self.assertEqual(calls[0][1]["timeout"],(5,11.0))
        first_query=parse_qs(urlparse(calls[0][0]).query)["query"][0]
        second_query=parse_qs(urlparse(calls[1][0]).query)["query"][0]
        self.assertIn("LIMIT 1",first_query)
        self.assertIn('STR(?company) > "https://www.wikidata.org/entity/Q123"',second_query)

    def test_wikidata_read_timeout_retries_with_exponential_backoff_and_jitter(self):
        class Response:
            status_code=200;headers={};content=b'{}'
            def raise_for_status(self):pass
            def json(self):return {"results":{"bindings":[]}}
        attempts=[];delays=[]
        def getter(*_args,**_kwargs):
            attempts.append(1)
            if len(attempts)<3: raise job_connectors.requests.ReadTimeout()
            return Response()
        with patch.object(candidate_importers.job_connectors,"validate_public_https_url",side_effect=lambda value,**_kw:value):
            rows,cursor=candidate_importers.fetch_wikidata_candidate_batch(5,getter=getter,sleeper=delays.append,jitter=lambda low,_high:low)
        self.assertEqual(len(attempts),3)
        self.assertEqual(delays,[0.5,1.0])
        self.assertEqual((rows,cursor),([],""))

    def test_yc_api_candidate_import_paginates_filters_and_preserves_provenance(self):
        class Response:
            status_code=200;headers={};content=b'[]'
            def raise_for_status(self):pass
            def json(self):return [
                {"id":1,"name":"Not Tech","slug":"not-tech","website":"https://example.org","tags":["Food"],"url":"https://www.ycombinator.com/companies/not-tech"},
                {"id":2,"name":"Dev Tools Co","slug":"dev-tools","website":"https://devtools.example","tags":["Developer Tools"],"url":"https://www.ycombinator.com/companies/dev-tools"},
                {"id":3,"name":"Cloud Co","slug":"cloud-co","website":"https://cloud.example","industries":["Infrastructure"],"url":"https://www.ycombinator.com/companies/cloud-co"},
            ]
        diagnostics={}
        with patch.object(candidate_importers.job_connectors,"validate_public_https_url",side_effect=lambda value,**kw:value):
            rows,cursor=candidate_importers.fetch_yc_candidate_batch(1,getter=lambda *_a,**_kw:Response(),diagnostics=diagnostics)
            next_rows,next_cursor=candidate_importers.fetch_yc_candidate_batch(1,cursor=cursor,getter=lambda *_a,**_kw:Response())
        self.assertEqual((rows[0]["name"],cursor),("Dev Tools Co","2"))
        self.assertEqual(rows[0]["candidate_source"],"yc_api")
        self.assertIn("license not specified",rows[0]["candidate_license"])
        self.assertEqual(next_rows[0]["name"],"Cloud Co")
        self.assertEqual(next_cursor,"3")
        self.assertTrue(diagnostics["has_more"])
        self.assertEqual(diagnostics["rejection_reasons"],{"not_technology_related":1})

    def test_yc_provider_timeout_is_bounded_and_provider_dispatch_is_independent(self):
        attempts=[]
        def timeout(*_a,**_kw):
            attempts.append(1);raise job_connectors.requests.ReadTimeout()
        with self.assertRaises(job_connectors.requests.ReadTimeout):
            candidate_importers.fetch_yc_candidate_batch(2,getter=timeout,sleeper=lambda _delay:None,jitter=lambda *_:0)
        self.assertEqual(len(attempts),3)
        self.assertEqual(expand_employers._selected_providers("all"),["yc_api"])
        self.assertEqual(expand_employers._selected_providers("wikidata"),["wikidata"])

    def test_multi_provider_failure_does_not_block_second_provider(self):
        report_path=Path(self.tempdir.name)/"fallback.json"
        seed={"name":"Fallback Corp","website_url":"https://fallback.example","candidate_source":"wikidata",
            "candidate_record_url":"https://www.wikidata.org/entity/Q555","candidate_license":"CC0-1.0"}
        def fetch(provider,_size,**_kwargs):
            if provider=="yc_api":raise job_connectors.requests.ReadTimeout()
            return [seed],"Q555"
        def mocked_validation(**_kwargs):
            result={"report_file":str(report_path),"employers_checked":0,"newly_activated":0,
                "validation_failures":0,"unavailable":0,"rate_limited":0,"interrupted":False,
                "network_connectivity":{"ok":True},"employers_selected":0,"successfully_verified":0,
                "total_postings_observed":0,"employers":[]}
            report_path.write_text(json.dumps(result))
            return result
        with patch.object(cli,"check_outbound_https",return_value={"ok":True,"status_code":200,"error":None}), \
             patch.object(expand_employers.candidate_importers,"fetch_provider_candidate_batch",side_effect=fetch), \
             patch.object(expand_employers.job_connectors,"discover_source_candidates",return_value=([],None)), \
             patch.object(expand_employers.time,"sleep",return_value=None), \
             patch.object(discovery,"validate_public_https_url",side_effect=lambda value,**_kw:value), \
             patch.object(cli,"run_verification",side_effect=mocked_validation):
            code=expand_employers.main(["--providers","yc_api,wikidata","--seed-limit","1","--limit","1","--report",str(report_path)])
        report=json.loads(report_path.read_text())
        statuses={r["provider"]:r["status"] for r in report["employer_expansion"]["providers"]}
        self.assertEqual(statuses,{"yc_api":"failed","wikidata":"succeeded"})
        self.assertEqual(report["employer_expansion"]["candidate_records_returned"],1)
        self.assertEqual(code,1)

    def test_retry_after_is_honored_for_wikidata_429(self):
        class Limited:
            status_code=429;headers={"Retry-After":"1"};content=b''
        class Success:
            status_code=200;headers={};content=b'{}'
            def raise_for_status(self):pass
            def json(self):return {"results":{"bindings":[]}}
        attempts=[];delays=[]
        def getter(*_args,**_kwargs):
            attempts.append(1);return Limited() if len(attempts)==1 else Success()
        with patch.object(candidate_importers.job_connectors,"validate_public_https_url",side_effect=lambda value,**_kw:value):
            candidate_importers.fetch_wikidata_candidate_batch(3,getter=getter,sleeper=delays.append,jitter=lambda *_:0)
        self.assertEqual(len(attempts),2)
        self.assertEqual(delays,[1.0])

    def test_checkpoint_upserts_candidate_batches_and_resumes_cursor(self):
        seed={"name":"Saved","website_url":"https://saved.example","candidate_source":"wikidata",
            "candidate_record_url":"https://www.wikidata.org/entity/Q42","candidate_license":"CC0-1.0"}
        with patch.object(discovery,"validate_public_https_url",side_effect=lambda value,**_kw:value):
            discovery.persist_candidate_seeds([seed]); discovery.persist_candidate_seeds([seed])
        discovery.save_employer_import_checkpoint("wikidata","Q42",2,"retry_pending","ReadTimeout")
        checkpoint=discovery.get_employer_import_checkpoint("wikidata")
        pending=discovery.get_pending_candidate_seeds(10,"wikidata")
        with discovery.database() as db: count=db.execute("SELECT COUNT(*) FROM employer_candidate_seeds").fetchone()[0]
        self.assertEqual(checkpoint["cursor_value"],"Q42")
        self.assertEqual(checkpoint["pages_completed"],2)
        self.assertEqual(checkpoint["status"],"retry_pending")
        self.assertEqual(count,1)
        self.assertEqual(pending[0]["name"],"Saved")

    def test_candidate_persistence_reports_insert_reuse_and_rejection_counts(self):
        good={"name":"Counted Corp","website_url":"https://counted.example","candidate_source":"wikidata",
            "candidate_record_url":"https://www.wikidata.org/entity/Q777","candidate_license":"CC0-1.0"}
        bad={**good,"name":"Unsafe Corp","website_url":"http://127.0.0.1/","candidate_record_url":"https://www.wikidata.org/entity/Q778"}
        with patch.object(discovery,"validate_public_https_url",side_effect=lambda value,**kw: (_ for _ in ()).throw(ValueError()) if value.startswith("http://") else value):
            first=discovery.persist_candidate_seeds([good,bad],return_details=True)
            second=discovery.persist_candidate_seeds([good],return_details=True)
            alternate=discovery.persist_candidate_seeds([{**good,"candidate_source":"yc_api",
                "candidate_record_url":"https://www.ycombinator.com/companies/counted","candidate_license":"unknown terms"}],return_details=True)
        self.assertEqual(first,{"inserted":1,"reused":0,"rejected":1,"rejection_reasons":{"unsafe_or_invalid_url":1}})
        self.assertEqual(second["reused"],1)
        self.assertEqual(alternate["reused"],1)
        with discovery.database() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM employer_candidate_seeds").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM employer_candidate_provenance").fetchone()[0],2)

    def test_empty_first_page_checkpoint_is_migrated_to_retryable_status(self):
        discovery.save_employer_import_checkpoint("wikidata","",1,"complete")
        discovery.initialize()
        checkpoint=discovery.get_employer_import_checkpoint("wikidata")
        self.assertEqual(checkpoint["status"],"empty_result")
        self.assertIn("Previous run completed with no usable",checkpoint["last_error"])

    def test_provider_health_api_is_loopback_only_and_reports_opt_in_wikidata(self):
        local=Request({"type":"http","client":("127.0.0.1",1234),"headers":[]})
        data=discovery.employer_discovery_providers(local)
        providers={p["provider"]:p for p in data["providers"]}
        self.assertIn("yc_api",providers)
        self.assertIn("dataset license not specified",providers["yc_api"]["access_terms"])
        self.assertIn("wikidata",providers)
        self.assertEqual(providers["yc_api"]["candidate_count"],0)
        remote=Request({"type":"http","client":("203.0.113.9",1234),"headers":[]})
        with self.assertRaises(HTTPException): discovery.employer_discovery_providers(remote)

    def test_empty_wikidata_run_reports_zero_stage_counts_and_non_success(self):
        report_path=Path(self.tempdir.name)/"empty-expansion.json"
        with patch.object(cli,"check_outbound_https",return_value={"ok":True,"status_code":200,"error":None}), \
             patch.object(expand_employers.candidate_importers,"fetch_provider_candidate_batch",return_value=([],"")) as fetch, \
             patch.object(expand_employers.time,"sleep",return_value=None):
            result=expand_employers.main(["--limit","5","--seed-limit","5","--report",str(report_path)])
        self.assertEqual(fetch.call_count,1)
        self.assertEqual(fetch.call_args.args[0],"yc_api")
        self.assertEqual(result,2)
        report=json.loads(report_path.read_text())
        stage=report["employer_expansion"]
        self.assertEqual(stage["directory_request_status"],"succeeded_empty")
        self.assertEqual(stage["candidate_records_returned"],0)
        self.assertEqual(stage["career_pages_inspected"],0)
        self.assertEqual(stage["ats_sources_discovered"],0)
        self.assertEqual(stage["sources_validated"],0)
        self.assertIn("no expansion work was performed",stage["message"])
        checkpoint=discovery.get_employer_import_checkpoint("yc_api")
        self.assertEqual(checkpoint["status"],"empty_result")
        self.assertIn("No usable first-page records",checkpoint["last_error"])

    def test_auto_expansion_pipeline_discovers_and_validates_with_persisted_provenance(self):
        old_db=discovery.DB
        discovery.DB=Path(self.tempdir.name)/"auto.sqlite3"
        discovery.initialize()
        original=cli.run_verification
        def mocked_run(**kwargs):
            kwargs["network_checker"]=lambda:{"ok":True,"status_code":200,"error":None}
            raw={"id":"job-1","text":"Software Engineer","hostedUrl":"https://jobs.lever.co/example/1"}
            kwargs["source_verifier"]=lambda employer_id:discovery.verify_employer_source_record(employer_id,fetcher=lambda _source:([raw],None))
            return original(**kwargs)
        try:
            with patch.object(cli,"run_verification",side_effect=mocked_run), \
                 patch.object(cli,"check_outbound_https",return_value={"ok":True,"status_code":200,"error":None}), \
                 patch.object(expand_employers.candidate_importers,"fetch_provider_candidate_batch",return_value=([{
                     "name":"Example Corp","website_url":"https://example.com","candidate_source":"wikidata",
                     "candidate_record_url":"https://www.wikidata.org/entity/Q123","candidate_license":"CC0-1.0"}],"Q123")), \
                 patch.object(expand_employers.job_connectors,"discover_source_candidates",return_value=([{
                     "name":"Example Corp","source_type":"lever","source_token":"example","careers_url":"https://jobs.lever.co/example",
                     "website_url":"https://example.com","evidence_url":"https://example.com/careers","status":"needs_validation"}],None)), \
                 patch.object(discovery,"validate_public_https_url",side_effect=lambda value,**_kw:value), \
                 patch.object(expand_employers.time,"sleep",return_value=None):
                code=expand_employers.main(["--providers","wikidata","--limit","1","--report",str(Path(self.tempdir.name)/"auto-report.json")])
            self.assertEqual(code,0)
            with discovery.database() as db:
                row=db.execute("SELECT active,candidate_source,candidate_license,candidate_record_url FROM employers WHERE source_type='lever' AND source_token='example'").fetchone()
                seed=db.execute("SELECT status,candidate_source,candidate_record_url FROM employer_candidate_seeds").fetchone()
            self.assertEqual(tuple(row),(1,"wikidata","CC0-1.0","https://www.wikidata.org/entity/Q123"))
            self.assertEqual(tuple(seed),("career_source_found","wikidata","https://www.wikidata.org/entity/Q123"))
        finally:
            discovery.DB=old_db

    def test_timeout_after_saved_batch_falls_back_to_seed_and_rerun_is_duplicate_safe(self):
        original=cli.run_verification
        def mocked_run(**kwargs):
            kwargs["network_checker"]=lambda:{"ok":True,"status_code":200,"error":None}
            raw={"id":"job-1","text":"Software Engineer","hostedUrl":"https://jobs.lever.co/resumable/1"}
            kwargs["source_verifier"]=lambda employer_id:discovery.verify_employer_source_record(employer_id,fetcher=lambda _source:([raw],None))
            return original(**kwargs)
        seed={"name":"Resumable Corp","website_url":"https://resumable.example","candidate_source":"wikidata",
            "candidate_record_url":"https://www.wikidata.org/entity/Q900","candidate_license":"CC0-1.0"}
        page_fetch=([seed],"Q900")
        with patch.object(cli,"run_verification",side_effect=mocked_run), \
             patch.object(cli,"check_outbound_https",return_value={"ok":True,"status_code":200,"error":None}), \
             patch.object(expand_employers.candidate_importers,"fetch_provider_candidate_batch",side_effect=[page_fetch,job_connectors.requests.ReadTimeout()]), \
             patch.object(expand_employers.job_connectors,"discover_source_candidates",return_value=([{
                 "name":"Resumable Corp","source_type":"lever","source_token":"resumable","careers_url":"https://jobs.lever.co/resumable",
                 "website_url":"https://resumable.example","evidence_url":"https://resumable.example/careers"}],None)), \
             patch.object(discovery,"validate_public_https_url",side_effect=lambda value,**_kw:value), \
             patch.object(expand_employers.time,"sleep",return_value=None):
            result=expand_employers.main(["--providers","wikidata","--limit","5","--seed-limit","2","--dataset-batch-size","1","--report",str(Path(self.tempdir.name)/"partial.json")])
        self.assertEqual(result,1)  # Partial work was retained, but the directory import needs resuming.
        report=json.loads((Path(self.tempdir.name)/"partial.json").read_text())
        self.assertEqual(report["employer_expansion"]["providers"][0]["failure"]["status"],"ReadTimeout")
        checkpoint=discovery.get_employer_import_checkpoint("wikidata")
        self.assertEqual(checkpoint["cursor_value"],"Q900")
        with discovery.database() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM employer_candidate_seeds").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT active FROM employers WHERE employer_id='lever:resumable'").fetchone()[0],1)
        # Resume from the saved QID. Empty page completes without revisiting or duplicating the source.
        with patch.object(cli,"run_verification",side_effect=mocked_run), \
             patch.object(cli,"check_outbound_https",return_value={"ok":True,"status_code":200,"error":None}), \
             patch.object(expand_employers.candidate_importers,"fetch_provider_candidate_batch",return_value=([],"Q900")) as fetch, \
             patch.object(expand_employers.time,"sleep",return_value=None):
            expand_employers.main(["--providers","wikidata","--limit","5","--seed-limit","2","--dataset-batch-size","1","--report",str(Path(self.tempdir.name)/"resume.json")])
        self.assertIn("Q900",fetch.call_args.kwargs["cursor"])
        with discovery.database() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM employer_candidate_seeds").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM employers WHERE source_type='lever' AND source_token='resumable'").fetchone()[0],1)

    def test_live_cli_flow_is_marked_mocked_in_deterministic_test(self):
        path = self._csv([{"name": "Example", "source_type": "greenhouse", "source_token": "example", "careers_url": "https://example.com/careers"}])
        output = []
        report_path = Path(self.tempdir.name) / "report.json"
        result = cli.run_verification(candidates_path=path, limit=1, report_path=report_path,
            network_checker=lambda: {"ok": True, "status_code": 200, "error": None},
            source_verifier=lambda _id: {"valid": True, "status": "healthy", "jobs_found": 2, "active": True, "evidence": {"records_valid": 2}},
            output=output.append)
        self.assertEqual(result["newly_activated"], 1)
        self.assertEqual(result["total_postings_observed"], 2)
        saved = json.loads(report_path.read_text())
        self.assertTrue(saved["mocked"])
        self.assertEqual(saved["mode"], "mocked_test")
        self.assertEqual(saved["report_file"], str(report_path))

    def test_network_failure_skips_sources_without_mutating_health(self):
        item = discovery.EmployerImportItem(name="Example", source_type="greenhouse", source_token="example", careers_url="https://example.com/careers")
        discovery.register_employer_sources(discovery.EmployerImportRequest(sources=[item]))
        source = next(row for row in cli._existing_sources() if row["employer_id"] == "greenhouse:example")
        result = cli.run_verification(limit=1, report_path=Path(self.tempdir.name) / "offline.json",
            network_checker=lambda: {"ok": False, "error": "offline"}, output=lambda _: None)
        self.assertEqual(result["results"][0]["status"], "outbound_unavailable")
        checked = next(row for row in cli._existing_sources() if row["employer_id"] == "greenhouse:example")
        self.assertEqual(checked["connector_status"], source["connector_status"])

    def test_header_only_candidate_file_verifies_configured_directory(self):
        path = Path(self.tempdir.name) / "empty.csv"
        path.write_text("name,source_type,source_token,careers_url,geographic_coverage\n")
        result = cli.run_verification(candidates_path=path, limit=2,
            report_path=Path(self.tempdir.name) / "configured.json",
            network_checker=lambda: {"ok": False, "error": "offline"}, output=lambda _: None)
        self.assertEqual(result["employers_selected"], 2)
        self.assertEqual(len(result["results"]), 2)

    def test_limit_and_interrupt_are_safe_and_report_progress(self):
        with self.assertRaises(ValueError):
            cli.run_verification(limit=101, network_checker=lambda: {"ok": True})
        items = [discovery.EmployerImportItem(name=f"Example {n}", source_type="greenhouse", source_token=f"board-{n}", careers_url="https://example.com/careers") for n in range(3)]
        discovery.register_employer_sources(discovery.EmployerImportRequest(sources=items))
        checked = []
        def stop_after_one(employer_id):
            checked.append(employer_id)
            if len(checked) == 2:
                raise KeyboardInterrupt()
            return {"valid": True, "status": "healthy", "jobs_found": 0, "active": True, "evidence": {}}
        result = cli.run_verification(limit=2, report_path=Path(self.tempdir.name) / "interrupted.json",
            network_checker=lambda: {"ok": True}, source_verifier=stop_after_one, output=lambda _: None)
        self.assertTrue(result["interrupted"])
        self.assertEqual(len(checked), 2)

    def test_argument_validation(self):
        with self.assertRaises(SystemExit) as caught:
            cli.main(["--limit", "0"])
        self.assertEqual(caught.exception.code, 2)


class SourceValidationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_db = discovery.DB
        discovery.DB = Path(self.tempdir.name) / "source.sqlite3"
        discovery.initialize()
        item = discovery.EmployerImportItem(name="Example", source_type="greenhouse", source_token="example", careers_url="https://example.com/careers")
        discovery.register_employer_sources(discovery.EmployerImportRequest(sources=[item]))
        self.employer_id = next(row["employer_id"] for row in cli._existing_sources() if row["employer_id"] == "greenhouse:example")

    def tearDown(self):
        discovery.DB = self.old_db
        self.tempdir.cleanup()

    def test_structurally_valid_provider_response_activates_and_records_sanitized_evidence(self):
        raw = {"id": "123", "title": "Software Engineer", "absolute_url": "https://boards.greenhouse.io/example/jobs/123", "location": {"name": "Austin, TX"}}
        with patch.object(discovery, "validate_public_https_url", return_value="https://boards.greenhouse.io/example/jobs/123"):
            result = discovery.verify_employer_source_record(self.employer_id, fetcher=lambda _source: ([raw], None))
        self.assertTrue(result["valid"])
        self.assertTrue(result["active"])
        with discovery.database() as db:
            row = db.execute("SELECT verification_evidence_json FROM employers WHERE employer_id=?", (self.employer_id,)).fetchone()
        evidence = json.loads(row[0])
        self.assertEqual(evidence["records_valid"], 1)
        self.assertNotIn("Software Engineer", json.dumps(evidence))

    def test_malformed_response_does_not_activate_candidate(self):
        with patch.object(discovery, "validate_public_https_url", return_value="https://boards.greenhouse.io/example/jobs/1"):
            result = discovery.verify_employer_source_record(self.employer_id, fetcher=lambda _source: ([{"title": "No ID", "absolute_url": "https://boards.greenhouse.io/example/jobs/1"}], None))
        self.assertFalse(result["valid"])
        self.assertFalse(result["active"])

    def test_provider_failure_and_rate_limit_are_recorded_without_deactivation(self):
        result = discovery.verify_employer_source_record(self.employer_id, fetcher=lambda _source: ([], "HTTP 429 rate limited; Retry-After 60s"))
        self.assertEqual(result["status"], "rate_limited")
        with discovery.database() as db:
            row = db.execute("SELECT active,connector_status FROM employers WHERE employer_id=?", (self.employer_id,)).fetchone()
            db.execute("UPDATE employers SET active=1 WHERE employer_id=?", (self.employer_id,))
        result = discovery.verify_employer_source_record(self.employer_id, fetcher=lambda _source: (_ for _ in ()).throw(TimeoutError()), force=True)
        self.assertEqual(result["status"], "network_timeout")

    def test_cache_observer_reports_mocked_provider_response(self):
        class Response:
            status_code = 200
            headers = {}
            content = b"{}"
            def raise_for_status(self): pass
            def json(self): return [{"id": "1", "title": "Engineer"}]
        job_connectors.clear_cache()
        source = job_connectors.EmployerSource("Example", "greenhouse", "example")
        with job_connectors.observe_fetch() as observer:
            job_connectors.fetch_greenhouse(source, getter=lambda *_args, **_kwargs: Response())
        self.assertEqual(observer["responses"][0]["status_code"], 200)


if __name__ == "__main__":
    unittest.main()
