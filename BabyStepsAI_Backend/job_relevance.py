"""Platform-independent normalization and relevance checks for job records."""

import re
from datetime import datetime, timezone


ROLE_FAMILIES = {
    "software development": (
        "software engineer", "software developer", "software development engineer", "software engineering", "swe",
        "backend engineer", "back-end engineer", "backend developer", "back-end developer",
        "backend software engineer", "front end engineer", "front-end engineer", "frontend engineer",
        "front end developer", "front-end developer", "frontend developer",
        "full stack engineer", "full-stack engineer", "fullstack engineer",
        "full stack developer", "full-stack developer", "fullstack developer",
        "application developer", "mobile software engineer", "mobile application developer", "software security engineer",
        "web application developer", "embedded software engineer", "systems software engineer",
        "ios engineer", "android engineer", "firmware engineer", "computer vision engineer",
        "distributed systems engineer", "site reliability engineer", "devops engineer",
        "cloud software engineer", "platform software engineer", "qa automation engineer", "sdet",
        "platform developer", "infrastructure software developer", "application engineer",
    ),
    "cybersecurity": (
        "cybersecurity engineer", "cyber security engineer", "security engineer",
        "product security engineer", "application security engineer", "cloud security engineer",
        "security analyst", "cybersecurity analyst", "soc analyst", "penetration tester",
        "penetration testing engineer", "threat intelligence analyst", "security researcher",
        "vulnerability researcher", "incident response analyst",
        "red team engineer", "red team researcher", "security research engineer", "ai security engineer",
    ),
    "artificial intelligence": (
        "machine learning engineer", "ml engineer", "ai engineer", "artificial intelligence engineer",
        "research engineer", "applied scientist", "deep learning engineer", "llm engineer",
        "generative ai engineer", "computer vision engineer", "machine learning scientist",
    ),
    "data and analytics": (
        "data engineer", "analytics engineer", "data scientist", "data analyst",
        "business intelligence engineer", "business intelligence analyst",
    ),
    "cloud and infrastructure": (
        "cloud engineer", "cloud infrastructure engineer", "devops engineer",
        "site reliability engineer", "platform engineer", "infrastructure software engineer",
    ),
    "systems and robotics": (
        "robotics software engineer", "robotics engineer", "embedded systems engineer",
        "firmware engineer", "systems software engineer", "distributed systems engineer",
    ),
    "testing and quality": (
        "qa engineer", "quality assurance engineer", "test automation engineer",
        "software test engineer", "sdet", "quality engineer",
    ),
    "specialized computing": ("quantum computing engineer", "bioinformatics engineer"),
    "technology leadership": ("engineering manager", "technical product manager"),
    "it and support": ("systems administrator", "technical support engineer", "help desk technician"),
}

