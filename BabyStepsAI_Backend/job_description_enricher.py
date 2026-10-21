"""Bounded, provenance-aware enrichment for saved job descriptions.

Only public official ATS or job-posting URLs are fetched. This module does not
run discovery, use AI, or synthesize missing qualifications.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import requests
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import job_connectors as connectors
from job_connectors import EmployerSource
from job_search_agent import DB as JOB_DB, USER

DB=JOB_DB
router=APIRouter()
MAX_KEYS_PER_RETRY=3
MIN_DESCRIPTION_CHARS=180


def now():return datetime.now(timezone.utc).isoformat()


class ClosingConnection(sqlite3.Connection):
    def __exit__(self,exc_type,exc_value,traceback):
        try:return super().__exit__(exc_type,exc_value,traceback)
        finally:self.close()


def connect():
    db=sqlite3.connect(DB,timeout=30,factory=ClosingConnection);db.row_factory=sqlite3.Row;return db


def initialize():
    with connect() as db:
        table=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='discovered_openings'").fetchone()
        if table:
            columns={row[1] for row in db.execute("PRAGMA table_info(discovered_openings)")}
            for name,declaration in (("description_source","TEXT NOT NULL DEFAULT ''"),
                ("description_retrieved_at","TEXT"),("description_error","TEXT NOT NULL DEFAULT ''"),
                ("description_version","INTEGER NOT NULL DEFAULT 0")):
                if name not in columns:db.execute(f"ALTER TABLE discovered_openings ADD COLUMN {name} {declaration}")
        db.execute("""CREATE TABLE IF NOT EXISTS job_description_retrievals(
            attempt_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,job_key TEXT NOT NULL,status TEXT NOT NULL,
            source_url TEXT NOT NULL DEFAULT '',source_type TEXT NOT NULL DEFAULT '',retrieved_at TEXT NOT NULL,
            content_fingerprint TEXT NOT NULL DEFAULT '',reason TEXT NOT NULL DEFAULT '',duration_ms INTEGER NOT NULL DEFAULT 0)""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_description_retrieval_job ON job_description_retrievals(user_id,job_key,retrieved_at DESC)")


initialize()


class _VisibleText(HTMLParser):
    """Collect visible main/article text while dropping navigation and scripts."""
    SKIP={"script","style","noscript","svg","nav","footer","header","form","button"}
    def __init__(self):super().__init__(convert_charrefs=True);self.skip_counts={};self.main_depth=0;self.main_seen=False;self.parts=[]
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag in self.SKIP or attrs.get("aria-hidden")=="true":self.skip_counts[tag]=self.skip_counts.get(tag,0)+1
        if tag in {"main","article"} or attrs.get("role")=="main":self.main_depth+=1;self.main_seen=True
    def handle_endtag(self,tag):
        if self.skip_counts.get(tag,0):
            self.skip_counts[tag]-=1
            if not self.skip_counts[tag]:del self.skip_counts[tag]
        if tag in {"main","article"} and self.main_depth:self.main_depth-=1
    def handle_data(self,data):
        if not self.skip_counts and data.strip():self.parts.append(data.strip())


def normalize_description(value):
    text=html.unescape(re.sub(r"<[^>]+>"," ",str(value or "")))
    text=re.sub(r"[\t\r ]+"," ",text)
    text=re.sub(r" *\n *","\n",text)
    text=re.sub(r"\n{3,}","\n\n",text).strip()
    return text[:60000]


def usable_description(value):
    text=normalize_description(value)
    alpha=len(re.findall(r"[A-Za-z]",text))
    return len(text)>=MIN_DESCRIPTION_CHARS and alpha>=120


def _structured_payload(document,canonical_url):
    parser=connectors._JsonLdParser();parser.feed(document)
    for doc in parser.documents:
        for posting in connectors._jsonld_jobs(doc):
            posting_url=posting.get("url")
            if posting_url and connectors.canonicalize_job_url(canonical_url)!=connectors.canonicalize_job_url(connectors.urljoin(canonical_url,str(posting_url))):
                continue
            description=normalize_description(posting.get("description"))
            if usable_description(description):
                return {"description":description,"required":{
                    key:posting.get(key) for key in ("qualifications","experienceRequirements","educationRequirements") if posting.get(key)},
                    "preferred":{key:posting.get(key) for key in ("skills","incentiveCompensation") if posting.get(key)},
                    "source_type":"jobposting_jsonld"}
    parser2=_VisibleText();parser2.feed(document)
    text=normalize_description("\n".join(parser2.parts))
    # A generic body dump is rejected unless the page has a substantial main
    # or article landmark. This avoids mistaking navigation for job content.
    if not parser2.main_seen and len(text)<500:
        return None
    if not usable_description(text):return None
    return {"description":text,"required":{},"preferred":{},"source_type":"official_job_page_html"}


