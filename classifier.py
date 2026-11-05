"""Decides whether a message is a job opening that fits YOUR resume.

Only relevant openings are posted; everything else (shortlists, events,
notices, chit-chat, jobs that don't fit you) is silently skipped.

Cheap keyword rules run first so the AI is only asked about real job posts.
Gemini (free tier) then reads the post next to your resume and decides.
Without a key or if the AI fails, a keyword match against your resume decides.
"""
import asyncio
import json
import os
import logging
import re
import time

import httpx

log = logging.getLogger("classifier")

PROMPT = """You help a student find job openings that fit them.

STUDENT
Basics: __PROFILE__
Resume(s):
<<<
__RESUME__
>>>

MESSAGE (from a placement / job group):
<<<
__TEXT__
>>>

Decide:
- is_job: true only if the message announces a job, internship or hiring drive the student could apply/register for
  (not shortlists, test/interview schedules, results, events, hackathons, courses, notices or chit-chat).
- relevant: true if is_job AND the student is capable of doing this job AND eligible.
  * Capable = the job's main required skills/work are covered by the skills, tools, projects, internships or
    certifications in the resume(s). Judge by what the job actually needs, NOT by its title: any role whose
    requirements the student can meet counts, even if the title is new or unusual. Generic fresher roles open to
    the student's branch (e.g. software/trainee/associate engineer) count if the resume shows the needed basics.
  * Eligible = branch and graduation year allowed, or not stated.
  * False if the job mainly needs skills the resume doesn't show (e.g. mechanical design, VLSI, sales quotas).
- reason: one short line naming the resume skills/projects that match the job, or what's missing.

Return only JSON (null when not stated):
{"is_job": bool, "relevant": bool, "reason": str, "company": str|null, "role": str|null, "location": str|null,
 "pay": str|null, "deadline": str|null, "apply_link": str|null}"""

# Messages that are clearly NOT new openings (skipped without asking the AI)
NOT_JOB = [r"shortlist", r"final select", r"\bselects\b", r"been selected", r"congratulat", r"online test",
           r"test link", r"interview (schedule|update|process)", r"\bgd\b", r"group discussion",
           r"pre[- ]?placement", r"\bppt\b", r"report to", r"reporting time", r"\bvenue\b", r"expert talk",
           r"webinar", r"workshop", r"hackathon", r"competition", r"challenge", r"\bcourse\b", r"offer (letter|accept)"]
JOB_SIGNALS = [r"hiring", r"\bapply\b", r"opening", r"vacanc", r"\bjobs?\b", r"\bintern(ship)?s?\b", r"\bctc\b",
               r"\blpa\b", r"stipend", r"salary", r"\bjd\b", r"eligib", r"\broles?\b", r"position", r"job title",
               r"recruit", r"campus drive", r"register", r"fresher", r"opportunity", r"\bwanted\b",
               r"\bdeveloper", r"\bengineer", r"\banalyst", r"\btrainee", r"walk[- ]?in"]
URL_RE = re.compile(r"https?://\S+")
APPLY_HINT = re.compile(r"forms\.gle|docs\.google\.com/forms|careers|/jobs?/|apply|workday|superset|unstop|"
                        r"hackerearth|naukri|linkedin\.com/jobs|lever\.co|greenhouse|register|bit\.ly|tinyurl", re.I)
FIELDS = ("company", "role", "location", "pay", "deadline", "apply_link", "reason")


def _hits(text, patterns):
    return sum(1 for p in patterns if re.search(p, text))


def best_link(text):
    """The most likely application link in a message (not the company homepage)."""
    urls = [u.rstrip(").,*") for u in URL_RE.findall(text)]
    return next((u for u in urls if APPLY_HINT.search(u)), urls[0] if urls else None)


def empty(relevant=False, reason=None):
    return {"relevant": relevant, "reason": reason, "company": None, "role": None, "location": None,
            "pay": None, "deadline": None, "apply_link": None}


def looks_like_job(text):
    t = text.lower()
    signals = _hits(t, JOB_SIGNALS)
    if _hits(t, NOT_JOB) and not re.search(r"register|apply|job title|\bctc\b|stipend|hiring", t):
        return False
    return signals >= 1


# Where you can work / interview (PREFERRED_CITIES in .env)
PREFERRED_CITIES = os.getenv("PREFERRED_CITIES_TEXT",
                             "Bengaluru or other South Indian cities (Mysuru, Mangaluru, Chennai, Coimbatore, "
                             "Hyderabad, Kochi, Thiruvananthapuram, Visakhapatnam, Vijayawada, Tirupati)")

# Roles you never want (EXCLUDE_ROLES in .env, comma-separated)
EXCLUDE_ROLES = [r.strip() for r in os.getenv("EXCLUDE_ROLES", "").split(",") if r.strip()]
# "Full Stack Developer" -> matches "full stack", "full-stack", "fullstack" in any wording
_cores = [re.sub(r"\s*(developer|engineer|development|intern)s?$", "", r, flags=re.I) for r in EXCLUDE_ROLES]
EXCLUDE_RE = re.compile("|".join(re.escape(c).replace(r"\ ", r"[\s-]?") for c in _cores if c), re.I) \
    if EXCLUDE_ROLES else None
# skills that only point to the excluded roles, so they don't count as a match on their own
EXCLUDED_SKILLS = {"full stack", "backend", "frontend", "web development", "node", "react", "javascript"} \
    if EXCLUDE_ROLES else set()

# Words too generic to prove a match on their own ("Excel", "statistics" appear in sales/ops posts too)
WEAK_SKILLS = {"excel", "statistics", "analytics", "testing", "cloud", "networking", "linux", "backend",
               "frontend", "business analyst"}