CLEARLY_UNRELATED = re.compile(
    r"\b(business development representative|sales development representative|account executive|"
    r"business development|sales development|recruiter|talent acquisition|customer success manager|"
    r"sales manager|sales representative|account manager|sales engineer|solutions engineer|"
    r"customer engineer|customer success engineer|marketing specialist|financial analyst|"
    r"administrative assistant|cybersecurity analyst|cyber security analyst|security researcher|"
    r"security research|red team|penetration tester)\b", re.I
)
COUNTRY_ALIASES = {
    "united states": ("united states", "usa", "u.s.a.", "u.s."),
    "united kingdom": ("united kingdom", "uk", "u.k.", "england", "scotland", "wales"),
    "south korea": ("south korea", "republic of korea", "korea"),
    "canada": ("canada",), "australia": ("australia",), "india": ("india",),
    "germany": ("germany",), "france": ("france",), "ireland": ("ireland",),
    "netherlands": ("netherlands", "the netherlands"), "singapore": ("singapore",),
    "japan": ("japan",), "israel": ("israel",), "spain": ("spain",),
    "sweden": ("sweden",), "norway": ("norway",), "denmark": ("denmark",),
    "switzerland": ("switzerland",), "italy": ("italy",), "brazil": ("brazil",),
    "mexico": ("mexico",), "poland": ("poland",), "portugal": ("portugal",),
    "new zealand": ("new zealand",), "china": ("china",), "taiwan": ("taiwan",),
    "south africa": ("south africa",), "united arab emirates": ("united arab emirates", "uae"),
}
US_STATES = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar", "california": "ca",
    "colorado": "co", "connecticut": "ct", "delaware": "de", "florida": "fl", "georgia": "ga",
    "hawaii": "hi", "idaho": "id", "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks",
    "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md", "massachusetts": "ma",
    "michigan": "mi", "minnesota": "mn", "mississippi": "ms", "missouri": "mo", "montana": "mt",
    "nebraska": "ne", "nevada": "nv", "new hampshire": "nh", "new jersey": "nj", "new mexico": "nm",
    "new york": "ny", "north carolina": "nc", "north dakota": "nd", "ohio": "oh", "oklahoma": "ok",
    "oregon": "or", "pennsylvania": "pa", "rhode island": "ri", "south carolina": "sc",
    "south dakota": "sd", "tennessee": "tn", "texas": "tx", "utah": "ut", "vermont": "vt",
    "virginia": "va", "washington": "wa", "west virginia": "wv", "wisconsin": "wi", "wyoming": "wy",
    "district of columbia": "dc",
}
US_STATE_NAMES_BY_ABBREVIATION = {abbreviation: name for name, abbreviation in US_STATES.items()}
US_CITY_STATE = re.compile(
    r"\b(austin|dallas|houston|san antonio|new york|new york city|los angeles|san francisco|"
    r"san diego|seattle|boston|chicago|atlanta|denver|miami|portland|phoenix|detroit|"
    r"philadelphia|washington dc|washington d c|raleigh|pittsburgh|salt lake city)\b", re.I
)
CITY_COUNTRIES = {
    "london": "united kingdom", "manchester": "united kingdom", "edinburgh": "united kingdom",
    "seoul": "south korea", "tokyo": "japan", "osaka": "japan", "berlin": "germany",
    "munich": "germany", "paris": "france", "toronto": "canada", "vancouver": "canada",
    "montreal": "canada", "sydney": "australia", "melbourne": "australia", "bangalore": "india",
    "bengaluru": "india", "dublin": "ireland", "amsterdam": "netherlands", "singapore": "singapore",
    "tel aviv": "israel", "madrid": "spain", "stockholm": "sweden", "oslo": "norway",
    "copenhagen": "denmark", "zurich": "switzerland", "rome": "italy", "sao paulo": "brazil",
    "mexico city": "mexico", "warsaw": "poland", "lisbon": "portugal", "auckland": "new zealand",
    "taipei": "taiwan", "shanghai": "china", "beijing": "china", "johannesburg": "south africa",
    "dubai": "united arab emirates",
}
def normalize_text(value):
    return re.sub(r"[^a-z0-9+#]+", " ", str(value or "").casefold()).strip()


def _arrangement(value):
    text=normalize_text(value)
    if re.search(r"\b(remote|work from home|telecommut(?:e|ing))\b",text):return "Remote"
    if re.search(r"\bhybrid\b",text):return "Hybrid"
    if re.search(r"\b(on site|onsite|in office|in person|office based)\b",text):return "On-site"
    return ""


def _arrangement_only(value):
    text=normalize_text(value)
    return bool(text and re.fullmatch(r"(?:remote|work from home|telecommute|telecommuting|hybrid|on site|onsite|in office|in person|office based)",text))


def _employment_type(value):
    """Keep contract/time classifications separate from workplace arrangement."""
    text=normalize_text(value)
    if not text or _arrangement_only(text):return ""
    if re.search(r"\b(intern|internship|co op|coop)\b",text):return "Internship"
    if re.search(r"\b(part time|parttime)\b",text):return "Part-time"
    if re.search(r"\b(full time|fulltime)\b",text):return "Full-time"
    if re.search(r"\b(contract|contractor)\b",text):return "Contract"
    if re.search(r"\b(temporary|temp)\b",text):return "Temporary"
    if re.search(r"\b(freelance|freelancer)\b",text):return "Freelance"
    return ""


