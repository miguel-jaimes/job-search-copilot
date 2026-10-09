# Job-Search Copilot — Eval, Step 1: freeze the test set
#
# Picks ~40 postings from the current feed and saves a FROZEN copy of each one
# (full description text included), so the eval keeps testing the same jobs
# even after the postings expire or the feed changes.
#
# Writes three files into eval/:
#   cases.jsonl            one test case per line (the frozen postings). Don't open
#                          it before labeling: it records which score bucket each
#                          case came from.
#   labels.csv             your answer key to fill in (my_label, my_reason).
#                          It deliberately does NOT show the AI's score (blind labeling).
#   postings_to_label.md   every posting's frozen text, easy to read while labeling.
#
# Nothing here calls the AI, and nothing writes to job_feed.csv, fit_scores.json
# or job_details.json. It only READS them.
#
# RUN:
#   python eval_freeze.py --preview      # show which jobs would be picked; writes nothing
#   python eval_freeze.py                # freeze the set (refuses if one already exists)
#   python eval_freeze.py --force        # rebuild it (backs up labels.csv first)
#   python eval_freeze.py --add --url URL --company "TI" --title "..." [--location ...]
#                         [--desc-file job.txt] [--holdout]
#                                        # add one posting by hand, e.g. one the
#                                        # pipeline missed before the AI saw it
# ---------------------------------------------------------------------------

import argparse
import csv
import json
import os
import random
import re
import shutil
import sys
import textwrap
from datetime import date, datetime

import descriptions
from us_location import is_non_us

HERE = os.path.dirname(os.path.abspath(__file__))
FEED_FILE = os.path.join(HERE, "job_feed.csv")
SCORES_FILE = os.path.join(HERE, "fit_scores.json")
EVAL_DIR = os.path.join(HERE, "eval")
CASES_FILE = os.path.join(EVAL_DIR, "cases.jsonl")
LABELS_FILE = os.path.join(EVAL_DIR, "labels.csv")
POSTINGS_FILE = os.path.join(EVAL_DIR, "postings_to_label.md")

SEED = 42                 # fixed seed: the same feed always gives the same picks and split
BUCKETS = [               # (name, lowest AI score, highest AI score, how many cases to pick)
    ("high", 70, 100, 13),
    ("mid", 50, 69, 14),
    ("low", 0, 49, 13),
]
HOLDOUT_EVERY = 4         # every 4th case goes to holdout: 40 cases -> 30 tune, 10 holdout
TRICKY_PER_TYPE = 2       # up to 2 cases of each tricky type below are included on purpose
AI_READ_LIMIT = 6000      # score_one() only sends the first 6,000 characters to the AI

# Tricky job types to include on purpose (the kinds of jobs the scorer gets wrong).
# Each one is checked against the title (and description where noted).
TRICKY = [
    ("SAP role",            lambda t, d: re.search(r"\bSAP\b", t + " " + d)),
    ("accounting/finance",  lambda t, d: re.search(r"accounting|financial|finance|\btax\b", t, re.I)),
    ("3+ years required",   lambda t, d: re.search(
        r"\b([3-9]|10)\s*(\+|-\s*\d+|to\s+\d+)?\s*\+?\s*years?\b[^.]{0,60}experience", d, re.I)),
    ("senior-sounding title", lambda t, d: re.search(
        r"\b(ii|iii|iv|[2-5]|staff|principal|consultant|advisor|specialist)\s*$|\b(ii|iii)\b", t, re.I)),
    ("marketing/sales data", lambda t, d: re.search(r"marketing|sales", t, re.I)),
]

LABEL_FIELDS = ["case_id", "company", "title", "url", "split", "my_label", "my_reason"]


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def bucket_of(score):
    for name, lo, hi, _ in BUCKETS:
        if lo <= score <= hi:
            return name
    return None