def _official_url(row):
    value=str(row.get("job_url") or row.get("url") or "").strip()
    try:return connectors.validate_public_https_url(value)
    except ValueError as error:raise ValueError(f"Saved job URL is not a permitted public HTTPS destination: {error}") from error


def _ats_detail(row):
    source=str(row.get("source_platform") or "").casefold();token=str(row.get("source_token") or "");job_id=str(row.get("source_id") or "")
    # Only documented read-only public posting endpoints are attempted here.
    if source=="greenhouse" and re.fullmatch(r"\d+",job_id):
        url=f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}?content=true"
        payload,error=connectors._get_json(url,ttl=0)
        if not error and isinstance(payload,dict):
            content=normalize_description(payload.get("content"))
            if usable_description(content):return {"description":content,"required":{},"preferred":{},"source_type":"greenhouse_job_detail_api"}
        if error:return None,error
    if source=="lever" and token and job_id:
        url=f"https://api.lever.co/v0/postings/{token}/{job_id}?mode=json"
        payload,error=connectors._get_json(url,ttl=0)
        if not error and isinstance(payload,dict):
            content=normalize_description(payload.get("descriptionPlain") or payload.get("description"))
            if usable_description(content):return {"description":content,"required":{},"preferred":{},"source_type":"lever_job_detail_api"}
        if error:return None,error
    return None,None


def _fetch_official_page(url,getter=None):
    getter=getter or requests.get
    try:
        allowed,reason=connectors._robots_allow(url,getter)
        if not allowed:return None,reason or "robots.txt disallows this posting page."
        response=connectors._safe_get(url,getter=getter,timeout=connectors.REQUEST_TIMEOUT,
            headers={"Accept":"text/html,application/ld+json","User-Agent":"BabyStepsAI public job detail reader"})
        response.raise_for_status()
        if len(getattr(response,"content",b""))>connectors.MAX_RESPONSE_BYTES:
            return None,"Official job page exceeded the configured response size limit."
        final_url=getattr(response,"url",url) or url
        connectors.validate_public_https_url(final_url)
        if (urlparse(final_url).hostname or "").casefold()!=(urlparse(url).hostname or "").casefold():
            # Redirects are already destination-validated. Keep enrichment on
            # the official employer host to prevent cross-company page swaps.
            host=(urlparse(final_url).hostname or "").casefold()
            ats_suffixes=("boards.greenhouse.io","job-boards.greenhouse.io","jobs.lever.co","jobs.ashbyhq.com","smartrecruiters.com")
            if not any(host==base or host.endswith("."+base) for base in ats_suffixes):
                return None,"Posting redirected away from its official employer or recognized ATS host."
        return _structured_payload(str(getattr(response,"text","")),final_url),None
    except requests.Timeout:return None,"Official job page timed out."
    except requests.RequestException as error:return None,f"Official job page request failed ({type(error).__name__})."
    except (ValueError,UnicodeError) as error:return None,f"Official job page was rejected: {error}"


