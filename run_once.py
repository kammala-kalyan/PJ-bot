"""One check, then exit - for GitHub Actions (runs every few minutes).

1. Campus: reads messages that arrived in your placement groups since the last
   run and posts the jobs that fit your resume to 🎯 Campus Jobs For Me.
2. Off-campus: reads new job-alert emails (LinkedIn, Naukri, Indeed, ...) and
   posts the jobs that fit your resume to 🌐 Off-Campus Jobs For Me.

Progress is remembered in data/state_v2.db, so nothing is posted twice.
Run locally with:  python run_once.py
"""
import asyncio
import html
import json
import logging
import os
import time

import careers
import mailjobs
from main import IST, Sorter, log

esc = html.escape
MAX_PER_CHAT = 300  # safety cap per run


async def check_campus(s):
    for chat_id, (entity, kind) in s.chats.items():
        last = s.state.last_id(chat_id)
        if last is None:  # first run for this chat: start from now, don't re-post history
            newest = await s.client.get_messages(entity, limit=1)
            if newest:
                s.state.mark(chat_id, newest[0].id, False)
            log.info("Started watching %s from now", getattr(entity, "title", chat_id))
            continue
        msgs = [m async for m in s.client.iter_messages(entity, min_id=last, reverse=True, limit=MAX_PER_CHAT)]
        for m in msgs:
            if getattr(m, "action", None):
                s.state.mark(chat_id, m.id, False)
                continue
            try:
                await s.handle(chat_id, m)
            except Exception:
                log.exception("Failed on message %s in %s", m.id, chat_id)
                break  # retry from here next run
        if msgs:
            log.info("%s: checked %s new message(s)", getattr(entity, "title", chat_id), len(msgs))


def email_post(job, site, sender, subject, link, src, private=True):
    """private=False leaves out your inbox address and Gmail link (for the friends group)."""
    title = f"<b>{esc(job.get('title') or 'Job opening')}</b>"
    if job.get("company"):
        title += f" · {esc(job['company'])}"
    lines = [title]
    meta = [f"{icon} {esc(job[k])}" for icon, k in (("📍", "location"), ("💰", "pay")) if job.get(k)]
    if meta:
        lines.append("   ".join(meta))
    if link:
        lines.append(f'🔗 <a href="{esc(link, quote=True)}">Apply / view job</a>')
    if job.get("reason"):
        lines.append(f"✅ <i>{esc(job['reason'])}</i>")
    lines.append(f"\n<b>Source:</b> {esc(site)} job alert")
    if private:
        lines.append(mailjobs.source_line(src, sender, subject))
    return "\n".join(lines)


async def check_email(s):
    for address, password, box_id in mailjobs.ACCOUNTS:
        try:
            await check_inbox(s, address, password, box_id)
        except Exception:
            log.exception("Email check failed for one inbox; will retry next round")


