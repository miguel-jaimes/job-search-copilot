# Job-Search Copilot — shared application tracker
#
# One tracker file (applications.csv) that BOTH tailor_resume.py and the MCP
# server write to, so every application is tracked no matter how you made it.

import csv
import os
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
APPS_FILE = os.path.join(HERE, "applications.csv")
APP_FIELDS = ["date", "company", "role", "status", "referral", "resume_file", "url", "notes"]


def read_apps():
    try:
        with open(APPS_FILE, encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


def upsert_application(company, role, status, referral="", url="", notes="", resume_file=""):
    """Add a row, or update the existing row for the same company + role.
    Returns 'Added' or 'Updated'."""
    apps = read_apps()
    key = (company.strip().lower(), role.strip().lower())
    for a in apps:
        if (a["company"].strip().lower(), a["role"].strip().lower()) == key:
            a["status"], a["date"] = status, date.today().isoformat()
            for field, val in (("referral", referral), ("url", url),
                               ("notes", notes), ("resume_file", resume_file)):
                if val:
                    a[field] = val
            action = "Updated"
            break
    else:
        apps.append({"date": date.today().isoformat(), "company": company, "role": role,
                     "status": status, "referral": referral, "resume_file": resume_file,
                     "url": url, "notes": notes})
        action = "Added"
    with open(APPS_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=APP_FIELDS)
        w.writeheader()
        w.writerows(apps)
    return action
