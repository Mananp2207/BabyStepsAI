import re
from pathlib import Path


def read_resume(filename):
    from pypdf import PdfReader

    path = Path(filename)
    if not path.exists():
        raise FileNotFoundError(f"Resume not found: {path}")
    texts, links = [], []
    with path.open("rb") as source:
        reader = PdfReader(source, strict=False)
        if reader.is_encrypted:
            raise ValueError("Password-protected PDF.")
        for page in reader.pages:
            texts.append(page.extract_text() or "")
            for annotation_ref in page.get("/Annots", []):
                annotation = (
                    annotation_ref.get_object()
                    if hasattr(annotation_ref, "get_object")
                    else annotation_ref
                )
                action = annotation.get("/A")
                if action and action.get("/URI"):
                    links.append(str(action["/URI"]))
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
