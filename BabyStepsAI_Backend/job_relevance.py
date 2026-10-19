"""Platform-independent normalization and relevance checks for job records."""

import re


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
    r"administrative assistant)\b", re.I
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


def normalize_job_record(record, source="unknown", company=None, job_key=None):
    """Adapt connector-specific posting fields to the shared job record shape."""
    location = record.get("location")
    if isinstance(location, dict):
        location = location.get("name", "")
    metadata = record.get("metadata") if isinstance(record.get("metadata"), list) else []
    metadata_employment = " ".join(
        str(item.get("value", "")) for item in metadata
        if isinstance(item, dict) and "employment" in str(item.get("name", "")).casefold()
    )
    metadata_location = " ".join(
        str(item.get("value", "")) for item in metadata
        if isinstance(item, dict) and any(term in str(item.get("name", "")).casefold()
                                          for term in ("location", "country", "state", "office", "region"))
    )
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
        "employment_type": str(record.get("employment_type") or record.get("employmentType") or metadata_employment),
        "work_arrangement": str(record.get("work_arrangement") or ""),
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

    if CLEARLY_UNRELATED.search(title):
        return False, "unrelated_role", {"basis": "unrelated_title", "matched_aliases": []}

    return False, "unrelated_role", {"basis": "no_selected_role_evidence", "matched_aliases": []}


def _location_values(job):
    values = [job.get("location", ""), job.get("metadata_location", ""), job.get("title", "")]
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
    return [str(value).strip() for value in values if value]


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


def infer_prompt_preferences(prompt, preferences):
    """Apply prompt-only location/level hints without overriding saved choices."""
    effective = dict(preferences or {})
    text = str(prompt or "").casefold()
    location = str(effective.get("location", "")).strip()
    if not location or location.casefold() in {"any", "anywhere", "global"}:
        for country, aliases in COUNTRY_ALIASES.items():
            if any(_contains_alias(text, alias) for alias in aliases):
                effective["location"] = country.title()
                break
        if not effective.get("location") or effective["location"] == location:
            for state in US_STATES:
                if _contains_alias(text, state):
                    effective["location"] = state.title()
                    break
    if not effective.get("levels"):
        inferred_levels = []
        if re.search(r"\b(new[- ]grads?|new graduates?|recent grads?|class of 20\d\d|graduat(?:e|ing) in 20\d\d)\b", text):
            inferred_levels.append("New Graduate")
        if re.search(r"\b(entry[- ]level|early career|junior|associate)\b", text):
            inferred_levels.extend(["Entry Level", "Junior", "Associate"])
        if re.search(r"\b(senior|staff|principal|manager|director)\b", text):
            inferred_levels.append("Senior")
        if inferred_levels:
            effective["levels"] = list(dict.fromkeys(inferred_levels))
    effective["search_prompt"] = str(prompt or "")
    return effective


def extract_experience_requirements(description):
    """Extract explicit year-based experience statements without grading a candidate.

    Text is labeled required/preferred/unclear from nearby language; no years are
    inferred from projects, education, or unrelated date references.
    """
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(description or "")))
    pattern = re.compile(
        r"(?P<years>\b\d{1,2}(?:\s*[-–]\s*\d{1,2})?\+?)\s+years?\s+(?:of\s+)?"
        r"(?:(?:professional|industry|relevant|related|work)\s+)?experience\b", re.I)
    output = {"required": [], "preferred": [], "unclear": []}
    for match in pattern.finditer(text):
        start = max(0, text.rfind(".", 0, match.start()) + 1)
        end = text.find(".", match.end())
        if end < 0:
            end = min(len(text), match.end() + 180)
        sentence = text[start:end].strip()
        context = sentence.casefold()
        if re.search(r"\b(preferred|preferably|ideally|nice to have|bonus|desirable)\b", context):
            category = "preferred"
        elif re.search(r"\b(must|required|requires?|minimum|at least|requirement)\b", context):
            category = "required"
        else:
            category = "unclear"
        item = {"requirement": sentence[:400], "years": match.group("years"), "status": category}
        if item not in output[category]:
            output[category].append(item)
    return output
