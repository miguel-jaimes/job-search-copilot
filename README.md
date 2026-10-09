# Job-Search Copilot

A human-in-the-loop Python tool that finds the right ~40 analyst roles instead of blasting 400 applications, then tailors a truthful one-page resume for each one.

It pulls live postings from 85 employers' public hiring APIs, ranks them with an LLM, and builds a resume PDF using **only** facts from a verified experience bank. It never submits anything: a person reviews every resume and clicks submit.

**Stack:** Python 3.12 · REST/JSON APIs · Claude API (Haiku + Sonnet) · MCP server · LaTeX (Tectonic) · `httpx` · `ThreadPoolExecutor`

<!--
## Demo
Screenshots go here in the next version:
- the ranked job feed
- a sample resume rendered from the fictional example bank
-->

---

## How it works

```mermaid
flowchart LR
    A["companies.json<br/>85 employers, 6 ATS types"] --> B["discover_jobs.py<br/>fetch · filter · dedupe"]
    B --> C[("job_feed.csv<br/>job_details.json")]
    C --> D["score_jobs.py<br/>Claude Haiku · cached"]
    D --> E["Ranked shortlist<br/>+ skill-gap report"]
    E --> F["Tailoring<br/>Claude selects content"]
    G["experience_bank.json<br/>only source of facts"] --> F
    H["resume_rules.py<br/>honesty guardrails"] --> F
    F --> I["render_resume.py<br/>JSON → LaTeX → 1-page PDF"]
    I --> J["tracker.py<br/>applications.csv"]
    M{{"MCP server · 13 tools<br/>runs it all from chat"}} -.-> B & D & F & I & J
```

| Stage | What happens |
|---|---|
| **1. Discover** | Fetches postings from Workday, Oracle HCM, Greenhouse, Lever, Ashby and Amazon. Every fetcher returns the same normalized record (`title, location, url, date, description`), so nothing downstream cares which system a job came from. Filters by title keywords, location and posting age. |
| **2. Score** | Claude Haiku rates each job's fit 0–100 with a one-line reason, flags (`too_senior`, `not_analytics`, …) and up to five missing skills, at temperature 0 so the same job always gets the same score. The model also extracts the posting's experience requirement, and code caps the score (3+ years required → at most 49). Scores are cached by URL, so only new postings cost money. |
| **3. Tailor** | Claude picks and reorders bullets from the experience bank to match the posting. It returns structured JSON, not a document. |
| **4. Render** | Plain Python turns that JSON into LaTeX and compiles a PDF. If it spills onto page 2, the renderer trims the least relevant bullet and recompiles until it fits. |
| **5. Track** | Logs each application and keeps a reusable library of screening-question answers. |

All of it runs from a chat window through an **MCP server** in the Claude desktop app: *"refresh my feed" → "show my top 10" → "tailor my resume to row 3" → "log that I applied"*.

## Key results

