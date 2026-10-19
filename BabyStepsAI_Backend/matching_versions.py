"""Stable fingerprints for determining whether eligibility results are stale."""
import hashlib
import json


def fingerprint(value) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def profile_fingerprint(profile, resume_id="") -> str:
    return fingerprint({"resume_id": resume_id, "profile": profile})


def job_fingerprint(job) -> str:
    fields = ("title", "company", "location", "url", "description", "employment_type", "country", "work_arrangement")
    return fingerprint({field: job.get(field) for field in fields})
