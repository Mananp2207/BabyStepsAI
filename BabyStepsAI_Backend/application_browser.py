"""Source-independent safety and page-understanding helpers for dry runs."""
import re
import hashlib
import json
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
    # Cookie choices, notification prompts, and pagination controls are not
    # application submission actions even when an employer page marks them as
    # submit buttons. Keeping these out of the report avoids false warnings.
    if text.casefold() in {"accept", "accept all", "necessary only", "customize settings", "deny", "reject", "decline", "create alert", "allow all", "manage preferences", "manage cookies"} or re.fullmatch(r"\d+", text):
        return "other"
    if str(tag).casefold() == "a" and re.fullmatch(r"apply(?: now)?", text, re.I):
        return "apply_link"
    if str(tag).casefold() == "button" and str(control_type).casefold() == "button" and re.fullmatch(r"apply(?: now)?", text, re.I):
        return "ambiguous" if inside_form else "apply_button"
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
    if re.search(r"\bcookies?\b",text,re.I) and re.search(r"\b(?:accept all|necessary only|customize settings|cookie preferences?)\b",text,re.I):
        return "cookie_preference_required"
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


def summarize_form_metadata(provider, fields, controls, interruption=None, expired=False):
    """Build a value-free compatibility report from public form metadata.

    ``fields`` must contain labels and control attributes only. This helper is
    deliberately independent of candidate verification and application filling.
    """
    fields = [item for item in (fields or []) if isinstance(item, dict)]
    inspection_meta = next((item for item in fields if item.get("__inspection_meta__")), {})
    ignored_controls = [item for item in (inspection_meta.get("ignored_controls") or []) if isinstance(item,dict)]
    fields = [item for item in fields if not item.get("__inspection_meta__")]
    controls = [item for item in (controls or []) if isinstance(item, dict)]
    questions_by_key = {}
    for index, item in enumerate(fields):
        question = re.sub(r"\s+", " ", str(item.get("question") or item.get("label") or "")).strip()[:160]
        kind = str(item.get("kind") or "unknown")[:40]
        key = "question:"+re.sub(r"[^a-z0-9]+", " ", question.casefold()).strip() if question else (str(item.get("question_key") or "").strip() or "unresolved|"+str(index))
        stable_id=str(item.get("question_key") or item.get("id") or item.get("name") or "").strip()[:160]
        fallback_id="q_"+hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        group = questions_by_key.setdefault(key, {"question": question, "question_id":stable_id or fallback_id,
            "required": None, "control_types": [], "controls": [], "conditional": False})
        if question and not group["question"]: group["question"] = question
        if item.get("required") is True: group["required"] = True
        elif item.get("required") is False and group["required"] is not True: group["required"] = False
        group["conditional"] = bool(group["conditional"] or item.get("conditional"))
        if kind not in group["control_types"]: group["control_types"].append(kind)
        control = {"kind": kind, "required":("unknown" if item.get("required") is None else bool(item.get("required"))), "label": str(item.get("label") or "")[:160]}
        for key_name in ("tag", "input_type", "role", "name", "id", "class_hint", "container_class_hint", "aria_hints", "container_control_count", "possible_custom_selector", "unresolved_reason"):
            if item.get(key_name) not in (None, "", False): control[key_name] = str(item[key_name])[:120] if not isinstance(item[key_name], bool) else True
        if kind == "file":
            control["upload_purpose"] = str(item.get("upload_purpose") or "unspecified")[:40]
            control["accept"] = str(item.get("accept") or "")[:120]
        group["controls"].append(control)
    questions = [item for item in questions_by_key.values() if item.get("question")]
    required_questions = [item for item in questions if item["required"] is True]
    optional_questions = [item for item in questions if item["required"] is False]
    unknown_requirement_questions = [item for item in questions if item["required"] is None]
    required = [item["question"] for item in required_questions]
    upload_fields = [str(item.get("question") or item.get("label") or "File upload")[:160]
                     for item in fields if item.get("kind") == "file"]
    supported_kinds = {"text", "email", "tel", "url", "number", "date", "textarea", "contenteditable",
                       "select", "combobox", "checkbox", "radio", "file"}
    supported = [str(item.get("label") or item.get("kind") or "Form field")[:160]
                 for item in fields if item.get("kind") in supported_kinds]
    unsupported = [str(item.get("label") or item.get("kind") or "Custom field")[:160]
                   for item in fields if item.get("kind") not in supported_kinds]
    known_file_questions={re.sub(r"[^a-z0-9]+"," ",str(item.get("question") or item.get("label") or "").casefold()).strip()
                          for item in fields if item.get("kind")=="file"}
    upload_prompts=[]
    for control in controls:
        if not control.get("upload_prompt"): continue
        question=str(control.get("upload_question") or "").strip()[:160]
        normalized=re.sub(r"[^a-z0-9]+"," ",question.casefold()).strip()
        if normalized and any(normalized==value or normalized in value or value in normalized for value in known_file_questions):
            continue
        purpose=str(control.get("upload_purpose") or "unspecified")[:40]
        required=control.get("upload_required")
        upload_prompts.append({"question":question or "Unresolved file upload", "purpose":purpose,
            "required":("unknown" if required is None else bool(required)),"verified_input":False})
    # A visible Attach/Upload trigger is an applicant-facing question even if
    # the employer hides or omits its backing file input from the public DOM.
    # Retain it in question counts, but keep its control explicitly unresolved.
    for prompt in upload_prompts:
        question=prompt["question"]
        key="question:"+re.sub(r"[^a-z0-9]+"," ",question.casefold()).strip() if question!="Unresolved file upload" else "unresolved|upload_prompt"
        group=questions_by_key.setdefault(key,{"question":question,"question_id":"q_"+hashlib.sha256(key.encode("utf-8")).hexdigest()[:16],
            "required":None,"control_types":[],"controls":[],"conditional":False})
        if prompt["required"] is True:group["required"]=True
        elif prompt["required"] is False and group["required"] is not True:group["required"]=False
        if "file_upload_prompt" not in group["control_types"]:group["control_types"].append("file_upload_prompt")
        group["controls"].append({"kind":"file_upload_prompt","required":prompt["required"],"label":question,
            "upload_purpose":prompt["purpose"],"verified_input":False})
    questions=[item for item in questions_by_key.values() if item.get("question")]
    required_questions=[item for item in questions if item["required"] is True]
    optional_questions=[item for item in questions if item["required"] is False]
    unknown_requirement_questions=[item for item in questions if item["required"] is None]
    required=[item["question"] for item in required_questions]
    actions = submission_controls(controls)
    final_actions=[item for item in actions if item.get("action")=="final_submission"]
    ambiguous_actions=[item for item in actions if item.get("action")=="ambiguous"]
    submission_status=("detected_visible" if any(item.get("visible",True) for item in controls if item.get("action")=="final_submission") else
                       "detected_hidden" if final_actions else
                       "ambiguous_unresolved" if ambiguous_actions else "not_detected_unresolved")
    unresolved_controls = []
    for item in fields:
        reasons=[]
        if not str(item.get("question") or item.get("label") or "").strip(): reasons.append("Applicant question label could not be determined")
        if item.get("unresolved_reason"): reasons.append(str(item["unresolved_reason"])[:160])
        if item.get("possible_custom_selector"): reasons.append("Text input has closed-choice wording but no reliable selector semantics")
        if item.get("kind") not in supported_kinds: reasons.append("Control type is not supported")
        if reasons:
            unresolved_controls.append({"question":str(item.get("question") or item.get("label") or "Unlabeled control")[:160],
                "kind":str(item.get("kind") or "unknown")[:40],"required":("unknown" if item.get("required") is None else bool(item.get("required"))),"reasons":list(dict.fromkeys(reasons))})
    for prompt in upload_prompts:
        unresolved_controls.append({"question":prompt["question"],"kind":"file_upload_prompt","required":prompt["required"],
            "reasons":["A visible upload button was found, but its file input was not exposed in read-only inspection."]})
    human_actions = []
    if interruption == "human_verification": human_actions.append("CAPTCHA or human verification")
    elif interruption == "authentication_required": human_actions.append("Employer sign-in is required")
    elif interruption == "automation_restricted": human_actions.append("Employer restricts automated access")
    elif interruption == "cookie_preference_required": human_actions.append("Cookie preference prompt left untouched")
    if expired: human_actions.append("This posting appears to be expired")
    multi_step = any(item.get("action") in {"safe_next", "review"} for item in controls)
    form_detected = bool(fields or upload_prompts)
    if interruption in {"human_verification", "authentication_required", "automation_restricted","cookie_preference_required"}:
        outcome = "needs_human_action"
    elif expired:
        outcome = "inaccessible"
    elif not form_detected:
        outcome = "unsupported"
    elif unsupported or unresolved_controls or submission_status in {"not_detected_unresolved","ambiguous_unresolved"}:
        outcome = "partially_compatible"
    else:
        outcome = "compatible"
    structural_signature={"provider":(provider or {}).get("provider"),
        "questions":[{"id":item.get("question_id"),"types":item.get("control_types"),
            "required":item.get("required"),"conditional":item.get("conditional")} for item in questions],
        "unresolved":[{"kind":item.get("kind"),"required":item.get("required")} for item in unresolved_controls],
        "submission":submission_status,"multi_step":multi_step}
    form_fingerprint=hashlib.sha256(json.dumps(structural_signature,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode("ascii")).hexdigest()
    return {
        "provider": dict(provider or {}),
        "form_detected": form_detected,
        "fields": [{"label": str(item.get("label") or "")[0:160],
                    "question": str(item.get("question") or item.get("label") or "")[0:160],
                    "kind": str(item.get("kind") or "unknown")[:40],
                    "required": ("unknown" if item.get("required") is None else bool(item.get("required"))),
                    **({"conditional":True} if item.get("conditional") else {}),
                    **({"upload_purpose":str(item.get("upload_purpose") or "unspecified")[:40]} if item.get("kind")=="file" else {}),
                    **({"possible_custom_selector":True} if item.get("possible_custom_selector") else {}),
                    **({"accept": str(item.get("accept") or "")[:120]} if item.get("kind") == "file" else {})}
                   for item in fields[:100]],
        "ignored_controls": ignored_controls[:100],
        "ignored_control_count": len(ignored_controls),
        "questions": questions[:100],
        "required_questions": required_questions[:100],
        "optional_questions": optional_questions[:100],
        "unique_question_count": len(questions),
        "unique_required_question_count": len(required_questions),
        "unique_optional_question_count": len(optional_questions),
        "unique_unknown_requirement_question_count": len(unknown_requirement_questions),
        "unknown_requirement_questions": unknown_requirement_questions[:100],
        "unique_unknown_requirement_question_count": len(unknown_requirement_questions),
        "unknown_requirement_questions": unknown_requirement_questions[:100],
        "required_control_count": sum(1 for item in fields if item.get("required") is True),
        "visible_form_control_count": len(fields),
        "action_control_count": len(controls),
        "total_dom_control_count": len(fields)+len(ignored_controls)+len(controls),
        "required_dom_control_count": sum(1 for item in fields if item.get("required") is True)+sum(1 for item in ignored_controls if item.get("required")),
        "ignored_required_control_count": sum(1 for item in ignored_controls if item.get("required")),
        "unresolved_required_control_count": sum(1 for item in unresolved_controls if item.get("required") is not False),
        "unresolved_visible_control_count": len(unresolved_controls),
        "supported_fields": supported[:100],
        "unsupported_fields": unsupported[:100],
        "unresolved_controls": unresolved_controls[:100],
        "conditional_questions": [item["question"] for item in questions if item.get("conditional")],
        "conditional_detection_note": "Read-only inspection can identify explicit conditional markers only; it does not activate controls to reveal other conditional questions.",
        "required_fields": required[:100],
        "file_upload_controls": upload_fields[:30],
        "human_actions_required": human_actions,
        "multi_step_detected": multi_step,
        "submission_control_detected": bool(final_actions),
        "submission_controls": actions[:30],
        "submission_control_status": submission_status,
        "upload_prompts": upload_prompts[:30],
        "form_fingerprint": form_fingerprint,
        "compatibility_status": ("needs_human_action" if interruption in {"human_verification","authentication_required","automation_restricted","cookie_preference_required"} else
            "inaccessible" if expired else "unsupported" if not form_detected else
            "unresolved" if unresolved_controls or submission_status in {"not_detected_unresolved","ambiguous_unresolved"} else
            "partially_compatible" if unsupported else "compatible"),
        "inspection_outcome": outcome,
        "reason": ("The page requires a human action before its form can be inspected." if human_actions else
                   "No public application form was detected." if not form_detected else
                   "Some required fields or control semantics could not be resolved." if unresolved_controls else
                   "The final submission control could not be identified reliably." if submission_status in {"not_detected_unresolved","ambiguous_unresolved"} else
                   "Some controls use unsupported or custom semantics." if unsupported else
                   "Public form metadata was inspected without entering or submitting information."),
    }
