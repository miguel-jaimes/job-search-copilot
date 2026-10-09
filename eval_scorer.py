# Job-Search Copilot — Eval, Step 3: the grader
#
# Runs the REAL production scorer (score_jobs.score_one) on the frozen test set,
# compares its answers to your labels, and reports how well it did.
#
#   test cases (eval/cases.jsonl) + your labels (eval/labels.csv) + this grader = the eval
#
# Each case is scored several times (RUNS) to measure consistency. The AI's label
# comes from the MEDIAN of those scores, so one odd run can't decide it.
#
# Never touches fit_scores.json or job_feed.csv: it calls score_one() directly,
# which makes one API call and returns the result without saving anything.
#
# RUN:
#   python eval_scorer.py --smoke                    # 3 cases x 1 run: cheap setup check, not logged
#   python eval_scorer.py --note "baseline"          # full run on the TUNE split
#   python eval_scorer.py --temperature 0 --note "temperature 0"
#   python eval_scorer.py --model claude-sonnet-4-5 --note "Sonnet"
#   python eval_scorer.py --rules eval/prompt_v2.txt --note "experience rules"
#                                    # try a new prompt WITHOUT editing score_jobs.py
#   python eval_scorer.py --holdout --note "final check"   # Step 7 only, ideally once
#
# Writes:
#   eval/runs/<date-time>_<split>.csv   one row per case (scores, labels, both reasons)
#   eval/results.csv                    one summary line per run (your before/after table)
# ---------------------------------------------------------------------------

import argparse
import csv
import json
import os
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import score_jobs

HERE = os.path.dirname(os.path.abspath(__file__))
EVAL_DIR = os.path.join(HERE, "eval")
CASES_FILE = os.path.join(EVAL_DIR, "cases.jsonl")
LABELS_FILE = os.path.join(EVAL_DIR, "labels.csv")
RUNS_DIR = os.path.join(EVAL_DIR, "runs")
RESULTS_FILE = os.path.join(EVAL_DIR, "results.csv")

RUNS = 3              # scores per case (to measure consistency)
STRONG_MIN = 70       # model_score 70+     -> Strong
MAYBE_MIN = 50        # model_score 50-69   -> Maybe; under 50 -> Skip
TOP_K = 10            # precision@k on the tune split (scaled down for smaller splits)
WORKERS = 5           # API calls at the same time
LABELS = ["Strong", "Maybe", "Skip"]
# Approximate prices (USD per million input / output tokens), only for the cost line.
PRICES = {"haiku": (1.00, 5.00), "sonnet": (3.00, 15.00), "opus": (15.00, 75.00)}

RESULT_FIELDS = ["timestamp", "split", "note", "model", "temperature", "runs", "cases",
                 "agreement_pct", "precision_at_k", "k", "precision_ceiling",
                 "missed_strong_as_skip", "skip_as_strong", "avg_spread", "label_flips",
                 "failed_calls", "cost_usd", "run_file"]


def to_label(score):
    if score >= STRONG_MIN:
        return "Strong"
    if score >= MAYBE_MIN:
        return "Maybe"
    return "Skip"


def load_cases(split):
    try:
        cases = [json.loads(line) for line in open(CASES_FILE, encoding="utf-8") if line.strip()]
    except FileNotFoundError:
        sys.exit("No eval/cases.jsonl. Run: python eval_freeze.py")
    try:
        with open(LABELS_FILE, encoding="utf-8-sig") as f:   # utf-8-sig also reads Excel's version
            labels = {r["case_id"].strip(): r for r in csv.DictReader(f)}
    except FileNotFoundError:
        sys.exit("No eval/labels.csv. Run: python eval_freeze.py")

    chosen, problems = [], []
    for c in cases:
        if c["split"] != split:
            continue
        row = labels.get(c["case_id"])
        label = (row or {}).get("my_label", "").strip().capitalize()
        if label not in LABELS:
            problems.append(f"{c['case_id']} ({label or 'blank'})")
            continue
        chosen.append({**c, "my_label": label, "my_reason": (row.get("my_reason") or "").strip()})
    if problems:
        sys.exit("These labels must be Strong, Maybe or Skip: " + ", ".join(problems))
    return chosen


