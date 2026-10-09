# Fit-Scorer Eval: Notes

## Test set
- 40 postings frozen Oct 8, 2026 from the live feed (`eval_freeze.py`, seed 42): 13 high / 14 mid / 13 low
  AI score, plus 2 of each tricky type (SAP, accounting/finance, 3+ years, senior-sounding title,
  marketing/sales). Stratified split: 30 tune, 10 holdout.
- Labeled blind (no AI scores visible) against `rubric.md`. Rubric was tightened after a consistency
  pass: 6 labels changed to match it, and the rubric gained explicit experience and required-tool rules.
- Tune labels: 11 Strong, 8 Maybe, 11 Skip. Holdout: 2 Strong, 5 Maybe, 3 Skip.

## Label audit (after the first baseline)
Rule: change a label only when the posting text proves it wrong, never because the AI disagrees.
- c19: posting requires 10+ years; I missed it while labeling. Changed Maybe -> Skip (a label error the
  eval caught).
- c07: posting asks for 3+ years of analytics but says education plus projects can substitute. Kept
  Strong; added the substitution rule to the rubric.

## Baseline (production settings: Haiku, temperature default, current SCORING_RULES)
Agreement 33.3% (10/30) | Precision@10 5/10 | Missed Strong as Skip 2 | Skip as Strong 1 |
avg spread 3.1 pts | 4 label flips | ~$0.36 per run

Two baseline runs with identical settings (before and after the label audit) moved individual cases:
c07 went Skip -> Maybe, c16 went Strong -> Maybe. So the costly-error counts moved by noise alone.
With 30 cases, one case = 3.3 points of agreement. A change has to beat that noise to count.

## Diagnosis: 20 disagreements grouped by cause

| Cause | Cases | # | Fix goes in |
|---|---|---|---|
| Amazon postings are cut off: the AI never sees the qualifications, gives ~72 and says "description incomplete" | c16 c18 c20 c23 c27 c35 | 6 | Discovery (save the full posting) |
| Experience rules wrong both ways: general "2+ years" over-penalized (62); "3+ required" under-penalized (62, should be <50); substitution clauses ignored | c07 c22 c36 / c33 c37 | 5 | `SCORING_RULES` |
| Domain knowledge treated as a hard gap (healthcare, fintech, PBM); no "partly analytics = Maybe" middle; one pure-finance role scored too high (c28) | c05 c10 c13 c17 c21 c24 c26 c28 | 8 | `SCORING_RULES` |
| Critical tool in the title (SAP) barely penalized: 78 | c01 | 1 | `SCORING_RULES` |

Also seen: the AI only uses a few scores (32, 42, 62, 72, 78). The prompt never says what each band means.

## Plan (one change per run)
1. Temperature 0 -> consistency. Goes first so later comparisons aren't swamped by noise.
2. Full Amazon descriptions -> the 6 Amazon misses.
3. Prompt: score bands tied to the rubric, exact experience rules, required-tool rule, domain
   knowledge is learnable. General rules only; no company or job names.
4. Holdout once at the end. The rubric was written while looking at tune jobs, so holdout is the
   check that step 3 didn't just fit these 30.

## Run log
- Run 1, temperature 0: agreement 36.7% (+1 case, within noise), P@10 5/10, missed 2, wasted 2, spread 0, flips 0. Kept. Consistent, not more correct: c16 settled on its wrong answer (72).
- Amazon data bug: fetcher saved description_short (~200 chars); full text + basic/preferred qualifications were in the same API response. Fixed in discover_jobs.py; 7 Amazon cases refreshed (now 4,900-7,200 chars) with eval_freeze.py --refresh amazon. Labels unchanged (they were made from the full postings).
- Known limit, left as is: score_one reads only the first 6,000 chars; qualifications at the end of long postings (c16, c18) may be cut.
- Run 2, full Amazon descriptions: agreement 53.3% (+5 cases), P@10 7/10, missed 2, wasted 0. Only Amazon cases moved; every other case scored the same. 5 of 6 Amazon misses fixed; c18 overshot (72 -> 28, "2+ yrs full-time" treated as senior). Biggest single gain was a DATA fix, not a prompt fix.
- Run 3b, prompt_v3 (experience band = hard limit; unspecified experience counts internships): agreement 66.7%, P@10 7/10, missed 1 (c07), wasted 0. Fixed c05 c16 c23 c35 c37; broke c12 c14 (both role-type/domain reasons). Current best prompt.
- Label audit 2 (c07, c22, c36 wording check): all three postings say what my labels assumed (c07 substitution clause @2706, c22 "2+ yrs experience with Alteryx/Tableau...", c36 "2 or more years of relevant experience"). The AI misread text it could see -> prompt wording isn't the fix.
- Run 4, prompt_v4 + experience_cap() in code (AI reads years/kind/substitution, code applies rubric caps): agreement 63.3% (-1 case vs v3, noise), Skip row 11/11, missed 1 (c21). 10 of 11 misses = AI BELOW my label. Reading errors: c22, c36 read as "professional" (posting says "experience with tools" / "relevant experience"). Years still leak into AI's own score (c07). Biggest group: domain knowledge treated as a gap (c05 c10 c13 c14 c21 c26 c27). Kept: architecture makes the Skip side reliable and errors diagnosable.
- Run 5, prompt_v5 (defined "real gap" + "analytics work"): agreement 50.0%, Strong row 1/11, P@10 8/10, missed 3. Too-narrow definition of analytics (systems, ops, product roles pushed out). Rejected. Final pick: v4 + code caps, temperature 0.
