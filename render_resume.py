# Job-Search Copilot — Resume Renderer
#
# Turns a structured resume (JSON) into YOUR exact LaTeX template (copied from
# your Overleaf main.tex) and compiles it to a PDF with Tectonic.
#
# Usually you don't run this directly — tailor_resume.py calls it for you.
# But you can re-render a saved JSON by hand (e.g. after editing a bullet):
#
#   python render_resume.py resumes/Miguel_Jaimes_Toyota.json
#
# OUTPUT (in the resumes/ folder):
#   Miguel_Jaimes_<Company>.tex   — the LaTeX source (you can paste into Overleaf)
#   Miguel_Jaimes_<Company>.pdf   — the finished resume
# ---------------------------------------------------------------------------

import json
import os
import re
import shutil
import subprocess
import sys

OUT_DIR = "resumes"

# ---------------------------------------------------------------------------
# YOUR TEMPLATE'S PREAMBLE — copied from your Overleaf main.tex, unchanged.
# (Packages, margins, section style, bullet style, \Role macro, tightening.)
# ---------------------------------------------------------------------------
PREAMBLE = r"""\documentclass[a4paper,10pt]{article}
\usepackage[empty]{fullpage}
\usepackage{enumitem}
\usepackage[hidelinks]{hyperref}
\usepackage{titlesec}
\usepackage{fancyhdr}
\usepackage{tabularx}
\usepackage{fontawesome5}

%-------------------- PAGE SETUP --------------------
\pagestyle{fancy}
\fancyhf{}
\renewcommand{\headrulewidth}{0pt}
\renewcommand{\footrulewidth}{0pt}
\addtolength{\oddsidemargin}{-0.4in}
\addtolength{\evensidemargin}{-0.4in}
\addtolength{\textwidth}{0.8in}
\addtolength{\topmargin}{-0.6in}
\addtolength{\textheight}{1.2in}

% Section headings
\titleformat{\section}{\vspace{-4pt}\scshape\raggedright\large}{}{0em}{}[\titlerule\vspace{-5pt}]

% Bullet style
\renewcommand{\labelitemi}{$\vcenter{\hbox{\tiny$\bullet$}}$}

% Role macros for consistent alignment & minimal spacing
\newcommand{\Role}[2]{%
  \noindent\textbf{#1}\hfill\textit{#2}\\[-7pt]
  \begin{itemize}
}
\newcommand{\EndRole}{%
  \end{itemize}\vspace{0pt}
}

% --------- Global micro-tightening to keep to 1 page ----------
\addtolength{\textheight}{0.15in}
\setlength{\parskip}{0pt}
\setlist[itemize]{leftmargin=10pt,itemsep=0.5pt,parsep=0pt,topsep=0pt,partopsep=0pt}
"""

# ---------------------------------------------------------------------------
# TEXT SAFETY — LaTeX treats some characters as commands. Escape them.
# ---------------------------------------------------------------------------
_LATEX_SPECIAL = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def esc(text):
    """Escape one piece of plain text so LaTeX prints it literally."""
    return "".join(_LATEX_SPECIAL.get(ch, ch) for ch in str(text or ""))


def rich(text):
    """Escape text, then turn **phrase** into bold — matching your resume's
    habit of bolding key phrases inside bullets."""
    safe = esc(text)
    return re.sub(r"\*\*(.+?)\*\*", r"\\textbf{\1}", safe)


# ---------------------------------------------------------------------------
# SECTION BUILDERS — each returns a chunk of LaTeX in your template's style
# ---------------------------------------------------------------------------

def build_header(p):
    parts = []
    if p.get("phone"):
        parts.append(r"\faPhone\ " + esc(p["phone"]))
    if p.get("email"):
        parts.append(r"\href{mailto:%s}{%s}" % (p["email"], esc(p["email"])))
    if p.get("linkedin"):
        parts.append(r"\faLinkedin\ \href{%s}{%s}" % (p["linkedin"], esc(p["linkedin"])))
    contact = " --- ".join(parts)
    return (
        "\\begin{center}\n"
        f"    {{\\LARGE \\textbf{{{esc(p.get('name', ''))}}}}} \\\\[4pt]\n"
        f"    {contact}\n"
        "\\end{center}\n"
        "\\vspace{-10pt}\n"
    )


def build_summary(summary):
    if not summary:
        return ""
    return "\\section*{Summary}\n" + rich(summary) + "\n\\vspace{-1pt}\n"