async def check_inbox(s, address, password, box_id):
    alerts = await asyncio.to_thread(mailjobs.fetch_alerts, address, password,
                                     lambda uid: s.state.done(box_id, uid))
    if not s.state.done(box_id, 0):  # newly connected inbox: older emails are history, start from now
        for a in alerts:
            s.state.mark(box_id, a[0], False)
        s.state.mark(box_id, 0, False)
        log.info("Started reading job alerts from a new inbox (from now on)")
        return
    posted = 0
    for uid, sender, subject, date, text, links in alerts:
        if text is None or len(text) < 80:  # not a job alert
            s.state.mark(box_id, uid, False)
            continue
        prompt = s.classifier.fill(mailjobs.EMAIL_PROMPT, text, limit=15000)
        prompt = prompt.replace("__SENDER__", sender).replace("__SUBJECT__", subject)
        try:
            data = await s.classifier.ask_json(prompt)
        except Exception as e:
            log.warning("AI unavailable for email '%s' (%s); will retry next run", subject[:60], e)
            break  # leave it unprocessed so the next run tries again
        jobs = data.get("jobs", []) if isinstance(data, dict) else data
        site = mailjobs.site_name(sender)
        for job in jobs or []:
            if not isinstance(job, dict) or not job.get("relevant"):
                continue
            if s.state.seen_key(mailjobs.job_key(job)):
                continue  # same job already posted (other email / other site)
            link = None
            m = str(job.get("link") or "")
            if m.upper().startswith("L") and m[1:].isdigit() and 0 < int(m[1:]) <= len(links):
                link = links[int(m[1:]) - 1]
            full = await asyncio.to_thread(mailjobs.job_description, link)
            if full:  # check experience etc. against the full job post, not just the alert's one-liner
                if mailjobs.SENIOR_LEVEL.search(full):
                    log.info("Skipped %s · %s: LinkedIn lists it as a senior role", job.get("title"), job.get("company"))
                    continue
                info = await s.classifier.check_opening(f"Company: {job.get('company')}\nJob title: {job.get('title')}\n"
                                                        f"Location: {job.get('location')}\n\n{full}")
                if not info.get("relevant"):
                    log.info("Skipped %s · %s after reading the full post: %s", job.get("title"), job.get("company"),
                             info.get("reason"))
                    continue
                job["reason"] = info.get("reason") or job.get("reason")
            post = email_post(job, site, sender, subject, link, date)
            public = email_post(job, site, sender, subject, link, date, private=False)
            await s.post_to("offcampus", lambda t, p=post: s.bot.send_text(t, p),
                            shared=lambda t, p=public: s.bot.send_text(t, p))
            posted += 1
            log.info("📧 ✅ %s · %s (%s)", job.get("title"), job.get("company"), site)
        s.state.mark(box_id, uid, True)
    if alerts:
        log.info("Checked %s alert email(s), posted %s matching job(s)", len(alerts), posted)


STATUS_ICON = {"applied": "📝", "assessment": "🧪", "interview": "🗓", "shortlisted": "⭐", "next round": "➡️",
               "offer": "🎉", "rejected": "❌", "documents": "📄", "recruiter": "👋", "other": "📬"}


async def check_updates(s):
    """Follow-up emails about jobs you applied to -> 📬 Application Updates tab."""
    for address, password, box_id in mailjobs.UPDATE_ACCOUNTS:
        try:
            mails = await asyncio.to_thread(mailjobs.fetch_updates, address, password,
                                            lambda uid, b=box_id: s.state.done(b, uid))
        except Exception:
            log.exception("Update check failed for one inbox; will retry next round")
            continue
        if not s.state.done(box_id, 0):  # newly connected inbox: start from now
            for m in mails:
                s.state.mark(box_id, m[0], False)
            s.state.mark(box_id, 0, False)
            log.info("Started reading application updates from a new inbox (from now on)")
            continue
        for uid, sender, subject, date, text, links in mails:
            if text is None:
                s.state.mark(box_id, uid, False)
                continue
            prompt = (mailjobs.UPDATE_PROMPT.replace("__SENDER__", sender).replace("__SUBJECT__", subject)
                      .replace("__TEXT__", text[:8000]))
            try:
                d = await s.classifier.ask_json(prompt)
            except Exception as e:
                log.warning("AI unavailable for update '%s' (%s); will retry", subject[:50], e)
                break
            if isinstance(d, list):
                d = d[0] if d else {}
            if d.get("is_update"):
                link = None
                m = str(d.get("link") or "")
                if m.upper().startswith("L") and m[1:].isdigit() and 0 < int(m[1:]) <= len(links):
                    link = links[int(m[1:]) - 1]
                status = (d.get("status") or "other").lower()
                head = f"{STATUS_ICON.get(status, '📬')} <b>{esc(status.title())}</b>"
                who = " · ".join(esc(x) for x in (d.get("role"), d.get("company")) if x)
                lines = [head + (f" - {who}" if who else "")]
                if d.get("summary"):
                    lines.append(esc(d["summary"]))
                if d.get("action"):
                    lines.append(f"👉 <b>To do:</b> {esc(d['action'])}")
                if d.get("deadline"):
                    lines.append(f"⏳ <b>By:</b> {esc(d['deadline'])}")
                if link:
                    lines.append(f'🔗 <a href="{esc(link, quote=True)}">Open</a>')
                lines.append("\n" + mailjobs.source_line(date, sender, subject))
                post = "\n".join(lines)
                await s.post_to("updates", lambda t, p=post: s.bot.send_text(t, p))
                log.info("📬 %s · %s · %s", status, d.get("company"), d.get("role"))
            s.state.mark(box_id, uid, bool(d.get("is_update")))


