import re
import pymupdf
from pathlib import Path


def read_resume(filename):
    path = Path(filename)
    if not path.exists():
        raise FileNotFoundError(f"Resume not found: {path}")
    texts, links = [], []
    with pymupdf.open(path) as doc:
        for page in doc:
            texts.append(page.get_text())
            links.extend(link.get("uri") or "" for link in page.get_links())
    text = "\n".join(texts)
    email = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text)
    phone = re.search(r"\+?\d[\d\s().-]{8,}\d", text)
    github = next((url for url in links if "github.com/" in url.lower()), "")
    if not github:
        found = re.search(r"(?:https?://)?(?:www\.)?github\.com/[\w-]+", text, re.I)
        github = found.group(0) if found else ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return {
        "full_name": lines[0] if lines else "",
        "email": email.group(0) if email else "",
        "phone": phone.group(0).strip() if phone else "",
        "github": github,
        "resume_text": text,
    }
