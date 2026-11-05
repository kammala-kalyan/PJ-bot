"""Reads job-alert emails (LinkedIn, Naukri, Indeed, Internshala, ...) from Gmail.

Each alert email can list many jobs. The AI splits it into single jobs, checks
each one against your resume, and only the ones that fit are posted - once.
The same job seen again (another email, another site) is not posted twice.

Needs GMAIL_ADDRESS and GMAIL_APP_PASSWORD (Google Account -> Security ->
2-Step Verification -> App passwords). Read-only: emails are never changed.
"""
import email
import imaplib
import logging
import os
import re
from email.header import decode_header, make_header
from html.parser import HTMLParser
from urllib.parse import quote

log = logging.getLogger("mail")

GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "")
# Optional second inbox (e.g. the one LinkedIn sends to)
GMAIL_ADDRESS2 = os.getenv("GMAIL_ADDRESS2", "")
GMAIL_APP_PASSWORD2 = os.getenv("GMAIL_APP_PASSWORD2", "").replace(" ", "")
# (address, password, pseudo chat id under which its emails are remembered as processed)
# Third inbox (e.g. your main Gmail) - read for job alerts and application follow-ups too
GMAIL_ADDRESS3 = os.getenv("GMAIL_ADDRESS3", "")
GMAIL_APP_PASSWORD3 = os.getenv("GMAIL_APP_PASSWORD3", "").replace(" ", "")
ACCOUNTS = [(a, pw, -1 - i) for i, (a, pw) in enumerate([(GMAIL_ADDRESS, GMAIL_APP_PASSWORD),
                                                         (GMAIL_ADDRESS2, GMAIL_APP_PASSWORD2),
                                                         (GMAIL_ADDRESS3, GMAIL_APP_PASSWORD3)]) if a and pw]
MAIL_DAYS = int(os.getenv("MAIL_DAYS", "2"))  # how far back to look for unread-by-bot alerts

SENDERS = ["linkedin.com", "naukri.com", "indeed.com", "internshala.com", "foundit.in", "glassdoor",
           "instahyre.com", "cutshort.io", "wellfound.com", "unstop.com", "shine.com", "apna.co",
           "hirist", "iimjobs.com", "timesjobs.com", "simplyhired"]
JOB_SUBJECT = re.compile(r"job|hiring|opening|vacanc|role|intern|position|opportunit|recommended|apply|"
                         r"career|match|engineer|developer|analyst|scientist", re.I)

EMAIL_PROMPT = """You help a student find jobs that fit them.

STUDENT
Basics: __PROFILE__
Skills profile:
<<<
__RESUME__
>>>

Below is a job-alert EMAIL from a job site. Links are marked like [L3].
List EVERY job/internship posting in it (ignore ads, courses, "people viewed your profile", news).
For each job decide "relevant": true only if the student is capable of the job's main requirements (by skills,
projects, tools - not by title) and is eligible as a 2027 fresher/intern (skip roles needing 2+ years experience).

Return only JSON: {"jobs": [{"title": str, "company": str|null, "location": str|null, "pay": str|null,
"link": "L<number>"|null, "relevant": bool, "reason": "short line naming matching resume skills"}]}

EMAIL from __SENDER__, subject: __SUBJECT__
<<<
__TEXT__
>>>"""