def normalize_job_record(record, source="unknown", company=None, job_key=None):
    """Adapt connector-specific posting fields to the shared job record shape."""
    location = record.get("location")
    if isinstance(location, dict):
        location = location.get("name", "")
    raw_employment=record.get("employment_type") or record.get("employmentType")
    raw_arrangement=(record.get("work_arrangement") or record.get("workplaceType") or
                     record.get("workplace_type") or record.get("locationType") or "")
    metadata = record.get("metadata") if isinstance(record.get("metadata"), list) else []
    metadata_employment = " ".join(
        str(item.get("value", "")) for item in metadata
        if isinstance(item, dict) and "employment" in str(item.get("name", "")).casefold()
    )
    metadata_location = " ".join(
        str(item.get("value", "")) for item in metadata
        if isinstance(item, dict) and any(term in str(item.get("name", "")).casefold()
                                          for term in ("location", "country", "state", "office", "region"))
        and not _arrangement_only(item.get("value", ""))
    )
    if not raw_employment and metadata_employment:raw_employment=metadata_employment
    if not raw_arrangement:
        raw_arrangement=next((item.get("value") for item in metadata if isinstance(item,dict) and
            any(term in str(item.get("name","")).casefold() for term in ("workplace","work arrangement","remote","onsite"))),"")
    if _arrangement_only(location):
        raw_arrangement=raw_arrangement or location
        location=""
    raw_arrangement=raw_arrangement or raw_employment
    return {
        "source": source,
        "external_id": str(record.get("id") or record.get("external_id") or ""),
        "job_key": job_key or record.get("job_key"),
        "company": company or record.get("company") or record.get("company_name") or "",
        "title": str(record.get("title") or record.get("name") or ""),
        "description": str(record.get("description") or record.get("content") or ""),
        "location": str(location or ""),
        "metadata_location": metadata_location,
        "locations": record.get("locations") or record.get("offices") or [],
        "url": str(record.get("url") or record.get("absolute_url") or record.get("apply_url") or ""),
        "employment_type": _employment_type(raw_employment),
        "work_arrangement": _arrangement(raw_arrangement),
        "metadata": metadata,
    }


def category_names(preferences):
    return [str(value).strip().casefold() for value in preferences.get("categories", [])
            if isinstance(value, str) and value.strip()]


def role_aliases(preferences):
    selected = []
    roles = [str(role).strip() for role in preferences.get("roles", []) if isinstance(role, str) and role.strip()]
    categories = category_names(preferences)
    for role in roles:
        normalized = normalize_text(role)
        selected.append(role)
        for category, aliases in ROLE_FAMILIES.items():
            alias_norms = {normalize_text(alias) for alias in aliases}
            belongs_to_family = normalized in alias_norms or (
                len(normalized) >= 10 and any(normalized in alias for alias in alias_norms)
            ) or any(alias in normalized for alias in alias_norms)
            if belongs_to_family:
                selected.extend(aliases)
                break
    for category in categories:
        selected.extend(ROLE_FAMILIES.get(category, ()))
    return list(dict.fromkeys(selected))


def relevance_for_job(job, preferences, prompt=""):
    """Return relevant, reason, and matched title/description evidence.

    Natural-language search text supplies a role only when saved role/category
    preferences are empty. Saved preferences and their exclusions remain in force.
    """
    title = str(job.get("title", ""))
    description = str(job.get("description") or job.get("content") or "")
    # Explicitly commercial or non-target roles are filtered before a broad
    # technical keyword elsewhere in the description can make them appear relevant.
    if CLEARLY_UNRELATED.search(title):
        # Explicitly different job families stay out of a broad technical
        # search, while a user who selected that role family can search it.
        selected_roles = [normalize_text(role) for role in preferences.get("roles", [])
                          if isinstance(role, str) and role.strip()]
        selected_categories = category_names(preferences)
        normalized_title = normalize_text(title)
        explicitly_selected = any(role and role in normalized_title for role in selected_roles)
        cybersecurity_title = bool(re.search(r"\b(cybersecurity|cyber security|security research|red team|penetration test)\b", title, re.I))
        if cybersecurity_title and ("cybersecurity" in selected_categories or any("security" in role for role in selected_roles)):
            explicitly_selected = True
        if not explicitly_selected:
            return False, "unrelated_role", {"basis":"unrelated_title","matched_aliases":[]}
    aliases = role_aliases(preferences)
    if not aliases:
        prompt_prefs = {"roles": [], "categories": []}
        prompt_lower = str(prompt or "").casefold()
        for category, category_aliases in ROLE_FAMILIES.items():
            trigger = category.split()[0]
            if trigger in prompt_lower or any(normalize_text(a) in normalize_text(prompt_lower) for a in category_aliases):
                prompt_prefs["categories"].append(category)
        aliases = role_aliases(prompt_prefs)
        if not aliases:
            # The prompt may contain a specific role without a family trigger.
            aliases = [str(prompt).strip()] if str(prompt).strip() else []

    normalized_title = normalize_text(title)
    generic_terms = {"engineer", "developer", "programmer", "analyst", "specialist", "technician", "scientist"}
    normalized_aliases = [(alias, normalize_text(alias)) for alias in aliases
                          if normalize_text(alias) and normalize_text(alias) not in generic_terms]
    exact = [alias for alias, normalized in normalized_aliases
             if normalized and (bool(re.search(r"\b" + re.escape(normalized) + r"\b", normalized_title))
                                if len(normalized) <= 4 else normalized in normalized_title)]
    if exact:
        return True, None, {"basis": "title_alias", "matched_aliases": exact}

    description_norm=normalize_text(description)
    described=[alias for alias,normalized in normalized_aliases if normalized and normalized in description_norm]
    if described:
        return True, "role evidence appears in the description; title is ambiguous", {
            "basis":"description_alias","matched_aliases":described}
    # Preserve generic technical titles for review rather than treating an
    # incomplete title as proof that the role is unrelated.
    if re.search(r"\b(engineer|developer|programmer|software|technology|technical)\b",title,re.I):
        return True, "technical role title is ambiguous and needs review", {
            "basis":"ambiguous_title","matched_aliases":[]}
    return False, "unrelated_role", {"basis": "no_selected_role_evidence", "matched_aliases": []}


