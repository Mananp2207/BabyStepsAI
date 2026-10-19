"""Public, read-only job feed adapters and conservative job normalization.

Only documented public listing interfaces belong here. Application endpoints are
intentionally out of scope: discovery must never submit applications.
"""
from __future__ import annotations

import hashlib
import ipaddress
import html
import json
import re
import socket
import time
import email.utils
import xml.etree.ElementTree as ET
import urllib.robotparser
from contextvars import ContextVar
from contextlib import contextmanager
from html.parser import HTMLParser
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import parse_qsl, quote, urlparse, urljoin, urlunparse, urlencode

import requests

REQUEST_TIMEOUT = (4, 15)
MAX_RESPONSE_BYTES = 12 * 1024 * 1024
_cache: dict[tuple[str, str], tuple[float, list[dict], str | None]] = {}
_fetch_observer = ContextVar("babysteps_fetch_observer", default=None)


@contextmanager
def observe_fetch():
    """Collect sanitized per-request evidence for local verification reports."""
    info = {"responses": [], "cache_hits": 0}
    token = _fetch_observer.set(info)
    try:
        yield info
    finally:
        _fetch_observer.reset(token)


@dataclass(frozen=True)
class EmployerSource:
    name: str
    source_type: str
    token: str
    careers_url: str = ""
    geographic_coverage: str = ""


def validate_public_https_url(url: str, *, resolve: bool = True) -> str:
    """Reject non-HTTPS, credentials, local/private IPs and metadata targets."""
    parsed = urlparse(str(url or ""))
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("A public HTTPS URL without embedded credentials is required.")
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "metadata.google.internal"} or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("Local and metadata destinations are not permitted.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("Private or non-public network destinations are not permitted.")
    if resolve:
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)}
        except OSError as exc:
            raise ValueError("Destination host could not be resolved.") from exc
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if not ip.is_global:
                raise ValueError("Private or non-public network destinations are not permitted.")
    return url


def canonicalize_job_url(url: str) -> str:
    """Conservative URL key that removes fragments and common tracking params."""
    parsed = urlparse(str(url or ""))
    tracking = {"source", "ref", "referrer", "gh_src", "lever-source", "trk", "fbclid", "gclid"}
    pairs = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
             if not key.casefold().startswith("utm_") and key.casefold() not in tracking]
    host = (parsed.hostname or "").casefold()
    if parsed.port and parsed.port not in (80, 443):
        host = f"{host}:{parsed.port}"
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme.casefold(), host, path, "", urlencode(sorted(pairs)), ""))


def _safe_get(url, *, getter=None, trusted_initial=False, **kwargs):
    """Follow only a small number of redirects, validating every destination."""
    getter = getter or requests.get
    current = validate_public_https_url(url, resolve=not trusted_initial)
    for _ in range(4):
        response = getter(current, allow_redirects=False, **kwargs)
        observer = _fetch_observer.get()
        if observer is not None:
            observer["responses"].append({"host": (urlparse(current).hostname or "").casefold(),
                                          "status_code": getattr(response, "status_code", None)})
        if getattr(response, "status_code", 200) not in {301, 302, 303, 307, 308}:
            return response
        location = response.headers.get("Location") if hasattr(response, "headers") else None
        if not location:
            return response
        current = validate_public_https_url(urljoin(current, location))
    raise requests.RequestException("Too many redirects.")


