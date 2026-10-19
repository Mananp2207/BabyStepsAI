"""Licensed, bounded employer seed sources used by the local expansion CLI."""
from __future__ import annotations

import re
import random
import time
from urllib.parse import urlencode, urlparse

import requests

import job_connectors

WIKIDATA_ENDPOINT = "https://query.wikidata.org/sparql"
WIKIDATA_RECORD = "https://www.wikidata.org/entity/"
WIKIDATA_QUERY = r'''
SELECT ?company ?companyLabel ?website WHERE {
  ?company wdt:P856 ?website;
           wdt:P452 ?industry.
  ?industry rdfs:label ?industryLabel.
  FILTER(LANG(?industryLabel) = "en")
  FILTER(REGEX(LCASE(STR(?industryLabel)),
    "software|computer|information technology|internet|semiconductor|cloud|cyber|data processing|telecommunication"))
  FILTER(STRSTARTS(LCASE(STR(?website)), "https://"))
  FILTER(STR(?company) > "{cursor}")
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
ORDER BY ?company
LIMIT {batch_size}
'''

# Documented, read-only JSON endpoints from the public yc-oss API project.
# The project documents its schema and endpoints but does not state a dataset
# license, so imported records preserve that uncertainty in provenance.
YC_HIRING_ENDPOINT = "https://yc-oss.github.io/api/companies/hiring.json"
YC_ACCESS_TERMS = "Public JSON API; dataset license not specified by provider."
PROVIDER_REGISTRY = {
    "yc_api": {"label":"YC public companies API", "endpoint":YC_HIRING_ENDPOINT,
               "access_terms":YC_ACCESS_TERMS, "evidence":"https://github.com/yc-oss/api"},
    "wikidata": {"label":"Wikidata SPARQL", "endpoint":WIKIDATA_ENDPOINT,
                 "access_terms":"CC0-1.0", "evidence":"https://www.wikidata.org/"},
}
YC_TECH_TERMS = re.compile(r"\b(software|developer|engineering|data|cloud|cyber|security|AI|artificial intelligence|machine learning|infrastructure|IT|technology|SaaS|semiconductor|robotics|computer vision)\b", re.I)


