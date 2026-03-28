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

def get_next_page_url(soup, current_url):
    """Detect and return the next page URL, or None if no next page."""
    next_patterns = ["next", "next page", "›", "»", "load more", "show more"]
    
    for tag in soup.find_all("a", href=True):
        text = tag.get_text(separator=" ", strip=True).lower()
        rel = tag.get("rel", [])
        aria = tag.get("aria-label", "").lower()

        is_next = (
            any(p == text for p in next_patterns) or
            "next" in rel or
            any(p in aria for p in next_patterns)
        )

        if is_next:
            href = tag["href"].strip()
            if href.startswith("http"):
                return href
            else:
                return urljoin(current_url, href)

    # Also check for buttons/spans with rel="next" (some sites use this)
    tag = soup.find(rel="next")
    if tag and tag.get("href"):
        return urljoin(current_url, tag["href"])

    return None

def extract_jobs_from_page(soup, career_url, name, keywords):
    """Extract job listings from a single parsed page."""
    job_signals = ["engineer", "developer", "analyst", "manager", "designer",
                   "scientist", "lead", "director", "intern", "associate",
                   "specialist", "architect", "consultant", "researcher"]
    jobs = []

    for tag in soup.find_all("a", href=True):
        text = tag.get_text(separator=" ", strip=True)
        href = tag["href"].strip()

        if not text or len(text) < 5 or len(text) > 200:
            continue

        text_lower = text.lower()
        is_job = any(s in text_lower for s in job_signals)
        matches_keyword = any(k in text_lower for k in keywords) if keywords else True

        if not (is_job and matches_keyword):
            continue

        if href.startswith("http"):
            full_url = href
        elif href.startswith("/"):
            parsed = urlparse(career_url)
            full_url = f"{parsed.scheme}://{parsed.netloc}{href}"
        else:
            continue

        parent = tag.find_parent()
        description = parent.get_text(separator=" ", strip=True)[:200] if parent else ""

        jobs.append({"title": text, "url": full_url, "description": description, "company": name})

    return jobs

def scrape_jobs(company):
    name = company["name"]
    career_url = company["career_url"]
    keywords = [k.lower() for k in company.get("keywords", [])]

    all_jobs = []
    seen_urls = set()
    current_url = career_url
    page_num = 1

    while current_url and page_num <= MAX_PAGES:
        log.info(f"[{name}] Scraping page {page_num}: {current_url}")
        try:
            resp = requests.get(current_url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
        except Exception as e:
            log.warning(f"[{name}] Failed to fetch page {page_num}: {e}")
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        page_jobs = extract_jobs_from_page(soup, career_url, name, keywords)

        # Deduplicate across pages
        for j in page_jobs:
            if j["url"] not in seen_urls:
                seen_urls.add(j["url"])
                all_jobs.append(j)

        next_url = get_next_page_url(soup, current_url)

        # Stop if next URL is same as current (infinite loop guard)
        if next_url == current_url:
            break

        current_url = next_url
        page_num += 1
        time.sleep(1)  # polite delay between pages

    log.info(f"[{name}] Total: {len(all_jobs)} job(s) across {page_num} page(s).")
    return all_jobs

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    resp = requests.post(url, json=payload, timeout=10)
    resp.raise_for_status()
    log.info("Telegram message sent.")

def format_messages(new_jobs):
    header = f"🚀 *{len(new_jobs)} New Job(s) Found*\n_{datetime.now().strftime('%b %d, %H:%M')}_\n\n"
    messages = []
    current = header

    for j in new_jobs:
        chunk = (
            f"🏢 *{j['company']}*\n"
            f"💼 {j['title']}\n"
            f"🔗 {j['url']}\n"
            f"📝 _{j['description'][:120]}..._\n"
            f"{'─' * 28}\n\n"
        )
        if len(current) + len(chunk) > 4000:
            messages.append(current)
            current = f"_(continued)_\n\n{chunk}"
        else:
            current += chunk

    if current.strip():
        messages.append(current)
    return messages

def run():
    log.info("=== Starting job scan ===")
    companies = load_companies()
    seen = load_seen_jobs()
    new_jobs = []

    for company in companies:
        jobs = scrape_jobs(company)
        for job in jobs:
            jid = job_id(company["name"], job["title"], job["url"])
            if jid not in seen:
                seen.add(jid)
                new_jobs.append(job)
                log.info(f"  NEW: [{company['name']}] {job['title']}")

    save_seen_jobs(seen)

    if new_jobs:
        log.info(f"Sending {len(new_jobs)} new job(s) via Telegram...")
        for msg in format_messages(new_jobs):
            send_telegram(msg)
            time.sleep(1)
    else:
        log.info("No new jobs this cycle.")

    log.info("=== Scan complete ===")

if __name__ == "__main__":
    run()