def _location_values(job):
    values = [job.get("location", ""), job.get("metadata_location", "")]
    values=[value for value in values if not _arrangement_only(value)]
    extras = job.get("locations") or []
    if isinstance(extras, (list, tuple)):
        for item in extras:
            values.append(item.get("name", "") if isinstance(item, dict) else item)
    elif isinstance(extras, dict):
        values.extend(str(value) for value in extras.values())
    metadata = job.get("metadata") or []
    if isinstance(metadata, list):
        values.extend(str(item.get("value", "")) for item in metadata if isinstance(item, dict)
                      and any(term in str(item.get("name", "")).casefold()
                              for term in ("location", "country", "state", "office", "region")))
    return [str(value).strip() for value in values if value and not _arrangement_only(value)]


def _contains_alias(text, alias):
    return bool(re.search(r"(?<![a-z])" + re.escape(alias.casefold()) + r"(?![a-z])", text.casefold()))


def _country_hits(values):
    text = " | ".join(values)
    hits = set()
    for country, aliases in COUNTRY_ALIASES.items():
        for alias in aliases:
            if _contains_alias(text, alias):
                hits.add(country)
                break
    for city, country in CITY_COUNTRIES.items():
        if _contains_alias(text, city):
            hits.add(country)
    # State names and common city/state forms are explicit US evidence.
    state_names = [name for name in US_STATES if name not in {"georgia", "washington"}]
    if any(_contains_alias(text, name) for name in state_names):
        hits.add("united states")
    if re.search(r"\bwashington\s+state\b|\bgeorgia\s*,\s*(?:ga|united states|usa)\b", text, re.I):
        hits.add("united states")
    if any(normalize_text(value) in {"us", "u s", "usa", "u s a"} for value in values):
        hits.add("united states")
    foreign_explicit = hits - {"united states"}
    if US_CITY_STATE.search(text) and not foreign_explicit:
        hits.add("united states")
    if re.search(r"\b(remote|based)\s*(in\s*)?(us|u\.s\.|usa|united states)\b|\b(us|usa|united states)\s*(remote|based)\b", text, re.I):
        hits.add("united states")
    # State abbreviations need a location-like delimiter to avoid matching
    # ordinary words (e.g. "in", "or", "me") in descriptions.
    if re.search(r",\s*(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)(?:\b|\s*,)", text):
        hits.add("united states")
    return hits


def _state_hits(values, *, allow_bare_abbreviation=False):
    text=" | ".join(str(value or "") for value in values)
    hits=set()
    for name,abbreviation in US_STATES.items():
        if _contains_alias(text,name):
            hits.add(name)
        if re.search(r"(?:,|\b)\s*"+re.escape(abbreviation)+r"\s*(?:,|\b|$)",text,re.I):
            hits.add(name)
        if allow_bare_abbreviation and normalize_text(text)==abbreviation:
            hits.add(name)
    return hits


