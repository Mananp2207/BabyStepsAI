"""Multi-provider, checkpointed employer discovery and read-only validation CLI.

Wikidata is an explicit opt-in provider. ``--providers all`` uses the documented
YC companies JSON API and saved candidates, then discovers official career links.
New feeds remain inactive until live validation succeeds. This program never
submits applications.
"""
from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path

import verify_employers
import job_connectors
import candidate_importers


def build_parser():
    parser=argparse.ArgumentParser(description="Discover and verify public employer job sources safely.")
    parser.add_argument("--providers",default="all",help="Comma-separated providers: all, yc_api, wikidata (Wikidata is opt-in; saved candidates are always resumed).")
    parser.add_argument("--candidates",help="Optional CSV/JSON candidate input")
    parser.add_argument("--limit",type=int,default=20,help="Maximum inactive feeds to validate (1-100)")
    parser.add_argument("--seed-limit",type=int,default=100,help="Maximum pending companies to inspect (1-500)")
    parser.add_argument("--batch-size","--dataset-batch-size",dest="batch_size",type=int,default=20,help="Provider result page size (1-100)")
    parser.add_argument("--timeout","--dataset-timeout",dest="timeout",type=float,default=20,help="Provider HTTP read timeout (5-120 seconds)")
    parser.add_argument("--restart-directory",action="store_true",help="Reset selected providers' cursors")
    parser.add_argument("--validate-only",action="store_true",help="Validate registered sources without importing providers")
    parser.add_argument("--report",help="JSON report output path")
    return parser


def _selected_providers(value: str) -> list[str]:
    requested=[part.strip().casefold() for part in str(value or "").split(",") if part.strip()]
    if not requested: raise ValueError("Choose at least one provider.")
    if "all" in requested:
        if len(requested)>1: raise ValueError("Use 'all' by itself or list individual providers.")
        return ["yc_api"]
    if len(requested)!=len(set(requested)): requested=list(dict.fromkeys(requested))
    unknown=set(requested)-set(candidate_importers.PROVIDER_REGISTRY)
    if unknown: raise ValueError("Unsupported provider(s): "+", ".join(sorted(unknown)))
    return requested