class _Text(HTMLParser):
    """HTML -> plain text, with each link replaced by a marker [Ln]."""

    def __init__(self):
        super().__init__()
        self.parts, self.links, self._href, self._skip = [], [], None, 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head"):
            self._skip += 1
        if tag == "a":
            self._href = dict(attrs).get("href")
        if tag in ("br", "p", "div", "tr", "li", "table", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head"):
            self._skip = max(0, self._skip - 1)
        if tag == "a" and self._href:
            if self._href.startswith("http"):
                self.links.append(self._href)
                self.parts.append(f" [L{len(self.links)}] ")
            self._href = None

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def email_text(msg):
    """-> (plain text with [Ln] link markers, list of links)."""
    html_part = plain = None
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype in ("text/html", "text/plain") and not part.get_filename():
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
            if ctype == "text/html" and html_part is None:
                html_part = text
            elif ctype == "text/plain" and plain is None:
                plain = text
    if html_part:
        p = _Text()
        p.feed(html_part)
        text, links = "".join(p.parts), p.links
    else:
        links = []

        def mark(m):
            links.append(m.group(0))
            return f"[L{len(links)}]"
        text = re.sub(r"https?://\S+", mark, plain or "")
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text, links


def meta(msg, address):
    """Where an email came from: inbox, time received, and a link that opens it in Gmail."""
    from email.utils import parsedate_to_datetime
    from datetime import timedelta, timezone
    msgid = (msg.get("Message-ID") or "").strip().strip("<>")
    try:
        when = parsedate_to_datetime(msg.get("Date")).astimezone(timezone(timedelta(hours=5, minutes=30)))
        when = when.strftime("%d %b, %I:%M %p")
    except Exception:
        when = ""
    link = (f"https://mail.google.com/mail/u/?authuser={quote(address)}#search/rfc822msgid%3A{quote(msgid, safe='')}"
            if msgid else None)
    return {"inbox": address, "when": when, "link": link}


def source_line(m, sender, subject):
    """HTML footer naming the source email, with a link that opens it."""
    from html import escape as esc
    parts = [f"📧 {esc(sender[:50])}", esc(subject[:70])]
    if m.get("when"):
        parts.append(f"received {esc(m['when'])}")
    line = "<i>" + " · ".join(parts) + f"\nInbox: {esc(m['inbox'])}</i>"
    if m.get("link"):
        line += f'\n<a href="{esc(m["link"], quote=True)}">📨 Open original email</a>'
    return line


def _header(msg, name):
    try:
        return str(make_header(decode_header(msg.get(name, ""))))
    except Exception:
        return msg.get(name, "")


def fetch_alerts(address, password, is_done):
    """-> list of (uid, sender, subject, date, text, links) for job-alert emails not processed yet."""
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(address, password)
    try:
        imap.select('"[Gmail]/All Mail"', readonly=True)  # also finds alerts filed under Promotions/Updates
        query = "from:(" + " OR ".join(SENDERS) + f") newer_than:{MAIL_DAYS}d"
        typ, data = imap.uid("search", None, "X-GM-RAW", f'"{query}"')
        uids = [int(u) for u in (data[0] or b"").split()]
        out = []
        for uid in uids:
            if is_done(uid):
                continue
            typ, msg_data = imap.uid("fetch", str(uid), "(BODY.PEEK[])")  # PEEK: doesn't mark as read
            if not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            subject, sender = _header(msg, "Subject"), _header(msg, "From")
            if not JOB_SUBJECT.search(subject):
                out.append((uid, sender, subject, meta(msg, address), None, None))  # not a job alert: just mark done
                continue
            text, links = email_text(msg)
            out.append((uid, sender, subject, meta(msg, address), text, links))
        return out
    finally:
        try:
            imap.logout()
        except Exception:
            pass


# ---------- follow-ups on jobs you applied to ----------
# Follow-ups are read from the same inboxes (ids -10, -11, -12 keep their place)
UPDATE_ACCOUNTS = [(a, pw, -10 - i) for i, (a, pw, _) in enumerate(ACCOUNTS)]
UPDATE_SENDERS = ["greenhouse.io", "greenhouse-mail.io", "lever.co", "myworkday.com", "workday.com",
                  "smartrecruiters.com", "ashbyhq.com", "icims.com", "successfactors", "taleo.net", "oraclecloud.com",
                  "hackerrank.com", "hackerearth.com", "mettl.com", "mercer", "codility.com", "imocha", "superset",
                  "joinsuperset.com", "hirepro", "amcat", "aspiringminds", "unstop.com", "naukri.com", "instahyre.com",
                  "wellfound.com", "cutshort.io", "zohorecruit", "keka.com", "darwinbox", "freshteam", "recruitee",
                  "workable.com", "jobvite", "eightfold.ai", "phenom", "calendly.com"]
UPDATE_SUBJECTS = ["application", "applied", "interview", "assessment", "shortlisted", "shortlist", "offer",
                   "next steps", "next round", "candidature", "candidate", "online test", "coding test",
                   "coding challenge", "hiring process", "selection process", "regret", "unfortunately",
                   "congratulations", "onboarding", "documents", "background verification", "joining", "schedule"]
NOT_UPDATE = re.compile(r"job alert|jobs for you|recommended jobs|new jobs|jobs matching|similar jobs|"
                        r"newsletter|webinar|course|% off|sale|digest", re.I)

UPDATE_PROMPT = """This email was received by a student who applies to jobs and internships.
Decide whether it is an update about one of THE STUDENT'S OWN job/internship applications or hiring processes:
application received/confirmation, online assessment or coding test invite, interview invite/schedule/reschedule,
shortlist, next round, rejection, offer, onboarding/document/background-check request, recruiter reaching out about
a specific role, or a reminder about any of these.
NOT an update: job alerts, job recommendations, newsletters, marketing, course ads, generic "apply now" mails.

Return only JSON:
{"is_update": bool, "company": str|null, "role": str|null,
 "status": "applied"|"assessment"|"interview"|"shortlisted"|"next round"|"offer"|"rejected"|"documents"|"recruiter"|"other",
 "action": "what the student must do, short, or null", "deadline": "date/time if any, or null",
 "link": "L<number> of the most important link (test/interview/portal), or null",
 "summary": "one or two short lines"}

EMAIL from __SENDER__, subject: __SUBJECT__
<<<
__TEXT__
>>>"""


def fetch_updates(address, password, is_done):
    """-> list of (uid, sender, subject, date, text, links) for possible application follow-ups."""
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(address, password)
    try:
        imap.select('"[Gmail]/All Mail"', readonly=True)
        subj = " OR ".join(f'"{s}"' if " " in s else s for s in UPDATE_SUBJECTS)
        query = (f"newer_than:{MAIL_DAYS}d -in:sent -in:spam (from:(" + " OR ".join(UPDATE_SENDERS)
                 + f") OR subject:({subj}))")
        typ, data = imap.uid("search", None, "X-GM-RAW", '"' + query.replace('"', '\\"') + '"')
        out = []
        for uid in [int(u) for u in (data[0] or b"").split()]:
            if is_done(uid):
                continue
            typ, msg_data = imap.uid("fetch", str(uid), "(BODY.PEEK[])")
            if not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            subject, sender = _header(msg, "Subject"), _header(msg, "From")
            if NOT_UPDATE.search(subject):
                out.append((uid, sender, subject, meta(msg, address), None, None))  # alert/marketing: mark done
                continue
            text, links = email_text(msg)
            out.append((uid, sender, subject, meta(msg, address), text, links))
        return out
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def site_name(sender):
    s = sender.lower()
    for name in ("LinkedIn", "Naukri", "Indeed", "Internshala", "Foundit", "Glassdoor", "Instahyre", "Cutshort",
                 "Wellfound", "Unstop", "Shine", "Apna", "Hirist", "iimjobs", "TimesJobs", "SimplyHired"):
        if name.lower() in s:
            return name
    return "Job site"


def job_key(job):
    """Same company + role = same job, whichever site or email it came from."""
    norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())  # noqa: E731
    return "job:" + norm(job.get("company")) + "|" + norm(job.get("title"))