def fetch_yc_candidate_batch(batch_size: int, *, cursor: str = "", timeout: float = 20,
                            getter=None, sleeper=time.sleep, jitter=random.uniform,
                            retries: int = 3, diagnostics: dict | None = None):
    """Import active-hiring YC company records from its documented JSON API.

    YC API output is a candidate directory only. The returned company URL is
    checked and crawled separately; no career URL or ATS identifier is guessed.
    Cursor is the last numeric YC company ID emitted. The API currently returns
    a whole filtered JSON array, which is sorted locally for stable resumption.
    """
    if not 1 <= int(batch_size) <= 500: raise ValueError("YC batch size must be between 1 and 500.")
    if not 5 <= float(timeout) <= 120: raise ValueError("YC timeout must be between 5 and 120 seconds.")
    if not 1 <= int(retries) <= 5: raise ValueError("YC retries must be between 1 and 5.")
    if cursor and not re.fullmatch(r"[0-9]+", str(cursor)): raise ValueError("Invalid YC company cursor.")
    get = getter or requests.get
    response = None
    for attempt in range(int(retries)):
        try:
            response = job_connectors._safe_get(
                YC_HIRING_ENDPOINT, getter=get, trusted_initial=True, timeout=(5, float(timeout)),
                headers={"Accept":"application/json", "User-Agent":"BabyStepsAI local employer discovery/1.0"})
            if response.status_code == 429:
                retry_after = job_connectors._retry_after_seconds(response.headers.get("Retry-After"))
                if attempt + 1 >= retries or (retry_after is not None and retry_after > 30):
                    raise requests.HTTPError("YC public API rate limited the request; checkpoint retained.", response=response)
                delay = retry_after if retry_after is not None else min(0.5 * (2 ** attempt), 30)
                sleeper(delay + jitter(0, min(0.5, delay * .1)))
                continue
            if response.status_code >= 500:
                response.raise_for_status()
            break
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as exc:
            if isinstance(exc, requests.HTTPError) and getattr(exc.response, "status_code", 500) < 500:
                raise
            if attempt + 1 >= retries: raise
            delay=min(0.5 * (2 ** attempt),30)
            sleeper(delay+jitter(0,min(0.5,delay*.1)))
    if response is None: raise requests.RequestException("YC API returned no response.")
    response.raise_for_status()
    if len(response.content) > 16 * 1024 * 1024:
        raise ValueError("YC API response exceeded the 16 MiB safety limit.")
    payload=response.json()
    if not isinstance(payload,list): raise ValueError("YC API returned an unexpected company-list shape.")
    records=[]
    for row in payload:
        if not isinstance(row,dict): continue
        try: company_id=int(row.get("id"))
        except (TypeError,ValueError): continue
        if company_id<=0: continue
        records.append((company_id,row))
    records.sort(key=lambda pair:pair[0])
    after=int(cursor or 0)
    output=[];seen=set();reasons={};eligible=0;last_seen=after
    def reject(reason): reasons[reason]=reasons.get(reason,0)+1
    for company_id,row in records:
        if company_id<=after: continue
        eligible+=1
        last_seen=company_id
        fields=" ".join(str(row.get(k) or "") for k in ("industry","subindustry","one_liner","description","long_description"))
        tags=row.get("tags") if isinstance(row.get("tags"),list) else []
        industries=row.get("industries") if isinstance(row.get("industries"),list) else []
        if not YC_TECH_TERMS.search(fields+" "+" ".join(str(x) for x in tags+industries)):
            reject("not_technology_related")
            continue
        name=str(row.get("name") or "").strip()
        website=str(row.get("website") or "").strip()
        slug=str(row.get("slug") or "").strip()
        evidence=str(row.get("url") or "").strip()
        try:
            parsed_evidence=urlparse(job_connectors.validate_public_https_url(evidence,resolve=False))
            valid_evidence=(parsed_evidence.hostname=="www.ycombinator.com" and
                re.fullmatch(r"/companies/[a-zA-Z0-9-]{1,120}/?",parsed_evidence.path) is not None)
        except ValueError:
            valid_evidence=False
        if not name or len(name)>160: reject("invalid_name"); continue
        if not valid_evidence: reject("invalid_company_evidence"); continue
        try: job_connectors.validate_public_https_url(website, resolve=False)
        except ValueError: reject("missing_or_unsafe_https_website"); continue
        domain=(urlparse(website).hostname or "").casefold().removeprefix("www.")
        identity=(re.sub(r"[^a-z0-9]+"," ",name.casefold()).strip(),domain)
        if identity in seen: reject("duplicate_in_provider"); continue
        seen.add(identity)
        output.append({"name":name,"website_url":website,"careers_url":"",
            "candidate_source":"yc_api","candidate_record_url":evidence,
            "candidate_license":"unknown; "+YC_ACCESS_TERMS,
            "evidence_url":evidence,"provider_record_id":str(company_id),
            "geographic_coverage":""})
        if len(output)>=int(batch_size): break
    next_cursor=str(last_seen)
    max_id=max((company_id for company_id,_ in records),default=after)
    metrics={"records_available":len(records),"records_examined":eligible,"records_returned":len(output),
             "records_rejected":sum(reasons.values()),"rejection_reasons":reasons,
             "has_more":last_seen<max_id,
             "request_status":"succeeded_with_candidates" if output else ("succeeded_no_usable_candidates" if records else "succeeded_empty")}
    if diagnostics is not None: diagnostics.update(metrics)
    return output,next_cursor


def fetch_provider_candidate_batch(provider: str, batch_size: int, **kwargs):
    """Provider-independent candidate source interface."""
    if provider == "yc_api": return fetch_yc_candidate_batch(batch_size, **kwargs)
    if provider == "wikidata":
        return fetch_wikidata_candidate_batch(batch_size, **kwargs)
    raise ValueError(f"Unsupported employer candidate provider: {provider}")


