"""Resume-version-scoped verification and reusable application answers."""
import json
import re
import sqlite3
import ipaddress
from urllib.parse import urlparse
from typing import Any
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from job_search_agent import USER
from ui_theme import apply_ui_theme
from secure_data import EncryptionUnavailable, decrypt_text, encrypt_text

DB = Path(__file__).resolve().parent / "babysteps.db"
router = APIRouter()

FIELDS = {
    "full_name": ("Contact", "Full name", "text", False),
    "email": ("Contact", "Email address", "email", False),
    "phone": ("Contact", "Phone number", "tel", False),
    "github": ("Links", "GitHub URL", "url", False),
    "linkedin": ("Links", "LinkedIn URL", "url", False),
    "portfolio": ("Links", "Portfolio URL", "url", False),
    "website": ("Links", "Personal website", "url", False),
    "location": ("Personal information", "Current location", "text", False),
    "country": ("Contact and location", "Country", "text", False),
    "state_province": ("Contact and location", "State / province", "text", False),
    "city": ("Contact and location", "City", "text", False),
    "postal_code": ("Contact and location", "Postal code", "text", False),
    "address_line1": ("Contact and location", "Residential or mailing address line 1 (optional)", "text", False),
    "address_line2": ("Contact and location", "Address line 2 (optional)", "text", False),
    "phone_country_code": ("Contact and location", "Phone country code", "text", False),
    "country_work_authorizations": ("Sensitive application answers", "Work authorization by country", "objects", True),
    "sponsorship_by_country": ("Sensitive application answers", "Sponsorship needs by country", "objects", True),
    "work_arrangement_preferences": ("Preferences", "Remote / hybrid / on-site preferences (one per line)", "list", False),
    "preferred_countries": ("Preferences", "Preferred countries (one per line)", "list", False),
    "other_professional_links": ("Links", "Other professional profile URLs (one per line)", "list", False),
    "salary_currency": ("Application answers", "Salary currency", "text", False),
    "earliest_available_date": ("Application answers", "Earliest available start date", "date", False),
    "notice_period": ("Availability", "Notice period", "text", False),
    "full_time_availability": ("Availability", "Full-time availability", "text", False),
    "internship_availability": ("Availability", "Internship availability", "text", False),
    "willingness_to_travel": ("Application answers", "Willingness to travel", "text", False),
    "education_entries": ("Education", "Education history", "objects", False),
    "experience_entries": ("Professional experience", "Employment history", "objects", False),
    "internship_entries": ("Internships", "Internship history", "objects", False),
    "project_entries": ("Projects", "Projects", "objects", False),
    "skills": ("Skills", "Technical skills", "chips", False),
    "university": ("Education", "University", "text", False),
    "degree": ("Education", "Degree", "text", False),
    "major": ("Education", "Major", "text", False),
    "graduation_date": ("Education", "Graduation month and year", "month", False),
    # Retained for compatibility with older profile data and consumers.
    "graduation_month": ("Education", "Graduation month", "text", False),
    "graduation_year": ("Education", "Graduation year", "text", False),
    "work_experience": ("Experience", "Professional experience (one item per line)", "list", False),
    "internships": ("Experience", "Internships (one item per line)", "list", False),
    "projects": ("Experience", "Projects (one item per line)", "list", False),
    "technical_skills": ("Qualifications", "Technical skills (one per line)", "list", False),
    "certifications": ("Qualifications", "Certifications (one per line)", "list", False),
    "licenses": ("Qualifications", "Relevant licenses (one per line)", "list", False),
    "spoken_languages": ("Qualifications", "Spoken languages (one per line)", "list", False),
    "preferred_locations": ("Preferences", "Preferred work locations (one per line)", "list", False),
    "work_authorization": ("Sensitive application answers", "Countries where authorized to work", "text", True),
    "requires_sponsorship": ("Sensitive application answers", "Will you require sponsorship? (Yes/No)", "text", True),
    "relocation_willingness": ("Preferences", "Relocation willingness", "text", False),
    "availability": ("Application answers", "Availability / notice period", "text", False),
    "salary_expectations": ("Application answers", "Salary expectations", "text", False),
    "employment_preferences": ("Application answers", "Employment preferences", "text", False),
    "additional_information": ("Application answers", "Additional application information", "list", False),
}


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect():
    db = sqlite3.connect(DB, timeout=30, factory=ClosingConnection)
    db.row_factory = sqlite3.Row
    return db


def now():
    return datetime.now(timezone.utc).isoformat()