def _get_json(url: str, *, params=None, ttl=900, getter=None):
    getter = getter or requests.get
    key = (url, json.dumps(params or {}, sort_keys=True))
    now = time.monotonic()
    cached = _cache.get(key)
    if cached and cached[0] > now:
        observer = _fetch_observer.get()
        if observer is not None:
            observer["cache_hits"] += 1
        return cached[1], cached[2]
    payload = None
    last_error = None
    for attempt in range(3):
        try:
            response = _safe_get(url, getter=getter, trusted_initial=True, params=params, timeout=REQUEST_TIMEOUT, headers={"Accept": "application/json"})
            status_code = getattr(response, "status_code", 200)
            if status_code == 429:
                retry_after = _retry_after_seconds(getattr(response, "headers", {}).get("Retry-After"))
                # A server-requested delay beyond this short local budget is
                # honored by stopping now instead of retrying early.
                if retry_after is not None and retry_after <= 2 and attempt < 2:
                    time.sleep(retry_after)
                    continue
                error = "HTTP 429 rate limited" + (f"; Retry-After {retry_after:g}s" if retry_after is not None else "")
                _cache[key] = (now + min(max(retry_after or 30, 30), 300), [], error)
                return [], error
            if status_code in {401, 403}:
                error = f"HTTP {status_code} authentication or permission required"
                _cache[key] = (now + 300, [], error)
                return [], error
            if status_code in {404, 410}:
                error = f"HTTP {status_code} source or posting not found"
                _cache[key] = (now + 300, [], error)
                return [], error
            response.raise_for_status()
            response_content = getattr(response, "content", b"")
            if isinstance(response_content, (bytes, str)) and len(response_content) > MAX_RESPONSE_BYTES:
                return [], "Feed response exceeded the configured size limit."
            payload = response.json()
            last_error = None
            break
        except requests.Timeout:
            last_error = "Feed request timed out."
        except requests.HTTPError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            last_error = f"Feed returned HTTP {status or 'error'}."
            if status is not None and status < 500:
                break
        except requests.RequestException as exc:
            last_error = f"Feed request failed: {type(exc).__name__}."
        except ValueError as exc:
            return [], f"Feed response was not valid JSON or destination was rejected: {exc}"
        if attempt < 2:
            time.sleep(0.2 * (2 ** attempt))
    if last_error:
        _cache[key] = (now + 30, [], last_error)
        return [], last_error
    jobs = _payload_jobs(payload)
    error = None if jobs is not None else "Malformed response: expected a documented jobs collection."
    result = jobs or []
    _cache[key] = (now + (ttl if not error else 30), result, error)
    return result, error