def evaluate_location(job, requested_location):
    """Return eligible/mismatch/unknown from explicit location evidence.

    This is target-driven and extensible: country targets use the shared country
    alias map; city/region targets use explicit textual mentions. Arrangement
    labels alone (Remote/Hybrid/In-Office) never establish a country.
    """
    target = str(requested_location or "").strip().casefold()
    if not target or target in {"any", "anywhere", "global"}:
        return "eligible", "No geographic restriction is saved."
    values = _location_values(job)
    text = " | ".join(values)
    countries = _country_hits(values)
    description = str(job.get("description") or job.get("content") or "")
    target_states=_state_hits([requested_location],allow_bare_abbreviation=True)
    if target_states:
        posting_states=_state_hits(values)
        matching=target_states & posting_states
        if matching:
            state=sorted(matching)[0].title()
            return "eligible", f"Explicit posting location matches {state}."
        if posting_states:
            states=", ".join(sorted(item.title() for item in posting_states))
            return "mismatch", f"Posting location indicates {states}, not the selected state preference."
        return "unknown", f"A {', '.join(sorted(x.title() for x in target_states))} preference is saved, but the posting does not identify a state."
    target_country = next((country for country, aliases in COUNTRY_ALIASES.items()
                           if target == country or target in aliases), None)
    if target_country:
        if target_country in countries:
            return "eligible", f"Explicit location evidence includes {target_country.title()}."
        if countries:
            return "mismatch", f"Posting location indicates {', '.join(sorted(countries))}, not {target_country.title()}."
        contextual = re.search(
            r"\b(?:locations?|based in|located in|work in|work location|remote in|position in|role in)\b\s*[:\-]?\s*(.{0,180})",
            description, re.I | re.S)
        if contextual:
            described_countries = _country_hits([contextual.group(1)])
            if target_country in described_countries:
                return "eligible", f"Job description lists a {target_country.title()} work location."
            if described_countries:
                return "mismatch", f"Job description lists {', '.join(sorted(described_countries))}, not {target_country.title()}."
        # Location can be absent or only an arrangement label. A country-level
        # target in description counts only when stated as a work location.
        if re.search(r"\b(remote|based|position|role|job|work location|work)\b.{0,45}\b(?:in\s+)?(?:united states|usa|u\.s\.|us)\b", description, re.I):
            return "eligible", f"Description explicitly identifies {target_country.title()} work location."
        if re.search(r"\b(remote|based|position|role|job|work location|work)\b.{0,45}\b(?:in\s+)?(?:korea|south korea|canada|united kingdom|uk|london|india|germany|france)\b", description, re.I):
            return "mismatch", "Description identifies a work location outside the selected country."
        return "unknown", f"Country cannot be verified from the listed location; {target_country.title()} eligibility needs review."
    normalized_target = normalize_text(target)
    if normalized_target and normalized_target in normalize_text(text):
        return "eligible", f"Posting explicitly mentions the requested location ({requested_location})."
    if normalized_target and re.search(
        r"\b(?:locations?|based in|located in|work in|work location|remote in|position in|role in)\b\s*[:\-]?\s*.{0,100}" + re.escape(normalized_target),
        description, re.I | re.S):
        return "eligible", f"Job description lists the requested location ({requested_location})."
    if countries:
        return "mismatch", f"Posting location does not match the requested location ({requested_location})."
    return "unknown", f"Posting location cannot be verified against the requested location ({requested_location})."


def prompt_constraints(prompt):
    """Extract explicit, safely enforceable refinements from a search prompt."""
    text = str(prompt or "").casefold()
    constraints = {"location": "", "employment_types": [], "levels": [],
                   "exclude_internships": False, "exclude_senior": False}
    for country, aliases in COUNTRY_ALIASES.items():
        if any(_contains_alias(text, alias) for alias in aliases):
            constraints["location"] = country.title()
            break
    if not constraints["location"]:
        for state in US_STATES:
            if _contains_alias(text, state):
                constraints["location"] = state.title()
                break
    if re.search(r"\b(full[- ]time|full time positions?)\b", text):
        constraints["employment_types"].append("Full-time")
    if re.search(r"\b(include|only|looking for|find)\s+(?:paid\s+)?internships?\b", text):
        constraints["employment_types"].append("Internship")
    constraints["exclude_internships"] = bool(re.search(r"\b(exclude|no|without|skip)\b[^.!?]{0,100}\binternships?\b", text))
    constraints["exclude_senior"] = bool(re.search(r"\b(exclude|no|without|skip)\b[^.!?]{0,100}\b(?:senior|staff|principal|lead|director|manager)\b", text))
    early_patterns = (
        (r"\bnew[- ]grads?|new graduates?|recent grads?|class of 20\d\d|graduat(?:e|ing) in 20\d\d\b", "New Graduate"),
        (r"\bentry[- ]level\b", "Entry Level"),
        (r"\bjunior\b", "Junior"), (r"\bassociate\b", "Associate"),
        (r"\bsoftware engineer\s+i\b|\bswe\s+i\b", "Entry Level"),
    )
    for pattern, level in early_patterns:
        if re.search(pattern, text): constraints["levels"].append(level)
    if re.search(r"\b(senior|staff|principal|lead|director|manager)\b", text) and not constraints["exclude_senior"]:
        constraints["levels"].append("Senior")
    constraints["levels"] = list(dict.fromkeys(constraints["levels"]))
    constraints["employment_types"] = list(dict.fromkeys(constraints["employment_types"]))
    return constraints


