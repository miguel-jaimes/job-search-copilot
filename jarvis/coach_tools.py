# Jarvis — the coach's tools (how Jarvis looks things up and remembers)
#
# A "tool" is a function Claude can ask us to run. Claude never touches your
# files directly: it says "please run get_job_context with company=Toyota",
# this file runs it, and the text result goes back to Claude.
#
# Most tools REUSE the job copilot's MCP server functions (job descriptions,
# saved answers, the tracker), so there's still one place that knows how to
# read those files. The two new tools are the coaching memory:
#   save_debrief          -> appends a session's scores and fixes to coaching_log.json
#   get_coaching_history  -> reads them back so Jarvis targets your weak spots

import json
import os
from collections import Counter
from datetime import datetime

import job_copilot_server as copilot   # the copilot's MCP server, imported as a normal module

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(HERE, "coaching_log.json")   # personal: kept out of git


# --- Tool descriptions Claude reads (the "menu") -------------------------------
# Each entry: a name, a description of WHEN to use it, and the inputs it takes
# as a JSON schema. Claude picks tools based on these descriptions, so they're
# written like instructions.

TOOLS = [
    {
        "name": "get_job_context",
        "description": "Get the saved job posting and the resume Miguel sent for an application, "
                       "by company name (and optionally role). Use at interview setup to tailor questions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "company": {"type": "string", "description": "Company name, e.g. Toyota"},
                "role": {"type": "string", "description": "Optional part of the role name"},
            },
            "required": ["company"],
        },
    },
    {
        "name": "get_pasted_job",
        "description": "Read job.txt, where Miguel pastes job descriptions for jobs outside his feed. "
                       "Use when get_job_context finds nothing for the company.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "find_answers",
        "description": "Search answers Miguel already approved for application questions. "
                       "Shows how he likes to frame his experience.",
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "Word to search for, e.g. leadership"},
            },
        },
    },
    {
        "name": "list_applications",
        "description": "List Miguel's tracked applications. Status 'Interview' means a real interview is coming.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Optional filter, e.g. Interview"},
            },
        },
    },
    {
        "name": "get_coaching_history",
        "description": "Past practice debriefs: scores, recurring fixes, questions already asked. "
                       "Use at interview setup and when coaching, to target his weak spots.",
        "input_schema": {
            "type": "object",
            "properties": {
                "company": {"type": "string", "description": "Optional: only sessions for this company"},
            },
        },
    },
    {
        "name": "save_debrief",
        "description": "Save the results of a debrief so future sessions can build on them. Call once per debrief.",
        "input_schema": {
            "type": "object",
            "properties": {
                "company": {"type": "string", "description": "Company practiced for, or 'General'"},
                "role": {"type": "string"},
                "scores": {
                    "type": "object",
                    "description": "1-5 scores",
                    "properties": {
                        "structure": {"type": "integer"},
                        "specificity": {"type": "integer"},
                        "relevance": {"type": "integer"},
                        "conciseness": {"type": "integer"},
                        "project_explanations": {"type": "integer"},
                    },
                },
                "strength": {"type": "string", "description": "His top strength this session"},
                "fixes": {"type": "array", "items": {"type": "string"}, "description": "Top fixes, most important first"},
                "stories_landed": {"type": "array", "items": {"type": "string"}},
                "stories_to_rework": {"type": "array", "items": {"type": "string"}},
                "drill": {"type": "string", "description": "One thing to practice before next time"},
                "questions_asked": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["company", "scores", "fixes", "questions_asked"],
        },
    },
]


# --- The coaching memory (new code) -------------------------------------------

def _load_log():
    try:
        with open(LOG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_debrief(company, scores, fixes, questions_asked, role="", strength="",
                 stories_landed=None, stories_to_rework=None, drill=""):
    log = _load_log()
    log.append({
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "company": company, "role": role, "scores": scores, "strength": strength,
        "fixes": fixes, "stories_landed": stories_landed or [],
        "stories_to_rework": stories_to_rework or [], "drill": drill,
        "questions_asked": questions_asked,
    })
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2, ensure_ascii=False)
    return f"Saved. {len(log)} practice session(s) on record."


def get_coaching_history(company=""):
    log = _load_log()
    if company:
        same = [s for s in log if company.lower() in s["company"].lower()]
        note = "" if same else f"No sessions for {company} yet; showing all sessions.\n"
        log = same or log
    else:
        note = ""
    if not log:
        return "No practice sessions on record yet. This is the first one."

    recent = log[-5:]
    lines = [note + f"{len(log)} session(s). Most recent last:"]
    for s in recent:
        sc = ", ".join(f"{k} {v}" for k, v in s["scores"].items())
        lines.append(f"\n{s['date']} | {s['company']} {s.get('role', '')}".rstrip())
        lines.append(f"  scores: {sc}")
        lines.append(f"  fixes: {'; '.join(s['fixes'])}")
        if s.get("stories_to_rework"):
            lines.append(f"  stories to rework: {'; '.join(s['stories_to_rework'])}")
        if s.get("drill"):
            lines.append(f"  drill: {s['drill']}")

    # Patterns across ALL sessions: the same fix showing up again is the real weak spot.
    fix_counts = Counter(f.lower() for s in log for f in s["fixes"])
    repeats = [f"{f} (x{n})" for f, n in fix_counts.most_common(5) if n > 1]
    if repeats:
        lines.append("\nFixes that keep coming up: " + "; ".join(repeats))
    asked = sorted({q for s in log for q in s.get("questions_asked", [])})
    lines.append(f"\nQuestions already practiced ({len(asked)}): " + " | ".join(asked[:25]))
    return "\n".join(lines)


# --- Running a tool Claude asked for --------------------------------------------

# Name -> the function that runs it. Four come straight from the copilot server.
_HANDLERS = {
    "get_job_context": copilot.get_job_context,
    "get_pasted_job": copilot.get_pasted_job,
    "find_answers": copilot.find_answers,
    "list_applications": copilot.list_applications,
    "get_coaching_history": get_coaching_history,
    "save_debrief": save_debrief,
}


def run_tool(name, args):
    """Run one tool. Returns (text_result, is_error). Errors go back to Claude as
    text instead of crashing the session, so it can recover or tell you."""
    func = _HANDLERS.get(name)
    if func is None:
        return f"Unknown tool: {name}", True
    try:
        return str(func(**args)), False
    except Exception as e:   # a bad argument or a missing file shouldn't end the practice session
        return f"{name} failed: {type(e).__name__}: {e}", True