def initialize():
    with connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS candidate_verifications (
            resume_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
            values_json TEXT NOT NULL, confirmed_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(resume_id) REFERENCES resumes(id))""")
        db.execute("""CREATE TABLE IF NOT EXISTS application_answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
            resume_id TEXT NOT NULL, question_key TEXT NOT NULL,
            question_text TEXT NOT NULL DEFAULT '',
            answer TEXT NOT NULL, sensitive INTEGER NOT NULL DEFAULT 0,
            confirmed INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, revoked_at TEXT,
            UNIQUE(user_id,resume_id,question_key),
            FOREIGN KEY(resume_id) REFERENCES resumes(id))""")
        existing = {row[1] for row in db.execute("PRAGMA table_info(application_answers)")}
        for name,declaration in (("question_text","TEXT NOT NULL DEFAULT ''"),("answer_type","TEXT NOT NULL DEFAULT 'factual'"),
                                 ("jurisdiction","TEXT NOT NULL DEFAULT ''"),("auto_reuse","INTEGER NOT NULL DEFAULT 0"),
                                 ("source","TEXT NOT NULL DEFAULT 'user_confirmed'"),
                                 ("answer_encrypted","TEXT NOT NULL DEFAULT ''"),
                                 ("question_category","TEXT NOT NULL DEFAULT 'other'"),
                                 ("sensitivity_classification","TEXT NOT NULL DEFAULT 'ordinary'"),
                                 ("confirmation_at","TEXT")):
            if name not in existing: db.execute(f"ALTER TABLE application_answers ADD COLUMN {name} {declaration}")


initialize()


SENSITIVE_PROFILE_FIELDS = {key for key, (_, _, _, sensitive) in FIELDS.items() if sensitive}
ENCRYPTED_MARKER = "__babysteps_fernet_v1__"


def _encode_profile_values(values):
    saved=dict(values)
    for key in SENSITIVE_PROFILE_FIELDS:
        value=saved.get(key)
        if value in (None,"",[],{}):
            continue
        if isinstance(value,dict) and set(value)=={ENCRYPTED_MARKER}:
            continue
        saved[key]={ENCRYPTED_MARKER:encrypt_text(json.dumps(value,ensure_ascii=False,separators=(",",":")))}
    return saved


def _decode_profile_values(values):
    decoded=dict(values); encrypted=dict(values); migrated=False
    for key in SENSITIVE_PROFILE_FIELDS:
        value=decoded.get(key)
        if isinstance(value,dict) and set(value)=={ENCRYPTED_MARKER}:
            decoded[key]=json.loads(decrypt_text(value[ENCRYPTED_MARKER]))
        elif value not in (None,"",[],{}):
            # Additive lazy migration keeps the original confirmed fact while
            # replacing its plaintext representation with authenticated ciphertext.
            encrypted[key]={ENCRYPTED_MARKER:encrypt_text(json.dumps(value,ensure_ascii=False,separators=(",",":")))}
            migrated=True
    return decoded,encrypted,migrated


def verified_values_from_json(values_json):
    """Decode profile JSON at the controlled application boundary."""
    values=json.loads(values_json or "{}")
    if not isinstance(values,dict): raise ValueError("Profile values must be a JSON object.")
    return _decode_profile_values(values)[0]


def encode_answer_for_storage(answer, sensitive):
    """Return plaintext/ciphertext columns for the information bank."""
    text=str(answer or "")
    return ("" if sensitive else text, encrypt_text(text) if sensitive and text else "")


def _answer_plaintext(row,db=None):
    if not row["sensitive"]:
        return row["answer"]
    encrypted=row["answer_encrypted"] if "answer_encrypted" in row.keys() else ""
    if encrypted:
        return decrypt_text(encrypted)
    legacy=row["answer"] or ""
    if legacy and db is not None:
        db.execute("UPDATE application_answers SET answer='',answer_encrypted=? WHERE id=?",
                   (encrypt_text(legacy),row["id"]))
    return legacy


def _active(db):
    row = db.execute("""SELECT r.id,r.stored_name,r.original_name,p.status,p.profile_json
        FROM resumes r LEFT JOIN resume_profiles p ON p.resume_id=r.id
        WHERE r.user_id=? AND r.active=1 ORDER BY r.created_at DESC LIMIT 1""", (USER,)).fetchone()
    if row is None:
        raise HTTPException(409, "Upload and activate a resume before verifying your profile.")
    return row


def _suggestions(profile):
    profile = profile if isinstance(profile, dict) else {}
    result = {key: ([] if kind in {"list", "objects", "chips"} else "") for key, (_, _, kind, _) in FIELDS.items()}
    for key in ("full_name", "email", "phone", "github", "linkedin", "portfolio", "website", "graduation_year"):
        value = profile.get(key)
        if isinstance(value, (str, int)):
            result[key] = str(value)
    for key in ("work_experience", "projects", "technical_skills", "certifications", "licenses"):
        value = profile.get(key)
        if isinstance(value, list):
            result[key] = [json.dumps(item, ensure_ascii=False) if isinstance(item, dict) else str(item) for item in value]
    result["location"] = str(profile.get("location") or profile.get("current_location") or "")
    result["spoken_languages"] = [str(x) for x in profile.get("spoken_languages", []) if x] if isinstance(profile.get("spoken_languages"), list) else []
    result["skills"] = list(dict.fromkeys(str(x) for x in (profile.get("technical_skills", []) + profile.get("professional_skills", [])) if isinstance(x, str))) if isinstance(profile.get("technical_skills", []), list) and isinstance(profile.get("professional_skills", []), list) else []
    def date_value(item, stem):
        direct = item.get(stem + "_date") or item.get(stem)
        if isinstance(direct, str) and re.fullmatch(r"\d{4}-\d{2}", direct): return direct
        month = str(item.get(stem + "_month") or "").strip(); year = str(item.get(stem + "_year") or "").strip()
        if month and re.fullmatch(r"\d{4}", year):
            try:
                number = int(month) if month.isdigit() else datetime.strptime(month[:3].title(), "%b").month
                if 1 <= number <= 12: return f"{year}-{number:02d}"
            except ValueError: pass
        return ""
    education = profile.get("education")
    if isinstance(education, list):
        result["education_entries"] = [{"institution": str(x.get("institution") or x.get("university") or x.get("school") or ""),
             "degree": str(x.get("degree") or ""), "major": str(x.get("major") or x.get("field") or ""),
             "graduation_date": date_value(x,"graduation"), "graduation_year_suggestion": str(x.get("graduation_year") or x.get("end_year") or ""),
             "gpa": str(x.get("gpa") or ""), "currently_enrolled": bool(x.get("currently_enrolled") or x.get("current_student"))} for x in education if isinstance(x,dict)]
    work = profile.get("work_experience")
    internships = profile.get("internships") if isinstance(profile.get("internships"), list) else []
    if isinstance(work, list):
        for entry in work:
            if not isinstance(entry, dict): continue
            item={"job_title":str(entry.get("job_title") or entry.get("title") or entry.get("position") or ""),
                  "employer":str(entry.get("employer") or entry.get("company") or entry.get("institution") or ""),
                  "location":str(entry.get("location") or ""),"employment_type":str(entry.get("employment_type") or ""),
                  "start_date":date_value(entry,"start"),"end_date":date_value(entry,"end"),
                  "start_year_suggestion":str(entry.get("start_year") or ""),"end_year_suggestion":str(entry.get("end_year") or ""),
                  "currently_working":bool(entry.get("currently_working") or entry.get("current")),
                  "responsibilities":_text_list(entry.get("responsibilities") or entry.get("description")),
                  "technologies":_text_list(entry.get("technologies") or entry.get("skills"))}
            if re.search(r"intern",(item["job_title"]+" "+item["employment_type"]),re.I): internships.append(item)
            else: result["experience_entries"].append(item)
    result["internship_entries"] = [_normalize_experience(x) if isinstance(x,dict) else _normalize_experience({"responsibilities":[str(x)]}) for x in internships if isinstance(x,dict) or (isinstance(x,str) and x.strip())]
    projects = profile.get("projects")
    if isinstance(projects,list):
        result["project_entries"]=[{"name":str(x.get("name") or x.get("title") or ""),"description":str(x.get("description") or ""),
          "technologies":_text_list(x.get("technologies") or x.get("skills")),"github_url":str(x.get("github_url") or x.get("github") or ""),
          "demo_url":str(x.get("demo_url") or x.get("live_demo_url") or ""),"start_date":date_value(x,"start"),"end_date":date_value(x,"end"),
          "start_year_suggestion":str(x.get("start_year") or ""),"end_year_suggestion":str(x.get("end_year") or ""),
          "accomplishments":_text_list(x.get("accomplishments") or x.get("outcomes"))} if isinstance(x,dict) else {"name":"","description":str(x),"technologies":[],"github_url":"","demo_url":"","start_date":"","end_date":"","accomplishments":[]} for x in projects if isinstance(x,dict) or (isinstance(x,str) and x.strip())]
    education = profile.get("education")
    if isinstance(education, list) and education and isinstance(education[0], dict):
        ed = education[0]
        for key, aliases in {"university": ("institution", "university", "school"),
                             "degree": ("degree",), "major": ("major", "field", "discipline"),
                             "graduation_month": ("graduation_month",),
                             "graduation_year": ("graduation_year", "year")}.items():
            for alias in aliases:
                if ed.get(alias):
                    result[key] = str(ed[alias]); break
    # AI-extracted dates remain suggestions. Accept legacy month/year values,
    # year-month strings, and common month names without asserting a day.
    month = str(result.get("graduation_month") or "").strip()
    year = str(result.get("graduation_year") or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}", month):
        result["graduation_date"] = month
    elif re.fullmatch(r"\d{4}-\d{2}", str(profile.get("graduation_date") or "")):
        result["graduation_date"] = str(profile["graduation_date"])
    elif year and month:
        try:
            month_num = int(month) if month.isdigit() else datetime.strptime(month[:3].title(), "%b").month
            if 1 <= month_num <= 12 and re.fullmatch(r"\d{4}", year):
                result["graduation_date"] = f"{year}-{month_num:02d}"
        except ValueError:
            pass
    if not result["graduation_date"] and year and re.fullmatch(r"\d{4}", year):
        # A year-only resume fact can be shown as a suggestion without
        # inventing a month; the month picker itself remains blank.
        result["graduation_date_suggestion"] = year
    return result


def _text_list(value):
    if value in (None, ""): return []
    if isinstance(value,list): return [str(x) for x in value if str(x).strip()]
    return [str(value)]


def _normalize_experience(entry):
    entry = entry if isinstance(entry,dict) else {}
    return {"job_title":str(entry.get("job_title") or entry.get("title") or entry.get("position") or ""),
            "employer":str(entry.get("employer") or entry.get("company") or entry.get("institution") or ""),
            "location":str(entry.get("location") or ""),"employment_type":str(entry.get("employment_type") or ""),
            "start_date":str(entry.get("start_date") or ""),"end_date":str(entry.get("end_date") or ""),
            "start_year_suggestion":str(entry.get("start_year_suggestion") or ""),"end_year_suggestion":str(entry.get("end_year_suggestion") or ""),
            "currently_working":bool(entry.get("currently_working") or entry.get("current")),
            "responsibilities":_text_list(entry.get("responsibilities") or entry.get("description")),
            "technologies":_text_list(entry.get("technologies") or entry.get("skills"))}


def verified_profile_facts(values, confirmed):
    """Return only fields/entries explicitly confirmed for the active resume."""
    facts={}
    structured={"education_entries":"education","experience_entries":"work_experience",
                "internship_entries":"internships","project_entries":"projects",
                "country_work_authorizations":"work_authorization_by_country",
                "sponsorship_by_country":"sponsorship_needs_by_country"}
    for key,value in values.items():
        if key in structured and isinstance(value,list):
            rows=[item for i,item in enumerate(value) if isinstance(item,dict) and confirmed.get(f"{key}:{i}")]
            if rows: facts[structured[key]]=rows; facts[key]=rows
        elif key=="skills" and isinstance(value,list):
            verified=bool(confirmed.get("skills"))
            if verified: facts["technical_skills"]=list(dict.fromkeys(str(x) for x in value if str(x).strip()))
        elif confirmed.get(key) and value not in (None,"",[],{}): facts[key]=value
    # Upgrade older field-level confirmations into normalized fact shapes.
    if "education" not in facts and (confirmed.get("university") or confirmed.get("degree")):
        facts["education"]=[{"institution":facts.get("university",""),"degree":facts.get("degree",""),"major":facts.get("major",""),
                             "graduation_date":facts.get("graduation_date"),"graduation_year":facts.get("graduation_year")}]
    if facts.get("education"):
        first=facts["education"][0]
        for output,source in (("university","institution"),("degree","degree"),("major","major"),("gpa","gpa")):
            if first.get(source): facts[output]=first[source]
        if first.get("graduation_date"):
            facts["graduation_date"]=first["graduation_date"]
            facts["graduation_year"]=first["graduation_date"][:4]
            facts["graduation_month"]=first["graduation_date"][5:7]
        elif first.get("graduation_year_suggestion") and first.get("graduation_year_suggestion").isdigit():
            # Do not promote a year-only suggestion into a confirmed graduation date.
            pass
    if "work_experience" not in facts and confirmed.get("work_experience"):
        facts["work_experience"]=values.get("work_experience",[])
    if "projects" not in facts and confirmed.get("projects"): facts["projects"]=values.get("projects",[])
    if facts.get("skills"): facts["technical_skills"]=facts["skills"]
    if facts.get("location"): facts["current_location"]=facts["location"]
    if confirmed.get("graduation_date"):
        date=values.get("graduation_date")
        if isinstance(date,str) and re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])",date):
            facts["graduation_date"]=date; facts["graduation_year"]=date[:4]; facts["graduation_month"]=date[5:7]
    return facts


_MATCHING_PLACEHOLDER_FACTS={"required","preferred","skills","skill","experience","qualifications",
                             "qualification","education","unknown","not provided","not specified"}


def normalize_degree(value):
    """Normalize common degree abbreviations while preserving unknown values."""
    text=re.sub(r"\s+"," ",str(value or "").casefold().replace("’","'")).strip()
    compact=re.sub(r"[^a-z0-9]","",text)
    if re.search(r"\b(ph\.?\s*d\.?|doctorate|doctoral)\b",text) or compact in {"phd","doctorate"}:
        return "doctorate"
    if re.search(r"\b(master'?s?|m\.?s\.?|m\.?a\.?|mba)\b",text) or compact in {"ms","ma","mba"}:
        return "master"
    if re.search(r"\b(bachelor'?s?|b\.?s\.?|b\.?a\.?|b\.?eng\.?|b\.?sc\.?)\b",text) or compact in {"bs","ba","beng","bsc"}:
        return "bachelor"
    if re.search(r"\b(associate'?s?|a\.?s\.?|a\.?a\.?)\b",text) or compact in {"as","aa"}:
        return "associate"
    return text


def normalize_major(value):
    text=re.sub(r"[^a-z0-9]+"," ",str(value or "").casefold()).strip()
    aliases={"cs":"computer science","comp sci":"computer science","computer sciences":"computer science",
             "computing science":"computer science","software engineering":"software engineering"}
    return aliases.get(text,text)


def _verified_month(value):
    text=str(value or "").strip()
    if re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])",text):
        return int(text[:4])*12+int(text[5:7])-1
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}",text):
        return int(text[:4])*12+int(text[5:7])-1
    return None


def _months_covered(entries, *, software_only=False):
    months=set()
    current=datetime.now(timezone.utc)
    for entry in entries:
        title=str(entry.get("job_title") or "").casefold()
        if software_only and not re.search(r"\b(software|developer|programmer|sdet|devops|site reliability|qa automation|firmware|embedded engineer)\b",title):
            continue
        start=_verified_month(entry.get("start_date"))
        end=_verified_month(entry.get("end_date"))
        if entry.get("currently_working") and end is None:
            end=current.year*12+current.month-1
        if start is None or end is None or end<start:
            continue
        months.update(range(start,end+1))
    return len(months)


def canonical_verified_candidate_facts(profile):
    """Create the minimal, normalized fact view used for eligibility matching.

    Caller must provide a profile already marked user-reviewed or assembled
    from field-level confirmations. Resume suggestions, partial year guesses,
    contact details, employer names, and sensitive disclosures are excluded.
    """
    source=profile if isinstance(profile,dict) else {}
    facts={}
    for key in ("country","state_province","location","current_location","work_authorization",
                "work_authorization_by_country","sponsorship_needs_by_country","sponsorship_required_now",
                "sponsorship_required_future","availability","relocation_willingness"):
        value=source.get(key)
        if value not in (None,"",[],{}):facts[key]=value
    for key in ("graduation_date","graduation_year","graduation_month"):
        value=str(source.get(key) or "").strip()
        if key=="graduation_date" and re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])",value):facts[key]=value
        elif key=="graduation_year" and re.fullmatch(r"20\d{2}",value):facts[key]=value
        elif key=="graduation_month":
            month=value.casefold()
            if re.fullmatch(r"(?:0[1-9]|1[0-2])",month):facts[key]=month
            else:
                try:facts[key]=f"{datetime.strptime(month[:3].title(),'%b').month:02d}"
                except ValueError:pass
    if not facts.get("graduation_date") and facts.get("graduation_year") and facts.get("graduation_month"):
        facts["graduation_date"]=f"{facts['graduation_year']}-{facts['graduation_month']}"

    raw_skills=[]
    for key in ("technical_skills","professional_skills","skills"):
        value=source.get(key)
        if isinstance(value,list):raw_skills.extend(value)
        elif isinstance(value,str):raw_skills.extend(re.split(r"[,;\n]",value))
    skills=[]
    seen_skills=set()
    for value in raw_skills:
        item=re.sub(r"\s+"," ",str(value or "")).strip()
        if item and item.casefold() not in _MATCHING_PLACEHOLDER_FACTS and item.casefold() not in seen_skills:
            skills.append(item);seen_skills.add(item.casefold())
    if skills:facts["technical_skills"]=skills

    raw_education=source.get("education") or source.get("education_entries") or []
    if isinstance(raw_education,dict):raw_education=[raw_education]
    education=[]
    for row in raw_education if isinstance(raw_education,list) else []:
        if not isinstance(row,dict):continue
        degree=str(row.get("degree") or "").strip()
        major=str(row.get("major") or row.get("field") or "").strip()
        raw_date=str(row.get("graduation_date") or "").strip()
        date=raw_date if re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])",raw_date) else ""
        # A year-only extracted suggestion is not a confirmed graduation date.
        if degree or major or date:
            education.append({"degree":degree,"degree_normalized":normalize_degree(degree),
                "major":major,"major_normalized":normalize_major(major),"graduation_date":date,
                "currently_enrolled":(bool(row["currently_enrolled"]) if row.get("currently_enrolled") is not None else None)})
    if education:facts["education"]=education

    raw_experience=source.get("work_experience") or source.get("experience_entries") or []
    if isinstance(raw_experience,str):raw_experience=[]
    professional=[];teaching=[];internships=[]
    all_experience=list(raw_experience) if isinstance(raw_experience,list) else []
    if isinstance(source.get("internships"),list):all_experience.extend(source["internships"])
    for row in all_experience:
        if not isinstance(row,dict):continue
        title=str(row.get("job_title") or row.get("title") or row.get("position") or "").strip()
        employment_type=str(row.get("employment_type") or row.get("category") or "").strip()
        normalized={"job_title":title,"start_date":str(row.get("start_date") or "").strip(),
            "end_date":str(row.get("end_date") or "").strip(),
            "currently_working":bool(row.get("currently_working") or row.get("current")),
            "responsibilities":[str(x) for x in row.get("responsibilities",[]) if str(x).strip()] if isinstance(row.get("responsibilities"),list) else [],
            "technologies":[str(x) for x in row.get("technologies",[]) if str(x).strip()] if isinstance(row.get("technologies"),list) else []}
        if re.search(r"\b(intern|internship|co[- ]?op)\b",title+" "+employment_type,re.I):internships.append(normalized)
        elif re.search(r"\b(tutor|teaching assistant|student assistant|teacher|lecturer|course assistant)\b",title,re.I):teaching.append(normalized)
        elif re.search(r"\b(coursework|course project|academic project|capstone|personal project|class project)\b",title+" "+employment_type,re.I):continue
        elif title:professional.append(normalized)
    if professional:facts["work_experience"]=professional
    if teaching:facts["teaching_experience"]=teaching
    if internships:facts["internships"]=internships
    projects=source.get("projects") or source.get("project_entries") or []
    if isinstance(projects,list):
        normalized_projects=[]
        for row in projects:
            if isinstance(row,dict):
                technologies=row.get("technologies") or row.get("skills") or []
                normalized_projects.append({"name":str(row.get("name") or row.get("title") or ""),
                    "description":str(row.get("description") or ""),
                    "technologies":[str(x) for x in technologies if str(x).strip()] if isinstance(technologies,list) else []})
        if normalized_projects:facts["projects"]=normalized_projects
    if facts.get("work_experience"):
        facts["dated_professional_experience_months"]=_months_covered(facts["work_experience"])
        facts["dated_software_experience_months"]=_months_covered(facts["work_experience"],software_only=True)
    if facts.get("teaching_experience"):
        facts["dated_teaching_experience_months"]=_months_covered(facts["teaching_experience"])
    if "location" in facts or "current_location" in facts:
        facts["location_evidence"]=str(facts.get("location") or facts.get("current_location") or "")
    return facts


def _migrate_legacy_values(values,confirmed,saved_keys):
    """Project confirmed legacy fields into structured cards without losing suggestions."""
    if "education_entries" not in saved_keys:
        old={key:values.get(key,"") if confirmed.get(key) else "" for key in ("university","degree","major","graduation_date","graduation_year","graduation_month")}
        if any(old.values()):
            date=old["graduation_date"]
            if not date and old["graduation_year"] and old["graduation_month"]:
                month=old["graduation_month"]
                try: month_num=int(month) if month.isdigit() else datetime.strptime(month[:3].title(),"%b").month
                except ValueError: month_num=0
                if month_num: date=f"{old['graduation_year']}-{month_num:02d}"
            migrated={"institution":old["university"],"degree":old["degree"],"major":old["major"],"graduation_date":date,"gpa":"","currently_enrolled":False}
            current=values.get("education_entries",[])
            values["education_entries"]=[migrated]+[x for x in current if isinstance(x,dict) and x.get("institution")!=migrated["institution"]]
            confirmed["education_entries:0"]=bool(confirmed.get("university") or confirmed.get("degree"))
    for legacy,structured in (("work_experience","experience_entries"),("internships","internship_entries"),("projects","project_entries")):
        if structured in saved_keys or not confirmed.get(legacy): continue
        raw=values.get(legacy,[]); parsed=[]
        for item in raw if isinstance(raw,list) else []:
            if isinstance(item,dict): parsed.append(item); continue
            try:
                candidate=json.loads(item)
                if isinstance(candidate,dict): parsed.append(candidate); continue
            except (TypeError,json.JSONDecodeError): pass
            if isinstance(item,str) and item.strip():
                if structured=="experience_entries": parsed.append({"job_title":"","employer":"","responsibilities":[item]})
                elif structured=="project_entries": parsed.append({"name":"","description":item})
                else: parsed.append({"job_title":"","employer":"","responsibilities":[item]})
        current=values.get(structured,[])
        values[structured]=parsed+[x for x in current if x not in parsed]
        for i,_ in enumerate(parsed): confirmed[f"{structured}:{i}"]=True
    if "skills" not in saved_keys and confirmed.get("technical_skills"):
        old=values.get("technical_skills",[])
        if isinstance(old,list): values["skills"]=list(dict.fromkeys([*old,*values.get("skills",[])])); confirmed["skills"]=True
    return values,confirmed


@router.get("/api/candidate-profile/verification")
def get_verification():
    with connect() as db:
        resume = _active(db)
        row = db.execute("SELECT values_json,confirmed_json,updated_at FROM candidate_verifications WHERE resume_id=?", (resume["id"],)).fetchone()
        profile = json.loads(resume["profile_json"] or "{}")
        values = _suggestions(profile)
        suggestion_keys={key for key,value in values.items() if _has_profile_value(value)}
        confirmed = {key: False for key in FIELDS}
        saved_keys=set()
        if row:
            try:
                saved_values=json.loads(row["values_json"]); saved_flags=json.loads(row["confirmed_json"])
                plain_values,encoded_values,migrated=_decode_profile_values(saved_values)
                if migrated:
                    db.execute("UPDATE candidate_verifications SET values_json=? WHERE resume_id=? AND user_id=?",
                               (json.dumps(encoded_values,ensure_ascii=False),resume["id"],USER))
                saved_keys=set(saved_values); values.update(plain_values)
                confirmed.update(saved_flags)
            except (TypeError, json.JSONDecodeError):
                raise HTTPException(500, "Saved verification data is invalid.")
            except EncryptionUnavailable as error:
                raise HTTPException(503,str(error)) from error
        # Migrate old separately-confirmed month/year values for display while
        # preserving their original rows and verification flags.
        if not values.get("graduation_date"):
            month, year = str(values.get("graduation_month") or "").strip(), str(values.get("graduation_year") or "").strip()
            if re.fullmatch(r"\d{4}-\d{2}", month):
                values["graduation_date"] = month
            elif year and month:
                try:
                    number = int(month) if month.isdigit() else datetime.strptime(month[:3].title(), "%b").month
                    if re.fullmatch(r"\d{4}", year) and 1 <= number <= 12:
                        values["graduation_date"] = f"{year}-{number:02d}"
                except ValueError:
                    pass
            if not confirmed.get("graduation_date"):
                confirmed["graduation_date"] = bool(confirmed.get("graduation_month") and confirmed.get("graduation_year") and values.get("graduation_date"))
        values,confirmed=_migrate_legacy_values(values,confirmed,saved_keys)
        # Older editor versions could persist a confirmation with an empty
        # value. Reconcile those flags on read so saved facts and their
        # reusable-answer permissions can never outlive the value they attest.
        if row:
            corrected = dict(confirmed)
            for key, (_, _, kind, _) in FIELDS.items():
                saved_value = values.get(key) if key in {"graduation_date", "university", "degree", "major"} else plain_values.get(key)
                if kind == "objects":
                    entries = saved_value if isinstance(saved_value, list) else []
                    entry_flags = []
                    for index, entry in enumerate(entries):
                        token = f"{key}:{index}"
                        valid = bool(corrected.get(token)) and _entry_is_confirmable(key, entry)
                        corrected[token] = valid
                        entry_flags.append(valid)
                    corrected[key] = bool(entries) and all(entry_flags)
                elif corrected.get(key):
                    corrected[key] = _profile_value_confirmable(key, saved_value)
            # Keep compatibility aliases aligned with the canonical month field.
            if "graduation_date" in plain_values or values.get("graduation_date"):
                corrected["graduation_month"] = corrected["graduation_year"] = bool(
                    corrected.get("graduation_date") and values.get("graduation_date"))
            # A field can also have an older reusable-answer row even if its
            # profile flag was already false. Revoke that reuse permission
            # whenever the current profile no longer attests a value.
            stale_keys = [key for key, (_, _, kind, _) in FIELDS.items()
                          if kind != "objects" and not corrected.get(key)]
            if corrected != confirmed:
                db.execute("UPDATE candidate_verifications SET confirmed_json=? WHERE resume_id=? AND user_id=?",
                           (json.dumps(corrected), resume["id"], USER))
            if stale_keys:
                stamp = now()
                placeholders = ",".join("?" for _ in stale_keys)
                db.execute(f"UPDATE application_answers SET confirmed=0,revoked_at=?,updated_at=?,auto_reuse=0 WHERE user_id=? AND resume_id=? AND revoked_at IS NULL AND question_key IN ({placeholders})",
                           (stamp, stamp, USER, resume["id"], *stale_keys))
            confirmed = corrected
        return {
            "resume_name": resume["original_name"],
            "resume_status": resume["status"] or "not_started",
            "fields": [{"key": key, "section": section, "label": label, "kind": kind,
                        "sensitive": sensitive, "value": values.get(key, ""),
                        "confirmed": bool(confirmed.get(key, False)),
                        "source": "user_confirmed" if confirmed.get(key) or (kind=="objects" and any(confirmed.get(f"{key}:{i}") for i,_ in enumerate(values.get(key,[])))) else ("user_provided" if key in saved_keys and _has_profile_value(values.get(key)) else ("resume_suggestion" if key in suggestion_keys or (key == "graduation_date" and values.get("graduation_date_suggestion")) else "missing")),
                        "suggestion": values.get("graduation_date_suggestion", "") if key == "graduation_date" else "",
                        "entry_confirmed": [bool(confirmed.get(f"{key}:{i}")) for i,_ in enumerate(values.get(key,[]))] if kind=="objects" else []}
                       for key, (section, label, kind, sensitive) in FIELDS.items()],
            "updated_at": row["updated_at"] if row else None,
        }


class VerificationUpdate(BaseModel):
    values: dict[str, Any]
    confirmed_fields: list[str] = Field(default_factory=list)


class AnswerUpdate(BaseModel):
    question: str = Field(min_length=3, max_length=300)
    answer: str = Field(max_length=4000)
    sensitive: bool = False
    confirmed: bool = False
    answer_type: str = "factual"
    jurisdiction: str = Field(default="",max_length=120)
    auto_reuse: bool = False


def _validate_profile_url(value,label):
    parsed=urlparse(value)
    if parsed.scheme!="https" or not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(400,f"{label} must be a public HTTPS URL.")
    host=parsed.hostname.casefold().rstrip(".")
    try:
        _ = parsed.port
        host_ascii=host.encode("idna").decode("ascii")
    except (UnicodeError,ValueError):
        raise HTTPException(400,f"{label} must be a valid public HTTPS URL.")
    if host in {"localhost","metadata.google.internal"} or host.endswith((".local",".localhost",".internal")):
        raise HTTPException(400,f"{label} cannot point to a local or private destination.")
    try:
        if not ipaddress.ip_address(host_ascii).is_global:
            raise HTTPException(400,f"{label} cannot point to a local or private destination.")
    except ValueError:
        if not re.fullmatch(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))+",host_ascii):
            raise HTTPException(400,f"{label} must be a valid public HTTPS URL.")


def _has_profile_value(value):
    """Return whether a saved profile value contains user-visible information."""
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set)):
        return any(_has_profile_value(item) for item in value)
    if isinstance(value, dict):
        return any(_has_profile_value(part) for key, part in value.items()
                   if key not in {"currently_enrolled", "currently_working"})
    return value is not None and value is not False


def _profile_value_confirmable(key, value):
    """A confirmation must describe a present, valid value, never a blank field."""
    if not _has_profile_value(value):
        return False
    if key == "email":
        return bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", str(value).strip()))
    if key in {"linkedin", "github", "portfolio", "website"}:
        try:
            _validate_profile_url(str(value).strip(), FIELDS[key][1])
        except HTTPException:
            return False
    if key == "other_professional_links":
        try:
            for link in value:
                _validate_profile_url(str(link).strip(), FIELDS[key][1])
        except HTTPException:
            return False
    if key in {"work_authorization", "requires_sponsorship"} and str(value).strip().casefold() in {"unknown", "not sure", "unsure"}:
        return False
    return True


def _entry_is_confirmable(key, entry):
    """Require actual evidence in a structured entry; booleans alone are not facts."""
    if not isinstance(entry, dict):
        return False
    values = [part for name, part in entry.items()
              if name not in {"currently_enrolled", "currently_working", "graduation_year_suggestion", "start_year_suggestion", "end_year_suggestion"}]
    if key == "country_work_authorizations":
        country = str(entry.get("country", "")).strip()
        status = str(entry.get("status", "")).strip()
        return bool(country and status and status.casefold() not in {"unknown", "not sure", "unsure"})
    if key == "sponsorship_by_country":
        country = str(entry.get("country", "")).strip()
        answer = str(entry.get("requires_sponsorship", "")).strip()
        return bool(country and answer and answer.casefold() not in {"unknown", "not sure", "unsure"})
    return any(_has_profile_value(part) for part in values)


@router.put("/api/application-answers/{question_key}")
def save_application_answer(question_key: str, payload: AnswerUpdate):
    import re
    if not re.fullmatch(r"[a-zA-Z0-9_-]{2,80}", question_key):
        raise HTTPException(400, "Answer key must contain letters, digits, hyphens, or underscores.")
    if payload.answer_type not in {"factual","preference","generated","sensitive","source"}:
        raise HTTPException(400,"Unknown answer type.")
    if payload.sensitive and payload.answer_type not in {"sensitive","factual"}:
        raise HTTPException(400,"Sensitive answers must use the sensitive or factual answer type.")
    stamp = now()
    with connect() as db:
        resume = _active(db)
        is_sensitive=bool(payload.sensitive)
        answer=payload.answer.strip()
        try:cipher=encrypt_text(answer) if is_sensitive and answer else ""
        except EncryptionUnavailable as error:raise HTTPException(503,str(error)) from error
        db.execute("""INSERT INTO application_answers
            (user_id,resume_id,question_key,question_text,answer,sensitive,confirmed,created_at,updated_at,revoked_at,answer_type,jurisdiction,auto_reuse,source,answer_encrypted,confirmation_at,sensitivity_classification)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,resume_id,question_key) DO UPDATE SET
            question_text=excluded.question_text,answer=excluded.answer,sensitive=excluded.sensitive,
            confirmed=excluded.confirmed,updated_at=excluded.updated_at,revoked_at=excluded.revoked_at,
            answer_type=excluded.answer_type,jurisdiction=excluded.jurisdiction,auto_reuse=excluded.auto_reuse,source=excluded.source,
            answer_encrypted=excluded.answer_encrypted,confirmation_at=excluded.confirmation_at,
            sensitivity_classification=excluded.sensitivity_classification""",
                   (USER, resume["id"], "custom:" + question_key, payload.question.strip(), "" if is_sensitive else answer,
                    int(is_sensitive), int(payload.confirmed and bool(answer)), stamp, stamp,
                    None if payload.confirmed and answer else stamp,payload.answer_type,payload.jurisdiction.strip(),
                    int(payload.auto_reuse and payload.confirmed and bool(answer) and not is_sensitive),"user_confirmed",
                    cipher,stamp if payload.confirmed and answer else None,"sensitive" if is_sensitive else "ordinary"))
        if question_key in {"graduation_date","technical_skills","professional_experience","location","availability","employment_type"}:
            db.execute("UPDATE job_match_results SET freshness_status='stale' WHERE user_id=? AND resume_id=? AND status='completed'",(USER,resume["id"]))
    with connect() as db:
        persisted=db.execute("""SELECT question_text,answer,answer_encrypted,sensitive,confirmed,auto_reuse,revoked_at
            FROM application_answers WHERE user_id=? AND resume_id=? AND question_key=?""",
            (USER,resume["id"],"custom:"+question_key)).fetchone()
        persisted_answer=_answer_plaintext(persisted,db) if persisted else ""
        persisted_verified=bool(persisted and persisted["question_text"]==payload.question.strip()
            and persisted_answer==answer and bool(persisted["sensitive"])==is_sensitive
            and bool(persisted["confirmed"])==bool(payload.confirmed and answer)
            and bool(persisted["auto_reuse"])==bool(payload.auto_reuse and payload.confirmed and answer and not is_sensitive)
            and persisted["revoked_at"] is None)
    return {"saved": True, "persisted_verified":persisted_verified,
            "confirmed": bool(payload.confirmed and payload.answer.strip()),
            "auto_reuse":bool(payload.auto_reuse and payload.confirmed and payload.answer.strip() and not payload.sensitive)}


@router.delete("/api/application-answers/{question_key}")
def revoke_application_answer(question_key: str):
    stamp = now()
    with connect() as db:
        resume = _active(db)
        db.execute("UPDATE application_answers SET confirmed=0,revoked_at=?,updated_at=? WHERE user_id=? AND resume_id=? AND question_key=?",
                   (stamp, stamp, USER, resume["id"], "custom:" + question_key))
    return {"revoked": True}


@router.put("/api/candidate-profile/verification")
def update_verification(payload: VerificationUpdate):
    entry_tokens={token for token in payload.confirmed_fields if re.fullmatch(
        r"(?:education_entries|experience_entries|internship_entries|project_entries|country_work_authorizations|sponsorship_by_country):\d+",token)}
    if set(payload.values) - set(FIELDS) or (set(payload.confirmed_fields) - set(FIELDS) - entry_tokens):
        raise HTTPException(400, "Unknown candidate profile field.")
    values = {}
    for key, value in payload.values.items():
        kind = FIELDS[key][2]
        if kind == "objects":
            if not isinstance(value,list) or len(value)>100 or any(not isinstance(item,dict) for item in value):
                raise HTTPException(400,f"{key} must be a list of structured entries.")
            allowed={"education_entries":{"institution","degree","major","graduation_date","graduation_year_suggestion","gpa","currently_enrolled"},
                     "experience_entries":{"job_title","employer","location","employment_type","start_date","end_date","start_year_suggestion","end_year_suggestion","currently_working","responsibilities","technologies"},
                     "internship_entries":{"job_title","employer","location","employment_type","start_date","end_date","start_year_suggestion","end_year_suggestion","currently_working","responsibilities","technologies"},
                     "project_entries":{"name","description","technologies","github_url","demo_url","start_date","end_date","start_year_suggestion","end_year_suggestion","accomplishments"},
                     "country_work_authorizations":{"country","status"},"sponsorship_by_country":{"country","requires_sponsorship"}}[key]
            clean=[]
            for index,item in enumerate(value):
                if set(item)-allowed: raise HTTPException(400,f"{key} entry {index+1} contains an unknown field.")
                entry={}
                for field,part in item.items():
                    if field in {"currently_enrolled","currently_working"}:
                        if not isinstance(part,bool): raise HTTPException(400,f"{field} must be true or false.")
                        entry[field]=part
                    elif field in {"responsibilities","technologies","accomplishments"}:
                        if not isinstance(part,list) or any(not isinstance(x,str) or len(x)>2000 for x in part): raise HTTPException(400,f"{field} must be a list of text values.")
                        entry[field]=[x.strip() for x in part if x.strip()]
                    else:
                        if not isinstance(part,str) or len(part)>4000: raise HTTPException(400,f"{field} must be text up to 4000 characters.")
                        entry[field]=part.strip()
                for field in ("graduation_date","start_date","end_date"):
                    date=entry.get(field,"")
                    if date and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])",date): raise HTTPException(400,f"{field} must use YYYY-MM.")
                if entry.get("start_date") and entry.get("end_date") and entry["end_date"]<entry["start_date"]: raise HTTPException(400,"An end date cannot occur before its start date.")
                if entry.get("currently_working") and entry.get("end_date"): raise HTTPException(400,"Clear the end date for a current position.")
                clean.append(entry)
            values[key]=clean
        elif kind == "chips":
            if not isinstance(value,list) or any(not isinstance(item,str) or len(item)>200 for item in value): raise HTTPException(400,f"{key} must be a list of skill names.")
            normalized=set(); clean_skills=[]
            for item in value:
                skill=" ".join(item.split())
                token=skill.casefold()
                if skill and token not in normalized:
                    normalized.add(token); clean_skills.append(skill)
            values[key]=clean_skills
        if kind == "date":
            if not isinstance(value,str) or (value and not re.fullmatch(r"\d{4}-\d{2}-\d{2}",value)):
                raise HTTPException(400,f"{key} must use YYYY-MM-DD format.")
            values[key]=value
        if kind == "list":
            if not isinstance(value, list) or any(not isinstance(item, str) or len(item) > 1000 for item in value):
                raise HTTPException(400, f"{key} must be a list of text values.")
            values[key] = [item.strip() for item in value if item.strip()]
        elif kind not in {"objects","chips","date"}:
            if not isinstance(value, str) or len(value) > 4000:
                raise HTTPException(400, f"{key} must be text up to 4000 characters.")
            values[key] = value.strip()
        if key in {"linkedin","github","portfolio","website"} and values.get(key):
            _validate_profile_url(values[key],FIELDS[key][1])
        if key == "email" and values.get(key) and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", values[key]):
            raise HTTPException(400,"Email address must use a valid format, such as name@example.com.")
        if key in {"other_professional_links"}:
            for link in values.get(key,[]):_validate_profile_url(link,FIELDS[key][1])
    for token in entry_tokens:
        key,index=token.rsplit(":",1)
        if key not in values or FIELDS[key][2]!="objects" or int(index)>=len(values[key]):
            raise HTTPException(400,"A confirmed profile entry does not exist.")
    # The month control is the canonical value. Keep legacy split keys
    # populated for matching/application-answer consumers and old clients.
    date_value = values.get("graduation_date", "")
    if date_value and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", date_value):
        raise HTTPException(400, "Graduation date must use YYYY-MM (for example, 2026-12).")
    if date_value:
        values["graduation_year"] = date_value[:4]
        values["graduation_month"] = date_value[5:7]
    elif "graduation_date" in values:
        values["graduation_year"] = ""
        values["graduation_month"] = ""
    confirmed = {key: key in payload.confirmed_fields for key in FIELDS}
    confirmed.update({token: _entry_is_confirmable(token.rsplit(":",1)[0], values[token.rsplit(":",1)[0]][int(token.rsplit(":",1)[1])]) for token in entry_tokens})
    for key,(_,_,kind,_) in FIELDS.items():
        if kind=="objects": confirmed[key]=bool(values.get(key)) and all(confirmed.get(f"{key}:{i}",False) for i in range(len(values.get(key,[]))))
        elif kind=="chips": confirmed[key]=key in payload.confirmed_fields and bool(values.get(key))
    if "graduation_date" in payload.values:
        confirmed["graduation_year"] = confirmed["graduation_month"] = "graduation_date" in payload.confirmed_fields and bool(date_value)
    # Empty values cannot be confirmed. Sensitive questions require explicit confirmation.
    for key in payload.confirmed_fields:
        if key in FIELDS and not _profile_value_confirmable(key, values.get(key)):
            confirmed[key] = False
    stamp = now()
    try:encoded_profile=json.dumps(_encode_profile_values(values),ensure_ascii=False)
    except EncryptionUnavailable as error:raise HTTPException(503,str(error)) from error
    with connect() as db:
        resume = _active(db)
        db.execute("""INSERT INTO candidate_verifications(resume_id,user_id,values_json,confirmed_json,updated_at)
            VALUES(?,?,?,?,?) ON CONFLICT(resume_id) DO UPDATE SET
            values_json=excluded.values_json,confirmed_json=excluded.confirmed_json,updated_at=excluded.updated_at""",
                   (resume["id"], USER, encoded_profile, json.dumps(confirmed), stamp))
        match_table=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_match_results'").fetchone()
        if match_table:
            match_columns={row[1] for row in db.execute("PRAGMA table_info(job_match_results)")}
            if "freshness_status" in match_columns:
                db.execute("UPDATE job_match_results SET freshness_status='stale' WHERE user_id=? AND resume_id=? AND status='completed'",
                           (USER,resume["id"]))
        # Keep a reusable answer ledger. Changing or unchecking a field revokes its old answer.
        for key, (_, _, kind, sensitive) in FIELDS.items():
            if kind=="objects": continue
            value = values.get(key)
            answer = "\n".join(value) if isinstance(value, list) else str(value or "")
            if confirmed[key] and answer:
                try:cipher=encrypt_text(answer) if sensitive else ""
                except EncryptionUnavailable as error:raise HTTPException(503,str(error)) from error
                category="work_authorization" if key in {"country_work_authorizations","work_authorization"} else "sponsorship" if key in {"sponsorship_by_country","requires_sponsorship"} else "profile_fact"
                db.execute("""INSERT INTO application_answers(user_id,resume_id,question_key,answer,sensitive,confirmed,created_at,updated_at,revoked_at,answer_type,auto_reuse,source,answer_encrypted,confirmation_at,question_category,sensitivity_classification)
                    VALUES(?,?,?,?,?,1,?,?,NULL,?,?,?,?,?,?,?) ON CONFLICT(user_id,resume_id,question_key) DO UPDATE SET
                    answer=excluded.answer,sensitive=excluded.sensitive,confirmed=1,updated_at=excluded.updated_at,revoked_at=NULL,
                    answer_type=excluded.answer_type,auto_reuse=excluded.auto_reuse,source=excluded.source,
                    answer_encrypted=excluded.answer_encrypted,confirmation_at=excluded.confirmation_at,
                    question_category=excluded.question_category,sensitivity_classification=excluded.sensitivity_classification""",
                           (USER, resume["id"], key, "" if sensitive else answer, int(sensitive), stamp, stamp,
                            "sensitive" if sensitive else "factual",int(not sensitive),"user_confirmed_profile",cipher,stamp,
                            category,"sensitive" if sensitive else "ordinary"))
            else:
                db.execute("UPDATE application_answers SET confirmed=0,revoked_at=?,updated_at=?,answer=CASE WHEN sensitive=1 THEN '' ELSE answer END,answer_encrypted=CASE WHEN sensitive=1 THEN '' ELSE answer_encrypted END,auto_reuse=0 WHERE user_id=? AND resume_id=? AND question_key=?",
                           (stamp, stamp, USER, resume["id"], key))
    return {"saved": True, "confirmed_fields": sum(confirmed.values()), "resume_name": resume["original_name"]}


@router.get("/api/candidate-profile/completeness")
def profile_completeness():
    data=get_verification(); fields={item["key"]:item for item in data["fields"]}
    required={"full_name":"Full name","email":"Email address"}
    confirmed=[]; missing=[]; review=[]; optional=[]
    ignored={"graduation_month","graduation_year","work_experience","internships","projects","technical_skills"}
    for key,item in fields.items():
        if key in ignored: continue
        value=item.get("value")
        present=bool(value) if isinstance(value,(list,dict,str)) else value is not None
        if item.get("confirmed") and present:
            confirmed.append(item["label"])
        elif key in required and not present:
            missing.append({"key":key,"label":required[key],"requiredness":"required","reason":"Needed for baseline application preparation."})
        elif key in required and not item.get("confirmed"):
            review.append({"key":key,"label":required[key],"requiredness":"required","reason":"Confirm this value before basic application preparation."})
        elif item.get("kind")=="objects":
            # Report each unconfirmed card below, rather than double-counting
            # both the collection and its individual entries.
            pass
        elif present or item.get("suggestion"):
            review.append({"key":key,"label":item["label"],"requiredness":"optional","reason":"Provided information has not been explicitly confirmed."})
        elif item.get("sensitive"):
            optional.append({"key":key,"label":item["label"],"requiredness":"optional","reason":"Sensitive disclosure is optional; confirm only if you choose to provide it."})
        else:
            optional.append({"key":key,"label":item["label"],"requiredness":"optional","reason":"Optional profile information."})
        if item.get("kind")=="objects" and isinstance(value,list):
            for index,entry_ok in enumerate(item.get("entry_confirmed",[])):
                if not entry_ok:
                    review.append({"key":f"{key}:{index}","label":f"{item['label']} entry {index+1}","requiredness":"optional","reason":"Review and confirm this entry before using it as candidate evidence."})
    with connect() as db:
        active=_active(db)
        resume_ok=(Path(__file__).resolve().parent/"private_resumes"/str(active["stored_name"])).is_file()
        job_specific=[]
        tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if {"job_match_results","discovered_openings"}.issubset(tables):
            match_cols={row[1] for row in db.execute("PRAGMA table_info(job_match_results)")}
            job_cols={row[1] for row in db.execute("PRAGMA table_info(discovered_openings)")}
            if {"result_json","job_key","user_id","status","updated_at"}.issubset(match_cols) and {"job_key","title","company"}.issubset(job_cols):
                freshness_filter=" AND m.freshness_status='current'" if "freshness_status" in match_cols else ""
                rows=db.execute("""SELECT DISTINCT m.job_key,m.result_json,j.title,j.company FROM job_match_results m
                    LEFT JOIN discovered_openings j ON j.job_key=m.job_key
                    WHERE m.user_id=? AND m.status='completed'"""+freshness_filter+" ORDER BY m.updated_at DESC LIMIT 200",(USER,)).fetchall()
                for row in rows:
                    try: result=json.loads(row["result_json"] or "{}")
                    except (TypeError,json.JSONDecodeError): continue
                    reasons=result.get("missing_information") or result.get("reasons_for_review") or []
                    if isinstance(reasons,str): reasons=[reasons]
                    for reason in reasons:
                        text=str(reason).strip()
                        if not text: continue
                        lowered=text.casefold()
                        key=("country_work_authorizations" if "authoriz" in lowered or "sponsor" in lowered else
                             "education_entries" if "graduat" in lowered or "degree" in lowered else
                             "experience_entries" if "experience" in lowered or "years" in lowered else
                             "availability" if "start date" in lowered or "availability" in lowered else
                             "skills" if "skill" in lowered or "qualification" in lowered else "")
                        job_specific.append({"job_key":row["job_key"],"company":row["company"] or "Employer","title":row["title"] or "Saved job","reason":text,"key":key,"requiredness":"job-specific"})
    for key,label in required.items():
        item=fields.get(key,{})
        if item.get("confirmed") and item.get("value") and label not in confirmed: confirmed.append(label)
    if not resume_ok: missing.append({"key":"active_resume","label":"Active resume","requiredness":"required","reason":"An active resume file is needed for dry-run preparation."})
    ready=bool(fields.get("full_name",{}).get("confirmed") and fields.get("email",{}).get("confirmed") and resume_ok)
    needs_review=len(review)
    return {"confirmed":confirmed,"suggested":[x["label"] for x in review],
            "missing":[x["label"] for x in missing],"missing_items":missing,
            "requires_review":[x["label"] for x in review],"review_items":review,
            "optional":optional,"job_specific":job_specific,
            "attention_items":missing+review,"counts":{"confirmed":len(confirmed),"missing":len(missing),"needs_review":needs_review,"optional":len(optional)},
            "ready_for_dry_runs":ready,
            "message":"Ready for supported dry runs; job-specific unknowns will still pause for review." if ready else "Confirm your name and email and ensure an active resume is available before a dry run."}


@router.get("/api/application-answers")
def list_answers():
    with connect() as db:
        resume = _active(db)
        rows = db.execute("SELECT id,question_key,question_text,answer,answer_encrypted,sensitive,confirmed,updated_at,answer_type,jurisdiction,auto_reuse,source,question_category,sensitivity_classification,confirmation_at FROM application_answers WHERE user_id=? AND resume_id=? AND revoked_at IS NULL ORDER BY question_key", (USER, resume["id"])).fetchall()
        # Decrypt/lazily migrate while this connection is still open. Never
        # echo sensitive content in the normal answer-bank list response.
        output=[]
        for row in rows:
            answer=_answer_plaintext(row,db)
            output.append({"key":row["question_key"],"question":row["question_text"],
                "sensitive":bool(row["sensitive"]),"confirmed":bool(row["confirmed"]),
                "answer":answer if row["confirmed"] and not row["sensitive"] else "","updated_at":row["updated_at"],
                "answer_type":row["answer_type"],"jurisdiction":row["jurisdiction"],"auto_reuse":bool(row["auto_reuse"]),
                "source":row["source"],"category":row["question_category"],
                "sensitivity_classification":row["sensitivity_classification"],"confirmed_at":row["confirmation_at"]})
    return output


@router.get("/candidate-profile", response_class=HTMLResponse)
def profile_page():
    return HTMLResponse(apply_ui_theme(PROFILE_PAGE))


PROFILE_PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Candidate profile — BabyStepsAI</title>
<style>
:root{--ink:#17263b;--muted:#68788e;--line:#e1e8f0;--brand:#2563eb;--wash:#f4f7fb}*{box-sizing:border-box}body{margin:0;background:var(--wash);color:var(--ink);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.top{background:#fff;border-bottom:1px solid var(--line)}.nav,main{max-width:1080px;margin:auto;padding:16px 22px}.nav{display:flex;align-items:center;justify-content:space-between}.brand{font-size:20px;font-weight:750}.brand span{color:var(--brand)}nav{display:flex;gap:18px}a{color:#315378;text-decoration:none}a:hover{text-decoration:underline}main{padding-top:28px;padding-bottom:60px}h1{margin:0 0 5px;font-size:30px;letter-spacing:-.6px}h2{font-size:20px;margin:0 0 14px}.muted,.source{color:var(--muted)}.lead{margin-top:0;color:var(--muted)}.panel,.section{background:#fff;border:1px solid var(--line);border-radius:15px;padding:18px;margin:16px 0;box-shadow:0 2px 8px #102a4308}.summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}.tile{padding:12px;background:#f7f9fc;border-radius:10px}.tile strong{display:block;font-size:19px}.tile small,.source{font-size:13px}.ready{color:#137451}.review{color:#a75d0a}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,300px),1fr));gap:12px}.field{display:flex;flex-direction:column;gap:5px;margin:10px 0}.field input,.field textarea,.field select,.skill-entry{width:100%;font:inherit;padding:10px 11px;border:1px solid #ccd7e5;border-radius:8px;background:#fff;color:var(--ink)}.field textarea{min-height:76px;resize:vertical}.field input[type=checkbox]{width:auto}.checkrow{display:flex;align-items:center;gap:9px;margin:8px 0}.checkrow input{width:auto}.card{border:1px solid var(--line);border-radius:12px;padding:14px;margin:10px 0;background:#fbfcfe}.cardhead{display:flex;justify-content:space-between;gap:8px;align-items:center}.chip{display:inline-flex;align-items:center;gap:6px;background:#eaf1ff;border-radius:30px;padding:5px 10px;margin:4px;font-size:14px}.chip button{padding:0 3px;border:0;background:none;color:#345;cursor:pointer}.badge{display:inline-block;font-size:12px;border-radius:20px;padding:3px 9px;background:#eef2f7;color:#516276}.suggestion{background:#fff7e7;color:#925d0b}.confirmed{background:#e7f6ee;color:#18734b}.buttons{display:flex;flex-wrap:wrap;gap:9px;margin:13px 0}button{font:inherit;font-weight:600;padding:9px 13px;border-radius:9px;border:1px solid #cbd7e6;background:#fff;color:var(--ink);cursor:pointer}button:hover{border-color:var(--brand);color:var(--brand)}button.primary{background:var(--brand);border-color:var(--brand);color:#fff}.entry-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:4px 12px}.empty{color:var(--muted);padding:12px;border:1px dashed var(--line);border-radius:9px}details summary{cursor:pointer;font-weight:600}.danger{color:#a33}.status{min-height:24px;margin:12px 0}.invalid-field{border-color:#b42318!important;box-shadow:0 0 0 3px #b4231822!important}.required-tag,.optional-tag{display:inline-block;font-size:11px;border-radius:20px;padding:2px 7px;margin-left:6px;background:#eef2f7;color:#526174}.required-tag{background:#fff0ee;color:#9b2c23}.success-text{color:#137451}.error-text{color:#a3261d}.review-list{margin-top:10px}@media(max-width:650px){.nav{align-items:flex-start;flex-direction:column;gap:10px}.nav,main{padding-left:14px;padding-right:14px}nav{flex-wrap:wrap;gap:12px}h1{font-size:26px}}
</style></head><body><header class="top"><div class="nav"><div class="brand">BabySteps<span>AI</span></div><nav aria-label="Main navigation"><a href="/">Home</a><a href="/find-jobs">Find jobs</a><a href="/applications">Applications</a></nav></div></header><main>
<h1>Candidate profile</h1><p class="lead">Review resume suggestions, confirm only facts you have checked, and save progress at any time.</p><p id="resume" class="muted"></p>
<section class="panel"><h2>Profile completeness</h2><p class="muted">Required fields support basic dry-run preparation. Other details are optional unless a specific job requires them. Demographic disclosures are always optional.</p><div id="completeness" class="summary" aria-live="polite"></div><p id="readiness" class="muted"></p><button type="button" id="review-missing">Review Missing Information</button><div id="job-specific" class="review-list"></div></section>
<form id="profile"></form><div class="buttons"><button type="button" id="save" class="primary">Save profile</button></div>
<section class="section"><h2>Reusable application answers</h2><p class="muted">Answers are tied to this resume version. Sensitive or legal answers require explicit confirmation.</p><button type="button" onclick="addAnswer()">Add an answer</button><div id="answer-list"></div></section><div id="status" class="status" role="status" aria-live="polite"></div></main>
<script>
let fields=[],latestCompleteness=null;const hiddenLegacy=new Set(['graduation_month','graduation_year','graduation_date','university','degree','major','work_experience','internships','projects','technical_skills']);
const schemas={education_entries:[['institution','Institution','text'],['degree','Degree','text'],['major','Major / field','text'],['graduation_date','Graduation month and year','month'],['gpa','GPA (optional)','text'],['currently_enrolled','Currently enrolled','bool']],experience_entries:[['job_title','Job title','text'],['employer','Employer','text'],['location','Location','text'],['employment_type','Employment type','text'],['start_date','Start month','month'],['end_date','End month','month'],['currently_working','Currently working','bool'],['responsibilities','Responsibilities','list'],['technologies','Technologies used','list']],internship_entries:[['job_title','Internship title','text'],['employer','Employer','text'],['location','Location','text'],['employment_type','Employment type','text'],['start_date','Start month','month'],['end_date','End month','month'],['currently_working','Currently working','bool'],['responsibilities','Responsibilities','list'],['technologies','Technologies used','list']],project_entries:[['name','Project name','text'],['description','Description','textarea'],['technologies','Technologies','list'],['github_url','GitHub URL','url'],['demo_url','Live demo URL','url'],['start_date','Start month (optional)','month'],['end_date','End month (optional)','month'],['accomplishments','Key accomplishments','list']]};
async function jsonReq(url,options={}){const r=await fetch(url,options);let d={};try{d=await r.json()}catch{}if(!r.ok){const detail=d.detail;const message=Array.isArray(detail)?detail.map(x=>(Array.isArray(x.loc)?x.loc.filter(y=>y!=='body').join(' → ')+': ':'')+(x.msg||'Invalid value')).join('; '):typeof detail==='string'?detail:'Request failed. Check the highlighted field and try again.';const error=Error(message);error.detail=detail;throw error}return d}function formatMonth(value){if(!/^\d{4}-(0[1-9]|1[0-2])$/.test(value||''))return '';const [y,m]=value.split('-').map(Number);return new Intl.DateTimeFormat(undefined,{month:'long',year:'numeric',timeZone:'UTC'}).format(new Date(Date.UTC(y,m-1,1)))}
function badge(source){const x=document.createElement('span');x.className='badge '+(source==='user_confirmed'?'confirmed':source==='resume_suggestion'?'suggestion':'');x.textContent=source==='user_confirmed'?'Confirmed':source==='resume_suggestion'?'Resume / AI suggestion':'Missing';return x}
function confirmControl(key,checked,text='I reviewed and confirm'){const label=document.createElement('label');label.className='checkrow';const box=document.createElement('input');box.type='checkbox';box.dataset.confirm=key;box.checked=!!checked;label.append(box,document.createTextNode(text));return label}
function fieldControl(key,label,value,type='text',description=''){const wrap=document.createElement('div');wrap.className='field';const title=document.createElement('label');const safe='profile-'+key.replace(/[^a-zA-Z0-9_-]/g,'-');title.htmlFor=safe;title.textContent=label;const tag=document.createElement('span');tag.className=['full_name','email'].includes(key)?'required-tag':'optional-tag';tag.textContent=['full_name','email'].includes(key)?' · Required for basic preparation':' · Optional';title.append(tag);const input=type==='textarea'?document.createElement('textarea'):document.createElement('input');input.id=safe;input.dataset.key=key;if(type!=='textarea')input.type=type==='month'?'month':type;if(type==='month'&&input.type!=='month'){input.type='text';input.pattern='[0-9]{4}-(0[1-9]|1[0-2])';input.inputMode='numeric';input.setAttribute('aria-label',label+' in YYYY-MM format')}input.value=value??'';const examples={github:'https://github.com/username',linkedin:'https://www.linkedin.com/in/username',portfolio:'https://example.com/portfolio',website:'https://example.com',other_professional_links:'One public HTTPS URL per line'};if(examples[key]){input.placeholder=examples[key];input.setAttribute('autocomplete','url')}if(type==='month'){input.setAttribute('aria-describedby',safe+'-help');const help=document.createElement('small');help.id=safe+'-help';help.className='source';help.textContent=description||'Choose a month and year. No day is required.';const readout=document.createElement('output');readout.setAttribute('aria-live','polite');readout.textContent=formatMonth(input.value);input.addEventListener('input',()=>readout.textContent=formatMonth(input.value));wrap.append(title,input,help,readout)}else wrap.append(title,input);return wrap}
function objectSection(f,section){const box=document.createElement('section');box.className='section';const heading=document.createElement('div');heading.className='cardhead';heading.append(Object.assign(document.createElement('h2'),{textContent:f.label}),badge(f.source));box.append(heading);const add=document.createElement('button');add.type='button';add.textContent='Add '+(f.key==='education_entries'?'education':f.key==='experience_entries'?'experience':f.key==='internship_entries'?'internship':'project');add.onclick=()=>{collect();const target=fields.find(x=>x.key===f.key);target.value.push(emptyEntry(f.key));target.entry_confirmed.push(false);render()};box.append(add);const rows=Array.isArray(f.value)?f.value:[];if(!rows.length)box.append(Object.assign(document.createElement('p'),{className:'empty',textContent:f.key==='internship_entries'?'No internships added. You do not need to enter “NA”.':'No entries yet. Add one when relevant.'}));rows.forEach((item,index)=>{const card=document.createElement('article');card.className='card';card.dataset.entry=f.key;card.dataset.index=index;card._entry=item;const top=document.createElement('div');top.className='cardhead';top.append(Object.assign(document.createElement('strong'),{textContent:entryTitle(f.key,item,index)}));const remove=document.createElement('button');remove.type='button';remove.textContent='Remove';remove.onclick=()=>{collect();const target=fields.find(x=>x.key===f.key);target.value.splice(index,1);target.entry_confirmed.splice(index,1);render()};top.append(remove);card.append(top);const grid=document.createElement('div');grid.className='entry-grid';for(const [key,label,type] of schemas[f.key]){if(type==='bool'){const wrap=document.createElement('label');wrap.className='checkrow';const check=document.createElement('input');check.type='checkbox';check.dataset.prop=key;check.checked=!!item[key];wrap.append(check,document.createTextNode(label));grid.append(wrap)}else{let val=item[key]??'';if(type==='list'&&Array.isArray(val))val=val.join('\n');grid.append(fieldControl(f.key+':'+index+':'+key,label,val,type,type==='month'?'Optional; saved as YYYY-MM.':''));grid.lastElementChild.querySelector('[data-key]').dataset.prop=key}}card.append(grid);const hints=[];if(item.graduation_year_suggestion)hints.push('Graduation year suggested: '+item.graduation_year_suggestion+'; choose a month only if known.');if(item.start_year_suggestion)hints.push('Start year suggested: '+item.start_year_suggestion+'.');if(item.end_year_suggestion)hints.push('End year suggested: '+item.end_year_suggestion+'.');if(hints.length)card.append(Object.assign(document.createElement('p'),{className:'source',textContent:'Resume / AI suggestion: '+hints.join(' ')}));card.append(confirmControl(f.key+':'+index,f.entry_confirmed?.[index],'I reviewed and confirm this entry'));const unconfirm=()=>{const checkbox=card.querySelector('[data-confirm="'+f.key+':'+index+'"]');if(checkbox)checkbox.checked=false};for(const control of card.querySelectorAll('[data-prop]')){control.addEventListener('input',unconfirm);control.addEventListener('change',unconfirm)}box.append(card)});section.append(box)}
function emptyEntry(key){return Object.fromEntries(schemas[key].map(([name,_label,type])=>[name,type==='bool'?false:type==='list'?[]:'']))}function entryTitle(key,item,index){return key==='education_entries'?(item.degree||'Education entry '+(index+1)):key==='project_entries'?(item.name||'Project '+(index+1)):(item.job_title|| (key==='internship_entries'?'Internship ':'Experience ')+(index+1))}
function valuePresent(value){return Array.isArray(value)?value.some(x=>String(x||'').trim()):String(value??'').trim().length>0}
function valueInvalid(key,value){if(!valuePresent(value))return false;if(key==='email')return !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(String(value).trim());if(['github','linkedin','portfolio','website'].includes(key)){try{const u=new URL(String(value).trim());return u.protocol!=='https:'||!u.hostname||!!u.username||!!u.password}catch{return true}}return false}
function fieldStatus(f,value=f.value,confirmed=f.confirmed){const present=valuePresent(value),invalid=valueInvalid(f.key,value),optional=!['full_name','email'].includes(f.key);if(invalid)return ['Invalid','Invalid value. Please correct this field.','state-invalid'];if(!present)return optional?['Optional · Not provided','This optional field is blank.','state-optional']:['Missing','This required field is blank.','state-missing'];if(confirmed)return ['Confirmed','Current saved value is confirmed.','state-confirmed'];return ['Needs Review',f.source==='resume_suggestion'?'Resume suggestion; review and confirm it.':'Provided value needs your confirmation.','state-review']}
function statusIndicator(f,value=f.value,confirmed=f.confirmed){const [text,description,state]=fieldStatus(f,value,confirmed);const item=document.createElement('span');item.className='field-status '+state;item.textContent=text;item.setAttribute('aria-label',f.label+': '+text+'. '+description);return item}
function paintField(row,f,input,checkbox){const value=input.value.trim(),[text,description,state]=fieldStatus(f,value,checkbox?.checked);const item=row.querySelector('.field-status');item.className='field-status '+state;item.textContent=text;item.setAttribute('aria-label',f.label+': '+text+'. '+description);const valid=!valueInvalid(f.key,value),present=valuePresent(value);input.classList.toggle('invalid-field',!valid);if(!valid){input.setAttribute('aria-invalid','true');input.setAttribute('aria-describedby','profile-'+f.key+'-error');let error=row.querySelector('.field-error');if(!error){error=document.createElement('small');error.className='field-error';error.id='profile-'+f.key+'-error';row.append(error)}error.textContent=f.key==='email'?'Enter an email address such as name@example.com.':'Use a valid public HTTPS URL.'}else{input.removeAttribute('aria-invalid');input.removeAttribute('aria-describedby');row.querySelector('.field-error')?.remove()}let control=row.querySelector('.checkrow');if(present&&valid){if(!control){control=confirmControl(f.key,false);row.append(control);checkbox=control.querySelector('input')}if(checkbox){checkbox.disabled=false;checkbox.onchange=()=>paintField(row,f,input,checkbox)}}else{control?.remove()}}
function render(){const form=document.querySelector('#profile');form.replaceChildren();const groups={};for(const f of fields){if(hiddenLegacy.has(f.key))continue;let section=groups[f.section];if(!section){section=document.createElement('section');section.className='section';section.append(Object.assign(document.createElement('h2'),{textContent:f.section}));groups[f.section]=section;form.append(section)}if(f.kind==='objects'){objectSection(f,section);continue}if(f.kind==='chips'){renderSkills(f,section);continue}const row=document.createElement('div');row.className='profile-field-row';const wrap=fieldControl(f.key,f.label,Array.isArray(f.value)?f.value.join('\n'):f.value,f.kind==='list'?'textarea':f.kind,f.suggestion?'Resume / AI suggestion: graduation year '+f.suggestion+'.':'');row.append(wrap,statusIndicator(f));if(f.sensitive)row.append(Object.assign(document.createElement('p'),{className:'source sensitive-note',textContent:'Sensitive/legal information is used only after explicit confirmation.'}));const input=wrap.querySelector('[data-key]');let checkbox=null;if(valuePresent(input.value)&&!valueInvalid(f.key,input.value)){const control=confirmControl(f.key,f.confirmed);row.append(control);checkbox=control.querySelector('input')}input.addEventListener('input',()=>{if(checkbox)checkbox.checked=false;paintField(row,f,input,checkbox)});input.addEventListener('change',()=>{if(checkbox)checkbox.checked=false;paintField(row,f,input,checkbox)});if(checkbox)checkbox.onchange=()=>paintField(row,f,input,checkbox);paintField(row,f,input,checkbox);section.append(row)}}
function normalizedSkill(value){return String(value||'').trim().replace(/\s+/g,' ').toLocaleLowerCase()}
function renderSkills(f,section){const wrap=document.createElement('div');wrap.className='field skill-field';const heading=document.createElement('label');heading.textContent=f.label;const tag=document.createElement('span');tag.className='optional-tag';tag.textContent=' · Optional';heading.append(tag);wrap.append(heading);const chips=document.createElement('div');chips.className='skill-chips';chips.dataset.skillChips='1';for(const skill of f.value||[])if(normalizedSkill(skill)&&![...chips.children].some(x=>normalizedSkill(x.dataset.value)===normalizedSkill(skill)))chips.append(skillChip(skill));const input=document.createElement('input');input.type='search';input.className='skill-entry';input.placeholder='Search or enter any skill';input.setAttribute('aria-label','Search or enter a custom technical skill');input.addEventListener('input',()=>{for(const chip of chips.children)chip.hidden=!!input.value&&!chip.dataset.value.toLocaleLowerCase().includes(input.value.toLocaleLowerCase())});const add=document.createElement('button');add.type='button';add.textContent='Add skill';const notice=document.createElement('span');notice.className='field-status state-review';notice.setAttribute('aria-live','polite');add.onclick=()=>{const value=input.value.trim().replace(/\s+/g,' ');if(value&&!Array.from(chips.children).some(x=>normalizedSkill(x.dataset.value)===normalizedSkill(value))){chips.append(skillChip(value));notice.textContent='Skill added. Review and confirm the skill list before saving.'}else if(value){notice.textContent='That skill is already in the list.'}input.value='';for(const chip of chips.children)chip.hidden=false;const checkbox=wrap.querySelector('[data-confirm="skills"]');if(checkbox)checkbox.checked=false};input.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();add.click()}});wrap.append(chips,input,add,statusIndicator(f),notice);if(chips.children.length)wrap.append(confirmControl(f.key,f.confirmed));section.append(wrap)}function skillChip(value){const chip=document.createElement('span');chip.className='chip';chip.dataset.value=value;chip.append(document.createTextNode(value));const b=document.createElement('button');b.type='button';b.setAttribute('aria-label','Remove '+value);b.textContent='Remove';b.onclick=()=>{const wrap=chip.closest('.field');const checkbox=wrap?.querySelector('[data-confirm="skills"]');if(checkbox)checkbox.checked=false;chip.remove();if(checkbox)checkbox.dispatchEvent(new Event('change',{bubbles:true}))};chip.append(b);return chip}
function collect(){const values={},confirmed=[];for(const f of fields){if(hiddenLegacy.has(f.key))continue;if(f.kind==='objects'){const cards=[...document.querySelectorAll('[data-entry="'+f.key+'"]')];values[f.key]=cards.map(card=>{const item=Object.assign({},card._entry||{});for(const control of card.querySelectorAll('[data-prop]')){const key=control.dataset.prop;item[key]=control.type==='checkbox'?control.checked:control.value.split('\n').map(x=>x.trim()).filter(Boolean);if(control.type!=='checkbox'&&!['responsibilities','technologies','accomplishments'].includes(key))item[key]=control.value.trim()}if(item.currently_working)item.end_date='';return item});cards.forEach((card,i)=>{if(card.querySelector('[data-confirm="'+f.key+':'+i+'"]')?.checked)confirmed.push(f.key+':'+i)});continue}if(f.kind==='chips'){values[f.key]=[...document.querySelectorAll('[data-skill-chips] .chip')].map(x=>x.dataset.value);if(document.querySelector('[data-confirm="'+f.key+'"]')?.checked)confirmed.push(f.key);continue}const control=document.querySelector('[data-key="'+f.key+'"]');values[f.key]=f.kind==='list'?control.value.split('\n').map(x=>x.trim()).filter(Boolean):control.value;if(document.querySelector('[data-confirm="'+f.key+'"]')?.checked)confirmed.push(f.key)}const verifiedEducation=(values.education_entries||[]).find((_,i)=>confirmed.includes('education_entries:'+i));values.university=verifiedEducation?.institution||'';values.degree=verifiedEducation?.degree||'';values.major=verifiedEducation?.major||'';values.graduation_date=verifiedEducation?.graduation_date||'';values.graduation_month=values.graduation_date?values.graduation_date.slice(5,7):'';values.graduation_year=values.graduation_date?values.graduation_date.slice(0,4):'';if(verifiedEducation){for(const key of ['university','degree','major'])if(values[key])confirmed.push(key);if(values.graduation_date)confirmed.push('graduation_date','graduation_month','graduation_year')}values.work_experience=(values.experience_entries||[]).map(x=>JSON.stringify(x));values.internships=(values.internship_entries||[]).map(x=>JSON.stringify(x));values.projects=(values.project_entries||[]).map(x=>JSON.stringify(x));values.technical_skills=values.skills||[];if(confirmed.includes('skills'))confirmed.push('technical_skills');for(const f of fields){if(Object.prototype.hasOwnProperty.call(values,f.key)){f.value=values[f.key];if(f.kind==='objects')f.entry_confirmed=values[f.key].map((_,i)=>confirmed.includes(f.key+':'+i));else if(f.kind==='chips'||f.kind!=='list')f.confirmed=confirmed.includes(f.key)}}return {values,confirmed_fields:confirmed}}
function canonical(v){if(Array.isArray(v))return '['+v.map(canonical).join(',')+']';if(v&&typeof v==='object')return '{'+Object.keys(v).sort().map(k=>JSON.stringify(k)+':'+canonical(v[k])).join(',')+'}';return JSON.stringify(v)}
function renderCompletion(completion){latestCompleteness=completion;const box=document.querySelector('#completeness');box.replaceChildren();for(const [name,count] of [['Confirmed',completion.counts.confirmed],['Missing',completion.counts.missing],['Needs Review',completion.counts.needs_review]]){const tile=document.createElement('div');tile.className='tile';tile.append(Object.assign(document.createElement('strong'),{textContent:String(count)}),Object.assign(document.createElement('small'),{textContent:name}));box.append(tile)}document.querySelector('#readiness').textContent=completion.message;document.querySelector('#readiness').className=completion.ready_for_dry_runs?'ready':'review';const jobs=document.querySelector('#job-specific');jobs.replaceChildren();if(completion.job_specific.length){jobs.append(Object.assign(document.createElement('h3'),{textContent:'Job-specific requirements (not general profile requirements)'}));const ul=document.createElement('ul');for(const item of completion.job_specific){const li=document.createElement('li');li.textContent=item.company+' — '+item.title+': '+item.reason;ul.append(li)}jobs.append(ul)}}
async function load(){const [data,completion]=await Promise.all([jsonReq('/api/candidate-profile/verification'),jsonReq('/api/candidate-profile/completeness')]);fields=data.fields;document.querySelector('#resume').textContent='Active resume: '+data.resume_name+' · Resume analysis: '+data.resume_status;render();renderCompletion(completion);await loadAnswers()}
document.querySelector('#review-missing').onclick=()=>{const item=latestCompleteness?.attention_items?.[0];const key=(item?.key||'full_name').split(':')[0];if(key==='active_resume'){location.assign('/');return}const target=[...document.querySelectorAll('[data-key]')].find(x=>x.dataset.key===key)||document.querySelector('[data-confirm]')||document.querySelector('#save');target?.scrollIntoView({behavior:'smooth',block:'center'});target?.focus();document.querySelector('#readiness').textContent=item?'Review '+item.label+': '+item.reason:'Review fields marked Missing or Needs Review, then save. Job-specific requirements are listed separately.'};
document.querySelector('#save').onclick=async()=>{const save=document.querySelector('#save'),status=document.querySelector('#status');save.disabled=true;save.setAttribute('aria-busy','true');status.replaceChildren();status.className='status';document.querySelectorAll('[aria-invalid="true"]').forEach(x=>{x.removeAttribute('aria-invalid');x.classList.remove('invalid-field')});try{const payload=collect();const saved=await jsonReq('/api/candidate-profile/verification',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});if(!saved.saved)throw Error('The server did not confirm the profile save.');const persisted=await jsonReq('/api/candidate-profile/verification');const byKey=Object.fromEntries(persisted.fields.map(x=>[x.key,x]));for(const [key,value] of Object.entries(payload.values)){if(byKey[key]&&canonical(byKey[key].value)!==canonical(value))throw Error('Could not verify the saved value for '+byKey[key].label+'. Your profile was not marked successful.')}for(const field of persisted.fields){if(!Object.prototype.hasOwnProperty.call(payload.values,field.key))continue;if(field.kind==='objects'){for(let i=0;i<(payload.values[field.key]||[]).length;i++)if(!!field.entry_confirmed?.[i]!==payload.confirmed_fields.includes(field.key+':'+i))throw Error('Could not verify confirmation for '+field.label+' entry '+(i+1)+'.')}else if(!!field.confirmed!==payload.confirmed_fields.includes(field.key))throw Error('Could not verify confirmation for '+field.label+'.')}const completion=await jsonReq('/api/candidate-profile/completeness');fields=persisted.fields;render();renderCompletion(completion);status.className='status success-text';const items=completion.attention_items||[];status.append(document.createTextNode(items.length?'Saved — '+items.length+' items need attention.':'Saved — no required items need attention.'));if(items.length){const list=document.createElement('ul');list.className='attention-list';for(const item of items){const li=document.createElement('li');li.textContent=item.label+': '+item.reason+' ('+item.requiredness+').';list.append(li)}status.append(list)}if(completion.optional.length)status.append(Object.assign(document.createElement('p'),{className:'muted',textContent:completion.optional.length+' optional fields remain blank or unconfirmed; they do not prevent saving.'}));if(completion.job_specific.length)status.append(Object.assign(document.createElement('p'),{className:'muted',textContent:completion.job_specific.length+' job-specific requirements are listed above.'}));await loadAnswers().catch(()=>{})}catch(e){status.className='status error-text';status.textContent='Save failed: '+e.message;const message=String(e.message).toLowerCase();const target=[...document.querySelectorAll('[data-key]')].find(x=>message.includes(x.dataset.key.toLowerCase().replaceAll('_',' '))||message.includes(x.dataset.key.toLowerCase()));if(target){target.setAttribute('aria-invalid','true');target.classList.add('invalid-field');target.focus()}}finally{save.disabled=false;save.removeAttribute('aria-busy')}}
async function loadAnswers(){const a=await jsonReq('/api/application-answers');const box=document.querySelector('#answer-list');box.replaceChildren();if(!a.length){box.append(Object.assign(document.createElement('p'),{className:'empty',textContent:'No reusable answers yet.'}));return}for(const x of a){const row=document.createElement('details');const summary=document.createElement('summary');summary.textContent=x.question+(x.sensitive?' · sensitive':'');row.append(summary);row.append(Object.assign(document.createElement('p'),{textContent:x.confirmed?x.answer:'Not confirmed'}));if(x.key.startsWith('custom:')){const edit=document.createElement('button');edit.textContent='Edit';edit.onclick=()=>addAnswer(x.key.slice(7),x.question,x.answer,x.sensitive);const revoke=document.createElement('button');revoke.textContent='Revoke';revoke.onclick=async()=>{await jsonReq('/api/application-answers/'+encodeURIComponent(x.key.slice(7)),{method:'DELETE'});await loadAnswers()};row.append(edit,revoke)}box.append(row)}}
async function addAnswer(key='',question='',answer='',sensitive=false){question=prompt('Application question:',question);if(question===null)return;answer=prompt('Your exact answer:',answer);if(answer===null)return;key=key||prompt('Short key (letters, numbers, hyphens):','custom-answer');if(!key)return;const confirmed=confirm('Confirm that this exact answer is accurate?');const markedSensitive=sensitive||confirm('Is this answer sensitive or legally significant?');try{const saved=await jsonReq('/api/application-answers/'+encodeURIComponent(key),{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({question,answer,sensitive:markedSensitive,confirmed})});if(!saved.saved||!saved.persisted_verified)throw Error('The saved answer could not be verified. No success confirmation was shown.');await loadAnswers();document.querySelector('#status').textContent='Answer saved and verified.'}catch(e){document.querySelector('#status').textContent='Save failed: '+e.message}}
schemas.country_work_authorizations=[['country','Country / jurisdiction','text'],['status','Authorization status','text']];schemas.sponsorship_by_country=[['country','Country / jurisdiction','text'],['requires_sponsorship','Will sponsorship be required?','text']];
load().catch(e=>document.querySelector('#status').textContent=e.message);
</script></body></html>'''

# Compact, theme-aware profile controls keep status text close to the field and
# make the same layout usable on narrow screens.
PROFILE_PAGE = PROFILE_PAGE.replace(
    '</style></head>',
    '''</style><style id="profile-editor-styles">
.profile-field-row{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:start;gap:4px 14px;padding:12px 0;border-bottom:1px solid var(--bsa-line,#dfe7f1)}
.profile-field-row>.field{grid-column:1;min-width:0;margin:0}.profile-field-row>.field-status{grid-column:2;grid-row:1;align-self:center}.profile-field-row>.checkrow,.profile-field-row>.sensitive-note,.profile-field-row>.field-error{grid-column:1/-1}
.field-status{display:inline-flex;align-items:center;max-width:100%;width:max-content;padding:3px 9px;border:1px solid currentColor;border-radius:999px;font-size:12px;font-weight:700;line-height:1.35}
.state-confirmed{color:#14532d;background:#e5f7eb}.state-review{color:#713f12;background:#fff4d6}.state-missing,.state-invalid{color:#8f1d18;background:#fff0ee}.state-optional{color:#37465b;background:#edf1f6}
.field-error{display:block;color:#8f1d18;font-size:13px;font-weight:600}.invalid-field{border-color:#b42318!important;box-shadow:0 0 0 3px #b4231822!important}.profile-field-row input:focus-visible,.profile-field-row textarea:focus-visible,.skill-entry:focus-visible,button:focus-visible{outline:3px solid #2563eb;outline-offset:2px}
.skill-chips{display:flex;flex-wrap:wrap;gap:8px;max-width:100%;margin:4px 0;min-width:0}.chip{display:inline-flex;align-items:center;gap:8px;max-width:100%;min-width:0;margin:0;padding:5px 8px 5px 11px;background:#dceaff;color:#132f56;border:1px solid #7899c6;border-radius:999px;font-size:14px;font-weight:650;overflow-wrap:anywhere}.chip button{flex:none;padding:2px 6px;background:#fff;color:#132f56;border:1px solid #59799f;border-radius:6px;font-size:12px}.skill-entry{max-width:100%}.skill-field>.field-status{margin:4px 0}.skill-field>.checkrow{margin-top:6px}
.required-tag,.optional-tag{font-size:11px}.optional-tag{background:#edf1f6;color:#37465b}.required-tag{background:#fff0ee;color:#8f1d18}.checkrow{width:max-content;max-width:100%;padding:3px 0}.attention-list{padding-left:22px}.buttons button:disabled{opacity:.65;cursor:wait}
html[data-bsa-theme="dark"] .state-confirmed,html[data-bsa-theme="dark"] .success-text{color:#b7f7cd;background:#173c2a;border-color:#4c9a69}html[data-bsa-theme="dark"] .state-review,html[data-bsa-theme="dark"] .review{color:#ffe29a;background:#493916;border-color:#b28a35}html[data-bsa-theme="dark"] .state-missing,html[data-bsa-theme="dark"] .state-invalid,html[data-bsa-theme="dark"] .field-error,html[data-bsa-theme="dark"] .error-text{color:#ffb4aa;background:#481f23;border-color:#b45a59}html[data-bsa-theme="dark"] .state-optional,html[data-bsa-theme="dark"] .optional-tag{color:#d0d8e5;background:#263449;border-color:#64748b}html[data-bsa-theme="dark"] .required-tag{color:#ffb4aa;background:#481f23;border-color:#b45a59}html[data-bsa-theme="dark"] .chip{color:#e8f1ff;background:#203b5e;border-color:#7899c6}html[data-bsa-theme="dark"] .chip button{color:#f2f6fc;background:#172337;border-color:#91a8c9}html[data-bsa-theme="dark"] .profile-field-row{border-color:var(--bsa-line)}
@media(max-width:620px){.profile-field-row{grid-template-columns:minmax(0,1fr);gap:6px}.profile-field-row>.field,.profile-field-row>.field-status,.profile-field-row>.checkrow,.profile-field-row>.sensitive-note,.profile-field-row>.field-error{grid-column:1;grid-row:auto}.profile-field-row>.field-status{justify-self:start}.skill-chips{gap:6px}.chip{font-size:13px}}
</style></head>''',1)
PROFILE_PAGE = PROFILE_PAGE.replace('function objectSection(f,section){', 'function entryHasValue(item){return !!item&&Object.entries(item).some(([key,value])=>!/[._]year_suggestion$/.test(key)&&!/[._]currently_(working|enrolled)$/.test(key)&&((Array.isArray(value)&&value.some(x=>String(x||\'\').trim()))||(typeof value===\'string\'&&value.trim())))}function objectSection(f,section){',1)
PROFILE_PAGE = PROFILE_PAGE.replace('badge(f.source));box.append(heading);', 'statusIndicator(f,Array.isArray(f.value)?f.value:[],f.confirmed));box.append(heading);',1)
PROFILE_PAGE = PROFILE_PAGE.replace("box.append(heading);const add=document.createElement('button');", "box.append(heading);box.append(Object.assign(document.createElement('p'),{className:'muted optional-note',textContent:'Optional · Job-specific requirements may ask for this information.'}));if(f.source==='resume_suggestion')box.append(Object.assign(document.createElement('p'),{className:'source',textContent:'Resume-extracted suggestions are unconfirmed until you review them.'}));const add=document.createElement('button');",1)
PROFILE_PAGE = PROFILE_PAGE.replace("row.append(wrap,statusIndicator(f));if(f.sensitive)", "row.append(wrap,statusIndicator(f));if(f.source==='resume_suggestion')row.append(Object.assign(document.createElement('p'),{className:'source',textContent:'Resume-extracted suggestion — review before confirming.'}));if(f.sensitive)",1)
PROFILE_PAGE = PROFILE_PAGE.replace('card.append(confirmControl(f.key+\':\'+index,f.entry_confirmed?.[index],\'I reviewed and confirm this entry\'));', 'if(entryHasValue(item))card.append(confirmControl(f.key+\':\'+index,f.entry_confirmed?.[index],\'I reviewed and confirm this entry\'));',1)
PROFILE_PAGE = PROFILE_PAGE.replace(
    "const unconfirm=()=>{const checkbox=card.querySelector('[data-confirm=\"'+f.key+':'+index+'\"]');if(checkbox)checkbox.checked=false};for(const control of card.querySelectorAll('[data-prop]')){control.addEventListener('input',unconfirm);control.addEventListener('change',unconfirm)}",
    "const unconfirm=()=>{const checkbox=card.querySelector('[data-confirm=\"'+f.key+':'+index+'\"]');if(checkbox)checkbox.checked=false;const current={};for(const control of card.querySelectorAll('[data-prop]'))if(control.type!=='checkbox')current[control.dataset.prop]=control.value.trim();let confirmation=card.querySelector('[data-confirm=\"'+f.key+':'+index+'\"]')?.closest('label');if(entryHasValue(current)&&!confirmation){confirmation=confirmControl(f.key+':'+index,false,'I reviewed and confirm this entry');card.append(confirmation)}else if(!entryHasValue(current))confirmation?.remove()};for(const control of card.querySelectorAll('[data-prop]')){control.addEventListener('input',unconfirm);control.addEventListener('change',unconfirm)}",
    1)
PROFILE_PAGE = PROFILE_PAGE.replace("function valuePresent(value){return Array.isArray(value)?value.some(x=>String(x||'').trim()):String(value??'').trim().length>0}", "function valuePresent(value){if(Array.isArray(value))return value.some(valuePresent);if(value&&typeof value==='object')return entryHasValue(value);return String(value??'').trim().length>0}",1)
PROFILE_PAGE = PROFILE_PAGE.replace("item.className='field-status '+state;item.textContent=text;item.setAttribute('aria-label'", "item.className='field-status '+state;item.textContent=text;item.setAttribute('role','status');item.setAttribute('aria-live','polite');item.setAttribute('aria-label'",1)
PROFILE_PAGE = PROFILE_PAGE.replace("find(x=>x.dataset.key===key)||document.querySelector('[data-confirm]')", "find(x=>x.dataset.key===key||x.dataset.key.startsWith(key+':'))||document.querySelector('[data-confirm]')",1)
PROFILE_PAGE = PROFILE_PAGE.replace("status.className='status';document.querySelectorAll('[aria-invalid=", "status.className='status field-status state-review';status.textContent='Saving profile…';document.querySelectorAll('[aria-invalid=",1)
PROFILE_PAGE = PROFILE_PAGE.replace("items.length?'Saved — '+items.length+' items need attention.':'Saved — no required items need attention.'", "items.length?'Saved — '+items.length+' item'+(items.length===1?' needs attention.':'s need attention.'):'Profile saved successfully. No required items need attention.'",1)
PROFILE_PAGE = PROFILE_PAGE.replace(
    "function valueInvalid(key,value){if(!valuePresent(value))return false;if(key==='email')return !/^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$/.test(String(value).trim());if(['github','linkedin','portfolio','website'].includes(key)){try{const u=new URL(String(value).trim());return u.protocol!=='https:'||!u.hostname||!!u.username||!!u.password}catch{return true}}return false}",
    "function isPublicHttpsURL(value){try{const u=new URL(String(value).trim()),host=u.hostname.toLowerCase();return u.protocol==='https:'&&!!host&&!u.username&&!u.password&&!['localhost','127.0.0.1','::1'].includes(host)&&!host.endsWith('.local')&&!host.endsWith('.internal')}catch{return false}}function valueInvalid(key,value){if(!valuePresent(value))return false;if(key==='email')return !/^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$/.test(String(value).trim());if(['github','linkedin','portfolio','website'].includes(key))return !isPublicHttpsURL(value);if(key==='other_professional_links')return String(value).split('\\n').map(x=>x.trim()).filter(Boolean).some(x=>!isPublicHttpsURL(x));return false}",1)
PROFILE_PAGE = PROFILE_PAGE.replace("let fields=[],latestCompleteness=null;", "let fields=[],latestCompleteness=null,profileDirty=false;window.addEventListener('beforeunload',e=>{if(profileDirty){e.preventDefault();e.returnValue=''}});document.addEventListener('input',e=>{if(e.target.closest('#profile'))profileDirty=true});document.addEventListener('change',e=>{if(e.target.closest('#profile'))profileDirty=true});document.addEventListener('click',e=>{if(e.target.closest('#profile')&&e.target.closest('button'))profileDirty=true},true);",1)
PROFILE_PAGE = PROFILE_PAGE.replace("add.onclick=()=>{collect();const target=fields.find(x=>x.key===f.key);", "add.onclick=()=>{profileDirty=true;collect();const target=fields.find(x=>x.key===f.key);",1)
PROFILE_PAGE = PROFILE_PAGE.replace("remove.onclick=()=>{collect();const target=fields.find(x=>x.key===f.key);", "remove.onclick=()=>{profileDirty=true;collect();const target=fields.find(x=>x.key===f.key);",1)
PROFILE_PAGE = PROFILE_PAGE.replace("fields=persisted.fields;render();renderCompletion(completion);status.className='status success-text';", "fields=persisted.fields;profileDirty=false;render();renderCompletion(completion);status.className='status success-text';",1)
PROFILE_PAGE = PROFILE_PAGE.replace("input.removeAttribute('aria-invalid');input.removeAttribute('aria-describedby');row.querySelector('.field-error')?.remove()", "input.removeAttribute('aria-invalid');const help=document.getElementById('profile-'+f.key+'-help');if(help)input.setAttribute('aria-describedby',help.id);else input.removeAttribute('aria-describedby');row.querySelector('.field-error')?.remove()",1)
PROFILE_PAGE = PROFILE_PAGE.replace("[['Confirmed',completion.counts.confirmed],['Missing',completion.counts.missing],['Needs Review',completion.counts.needs_review]]", "[['Confirmed',completion.counts.confirmed],['Missing',completion.counts.missing],['Needs Review',completion.counts.needs_review],['Optional — Not provided',completion.counts.optional||0],['Job-specific',completion.job_specific.length]]",1)
PROFILE_PAGE = PROFILE_PAGE.replace('wrap.append(chips,input,add,statusIndicator(f),notice)', 'const skillState=statusIndicator(f);skillState.classList.add(\'skill-status\');wrap.append(chips,input,add,skillState,notice)',1)
PROFILE_PAGE = PROFILE_PAGE.replace("if(chips.children.length)wrap.append(confirmControl(f.key,f.confirmed));", "if(chips.children.length){const control=confirmControl(f.key,f.confirmed),check=control.querySelector('input');check.onchange=()=>{const remaining=chips.children.length,state=check.checked?'state-confirmed':remaining?'state-review':'state-optional';skillState.className='field-status skill-status '+state;skillState.textContent=check.checked?'Confirmed':remaining?'Needs Review':'Optional · Not provided';skillState.setAttribute('aria-label','Technical skills: '+skillState.textContent)};wrap.append(control)}",1)
PROFILE_PAGE = PROFILE_PAGE.replace(
    "if(chips.children.length){const control=confirmControl(f.key,f.confirmed),check=control.querySelector('input');check.onchange=()=>{const remaining=chips.children.length,state=check.checked?'state-confirmed':remaining?'state-review':'state-optional';skillState.className='field-status skill-status '+state;skillState.textContent=check.checked?'Confirmed':remaining?'Needs Review':'Optional · Not provided';skillState.setAttribute('aria-label','Technical skills: '+skillState.textContent)};wrap.append(control)}",
    "attachSkillConfirmation(wrap,chips,skillState,f);",1)
PROFILE_PAGE = PROFILE_PAGE.replace("function renderSkills(f,section){", "function attachSkillConfirmation(wrap,chips,skillState,f){let control=wrap.querySelector('[data-confirm=\"skills\"]')?.closest('.checkrow');if(!chips.children.length){control?.remove();return}if(control)return;control=confirmControl(f.key,false);const check=control.querySelector('input');check.onchange=()=>{const remaining=chips.children.length,state=check.checked?'state-confirmed':remaining?'state-review':'state-optional';skillState.className='field-status skill-status '+state;skillState.textContent=check.checked?'Confirmed':remaining?'Needs Review':'Optional · Not provided';skillState.setAttribute('aria-label','Technical skills: '+skillState.textContent)};wrap.append(control)}function renderSkills(f,section){",1)
PROFILE_PAGE = PROFILE_PAGE.replace("input.value='';for(const chip of chips.children)chip.hidden=false;const checkbox=wrap.querySelector('[data-confirm=\"skills\"]');", "input.value='';for(const chip of chips.children)chip.hidden=false;attachSkillConfirmation(wrap,chips,skillState,f);const checkbox=wrap.querySelector('[data-confirm=\"skills\"]');",1)
PROFILE_PAGE = PROFILE_PAGE.replace("if(checkbox)checkbox.checked=false;chip.remove();const state=wrap?.querySelector('.skill-status');", "if(checkbox)checkbox.checked=false;chip.remove();if(!wrap?.querySelector('.chip'))checkbox?.closest('.checkrow')?.remove();const state=wrap?.querySelector('.skill-status');",1)
PROFILE_PAGE = PROFILE_PAGE.replace("input.addEventListener('input',()=>{if(checkbox)checkbox.checked=false;paintField(row,f,input,checkbox)});input.addEventListener('change',()=>{if(checkbox)checkbox.checked=false;paintField(row,f,input,checkbox)});", "const refresh=()=>{const current=row.querySelector('[data-confirm=\"'+f.key+'\"]');if(current)current.checked=false;paintField(row,f,input,current)};input.addEventListener('input',refresh);input.addEventListener('change',refresh);",1)
PROFILE_PAGE = PROFILE_PAGE.replace("if(value&&!Array.from(chips.children).some(x=>normalizedSkill(x.dataset.value)===normalizedSkill(value))){chips.append(skillChip(value));notice.textContent='Skill added. Review and confirm the skill list before saving.'}", "if(value&&!Array.from(chips.children).some(x=>normalizedSkill(x.dataset.value)===normalizedSkill(value))){chips.append(skillChip(value));skillState.className='field-status skill-status state-review';skillState.textContent='Needs Review';skillState.setAttribute('aria-label','Technical skills: Needs Review. Review and confirm the current skill list.');notice.textContent='Skill added. Review and confirm the skill list before saving.'}",1)
PROFILE_PAGE = PROFILE_PAGE.replace("const checkbox=wrap?.querySelector('[data-confirm=\"skills\"]');if(checkbox)checkbox.checked=false;chip.remove()", "const checkbox=wrap?.querySelector('[data-confirm=\"skills\"]');if(checkbox)checkbox.checked=false;chip.remove();const state=wrap?.querySelector('.skill-status');if(state){const remaining=wrap.querySelectorAll('.chip').length;state.className='field-status skill-status '+(remaining?'state-review':'state-optional');state.textContent=remaining?'Needs Review':'Optional · Not provided';state.setAttribute('aria-label','Technical skills: '+state.textContent)}",1)
# Location access is strictly opt-in and one-shot. Coordinates remain in local
# JavaScript memory only; only the user-confirmed approximate address fields
# are saved through the existing field-level verification form.
PROFILE_PAGE = PROFILE_PAGE.replace(
    '<form id="profile"></form>',
    '''<section class="panel" aria-labelledby="location-heading"><h2 id="location-heading">Location preferences</h2>
<p class="muted">Use your confirmed residence or mailing location for applications. Current device location is optional, requested only when you click, and never continuously tracked.</p>
<div class="buttons"><button type="button" id="use-location">Use My Current Location</button><button type="button" id="manual-location">Enter Location Manually</button><button type="button" id="not-now">Not Now</button></div>
<p id="location-status" class="status" role="status" aria-live="polite"></p></section><form id="profile"></form>''')
PROFILE_PAGE = PROFILE_PAGE.replace(
    '</script></body></html>',
    '''</script><script>
(()=>{const status=document.querySelector('#location-status'),geo=document.querySelector('#use-location');
const setStatus=(text)=>{status.textContent=text};
document.querySelector('#not-now').onclick=()=>setStatus('Location sharing was skipped. You can enter an address manually at any time.');
document.querySelector('#manual-location').onclick=()=>{const target=document.querySelector('[data-key="city"]');target?.focus();setStatus('Enter your residence or mailing city, state/province, country, and postal code in the profile fields, then save and confirm them.');};
geo.onclick=()=>{
 let deniedBefore=false;try{deniedBefore=localStorage.getItem('babysteps-location-denied')==='1'}catch{}if(deniedBefore){setStatus('Location permission was previously denied. Enter your location manually or change browser permissions in settings.');return;}
 if(!navigator.geolocation){setStatus('This browser does not provide location access. Please enter your location manually.');return;}
 geo.disabled=true;setStatus('Waiting for your browser’s location permission…');
 navigator.geolocation.getCurrentPosition(async pos=>{
   try{setStatus('Finding an approximate location…');const q=new URLSearchParams({format:'jsonv2',lat:String(pos.coords.latitude),lon:String(pos.coords.longitude),zoom:'10'});
     const response=await fetch('https://nominatim.openstreetmap.org/reverse?'+q.toString(),{headers:{'Accept':'application/json'}});
     if(!response.ok)throw Error('Reverse geocoding unavailable');const data=await response.json(),a=data.address||{};
     const values={city:a.city||a.town||a.village||a.municipality||a.county||'',state_province:a.state||a.region||a.province||'',country:a.country||'',postal_code:a.postcode||''};
     if(!values.city&&!values.state_province&&!values.country)throw Error('No approximate address was returned');
     for(const [key,value] of Object.entries(values)){const control=document.querySelector('[data-key="'+key+'"]');if(control&&value){control.value=value;control.dispatchEvent(new Event('input',{bubbles:true}));const confirmBox=document.querySelector('[data-confirm="'+key+'"]');if(confirmBox)confirmBox.checked=false;}}
     setStatus('Approximate location filled as an unconfirmed suggestion. Verify the residence or mailing address, then confirm each field and save. Coordinates were not saved. Map data © OpenStreetMap contributors.');
   }catch(e){setStatus('Could not convert the location to an address. Please enter your location manually.');}
   finally{geo.disabled=false;}
 },error=>{if(error.code===error.PERMISSION_DENIED){try{localStorage.setItem('babysteps-location-denied','1')}catch{}}geo.disabled=false;setStatus(error.code===error.PERMISSION_DENIED?'Location permission was denied. You can enter your location manually.':'Location is unavailable or timed out. Please enter your location manually.');},{enableHighAccuracy:false,timeout:10000,maximumAge:0});
};})();
</script></body></html>''')
PROFILE_PAGE = PROFILE_PAGE.replace("chip.remove();const state=wrap?.querySelector('.skill-status');", "chip.remove();if(!wrap?.querySelector('.chip'))checkbox?.closest('.checkrow')?.remove();const state=wrap?.querySelector('.skill-status');",1)
