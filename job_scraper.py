import json
import os
import hashlib
import logging
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urljoin
import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
SEEN_JOBS_FILE = Path("seen_jobs.json")
COMPANIES_FILE = Path("companies.json")
MAX_PAGES = 20

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# ── Persistence ───────────────────────────────────────────────────────────────

def load_seen_jobs():
    if SEEN_JOBS_FILE.exists():
        return set(json.loads(SEEN_JOBS_FILE.read_text()))
    return set()

def save_seen_jobs(seen):
    SEEN_JOBS_FILE.write_text(json.dumps(list(seen)))

def load_companies():
    return json.loads(COMPANIES_FILE.read_text())

def job_id(company_name, title, url):
    raw = f"{company_name}|{title.strip().lower()}|{url.strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]

# ── Workday Scraper ───────────────────────────────────────────────────────────

def scrape_workday(company):
    """
    Handles companies whose career_url starts with 'workday:'.
    Format: workday:{tenant}:{wd_server}:{site}
    Example: workday:mastercard:wd1:CorporateCareers
    """
    name = company["name"]
    keywords = [k.lower() for k in company.get("keywords", [])]

    parts = company["career_url"].split(":")
    if len(parts) != 4:
        log.warning(f"[{name}] Invalid workday URL format. Expected workday:tenant:wdN:site")
        return []

    _, tenant, wd_server, site = parts
    api_url = f"https://{tenant}.{wd_server}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    base_job_url = f"https://{tenant}.{wd_server}.myworkdayjobs.com/en-US/{site}"

    headers = {
        **HEADERS,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Referer": base_job_url,
    }

    all_jobs = []
    offset = 0
    limit = 20

    while True:
        payload = {
            "appliedFacets": {},
            "limit": limit,
            "offset": offset,
            "searchText": ""
        }
        try:
            resp = requests.post(api_url, json=payload, headers=headers, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.warning(f"[{name}] Workday API error at offset {offset}: {e}")
            break

        postings = data.get("jobPostings", [])
        if not postings:
            break

        for job in postings:
            title = job.get("title", "").strip()
            path = job.get("externalPath", "")
            full_url = f"https://{tenant}.{wd_server}.myworkdayjobs.com/en-US/{site}{path}"
            description = job.get("locationsText", "") or job.get("timeType", "")

            if not title:
                continue

            title_lower = title.lower()
            matches_keyword = any(k in title_lower for k in keywords) if keywords else True
            if not matches_keyword:
                continue

            all_jobs.append({
                "title": title,
                "url": full_url,
                "description": description,
                "company": name
            })

        total = data.get("total", 0)
        offset += limit
        if offset >= total:
            break

        time.sleep(1)

    log.info(f"[{name}] Workday: found {len(all_jobs)} job(s).")
    return all_jobs

# ── Microsoft Scraper ─────────────────────────────────────────────────────────

def scrape_microsoft(company):
    """
    Handles Microsoft's hidden REST API which returns JSON directly.
    Detects URLs containing jobs.careers.microsoft.com
    """
    name = company["name"]
    keywords = [k.lower() for k in company.get("keywords", [])]
    base_url = company["career_url"]

    all_jobs = []
    page = 1

    while page <= MAX_PAGES:
        url = base_url if page == 1 else base_url + f"&pg={page}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.warning(f"[{name}] Microsoft API error on page {page}: {e}")
            break

        jobs = data.get("operationResult", {}).get("result", {}).get("jobs", [])
        if not jobs:
            break

        for job in jobs:
            title = job.get("title", "").strip()
            job_id_ms = job.get("jobId", "")
            full_url = f"https://jobs.careers.microsoft.com/global/en/job/{job_id_ms}/"
            location = job.get("properties", {}).get("primaryLocation", "")
            description = f"{location} | {job.get('properties', {}).get('employmentType', '')}"

            if not title:
                continue

            title_lower =