def apply_prompt_preferences(prompt, preferences, *, ignore_fields=()):
    """Return workflow-scoped preferences; never persist or mutate saved choices."""
    effective = dict(preferences or {})
    constraints = prompt_constraints(prompt)
    ignored = set(ignore_fields)
    if "location" not in ignored and constraints["location"]:
        effective["location"] = constraints["location"]
    if "employment_types" not in ignored and constraints["employment_types"]:
        effective["employment_types"] = constraints["employment_types"]
    if "levels" not in ignored and constraints["levels"]:
        effective["levels"] = constraints["levels"]
    effective["_prompt_exclude_internships"] = constraints["exclude_internships"] and "employment_types" not in ignored
    effective["_prompt_exclude_senior"] = constraints["exclude_senior"] and "levels" not in ignored
    effective["search_prompt"] = str(prompt or "")
    return effective


def infer_prompt_preferences(prompt, preferences):
    """Compatibility wrapper for existing callers."""
    saved=preferences or {}
    ignore=[field for field in ("levels","employment_types") if saved.get(field)]
    location=str(saved.get("location") or "").casefold()
    if location and location not in {"any","anywhere","global"}:ignore.append("location")
    effective=apply_prompt_preferences(prompt, saved, ignore_fields=ignore)
    constraints=prompt_constraints(prompt)
    effective["_prompt_exclude_internships"]=constraints["exclude_internships"]
    effective["_prompt_exclude_senior"]=constraints["exclude_senior"]
    return effective


def extract_experience_requirements(description):
    """Extract explicit year-based experience statements without grading a candidate.

    Text is labeled required/preferred/unclear from nearby language; no years are
    inferred from projects, education, or unrelated date references.
    """
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(description or "")))
    pattern = re.compile(
        r"(?P<years>\b\d{1,2}(?:\s*[-–]\s*\d{1,2})?\+?)\s+years?\s+(?:of\s+)?"
        r"(?:(?:professional|industry|relevant|related|work|software|engineering|technical|development)\s+){0,3}experience\b", re.I)
    output = {"required": [], "preferred": [], "unclear": []}
    headings=re.compile(r"\b(?:nice to have|bonus points|preferred qualifications|preferred experience|the skillset you(?:'|’)ll bring|what you(?:'|’)ll bring|requirements|required qualifications|minimum qualifications)\b",re.I)
    preferred_headings={"nice to have","bonus points","preferred qualifications","preferred experience"}
    required_headings={"requirements","required qualifications","minimum qualifications"}
    for match in pattern.finditer(text):
        start = max(0, text.rfind(".", 0, match.start()) + 1)
        end = text.find(".", match.end())
        if end < 0:
            end = min(len(text), match.end() + 180)
        sentence = text[start:end].strip()
        preceding=[heading.group(0).casefold().replace("’","'") for heading in headings.finditer(text,0,match.start())]
        section=preceding[-1] if preceding else ""
        section=section.replace("’","'")
        if section in preferred_headings:
            category="preferred"
        elif section in required_headings or "skillset you" in section or "what you" in section:
            category="required"
        else:
            before=text[max(0,match.start()-100):match.start()].casefold()
            after=text[match.end():min(len(text),match.end()+100)].casefold()
            if re.search(r"\b(?:must|required|requires?|minimum|at least)\b[^.;]{0,80}$",before):
                category="required"
            elif re.search(r"^\s*(?:(?:of\s+professional\s+)?experience\s+)?(?:is\s+)?(?:preferred|desired)\b",after) or re.search(r"\b(?:preferred|desired)\s+experience\b",after):
                category="preferred"
            elif re.search(r"\b(?:preferred|nice to have|bonus|desirable)\b",before[-70:]):
                category="preferred"
            else:
                category="unclear"
        item = {"requirement": match.group(0), "years": match.group("years"), "status": category}
        if item not in output[category]:
            output[category].append(item)
    return output