def score_all(client, profile, cases, runs, say):
    """Score every case `runs` times, in parallel. Returns results per case + token totals."""
    jobs = [(c, r) for c in cases for r in range(runs)]
    results = {c["case_id"]: [] for c in cases}
    tokens_in = tokens_out = failed = 0

    def work(case):
        job = {"company": case["company"], "title": case["title"], "location": case.get("location", "")}
        for attempt in range(2):          # one retry for a hiccup (bad JSON, timeout)
            try:
                return score_jobs.score_one(client, profile, job, case["description"])
            except Exception as e:
                err = e
        raise err

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(work, c): c for c, _ in jobs}
        for n, fut in enumerate(as_completed(futures), start=1):
            case = futures[fut]
            try:
                result, usage = fut.result()
            except Exception as e:
                failed += 1
                say(f"  ! {case['case_id']} call failed ({type(e).__name__})")
                continue
            results[case["case_id"]].append(result)
            tokens_in += usage.input_tokens
            tokens_out += usage.output_tokens
            if n % 10 == 0 or n == len(jobs):
                say(f"  {n}/{len(jobs)} calls done")
    return results, tokens_in, tokens_out, failed


def summarize(cases, results):
    """One row per case: the median score, the AI's label, the spread across runs."""
    rows = []
    for c in cases:
        rs = results[c["case_id"]]
        if not rs:
            continue                       # every call failed; counted in failed_calls
        scores = [r["model_score"] for r in rs]
        med = statistics.median(scores)
        # The reason shown is from the run whose score is closest to the median.
        mid = min(rs, key=lambda r: abs(r["model_score"] - med))
        rows.append({
            "case_id": c["case_id"], "company": c["company"], "title": c["title"],
            "source": c.get("source", "feed"),
            "my_label": c["my_label"], "ai_label": to_label(med), "median_score": med,
            "scores": scores, "spread": max(scores) - min(scores),
            "flipped": len({to_label(s) for s in scores}) > 1,
            "my_reason": c["my_reason"], "ai_reason": mid["reason"],
            "experience": describe_experience(mid),
            "missing_skills": mid.get("missing_skills", []), "flags": mid.get("flags", []),
            "desc_chars": len(c["description"]),
        })
    return rows


def describe_experience(result):
    """What the AI read from the posting's experience requirement, and the cap code
    applied (only when the prompt asks for an "experience" field)."""
    exp = result.get("experience")
    if not exp:
        return ""
    sub = ", substitute OK" if exp.get("substitute_allowed") in (True, "true", "True") else ""
    cap = result.get("experience_cap", 100)
    after = f"capped at {cap}" if cap < 100 else "no cap"
    return (f"{exp.get('min_years_required', '?')} yrs {exp.get('kind', '?')}{sub} -> {after} "
            f"(AI's own score {result.get('raw_score', '?')})")


def metrics(rows, k):
    n = len(rows)
    agree = sum(r["my_label"] == r["ai_label"] for r in rows)
    confusion = {m: {a: 0 for a in LABELS} for m in LABELS}
    for r in rows:
        confusion[r["my_label"]][r["ai_label"]] += 1
    ranked = sorted(rows, key=lambda r: (-r["median_score"], r["case_id"]))
    top = ranked[:k]
    strong_total = sum(r["my_label"] == "Strong" for r in rows)
    return {
        "n": n, "agree": agree, "agreement_pct": round(100 * agree / n, 1) if n else 0,
        "confusion": confusion, "top": top,
        "precision_hits": sum(r["my_label"] == "Strong" for r in top),
        "k": len(top), "ceiling": min(len(top), strong_total),
        "missed": [r for r in rows if r["my_label"] == "Strong" and r["ai_label"] == "Skip"],
        "wasted": [r for r in rows if r["my_label"] == "Skip" and r["ai_label"] == "Strong"],
        "avg_spread": round(statistics.mean(r["spread"] for r in rows), 1) if n else 0,
        "flips": [r for r in rows if r["flipped"]],
    }


