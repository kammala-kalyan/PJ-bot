"""Placement Sorter - only the jobs that fit your resume.

Reads your college placement group(s) and any off-campus job channels with
YOUR account (read-only). Every job opening is checked against your resume(s)
in resumes/; only relevant ones are posted by YOUR bot into two tabs:
  🎯 Campus Jobs For Me     (from SOURCE_CHATS)
  🌐 Off-Campus Jobs For Me (from OFFCAMPUS_CHATS)
Everything else is skipped. Runs forever: history first, new messages within seconds.
"""
import asyncio
import hashlib
import html
import itertools
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv
from telethon import TelegramClient, events, utils
from telethon.sessions import StringSession

from classifier import Classifier, normalize
from resume import Resumes

load_dotenv(override=True)  # .env always wins over stale terminal variables
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("sorter")


def env(name, default=None):
    value = os.getenv(name, default)
    if value in (None, ""):
        raise SystemExit(f"Missing {name} in .env (see .env.example)")
    return value


def chat_list(name):
    return [s.strip() for s in os.getenv(name, "").split(",") if s.strip()]


API_ID = int(env("API_ID"))
API_HASH = env("API_HASH")
SESSION_STRING = env("SESSION_STRING")
BOT_TOKEN = env("BOT_TOKEN")
TARGET_GROUP_ID = int(env("TARGET_GROUP_ID"))
SOURCES = {"campus": chat_list("SOURCE_CHATS"), "offcampus": chat_list("OFFCAMPUS_CHATS")}
BACKFILL_DAYS = {"campus": int(os.getenv("BACKFILL_DAYS", "90")),
                 "offcampus": int(os.getenv("OFFCAMPUS_BACKFILL_DAYS", "7"))}  # 0 = no history
