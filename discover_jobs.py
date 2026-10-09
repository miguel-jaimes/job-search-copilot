# Job-Search Copilot — Phase 1: Discovery Engine
#
# Pulls live job openings from your target companies that use Greenhouse, Lever,
# or Workday, filters them to the roles you care about (and drops stale postings),
# scores a rough fit, and writes a ranked CSV feed.
#
# WHY YOU RUN THIS LOCALLY: the cloud environment this was built in blocks these
# job-board endpoints. On your own machine there's no such block, so it just works.
#
# ---------------------------------------------------------------------------
# SETUP (one time):
#   1. Install Python 3.9+  (check: python --version)
#   2. pip install httpx
#   3. Put companies.json next to this file (already provided)
#
# RUN:
#   python discover_jobs.py                 # uses defaults below
#   python discover_jobs.py --verify        # re-check which slugs are valid
#   python discover_jobs.py --location Texas # narrow to one location string
#   python discover_jobs.py --max-age 45     # only postings from the last 45 days
#
# OUTPUT:
#   job_feed.csv   — ranked list of matching openings, best fit first
# ---------------------------------------------------------------------------

import argparse
import csv
import json
import re
import sys
import time

from us_location import is_non_us
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

try:
    import httpx
except ImportError:
    sys.exit("Missing dependency. Run:  pip install httpx")

# ---------------------------------------------------------------------------
# WHAT COUNTS AS A MATCH  — edit these to taste
# ---------------------------------------------------------------------------

# A title must contain at least one of these to be kept. (Tightened: dropped the
# vague 'operations' and 'strategy' that were catching security/ops-manager roles.)
TITLE_KEYWORDS = [
    "analyst", "analytics", "business intelligence", " bi ",
    "data analyst", "data science", "insights", "reporting",
]

# HARD excludes: if a title contains any of these, it's always dropped
# (too senior, or a field you don't do). Matched as plain substrings, so
# "manager" also catches "managerial".
TITLE_EXCLUDE = [
    "senior", "staff", "principal", "lead", "manager", "director",
    "head of", "iii", " ii", "phd",
    "security", "reliability", "software engineer", "machine learning engineer",
    "counsel", "attorney",
]

# FIELD words: they usually mean a sales/marketing/recruiting job ("Account
# Executive, Analytics"), so they drop a title... UNLESS the title is an analyst
# role. Then the business area doesn't matter: "Marketing Data Analyst" and
# "Sales Operations Analyst" are analytics jobs, so they go on to the AI scorer,
# which judges the actual fit. (Before Oct 8 these were hard excludes, which
# silently dropped TI's "Web Marketing Program - Digital Experience Data Analyst".)
TITLE_EXCLUDE_FIELD = ["sales", "account", "marketing", "recruit"]
_ANALYST_ROLE_RE = re.compile(r"\b(?:analysts?|data scientists?|business intelligence)\b", re.I)

# Short words that must match as WHOLE words. As substrings they misfire:
# "intern" is inside "International" and "Internal Audit", "vp" can sit inside
# other words, and "sr" inside "SRE". \b in a regex means "word boundary".
TITLE_EXCLUDE_WORDS = [
    r"sr\.?",            # Sr / Sr. / SR
    r"vp", r"avp", r"vice president",
    r"mgr",              # "Mgr, Analysts"
    r"iv",               # "Analyst IV" (level 4)
    r"intern", r"internship", r"summer analyst",   # internships, not full-time
]
_EXCLUDE_WORDS_RE = re.compile(r"\b(?:" + "|".join(TITLE_EXCLUDE_WORDS) + r")\b", re.I)

# Words that suggest an entry-level / new-grad role (boosts the fit score).
NEWGRAD_SIGNALS = [
    "new grad", "new graduate", "early career", "entry level", "entry-level",
    "university", "campus", "associate", "rotational", "junior",
]

# Locations you'd accept. Empty list = accept everything. Case-insensitive substring.
DEFAULT_LOCATIONS = []  # e.g. ["Texas", "Dallas", "Remote", "TX"]

