"""Deterministic, unconfirmed resume text parsing."""
import re
PROFILE_LIST_FIELDS = [
    "education",
    "work_experience",
    "projects",
    "technical_skills",
    "professional_skills",
    "certifications",
    "licenses",
    "spoken_languages",
    "unknown_information",
]
PROFILE_SCALAR_FIELDS = ["full_name", "email", "phone", "linkedin", "github"]

def extract_resume_profile(text):
    """Build conservative, unconfirmed profile suggestions from literal PDF text."""
    import re

    lines = [re.sub(r"\s+", " ", line).strip(" •\t") for line in str(text or "").splitlines()]
    lines = [line for line in lines if line]
    profile = {key: [] for key in PROFILE_LIST_FIELDS}
    profile.update({key: "" for key in PROFILE_SCALAR_FIELDS})
    if not lines:
        return profile
    email = re.search(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+(?![\w.-])", text, re.I)
    phone = re.search(r"(?<!\w)(?:\+?\d[\d(). xX-]{7,}\d)(?!\w)", text)
    profile["email"] = email.group(0) if email else ""
    profile["phone"] = re.sub(r"[.,;]+$", "", phone.group(0)) if phone else ""
    for url in re.findall(r"https?://[^\s<>\])}]+", text, re.I):
        clean = url.rstrip(".,;:")
        if "linkedin.com/" in clean.casefold() and not profile["linkedin"]: profile["linkedin"] = clean
        if "github.com/" in clean.casefold() and not profile["github"]: profile["github"] = clean
    first = lines[0]
    if len(first) <= 80 and not re.search(r"@|https?://|\d", first) and len(first.split()) in (2, 3, 4) and not first.isupper():
        profile["full_name"] = first

    aliases = {
        "education": r"education", "work_experience": r"experience|employment|work history",
        "projects": r"projects?", "technical_skills": r"technical skills|technologies|skills",
        "professional_skills": r"professional skills", "certifications": r"certifications?",
        "licenses": r"licenses?", "spoken_languages": r"languages?",
    }
    active = None
    for line in lines:
        heading = re.sub(r"[^a-z ]", "", line.casefold()).strip()
        match = next((key for key, pattern in aliases.items() if re.fullmatch(r"(?:" + pattern + r")(?: and [a-z ]+)?", heading)), None)
        if match:
            active = match
            continue
        if active:
            if active in {"education", "work_experience", "projects"}:
                profile[active].append({"description": line})
            else:
                profile[active].append(line)

    skill_terms = ("Python", "Java", "JavaScript", "TypeScript", "C++", "C#", "Go", "Rust", "SQL", "HTML", "CSS", "React", "Node.js", "FastAPI", "Django", "Flask", "AWS", "Azure", "GCP", "Docker", "Kubernetes", "PostgreSQL", "SQLite", "Git", "Linux", "TensorFlow", "PyTorch")
    profile["technical_skills"] = list(dict.fromkeys(profile["technical_skills"] + [term for term in skill_terms if re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text, re.I)]))
    edu_lines = [line for line in lines if re.search(r"\b(university|college|bachelor|master|ph\.d|gpa|coursework|graduat)\b", line, re.I)]
    if edu_lines and not profile["education"]:
        profile["education"] = [{"description": line} for line in edu_lines]
    if profile["education"]:
        joined = " ".join(edu_lines)
        first_edu = profile["education"][0]
        description = str(first_edu.get("description") or "")
        degree = re.search(r"\b(?:B\.?S\.?|B\.?A\.?|B\.?E\.?|M\.?S\.?|M\.?A\.?|M\.?B\.?A\.?|Ph\.?D\.?|Bachelor(?:'s)?|Master(?:'s)?|Doctorate|Associate(?:'s)?)\b[^,;|]*", description, re.I)
        institution = re.search(r"\b([A-Z][A-Za-z&.' -]{1,70}(?:University|College|Institute))\b", description)
        if degree: first_edu["degree"] = degree.group(0).strip()
        if institution: first_edu["institution"] = institution.group(1).strip()
        major = re.search(r"\b(?:in|of)\s+([^,;|]+)", str(degree.group(0) if degree else ""), re.I)
        if not major and degree:
            major = re.search(r"^(?:B\.?S\.?|B\.?A\.?|B\.?E\.?|M\.?S\.?|M\.?A\.?|M\.?B\.?A\.?|Ph\.?D\.?)\s+(.+)$", degree.group(0), re.I)
        if major: first_edu["major"] = major.group(1).strip()
        gpa = re.search(r"\bGPA\s*[:\-]?\s*(\d(?:\.\d{1,2})?(?:\s*/\s*\d(?:\.\d{1,2})?)?)", joined, re.I)
        first_edu["gpa"] = gpa.group(1) if gpa else ""
        courses = next((re.split(r"[,;|]", re.sub(r"^.*?coursework\s*[:\-]?\s*", "", line, flags=re.I)) for line in edu_lines if re.search(r"coursework", line, re.I)), [])
        first_edu["coursework"] = [x.strip() for x in courses if x.strip()]
        year = re.search(r"\b(20\d{2})\b", joined)
        if year: first_edu["graduation_year"] = year.group(1)
    internships = [item for item in profile["work_experience"] if re.search(r"intern", item.get("description", ""), re.I)]
    profile["internships"] = internships
    profile["work_experience"] = [item for item in profile["work_experience"] if item not in internships]
    return profile
