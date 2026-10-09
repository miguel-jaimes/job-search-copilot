# Job-Search Copilot — shared rules
#
# The ONE place the tailoring rules, the resume data shape, and the
# screening-answer rules live. Both tailor_resume.py (the API script) and
# job_copilot_server.py (the MCP server) read from here, so the honesty
# guardrails are identical no matter which way you tailor.

# The exact shape a tailored resume must have. render_resume.py reads these keys.
SCHEMA = """{
  "company": "Company name from the job posting, e.g. Toyota",
  "role_short": "2-3 word role name for the file name, e.g. Analytics Analyst",
  "summary": "One tailored sentence.",
  "education": [
    {"school": "...", "date": "Expected <Month YYYY, from the bank's graduation field>", "degree": "...",
     "gpa": "...", "coursework": "comma-separated, only courses relevant to this job (or empty)"}
  ],
  "certifications": ["only if relevant, else empty list"],
  "projects": [
    {"title": "...", "date": "Month YYYY", "built_with": "comma-separated tools",
     "bullets": ["...", "..."]}
  ],
  "experience": [
    {"org": "...", "title": "...", "date": "Month YYYY – Month YYYY",
     "bullets": ["...", "..."]}
  ],
  "skills": [
    {"category": "e.g. Programming & Databases", "items": "comma-separated"}
  ],
  "why": "2-3 sentences: what you emphasized for this job and what you left out."
}"""

# How to select and write the resume content.
TAILORING_RULES = """YOUR TASK
- SELECT the experiences and individual bullets that best match this job. Leave out the rest.
- Items with "type": "work" go in "experience". Items with "type": "project" go in "projects".
- REORDER and lightly REPHRASE bullets to mirror the job's language and priorities,
  using its keywords only where they honestly apply.
- Write ONE tailored summary sentence, based on the bank's summary_options.
- Pick only the skills relevant to this job, grouped into 3-4 categories.
- ONE PAGE (strict): 1-2 projects and 1-3 experiences, 9-11 bullets total.
  Keep each bullet to about 30 words (two printed lines at most).
- List items and bullets MOST RELEVANT FIRST. If the page overflows, the
  renderer trims from the end, so the least important content must be last.
- In each bullet, wrap 1-3 key phrases (tools, metrics, key actions) in **double asterisks**
  to bold them, matching the candidate's existing resume style.
- Format dates like "June 2026 – August 2026" or "November 2025". Take the graduation
  date from the bank's education "graduation" field (e.g. "2026-12" -> "Expected December 2026").

ANALYTICS VS. MEDIA WORK
- The bank has two kinds of experience. ANALYTICS (BI, data, reporting, automation)
  is the default. MEDIA is any experience whose tags include "media" (e.g. Stacked
  Cast video production) plus the skills group "creative_tools".
- By DEFAULT, leave media experience and creative_tools OUT.
- Include the media experience ONLY when the job's duties involve marketing, social
  media, digital content, video, communications, brand, or the media/entertainment
  industry. For analytics-flavored roles of that kind (marketing or social media
  analytics), lead with its metric bullets (views, A/B tests, audience growth),
  not editing or filming.
- List creative_tools ONLY when the job names those tools or asks for video or
  design work, and put them in their own category (e.g. "Creative & Media"),
  never mixed into technical or data skills.
- In "why", say whether media work was included and the reason.

STRICT RULES
- Use ONLY information in the experience bank. Do NOT invent employers, projects,
  metrics, tools, dates, or achievements. If it is not in the bank, it cannot appear.
- Keep every metric exactly as written in the bank.
- Plain text only inside fields — no LaTeX, no markdown other than **bold**."""

# How to answer open-ended application questions ("Why us?", "Tell me about a time...").
ANSWER_RULES = """SCREENING-QUESTION RULES
- Answer ONLY from the experience bank and the job description. Never invent
  experiences, numbers, tools, or motivations the candidate hasn't stated.
- Pick the ONE most relevant real example rather than listing everything.
- Prefer analytics examples. Use the media (Stacked Cast) experience only when the
  question or job is about marketing, social media, content, creativity, or working
  with clients/stakeholders, or when no analytics example fits.
- Behavioral questions ("Tell me about a time..."): use a short STAR shape —
  situation, task, action, result — with the bank's real metrics.
- "Why this company/role?": connect specific things in the job description to
  specific real experience. If the candidate's personal motivation is needed and
  isn't in the bank, ASK the candidate instead of making one up.
- Respect any length limit the form states; default to 100-150 words.
- Plain, confident, first-person language. No clichés ("passionate", "synergy")."""