# Search the whole US: when True, ignore each company's "locations" list in
# companies.json (which keeps only Texas/remote postings for national employers).
# Clearly overseas postings (Bengaluru, London, Warsaw...) are dropped before
# scoring so you don't pay to score jobs you can't take (see us_location.py).
# Set to False to go back to Texas + remote only.
NATIONWIDE = True

# Drop postings older than this many days (based on the posting's date when known).
# Postings with no readable date are KEPT (we don't guess them stale).
DEFAULT_MAX_AGE_DAYS = 45

REQUEST_PAUSE = 0.4  # seconds between API calls — be polite, avoid rate limits

# Workday and Oracle sites can have thousands of jobs, so we search them by keyword
# instead of listing everything. Each search is capped; duplicates are merged.
SEARCH_TERMS = ["analyst", "analytics", "data", "business intelligence", "reporting"]
MAX_PER_SEARCH = 100     # most results past the first few pages don't match anyway
FALLBACK_MAX = 400       # plain listing (no search) if every keyword search fails
RETRIES = 3              # retries per request on temporary server errors (waits 1s, 2s, 4s)
RETRY_STATUSES = {429, 500, 502, 503, 504}

# How many companies to fetch at the same time. Each company is a different
# server, so this is still only one request at a time per company.
FETCH_WORKERS = 8


# ---------------------------------------------------------------------------
# ATS PULLERS  — each returns a list of normalized job dicts
# ---------------------------------------------------------------------------

def fetch_greenhouse(slug, client):
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    r = client.get(url, timeout=20)
    r.raise_for_status()
    jobs = r.json().get("jobs", [])
    out = []
    for j in jobs:
        out.append({
            "title": j.get("title", "").strip(),
            "location": (j.get("location") or {}).get("name", "").strip(),
            "url": j.get("absolute_url", ""),
            "date": _parse_date(j.get("updated_at", "")),
            "description": _strip_html(j.get("content", "")),
        })
    return out


def fetch_lever(slug, client):
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    r = client.get(url, timeout=20)
    r.raise_for_status()
    postings = r.json()
    out = []
    for j in postings:
        cats = j.get("categories", {}) or {}
        out.append({
            "title": j.get("text", "").strip(),
            "location": cats.get("location", "").strip(),
            "url": j.get("hostedUrl", ""),
            "date": _ms_to_date(j.get("createdAt")),
            "description": _strip_html(j.get("descriptionPlain") or j.get("description", "")),
        })
    return out