def main(argv=None):
    parser=build_parser();args=parser.parse_args(argv)
    if not 1<=args.limit<=verify_employers.MAX_BATCH: parser.error(f"--limit must be between 1 and {verify_employers.MAX_BATCH}.")
    if not 1<=args.seed_limit<=500: parser.error("--seed-limit must be between 1 and 500.")
    if not 1<=args.batch_size<=100: parser.error("--batch-size must be between 1 and 100.")
    if not 5<=args.timeout<=120: parser.error("--timeout must be between 5 and 120 seconds.")
    if args.validate_only and args.candidates: parser.error("--validate-only cannot be combined with --candidates.")
    try: providers=[] if args.validate_only else _selected_providers(args.providers)
    except ValueError as error: parser.error(str(error))

    connectivity=verify_employers.check_outbound_https() if providers or args.candidates else None
    provider_reports=[];all_directory_rows=[];failures=[]
    stats={"directory_source_requested":providers,"directory_request_status":"not_started",
        "candidate_records_returned":0,"candidate_records_inserted":0,"candidate_records_reused":0,
        "candidate_records_rejected":0,"rejection_reasons":{},"candidates_available_for_discovery":0,
        "career_pages_inspected":0,"ats_sources_discovered":0,"inactive_sources_registered":0,
        "sources_validated":0,"sources_activated":0,"failures":failures}

    if args.candidates:
        path=Path(args.candidates)
        try:
            if not path.is_file() or path.stat().st_size>verify_employers.MAX_CANDIDATE_FILE_BYTES:
                raise ValueError("Candidate file is missing or exceeds the 2 MiB limit.")
            if path.suffix.casefold()==".csv":
                with path.open(encoding="utf-8-sig",newline="") as handle: candidate_rows=list(csv.DictReader(handle))
            elif path.suffix.casefold()==".json":
                data=json.loads(path.read_text(encoding="utf-8"));candidate_rows=data.get("sources") if isinstance(data,dict) else data
            else: raise ValueError("Candidate file must be CSV or JSON.")
            if not isinstance(candidate_rows,list) or len(candidate_rows)>verify_employers.MAX_CANDIDATE_ROWS:
                raise ValueError("Candidate file must contain at most 1,000 rows.")
        except (OSError,ValueError,json.JSONDecodeError) as error: parser.error(str(error))
        website_rows=[row for row in candidate_rows if isinstance(row,dict) and row.get("website_url") and not row.get("source_type")]
        if website_rows:
            for row in website_rows:
                row.setdefault("candidate_source","local_seed")
                row.setdefault("candidate_record_url",row.get("evidence_url") or row["website_url"])
                row.setdefault("candidate_license","user-supplied candidate record")
                row.setdefault("evidence_url",row["website_url"])
            saved=verify_employers.discovery.persist_candidate_seeds(website_rows,return_details=True)
            stats.update({"candidate_records_returned":len(website_rows),"candidate_records_inserted":saved["inserted"],
                "candidate_records_reused":saved["reused"],"candidate_records_rejected":saved["rejected"],
                "rejection_reasons":saved["rejection_reasons"]})
            candidate_rows=[row for row in candidate_rows if row not in website_rows]
        candidates_path=None
    else:
        candidate_rows=[];candidates_path=None

    if providers:
        if not connectivity.get("ok"):
            stats["directory_request_status"]="skipped_network_unavailable"
            for provider in providers:
                failure={"stage":"directory_import","provider":provider,"status":"outbound_unavailable",
                         "reason":"HTTPS probe failed; provider skipped and saved candidates retained."}
                failures.append(failure)
                provider_reports.append({"provider":provider,"status":"outbound_unavailable","candidates_returned":0,
                    "inserted":0,"reused":0,"rejected":0,"failure":failure})
        else:
            stats["directory_request_status"]="requested"
            for provider in providers:
                checkpoint=verify_employers.discovery.get_employer_import_checkpoint(provider)
                if args.restart_directory:
                    checkpoint={"cursor_value":"","pages_completed":0,"status":"restarted"}
                    verify_employers.discovery.save_employer_import_checkpoint(provider,"",0,"restarted")
                progress={"provider":provider,"status":"running","candidates_returned":0,
                    "inserted":0,"reused":0,"rejected":0,"rejection_reasons":{},"pages":0,"failure":None}
                max_pages=(args.seed_limit+args.batch_size-1)//args.batch_size+2
                for _ in range(max_pages):
                    remaining=args.seed_limit-progress["candidates_returned"]
                    if remaining<=0: break
                    page_size=min(args.batch_size,remaining)
                    try:
                        diagnostics={}
                        rows,next_cursor=candidate_importers.fetch_provider_candidate_batch(provider,page_size,
                            cursor=checkpoint["cursor_value"],timeout=args.timeout,diagnostics=diagnostics)
                    except Exception as error:
                        progress["status"]="failed"
                        progress["failure"]={"stage":"directory_import","provider":provider,
                            "status":type(error).__name__,"reason":f"Provider request failed: {type(error).__name__}."}
                        failures.append(progress["failure"])
                        verify_employers.discovery.save_employer_import_checkpoint(provider,checkpoint["cursor_value"],
                            checkpoint["pages_completed"],"retry_pending",type(error).__name__)
                        print(f"[directory_import:{provider}] failed ({type(error).__name__}); continuing with other providers and saved candidates.")
                        break
                    old_cursor=checkpoint["cursor_value"]
                    if next_cursor==old_cursor and rows:
                        progress["status"]="failed"
                        progress["failure"]={"stage":"directory_import","provider":provider,"status":"cursor_stalled","reason":"Provider page did not advance its checkpoint."}
                        failures.append(progress["failure"])
                        verify_employers.discovery.save_employer_import_checkpoint(provider,old_cursor,checkpoint["pages_completed"],"retry_pending","cursor_stalled")
                        break
                    persisted=verify_employers.discovery.persist_candidate_seeds(rows,return_details=True)
                    progress["candidates_returned"]+=len(rows);progress["inserted"]+=persisted["inserted"]
                    progress["reused"]+=persisted["reused"]
                    progress["rejected"]+=persisted["rejected"]+diagnostics.get("records_rejected",0)
                    for reason,count in {**diagnostics.get("rejection_reasons",{}),**persisted.get("rejection_reasons",{})}.items():
                        progress["rejection_reasons"][reason]=progress["rejection_reasons"].get(reason,0)+count
                    all_directory_rows.extend(rows)
                    page_count=checkpoint["pages_completed"]+1
                    page_status=diagnostics.get("request_status","succeeded")
                    has_more=diagnostics.get("has_more",len(rows)>=page_size and next_cursor!=old_cursor)
                    if not rows and not next_cursor and not old_cursor: checkpoint_status="empty_result"
                    elif not has_more: checkpoint_status="complete"
                    else: checkpoint_status="in_progress"
                    checkpoint={"cursor_value":next_cursor,"pages_completed":page_count,"status":checkpoint_status}
                    verify_employers.discovery.save_employer_import_checkpoint(provider,next_cursor,page_count,
                        checkpoint_status,"No usable first-page records; retry from the beginning." if checkpoint_status=="empty_result" else "")
                    progress["pages"]+=1;progress["last_request_status"]=page_status
                    progress["cursor"]=next_cursor;progress["checkpoint_status"]=checkpoint_status
                    if not has_more or not next_cursor or next_cursor==old_cursor: break
                    time.sleep(0.25)
                if progress["status"]=="running":
                    progress["status"]=("succeeded" if progress["candidates_returned"] else
                        "succeeded_no_usable_candidates" if progress["rejected"] else "empty")
                provider_reports.append(progress)
                stats["candidate_records_returned"]+=progress["candidates_returned"]
                stats["candidate_records_inserted"]+=progress["inserted"]
                stats["candidate_records_reused"]+=progress["reused"]
                stats["candidate_records_rejected"]+=progress["rejected"]
                for reason,count in progress["rejection_reasons"].items():
                    stats["rejection_reasons"][reason]=stats["rejection_reasons"].get(reason,0)+count
            if not failures:
                stats["directory_request_status"]=("succeeded_with_candidates" if stats["candidate_records_returned"] else
                    "succeeded_no_usable_candidates" if stats["candidate_records_rejected"] else "succeeded_empty")

    # Saved candidates are always resumed, independent of provider selection.
    pending=[] if args.validate_only else verify_employers.discovery.get_pending_candidate_seeds(args.seed_limit)
    stats["candidates_available_for_discovery"]=len(pending)
    discovered_errors=[];sources_found=0;registered=0;pages_checked=0
    if pending and connectivity and connectivity.get("ok"):
        for index,row in enumerate(pending,1):
            if sources_found>=args.limit: break
            try:
                print(f"[career_page_discovery] {index}/{min(len(pending),args.seed_limit)} {row['name']}…")
                found,error=job_connectors.discover_source_candidates(row["name"],row["website_url"])
                pages_checked+=1
                if error:
                    fail={"stage":"career_page_discovery","provider":row.get("candidate_source"),"company":row["name"],"reason":error[:300]}
                    discovered_errors.append(fail);verify_employers.discovery.update_candidate_seed(row,"discovery_failed",{"reason":error[:300]})
                    continue
                source_items=[]
                for candidate in found[:max(0,args.limit-sources_found)]:
                    item={**{k:candidate.get(k,"") for k in ("name","source_type","source_token","careers_url","geographic_coverage","website_url","evidence_url")},
                        **{k:row.get(k,"") for k in ("candidate_source","candidate_record_url","candidate_license")}}
                    source_items.append(verify_employers.discovery.EmployerImportItem.model_validate(item))
                result=verify_employers.discovery.register_employer_sources(verify_employers.discovery.EmployerImportRequest(sources=source_items)) if source_items else {"imported":0}
                sources_found+=len(found);registered+=result.get("imported",0)
                verify_employers.discovery.update_candidate_seed(row,"career_source_found" if found else "no_public_career_source",
                    {"source_candidates":len(found),"new_sources_registered":result.get("imported",0),
                     "careers_url":found[0].get("careers_url","") if found else "",
                     "evidence_url":found[0].get("evidence_url","") if found else ""})
            except Exception as error:
                fail={"stage":"career_page_discovery","provider":row.get("candidate_source"),"company":row.get("name"),"reason":f"Discovery failed: {type(error).__name__}."}
                discovered_errors.append(fail);verify_employers.discovery.update_candidate_seed(row,"discovery_failed",{"reason":type(error).__name__})
            if index<len(pending) and sources_found<args.limit: time.sleep(0.5)
    elif pending:
        failure={"stage":"career_page_discovery","status":"outbound_unavailable","reason":"Saved candidates remain pending; official websites were not queried without outbound HTTPS."}
        discovered_errors.append(failure);failures.append(failure)

    if candidate_rows:
        # Explicit source-token imports retain the existing validation CLI path.
        candidates_path=args.candidates
    employer_ids=None if candidate_rows else verify_employers.discovery.pending_candidate_employer_ids(None,args.limit)
    print(f"[feed_validation] validating {len(employer_ids)} inactive registered source candidate(s).")
    report=verify_employers.run_verification(candidates_path=candidates_path,limit=args.limit,
        report_path=args.report,employer_ids=employer_ids,network_result=connectivity)
    stats.update({"career_pages_inspected":pages_checked,"ats_sources_discovered":sources_found,
        "inactive_sources_registered":registered,"sources_validated":report["employers_checked"],
        "sources_activated":report["newly_activated"],"failures":failures+discovered_errors,
        "sources_found":sources_found,"providers":provider_reports,
        "saved_candidates_used":len(pending) if pending else 0})
    no_work=(not stats["candidate_records_returned"] and not pending and not employer_ids and not candidate_rows)
    if no_work:
        stats["message"]="No candidates were returned or saved and no inactive sources were available; no expansion work was performed."
    report["employer_expansion"]=stats
    if discovered_errors: report["validation_failures"]+=len(discovered_errors)
    _rewrite_report(report)
    print(f"[expansion_summary] providers={','.join(providers) if providers else 'saved-only'}; "
        f"candidates returned/inserted/reused/rejected={stats['candidate_records_returned']}/{stats['candidate_records_inserted']}/{stats['candidate_records_reused']}/{stats['candidate_records_rejected']}; "
        f"career pages={pages_checked}; ATS sources={sources_found}; inactive registered={registered}; "
        f"validated/activated={report['employers_checked']}/{report['newly_activated']}")
    if no_work: print(f"[expansion_summary] {stats['message']}")
    if report["interrupted"]: return 130
    if failures or discovered_errors or report["validation_failures"] or report["unavailable"] or report["rate_limited"] or not report["network_connectivity"].get("ok"):
        return 1
    if no_work: return 2
    return 0


def _rewrite_report(report):
    path=Path(report["report_file"]);temp=path.with_suffix(path.suffix+".tmp")
    temp.write_text(json.dumps(report,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    temp.replace(path)


if __name__=="__main__": raise SystemExit(main())