PROFILE = os.getenv("PROFILE", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or None
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
AI_MIN_INTERVAL = float(os.getenv("AI_MIN_INTERVAL", "6"))
MAX_MEDIA_MB = int(os.getenv("MAX_MEDIA_MB", "20"))
DB_PATH = os.getenv("DB_PATH", "data/state_v2.db")
LABELS_PATH = os.getenv("LABELS_PATH", "data/labels.jsonl")  # pre-checked history labels (optional)
ATTACH_WINDOW = timedelta(minutes=15)  # a file/photo with no text belongs to the message just before it
TOPICS = {"campus": "🎯 Campus Jobs For Me", "offcampus": "🌐 Off-Campus Jobs For Me",
          "updates": "📬 Application Updates"}
# Separate channels instead of topic tabs (CAMPUS_CHANNEL_ID / OFFCAMPUS_CHANNEL_ID in .env)
CHANNELS = {k: int(os.environ[f"{k.upper()}_CHANNEL_ID"]) for k in TOPICS if os.getenv(f"{k.upper()}_CHANNEL_ID")}
IST = timezone(timedelta(hours=5, minutes=30))
esc = html.escape


class BotError(RuntimeError):
    pass


class State:
    """Remembers tab ids, which messages were checked, and which jobs were posted (survives restarts)."""

    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)  # email reader checks it from a worker thread
        self.db.execute("CREATE TABLE IF NOT EXISTS topics(key TEXT PRIMARY KEY, thread_id INTEGER)")
        self.db.execute("CREATE TABLE IF NOT EXISTS processed(chat_id INTEGER, msg_id INTEGER, posted INTEGER,"
                        " PRIMARY KEY(chat_id, msg_id))")
        self.db.execute("CREATE TABLE IF NOT EXISTS posted_text(hash TEXT PRIMARY KEY)")
        self.db.execute("CREATE TABLE IF NOT EXISTS seen_jobs(key TEXT PRIMARY KEY)")  # career-page openings
        self.db.commit()

    def job_seen(self, key):
        return self.db.execute("SELECT 1 FROM seen_jobs WHERE key=?", (key,)).fetchone() is not None

    def mark_jobs(self, keys):
        self.db.executemany("INSERT OR IGNORE INTO seen_jobs VALUES(?)", [(k,) for k in keys])
        self.db.commit()

    def topic(self, key):
        row = self.db.execute("SELECT thread_id FROM topics WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_topic(self, key, thread_id):
        self.db.execute("INSERT OR REPLACE INTO topics VALUES(?,?)", (key, thread_id))
        self.db.commit()

    def drop_topic(self, key):
        self.db.execute("DELETE FROM topics WHERE key=?", (key,))
        self.db.commit()

    def done(self, chat_id, msg_id):
        return self.db.execute("SELECT 1 FROM processed WHERE chat_id=? AND msg_id=?",
                               (chat_id, msg_id)).fetchone() is not None

    def mark(self, chat_id, msg_id, posted):
        self.db.execute("INSERT OR REPLACE INTO processed VALUES(?,?,?)", (chat_id, msg_id, int(posted)))
        self.db.commit()

    def last_id(self, chat_id):
        row = self.db.execute("SELECT MAX(msg_id) FROM processed WHERE chat_id=?", (chat_id,)).fetchone()
        return row[0]

    def seen_key(self, key):
        """True if this job key (e.g. company+role) was already posted; records it otherwise."""
        h = hashlib.sha1(key.encode()).hexdigest()
        if self.db.execute("SELECT 1 FROM posted_text WHERE hash=?", (h,)).fetchone():
            return True
        self.db.execute("INSERT INTO posted_text VALUES(?)", (h,))
        self.db.commit()
        return False

    def seen_text(self, text):
        """True if the same job text was already posted (e.g. reposted in another channel)."""
        h = hashlib.sha1(re.sub(r"\W+", "", text.lower())[:600].encode()).hexdigest()
        if self.db.execute("SELECT 1 FROM posted_text WHERE hash=?", (h,)).fetchone():
            return True
        self.db.execute("INSERT INTO posted_text VALUES(?)", (h,))
        self.db.commit()
        return False


class Bot:
    """Tiny Telegram Bot API client."""

    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}"
        self.http = httpx.AsyncClient(timeout=120)

    async def call(self, method, data=None, files=None):
        for _ in range(6):
            r = await self.http.post(f"{self.base}/{method}", data=data, files=files)
            j = r.json()
            if j.get("ok"):
                return j["result"]
            if j.get("error_code") == 429:  # Telegram rate limit: wait and retry
                await asyncio.sleep(j.get("parameters", {}).get("retry_after", 5) + 1)
                continue
            raise BotError(f"{method}: {j.get('description')}")
        raise BotError(f"{method}: still rate-limited after retries")

    @staticmethod
    def where(dest):
        """dest = a topic id in the target group, or ("channel", chat_id) for a separate channel."""
        if isinstance(dest, tuple):
            return {"chat_id": dest[1]}
        return {"chat_id": TARGET_GROUP_ID, "message_thread_id": dest}

    async def send_text(self, dest, text):
        return await self.call("sendMessage", {
            **self.where(dest), "text": text,
            "parse_mode": "HTML", "link_preview_options": json.dumps({"is_disabled": True}),
        })

    async def send_file(self, dest, data, filename, is_photo):
        field, method = ("photo", "sendPhoto") if is_photo else ("document", "sendDocument")
        return await self.call(method, self.where(dest), files={field: (filename, data)})


def media_info(msg):
    """-> (label, should we copy the file?, filename, is_photo)"""
    if not msg.media or msg.web_preview:
        return None, False, None, False
    if msg.photo:
        return "🖼 photo", True, "photo.jpg", True
    if msg.document and not msg.gif and not msg.sticker:
        name = msg.file.name or f"file{msg.file.ext or ''}"
        return f"📎 {name}", (msg.file.size or 0) <= MAX_MEDIA_MB * 1024 * 1024, name, False
    return None, False, None, False


def message_link(chat, msg_id):
    if getattr(chat, "username", None):
        return f"https://t.me/{chat.username}/{msg_id}"
    peer = str(utils.get_peer_id(chat))
    return f"https://t.me/c/{peer[4:]}/{msg_id}" if peer.startswith("-100") else None


def load_labels(path):
    labels = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    d = json.loads(line)
                    labels[(d["chat_id"], d["id"])] = d
        log.info("Loaded %s pre-checked history labels", len(labels))
    return labels