def build_education(edu_list, certs):
    out = ["\\section*{Education}"]
    for e in edu_list:
        out.append(f"\\textbf{{{esc(e.get('school'))}}} \\hfill \\textit{{{esc(e.get('date'))}}} \\\\")
        out.append(f"{esc(e.get('degree'))} \\\\")
        if e.get("gpa"):
            out.append(f"\\textit{{{esc(e['gpa'])} GPA}} \\\\")
        if e.get("coursework"):
            out.append(f"\\textit{{Relevant Coursework:}} {esc(e['coursework'])} \\\\")
    if certs:
        out.append("\\textit{Certifications:} " + esc(", ".join(certs)) + " \\\\")
    # remove the trailing line break on the last line
    out[-1] = out[-1].rstrip(" \\")
    out.append("\\vspace{-1pt}")
    return "\n".join(out) + "\n"


def build_projects(projects):
    if not projects:
        return ""
    out = ["\\section*{Projects}"]
    for i, pr in enumerate(projects):
        out.append(f"\\noindent\\textbf{{{esc(pr.get('title'))}}} \\hfill \\textit{{{esc(pr.get('date'))}}} \\\\")
        if pr.get("built_with"):
            out.append(f"\\noindent\\textit{{Built with}} \\textbf{{\\textit{{{esc(pr['built_with'])}}}}}\\\\[-3pt]")
        out.append("\\begin{itemize}[leftmargin=10pt,itemsep=0.5pt,topsep=0pt]")
        for b in pr.get("bullets", []):
            out.append(f"    \\item {rich(b)}")
        out.append("\\end{itemize}")
        out.append("\\vspace{6pt}" if i < len(projects) - 1 else "\\vspace{2pt}")
    return "\n".join(out) + "\n"


def build_experience(roles):
    if not roles:
        return ""
    out = ["\\section*{Experience}"]
    for i, r in enumerate(roles):
        heading = f"{esc(r.get('org'))} - {esc(r.get('title'))}"
        out.append(f"\\Role{{{heading}}}{{{esc(r.get('date'))}}}")
        out.append("\\vspace{2pt}")
        for b in r.get("bullets", []):
            out.append(f"    \\item {rich(b)}")
        out.append("\\EndRole")
        if i < len(roles) - 1:
            out.append("\\vspace{6pt}")
    return "\n".join(out) + "\n\\vspace{6pt}\n"


def build_skills(skills):
    if not skills:
        return ""
    out = ["\\section*{Technical Skills}"]
    for s in skills:
        out.append(f"\\textbf{{{esc(s.get('category'))}:}} {esc(s.get('items'))} \\\\")
    out[-1] = out[-1].rstrip(" \\")
    # small gap before the next section (skills is no longer the last section)
    return "\n".join(out) + "\n\\vspace{-1pt}\n"


# ---------------------------------------------------------------------------
# SECTION ORDER — the one place that decides where each section goes.
# The header (name + contact line) always comes first. To move a section,
# just reorder this list; no other code needs to change.
# ---------------------------------------------------------------------------
SECTION_ORDER = ["summary", "skills", "projects", "experience", "education"]

# Name in the list -> the function that builds that section from the resume JSON.
SECTION_BUILDERS = {
    "summary":    lambda r: build_summary(r.get("summary")),
    "skills":     lambda r: build_skills(r.get("skills", [])),
    "projects":   lambda r: build_projects(r.get("projects", [])),
    "experience": lambda r: build_experience(r.get("experience", [])),
    "education":  lambda r: build_education(r.get("education", []), r.get("certifications", [])),
}


def build_tex(resume):
    """Assemble the full .tex document: the header first, then each section
    in SECTION_ORDER (Summary, Technical Skills, Projects, Experience, Education)."""
    body = build_header(resume.get("profile", {}))
    for name in SECTION_ORDER:
        body += "\n" + SECTION_BUILDERS[name](resume)
    return PREAMBLE + "\n\\begin{document}\n\n" + body + "\n\\end{document}\n"


# ---------------------------------------------------------------------------
# COMPILE + CHECK
# ---------------------------------------------------------------------------

def safe_name(text):
    return re.sub(r"[^A-Za-z0-9]+", "_", text or "Resume").strip("_")


MAX_SKILL_LINES = 4      # skills categories kept when trimming to fit one page
MIN_BULLETS_PER_ITEM = 2 # never trim a job/project below this many bullets


