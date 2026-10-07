# Job-Search Copilot — shared job-description lookup
#
# Used by the MCP server (get_job_description) and score_jobs.py.
# Greenhouse, Lever, Ashby and Amazon descriptions are saved during discovery
# (job_details.json). Workday and Oracle only list titles, so their
# descriptions are fetched live, one posting at a time.

import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
DETAILS_FILE = os.path.join(HERE, "job_details.json")


def strip_html(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"&[a-z#0-9]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def load_details():
    try:
        with open(DETAILS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def fetch_live(url, ats, client=None):
    """Fetch one Workday or Oracle posting's description. Returns '' if unsupported."""
    import httpx
    own = client is None
    if own:
        client = httpx.Client(follow_redirects=True, timeout=20, headers={
            "Accept": "application/json", "User-Agent": "job-copilot/1.0 (personal job search)"})
    try:
        if ats == "workday":
            # Job link = <careers site> + /job/...  (works for both Workday URL styles)
            if "/job/" not in url:
                return ""
            from discover_jobs import workday_parts
            site_url, path = url.split("/job/", 1)
            host, tenant, site, _ = workday_parts(site_url)
            r = client.get(f"https://{host}/wday/cxs/{tenant}/{site}/job/{path}")
            r.raise_for_status()
            return strip_html(r.json().get("jobPostingInfo", {}).get("jobDescription", ""))
        if ats == "oracle":
            m = re.match(r"https?://([^/]+)/.*?/sites/([^/]+)/job/([^/?#]+)", url)
            if not m:
                return ""
            host, site, rid = m.groups()
            r = client.get(
                f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails",
                params={"expand": "all", "onlyData": "true",
                        "finder": f'ById;Id="{rid}",siteNumber={site}'},
                headers={"REST-Framework-Version": "2"},
            )
            r.raise_for_status()
            item = (r.json().get("items") or [{}])[0]
            parts = [item.get(k, "") for k in
                     ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr")]
            return strip_html(" ".join(p for p in parts if p))
        return ""
    finally:
        if own:
            client.close()


def get_description(url, details=None, client=None):
    """Description for one apply link: saved copy first, live fetch for Workday/Oracle."""
    details = details if details is not None else load_details()
    info = details.get(url, {})
    desc = info.get("description", "")
    if not desc and info.get("ats") in ("workday", "oracle"):
        desc = fetch_live(url, info["ats"], client)
    return desc
