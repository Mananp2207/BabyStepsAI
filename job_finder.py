import requests
from config import write_json

BOARDS = {"Cloudflare": "cloudflare", "Datadog": "datadog", "Figma": "figma"}


def find_jobs():
    found, seen = [], set()
    for company, token in BOARDS.items():
        try:
            response = requests.get(
                f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
                params={"content": "true"}, timeout=30)
            response.raise_for_status()
            jobs = response.json().get("jobs", [])
            for job in jobs:
                job_id = str(job.get("id", ""))
                url = job.get("absolute_url", "")
                key = (company, job_id or url)
                if not url or key in seen:
                    continue
                seen.add(key)
                found.append({
                    "company": company, "job_id": job_id,
                    "title": job.get("title", ""),
                    "location": (job.get("location") or {}).get("name", ""),
                    "url": url, "description_html": job.get("content") or "",
                })
            print(f"Checked {company}: {len(jobs)} jobs")
        except (requests.RequestException, ValueError) as exc:
            print(f"Could not check {company}: {exc}")
    write_json("discovered_jobs.json", found)
    print(f"Discovered {len(found)} unique postings. Saved discovered_jobs.json")
    return found


if __name__ == "__main__":
    find_jobs()
