"""Safe local CLI for read-only validation and expansion of public job sources.

Examples:
    python verify_employers.py
    python verify_employers.py --candidates employer_candidates.csv --limit 20

Newly imported sources remain inactive unless a live, structurally valid public
response is received. This tool never opens an application form or submits one.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

import job_connectors as connectors
import job_search_agent as discovery

MAX_CANDIDATE_FILE_BYTES = 2 * 1024 * 1024
MAX_CANDIDATE_ROWS = 1000
MAX_BATCH = 100


def parse_candidates(path: str | Path):
    """Read CSV or JSON candidates, preserving row-level validation failures."""
    source_path = Path(path)
    if not source_path.is_file():
        raise ValueError("Candidate file does not exist or is not a regular file.")
    if source_path.stat().st_size > MAX_CANDIDATE_FILE_BYTES:
        raise ValueError("Candidate file exceeds the 2 MiB size limit.")
    suffix = source_path.suffix.casefold()
    if suffix == ".csv":
        with source_path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    elif suffix == ".json":
        data = json.loads(source_path.read_text(encoding="utf-8"))
        rows = data.get("sources") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise ValueError("JSON candidate file must be a list or an object with a 'sources' list.")
    else:
        raise ValueError("Candidate file must use .csv or .json extension.")
    if len(rows) > MAX_CANDIDATE_ROWS:
        raise ValueError(f"Candidate file may contain at most {MAX_CANDIDATE_ROWS} rows.")
    valid, invalid, seen = [], [], set()
    for index, row in enumerate(rows, start=2 if suffix == ".csv" else 1):
        try:
            if not isinstance(row, dict):
                raise ValueError("candidate row must be an object")
            item = discovery.EmployerImportItem.model_validate(row)
            source_type = item.source_type.casefold()
            token = item.source_token.strip()
            careers_url = item.careers_url.strip()
            url_based = source_type in {"career_page", "public_html", "rss", "sitemap", "workday"}
            if url_based:
                connectors.validate_public_https_url(careers_url)
            elif not token:
                raise ValueError("a public provider identifier is required")
            key = (source_type, token if token else careers_url.rstrip("/").casefold())
            if key in seen:
                invalid.append({"row": index, "status": "duplicate_in_file", "reason": "Duplicate source entry in candidate file."})
                continue
            seen.add(key)
            valid.append(item)
        except Exception as exc:
            reason = getattr(exc, "detail", None) or str(exc)
            invalid.append({"row": index, "status": "invalid_candidate", "reason": str(reason)[:300]})
    return valid, invalid


def check_outbound_https(*, getter=None):
    """Probe a fixed, well-known HTTPS domain; no caller-supplied URL is fetched."""
    getter = getter or requests.get
    url = "https://example.com/"
    try:
        connectors.validate_public_https_url(url)
        response = getter(url, timeout=(3, 5), allow_redirects=False, headers={"User-Agent": "BabyStepsAI source verification connectivity check"})
        return {"ok": True, "status_code": getattr(response, "status_code", None), "error": None}
    except requests.Timeout:
        return {"ok": False, "status_code": None, "error": "Outbound HTTPS connectivity check timed out."}
    except requests.RequestException as exc:
        return {"ok": False, "status_code": None, "error": f"Outbound HTTPS connectivity check failed: {type(exc).__name__}."}
    except ValueError as exc:
        return {"ok": False, "status_code": None, "error": str(exc)}


def _existing_sources():
    with discovery.database() as db:
        rows = db.execute("SELECT * FROM employers WHERE connector_status!='disabled'").fetchall()
    eligible=[dict(row) for row in rows if discovery.employer_retry_eligible(dict(row))]
    # Prefer already supported public feeds for useful validations per bounded
    # run, while retaining custom career pages for a later eligible slot.
    eligible.sort(key=lambda row:(bool(row.get("active")),row.get("source_type") not in {"greenhouse","lever","ashby","smartrecruiters"},row.get("last_attempt_at") or "",row["name"].casefold()))
    return eligible


def _register_candidates(items):
    if not items:
        return
    discovery.register_employer_sources(discovery.EmployerImportRequest(sources=items))


def _employer_id_for(item):
    source_type = item.source_type.casefold()
    token = item.source_token.strip()
    if source_type in {"career_page", "public_html", "rss", "sitemap", "workday"}:
        import hashlib
        token = hashlib.sha256(item.careers_url.strip().encode("utf-8")).hexdigest()[:24]
    return f"{source_type}:{token}"


def _status_bucket(result):
    status = result.get("status")
    if status in {"temporarily_unavailable","network_timeout","permanently_unavailable"}:
        return "unavailable"
    if status == "rate_limited":
        return "rate_limited"
    if status in {"unsupported","unsupported_structure","unsupported_provider","javascript_rendered"}:
        return "unsupported"
    if status in {"invalid_configuration","invalid_provider_identifier","malformed_response","authentication_required","access_restricted","robots_disallowed"}:
        return "validation_failures"
    return "success" if result.get("valid") else "validation_failures"


def run_verification(*, candidates_path=None, limit=20, report_path=None,
                     network_checker=check_outbound_https, source_verifier=None, mocked=None,
                     employer_ids=None,
                     network_result=None,
                     output=print, now=None):
    """Verify configured sources or imported candidate rows, sequentially."""
    if not 1 <= int(limit) <= MAX_BATCH:
        raise ValueError(f"--limit must be between 1 and {MAX_BATCH}.")
    started = (now or datetime.now(timezone.utc)).isoformat()
    invalid_rows, import_error = [], None
    items = []
    if candidates_path:
        try:
            items, invalid_rows = parse_candidates(candidates_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            import_error = str(exc)
            invalid_rows.append({"row": None, "status": "candidate_file_error", "reason": import_error})
        items = items[:int(limit)]
        try:
            _register_candidates(items)
        except Exception as exc:
            import_error = str(getattr(exc, "detail", None) or type(exc).__name__)
            invalid_rows.append({"row": None, "status": "candidate_import_error", "reason": import_error[:300]})
            items = []

    # A header-only example file is useful as a safe default: it means “verify
    # the configured directory.” Invalid candidate rows do not silently switch
    # scope, because that could obscure a rejected import.
    if employer_ids is not None:
        with discovery.database() as db:
            source_rows=[dict(row) for row in db.execute(
                f"SELECT employer_id,name,source_type,source_token,careers_url,geographic_coverage,active,connector_status FROM employers WHERE employer_id IN ({','.join('?' for _ in employer_ids)}) ORDER BY name COLLATE NOCASE LIMIT ?",
                [*employer_ids,int(limit)]).fetchall()] if employer_ids else []
    elif candidates_path and (items or invalid_rows):
        source_rows = []
        for item in items:
            source_rows.append({"employer_id":_employer_id_for(item),"name":item.name,
                "source_type":item.source_type,"source_token":item.source_token,
                "careers_url":item.careers_url,"geographic_coverage":item.geographic_coverage})
    else:
        source_rows = _existing_sources()[:int(limit)]

    # Dependency injection is used by deterministic tests. Mark those reports
    # as mocked so no test output can be mistaken for live source validation.
    if mocked is None:
        mocked = network_checker is not check_outbound_https or source_verifier is not None
    connectivity = network_result if network_result is not None else network_checker()
    report = {"mode":"mocked_test" if mocked else "live_read_only_verification", "mocked":mocked, "started_at":started,
        "finished_at":None,"interrupted":False,"network_connectivity":connectivity,
        "employers_selected":len(source_rows),"employers_checked":0,
        "successfully_verified":0,"newly_activated":0,"unavailable":0,
        "rate_limited":0,"unsupported":0,"validation_failures":len(invalid_rows),
        "total_postings_observed":0,"candidate_file_errors":invalid_rows,
        "results":[],"message":import_error}

    # If general egress is down, don't spend many minutes repeating guaranteed
    # timeouts. The CLI records these as skipped, not as source-health failures.
    if not connectivity.get("ok"):
        for source in source_rows:
            report["results"].append({"employer":source["name"],"source_type":source["source_type"],
                "status":"outbound_unavailable","checked_live":False,
                "reason":"Skipped after the general outbound HTTPS probe failed; employer health was not changed."})
        report["finished_at"]=(now or datetime.now(timezone.utc)).isoformat()
    else:
        verifier = source_verifier or discovery.verify_employer_source_record
        try:
            for index, source in enumerate(source_rows, start=1):
                output(f"[{index}/{len(source_rows)}] Checking {source['name']} ({source['source_type']})…")
                connectors.clear_cache()  # A cached response must not count as live verification.
                before_active = bool(source.get("active", 0))
                try:
                    result = verifier(source["employer_id"])
                    if result is None:
                        result = {"valid":False,"status":"invalid_configuration","active":False,
                                  "jobs_found":0,"message":"Employer source record was not found.","evidence":{}}
                    bucket = _status_bucket(result)
                    report["employers_checked"] += 1
                    if bucket != "success":
                        report[bucket] += 1
                    report["total_postings_observed"] += int(result.get("jobs_found",0) or 0)
                    if result.get("valid"):
                        report["successfully_verified"] += 1
                        if not before_active and result.get("active"):
                            report["newly_activated"] += 1
                    report["results"].append({"employer":source["name"],"source_type":source["source_type"],
                        "status":result.get("status"),"checked_live":True,"valid":bool(result.get("valid")),
                        "active":bool(result.get("active")),"postings_observed":int(result.get("jobs_found",0) or 0),
                        "evidence":result.get("evidence",{}),"reason":result.get("error") or (None if result.get("valid") else result.get("message"))})
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    report["employers_checked"] += 1
                    report["validation_failures"] += 1
                    report["results"].append({"employer":source["name"],"source_type":source["source_type"],
                        "status":"validation_failures","checked_live":True,"valid":False,
                        "reason":f"Verification failed: {type(exc).__name__}."})
        except KeyboardInterrupt:
            report["interrupted"] = True
            output("Interrupted safely. Completed validations are preserved; rerun to continue idempotently.")
        report["finished_at"]=(now or datetime.now(timezone.utc)).isoformat()

    if report_path:
        target=Path(report_path)
    else:
        suffix=datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        target=Path.cwd()/f"employer-verification-{suffix}.json"
    target.parent.mkdir(parents=True,exist_ok=True)
    report["report_file"] = str(target)
    fd_name=None
    try:
        with tempfile.NamedTemporaryFile("w",encoding="utf-8",dir=target.parent,delete=False,suffix=".tmp") as handle:
            fd_name=handle.name
            json.dump(report,handle,indent=2,ensure_ascii=False)
            handle.write("\n")
        Path(fd_name).replace(target)
    finally:
        if fd_name and Path(fd_name).exists():
            Path(fd_name).unlink()
    output("\nEmployer verification summary")
    for label,key in (("Selected","employers_selected"),("Checked live","employers_checked"),
        ("Verified","successfully_verified"),("Newly activated","newly_activated"),
        ("Unavailable","unavailable"),("Rate limited","rate_limited"),
        ("Unsupported","unsupported"),("Validation failures","validation_failures"),
        ("Postings observed","total_postings_observed")):
        output(f"  {label}: {report[key]}")
    output(f"  Network HTTPS: {'available' if connectivity.get('ok') else 'unavailable'}")
    output(f"  JSON report: {target}")
    return report


def build_parser():
    parser=argparse.ArgumentParser(description="Verify public employer job sources without submitting applications.")
    parser.add_argument("--candidates",help="Local CSV or JSON source list to import and validate")
    parser.add_argument("--limit",type=int,default=20,help=f"Maximum sources to check (1–{MAX_BATCH}; default 20)")
    parser.add_argument("--report",help="JSON report path (default: timestamped report in current directory)")
    return parser


def main(argv=None):
    parser=build_parser()
    args=parser.parse_args(argv)
    if not 1 <= args.limit <= MAX_BATCH:
        parser.error(f"--limit must be between 1 and {MAX_BATCH}.")
    try:
        report=run_verification(candidates_path=args.candidates,limit=args.limit,report_path=args.report)
    except KeyboardInterrupt:
        print("Interrupted before verification began.",file=sys.stderr)
        return 130
    if report["interrupted"]:
        return 130
    if report["validation_failures"] or report["unavailable"] or report["rate_limited"] or not report["network_connectivity"].get("ok"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
