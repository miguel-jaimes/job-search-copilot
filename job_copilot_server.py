# Job-Search Copilot — Phase 3: the MCP server
#
# Lets Claude (in the Claude desktop app) use your job-search tool by conversation:
# find jobs, read postings, tailor a resume (Claude does the selecting, following
# the same rules as tailor_resume.py), build the PDF, answer screening questions
# from your real experience, and track applications.
#
# You don't run this yourself — the Claude desktop app starts it (see
# claude_desktop_config.json). It reuses your existing scripts; nothing is duplicated.

import contextlib
import csv
import io
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime
from typing import Any

from mcp.server.mcpserver import MCPServer

# --- Setup that makes the server work when the Claude app launches it -------

# 1) The app starts us from its own folder. Work from the project folder instead,
#    so every relative file name (job_feed.csv, resumes/, ...) resolves correctly.
HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

# 2) The app only searches /usr/bin etc. for programs, so it can't see Anaconda's
#    tools (like Tectonic). Add the folder our own Python lives in to the search path.
os.environ["PATH"] = os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", "")

import render_resume as renderer                        # noqa: E402  (our renderer)
from resume_rules import SCHEMA, TAILORING_RULES, ANSWER_RULES  # noqa: E402  (shared rules)
import tracker                                          # noqa: E402  (shared application tracker)
import descriptions                                     # noqa: E402  (shared job-description lookup)

BANK_FILE = "experience_bank.json"
FEED_FILE = "job_feed.csv"
DETAILS_FILE = "job_details.json"
ANSWERS_FILE = "answers.json"
JOB_FILE = "job.txt"   # where you paste descriptions for sites the tool doesn't cover

mcp = MCPServer(
    "job-copilot",
    instructions=(
        "Job-search copilot for Miguel. Typical flow: show_feed (or refresh_feed first) -> "
        "get_job_description for a chosen row -> get_experience_bank -> select content "
        "following its rules -> render_resume. For application questions, check find_answers "
        "first, draft from the bank with ANSWER_RULES, and save_answer once Miguel approves. "
        "For jobs outside the feed (e.g. Southwest), use get_pasted_job or a description Miguel "
        "pastes in chat. To answer questions for an application already tailored, call "
        "get_job_context with the company name first. To decide what to learn next, use "
        "skill_gaps. "
        "Never invent experience: the experience bank is the only source of facts."
    ),
)

_refresh_proc = None   # the running discovery process, if any
_last_job = ""         # last job description handed to Claude (saved with the resume)


# --- Small helpers ------------------------------------------------------------

