"""Reads your resume(s) from the resumes/ folder (including subfolders).

Drop PDF, DOCX, TXT or MD files anywhere under resumes/ (e.g. one folder per
track or company). A PDF and DOCX copy of the same resume count once, and files
with "cheatsheet" in the name are ignored. Edit or replace files any time: the
bot notices the change on the next message, no restart needed.

All resumes together are condensed into one skills profile (skills, tools,
projects, experience, certifications) that every job is matched against.
"""
import hashlib
import json
import logging
import os
import re
import time

import httpx

log = logging.getLogger("resume")
RESUME_DIR = os.getenv("RESUME_DIR", "resumes")
CACHE = "data/skills_profile.json"
EXTS = (".pdf", ".docx", ".txt", ".md")
SKIP_NAMES = re.compile(r"cheat ?sheet|~\$", re.I)

# Skills/roles we look for when the AI is unavailable (keyword fallback)
SKILL_TERMS = [
    "data science", "data scientist", "data analyst", "data analytics", "data engineer", "machine learning",
    "deep learning", "artificial intelligence", "generative ai", "genai", "llm", "nlp", "computer vision",
    "python", "sql", "power bi", "tableau", "pandas", "tensorflow", "pytorch", "scikit", "statistics",
    "servicenow", "itsm", "itom", "cmdb", "flow designer", "devops", "cloud", "aws", "azure", "gcp", "docker",
    "kubernetes", "java", "javascript", "react", "node", "full stack", "frontend", "backend", "web development",
    "android", "flutter", "cyber security", "networking", "linux", "excel", "business analyst", "analytics",
    "selenium", "testing", "quality assurance", "c++", "dsa", "rest api", "flask", "fastapi", "django",
]

SUMMARY_PROMPT = """Below are several resumes and profiles of ONE student (different versions for different jobs).
Merge them into a single compact skills profile used to match job postings. Include everything the student
has evidence for, without repeats:
- Education (degree, branch, college, graduation year, CGPA)
- Technical skills, languages, tools, frameworks, platforms
- Projects (one line each: what it does + tech used)
- Internships / experience (one line each)
- Certifications, achievements, coding profiles
- Job areas the student is capable of (based on the evidence above, not just titles)
Plain text, max 2500 characters.

<<<
__TEXT__
>>>"""


def _read(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        from pypdf import PdfReader
        return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    if ext == ".docx":
        import docx
        doc = docx.Document(path)
        cells = [c.text for t in doc.tables for row in t.rows for c in row.cells]
        return "\n".join([p.text for p in doc.paragraphs] + cells)
    return open(path, encoding="utf-8", errors="ignore").read()


def _norm(text):
    return re.sub(r"\W+", "", text.lower())


class Resumes:
    def __init__(self, folder=RESUME_DIR, api_key=None, model="gemini-2.5-flash"):
        self.folder = folder
        self.api_key = api_key
        self.model = model.split(",")[0].strip()  # first model of the GEMINI_MODEL chain
        self._sig = None
        self.raw = ""  # all unique resume texts
        self.text = ""  # condensed skills profile (what the matcher reads)
        self.skills = set()
        os.makedirs(folder, exist_ok=True)

    def _files(self):
        out = []
        for root, _, names in os.walk(self.folder):
            for n in names:
                if n.lower().endswith(EXTS) and not n.startswith(".") and not SKIP_NAMES.search(n):
                    out.append(os.path.join(root, n))
        return sorted(out)

    def _signature(self):
        return tuple((f, os.path.getmtime(f)) for f in self._files())

    def refresh(self):
        """Re-reads the resumes only if a file was added, changed or removed."""
        sig = self._signature()
        if sig == self._sig:
            return
        self._sig = sig
        if not sig and os.getenv("SKILLS_PROFILE"):  # cloud: no resume files, profile comes from a secret
            self.text = os.environ["SKILLS_PROFILE"]
            low = self.text.lower()
            self.skills = {s for s in SKILL_TERMS if s in low}
            log.info("Using SKILLS_PROFILE (%s chars, %s keyword skills)", len(self.text), len(self.skills))
            return
        texts, seen = [], []
        for path, _ in sig:
            try:
                text = re.sub(r"[ \t]+", " ", _read(path)).strip()
            except Exception as e:
                log.warning("Could not read %s: %s", path, e)
                continue
            key = _norm(text)
            # a PDF and DOCX of the same resume are near-identical: keep one
            if not key or any(_similar(key, k) for k in seen):
                continue
            seen.append(key)
            texts.append(f"--- {os.path.relpath(path, self.folder)} ---\n{text}")
        self.raw = "\n\n".join(texts)
        low = self.raw.lower()
        self.skills = {s for s in SKILL_TERMS if s in low}
        self.text = self._condense() if texts else ""
        if texts:
            log.info("Loaded %s unique resume(s) from %s files; profile %s chars; %s keyword skills",
                     len(texts), len(sig), len(self.text), len(self.skills))
        else:
            log.warning("No resume found in %s/ - using PROFILE from .env only", self.folder)

    def _condense(self):
        """One merged skills profile, cached so the AI is asked only when resumes change."""
        digest = hashlib.sha1(self.raw.encode()).hexdigest()
        try:
            cached = json.load(open(CACHE, encoding="utf-8"))
            if cached.get("digest") == digest:
                return cached["profile"]
        except Exception:
            pass
        profile = None
        if self.api_key:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
            body = {"contents": [{"parts": [{"text": SUMMARY_PROMPT.replace("__TEXT__", self.raw[:60000])}]}],
                    "generationConfig": {"temperature": 0}}
            for wait in (0, 10, 30, 60):  # Gemini is sometimes briefly busy (503)
                time.sleep(wait)
                try:
                    r = httpx.post(url, json=body, headers={"x-goog-api-key": self.api_key}, timeout=120)
                    r.raise_for_status()
                    profile = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                    break
                except Exception as e:
                    log.warning("Could not condense resumes with AI (%s)", e)
            if not profile:
                log.warning("Using raw resume text for now; will retry when resumes change")
        if not profile:
            return self.raw[:12000]
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        json.dump({"digest": digest, "profile": profile}, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)
        return profile


def _similar(a, b):
    """True if two normalized texts are basically the same resume."""
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    if len(short) < 0.85 * len(long_):
        return False
    grams = {short[i:i + 12] for i in range(0, len(short) - 12, 12)}
    return sum(g in long_ for g in grams) >= 0.9 * len(grams)