def report(m, rows, split, runs, model, temperature, cost, calls, failed, say):
    temp = "API default (1.0)" if temperature is None else temperature
    say(f"\n=== Eval: {split} split | {m['n']} cases x {runs} run(s) | {model} | temperature {temp} ===\n")
    say(f"Agreement: {m['agree']}/{m['n']} = {m['agreement_pct']}%")

    say("\nConfusion table (rows = my label, columns = AI label):")
    say(f"  {'':>8}" + "".join(f"{a:>8}" for a in LABELS))
    for mine in LABELS:
        say(f"  {mine:>8}" + "".join(f"{m['confusion'][mine][a]:>8}" for a in LABELS))

    ids = lambda rs: ", ".join(r["case_id"] for r in rs) or "none"
    say("\nCostly errors:")
    say(f"  Missed jobs  (I said Strong, AI said Skip): {len(m['missed'])}  [{ids(m['missed'])}]")
    say(f"  Wasted time  (I said Skip, AI said Strong): {len(m['wasted'])}  [{ids(m['wasted'])}]")

    say(f"\nPrecision@{m['k']}: {m['precision_hits']}/{m['k']} of the AI's top {m['k']} are ones I labeled Strong "
        f"(best possible: {m['ceiling']}/{m['k']})")
    say(f"Consistency: scores moved {m['avg_spread']} points on average between runs; "
        f"{len(m['flips'])} case(s) changed label between runs [{ids(m['flips'])}]")

    manual = [r for r in rows if r["source"] == "manual"]
    if manual:
        say(f"Pipeline misses (added by hand; the feed never surfaced them): {len(manual)} [{ids(manual)}]")

    say(f"Cost: ~${cost:.2f} ({calls} calls{f', {failed} failed' if failed else ''})")

    misses = [r for r in rows if r["my_label"] != r["ai_label"]]
    if misses:
        say(f"\nDisagreements ({len(misses)}), with both reasons:")
        for r in sorted(misses, key=lambda r: r["case_id"]):
            say(f"  {r['case_id']} {r['company']} — {r['title']}")
            say(f"      me: {r['my_label']:<6} \"{r['my_reason']}\"")
            say(f"      AI: {r['ai_label']:<6} {r['scores']}  \"{r['ai_reason']}\"")
            if r.get("experience"):
                say(f"      experience read: {r['experience']}")