def count_pages(pdf_path, log_path=None):
    """Page count. First try a PDF library; if none is installed, read the
    '(N pages)' line the LaTeX engine writes into its log file."""
    for mod in ("pypdf", "PyPDF2"):
        try:
            lib = __import__(mod)
            return len(lib.PdfReader(pdf_path).pages)
        except Exception:
            continue
    if log_path and os.path.exists(log_path):
        with open(log_path, encoding="utf-8", errors="ignore") as f:
            m = re.findall(r"\((\d+) pages?", f.read())
        if m:
            return int(m[-1])
    return None


def trim_one(resume):
    """Remove the single least-important piece of content. Claude lists items
    most-relevant-first, so we always cut from the END. Returns a description
    of what was cut, or None if nothing sensible is left to cut."""
    skills = resume.get("skills", [])
    if len(skills) > MAX_SKILL_LINES:
        cut = skills.pop()
        return f"skills line '{cut.get('category')}'"

    # the job/project with the most bullets gives up its last bullet
    items = resume.get("projects", []) + resume.get("experience", [])
    biggest = max(items, key=lambda it: len(it.get("bullets", [])), default=None)
    if biggest and len(biggest.get("bullets", [])) > MIN_BULLETS_PER_ITEM:
        biggest["bullets"].pop()
        name = biggest.get("title") if biggest in resume.get("projects", []) else biggest.get("org")
        return f"last bullet of '{name}'"

    # every item is already at the minimum: drop the least relevant project
    if len(resume.get("projects", [])) > 1:
        cut = resume["projects"].pop()
        return f"project '{cut.get('title')}'"
    return None


def compile_once(resume, basename):
    """Write the .tex and compile it. Returns (pdf_path, pages) or (None, None)."""
    tex_path = os.path.join(OUT_DIR, basename + ".tex")
    pdf_path = os.path.join(OUT_DIR, basename + ".pdf")
    log_path = os.path.join(OUT_DIR, basename + ".log")
    with open(tex_path, "w", encoding="utf-8") as f:
        f.write(build_tex(resume))
    result = subprocess.run(
        ["tectonic", "--keep-logs", "--outdir", OUT_DIR, tex_path],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print("Tectonic hit an error. Last lines of its log:\n")
        print("\n".join(result.stderr.strip().splitlines()[-15:]))
        return None, None
    return pdf_path, count_pages(pdf_path, log_path)


def render(resume, basename, max_trims=15):
    os.makedirs(OUT_DIR, exist_ok=True)
    tex_path = os.path.join(OUT_DIR, basename + ".tex")

    if not shutil.which("tectonic"):
        with open(tex_path, "w", encoding="utf-8") as f:
            f.write(build_tex(resume))
        print(f"Wrote {tex_path}")
        print("Tectonic not found — install with:  conda install -c conda-forge tectonic")
        print(f"(You can still paste {tex_path} into Overleaf to get the PDF.)")
        return None

    print("Compiling PDF with Tectonic... (first run downloads LaTeX packages, ~30s)")
    pdf_path, pages = compile_once(resume, basename)
    if not pdf_path:
        return None

    # AUTO-FIT: while it spills onto page 2, cut the least relevant content and recompile
    trimmed = []
    while pages and pages > 1 and len(trimmed) < max_trims:
        cut = trim_one(resume)
        if not cut:
            break
        trimmed.append(cut)
        pdf_path, pages = compile_once(resume, basename)
        if not pdf_path:
            return None

    if trimmed:
        print("Trimmed to fit one page: " + "; ".join(trimmed))
        # keep the .json in sync with what's actually in the PDF
        with open(os.path.join(OUT_DIR, basename + ".json"), "w", encoding="utf-8") as f:
            json.dump(resume, f, indent=2, ensure_ascii=False)

    print(f"Saved {pdf_path}" + (f"  ({pages} page{'s' if pages != 1 else ''})" if pages else ""))
    if pages is None:
        print("  (Couldn't count pages — for auto-fit, run:  pip install pypdf)")
    elif pages > 1:
        print("  WARNING: still 2 pages after trimming. Delete a bullet in the .json and re-render.")
    return pdf_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("Usage:  python render_resume.py <resume.json>")
    path = sys.argv[1]
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    base = os.path.splitext(os.path.basename(path))[0]
    pdf = render(data, base)
    if pdf and sys.platform == "darwin":
        subprocess.run(["open", pdf])
