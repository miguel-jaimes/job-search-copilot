# Job-Search Copilot — AI fit scoring
#
# Reads each job's FULL description and scores how well it fits your experience
# bank (0-100), with a one-line reason and flags. Then re-sorts job_feed.csv so
# the best real fits come first — not just the best-sounding titles.
#
# Scores are saved in fit_scores.json, so each run only pays for NEW postings.
# Uses Claude Haiku (fast and cheap): roughly 0.3 cents per job.
#
# RUN:
#   python score_jobs.py              # score any unscored jobs in the feed
#   python score_jobs.py --rescore    # score everything again (e.g. after updating your bank)
#   python discover_jobs.py --score   # discovery + scoring in one go
# ---------------------------------------------------------------------------

import argparse
import csv
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

try:
    from dotenv import load_dotenv
    import anthropic
except ImportError:
    sys.exit("Missing dependency. Run:  pip install anthropic python-dotenv")

import descriptions
from us_location import is_non_us

HERE = os.path.dirname(os.path.abspath(__file__))
FEED_FILE = os.path.join(HERE, "job_feed.csv")
BANK_FILE = os.path.join(HERE, "experience_bank.json")
SCORES_FILE = os.path.join(HERE, "fit_scores.json")

MODEL = "claude-haiku-4-5"
WORKERS = 5                       # jobs scored at the same time
LOCATION_PENALTY = 0.5            # score multiplier for jobs outside your locations
# Jobs anywhere in the US get no penalty; only clearly overseas jobs are down-ranked.
# The location check itself is done in code (see apply_location / us_location.py).
PREFERRED_LOCATIONS = "anywhere in the United States (on-site, hybrid, or remote)"

# Approximate Haiku prices (USD per million tokens) — only used for the cost estimate.
PRICE_IN, PRICE_OUT = 1.00, 5.00


def compact_profile(bank):
    """A short text version of the experience bank — enough to judge fit, cheap to send."""
    lines = []
    for e in bank.get("education", []):
        lines.append(f"Education: {e.get('degree')}, {e.get('school')} (graduating {e.get('graduation')})")
    for c in bank.get("certifications", []):
        lines.append(f"Certification: {c.get('name')}")
    skills = bank.get("skills", {})
    lines.append("Skills: " + "; ".join(f"{k}: {', '.join(v)}" for k, v in skills.items() if v))
    for x in bank.get("experiences", []):
        lines.append(f"\n{x.get('type', '').upper()}: {x.get('title')} — {x.get('org')} "
                     f"({x.get('start') or '?'} to {x.get('end') or '?'})")
        for b in x.get("bullets", []):
            lines.append(f"- {b.get('text', '')}")
    return "\n".join(lines)


SCORING_RULES = f"""You score how well a job posting fits a candidate. The candidate is a
new-grad Business Analytics student (graduating soon) with one BI internship,
class projects, and prior non-analytics work. Judge ONLY from the profile given;
do not assume skills it doesn't show.

Score 0-100 on fit, IGNORING location (location is checked separately):
- Skills and experience match to the job's actual duties and requirements (most weight)
- Seniority: entry-level / new grad / 0-2 years fits well. Roles requiring 3+ years
  of full-time experience score low; senior, lead or manager roles score very low.
- Role type: BI, data, analytics, reporting, and operations-analysis roles fit best.
  Engineering-heavy, sales, legal, or unrelated roles score low.

Location: location_ok is true only if the job is {PREFERRED_LOCATIONS}.

missing_skills: up to 5 specific tools, technologies or skills the job requires or
prefers that the profile does NOT show. Use short, standard names (e.g. "Snowflake",
"dbt", "Python", "A/B testing", "Salesforce"). Do NOT include years of experience,
degrees, certifications-by-name-only, or location. Empty list if nothing is missing.

Return ONLY a JSON object, no other text:
{{"score": <0-100 integer>,
  "reason": "<one line, max 20 words: the main match and the main gap>",
  "seniority": "entry" | "mid" | "senior",
  "location_ok": true | false,
  "missing_skills": [<0-5 short skill names>],
  "flags": [<zero or more of: "too_senior", "not_analytics", "missing_key_skill", "location", "no_description">]}}"""