COMPANIES = json.load(open("companies.json", encoding="utf-8")) if os.path.exists("companies.json") else []
MAX_AI_PER_ROUND = int(os.getenv("CAREERS_MAX_AI", "12"))  # keeps the free AI quota safe on busy days


async def check_careers(s):
    """New openings on company career pages -> resume check -> 🌐 tab."""
    if not COMPANIES:
        return
    results = await careers.fetch_all(COMPANIES)
    new = []
    for company, jobs in results:
        if jobs is None:
            continue
        prefix = f"{company['ats']}:{company['slug']}:"
        keys = [prefix + j["id"] for j in jobs]
        if not s.state.job_seen(prefix + "*"):  # first time: remember today's openings, alert only on new ones
            s.state.mark_jobs(keys + [prefix + "*"])
            continue
        fresh = [(k, j) for k, j in zip(keys, jobs) if not s.state.job_seen(k)]
        s.state.mark_jobs([k for k, _ in fresh])
        new += [(company, j) for _, j in fresh if careers.candidate(j)]
    posted = 0
    for company, job in new[:MAX_AI_PER_ROUND]:
        if s.state.seen_key(mailjobs.job_key({"company": company["name"], "title": job["title"]})):
            continue  # same role already posted (e.g. listed for several cities)
        text = await careers.job_text(company, job)
        info = await s.classifier.check_opening(
            f"Company: {company['name']}\nJob title: {job['title']}\nLocation: {job['location']}\n\n{text}")
        if not info.get("relevant"):
            continue
        info.update(company=company["name"], role=job["title"], location=job["location"], apply_link=job["url"])
        post = career_post(info, company)
        await s.post_to("offcampus", lambda t, p=post: s.bot.send_text(t, p))
        posted += 1
        log.info("🏢 ✅ %s · %s", job["title"], company["name"])
    if new:
        log.info("Career pages: %s new fresher-level opening(s), posted %s matching", len(new), posted)


def career_post(info, company):
    lines = [f"<b>{esc(info['role'])}</b> · {esc(info['company'])}"]
    meta = [f"{icon} {esc(info[k])}" for icon, k in (("📍", "location"), ("💰", "pay"), ("⏳", "deadline"))
            if info.get(k)]
    if meta:
        lines.append("   ".join(meta))
    lines.append(f'🔗 <a href="{esc(info["apply_link"], quote=True)}">Apply on company site</a>')
    if info.get("reason"):
        lines.append(f"✅ <i>{esc(info['reason'])}</i>")
    platform = {"greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby", "smartrecruiters": "SmartRecruiters",
                "workday": "Workday"}.get(company["ats"], company["ats"])
    lines.append(f"\n<b>Source:</b> {esc(company['name'])} official careers page (via {platform})")
    lines.append("<i>🏢 Found minutes after the company posted it - apply early</i>")
    return "\n".join(lines)


async def main():
    # GitHub can only start a run every 5+ minutes, so each run stays awake for LOOP_MINUTES
    # and checks every CHECK_EVERY seconds; the next scheduled run takes over when it ends.
    loop_minutes = float(os.getenv("LOOP_MINUTES", "0"))  # 0 = single check (local use)
    check_every = int(os.getenv("CHECK_EVERY", "120"))
    end = time.monotonic() + loop_minutes * 60
    s = Sorter()
    await s.setup()
    try:
        if mailjobs.ACCOUNTS or COMPANIES:
            await s.thread_for("offcampus")
        while True:
            await check_campus(s)
            try:
                await check_careers(s)
            except Exception:
                log.exception("Career-page check failed; will retry next round")
            if mailjobs.UPDATE_ACCOUNTS:
                await check_updates(s)
            if mailjobs.ACCOUNTS:
                try:
                    await check_email(s)
                except Exception:
                    log.exception("Email check failed; will retry next round")
            if time.monotonic() + check_every > end:
                break
            await asyncio.sleep(check_every)
    finally:
        await s.client.disconnect()
        await s.bot.http.aclose()
        await s.classifier.http.aclose()
    log.info("Done at %s", __import__("datetime").datetime.now(IST).strftime("%d %b %I:%M %p"))


if __name__ == "__main__":
    logging.getLogger("telethon").setLevel(logging.WARNING)
    asyncio.run(main())