def fetch_workday(careers_url, client):
    """Workday has no single API. Each company runs its own 'tenant'.
    We derive the internal jobs endpoint from the public careers-page URL you give.

    Two URL styles exist (see workday_parts below):
      https://att.wd1.myworkdayjobs.com/ATTGeneral
      https://wd12.myworkdaysite.com/recruiting/parklandhospital/Parkland_Careers
    Either way the API is  https://{host}/wday/cxs/{tenant}/{site}/jobs  (POST).
    """
    host, tenant, site, public_base = workday_parts(careers_url)
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"

    # Big employers post thousands of jobs, so instead of listing everything we ask
    # Workday to SEARCH for your keywords, then merge the results (no duplicates).
    out, seen = [], set()

    def collect(term, max_jobs):
        """Page through one search, adding new postings to `out`."""
        offset, total = 0, None
        while True:
            body = {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": term}
            data = _workday_post(client, api, body)
            postings = data.get("jobPostings", [])
            # Workday only reports the real total on the FIRST page (later pages say 0),
            # so remember it from page one instead of re-reading it every page.
            if total is None:
                total = data.get("total", 0)
            page_hits = 0
            for j in postings:
                path = j.get("externalPath", "")
                if title_matches(j.get("title", "")):
                    page_hits += 1
                if path in seen:
                    continue
                seen.add(path)
                out.append({
                    "title": j.get("title", "").strip(),
                    "location": _workday_location(j.get("locationsText", ""), path),
                    # externalPath is relative to the site, e.g. "/job/Plano-Texas/..."
                    # so the public link needs the site name: host/<site>/job/...
                    "url": f"{public_base}{path}" if path else careers_url,
                    "date": _workday_posted(j.get("postedOn", "")),
                    "description": "",  # fetched later, one posting at a time, when needed
                })
            offset += 20
            # Search results are sorted by relevance, so once a whole page has no
            # titles you'd want, the rest of the pages won't either — stop early.
            if term and page_hits == 0:
                break
            if len(postings) < 20 or offset >= total or offset >= max_jobs:
                break
            time.sleep(REQUEST_PAUSE)

    collect.found = out          # lets _run_searches see whether anything came back
    _run_searches(collect)
    return out


def _workday_location(locations_text, path):
    """Workday's location field is sometimes useless:
      - blank (Accenture leaves it empty for every job), or
      - "3 Locations" (the real list is hidden).
    But the job's link always starts with its primary location:
      /job/Navi-Mumbai/Supply-Chain-Analyst_123  ->  "Navi Mumbai"
      /job/SSC-Irving-TX/Fuels-Analyst_456       ->  "SSC Irving TX"
    So we borrow it from there when the field doesn't tell us enough."""
    loc = (locations_text or "").strip()
    parts = path.split("/")                      # ["", "job", "<location>", "<title_id>"]
    primary = parts[2].replace("-", " ") if len(parts) > 3 and parts[1] == "job" else ""
    if not loc:
        return primary
    if primary and re.fullmatch(r"\d+\s+locations", loc, re.I):
        return f"{loc} (incl. {primary})"
    return loc


def _server_down(e):
    code = getattr(getattr(e, "response", None), "status_code", None)
    return isinstance(e, httpx.TransportError) or (code is not None and code >= 500)


def _run_searches(collect):
    """Shared search plan for Workday and Oracle.

    1. Run each keyword search. One failed search (e.g. a 502 because that
       search was too heavy for their server) is skipped; the others still run.
    2. If two searches IN A ROW hit server errors, the server looks down, so
       stop searching instead of hammering it for minutes.
    3. If no search worked or none found anything, try the plain newest-first
       listing once (it's a lighter request) before giving up.
    """
    last_error, ok_searches, server_fails = None, 0, 0
    for term in SEARCH_TERMS:
        try:
            collect(term, MAX_PER_SEARCH)
            ok_searches += 1
            server_fails = 0
        except Exception as e:
            last_error = e
            if _server_down(e):
                server_fails += 1
                if server_fails >= 2:
                    break
        time.sleep(REQUEST_PAUSE)
    if ok_searches == 0 or not collect.found:
        try:
            collect("", FALLBACK_MAX)
        except Exception:
            if last_error:
                raise last_error
            raise


def workday_parts(careers_url):
    """Split a Workday careers URL into (host, tenant, site, public_base).

      https://att.wd1.myworkdayjobs.com/en-US/ATTGeneral
        host = att.wd1.myworkdayjobs.com, tenant = att (first label of the host),
        site = ATTGeneral, public_base = https://att.wd1.myworkdayjobs.com/ATTGeneral
      https://wd12.myworkdaysite.com/recruiting/parklandhospital/Parkland_Careers
        host = wd12.myworkdaysite.com, tenant = parklandhospital (in the path),
        site = Parkland_Careers, public_base = the URL itself
    The optional language segment (/en-US/) is ignored in both styles.
    """
    url = careers_url.strip().rstrip("/")
    m = re.match(r"https?://([^/]+\.myworkdaysite\.com)/(?:[a-zA-Z]{2}-[a-zA-Z]{2}/)?"
                 r"recruiting/([^/]+)/([^/?#]+)", url)
    if m:
        host, tenant, site = m.groups()
        return host, tenant, site, f"https://{host}/recruiting/{tenant}/{site}"
    m = re.match(r"https?://([^/]+)/(?:[a-zA-Z]{2}-[a-zA-Z]{2}/)?([^/?#]+)", url)
    if not m:
        raise ValueError(f"Can't parse Workday URL: {careers_url}")
    host, site = m.groups()
    return host, host.split(".")[0], site, f"https://{host}/{site}"


def _call(client, method, url, **kwargs):
    """Make one API request, retrying on temporary server errors.

    502/503/504 (and 429 "slow down") mean the company's servers are busy, not
    that the URL is wrong, so we wait and try again: 1s, then 2s, then 4s.
    A 4xx like 404 means the URL really is wrong — no point retrying that.
    """
    for attempt in range(RETRIES + 1):
        try:
            r = client.request(method, url, timeout=25, **kwargs)
            if r.status_code in RETRY_STATUSES and attempt < RETRIES:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r
        except httpx.TransportError:  # timeout / dropped connection
            if attempt == RETRIES:
                raise
            time.sleep(2 ** attempt)


def _workday_post(client, api, body):
    r = _call(client, "POST", api, json=body,
              headers={"Accept": "application/json", "Content-Type": "application/json"})
    return r.json()


def fetch_oracle(careers_url, client):
    """Oracle Cloud Recruiting (HCM) — used by e.g. Texas Instruments.

    Example careers URL:  https://careers.ti.com/en/sites/CX/jobs
      host        = careers.ti.com
      site number = CX   (the segment after /sites/)
      API = https://careers.ti.com/hcmRestApi/resources/latest/
            recruitingCEJobRequisitions?onlyData=true
            &expand=requisitionList.secondaryLocations
            &finder=findReqs;siteNumber=CX,limit=25,offset=0
    """
    hm = re.match(r"https?://([^/]+)", careers_url.strip())
    sm = re.search(r"/sites/([^/?#]+)", careers_url)
    if not hm or not sm:
        raise ValueError(f"Can't parse Oracle URL: {careers_url}")
    host, site = hm.group(1), sm.group(1)
    base = f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"

    headers = {"Accept": "application/json",
               "REST-Framework-Version": "2"}   # Oracle requires this or it returns non-JSON
    out, seen = [], set()

    def collect(term, max_jobs):
        """Page through one keyword search ('' = everything, newest first)."""
        offset = 0
        while True:
            finder = f"findReqs;siteNumber={site},limit=25,offset={offset}"
            if term:
                finder += f',keyword="{term}"'
            params = {"onlyData": "true",
                      "expand": "requisitionList.secondaryLocations",
                      "finder": finder}
            r = _call(client, "GET", base, params=params, headers=headers)
            try:
                data = r.json()
            except Exception:
                # Oracle sometimes returns an HTML error/consent page; show a hint instead of crashing
                raise ValueError(
                    f"Oracle returned non-JSON (status {r.status_code}). "
                    f"The site number may differ from '{site}' — check the careers URL."
                )
            items = data.get("items", [])
            reqs = items[0].get("requisitionList", []) if items else []
            if not reqs:
                break
            page_hits = 0
            for j in reqs:
                rid = j.get("Id", "")
                if title_matches(j.get("Title") or ""):
                    page_hits += 1
                if rid in seen:
                    continue
                seen.add(rid)
                out.append({
                    "title": (j.get("Title") or "").strip(),
                    "location": (j.get("PrimaryLocation") or "").strip(),
                    # public job pages live under /hcmUI/CandidateExperience/
                    "url": (f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{rid}"
                            if rid else careers_url),
                    "date": _parse_date(j.get("PostedDate", "")),
                    "description": "",
                })
            offset += 25
            total = items[0].get("TotalJobsCount", 0)
            if term and page_hits == 0:      # relevance-sorted: nothing useful further down
                break
            if offset >= total or offset >= max_jobs:
                break
            time.sleep(REQUEST_PAUSE)

    # Same plan as Workday (see _run_searches): keyword searches, stop if the
    # server is down, plain newest-first listing as a last resort.
    collect.found = out
    _run_searches(collect)
    return out


def fetch_ashby(slug, client):
    """Ashby public job-board API. Used by many newer startups (Cohere, etc.).
      slug = the org name in jobs.ashbyhq.com/<slug>
      API  = https://api.ashbyhq.com/posting-api/job-board/<slug>
    """
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=false"
    r = client.get(url, timeout=20, headers={"Accept": "application/json"})
    r.raise_for_status()
    jobs = r.json().get("jobs", [])
    out = []
    for j in jobs:
        out.append({
            "title": (j.get("title") or "").strip(),
            "location": (j.get("location") or "").strip(),
            "url": j.get("jobUrl", ""),
            "date": _parse_date(j.get("publishedAt", "")),
            "description": "",
        })
    return out


def fetch_amazon(query, client):
    """Amazon Jobs public search API.
      query = base search text stored in ats_slug (e.g. 'analyst'); '' means all.
      API   = https://www.amazon.jobs/en/search.json
    """
    base = "https://www.amazon.jobs/en/search.json"
    out = []
    offset = 0
    while True:
        params = {
            "base_query": query or "",
            "normalized_country_code[]": "USA",
            "result_limit": 100,
            "offset": offset,
            "sort": "recent",
        }
        r = client.get(base, params=params, timeout=25, headers={
            "Accept": "application/json",
            "Accept-Encoding": "identity",   # ask Amazon NOT to gzip — avoids DecodingError
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            "X-Requested-With": "XMLHttpRequest",
        })
        r.raise_for_status()
        try:
            data = r.json()
        except Exception:
            raise ValueError(f"Amazon returned non-JSON (status {r.status_code}).")
        jobs = data.get("jobs", [])
        if not jobs:
            break
        for j in jobs:
            path = j.get("job_path", "")
            out.append({
                "title": (j.get("title") or "").strip(),
                "location": (j.get("normalized_location") or j.get("location") or "").strip(),
                "url": f"https://www.amazon.jobs{path}" if path else "https://www.amazon.jobs",
                "date": _parse_date_loose(j.get("posted_date", "")),
                "description": _amazon_description(j),
            })
        offset += 100
        if offset >= data.get("hits", 0) or offset >= 500:  # safety cap
            break
        time.sleep(REQUEST_PAUSE)
    return out


def _amazon_description(j):
    """The full posting: description + basic and preferred qualifications.

    Amazon's search results include a 200-character teaser (description_short)
    AND the full text, split into three fields. Until Oct 9 we saved only the
    teaser, so the AI scorer never saw an Amazon job's required years or tools.
    The eval caught it: every Amazon case was ~200 characters, cut off mid-sentence.
    """
    parts = [_strip_html(j.get("description") or j.get("description_short") or "")]
    for label, key in (("Basic qualifications", "basic_qualifications"),
                       ("Preferred qualifications", "preferred_qualifications")):
        text = _strip_html(j.get(key) or "")
        if text:
            parts.append(f"{label}: {text}")
    return "\n\n".join(p for p in parts if p)


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever,
            "workday": fetch_workday, "oracle": fetch_oracle,
            "ashby": fetch_ashby, "amazon": fetch_amazon}


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def _strip_html(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&[a-z]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(s):
    """ISO string -> date, or None."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except Exception:
        return None


def _parse_date_loose(s):
    """Parse human date formats like 'September 19, 2026' or ISO. Returns date or None."""
    if not s:
        return None
    d = _parse_date(s)
    if d:
        return d
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except Exception:
            continue
    return None


def _ms_to_date(ms):
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date()
    except Exception:
        return None


def _workday_posted(text):
    """Workday gives text like 'Posted 5 Days Ago' or 'Posted Today'. Best-effort."""
    if not text:
        return None
    t = text.lower()
    today = datetime.now(timezone.utc).date()
    if "today" in t:
        return today
    if "yesterday" in t:
        return today - timedelta(days=1)
    m = re.search(r"(\d+)\+?\s*day", t)
    if m:
        return today - timedelta(days=int(m.group(1)))
    m = re.search(r"(\d+)\+?\s*month", t)
    if m:
        return today - timedelta(days=int(m.group(1)) * 30)
    return None


def title_matches(title):
    t = f" {title.lower()} "
    if not any(k in t for k in TITLE_KEYWORDS):
        return False
    if any(x in t for x in TITLE_EXCLUDE):
        return False
    if _EXCLUDE_WORDS_RE.search(t):
        return False
    # field words only drop titles that aren't analyst roles
    if any(x in t for x in TITLE_EXCLUDE_FIELD) and not _ANALYST_ROLE_RE.search(t):
        return False
    return True


def location_matches(location, wanted):
    if not wanted:
        return True
    loc = location.lower()
    return any(w.lower() in loc for w in wanted)


def company_location_ok(location, wanted):
    """Per-company filter from companies.json ("locations": ["TX", "Texas", "Remote"]).

    Used for national employers (big banks, CVS...) so you only get their Texas or
    remote postings. Matches whole words, so "TX" won't match inside another word.
    Workday's "3 Locations" (location list hidden) is kept; the AI scorer sorts it out.
    """
    if not wanted or not location:
        return True
    if re.search(r"\d+\s+locations", location, re.I):
        return True
    return any(re.search(rf"\b{re.escape(w)}\b", location, re.I) for w in wanted)


def is_fresh(job_date, max_age_days):
    """Keep if within the window OR if we couldn't read a date (don't guess stale)."""
    if job_date is None:
        return True
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=max_age_days)
    return job_date >= cutoff


def fit_score(job, company):
    score = 30
    t = job["title"].lower()
    if any(s.strip() in f" {t} " for s in NEWGRAD_SIGNALS):
        score += 25
    if "business intelligence" in t or " bi " in t:
        score += 20
    elif "analyst" in t or "analytics" in t:
        score += 15
    elif "data" in t:
        score += 8
    if company.get("is_tx_local"):
        score += 12
    if company.get("tier") == 1:
        score += 8
    elif company.get("tier") == 2:
        score += 4
    return min(score, 100)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def load_companies(path="companies.json"):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        sys.exit(f"Can't find {path}. Keep it next to this script.")


def fetch_all(companies, client):
    """Fetch every company, FETCH_WORKERS at a time.

    Yields (company, jobs, error) as each one finishes, so progress prints live.
    httpx.Client is safe to share between threads.
    """
    todo = [c for c in companies if c.get("ats_type") in FETCHERS and c.get("ats_slug")]

    def one(c):
        try:
            return c, FETCHERS[c["ats_type"]](c["ats_slug"], client), None
        except Exception as e:
            return c, [], e

    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        futures = [pool.submit(one, c) for c in todo]
        for i, fut in enumerate(as_completed(futures), 1):
            c, jobs, err = fut.result()
            yield i, len(todo), c, jobs, err


def _error_code(e):
    return getattr(getattr(e, "response", None), "status_code", type(e).__name__)


def verify_slugs(companies, client):
    """Check every slug/URL works. Only needs one page per company, so we
    temporarily shrink the Workday/Oracle searches to a single quick request."""
    global SEARCH_TERMS, MAX_PER_SEARCH, FALLBACK_MAX
    saved = SEARCH_TERMS, MAX_PER_SEARCH, FALLBACK_MAX
    SEARCH_TERMS, MAX_PER_SEARCH, FALLBACK_MAX = SEARCH_TERMS[:1], 20, 20
    try:
        _verify(companies, client)
    finally:
        SEARCH_TERMS, MAX_PER_SEARCH, FALLBACK_MAX = saved


def _fail_hint(code):
    """Turn an HTTP status code into advice. The first digit says whose problem it is."""
    if isinstance(code, int) and code >= 500:
        return "company's server is down/busy - try again later"
    if code == 422:
        # 422 "Unprocessable": the server is there but rejects what we asked for.
        # On Workday this usually means the company left Workday or renamed the site.
        return "site rejects the request - company may have moved off this ATS; re-check its careers page"
    return "fix slug/url in companies.json"


def _verify(companies, client):
    print("Verifying slugs against live APIs (quick check, 1 page each)...\n")
    for c in companies:
        ats = c.get("ats_type")
        if ats not in FETCHERS or not c.get("ats_slug"):
            print(f"  SKIP  {c['name']:<28} (unsupported site - use its email job alerts)")
    for _i, _n, c, jobs, err in fetch_all(companies, client):
        ats = c["ats_type"]
        if err is not None:
            code = _error_code(err)
            print(f"  FAIL  {c['name']:<28} {ats}  ({code}) -> {_fail_hint(code)}")
        elif not jobs:
            # "OK" only proves the server answered. Zero jobs at a big employer
            # usually means the right server but the wrong (empty) site.
            print(f"  WARN  {c['name']:<28} {ats}  (0 jobs) -> URL answers but lists nothing; double-check it")
        else:
            print(f"  OK    {c['name']:<28} {ats}  ({len(jobs)} jobs)")


def run(locations, verify, max_age_days):
    companies = load_companies()
    headers = {"User-Agent": "job-copilot/1.0 (personal job search)"}
    with httpx.Client(headers=headers, follow_redirects=True) as client:
        if verify:
            verify_slugs(companies, client)
            print()

        rows = []
        details = {}   # apply_url -> full job description + basics
        started, n = time.time(), 0
        for i, n, c, jobs, err in fetch_all(companies, client):
            ats = c["ats_type"]
            if err is not None:
                print(f"  [{i:>3}/{n}] ! {c['name']}: fetch failed ({_error_code(err)}) — skipping")
                continue

            kept = 0
            for j in jobs:
                if not title_matches(j["title"]):
                    continue
                if not location_matches(j["location"], locations):
                    continue
                if not NATIONWIDE and not company_location_ok(j["location"], c.get("locations")):
                    continue
                if NATIONWIDE and is_non_us(j["location"]):
                    continue
                if not is_fresh(j.get("date"), max_age_days):
                    continue
                rows.append({
                    "fit_score": fit_score(j, c),
                    "company": c["name"],
                    "tier": c.get("tier", ""),
                    "tx_local": "yes" if c.get("is_tx_local") else "",
                    "title": j["title"],
                    "location": j["location"],
                    "date_posted": j["date"].isoformat() if j.get("date") else "",
                    "apply_url": j["url"],
                })
                # Save the full description on the side (too long for the CSV),
                # keyed by apply link, so tools can read a posting without copy-paste.
                details[j["url"]] = {
                    "company": c["name"], "title": j["title"], "location": j["location"],
                    "ats": ats, "description": j.get("description", ""),
                }
                kept += 1
            print(f"  [{i:>3}/{n}] {c['name']:<28} {kept:>3} matching roles")
        mins, secs = divmod(int(time.time() - started), 60)
        print(f"\nFetched {n} companies in {mins}m {secs:02d}s")

    rows.sort(key=lambda r: (-r["fit_score"], r["company"]))

    out = "job_feed.csv"
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "fit_score", "company", "tier", "tx_local", "title",
            "location", "date_posted", "apply_url",
        ])
        w.writeheader()
        w.writerows(rows)

    with open("job_details.json", "w", encoding="utf-8") as f:
        json.dump(details, f, ensure_ascii=False)

    print(f"\nDone. {len(rows)} matching roles (last {max_age_days} days) -> {out}")
    if rows:
        # This is the rough KEYWORD score (title words + company tier), not the AI score.
        # With --score, the real ranking prints later under "Top 10 by AI fit".
        print("\nTop 10 by KEYWORD fit (rough title match - the AI ranking comes after scoring):")
        for r in rows[:10]:
            print(f"  [{r['fit_score']}] {r['company']} — {r['title']} ({r['location']})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Phase 1 job-discovery feed")
    ap.add_argument("--verify", action="store_true",
                    help="Re-check which company slugs return data before pulling")
    ap.add_argument("--location", action="append", default=None,
                    help="Only keep roles whose location contains this (repeatable)")
    ap.add_argument("--max-age", type=int, default=DEFAULT_MAX_AGE_DAYS,
                    help=f"Drop postings older than N days (default {DEFAULT_MAX_AGE_DAYS})")
    ap.add_argument("--score", action="store_true",
                    help="After discovery, AI-score every job's fit (see score_jobs.py)")
    args = ap.parse_args()
    locs = args.location if args.location is not None else DEFAULT_LOCATIONS
    run(locs, args.verify, args.max_age)
    if args.score:
        print("\nScoring fit with AI...")
        import score_jobs
        score_jobs.run()
