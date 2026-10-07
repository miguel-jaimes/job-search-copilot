# Job-Search Copilot — Phase 2: Resume Tailoring Engine
#
# Reads your experience bank + a job description, asks Claude to SELECT the
# best-fitting experience for that job, and returns it as STRUCTURED DATA.
# That data is then dropped into your exact Overleaf template and compiled
# to a PDF by render_resume.py.
#
# It NEVER invents experience — it can only pick and rephrase what's in your bank.
#
# ---------------------------------------------------------------------------
# SETUP (one time):
#   pip install anthropic python-dotenv
#   conda install -c conda-forge tectonic
#   .env contains:  ANTHROPIC_API_KEY=sk-ant-...
#   experience_bank.json and render_resume.py are in this folder.
#
# RUN:
#   python tailor_resume.py job.txt
#   python tailor_resume.py job.txt --company "Toyota"   # override the file name
#
# OUTPUT (in the resumes/ folder):
#   Miguel_Jaimes_<Company>_<Role>.json  — the selections (editable)
#   Miguel_Jaimes_<Company>_<Role>.tex   — LaTeX source (pasteable into Overleaf)
#   Miguel_Jaimes_<Company>_<Role>.pdf   — the finished resume
# ---------------------------------------------------------------------------

import argparse
import json
import os
import subprocess
import sys

try:
    from dotenv import load_dotenv
    import anthropic
except ImportError:
    sys.exit("Missing dependency. Run:  pip install anthropic python-dotenv")

import render_resume  # our renderer, in the same folder
import tracker        # shared application tracker (applications.csv)

load_dotenv()
API_KEY = os.getenv("ANTHROPIC_API_KEY")
if not API_KEY:
    sys.exit("No ANTHROPIC_API_KEY found. Check your .env file.")

MODEL = "claude-sonnet-4-5"   # good quality for writing; swap to a Haiku model to save money
BANK_FILE = "experience_bank.json"

# The resume data shape and the tailoring rules live in resume_rules.py,
# shared with the MCP server so both ways of tailoring follow identical rules.
from resume_rules import SCHEMA, TAILORING_RULES


def load_bank(path=BANK_FILE):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        sys.exit(f"Can't find {path}. Keep it next to this script.")


def load_job(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        sys.exit(f"Can't find the job file '{path}'. Save the job description into a .txt file first.")


def build_prompt(bank, job_text):
    return f"""You are tailoring a one-page resume to a specific job posting.

You are given (1) the candidate's full EXPERIENCE BANK as JSON — more experience
than fits on one page — and (2) a JOB DESCRIPTION.

{TAILORING_RULES}

OUTPUT
Return ONLY a JSON object with exactly this shape — no commentary, no code fences:
{SCHEMA}

=== EXPERIENCE BANK (JSON) ===
{json.dumps(bank, indent=2)}

=== JOB DESCRIPTION ===
{job_text}
"""


def extract_json(text):
    """Pull the JSON object out of Claude's reply, even if it adds stray text."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object found in the model's reply.")
    return json.loads(text[start:end + 1])


def main(job_path, company_override):
    bank = load_bank()
    job_text = load_job(job_path)

    client = anthropic.Anthropic(api_key=API_KEY)
    print("Tailoring your resume to the job... (~20 seconds)\n")
    resp = client.messages.create(
        model=MODEL,
        max_tokens=3000,
        messages=[{"role": "user", "content": build_prompt(bank, job_text)}],
    )
    raw = resp.content[0].text

    try:
        resume = extract_json(raw)
    except Exception as e:
        with open("tailor_raw_output.txt", "w", encoding="utf-8") as f:
            f.write(raw)
        sys.exit(f"Couldn't read the model's reply as JSON ({e}). "
                 "Raw reply saved to tailor_raw_output.txt — just re-run.")

    # Contact info always comes straight from your bank, never from the model.
    resume["profile"] = bank.get("profile", {})

    company = company_override or resume.get("company") or "Company"
    role = resume.get("role_short") or ""
    name = render_resume.safe_name(resume["profile"].get("name", "Resume"))
    basename = "_".join(filter(None, [name, render_resume.safe_name(company),
                                      render_resume.safe_name(role)]))

    os.makedirs(render_resume.OUT_DIR, exist_ok=True)
    json_path = os.path.join(render_resume.OUT_DIR, basename + ".json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(resume, f, indent=2, ensure_ascii=False)
    print(f"Wrote {json_path}")

    # Keep the job description next to the resume, so the MCP server (and you)
    # can always see which posting this resume was tailored to — any site, any time.
    with open(os.path.join(render_resume.OUT_DIR, basename + "_job.txt"), "w", encoding="utf-8") as f:
        f.write(job_text)

    pdf = render_resume.render(resume, basename)

    if pdf:
        action = tracker.upsert_application(company, role, "Tailored",
                                            resume_file=os.path.basename(pdf))
        print(f"{action} in applications.csv: {company} — {role} (Tailored)")

    print("\n--- WHY THESE CHOICES ---")
    print(resume.get("why", "(none given)"))
    u = resp.usage
    print(f"\nTokens: {u.input_tokens} in, {u.output_tokens} out")

    if pdf and sys.platform == "darwin":
        subprocess.run(["open", pdf])   # pop the PDF open on your Mac


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Tailor your resume to a job and export a PDF")
    ap.add_argument("job_file", help="Text file containing the job description")
    ap.add_argument("--company", default=None, help="Override the company name used in the file name")
    args = ap.parse_args()
    main(args.job_file, args.company)