def extract_graduation_windows(description):
    """Return explicit employer graduation windows as evidence, without inferring eligibility."""
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(description or "")))
    month_names=r"January|February|March|April|May|June|July|August|September|October|November|December|Jan\.?|Feb\.?|Mar\.?|Apr\.?|Jun\.?|Jul\.?|Aug\.?|Sep\.?|Sept\.?|Oct\.?|Nov\.?|Dec\.?"
    month_numbers={"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,"jul":7,"aug":8,"sep":9,"sept":9,"oct":10,"nov":11,"dec":12}
    month_pattern=re.compile(rf"\b(?:graduate|graduating|graduated)\s+(?:(?:in|by)\s+)?({month_names})\s+(20\d{{2}})\b",re.I)
    windows=[]
    for match in month_pattern.finditer(text):
        month=month_numbers[match.group(1)[:3].casefold()]
        item={"start_year":int(match.group(2)),"end_year":int(match.group(2)),
              "start_month":month,"end_month":month,"requirement":match.group(0)}
        if item not in windows:windows.append(item)
    patterns = (
        re.compile(r"\b(?:class(?:es)? of|graduat(?:e|ing) in)\s+(20\d{2})(?:\s*(?:-|–|to|or)\s*(20\d{2}))?\b", re.I),
        re.compile(r"\bmust (?:have )?graduat(?:e|ed) between\s+(20\d{2})\s+and\s+(20\d{2})\b", re.I),
        re.compile(r"\bmust have graduated by\s+(20\d{2})\b", re.I),
        re.compile(r"\bgraduat(?:e|ed) no later than\s+(20\d{2})\b", re.I),
    )
    for index,pattern in enumerate(patterns):
        for match in pattern.finditer(text):
            start, end = ("1900",match.group(1)) if index>=2 else (match.group(1), match.group(2) or match.group(1))
            item = {"start_year": int(start), "end_year": int(end), "requirement": match.group(0)}
            if item not in windows:
                windows.append(item)
    return windows


def extract_education_requirements(description):
    """Extract degree evidence into required/preferred/unclear buckets."""
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(description or "")))
    degree = re.compile(r"\b(?:bachelor(?:'s)?|master(?:'s)?|doctoral?|ph\.?d\.?|b\.?s\.?|b\.?a\.?|m\.?s\.?|m\.?a\.?|degree)\b", re.I)
    headings = re.compile(r"\b(?:nice to have|bonus points|preferred qualifications|preferred experience|requirements|required qualifications|minimum qualifications|basic qualifications|qualifications)", re.I)
    preferred = {"nice to have", "bonus points", "preferred qualifications", "preferred experience"}
    output = {"required": [], "preferred": [], "unclear": []}
    for match in degree.finditer(text):
        start = max(0, text.rfind(".", 0, match.start()) + 1)
        end = text.find(".", match.end())
        if end < 0:
            end = min(len(text), match.end() + 180)
        sentence = text[start:end].strip()
        if not sentence:
            continue
        sections = [item.group(0).casefold() for item in headings.finditer(text, 0, match.start())]
        section = sections[-1] if sections else ""
        if section in preferred:
            bucket = "preferred"
        elif section in {"requirements", "required qualifications", "minimum qualifications", "basic qualifications", "qualifications"}:
            bucket = "required"
        elif re.search(r"\b(?:required|must have|minimum)\b", sentence, re.I):
            bucket = "required"
        elif re.search(r"\b(?:preferred|nice to have|bonus|desired)\b", sentence, re.I):
            bucket = "preferred"
        else:
            bucket = "unclear"
        normalized = sentence[:300]
        if normalized not in output[bucket]:
            output[bucket].append(normalized)
    return output


def classify_career_category(title, preferences=None):
    """Return the matching saved category when title evidence supports one."""
    title_norm = normalize_text(title)
    selected = category_names(preferences or {})
    categories = list(dict.fromkeys(selected + list(ROLE_FAMILIES)))
    for category in categories:
        aliases = ROLE_FAMILIES.get(category, ())
        if any(normalize_text(alias) and normalize_text(alias) in title_norm for alias in aliases):
            return category.title()
    return None


