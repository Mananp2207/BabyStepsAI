import re
from html import unescape
from config import read_json, write_json


def plain_text(html):
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html or ""))).strip()


def classify(job, prefs):
    title = job.get("title", "").lower()
    description = plain_text(job.get("description_html", "")).lower()
    roles = [str(x).lower() for x in prefs.get("roles", [])]
    levels = set(prefs.get("experience_levels", []))
    reasons = []
    if prefs.get("exclude_internships") and re.search(r"\bintern(ship)?\b", title):
        return "excluded", ["Internship excluded by preferences"]
    if roles and not any(role in title for role in roles):
        return "excluded", ["Title outside requested roles"]
    early = bool(levels & {"new_grad", "entry_level", "junior", "associate", "early_career"})
    if early and re.search(r"\b(senior|staff|principal|director|manager|lead|vp)\b", title):
        return "excluded", ["Senior title excluded for early-career search"]
    location = job.get("location", "")
    requested = [str(x).lower() for x in prefs.get("locations", [])]
    if "united states" in requested:
        foreign = ["london", "united kingdom", "tel aviv", "israel", "india",
                   "canada", "germany", "france", "singapore", "australia"]
        if any(place in location.lower() for place in foreign):
            return "excluded", ["Listed location outside United States"]
        if not location or location.lower() in {"hybrid", "in-office", "remote", "onsite"}:
            reasons.append("Geographic location unverified")
    if not description:
        reasons.append("Job description missing")
    if early:
        if re.search(r"\b(new grad(?:uate)?|entry.level|junior|early career|recent graduate)\b", title):
            reasons.append("Early-career title")
        else:
            reasons.append("Experience requirements need verification")
    reasons.append("Employment type, authorization and mandatory requirements not verified")
    return "needs_review", reasons


def match_jobs():
    jobs = read_json("discovered_jobs.json", [])
    config = read_json("user_preferences.json", {})
    prefs = config.get("job_search", {})
    profile = read_json("candidate_profile.json", {})
    if not profile:
        print("Warning: candidate profile missing; run candidate_profile.py first")
    results = {"possible_matches": [], "needs_review": [], "excluded": []}
    for job in jobs:
        status, reasons = classify(job, prefs)
        record = {key: value for key, value in job.items() if key != "description_html"}
        record.update({"status": status, "reasons": reasons})
        results[{"needs_review": "needs_review", "excluded": "excluded"}[status]].append(record)
    write_json("matching_results.json", results)
    print("BabyStepsAI matching results (conservative, no auto-approval):")
    for key, value in results.items():
        print(f"{key}: {len(value)}")
    print("Saved matching_results.json")
    return results


if __name__ == "__main__":
    match_jobs()