NON_TECH_ROLE = re.compile(r"\b(sales|marketing|business development|\bbda\b|\bbde\b|key account|store manager|"
                           r"recruit(er|ment) intern|talent acquisition|telecall|field executive)\b", re.I)


def keyword_match(text, skills):
    """Fallback when the AI is unavailable: needs a specific resume skill, and not a sales/ops role."""
    t = text.lower()
    found = sorted(s for s in skills if re.search(rf"(?<![a-z]){re.escape(s)}(?![a-z])", t))
    strong = [s for s in found if s not in WEAK_SKILLS | EXCLUDED_SKILLS]
    if EXCLUDE_RE and EXCLUDE_RE.search(t[:250]):
        return empty(False)  # the job title (top of the post) is a role you excluded
    if strong and not NON_TECH_ROLE.search(t):
        return empty(True, "Matches your resume: " + ", ".join(list(dict.fromkeys(strong + found))[:5]))
    return empty(False)


def normalize(data):
    info = empty(bool(data.get("relevant")) and data.get("is_job", True) is not False)
    for k in FIELDS:
        v = data.get(k)
        if isinstance(v, str) and v.strip() and v.strip().lower() not in ("null", "none", "n/a"):
            info[k] = v.strip()[:160]
    if info["apply_link"] and not info["apply_link"].startswith("http"):
        info["apply_link"] = None
    return info


class Classifier:
    def __init__(self, resumes, profile="", api_key=None, model="gemini-2.5-flash", min_interval=6.0):
        self.resumes = resumes
        self.profile = profile
        self.api_key = api_key
        self.models = [m.strip() for m in model.split(",") if m.strip()]  # tried in order
        self._rested = {}  # model -> time until which it is skipped
        self.min_interval = min_interval  # seconds between AI calls (free-tier friendly)
        self.http = httpx.AsyncClient(timeout=60)
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def check(self, text):
        """-> info dict with info["relevant"] True/False."""
        clean = text.strip()
        if len(clean) < 25 or not looks_like_job(clean):
            return empty(False)
        self.resumes.refresh()
        info = None
        if self.api_key:
            try:
                info = await self._ai(clean)
            except Exception as e:  # never lose a job because the AI failed
                log.warning("AI check failed, using keyword match: %s", e)
        if info is None:
            info = keyword_match(clean, self.resumes.skills)
        if info["relevant"] and not info.get("apply_link"):
            info["apply_link"] = best_link(clean)
        return info

    async def check_opening(self, text):
        """For text that is known to be a single job opening (career pages): resume check only."""
        self.resumes.refresh()
        if self.api_key:
            try:
                return await self._ai(text)
            except Exception as e:
                log.warning("AI check failed, using keyword match: %s", e)
        return keyword_match(text, self.resumes.skills)

    def fill(self, template, text, limit=6000):
        basics = self.profile or "(not given)"
        if PREFERRED_CITIES:
            basics += ("\nLOCATION RULE (strict): the student can only take jobs located in " + PREFERRED_CITIES
                       + ", or fully remote/work-from-home jobs open to India. Mark relevant=false if the job is only in "
                       "other cities (e.g. Pune, Mumbai, Delhi NCR, Gurgaon, Noida, Kolkata, Ahmedabad, or abroad). "
                       "INTERVIEW RULE: the student can attend in-person interviews, tests or walk-ins ONLY in Bengaluru. "
                       "Mark relevant=false if any round must be attended in person in a city other than Bengaluru "
                       "(even Chennai or Hyderabad). Virtual/online rounds, on-campus drives and in-person rounds in "
                       "Bengaluru are fine. If location or interview mode is not stated, keep it relevant and say "
                       "'interview mode not stated' in the reason.")
        if EXCLUDE_ROLES:
            basics += ("\nThe student does NOT want these roles - mark relevant=false when the job is mainly one of "
                       "them (a post listing several roles is still relevant if another listed role fits): "
                       + ", ".join(EXCLUDE_ROLES))
        return (template.replace("__PROFILE__", basics)
                .replace("__RESUME__", self.resumes.text[:12000] or "(no resume uploaded yet)")
                .replace("__TEXT__", text[:limit]))

    async def _ai(self, text):
        data = await self.ask_json(self.fill(PROMPT, text))
        return normalize(data[0] if isinstance(data, list) else data)

    async def ask_json(self, prompt):
        """Asks the AI for JSON. Tries each model in turn; one that used up its free daily quota rests 6 hours."""
        body = {"contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0}}
        async with self._lock:
            for rnd in range(2):  # second round only after a short wait (per-minute limits)
                if rnd:
                    await asyncio.sleep(30)
                for model in self.models:
                    if self._rested.get(model, 0) > time.time():
                        continue
                    wait = self.min_interval - (time.monotonic() - self._last)
                    if wait > 0:
                        await asyncio.sleep(wait)
                    self._last = time.monotonic()
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
                    try:
                        r = await self.http.post(url, json=body, headers={"x-goog-api-key": self.api_key})
                    except httpx.HTTPError as e:
                        log.info("AI %s unreachable (%s), trying next", model, e)
                        continue
                    if r.status_code == 200:
                        return json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
                    if r.status_code == 429 and "PerDay" in r.text:
                        self._rested[model] = time.time() + 6 * 3600
                        log.info("AI %s used its free daily quota; switching model", model)
                    elif r.status_code == 404:
                        self._rested[model] = time.time() + 24 * 3600
                    else:
                        log.info("AI %s busy (%s), trying next", model, r.status_code)
        raise RuntimeError("all AI models busy or out of quota")