def fetch_wikidata_candidate_batch(batch_size: int, *, cursor: str = "", timeout: float = 20,
                                   getter=None, sleeper=time.sleep, jitter=random.uniform,
                                   retries: int = 3, max_retry_after: float = 30,
                                   diagnostics: dict | None = None):
    """Fetch one deterministic CC0 Wikidata page using a QID keyset cursor.

    These are discovery seeds only. Each official site must separately expose
    its career source; no ATS identifier is inferred from a company name.
    """
    if not 1 <= int(batch_size) <= 100: raise ValueError("Wikidata batch size must be between 1 and 100.")
    if not 5 <= float(timeout) <= 120: raise ValueError("Wikidata timeout must be between 5 and 120 seconds.")
    if not 1 <= int(retries) <= 5: raise ValueError("Wikidata retries must be between 1 and 5.")
    if cursor and not re.fullmatch(r"Q[1-9][0-9]*",str(cursor)): raise ValueError("Invalid Wikidata cursor.")
    cursor_url = WIKIDATA_RECORD + cursor if cursor else ""
    query = WIKIDATA_QUERY.replace("{batch_size}", str(int(batch_size))).replace("{cursor}", cursor_url)
    params = {"query": query, "format": "json"}
    url = WIKIDATA_ENDPOINT + "?" + urlencode(params)
    get = getter or requests.get
    response = None
    last_error = None
    for attempt in range(int(retries)):
        try:
            response = job_connectors._safe_get(
                url, getter=get, timeout=(5, float(timeout)),
                headers={"Accept": "application/sparql-results+json", "User-Agent": "BabyStepsAI local employer discovery/1.0"},
            )
            if response.status_code == 429:
                retry_after = job_connectors._retry_after_seconds(response.headers.get("Retry-After"))
                if attempt + 1 >= retries or (retry_after is not None and retry_after > max_retry_after):
                    raise requests.HTTPError("Wikidata Query Service rate limited the request (HTTP 429); checkpoint retained for later retry.",response=response)
                delay = retry_after if retry_after is not None else min(0.5 * (2 ** attempt), max_retry_after)
                sleeper(delay + jitter(0, min(0.5, delay * 0.1)))
                continue
            if response.status_code >= 500:
                response.raise_for_status()
            if response.status_code < 500:
                break
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as exc:
            last_error = exc
            if isinstance(exc, requests.HTTPError) and getattr(exc.response,"status_code",500)<500:
                raise
            if attempt + 1 >= retries: raise
            delay=min(0.5 * (2 ** attempt),max_retry_after)
            sleeper(delay+jitter(0,min(0.5,delay*0.1)))
    if response is None:
        raise last_error or requests.RequestException("Wikidata Query Service returned no response.")
    response.raise_for_status()
    if len(response.content) > 8 * 1024 * 1024:
        raise ValueError("Wikidata response exceeded the 8 MiB limit.")
    payload = response.json()
    bindings = payload.get("results", {}).get("bindings") if isinstance(payload, dict) and isinstance(payload.get("results"),dict) else None
    if not isinstance(bindings, list):
        raise ValueError("Wikidata returned an unexpected SPARQL result shape.")
    output, seen = [], set()
    metrics = {"bindings_returned":len(bindings), "records_returned":0, "records_rejected":0,
               "rejection_reasons":{}}
    next_cursor=cursor
    for binding in bindings:
        try:
            name = str(binding["companyLabel"]["value"]).strip()
            entity = str(binding["company"]["value"])
            qid=entity.removeprefix(WIKIDATA_RECORD)
            website = str(binding["website"]["value"]).strip()
            if not re.fullmatch(r"Q[1-9][0-9]*",qid) or len(entity) > 120:
                metrics["records_rejected"] += 1
                metrics["rejection_reasons"]["invalid_entity"] = metrics["rejection_reasons"].get("invalid_entity",0)+1
                continue
            next_cursor=qid
            job_connectors.validate_public_https_url(website)
            if not name or len(name)>160:
                metrics["records_rejected"] += 1
                metrics["rejection_reasons"]["invalid_name"] = metrics["rejection_reasons"].get("invalid_name",0)+1
                continue
            normalized_website = website.casefold().rstrip("/")
            key = (re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip(), normalized_website)
            if key in seen:
                metrics["records_rejected"] += 1
                metrics["rejection_reasons"]["duplicate_in_page"] = metrics["rejection_reasons"].get("duplicate_in_page",0)+1
                continue
            seen.add(key)
            output.append({
                "name": name,
                "website_url": website,
                "candidate_source": "wikidata",
                "candidate_record_url": entity,
                "candidate_license": "CC0-1.0",
            })
            metrics["records_returned"] += 1
        except (KeyError, TypeError, ValueError):
            metrics["records_rejected"] += 1
            metrics["rejection_reasons"]["malformed_or_unsafe_record"] = metrics["rejection_reasons"].get("malformed_or_unsafe_record",0)+1
            continue
    if diagnostics is not None:
        diagnostics.update(metrics)
        diagnostics["has_more"] = len(bindings) >= int(batch_size) and next_cursor != cursor
        diagnostics["request_status"] = "succeeded_with_candidates" if output else ("succeeded_empty" if not bindings else "succeeded_no_usable_candidates")
    return output,next_cursor


def import_wikidata_candidates(limit: int, *, getter=None, timeout: float = 20) -> list[dict]:
    """Compatibility helper for a bounded multi-page read; the CLI checkpoints pages."""
    if not 1<=int(limit)<=500: raise ValueError("Wikidata seed limit must be between 1 and 500.")
    rows=[];cursor=""
    while len(rows)<int(limit):
        page_size=min(100,int(limit)-len(rows))
        page,next_cursor=fetch_wikidata_candidate_batch(page_size,cursor=cursor,getter=getter,timeout=timeout)
        if next_cursor==cursor and not page: break
        rows.extend(page);cursor=next_cursor
        if len(page)<page_size: break
    return rows[:int(limit)]