def _strip_html(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"&[a-z#0-9]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _read_feed():
    try:
        with open(FEED_FILE, encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


def _quiet(func, *args, **kwargs):
    """Run a function while capturing anything it print()s. In an MCP server,
    stray prints would corrupt the connection to the app, so we catch them
    and return them as text instead."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = func(*args, **kwargs)
    return result, buf.getvalue()


# --- Discovery ----------------------------------------------------------------

@mcp.tool()
def refresh_feed(location: str = "", max_age_days: int = 45) -> str:
    """Start a fresh job search across all target companies, then AI-score each job's fit.
    Takes 2-4 minutes and runs in the background; call show_feed afterwards.
    location: optional filter such as "Plano" or "Texas". max_age_days: skip older postings."""
    global _refresh_proc
    if _refresh_proc and _refresh_proc.poll() is None:
        return "A refresh is already running. Call show_feed in a minute."
    cmd = [sys.executable, "discover_jobs.py", "--max-age", str(max_age_days), "--score"]
    if location:
        cmd += ["--location", location]
    log = open("refresh_log.txt", "w", encoding="utf-8")
    _refresh_proc = subprocess.Popen(cmd, cwd=HERE, stdout=log, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL)
    where = f" in '{location}'" if location else ""
    return (f"Started a fresh search{where} (postings from the last {max_age_days} days). "
            "It takes 2-4 minutes (search, then AI fit scoring). Call show_feed once it finishes.")


@mcp.tool()
def show_feed(limit: int = 15, company: str = "", location: str = "") -> str:
    """Show the ranked job feed (best AI fit first, with a one-line reason per job) with row numbers.
    Optional filters: company name and/or location text. Use the row number with
    get_job_description."""
    status = ""
    if _refresh_proc and _refresh_proc.poll() is None:
        status = "NOTE: a refresh is still running — showing the previous feed.\n"
    rows = _read_feed()
    if not rows:
        return status + "No job feed yet. Call refresh_feed first."
    updated = datetime.fromtimestamp(os.path.getmtime(FEED_FILE)).strftime("%b %d, %I:%M %p")
    out = [status + f"Job feed ({len(rows)} roles, updated {updated}):"]
    shown = 0
    for i, r in enumerate(rows, start=1):
        if company and company.lower() not in r["company"].lower():
            continue
        if location and location.lower() not in r["location"].lower():
            continue
        score = f"AI fit {r['ai_score']}" if r.get("ai_score") else f"keyword fit {r['fit_score']}"
        line = (f"#{i} [{score}] {r['company']} — {r['title']} | "
                f"{r['location']} | posted {r['date_posted'] or '?'} | {r['apply_url']}")
        if r.get("ai_reason"):
            line += f"\n     why: {r['ai_reason']}" + (f"  (flags: {r['ai_flags']})" if r.get("ai_flags") else "")
        out.append(line)
        shown += 1
        if shown >= limit:
            break
    if shown == 0:
        out.append("No roles match those filters.")
    return "\n".join(out)


@mcp.tool()
def get_job_description(row: int) -> str:
    """Get the full job description for a row number from show_feed."""
    rows = _read_feed()
    if not 1 <= row <= len(rows):
        return f"Row {row} doesn't exist. The feed has {len(rows)} rows."
    r = rows[row - 1]
    try:
        desc = descriptions.get_description(r["apply_url"])
    except Exception as e:
        desc = ""
        print(f"description fetch failed: {e}", file=sys.stderr)
    header = f"{r['company']} — {r['title']} ({r['location']})\nApply: {r['apply_url']}\n\n"
    global _last_job
    _last_job = header + desc if desc else ""
    if not desc:
        return header + ("Couldn't retrieve this description automatically. Ask Miguel to "
                         "paste it from the apply link.")
    return header + desc


@mcp.tool()
def skill_gaps(min_score: int = 50, max_score: int = 79, top: int = 10) -> str:
    """Find the skills Miguel is most often missing in jobs he's CLOSE to qualifying for
    (AI fit score between min_score and max_score). Skips too-senior, non-analytics and
    out-of-area roles, since new skills won't fix those. Flags gaps his experience bank
    already mentions (a wording problem, not a skill problem). Free: uses saved scores."""
    import score_jobs
    return score_jobs.skill_gaps(min_score, max_score, top)


# --- Tailoring ------------------------------------------------------------------

@mcp.tool()
def get_experience_bank() -> str:
    """Get Miguel's experience bank (the ONLY allowed source of facts) plus the
    tailoring rules, the resume JSON schema for render_resume, and the rules for
    answering application questions. Call this before tailoring or answering."""
    bank = _load_json(BANK_FILE, None)
    if bank is None:
        return f"Can't read {BANK_FILE}."
    return (f"{TAILORING_RULES}\n\nRESUME JSON SCHEMA (what render_resume expects):\n{SCHEMA}\n\n"
            f"{ANSWER_RULES}\n\n=== EXPERIENCE BANK (JSON) ===\n{json.dumps(bank, indent=2)}")


@mcp.tool()
def render_resume(resume: dict[str, Any], job_description: str = "") -> str:
    """Build the tailored one-page PDF in Miguel's LaTeX template from a resume object
    that follows the schema from get_experience_bank. Contact info is filled in from
    the bank automatically. Auto-trims the least relevant content if it overflows,
    opens the PDF, and logs the job in the application tracker as 'Tailored'.
    job_description: pass the posting text when it came from chat or job.txt (not needed
    right after get_job_description) — it's saved next to the resume for later questions."""
    bank = _load_json(BANK_FILE, {})
    resume["profile"] = bank.get("profile", {})           # never trust generated contact info
    name = renderer.safe_name(resume["profile"].get("name", "Resume"))
    company = resume.get("company") or "Company"
    role = resume.get("role_short") or ""
    basename = "_".join(filter(None, [name, renderer.safe_name(company),
                                      renderer.safe_name(role)]))

    os.makedirs(renderer.OUT_DIR, exist_ok=True)
    json_path = os.path.join(renderer.OUT_DIR, basename + ".json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(resume, f, indent=2, ensure_ascii=False)

    job_text = job_description or _last_job
    if job_text:
        with open(os.path.join(renderer.OUT_DIR, basename + "_job.txt"), "w", encoding="utf-8") as f:
            f.write(job_text)

    pdf, printed = _quiet(renderer.render, resume, basename)
    if not pdf:
        return "PDF build failed. Renderer output:\n" + printed

    if sys.platform == "darwin":
        subprocess.run(["open", pdf])
    tracker.upsert_application(company, role, "Tailored", resume_file=os.path.basename(pdf))
    notes = "\n".join(l for l in printed.splitlines() if l.startswith(("Trimmed", "Saved", "  ")))
    return (f"Resume built and opened: {pdf}\n{notes}\n"
            f"Logged in tracker as 'Tailored' with company='{company}', role='{role}'. "
            f"Use exactly these names with log_application so it updates this row.")


# --- Jobs outside the feed, and past applications --------------------------------------

@mcp.tool()
def get_pasted_job() -> str:
    """Read job.txt — where Miguel pastes descriptions for jobs on sites the tool
    doesn't cover (e.g. Southwest, American Airlines, company career pages)."""
    global _last_job
    try:
        with open(JOB_FILE, encoding="utf-8") as f:
            text = f.read().strip()
    except FileNotFoundError:
        return "job.txt doesn't exist. Ask Miguel to paste the description into chat instead."
    if not text:
        return "job.txt is empty. Ask Miguel to paste the description."
    updated = datetime.fromtimestamp(os.path.getmtime(JOB_FILE)).strftime("%b %d, %I:%M %p")
    _last_job = text
    return f"job.txt (last edited {updated}):\n\n{text}"


def _resume_records():
    """Every tailored resume in resumes/, newest first."""
    out_dir = renderer.OUT_DIR
    if not os.path.isdir(out_dir):
        return []
    recs = []
    for fn in os.listdir(out_dir):
        if not fn.endswith(".json"):
            continue
        base = fn[:-5]
        data = _load_json(os.path.join(out_dir, fn), {})
        recs.append({
            "base": base,
            "company": data.get("company", ""),
            "role": data.get("role_short", ""),
            "modified": os.path.getmtime(os.path.join(out_dir, fn)),
            "has_job": os.path.exists(os.path.join(out_dir, base + "_job.txt")),
            "data": data,
        })
    return sorted(recs, key=lambda r: r["modified"], reverse=True)


@mcp.tool()
def list_resumes() -> str:
    """List every tailored resume (from chat or from tailor_resume.py), newest first."""
    recs = _resume_records()
    if not recs:
        return "No tailored resumes yet."
    lines = [f"{len(recs)} tailored resume(s):"]
    for r in recs:
        when = datetime.fromtimestamp(r["modified"]).strftime("%b %d")
        note = "" if r["has_job"] else "  (job description not saved)"
        lines.append(f"{when} | {r['company']} — {r['role']} | {r['base']}.pdf{note}")
    return "\n".join(lines)


@mcp.tool()
def get_job_context(company: str, role: str = "") -> str:
    """For an application already tailored (from chat or tailor_resume.py): return the
    job description it was tailored to AND the resume content that was sent. Call this
    before answering that application's questions so answers match the resume."""
    c, ro = company.lower(), role.lower()
    matches = [r for r in _resume_records()
               if c in r["company"].lower() or c in r["base"].lower()]
    if ro:
        matches = [r for r in matches if ro in r["role"].lower() or ro in r["base"].lower()] or matches
    if not matches:
        return (f"No tailored resume found for '{company}'. Call list_resumes to see what exists, "
                "or tailor one first.")
    r = matches[0]
    job_path = os.path.join(renderer.OUT_DIR, r["base"] + "_job.txt")
    if r["has_job"]:
        with open(job_path, encoding="utf-8") as f:
            job = f.read()
        job_note = "JOB DESCRIPTION (saved with this resume):"
    else:
        # Older resumes made before job descriptions were saved: fall back to job.txt.
        try:
            with open(JOB_FILE, encoding="utf-8") as f:
                job = f.read()
        except FileNotFoundError:
            job = ""
        job_note = ("JOB DESCRIPTION — not saved with this resume; below is job.txt (the most "
                    "recently pasted job). Confirm with Miguel it's the same job:")
    d = r["data"]
    sent = [f"Summary: {d.get('summary', '')}"]
    for sec in ("projects", "experience"):
        for it in d.get(sec, []):
            head = it.get("title") if sec == "projects" else f"{it.get('org')} — {it.get('title')}"
            sent.append(f"\n{head}")
            sent += [f"  - {b}" for b in it.get("bullets", [])]
    sent.append("\nSkills: " + "; ".join(f"{s.get('category')}: {s.get('items')}" for s in d.get("skills", [])))
    others = f"\n(Also found {len(matches) - 1} older match(es); pass role= to pick another.)" if len(matches) > 1 else ""
    return (f"Resume: {r['base']}.pdf{others}\n\n{job_note}\n{job or '(none)'}\n\n"
            f"RESUME CONTENT SENT:\n" + "\n".join(sent))


# --- Screening-question answers -----------------------------------------------------

@mcp.tool()
def find_answers(keyword: str = "", limit: int = 10) -> str:
    """Search Miguel's saved application answers (newest first). Use before drafting a
    new answer — a similar question may already have an approved answer to adapt."""
    answers = _load_json(ANSWERS_FILE, [])
    k = keyword.lower()
    hits = [a for a in reversed(answers)
            if not k or k in a["question"].lower() or k in a["answer"].lower()]
    if not hits:
        return "No saved answers match." if answers else "No saved answers yet."
    out = []
    for a in hits[:limit]:
        where = " / ".join(x for x in (a.get("company"), a.get("role")) if x)
        out.append(f"Q: {a['question']}\n({a['date']}{' — ' + where if where else ''})\nA: {a['answer']}")
    return "\n\n".join(out)


@mcp.tool()
def save_answer(question: str, answer: str, company: str = "", role: str = "") -> str:
    """Save an application answer Miguel has approved, so it can be reused later."""
    answers = _load_json(ANSWERS_FILE, [])
    answers.append({"date": date.today().isoformat(), "question": question.strip(),
                    "answer": answer.strip(), "company": company, "role": role})
    with open(ANSWERS_FILE, "w", encoding="utf-8") as f:
        json.dump(answers, f, indent=2, ensure_ascii=False)
    return f"Saved. The answer library now has {len(answers)} answers."


# --- Application tracker ---------------------------------------------------------------

@mcp.tool()
def log_application(company: str, role: str, status: str = "Applied", referral: str = "",
                    url: str = "", notes: str = "") -> str:
    """Record or update an application in the tracker (applications.csv).
    status examples: Tailored, Applied, Referred, Interview, Offer, Rejected.
    The same company + role updates the existing row instead of adding a duplicate, so
    reuse the exact names render_resume reported (or check list_applications first)."""
    action = tracker.upsert_application(company, role, status, referral, url, notes)
    return f"{action}: {company} — {role} → {status}"


@mcp.tool()
def list_applications(status: str = "") -> str:
    """List tracked applications, optionally filtered by status (e.g. 'Applied')."""
    apps = [a for a in tracker.read_apps() if not status or a["status"].lower() == status.lower()]
    if not apps:
        return "No applications tracked yet." if not status else f"No applications with status '{status}'."
    lines = [f"{len(apps)} application(s):"]
    for a in sorted(apps, key=lambda a: a["date"], reverse=True):
        extra = f" | referral: {a['referral']}" if a["referral"] else ""
        lines.append(f"{a['date']} | {a['company']} — {a['role']} | {a['status']}{extra}")
    return "\n".join(lines)


if __name__ == "__main__":
    print("job-copilot server starting", file=sys.stderr)   # stderr only — never stdout
    mcp.run()