def candidate_pool():
    """Feed jobs that can be test cases: scored, in the US, one per company+title."""
    try:
        with open(FEED_FILE, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except FileNotFoundError:
        sys.exit("No job_feed.csv yet. Run: python discover_jobs.py --score")
    scores = load_json(SCORES_FILE, {})

    pool, seen = [], set()
    for r in rows:
        s = scores.get(r["apply_url"])
        if not s or is_non_us(r.get("location", "")):
            continue
        # Use the AI's raw skill-fit score (before the location penalty), because
        # the eval measures skill fit only.
        ai = s.get("model_score", s.get("score"))
        if ai is None:
            continue
        key = (r["company"].strip().lower(), r["title"].strip().lower())
        if key in seen:            # duplicate postings would count the same job twice
            continue
        seen.add(key)
        pool.append({"url": r["apply_url"], "company": r["company"], "title": r["title"],
                     "location": r.get("location", ""), "ai": int(ai), "bucket": bucket_of(int(ai))})
    return pool


def pick_cases(pool, details, client, say):
    """Choose ~40 jobs: tricky types first, then fill each score bucket. Fetches
    descriptions and swaps out any job whose description can't be found."""
    rng = random.Random(SEED)
    order = pool[:]
    rng.shuffle(order)
    picked, used = [], set()

    def take(job, why):
        try:
            desc = descriptions.get_description(job["url"], details, client)
        except Exception as e:
            say(f"  (skipped, description fetch failed: {type(e).__name__}) {job['company']} — {job['title']}")
            used.add(job["url"])
            return False
        used.add(job["url"])
        if not desc.strip():       # a case with no text isn't a fair test of the scorer
            say(f"  (skipped, no description found) {job['company']} — {job['title']}")
            return False
        picked.append({**job, "description": desc, "pick_reason": why})
        return True

    say("Fetching descriptions (Workday/Oracle are fetched live, so this can take a minute)...")
    for name, test in TRICKY:
        n = 0
        for job in order:
            if n >= TRICKY_PER_TYPE:
                break
            if job["url"] in used:
                continue
            desc_hint = details.get(job["url"], {}).get("description", "")
            if test(job["title"], desc_hint) and take(job, f"tricky: {name}"):
                n += 1

    for name, lo, hi, target in BUCKETS:
        have = sum(1 for c in picked if c["bucket"] == name)
        for job in order:
            if have >= target:
                break
            if job["bucket"] == name and job["url"] not in used and take(job, f"bucket: {name}"):
                have += 1
        if have < target:
            say(f"  Note: only {have} jobs available in the '{name}' bucket (wanted {target}).")
    return picked


def assign_split_and_ids(picked):
    """Stratified split: list the cases bucket by bucket (shuffled inside each bucket),
    then send every 4th one to holdout, so tune and holdout get a similar mix of high,
    mid and low jobs. Then shuffle again before numbering, so the case order in
    labels.csv gives no hint of the AI's opinion."""
    rng = random.Random(SEED + 1)
    ordered = []
    for name, *_ in BUCKETS:
        group = [c for c in picked if c["bucket"] == name]
        rng.shuffle(group)
        ordered += group
    for i, c in enumerate(ordered):
        c["split"] = "holdout" if i % HOLDOUT_EVERY == HOLDOUT_EVERY - 1 else "tune"
    rng.shuffle(ordered)
    for i, c in enumerate(ordered, start=1):
        c["case_id"] = f"c{i:02d}"
    return ordered


def case_record(c, source):
    return {"case_id": c["case_id"], "split": c["split"], "source": source,
            "company": c["company"], "title": c["title"], "location": c["location"],
            "url": c["url"], "frozen_on": date.today().isoformat(),
            "pick_reason": c.get("pick_reason", ""), "feed_ai_score": c.get("ai"),
            "description": c["description"]}


def posting_section(c):
    desc = c["description"]
    cut = len(desc) > AI_READ_LIMIT
    body = desc[:AI_READ_LIMIT] if cut else desc
    text = textwrap.fill(body, width=100)
    if cut:
        text += ("\n\n----- [The AI stops reading here: 6,000-character limit. "
                 "Requirements below this line are invisible to it.] -----\n\n"
                 + textwrap.fill(desc[AI_READ_LIMIT:], width=100))
    return (f"\n\n## {c['case_id']} | {c['company']} — {c['title']}\n"
            f"Location: {c['location'] or '(none listed)'}  |  Split: {c['split']}  |  {c['url']}\n\n{text}\n")


def write_labels_row(writer, c):
    writer.writerow({"case_id": c["case_id"], "company": c["company"], "title": c["title"],
                     "url": c["url"], "split": c["split"], "my_label": "", "my_reason": ""})


def freeze(args, client, say):
    if os.path.exists(CASES_FILE) and not (args.force or args.preview):
        sys.exit("A frozen test set already exists (eval/cases.jsonl). That's on purpose: the\n"
                 "set must not change between runs. To rebuild it anyway: python eval_freeze.py --force\n"
                 "(your labels.csv is backed up to labels.backup.csv first).")

    details = descriptions.load_details()
    pool = candidate_pool()
    say(f"{len(pool)} scored US jobs in the feed (duplicates removed).")
    for name, lo, hi, _ in BUCKETS:
        say(f"  {name:>4} ({lo}-{hi}): {sum(1 for j in pool if j['bucket'] == name)} jobs")

    picked = pick_cases(pool, details, client, say)
    cases = assign_split_and_ids(picked)

    if args.preview:
        # Preview shows COUNTS, never a job's score or bucket: you label these jobs
        # next, and seeing the AI's opinion first would bias your labels.
        say(f"\nPREVIEW: {len(cases)} cases would be frozen (nothing written).")
        say("Cases per score bucket (tune / holdout):")
        for name, *_ in BUCKETS:
            group = [c for c in cases if c["bucket"] == name]
            say(f"  {name:>4}: {len(group):>2}  ({sum(c['split'] == 'tune' for c in group)} / "
                f"{sum(c['split'] == 'holdout' for c in group)})")
        say("Tricky types included on purpose:")
        for name, _ in TRICKY:
            say(f"  {name}: {sum(c['pick_reason'] == 'tricky: ' + name for c in cases)}")
        say("Jobs (in case order, no scores shown):")
        for c in cases:
            say(f"  {c['case_id']}  {c['company']} — {c['title']}")
        say(f"\nTune: {sum(c['split'] == 'tune' for c in cases)}  "
            f"Holdout: {sum(c['split'] == 'holdout' for c in cases)}")
        return

    os.makedirs(EVAL_DIR, exist_ok=True)
    if os.path.exists(LABELS_FILE):
        shutil.copy(LABELS_FILE, os.path.join(EVAL_DIR, "labels.backup.csv"))
        say("Backed up the old labels to eval/labels.backup.csv")

    with open(CASES_FILE, "w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(case_record(c, "feed"), ensure_ascii=False) + "\n")
    # utf-8-sig adds a small marker at the start so Excel shows accents and other
    # non-English characters correctly.
    with open(LABELS_FILE, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=LABEL_FIELDS)
        w.writeheader()
        for c in cases:
            write_labels_row(w, c)
    with open(POSTINGS_FILE, "w", encoding="utf-8") as f:
        f.write("# Postings to label\n\nRubric: Strong / Maybe / Skip. Judge fit only; ignore location.")
        for c in cases:
            f.write(posting_section(c))

    tune = sum(c["split"] == "tune" for c in cases)
    say(f"\nFroze {len(cases)} cases: {tune} tune, {len(cases) - tune} holdout.")
    say("  eval/cases.jsonl          (the frozen test set; don't open it before labeling)")
    say("  eval/labels.csv           (fill in my_label and my_reason)")
    say("  eval/postings_to_label.md (read the postings here)")


def guess_ats(url):
    if "myworkdayjobs.com" in url or "myworkdaysite.com" in url:
        return "workday"
    if "oraclecloud.com" in url:
        return "oracle"
    return ""


def add_one(args, client, say):
    if not os.path.exists(CASES_FILE):
        sys.exit("Freeze the main set first: python eval_freeze.py")
    if not (args.url and args.company and args.title):
        sys.exit("--add needs --url, --company and --title.")

    existing = [json.loads(line) for line in open(CASES_FILE, encoding="utf-8") if line.strip()]
    if any(c["url"] == args.url for c in existing):
        sys.exit("That URL is already in the test set.")

    try:
        with open(FEED_FILE, encoding="utf-8") as f:
            in_feed = any(r["apply_url"] == args.url for r in csv.DictReader(f))
    except FileNotFoundError:
        in_feed = False

    desc = ""
    if args.desc_file:
        with open(args.desc_file, encoding="utf-8") as f:
            desc = descriptions.strip_html(f.read())
    if not desc:
        desc = descriptions.get_description(args.url, descriptions.load_details(), client)
    if not desc and guess_ats(args.url):
        desc = descriptions.fetch_live(args.url, guess_ats(args.url), client)
    if not desc.strip():
        sys.exit("Couldn't get a description for that URL. Paste it into job.txt and add:  --desc-file job.txt")

    next_n = max(int(c["case_id"][1:]) for c in existing) + 1
    c = {"case_id": f"c{next_n:02d}", "split": "holdout" if args.holdout else "tune",
         "company": args.company, "title": args.title, "location": args.location or "",
         "url": args.url, "description": desc,
         "pick_reason": "added by hand" + ("" if in_feed else " (pipeline missed it)"),
         "ai": None}
    # "manual" = the pipeline never surfaced it, a miss BEFORE the AI.
    source = "feed" if in_feed else "manual"

    with open(CASES_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(case_record(c, source), ensure_ascii=False) + "\n")
    with open(LABELS_FILE, "a", newline="", encoding="utf-8") as f:
        write_labels_row(csv.DictWriter(f, fieldnames=LABEL_FIELDS), c)
    with open(POSTINGS_FILE, "a", encoding="utf-8") as f:
        f.write(posting_section(c))
    say(f"Added {c['case_id']} ({c['split']}, source: {source}): {args.company} — {args.title}")
    if in_feed:
        say("  Note: this job IS in the feed, so it doesn't count as a pipeline miss.")


def refresh(args, client, say):
    """Re-copy the description of some cases from the CURRENT job_details.json.

    Only for fixing a data bug (e.g. the Oct 9 Amazon fix: the feed had saved a
    200-character teaser instead of the full posting). Case IDs, split, labels
    and every other case stay exactly the same, so the test set is still the same
    40 jobs; only their text is repaired. Run discover_jobs.py first so
    job_details.json holds the fixed text. Backs up cases.jsonl first.
    """
    if not os.path.exists(CASES_FILE):
        sys.exit("No eval/cases.jsonl yet. Run: python eval_freeze.py")
    cases = [json.loads(line) for line in open(CASES_FILE, encoding="utf-8") if line.strip()]
    wanted = {x.strip() for x in args.refresh.split(",") if x.strip()}
    if args.refresh.strip().lower() == "amazon":
        wanted = {c["case_id"] for c in cases if "amazon.jobs" in c["url"]}
    unknown = wanted - {c["case_id"] for c in cases}
    if unknown:
        sys.exit("Unknown case id(s): " + ", ".join(sorted(unknown)))
    if args.desc_file and len(wanted) != 1:
        sys.exit("--desc-file refreshes ONE case: pass a single case id.")

    details = descriptions.load_details()
    changed, missing = 0, []
    for c in cases:
        if c["case_id"] not in wanted:
            continue
        if args.desc_file:
            with open(args.desc_file, encoding="utf-8") as f:
                new = descriptions.strip_html(f.read())
        else:
            new = descriptions.get_description(c["url"], details, client)
        if not new.strip():
            if c.get("refreshed_on"):
                say(f"  {c['case_id']}: not in the feed now; keeping the text refreshed on {c['refreshed_on']}")
            else:
                missing.append(c["case_id"])
            continue
        if new == c["description"]:
            hint = "  <- still short: did you re-run discover_jobs.py first?" if len(new) < 300 else ""
            say(f"  {c['case_id']}: unchanged ({len(new)} chars){hint}")
            continue
        say(f"  {c['case_id']}: {len(c['description'])} -> {len(new)} chars  {c['company']} — {c['title']}")
        c["description"] = new
        c["refreshed_on"] = date.today().isoformat()
        changed += 1

    if changed:
        backup = os.path.join(EVAL_DIR, f"cases.backup-{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.jsonl")
        shutil.copy(CASES_FILE, backup)
        with open(CASES_FILE, "w", encoding="utf-8") as f:
            for c in cases:
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
        with open(POSTINGS_FILE, "w", encoding="utf-8") as f:
            f.write("# Postings to label\n\nRubric: Strong / Maybe / Skip. Judge fit only; ignore location.")
            for c in cases:
                f.write(posting_section(c))
        say(f"\nRefreshed {changed} case(s). Old version saved to eval/{os.path.basename(backup)}")
    else:
        say("\nNothing changed.")
    if missing:
        say(f"No description found for: {', '.join(missing)} (posting probably closed and left the feed).\n"
            "  Paste the posting into job.txt from the page, then for each one:\n"
            "  python eval_freeze.py --refresh CASE_ID --desc-file job.txt")


def main(argv=None, client=None, say=print):
    ap = argparse.ArgumentParser(description="Freeze the fit-scorer eval test set")
    ap.add_argument("--preview", action="store_true", help="Show the picks; write nothing")
    ap.add_argument("--force", action="store_true", help="Rebuild an existing test set")
    ap.add_argument("--add", action="store_true", help="Add one posting by hand")
    ap.add_argument("--url"); ap.add_argument("--company"); ap.add_argument("--title")
    ap.add_argument("--location")
    ap.add_argument("--desc-file", help="Text file with the posting (for sites the tool can't fetch)")
    ap.add_argument("--holdout", action="store_true", help="Put the added case in holdout instead of tune")
    ap.add_argument("--refresh", help='Re-copy descriptions for these case ids ("c16,c18" or "amazon")')
    args = ap.parse_args(argv)

    own = client is None
    if own:
        import httpx
        client = httpx.Client(follow_redirects=True, timeout=20, headers={
            "Accept": "application/json", "User-Agent": "job-copilot/1.0 (personal job search)"})
    try:
        if args.refresh:
            refresh(args, client, say)
        elif args.add:
            add_one(args, client, say)
        else:
            freeze(args, client, say)
    finally:
        if own:
            client.close()


if __name__ == "__main__":
    main()
