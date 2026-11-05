"""Watches company career pages for NEW job openings, minutes after they are posted.

Companies publish openings in their hiring system (Greenhouse, Lever, Ashby,
SmartRecruiters, Workday) first - LinkedIn often copies them from there later.
These systems have public job feeds, so the bot can check them every few
minutes, keep only new openings in Bengaluru / India / Remote that a fresher can
apply for, check them against your resume, and post the matches.

The company list lives in companies.json (made by discover_companies.py).
"""
import asyncio
import html
import logging
import re

import httpx

log = logging.getLogger("careers")

INDIA = re.compile(r"bengaluru|bangalore|india|karnataka|hyderabad|pune|chennai|mumbai|gurgaon|gurugram|noida|"
                   r"delhi|kolkata|ahmedabad|kochi|coimbatore|mysore|mysuru|jaipur|indore|chandigarh", re.I)
REMOTE_FOR_US = re.compile(r"apac|asia|global|worldwide|anywhere", re.I)


class _Loc:
    @staticmethod
    def search(loc):
        """True for India locations, or remote jobs that name no other place (or say APAC/worldwide)."""
        loc = loc or ""
        if INDIA.search(loc):
            return True
        if re.search(r"remote", loc, re.I):
            if REMOTE_FOR_US.search(loc):
                return True
            rest = re.sub(r"remote|work from home|wfh|hybrid|[^a-z]", "", loc.lower())
            return not rest  # "Remote" alone is fine; "San Francisco, Remote" / "Remote - USA" are not
        return False


LOCATION_OK = _Loc()
# a quick first filter on the job title; the resume check happens afterwards
TITLE_OK = re.compile(r"data|machine learning|\bml\b|\bai\b|artificial intelligence|analyst|analytics|scientist|"
                      r"intern|graduate|new grad|fresher|entry|trainee|apprentice|associate|servicenow|\bqa\b|"
                      r"quality|test|sdet|software engineer|software developer|sde|python|nlp|llm|genai|gen ai|"
                      r"generative|vision|research|business intelligence|\bbi\b|power bi|automation|support engineer|"
                      r"cloud|devops|platform engineer|solutions engineer|consultant", re.I)
TITLE_SENIOR = re.compile(r"senior|\bsr\.?\b|staff|principal|lead\b|manager|director|head of|\bhead\b|architect|"
                          r"\bvp\b|vice president|\b(ii|iii|iv|2|3)\b|expert|specialist ii|chief|distinguished", re.I)
HEADERS = {"User-Agent": "Mozilla/5.0 (job-alert bot; checks public job feeds)"}


def clean(text, limit=3500):
    text = re.sub(r"<[^>]+>", " ", html.unescape(text or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]


# ---------- one function per hiring platform: -> list of jobs ----------
# job = {"id", "title", "location", "url", "text"(may be empty; fetched later if needed)}

async def greenhouse(http, slug):
    r = await http.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    r.raise_for_status()
    return [{"id": str(j["id"]), "title": j["title"], "location": (j.get("location") or {}).get("name", ""),
             "url": j.get("absolute_url"), "text": ""} for j in r.json().get("jobs", [])]


async def greenhouse_text(http, slug, job_id):
    r = await http.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}")
    return clean(r.json().get("content", "")) if r.status_code == 200 else ""


async def lever(http, slug):
    out, skip = [], 0
    while True:  # Lever pages at 100
        r = await http.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json", "skip": skip,
                                                                               "limit": 100})
        r.raise_for_status()
        page = r.json()
        for j in page:
            cat = j.get("categories") or {}
            loc = ", ".join(filter(None, [cat.get("location")] + (cat.get("allLocations") or [])))
            out.append({"id": j["id"], "title": j.get("text", ""), "location": loc, "url": j.get("hostedUrl"),
                        "text": clean(j.get("descriptionPlain") or j.get("description", ""))})
        if len(page) < 100:
            return out
        skip += 100


async def ashby(http, slug):
    r = await http.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        locs = [j.get("location") or ""] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        if j.get("isRemote"):
            locs.append("Remote")
        out.append({"id": j["id"], "title": j.get("title", ""), "location": ", ".join(filter(None, locs)),
                    "url": j.get("jobUrl"), "text": clean(j.get("descriptionPlain") or j.get("descriptionHtml"))})
    return out


async def smartrecruiters(http, slug):
    out, offset = [], 0
    while True:
        r = await http.get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
                           params={"limit": 100, "offset": offset})
        r.raise_for_status()
        data = r.json()
        for j in data.get("content", []):
            loc = j.get("location") or {}
            where = ", ".join(filter(None, [loc.get("city"), loc.get("region"), loc.get("country"),
                                            "Remote" if loc.get("remote") else ""]))
            out.append({"id": j["id"], "title": j.get("name", ""), "location": where,
                        "url": f"https://jobs.smartrecruiters.com/{slug}/{j['id']}", "text": ""})
        offset += 100
        if offset >= data.get("totalFound", 0) or offset >= 1000:
            return out


async def smartrecruiters_text(http, slug, job_id):
    r = await http.get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{job_id}")
    if r.status_code != 200:
        return ""
    sections = (r.json().get("jobAd") or {}).get("sections") or {}
    return clean(" ".join((v or {}).get("text", "") for v in sections.values()))


async def workday(http, slug):
    """slug = "host|tenant|site", e.g. "nvidia.wd5.myworkdayjobs.com|nvidia|NVIDIAExternalCareerSite"."""
    host, tenant, site = slug.split("|")
    url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    out = []
    for offset in (0, 20, 40):  # newest first; the first 60 are plenty for "new" jobs
        r = await http.post(url, json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": "India"})
        r.raise_for_status()
        posts = r.json().get("jobPostings", [])
        for j in posts:
            path = j.get("externalPath", "")
            out.append({"id": path or j.get("title", ""), "title": j.get("title", ""),
                        "location": j.get("locationsText", ""), "url": f"https://{host}/en-US/{site}{path}",
                        "text": ""})
        if len(posts) < 20:
            break
    return out


async def workday_text(http, slug, job_id):
    host, tenant, site = slug.split("|")
    r = await http.get(f"https://{host}/wday/cxs/{tenant}/{site}{job_id}")
    if r.status_code != 200:
        return ""
    return clean((r.json().get("jobPostingInfo") or {}).get("jobDescription", ""))


FETCH = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "smartrecruiters": smartrecruiters,
         "workday": workday}
TEXT = {"greenhouse": greenhouse_text, "smartrecruiters": smartrecruiters_text, "workday": workday_text}


def candidate(job):
    """Cheap filter before any AI: right place, fresher-level, title in your areas."""
    title = job.get("title") or ""
    return (LOCATION_OK.search(job.get("location") or "") and TITLE_OK.search(title)
            and not TITLE_SENIOR.search(title))


async def fetch_all(companies, concurrency=16):
    """-> list of (company, jobs or None on error) for every company in parallel."""
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(timeout=25, headers=HEADERS, follow_redirects=True) as http:
        async def one(c):
            async with sem:
                try:
                    return c, await FETCH[c["ats"]](http, c["slug"])
                except Exception as e:
                    log.debug("%s (%s) failed: %s", c["name"], c["ats"], e)
                    return c, None
        return await asyncio.gather(*(one(c) for c in companies))


async def job_text(company, job):
    if job.get("text") or company["ats"] not in TEXT:
        return job.get("text", "")
    async with httpx.AsyncClient(timeout=25, headers=HEADERS) as http:
        try:
            return await TEXT[company["ats"]](http, company["slug"], job["id"])
        except Exception:
            return ""