def _save_attempt(job_key,status,url,source,reason="",duration_ms=0,fingerprint=""):
    with connect() as db:
        db.execute("""INSERT INTO job_description_retrievals(attempt_id,user_id,job_key,status,source_url,source_type,retrieved_at,content_fingerprint,reason,duration_ms)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",(str(uuid.uuid4()),USER,job_key,status,url,source,now(),fingerprint,reason[:500],duration_ms))


def enrich_one(job_key,*,getter=None,force=False):
    """Fetch one saved posting; returns an explicit terminal enrichment outcome."""
    started=datetime.now(timezone.utc)
    try:source_type,source_token,source_id=job_key.split(":",2)
    except ValueError:return {"job_key":job_key,"status":"failed","reason":"Saved job has an invalid source identifier."}
    with connect() as db:
        item=db.execute("SELECT * FROM discovered_openings WHERE user_id=? AND job_key=?",(USER,job_key)).fetchone()
        employer=db.execute("SELECT source_type,source_token,careers_url FROM employers WHERE source_type=? AND source_token=?",(source_type,source_token)).fetchone()
    if not item:return {"job_key":job_key,"status":"failed","reason":"Saved job was not found."}
    row=dict(item);row["source_token"]=source_token;row["source_id"]=source_id;url="";source=""
    if employer:row["source_careers_url"]=employer["careers_url"]
    current=normalize_description(row.get("description"))
    if usable_description(current) and not force:return {"job_key":job_key,"status":"available","description_chars":len(current),"source":row.get("description_source") or "discovery feed"}
    try:url=_official_url(row)
    except ValueError as error:
        reason=str(error);_save_attempt(job_key,"failed",str(row.get("job_url") or row.get("url") or ""),"url_validation",reason)
        return {"job_key":job_key,"status":"failed","reason":reason}
    source=str(row.get("source_platform") or "")
    result=None;reason=""
    # ATS IDs are preferred. For public_html postings the stable source ID is
    # the canonical detail URL, so retrieve that one saved page directly.
    ats,ats_error=_ats_detail(row)
    if ats:result=ats
    elif ats_error:reason=ats_error
    if result is None:
        result,page_error=_fetch_official_page(url,getter)
        if page_error:reason=page_error
    if not result or not usable_description(result.get("description")):
        if not reason:reason="The official posting did not expose a complete job description in public structured data or page content."
        elapsed=int((datetime.now(timezone.utc)-started).total_seconds()*1000)
        with connect() as db:
            db.execute("UPDATE discovered_openings SET description_error=? WHERE user_id=? AND job_key=?",(reason[:500],USER,job_key))
        _save_attempt(job_key,"failed",url,source,reason,elapsed)
        return {"job_key":job_key,"status":"failed","reason":reason}
    description=normalize_description(result["description"])
    content_fingerprint=hashlib.sha256((str(row.get("title") or "").strip().casefold()+"\n"+description.strip()).encode()).hexdigest()
    stamp=now();elapsed=int((datetime.now(timezone.utc)-started).total_seconds()*1000)
    with connect() as db:
        old=db.execute("SELECT content_fingerprint FROM discovered_openings WHERE user_id=? AND job_key=?",(USER,job_key)).fetchone()
        version=db.execute("SELECT description_version FROM discovered_openings WHERE user_id=? AND job_key=?",(USER,job_key)).fetchone()[0]+1
        db.execute("""UPDATE discovered_openings SET description=?,description_source=?,description_retrieved_at=?,
            description_error='',description_version=?,content_fingerprint=?,last_verified_at=?,
            required_qualifications_json=?,preferred_qualifications_json=? WHERE user_id=? AND job_key=?""",
            (description,result["source_type"],stamp,version,content_fingerprint,stamp,
             json.dumps(result.get("required") or {},ensure_ascii=False),json.dumps(result.get("preferred") or {},ensure_ascii=False),USER,job_key))
        if old and old["content_fingerprint"]!=content_fingerprint:
            db.execute("UPDATE job_match_results SET freshness_status='stale' WHERE user_id=? AND job_key=? AND status='completed'",(USER,job_key))
    _save_attempt(job_key,"succeeded",url,result["source_type"],"",elapsed,content_fingerprint)
    return {"job_key":job_key,"status":"succeeded","description_chars":len(description),
            "source":result["source_type"],"retrieved_at":stamp,"description_version":version}


def enrich_saved_jobs(job_keys,*,limit=12,getter=None,force=False):
    keys=list(dict.fromkeys(str(key) for key in job_keys if key))[:max(0,min(int(limit),12))]
    results=[enrich_one(key,getter=getter,force=force) for key in keys]
    return {"total":len(keys),"attempted":sum(item["status"] in {"succeeded","failed"} for item in results),
        "succeeded":sum(item["status"]=="succeeded" for item in results),
        "failed":sum(item["status"]=="failed" for item in results),
        "available":sum(item["status"]=="available" for item in results),"results":results}


class EnrichmentRequest(BaseModel):
    job_keys:list[str]=Field(min_length=1,max_length=MAX_KEYS_PER_RETRY)
    force:bool=False


@router.post("/api/jobs/enrich-descriptions")
def enrich_saved_job_descriptions(payload:EnrichmentRequest,request:Request):
    host=getattr(getattr(request,"client",None),"host",None)
    import ipaddress
    try:
        if not host or not ipaddress.ip_address(host).is_loopback:raise HTTPException(403,"Saved job enrichment is available only from this device.")
    except ValueError:raise HTTPException(403,"Saved job enrichment is available only from this device.")
    return enrich_saved_jobs(payload.job_keys,limit=MAX_KEYS_PER_RETRY,force=payload.force)