def save(rows, m, args, split, model, temperature, cost, calls, failed, say):
    os.makedirs(RUNS_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    run_file = os.path.join(RUNS_DIR, f"{stamp}_{split}.csv")
    n = 2
    while os.path.exists(run_file):                  # never overwrite an earlier run
        run_file = os.path.join(RUNS_DIR, f"{stamp}_{split}_{n}.csv")
        n += 1
    fields = ["case_id", "company", "title", "source", "my_label", "ai_label", "agree", "median_score",
              "scores", "spread", "flipped", "my_reason", "ai_reason", "experience", "missing_skills", "flags",
              "desc_chars"]
    with open(run_file, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in sorted(rows, key=lambda r: r["case_id"]):
            w.writerow({**{k: r[k] for k in fields if k in r},
                        "agree": r["my_label"] == r["ai_label"],
                        "scores": " ".join(str(s) for s in r["scores"]),
                        "missing_skills": "; ".join(r["missing_skills"]),
                        "flags": ", ".join(r["flags"])})

    new = not os.path.exists(RESULTS_FILE)
    with open(RESULTS_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=RESULT_FIELDS)
        if new:
            w.writeheader()
        w.writerow({"timestamp": stamp, "split": split, "note": args.note, "model": model,
                    "temperature": "default" if temperature is None else temperature,
                    "runs": args.runs, "cases": m["n"], "agreement_pct": m["agreement_pct"],
                    "precision_at_k": m["precision_hits"], "k": m["k"], "precision_ceiling": m["ceiling"],
                    "missed_strong_as_skip": len(m["missed"]), "skip_as_strong": len(m["wasted"]),
                    "avg_spread": m["avg_spread"], "label_flips": len(m["flips"]),
                    "failed_calls": failed, "cost_usd": round(cost, 3),
                    "run_file": os.path.relpath(run_file, HERE)})
    say(f"\nSaved: {os.path.relpath(run_file, HERE)}  and one line in eval/results.csv")


def previous_holdout_runs():
    try:
        with open(RESULTS_FILE, encoding="utf-8") as f:
            return sum(1 for r in csv.DictReader(f) if r["split"] == "holdout")
    except FileNotFoundError:
        return 0


def main(argv=None, client=None, say=print):
    ap = argparse.ArgumentParser(description="Grade the AI fit scorer against your labels")
    ap.add_argument("--note", default="", help="What changed in this run (goes in results.csv)")
    ap.add_argument("--runs", type=int, default=RUNS, help=f"Scores per case (default {RUNS})")
    ap.add_argument("--temperature", type=float, help="Try a temperature (default: whatever score_jobs.py uses)")
    ap.add_argument("--model", help="Try a different model (default: score_jobs.MODEL)")
    ap.add_argument("--holdout", action="store_true", help="Grade the HOLDOUT split (Step 7 only)")
    ap.add_argument("--smoke", action="store_true", help="3 cases x 1 run, nothing saved: a cheap setup check")
    ap.add_argument("--rules", help="Try a candidate prompt from a text file instead of score_jobs.SCORING_RULES")
    args = ap.parse_args(argv)

    split = "holdout" if args.holdout else "tune"
    if args.holdout:
        before = previous_holdout_runs()
        say("NOTE: holdout is the final check. Every look at it leaks a little information into "
            "your decisions, so run it once, after you've finished tuning.")
        if before:
            say(f"      (You've run holdout {before} time(s) before.)")

    # Experiment settings are swapped in for this run only; score_jobs.py itself is unchanged.
    if args.rules:
        try:
            with open(args.rules, encoding="utf-8") as f:
                score_jobs.SCORING_RULES = f.read().strip()
        except FileNotFoundError:
            sys.exit(f"Can't find the rules file: {args.rules}")
        tag = f"[rules: {os.path.basename(args.rules)}]"
        args.note = f"{args.note} {tag}".strip()   # results.csv records which prompt was used
        say(f"Using candidate prompt {args.rules} (production score_jobs.py is unchanged).")
    if args.temperature is not None:
        score_jobs.TEMPERATURE = args.temperature
    if args.model:
        score_jobs.MODEL = args.model
    model, temperature = score_jobs.MODEL, score_jobs.TEMPERATURE

    cases = load_cases(split)
    if args.smoke:
        cases, args.runs = cases[:3], 1
    if not cases:
        sys.exit(f"No labeled cases in the {split} split.")

    bank = score_jobs.load_json(score_jobs.BANK_FILE, None)
    if bank is None:
        sys.exit("Can't read experience_bank.json.")
    profile = score_jobs.compact_profile(bank)

    if client is None:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(HERE, ".env"))
        key = os.getenv("ANTHROPIC_API_KEY")
        if not key:
            sys.exit("No ANTHROPIC_API_KEY found. Check your .env file.")
        client = score_jobs.anthropic.Anthropic(api_key=key)

    calls = len(cases) * args.runs
    say(f"Scoring {len(cases)} {split} cases x {args.runs} run(s) = {calls} API calls...")
    results, t_in, t_out, failed = score_all(client, profile, cases, args.runs, say)
    p_in, p_out = next((v for name, v in PRICES.items() if name in model),
                       (score_jobs.PRICE_IN, score_jobs.PRICE_OUT))
    cost = t_in / 1e6 * p_in + t_out / 1e6 * p_out

    rows = summarize(cases, results)
    if not rows:
        sys.exit("Every API call failed. Check your API key and connection.")
    # precision@k: 10 for the 30-case tune split; scaled down for smaller splits (holdout: 3).
    k = max(1, min(TOP_K, round(TOP_K * len(rows) / 30)))
    m = metrics(rows, k)
    report(m, rows, split, args.runs, model, temperature, cost, calls, failed, say)

    if args.smoke:
        say("\n(Smoke test: nothing saved.)")
    else:
        save(rows, m, args, split, model, temperature, cost, calls, failed, say)
    return m


if __name__ == "__main__":
    main()
