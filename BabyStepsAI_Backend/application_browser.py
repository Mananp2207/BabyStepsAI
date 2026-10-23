"""Source-independent safety and page-understanding helpers for dry runs."""
import re
from urllib.parse import urljoin
from urllib.parse import urlparse


FINAL_WORDING = re.compile(
    r"\b(submit|send|apply|finish|complete)\b.*\b(application|my application|your application)\b|"
    r"\b(submit application|send application|apply now|finish application|complete application)\b",
    re.I,
)
REVIEW_WORDING = re.compile(r"\b(review|preview)\b", re.I)
AUTH_WORDING = re.compile(r"\b(sign in|log in|login|authentication required|create an account to apply)\b", re.I)
EXPIRED_WORDING = re.compile(r"\b(job (has )?expired|position (is )?no longer available|job is closed|no longer accepting applications)\b", re.I)
HUMAN_WORDING = re.compile(r"\b(captcha|verify you are human|human verification|security challenge|robot check)\b", re.I)
AUTOMATION_RESTRICTION = re.compile(r"\b(automated applications? (?:are )?not permitted|do not use (?:bots|automation|automated tools)|no automated submissions|automation is prohibited|must complete (?:this|the) application (?:yourself|manually))\b", re.I)

# Provider recognition is descriptive. All adapters share the same semantic
# field reader and safety gates; recognizing a host never implies that every
# tenant or form layout is supported.
PROVIDER_HOSTS = {
    "ashby": ("jobs.ashbyhq.com", "ashbyhq.com"),
    "greenhouse": ("boards.greenhouse.io", "job-boards.greenhouse.io", "greenhouse.io"),
    "lever": ("jobs.lever.co", "lever.co"),
    "smartrecruiters": ("jobs.smartrecruiters.com", "smartrecruiters.com"),
    "workday": ("myworkdayjobs.com", "workday.com"),
}


def detect_application_provider(url):
    """Return a conservative provider descriptor for a public application URL."""
    try:
        host = (urlparse(str(url or "")).hostname or "").rstrip(".").casefold()
    except ValueError:
        host = ""
    for provider, domains in PROVIDER_HOSTS.items():
        if any(host == domain or host.endswith("." + domain) for domain in domains):
            support = "limited_public_layout" if provider in {"smartrecruiters", "workday"} else "accessibility_first"
            return {"provider": provider, "support_level": support, "adapter": "shared_semantic_form"}
    return {"provider": "employer_site", "support_level": "generic_accessible_form", "adapter": "shared_semantic_form"}


def classify_control(label, tag="button", control_type="submit", inside_form=False):
    """Classify a visible control; unknown behavior is never treated as navigation."""
    text = re.sub(r"\s+", " ", str(label or "")).strip()
    if str(tag).casefold() == "a" and re.fullmatch(r"apply(?: now)?", text, re.I):
        return "apply_link"
    if str(tag).casefold() == "button" and str(control_type).casefold() == "button" and not inside_form and re.fullmatch(r"apply(?: now)?", text, re.I):
        return "apply_button"
    if FINAL_WORDING.search(text) or str(control_type).casefold() == "submit":
        return "final_submission"
    if REVIEW_WORDING.search(text):
        return "review"
    if text.casefold() in {"next", "next step", "go to next step"} and str(control_type).casefold() == "button":
        return "safe_next"
    if re.fullmatch(r"(?:continue|save and continue) to (?:the )?(?:next )?(?:step|section|education|experience|work experience|work history|additional information|questions|review)", text, re.I) and str(control_type).casefold() == "button":
        return "safe_next"
    if text.casefold() in {"continue", "save", "apply", "apply now"}:
        return "ambiguous"
    return "other"


def apply_targets(page_url, links):
    """Resolve only explicit Apply links; destination validation remains caller-owned."""
    result = []
    for item in links or []:
        if not isinstance(item, dict):
            continue
        if classify_control(item.get("label"), tag="a", control_type="link") != "apply_link":
            continue
        href = str(item.get("href") or "").strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:")):
            continue
        result.append(urljoin(page_url, href))
    return list(dict.fromkeys(result))


def page_interruption(body_text, has_password_field=False):
    text = str(body_text or "")
    if HUMAN_WORDING.search(text):
        return "human_verification"
    if AUTOMATION_RESTRICTION.search(text):
        return "automation_restricted"
    if has_password_field or AUTH_WORDING.search(text):
        return "authentication_required"
    if EXPIRED_WORDING.search(text):
        return "expired_listing"
    return None


def submission_controls(controls):
    """Return user-facing descriptions of any final/ambiguous controls, no clicks."""
    found = []
    for control in controls or []:
        label = str(control.get("label") or "").strip()
        action = classify_control(label, control.get("tag"), control.get("type"), control.get("inside_form", False))
        if action in {"final_submission", "ambiguous"}:
            found.append({"label": label[:160], "action": action})
    return found