def score_one(client, profile, job, desc):
    body = (f"{SCORING_RULES}\n\n=== CANDIDATE PROFILE ===\n{profile}\n\n"
            f"=== JOB ===\nCompany: {job['company']}\nTitle: {job['title']}\n"
            f"Location: {job['location']}\n\nDescription:\n{desc[:6000] or '(no description available — judge from the title only)'}")
    resp = client.messages.create(model=MODEL, max_tokens=300,
                                  messages=[{"role": "user", "content": body}])
    text = resp.content[0].text
    result = json.loads(text[text.find("{"): text.rfind("}") + 1])
    model_score = max(0, min(100, int(result.get("score", 0))))
    flags = [f for f in result.get("flags", []) if f != "location"]   # location is set in code
    if not desc and "no_description" not in flags:
        flags.append("no_description")
    return apply_location({
        "model_score": model_score,
        "reason": result.get("reason", "").strip(),
        "seniority": result.get("seniority", ""),
        "missing_skills": [str(x).strip() for x in result.get("missing_skills", []) if str(x).strip()][:5],
        "flags": flags,
        "scored_on": date.today().isoformat(),
        "company": job["company"],
        "title": job["title"],
    }, job["location"]), resp.usage


def apply_location(s, location):
    """Set the final score from the model's skill-fit score plus a location check.

    The AI judged location unreliably (it marked jobs in New York and Charlotte as
    "not OK"), so location is decided in code with is_non_us(): only clearly
    overseas jobs get the penalty. Because this needs no API call, it's re-applied
    to every cached score each run, so changing the rule never costs credit.
    """
    loc_ok = not is_non_us(location)
    s["location_ok"] = loc_ok
    s["score"] = round(s["model_score"] * (1 if loc_ok else LOCATION_PENALTY))
    s["flags"] = [f for f in s.get("flags", []) if f != "location"] + ([] if loc_ok else ["location"])
    s.pop("location_rule", None)   # leftover from the older AI-judged location check
    return s


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def run(rescore=False, quiet=False):
    say = (lambda *a, **k: None) if quiet else print
    load_dotenv(os.path.join(HERE, ".env"))
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("No ANTHROPIC_API_KEY found. Check your .env file.")

    try:
        with open(FEED_FILE, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except FileNotFoundError:
        sys.exit("No job_feed.csv yet. Run: python discover_jobs.py")

    bank = load_json(BANK_FILE, None)
    if bank is None:
        sys.exit("Can't read experience_bank.json.")
    profile = compact_profile(bank)
    scores = {} if rescore else load_json(SCORES_FILE, {})
    details = descriptions.load_details()

    # Score new jobs, plus any scored before skill-gap tracking existed (one-time upgrade).
    # Score new jobs (plus any scored before skill-gap tracking existed). Clearly
    # overseas jobs are never sent to the API; discovery normally drops them
    # already, so this is just a safety net for older feeds.
    def needs_score(r):
        if is_non_us(r["location"]):
            return False
        s = scores.get(r["apply_url"])
        return s is None or "missing_skills" not in s
    todo = [r for r in rows if needs_score(r)]
    say(f"{len(rows)} jobs in feed, {len(rows) - len(todo)} already scored, {len(todo)} to score.")

    tokens_in = tokens_out = failed = 0
    if todo:
        client = anthropic.Anthropic(api_key=key)

        def work(job):
            try:
                desc = descriptions.get_description(job["apply_url"], details)
            except Exception:
                desc = ""
            return job, *score_one(client, profile, job, desc)

        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = [pool.submit(work, j) for j in todo]
            for n, fut in enumerate(as_completed(futures), start=1):
                try:
                    job, result, usage = fut.result()
                except Exception as e:
                    failed += 1
                    say(f"  ! scoring failed for one job ({type(e).__name__}) — will retry next run")
                    continue
                scores[job["apply_url"]] = result
                tokens_in += usage.input_tokens
                tokens_out += usage.output_tokens
                say(f"  [{n}/{len(todo)}] {result['score']:>3}  {job['company']} — {job['title']}")

        with open(SCORES_FILE, "w", encoding="utf-8") as f:
            json.dump(scores, f, indent=2, ensure_ascii=False)

    # Re-apply the location rule to every cached score (free — no API calls).
    for r in rows:
        if r["apply_url"] in scores and "model_score" in scores[r["apply_url"]]:
            apply_location(scores[r["apply_url"]], r["location"])
    with open(SCORES_FILE, "w", encoding="utf-8") as f:
        json.dump(scores, f, indent=2, ensure_ascii=False)

    # Write the scores into the feed and re-sort by AI score (unscored jobs go last).
    for r in rows:
        s = scores.get(r["apply_url"])
        r["ai_score"] = s["score"] if s else ""
        r["ai_reason"] = s["reason"] if s else ""
        r["ai_flags"] = ", ".join(s["flags"]) if s else ""
    rows.sort(key=lambda r: (r["ai_score"] == "", -(r["ai_score"] or 0), -int(r["fit_score"] or 0)))
    base = [k for k in rows[0].keys() if k not in ("ai_score", "ai_reason", "ai_flags")] if rows else []
    with open(FEED_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["ai_score", *base, "ai_reason", "ai_flags"])
        w.writeheader()
        w.writerows(rows)

    cost = tokens_in / 1e6 * PRICE_IN + tokens_out / 1e6 * PRICE_OUT
    say(f"\nScored {len(todo) - failed} job(s) this run (~${cost:.2f})."
        + (f" {failed} failed — re-run to retry." if failed else ""))
    say("\nTop 10 by AI fit:")
    for r in rows[:10]:
        say(f"  [{r['ai_score']}] {r['company']} — {r['title']} ({r['location']})\n        {r['ai_reason']}")
    return rows


SKIP_FLAGS = {"too_senior", "not_analytics", "location"}   # gaps a new skill won't fix


def skill_gaps(min_score=50, max_score=79, top=10):
    """Which missing skills show up most often in jobs you're CLOSE to qualifying for?
    Returns a plain-text report. Uses saved scores only — no API calls, free."""
    scores = load_json(SCORES_FILE, {})
    try:
        with open(FEED_FILE, encoding="utf-8") as f:
            feed_urls = {r["apply_url"] for r in csv.DictReader(f)}
    except FileNotFoundError:
        feed_urls = set(scores)
    current = {u: s for u, s in scores.items() if u in feed_urls}
    if not current:
        return "No scored jobs yet. Run: python score_jobs.py"

    no_data = sum(1 for s in current.values() if "missing_skills" not in s)
    pool = [s for s in current.values()
            if "missing_skills" in s
            and min_score <= s["score"] <= max_score
            and not SKIP_FLAGS & set(s.get("flags", []))]
    if not pool:
        msg = f"No jobs scored {min_score}-{max_score} (after skipping too-senior, non-analytics and out-of-area roles)."
        if no_data:
            msg += f" {no_data} job(s) were scored before skill tracking — run: python score_jobs.py"
        return msg

    # Count each skill once per job; group spelling variants ("snowflake" == "Snowflake").
    counts, spellings, examples, score_sum = {}, {}, {}, {}
    for s in pool:
        for skill in {x.strip() for x in s["missing_skills"]}:
            key = skill.lower()
            counts[key] = counts.get(key, 0) + 1
            spellings.setdefault(key, {}).setdefault(skill, 0)
            spellings[key][skill] += 1
            score_sum[key] = score_sum.get(key, 0) + s["score"]
            if len(examples.setdefault(key, [])) < 3 and s.get("company") not in examples[key]:
                examples[key].append(s.get("company", "?"))

    bank = load_json(BANK_FILE, {})
    bank_text = json.dumps(bank).lower()

    ranked = sorted(counts, key=lambda k: (-counts[k], -score_sum[k]))[:top]
    lines = [f"Skill gaps across {len(pool)} jobs scoring {min_score}-{max_score} "
             f"(too-senior, non-analytics and out-of-area roles skipped):\n"]
    for i, key in enumerate(ranked, start=1):
        name = max(spellings[key], key=spellings[key].get)
        pct = round(100 * counts[key] / len(pool))
        avg = round(score_sum[key] / counts[key])
        in_bank = re.search(r"(?<![a-z0-9])" + re.escape(key) + r"(?![a-z0-9])", bank_text)
        note = "  <- your bank mentions this: likely a WORDING gap, not a skill gap" if in_bank else ""
        lines.append(f"{i:>2}. {name}: {counts[key]} job{'s' if counts[key] != 1 else ''} ({pct}%), avg score {avg} "
                     f"— e.g. {', '.join(examples[key])}{note}")
    if no_data:
        lines.append(f"\n({no_data} job(s) were scored before skill tracking and aren't counted — "
                     "run: python score_jobs.py)")
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="AI fit scoring for your job feed")
    ap.add_argument("--rescore", action="store_true", help="Ignore saved scores and score everything again")
    ap.add_argument("--gaps", action="store_true", help="Show the most common skill gaps (free, no scoring)")
    args = ap.parse_args()
    if args.gaps:
        print(skill_gaps())
    else:
        run(rescore=args.rescore)