def _retry_after_seconds(value):
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            date = email.utils.parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0.0, (date - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _http_failure(prefix, exc):
    response=getattr(exc,"response",None)
    status=getattr(response,"status_code",None)
    headers=getattr(response,"headers",{}) or {}
    if status==429:
        retry=_retry_after_seconds(headers.get("Retry-After"))
        return f"HTTP 429 rate limited"+(f"; Retry-After {retry:g}s" if retry is not None else "")
    if status in {401,403}:
        return f"HTTP {status} access restricted or authentication required"
    if status in {404,410}:
        return f"HTTP {status} permanently unavailable"
    if status:
        return f"{prefix} returned HTTP {status}."
    return f"{prefix} request failed: {type(exc).__name__}."


def _payload_jobs(payload):
    if isinstance(payload, list):
        return payload if all(isinstance(item, dict) for item in payload) else None
    if isinstance(payload, dict):
        for key in ("jobs", "content"):
            value = payload.get(key)
            if isinstance(value, list) and all(isinstance(item, dict) for item in value):
                return value
    return None


def fetch_greenhouse(source: EmployerSource, getter=None):
    url = f"https://boards-api.greenhouse.io/v1/boards/{quote(source.token, safe='')}/jobs"
    return _get_json(url, params={"content": "true"}, getter=getter)


def fetch_lever(source: EmployerSource, getter=None):
    url = f"https://api.lever.co/v0/postings/{quote(source.token, safe='')}"
    return _get_json(url, params={"mode": "json"}, getter=getter)


def fetch_ashby(source: EmployerSource, getter=None):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{quote(source.token, safe='')}"
    jobs, error = _get_json(url, params={"includeCompensation": "true"}, getter=getter)
    return [job for job in jobs if job.get("isListed", True)] if not error else [], error


def fetch_smartrecruiters(source: EmployerSource, getter=None):
    # Public customer career-page listing API. Paginate using the documented
    # offset/limit contract, capped to five pages to bound local runtime.
    url = f"https://api.smartrecruiters.com/v1/companies/{quote(source.token, safe='')}/postings"
    collected=[]
    for offset in range(0,500,100):
        jobs,error=_get_json(url,params={"limit":100,"offset":offset},getter=getter)
        if error:
            return collected,error
        collected.extend(jobs)
        if len(jobs)<100:
            break
    return collected,None


def fetch_source(source: EmployerSource, getter=None):
    connector = {
        "greenhouse": fetch_greenhouse,
        "lever": fetch_lever,
        "ashby": fetch_ashby,
        "smartrecruiters": fetch_smartrecruiters,
    }.get(source.source_type)
    if source.source_type in {"career_page", "public_html"}:
        return fetch_public_career_page(source, getter=getter)
    if source.source_type == "rss":
        return fetch_rss(source, getter=getter)
    if source.source_type == "sitemap":
        return fetch_sitemap_jsonld(source, getter=getter)
    if source.source_type == "workday":
        return [], "No documented universal unauthenticated Workday job-board endpoint is configured."
    if not connector:
        return [], f"Connector '{source.source_type}' has no verified public feed implementation."
    return connector(source, getter=getter)


class _JsonLdParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_jsonld = False
        self.parts = []
        self.documents = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "script" and dict(attrs).get("type", "").lower() == "application/ld+json":
            self.in_jsonld = True
            self.parts = []

    def handle_data(self, data):
        if self.in_jsonld:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "script" and self.in_jsonld:
            self.in_jsonld = False
            try:
                self.documents.append(json.loads("".join(self.parts)))
            except (TypeError, json.JSONDecodeError):
                pass


class _JobLinkParser(HTMLParser):
    """Extract visible, likely posting links without executing page scripts."""
    def __init__(self):
        super().__init__()
        self.links = []
        self._href = None
        self._parts = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            self._href = dict(attrs).get("href")
            self._parts = []

    def handle_data(self, data):
        if self._href is not None:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._href is not None:
            label = _plain(" ".join(self._parts))
            target = self._href
            if label and re.search(r"\b(job|career|position|engineer|developer|analyst|designer|scientist|intern)\b", label, re.I):
                if re.search(r"/(jobs?|careers?|positions?)/|gh_jid=|lever\.co/|ashbyhq\.com/", target, re.I):
                    self.links.append((label, target))
            self._href = None
            self._parts = []


class _LinkTargetParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.targets = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            href = dict(attrs).get("href")
            if href:
                self.targets.append(href)


class _NextPageParser(HTMLParser):
    """Recognize explicit same-origin pagination hints only."""
    def __init__(self):
        super().__init__(); self.links=[]
    def handle_starttag(self,tag,attrs):
        if tag.lower()!="a": return
        attrs=dict(attrs); href=attrs.get("href",""); rel=(attrs.get("rel","")+" "+attrs.get("aria-label","")).casefold()
        if href and ("next" in rel or attrs.get("data-page-type","").casefold()=="next"):
            self.links.append(href)


def discover_source_candidates(company_name: str, website_url: str, *, getter=None):
    """Follow explicit career-page links and identify public ATS links; never guesses identifiers."""
    getter = getter or requests.get
    try:
        url = validate_public_https_url(website_url)
        allowed, reason = _robots_allow(url, getter)
        if not allowed:
            return [], reason
        response = _safe_get(url, getter=getter, timeout=REQUEST_TIMEOUT,
                             headers={"Accept": "text/html", "User-Agent": "BabyStepsAI public career source verifier"})
        response.raise_for_status()
        if len(response.content) > MAX_RESPONSE_BYTES:
            return [], "Employer page exceeded the configured size limit."
    except requests.Timeout:
        return [], "Employer page request timed out."
    except requests.RequestException as exc:
        return [], f"Employer page request failed: {type(exc).__name__}."
    except ValueError as exc:
        return [], f"Employer URL rejected: {exc}"
    page_candidates = [(url, response.text if isinstance(response.text, str) else "")]
    # Follow only explicit, same-origin next-page links and cap at three pages.
    seen_pages={url}
    for _ in range(2):
        next_page=_NextPageParser();next_page.feed(page_candidates[-1][1])
        target=next((urljoin(page_candidates[-1][0],href) for href in next_page.links),None)
        if not target or target in seen_pages: break
        try:
            validate_public_https_url(target)
            if (urlparse(target).hostname or "").casefold()!=(urlparse(url).hostname or "").casefold(): break
            allowed,_reason=_robots_allow(target,getter)
            if not allowed: break
            page=_safe_get(target,getter=getter,timeout=REQUEST_TIMEOUT,headers={"Accept":"text/html","User-Agent":"BabyStepsAI public career listing reader"})
            page.raise_for_status()
            if len(page.content)>MAX_RESPONSE_BYTES: break
        except (ValueError,requests.RequestException):
            break
        seen_pages.add(target)
        page_candidates.append((target,page.text if isinstance(page.text,str) else ""))
    # Only follow an explicit same-company careers/jobs link; keep discovery bounded.
    class CareerLinks(HTMLParser):
        def __init__(self): super().__init__(); self.links=[]; self.href=None; self.label=[]
        def handle_starttag(self, tag, attrs):
            if tag.lower()=="a": self.href=dict(attrs).get("href"); self.label=[]
        def handle_data(self, data):
            if self.href is not None: self.label.append(data)
        def handle_endtag(self, tag):
            if tag.lower()=="a" and self.href is not None:
                label=_plain(" ".join(self.label))
                if re.search(r"\b(careers?|jobs?|join our team|open positions)\b",label,re.I): self.links.append(self.href)
                self.href=None
    career_links=CareerLinks(); career_links.feed(page_candidates[0][1])
    website_host=(urlparse(url).hostname or "").casefold()
    for href in career_links.links[:5]:
        target=urljoin(url,href)
        try:
            validate_public_https_url(target)
            if (urlparse(target).hostname or "").casefold()!=website_host: continue
            allowed,_=_robots_allow(target,getter)
            if not allowed: continue
            page=_safe_get(target,getter=getter,timeout=REQUEST_TIMEOUT,headers={"Accept":"text/html","User-Agent":"BabyStepsAI public career source verifier"})
            page.raise_for_status()
            if len(page.content)<=MAX_RESPONSE_BYTES: page_candidates.append((target,page.text if isinstance(page.text,str) else ""))
        except (ValueError,requests.RequestException):
            continue
    candidates = {}
    for evidence_url, page_text in page_candidates:
      parser = _LinkTargetParser(); parser.feed(page_text)
      for href in parser.targets:
        target = urljoin(evidence_url, href)
        try:
            validate_public_https_url(target)
        except ValueError:
            continue
        parsed = urlparse(target)
        host = (parsed.hostname or "").casefold()
        parts = [part for part in parsed.path.split("/") if part]
        kind = token = None
        if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"} and parts:
            kind, token = "greenhouse", parts[0]
        elif host in {"jobs.lever.co", "jobs.eu.lever.co"} and parts:
            kind, token = "lever", parts[0]
        elif host == "jobs.ashbyhq.com" and parts:
            kind, token = "ashby", parts[0]
        elif host in {"careers.smartrecruiters.com", "www.smartrecruiters.com"} and parts:
            kind, token = "smartrecruiters", parts[0]
        elif host.endswith("myworkdayjobs.com"):
            candidates["workday"] = {"name": company_name, "source_type": "workday", "source_token": "",
                "careers_url": target, "website_url": website_url, "evidence_url": evidence_url,
                "status": "unsupported", "reason": "Workday tenant-specific endpoint has no configured public connector."}
            continue
        if kind and token and re.fullmatch(r"[A-Za-z0-9._-]{1,200}", token):
            candidates[f"{kind}:{token}"] = {"name": company_name, "source_type": kind, "source_token": token,
                "careers_url": target, "website_url": website_url, "evidence_url": evidence_url,
                "status": "needs_validation", "reason": "Found an explicit ATS link; public feed must validate before activation."}
    if not candidates:
        career_url=page_candidates[-1][0]
        candidates["career_page"] = {"name": company_name, "source_type": "public_html", "source_token": "",
            "careers_url": career_url, "website_url": website_url, "evidence_url": career_url, "status": "needs_validation",
            "reason": "No known ATS link found; a conservative public-page parser can be validated."}
    return list(candidates.values()), None


def _jsonld_jobs(document):
    output = []
    if isinstance(document, list):
        for item in document:
            output.extend(_jsonld_jobs(item))
        return output
    if not isinstance(document, dict):
        return output
    kind = document.get("@type", [])
    if isinstance(kind, str):
        kind = [kind]
    if "JobPosting" in kind:
        output.append(document)
    for key in ("@graph", "itemListElement"):
        if isinstance(document.get(key), list):
            output.extend(_jsonld_jobs(document[key]))
    nested = document.get("mainEntity")
    if nested:
        output.extend(_jsonld_jobs(nested))
    return output


def _robots_allow(url, getter=None):
    getter = getter or requests.get
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    response = _safe_get(robots_url, getter=getter, timeout=REQUEST_TIMEOUT,
                         headers={"User-Agent": "BabyStepsAI public career listing reader"})
    if getattr(response, "status_code", 200) in (404, 410):
        return True, None
    if getattr(response,"status_code",200) in (401,403):
        return False,"robots.txt access restricted; career page was not fetched."
    if getattr(response,"status_code",200)==429:
        return False,_http_failure("robots.txt",requests.HTTPError(response=response))
    response.raise_for_status()
    parser = urllib.robotparser.RobotFileParser()
    parser.set_url(robots_url)
    robots_text = getattr(response, "text", "")
    parser.parse(robots_text.splitlines() if isinstance(robots_text, str) else [])
    allowed = parser.can_fetch("BabyStepsAI", url)
    return allowed, None if allowed else "Career page disallowed by robots.txt."


def fetch_public_career_page(source: EmployerSource, getter=None):
    getter = getter or requests.get
    try:
        url = validate_public_https_url(source.careers_url)
        allowed, reason = _robots_allow(url, getter)
        if not allowed:
            return [], reason
        response = _safe_get(url, getter=getter, timeout=REQUEST_TIMEOUT, headers={"Accept": "text/html", "User-Agent": "BabyStepsAI public career listing reader"})
        response.raise_for_status()
        if len(response.content) > MAX_RESPONSE_BYTES:
            return [], "Career page exceeded the configured size limit."
        response_url=getattr(response,"url",None)
        final_url=response_url if isinstance(response_url,str) and response_url.startswith("https://") else url
        final_host = (urlparse(final_url).hostname or "").casefold()
        ats = _recognized_ats_target(final_url, source.name)
        if ats and final_host != (urlparse(url).hostname or "").casefold():
            provider, token = ats
            redirected = EmployerSource(source.name, provider, token, final_url, source.geographic_coverage)
            jobs, error = fetch_source(redirected, getter=getter)
            if error:
                return [], f"Redirected to recognized ATS ({provider}), but its public feed failed: {error}"
            return jobs, None
        page_candidates=[(url,response.text if isinstance(response.text,str) else "")]
        seen_pages={url}
        for _ in range(2):
            next_page=_NextPageParser();next_page.feed(page_candidates[-1][1])
            target=next((urljoin(page_candidates[-1][0],href) for href in next_page.links),None)
            if not target or target in seen_pages: break
            try:
                validate_public_https_url(target)
                if (urlparse(target).hostname or "").casefold()!=(urlparse(url).hostname or "").casefold(): break
                allowed,_reason=_robots_allow(target,getter)
                if not allowed: break
                page=_safe_get(target,getter=getter,timeout=REQUEST_TIMEOUT,headers={"Accept":"text/html","User-Agent":"BabyStepsAI public career listing reader"})
                page.raise_for_status()
                if len(page.content)>MAX_RESPONSE_BYTES: break
            except (ValueError,requests.RequestException):
                break
            seen_pages.add(target)
            page_candidates.append((target,page.text if isinstance(page.text,str) else ""))
        documents=[]; links=[]
        for page_url,page_text in page_candidates:
            parser=_JsonLdParser();parser.feed(page_text);documents.extend((page_url,doc) for doc in parser.documents)
            link_parser=_JobLinkParser();link_parser.feed(page_text)
            links.extend((label,urljoin(page_url,href)) for label,href in link_parser.links)
    except requests.Timeout:
        return [], "Career page request timed out."
    except requests.RequestException as exc:
        return [], _http_failure("Career page",exc)
    except ValueError as exc:
        return [], f"Career page URL rejected: {exc}"
    jobs = []
    for page_url,document in documents:
        for item in _jsonld_jobs(document):
            location = item.get("jobLocation") or {}
            if isinstance(location, list):
                location = location[0] if location else {}
            address = location.get("address", {}) if isinstance(location, dict) else {}
            url_value = urljoin(page_url, str(item.get("url") or ""))
            jobs.append({
                "id": str(item.get("identifier", {}).get("value") if isinstance(item.get("identifier"), dict) else item.get("identifier") or url_value),
                "title": item.get("title", ""), "location": {"name": ", ".join(filter(None, [
                    address.get("addressLocality", "") if isinstance(address, dict) else "",
                    address.get("addressRegion", "") if isinstance(address, dict) else "",
                    address.get("addressCountry", "") if isinstance(address, dict) else ""]))},
                "country": address.get("addressCountry", "") if isinstance(address, dict) else "",
                "description": item.get("description", ""), "url": url_value,
                "datePosted": item.get("datePosted"), "employmentType": item.get("employmentType", ""),
                "validThrough": item.get("validThrough"),
            })
    if not jobs:
        page_host = urlparse(source.careers_url).hostname or ""
        ats_hosts = ("boards.greenhouse.io", "job-boards.greenhouse.io", "jobs.lever.co", "jobs.ashbyhq.com", "smartrecruiters.com")
        for label, target in links:
            try:
                validate_public_https_url(target)
            except ValueError:
                continue
            host = (urlparse(target).hostname or "").casefold()
            if host != page_host.casefold() and not any(host == allowed or host.endswith("." + allowed) for allowed in ats_hosts):
                continue
            jobs.append({"id": target, "title": label, "location": {"name": ""}, "description": "",
                         "url": target, "jobUrl": target, "datePosted": None})
    if not jobs:
        lowered = response.text.casefold()
        has_careers_content = bool(re.search(r"\b(careers?|open roles|open positions|join (our|the) team|we're hiring)\b", lowered))
        js_shell = any(marker in lowered for marker in ("__next_data__", "id=\"root\"", "id=\"app\"", "ng-app", "data-reactroot"))
        script_heavy = lowered.count("<script") >= 5 and len(_plain(response.text)) < 600
        if has_careers_content and (js_shell or script_heavy):
            return [], "JavaScript-rendered career page: public HTML contains a client-rendered shell but no static job records."
        return [], "Unsupported career page layout: no JobPosting JSON-LD or recognizable public job links found."
    return jobs, None


def _recognized_ats_target(url, company_name=""):
    parsed=urlparse(url); host=(parsed.hostname or "").casefold(); parts=[p for p in parsed.path.split("/") if p]
    if host in {"boards.greenhouse.io","job-boards.greenhouse.io"} and parts:
        return "greenhouse",parts[0]
    if host in {"jobs.lever.co","jobs.eu.lever.co"} and parts:
        return "lever",parts[0]
    if host=="jobs.ashbyhq.com" and parts:
        return "ashby",parts[0]
    if host in {"careers.smartrecruiters.com","www.smartrecruiters.com"} and parts:
        return "smartrecruiters",parts[0]
    return None


def fetch_jsonld_career_page(source: EmployerSource, getter=None):
    """Backward-compatible name for the structured/public career-page reader."""
    return fetch_public_career_page(source, getter=getter)


def fetch_rss(source: EmployerSource, getter=None):
    getter = getter or requests.get
    try:
        url = validate_public_https_url(source.careers_url)
        allowed, reason = _robots_allow(url, getter)
        if not allowed:
            return [], reason
        response = _safe_get(url, getter=getter, timeout=REQUEST_TIMEOUT,
                             headers={"Accept": "application/rss+xml, application/atom+xml, application/xml"})
        response.raise_for_status()
        if len(response.content) > MAX_RESPONSE_BYTES:
            return [], "RSS/Atom feed exceeded the configured size limit."
        root = ET.fromstring(response.content)
    except requests.Timeout:
        return [], "RSS/Atom request timed out."
    except requests.RequestException as exc:
        return [], _http_failure("RSS/Atom",exc)
    except (ValueError, ET.ParseError) as exc:
        return [], f"RSS/Atom feed rejected or malformed: {type(exc).__name__}."
    jobs = []
    for item in root.iter():
        if item.tag.rsplit("}", 1)[-1].lower() not in {"item", "entry"}:
            continue
        children = {child.tag.rsplit("}", 1)[-1].lower(): child for child in item}
        title = (children.get("title").text or "").strip() if children.get("title") is not None else ""
        link_node = children.get("link")
        url = (link_node.attrib.get("href") if link_node is not None else "") or (link_node.text if link_node is not None else "")
        if not title or not url:
            continue
        jobs.append({"id": url, "title": title, "url": url, "jobUrl": url,
                     "description": children.get("description").text if children.get("description") is not None else "",
                     "datePosted": children.get("pubdate").text if children.get("pubdate") is not None else None,
                     "location": {"name": ""}})
    return jobs, None if jobs else "Unsupported RSS/Atom layout: no titled job entries with links were found."


def fetch_sitemap_jsonld(source: EmployerSource, getter=None):
    getter = getter or requests.get
    """Read a sitemap and inspect a small bounded sample of public job pages."""
    try:
        url = validate_public_https_url(source.careers_url)
        allowed, reason = _robots_allow(url, getter)
        if not allowed:
            return [], reason
        response = _safe_get(url, getter=getter, timeout=REQUEST_TIMEOUT, headers={"Accept": "application/xml, text/xml"})
        response.raise_for_status()
        if len(response.content) > MAX_RESPONSE_BYTES:
            return [], "Sitemap exceeded the configured size limit."
        root = ET.fromstring(response.content)
    except requests.Timeout:
        return [], "Sitemap request timed out."
    except requests.RequestException as exc:
        return [], _http_failure("Sitemap",exc)
    except (ValueError, ET.ParseError) as exc:
        return [], f"Sitemap rejected or malformed: {type(exc).__name__}."
    urls = [node.text.strip() for node in root.iter() if node.tag.rsplit("}", 1)[-1].lower() == "loc" and node.text]
    urls = [item for item in urls if re.search(r"/(jobs?|careers?|positions?)/", urlparse(item).path, re.I)][:20]
    jobs = []
    for item_url in urls:
        try:
            validate_public_https_url(item_url)
            allowed, _reason = _robots_allow(item_url, getter)
            if not allowed:
                continue
            page = _safe_get(item_url, getter=getter, timeout=REQUEST_TIMEOUT,
                             headers={"Accept": "text/html", "User-Agent": "BabyStepsAI public career listing reader"})
            page.raise_for_status()
            parser = _JsonLdParser()
            parser.feed(page.text)
            for document in parser.documents:
                for item in _jsonld_jobs(document):
                    item.setdefault("url", item_url)
                    jobs.append(item)
        except (ValueError, requests.RequestException):
            continue
    return jobs, None if jobs else "Unsupported sitemap: no permitted JobPosting JSON-LD pages found in the bounded sample."


def _plain(value):
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", value))).strip()


def _get_path(data, *path):
    value = data
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def normalize_job(source_type: str, source: EmployerSource, raw: dict) -> dict:
    """Normalize adapter records without filling unknown facts by inference."""
    if source_type == "greenhouse":
        external_id = raw.get("id")
        title = raw.get("title")
        loc = _get_path(raw, "location", "name") or ""
        description = _plain(raw.get("content", ""))
        url = raw.get("absolute_url") or ""
        posting_date = raw.get("first_published") or raw.get("updated_at")
        employment = ""
        for meta in raw.get("metadata") or []:
            if isinstance(meta, dict) and "employment" in str(meta.get("name", "")).lower():
                employment = str(meta.get("value") or "")
    elif source_type == "lever":
        external_id = raw.get("id")
        title = raw.get("text")
        categories = raw.get("categories") or {}
        loc = categories.get("location") or ", ".join(categories.get("allLocations") or [])
        description = _plain(raw.get("descriptionPlain") or raw.get("description") or "")
        url = raw.get("applyUrl") or raw.get("hostedUrl") or ""
        posting_date = raw.get("createdAt")
        employment = str(categories.get("commitment") or "")
    elif source_type == "ashby":
        external_id = raw.get("id") or raw.get("jobUrl") or raw.get("applyUrl")
        title = raw.get("title")
        loc = raw.get("location") or ""
        description = _plain(raw.get("descriptionPlain") or raw.get("descriptionHtml") or "")
        url = raw.get("applyUrl") or raw.get("jobUrl") or ""
        posting_date = raw.get("publishedAt")
        employment = str(raw.get("employmentType") or "")
    elif source_type == "smartrecruiters":
        external_id = raw.get("id")
        title = raw.get("name")
        loc = ", ".join(filter(None, (_get_path(raw, "location", "city"), _get_path(raw, "location", "region"), _get_path(raw, "location", "country"))))
        description = _plain(raw.get("jobAd", {}).get("sections", {}).get("jobDescription", {}).get("text", "") if isinstance(raw.get("jobAd"), dict) else "")
        url = raw.get("applyUrl") or raw.get("jobAdUrl") or ""
        posting_date = raw.get("releasedDate") or raw.get("postedDate")
        employment = str(raw.get("typeOfEmployment", {}).get("label", "") if isinstance(raw.get("typeOfEmployment"), dict) else "")
    elif source_type in {"career_page", "public_html", "sitemap", "rss"}:
        external_id = raw.get("id") or raw.get("url")
        title = raw.get("title")
        loc = _get_path(raw, "location", "name") or ""
        description = _plain(raw.get("description", ""))
        url = raw.get("url") or ""
        posting_date = raw.get("datePosted")
        employment = str(raw.get("employmentType") or "")
    else:
        raise ValueError(f"No normalizer for {source_type}.")
    if not isinstance(external_id, (str, int)) or not isinstance(title, str) or not title.strip():
        raise ValueError("Job record lacks a stable source ID or title.")
    external_id = str(external_id)
    stable_key = f"{source_type}:{source.token}:{external_id}"
    fingerprint = hashlib.sha256((title.strip().casefold() + "\n" + description.strip()).encode()).hexdigest()
    address = raw.get("address", {})
    postal = address.get("postalAddress", {}) if isinstance(address, dict) else {}
    country = (postal.get("addressCountry") or raw.get("country") or "") if isinstance(postal, dict) else ""
    if source_type in {"career_page", "public_html", "sitemap", "rss"}:
        country = raw.get("country") or country
    work_arrangement = raw.get("workplaceType") or ("Remote" if raw.get("isRemote") is True else "")
    active = raw.get("status", "").casefold() not in {"closed", "inactive", "expired", "archived"}
    valid_through = raw.get("validThrough")
    if valid_through:
        try:
            expiry = datetime.fromisoformat(str(valid_through).replace("Z", "+00:00"))
            if expiry.tzinfo is None: expiry = expiry.replace(tzinfo=timezone.utc)
            active = active and expiry > datetime.now(timezone.utc)
        except ValueError:
            pass
    return {
        "job_key": stable_key, "source_platform": source_type, "source_id": external_id,
        "company": source.name, "title": title.strip(), "career_category": None,
        "location": str(loc or ""), "country": str(country or ""),
        "work_arrangement": str(work_arrangement or ""), "employment_type": employment,
        "experience_level": None, "salary": raw.get("compensation") or raw.get("salaryRange"),
        "description": description, "required_qualifications": None, "preferred_qualifications": None,
        "application_url": str(url or ""), "job_url": str(raw.get("jobUrl") or raw.get("hostedUrl") or url or ""),
        "posting_date": posting_date, "last_verified_at": datetime.now(timezone.utc).isoformat(),
        "active": active, "content_fingerprint": fingerprint,
        "raw_source_record": raw,
    }


def clear_cache():
    _cache.clear()


class WorkdayAdapter:
    """Explicit unsupported adapter; Workday career endpoints are tenant-specific."""
    source_type = "workday"

    def fetch(self, _source):
        return [], "No documented universal unauthenticated Workday job-board endpoint is configured."
