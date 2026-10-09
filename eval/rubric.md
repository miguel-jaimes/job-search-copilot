# Labeling Rubric (Fit-Scorer Eval)

The answer key for the eval. Each test case gets one label: **Strong**, **Maybe** or **Skip**.
The question behind every label: *would I apply to this job?*

**Judge fit only. Ignore location** (the AI is told to ignore it; location is handled in code).

---

## Strong: I'd apply this week
All of these are true:
- **The core duties are analytics work:** BI, data analysis, reporting, dashboards, process/workflow
  automation or optimization. Analytics has to be the job itself, not a side task.
- **Experience asked is 0–2 years.** "2+ years of experience" still counts as Strong when it means
  experience in general (skills that internships and projects can show, not specifically full-time work)
  **and** the stack is a close match to mine.
- **No critical tool is missing** (see "Required tools").

A new-grad or early-career program is a plus.

## Maybe: I'd apply if I have time
Still an analytics-type job, but with a real gap. Any of these:
- **2–3 years of work experience** required (or a range that starts at 2, e.g. "2–4 years").
- **1–3 years** asked and the analytics fit is vague.
- **A critical tool I don't have** (see "Required tools").
- **Finance- or forecasting-heavy, but uses an analytics stack** (Power BI, Tableau, Excel, Python, SQL).
- **Partly analytics:** some data or reporting duties, but analytics isn't the core of the job.

## Skip: I wouldn't apply
Any of these:
- **3+ years required** (or a range that starts at 3 or more, e.g. "3–5", "4+", "5+").
- **Vague analytics fit and more than 3 years asked.**
- **Senior, lead or manager** role.
- **Not really an analytics job:** finance, credit, risk or operations work without an analytics stack
  or analytics duties.

---

## Rules for the tricky parts

**Required tools (e.g. SAP, ERP).** If the tool is in the job title or is clearly a critical part of the
role, it drops the label one level (Strong → Maybe). If it's only *preferred*, or one of many tools in
the stack, it doesn't change the label.

**Experience ranges** count by their **minimum** ("2–4 years" is treated as 2+).

**General vs. work experience.** "X years of experience" with a skill can be met by internships and
projects; "X years of professional / full-time / work experience" can't.

**Substitution clauses.** If the posting says education, projects or equivalent experience can
substitute for the years asked, judge the job as if the experience requirement is met.

**Finance roles** are Maybe when they use an analytics stack, and Skip when they're purely financial.

**Keep every rule general.** A rule must make sense for a job I've never seen. No company names.
