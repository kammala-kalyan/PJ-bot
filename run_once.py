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
import logging

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


def email_post(job, site, subject, link):
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
    lines.append(f"\n<i>📧 {esc(site)} alert · {esc(subject[:80])}</i>")
    return "\n".join(lines)


async def check_email(s):
    if not (mailjobs.GMAIL_ADDRESS and mailjobs.GMAIL_APP_PASSWORD):
        return
    alerts = await asyncio.to_thread(mailjobs.fetch_alerts, lambda uid: s.state.done(mailjobs.MAIL_CHAT_ID, uid))
    posted = 0
    for uid, sender, subject, date, text, links in alerts:
        if text is None or len(text) < 80:  # not a job alert
            s.state.mark(mailjobs.MAIL_CHAT_ID, uid, False)
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
            post = email_post(job, site, subject, link)
            await s.post_to("offcampus", lambda t, p=post: s.bot.send_text(t, p))
            posted += 1
            log.info("📧 ✅ %s · %s (%s)", job.get("title"), job.get("company"), site)
        s.state.mark(mailjobs.MAIL_CHAT_ID, uid, True)
    if alerts:
        log.info("Checked %s alert email(s), posted %s matching job(s)", len(alerts), posted)


async def main():
    s = Sorter()
    await s.setup()
    try:
        await check_campus(s)
        if mailjobs.GMAIL_ADDRESS:
            await s.thread_for("offcampus")
            await check_email(s)
    finally:
        await s.client.disconnect()
        await s.bot.http.aclose()
        await s.classifier.http.aclose()
    log.info("Done at %s", __import__("datetime").datetime.now(IST).strftime("%d %b %I:%M %p"))


if __name__ == "__main__":
    logging.getLogger("telethon").setLevel(logging.WARNING)
    asyncio.run(main())