def classify_experience_level(title, description="", employment_type=""):
    """Classify posting seniority from explicit posting evidence; return level and rationale."""
    title_text = str(title or "")
    combined = f"{title_text} {employment_type or ''}"
    requirements = extract_experience_requirements(description)
    reasons = []
    if re.search(r"\b(intern|internship)\b", combined, re.I):
        return "Internship", ["Title or employment metadata identifies an internship."]
    if re.search(r"\b(senior|staff|principal|lead|director|manager|vp|head of)\b", title_text, re.I):
        return "Senior", ["Title identifies a senior or leadership role."]
    if re.search(r"\b(new\s*grad(?:uate)?|university graduate|university recruiting|university hire|campus recruiting|campus hire|graduate program|rotational(?: engineering)? program)\b", combined, re.I):
        reasons.append("Title identifies a new graduate or graduate program.")
        return "New Graduate", reasons
    if re.search(r"\b(entry[- ]level|junior|associate|early career|graduate software engineer|software engineer i)\b", combined, re.I):
        reasons.append("Title identifies an entry-level or early-career role.")
        return "Early Career", reasons
    required = requirements["required"]
    if required:
        lower_bounds = [int(re.match(r"\d+", item["years"]).group()) for item in required]
        if max(lower_bounds) >= 5:
            reasons.append("Required qualifications specify at least 5 years of experience.")
            return "Experienced", reasons
        if any("–" in item["years"] or "-" in item["years"] for item in required):
            reasons.append("Required experience spans a range; seniority is ambiguous.")
            return "Ambiguous", reasons
        if max(lower_bounds) >= 3:
            reasons.append("Required qualifications specify multiple years of experience; the title is not level-specific.")
            return "Mid-Level", reasons
    if extract_graduation_windows(description):
        reasons.append("Description includes a graduation eligibility window.")
        return "Graduate Eligible", reasons
    return "Ambiguous", ["Neither the title nor required qualifications establish an experience level."]


def discovery_relevance_score(job, preferences, confirmed_skills=()):
    """Transparent ranking score; this score never decides eligibility."""
    score = 50
    explanations = ["Role passed the selected role and deterministic preference filters."]
    level, level_reasons = classify_experience_level(
        job.get("title"), job.get("description"), job.get("employment_type"))
    requested = {str(item).casefold() for item in preferences.get("levels", []) if isinstance(item, str)}
    early = {"new graduate", "entry level", "junior", "associate", "early career", "internship"}
    if requested and level.casefold() in requested:
        score += 20
        explanations.append("Posting level matches a selected experience-level preference (+20).")
    elif requested & early and level in {"New Graduate", "Early Career", "Internship", "Graduate Eligible"}:
        score += 15
        explanations.append("Posting has explicit early-career evidence (+15).")
    elif requested & early and level in {"Experienced", "Senior"}:
        score -= 15
        explanations.append("Posting has experienced or senior requirements; ranked lower for an early-career search (-15).")
    title_description = f"{job.get('title', '')} {job.get('description', '')}".casefold()
    aliases = role_aliases(preferences)
    if aliases and any(re.search(r"(?<![a-z0-9])" + re.escape(alias.casefold()) + r"(?![a-z0-9])", str(job.get("title", "")).casefold()) for alias in aliases):
        score += 10
        explanations.append("Title evidence matches a selected career category or role (+10).")
    skill_hits = [str(skill).strip() for skill in confirmed_skills if isinstance(skill, str) and len(skill.strip()) >= 2
                  and re.search(r"(?<![a-z0-9])" + re.escape(skill.strip().casefold()) + r"(?![a-z0-9])", title_description)]
    if skill_hits:
        skill_points = min(15, 5 * len(skill_hits))
        score += skill_points
        explanations.append(f"{len(skill_hits)} confirmed candidate skill(s) appear in the posting (+{skill_points}).")
    if job.get("location_status") == "eligible":
        score += 12
        explanations.append("Posting location matches the selected location (+12).")
    if preferences.get("employment_types"):
        wanted = [str(value).casefold() for value in preferences["employment_types"]]
        observed = str(job.get("employment_type") or "").casefold()
        if observed and any(value in observed for value in wanted):
            score += 8
            explanations.append("Employment type matches a selected preference (+8).")
    posted = job.get("posting_date")
    if posted:
        try:
            parsed = datetime.fromisoformat(str(posted).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - parsed).days
            if 0 <= age <= 7:
                score += 10
                explanations.append("Posting is less than one week old (+10).")
            elif 0 <= age <= 30:
                score += 6
                explanations.append("Posting is less than one month old (+6).")
        except (TypeError, ValueError):
            pass
    return max(0, min(100, score)), explanations, level, level_reasons
