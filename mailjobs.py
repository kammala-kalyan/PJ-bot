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

log = logging.getLogger("mail")

GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "")
MAIL_DAYS = int(os.getenv("MAIL_DAYS", "2"))  # how far back to look for unread-by-bot alerts
MAIL_CHAT_ID = -1  # emails are stored in the same "processed" table under this pseudo chat id

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


def _header(msg, name):
    try:
        return str(make_header(decode_header(msg.get(name, ""))))
    except Exception:
        return msg.get(name, "")


def fetch_alerts(is_done):
    """-> list of (uid, sender, subject, date, text, links) for job-alert emails not processed yet."""
    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD):
        return []
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
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
                out.append((uid, sender, subject, msg.get("Date", ""), None, None))  # not a job alert: just mark done
                continue
            text, links = email_text(msg)
            out.append((uid, sender, subject, msg.get("Date", ""), text, links))
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