- **85 employers across 6 applicant-tracking systems**, searched nationwide, using only their public job-board APIs.
- **Full refresh cut from 20+ minutes to about 1 minute** after the employer list doubled: companies are fetched in parallel (8 workers), paging stops once relevance-sorted results stop matching, and only server errors (5xx) are retried. A mock benchmark went from 142 s to 7 s with identical results.
- **An AI ranking measured against a hand-labeled answer key:** agreement with my labels rose from 33% to 63% on 30 tuning jobs and from 50% to 70% on 10 held-out jobs; 7 of the AI's top 10 are now roles I'd actually apply to. See [Evaluation](#evaluation-measuring-the-ai-fit-scorer).
- **Cheap at scale:** the full feed of 560 jobs was rescored for $2.73, scores are cached, and clearly overseas jobs are filtered in code and never sent to the API.
- **13-tool MCP server** covering the feed, job descriptions, tailoring, rendering, skill gaps, screening answers and application tracking.

## Design decisions

1. **Human in the loop.** The tool never submits applications. It speeds up finding and tailoring; a person makes every decision.
2. **No invented facts.** The experience bank is the single source of truth. The model may select, reorder and lightly rephrase, but never add tools, numbers or experiences. The same rules file (`resume_rules.py`) guards both tailoring paths, so they can't drift apart.
3. **Content separate from layout.** The LLM only produces JSON content; deterministic code owns the layout, LaTeX escaping, file names and the one-page limit.
4. **Let the LLM judge fuzzy things and extract facts; let code decide rules.** Skill fit is a judgment call, so the model scores it. Location and experience thresholds are rules, so code applies them after scoring: the model reports "3 years required", and code enforces the cap. (The model was inconsistent about both, which is how this split came about.)
5. **Sanctioned APIs only.** Public ATS JSON endpoints, polite parallelism (one request stream per employer), no LinkedIn or Indeed scraping.
6. **Cheap model for bulk work, stronger model for writing.** Haiku scores hundreds of jobs; Sonnet (or Claude in the app) writes resumes.

## Evaluation: measuring the AI fit scorer

The copilot ranks every job with an LLM (Claude Haiku) that scores fit from 0 to 100. To find out whether those
scores are right, and to improve them with evidence instead of guesswork, I built an **eval**: a fixed set of real
postings, a hand-made answer key, and a grader that compares the two.

### Results

| Metric | Before | After | What it means |
| --- | --- | --- | --- |
| Agreement with my labels (30 tune jobs) | 33% | **63%** | AI label (Strong / Maybe / Skip) matches mine |
| Agreement on 10 held-out jobs (never tuned on) | 50% | **70%** | The changes generalize |
| Precision@10 | 5/10 | **7/10** | Of the AI's top 10, how many I'd actually apply to |
| Jobs I'd skip ranked "Strong" | 1 | **0** | Wasted-time errors |
| Score spread between identical runs | 3.1 pts | **0** | Same job, same score, every run |
| Cost of one full eval run | | ~$0.40 | 90 API calls on Haiku |

### Method

1. **Frozen test set.** 40 real postings from the live feed, full text saved so the test never changes as postings
   expire. Over-sampled high and borderline scores plus known-tricky types (SAP roles, finance roles,
   "3+ years required", senior-sounding titles), split 30 tune / 10 holdout with a fixed seed.
2. **Blind labels.** I labeled every posting Strong / Maybe / Skip against a written rubric, without seeing the AI's
   scores. When the AI disagreed, I checked the posting text before changing anything; one disagreement turned out
   to be my labeling error.
3. **Grader.** `eval_scorer.py` calls the production scoring function directly (no cache, writes nothing), three
   times per job, and reports agreement, a confusion table, the two costly errors, precision@10, consistency
   and cost. Every run is logged to `eval/results.csv`.
4. **One change per run**, each kept or rejected on the numbers:

| Change | Tune agreement | Kept? |
| --- | --- | --- |
| Baseline (production as it was) | 33% | |
| Temperature 0 | 37% | Yes: consistency |
| Fixed a data bug: Amazon postings were saved as 200-character teasers | 53% | Yes |
| Experience rules in the prompt | 57% | Superseded |
| Removed a conflict between two prompt instructions | 67% | Superseded |
| **AI extracts experience facts; code applies the rule** | **63%** | **Shipped** |
| Stricter definition of "analytics work" | 50% | Rejected |

### What I learned

- **The biggest gain was a data fix, not a prompt fix.** No prompt can score requirements the model never receives.
- **LLMs read rules better than they apply them.** The model wrote "lacks required 3+ years" and still scored the job
  as a Maybe. Moving the rule into code (the model extracts the facts, code applies the threshold) made it
  deterministic and unit-testable, the same pattern the project already uses for location.
- **Conflicting instructions produce compromises.** When the score bands and an experience rule disagreed, five jobs
  landed on exactly 52: the bottom of the band, between the two instructions.
- **Stricter isn't always better.** A narrower definition raised precision@10 to 8/10 but found only 1 of 11 good
  jobs, so it was rejected.

**Limits:** 30 tune cases gives roughly ±17 points of margin, so differences of one or two cases are noise; all labels
come from one labeler; the holdout set has now been used, so the next round needs freshly labeled postings; the eval covers the fit scorer, not resume tailoring. The test postings and my labels are
kept out of the repo (`.gitignore`); the scripts, the rubric and the summary results are included.

## Project structure

| File | Purpose |
|---|---|
| `discover_jobs.py` | Stage 1: fetchers for 6 ATS types, filters, parallel fetch, retries, `--verify` health check |
| `descriptions.py` | Shared job-description lookup (saved copy first, live fetch for Workday/Oracle) |
| `us_location.py` | `is_non_us()` helper used by discovery and scoring |
| `score_jobs.py` | Stage 2: LLM fit scoring, caching, skill-gap report |
| `resume_rules.py` | Resume schema plus tailoring and screening-answer guardrails |
| `tailor_resume.py` | Stage 3 from the command line (Claude API) |
| `render_resume.py` | Stage 4: JSON → LaTeX → PDF with one-page auto-fit |
| `tracker.py` | Application log helpers |
| `job_copilot_server.py` | MCP server exposing the whole workflow as 13 tools |
| `add_companies.py` | Merges new employers into `companies.json` |
| `companies.json` | Target employers and their public careers-site identifiers |
| `experience_bank.example.json` | A **fictional** profile so anyone can run the project |
| `eval_freeze.py` | Freezes real postings into a fixed test set for the scorer eval |
| `eval_scorer.py` | Grades the production scorer against hand labels (agreement, precision@10, consistency, cost) |
| `eval/` | Labeling rubric, tested prompt versions, run log and notes (test postings and labels stay private) |

## Setup

Tested on macOS with Python 3.12.

```bash
git clone https://github.com/miguel-jaimes/job-search-copilot.git
cd job-search-copilot

pip install -r requirements.txt
conda install -c conda-forge tectonic        # LaTeX engine for the PDFs

cp .env.example .env                          # then add your Anthropic API key
cp experience_bank.example.json experience_bank.json   # then replace with your own facts
```

`.env` and `experience_bank.json` are in `.gitignore`, so your key and personal data stay on your machine.

## Usage

```bash
python discover_jobs.py --verify      # check every employer's endpoint (1 page each)
python discover_jobs.py --score       # full refresh + AI scores
python score_jobs.py --gaps           # most common missing skills in near-fit jobs
python tailor_resume.py job.txt --company "Acme"   # tailor to a pasted posting
python render_resume.py resumes/X.json             # re-render an edited resume

python eval_scorer.py                 # regression test for the scorer (~$0.40 per run)
python eval_scorer.py --rules eval/prompt_v5.txt --note "what changed"   # test a prompt change
```

To use it from chat, add the server to the Claude desktop app's config (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "job-copilot": {
      "command": "/path/to/python",
      "args": ["/path/to/job-search-copilot/job_copilot_server.py"]
    }
  }
}
```

## What I learned

- **Status codes tell you whose problem it is.** A 4xx means my request is wrong, so fail fast. A 5xx means their server hiccupped, so retry with backoff. Treating a 502 as "bad URL" was an early bug.
- **Normalize early.** Six different response formats become one record at the edge, which kept the filters, scoring and MCP tools simple.
- **Verify assumptions against real responses.** Workday only reports its result total on the first page, and Oracle returns non-JSON without a specific header. Neither showed up in documentation; both showed up in testing.
- **Scaling exposes design limits.** A sequential loop that was fine for 40 employers took 20+ minutes at 85. Parallelism plus stopping early fixed it.

## Limitations and next steps

- Employers on iCIMS, Eightfold, Taleo, SuccessFactors and Avature aren't supported yet; their email job alerts cover the gap.
- `companies.json` reflects one person's search (entry-level analyst roles, DFW-focused). Edit it for your own targets.
- The scorer reads candidate facts from `experience_bank.json`, but its prompt in `score_jobs.py` describes the candidate as a new-grad analytics student. Adjust that wording for other profiles.
- The scorer reads only the first 6,000 characters of a posting, so requirements at the end of very long postings can be missed.
- The model sometimes reads general experience ("2 years with Tableau") as professional experience and caps the score too early. It also leans conservative now: most remaining misses are jobs scored lower than my label.
- No pytest suite yet. Next: `pytest` with `httpx.MockTransport` for the fetchers, filters, LaTeX escaping, auto-fit and the experience cap.

## License

MIT. See [LICENSE](LICENSE).

---

Built by **Miguel Jaimes**, B.S. Business Analytics, UT Dallas (Dec 2026) · [LinkedIn](https://www.linkedin.com/in/migueljaimesprofile/)