def label_to_info(label):
    """History label -> info. Uses 'relevant' if set (resume check), else the earlier 'for_you' flag."""
    info = normalize({**label, "relevant": label.get("relevant", label.get("for_you", False))})
    info["reason"] = label.get("reason")
    return info


class Sorter:
    def __init__(self):
        self.client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
        self.bot = Bot(BOT_TOKEN)
        self.state = State(DB_PATH)
        self.resumes = Resumes(api_key=GEMINI_API_KEY, model=GEMINI_MODEL)
        self.classifier = Classifier(self.resumes, PROFILE, GEMINI_API_KEY, GEMINI_MODEL, AI_MIN_INTERVAL)
        self.queue = asyncio.PriorityQueue()  # live messages (priority 0) jump ahead of history (1)
        self.seq = itertools.count()
        self.chats = {}  # peer id -> (entity, "campus"/"offcampus")
        self.last_posted = {}  # chat id -> (date, posted?) of the previous text message, for text-less files
        self.labels = load_labels(LABELS_PATH)

    async def thread_for(self, key):
        channel = CHANNELS.get(key)  # a separate channel for this kind of job, if set
        if channel:
            return ("channel", channel)
        fixed = os.getenv(f"TOPIC_{key.upper()}")  # e.g. TOPIC_CAMPUS=208: always use this existing tab
        if fixed:
            return int(fixed)
        thread_id = self.state.topic(key)
        if thread_id:
            return thread_id
        res = await self.bot.call("createForumTopic", {"chat_id": TARGET_GROUP_ID, "name": TOPICS[key]})
        self.state.set_topic(key, res["message_thread_id"])
        log.info("Created tab %s", TOPICS[key])
        return res["message_thread_id"]

    async def post_to(self, key, send):
        try:
            await send(await self.thread_for(key))
        except BotError as e:
            if "thread not found" in str(e).lower() or "topic_deleted" in str(e).lower():
                self.state.drop_topic(key)  # tab was deleted: recreate it and retry once
                await send(await self.thread_for(key))
            else:
                raise

    def build_post(self, info, chat, msg, text):
        title = f"<b>{esc(info.get('role') or 'Job opening')}</b>"
        if info.get("company"):
            title += f" · {esc(info['company'])}"
        head = [title]
        meta = [f"{icon} {esc(info[k])}" for icon, k in (("📍", "location"), ("💰", "pay"), ("⏳", "deadline"))
                if info.get(k)]
        if meta:
            head.append("   ".join(meta))
        if info.get("apply_link"):
            head.append(f'🔗 <a href="{esc(info["apply_link"], quote=True)}">Apply link</a>')
        if info.get("reason"):
            head.append(f"✅ <i>{esc(info['reason'])}</i>")
        header = "\n".join(head) + "\n\n"

        when = msg.date.astimezone(IST).strftime("%d %b %Y, %I:%M %p")
        link = message_link(chat, msg.id)
        footer = f"\n\n<i>{esc(getattr(chat, 'title', '') or 'Chat')} · {when}</i>"
        if link:
            footer += f' · <a href="{link}">original</a>'

        room = 3900 - len(header) - len(footer)
        raw = text
        while len(esc(raw)) > room:
            raw = raw[: int(len(raw) * 0.9)]
        body = esc(raw) + ("…" if raw != text else "")
        if len(body) > 300:
            body = f"<blockquote expandable>{body}</blockquote>"
        return header + body + footer

    # ---------- processing ----------
    def enqueue(self, priority, chat_id, msg):
        if getattr(msg, "action", None) or self.state.done(chat_id, msg.id):
            return
        self.queue.put_nowait((priority, next(self.seq), chat_id, msg))

    async def worker(self):
        while True:
            _, _, chat_id, msg = await self.queue.get()
            try:
                await self.handle(chat_id, msg)
            except Exception:
                log.exception("Failed on message %s in %s", msg.id, chat_id)
            finally:
                self.queue.task_done()

    async def handle(self, chat_id, msg):
        if self.state.done(chat_id, msg.id):
            return
        chat, kind = self.chats[chat_id]
        text = (msg.message or "").strip()
        label, copy_file, filename, is_photo = media_info(msg)

        if not text:  # a file/photo alone: post it only if the message right before it was a posted job
            prev = self.last_posted.get(chat_id)
            if label and copy_file and prev and prev[1] and msg.date - prev[0] <= ATTACH_WINDOW:
                data = await msg.download_media(file=bytes)
                await self.post_to(kind, lambda t: self.bot.send_file(t, data, filename, is_photo))
                log.info("#%s attachment -> %s", msg.id, TOPICS[kind])
            self.state.mark(chat_id, msg.id, False)
            return

        if (chat_id, msg.id) in self.labels:
            info = label_to_info(self.labels[(chat_id, msg.id)])
        else:
            info = await self.classifier.check(text)
        posted = bool(info["relevant"]) and not self.state.seen_text(text)
        self.last_posted[chat_id] = (msg.date, posted)

        if posted:
            post = self.build_post(info, chat, msg, text)

            async def send(thread_id):
                await self.bot.send_text(thread_id, post)
                if copy_file:
                    try:
                        await self.bot.send_file(thread_id, await msg.download_media(file=bytes), filename, is_photo)
                    except Exception as e:
                        log.warning("Could not copy attachment of message %s: %s", msg.id, e)

            await self.post_to(kind, send)
            log.info("#%s ✅ %s · %s -> %s", msg.id, info.get("role"), info.get("company"), TOPICS[kind])
        self.state.mark(chat_id, msg.id, posted)

    async def backfill(self, chat_id):
        chat, kind = self.chats[chat_id]
        days = BACKFILL_DAYS[kind]
        if days <= 0:
            return
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        recent = []
        async for m in self.client.iter_messages(chat):  # newest first
            if m.date < cutoff:
                break
            recent.append(m)
        for m in reversed(recent):  # oldest first
            self.enqueue(1, chat_id, m)
        log.info("Checking last %s days of %s (%s messages)", days, getattr(chat, "title", chat_id), len(recent))

    # ---------- startup ----------
    async def setup(self):
        """Connects, checks the bot and target group, and resolves the source chats."""
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise SystemExit("SESSION_STRING is invalid or expired. Run login.py again.")
        self.resumes.refresh()

        me = await self.bot.call("getMe")
        for key, chat_id in CHANNELS.items():
            ch = await self.bot.call("getChat", {"chat_id": chat_id})  # fails clearly if the bot isn't admin there
            log.info("Bot @%s posts %s jobs into channel '%s'", me["username"], key, ch.get("title"))
        if len(CHANNELS) < len(TOPICS):
            target = await self.bot.call("getChat", {"chat_id": TARGET_GROUP_ID})
            if not target.get("is_forum"):
                raise SystemExit("Turn on Topics in your target group (Group settings → Topics), then restart.")
            log.info("Bot @%s will post into '%s'", me["username"], target.get("title"))

        await self.client.get_dialogs()  # loads your chats so ids can be resolved
        for kind, sources in SOURCES.items():
            for s in sources:
                entity = await self.client.get_entity(int(s) if s.lstrip("-").isdigit() else s)
                peer_id = utils.get_peer_id(entity)
                if peer_id == TARGET_GROUP_ID:
                    raise SystemExit("The target group can't also be a source chat.")
                self.chats[peer_id] = (entity, kind)
                log.info("Watching (%s): %s", kind, getattr(entity, "title", s))
        if not self.chats:
            raise SystemExit("Add your placement group id(s) to SOURCE_CHATS in .env")
        for kind in {k for _, k in self.chats.values()}:
            await self.thread_for(kind)
        log.info("Tabs in use: %s", {k: self.state.topic(k) for k in TOPICS})

    async def run(self):
        """Always-on mode (laptop / server): history first, then live messages."""
        await self.setup()

        @self.client.on(events.NewMessage(chats=list(self.chats)))
        async def on_new(event):
            self.enqueue(0, event.chat_id, event.message)

        asyncio.create_task(self.worker())
        for chat_id in self.chats:
            asyncio.create_task(self.backfill(chat_id))
        log.info("Running. Only jobs that fit your resume will be posted.")
        await self.client.run_until_disconnected()


async def main():
    while True:  # reconnect automatically if Telegram drops the connection
        try:
            await Sorter().run()
        except SystemExit:
            raise
        except Exception:
            log.exception("Crashed; restarting in 30s")
            await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(main())
