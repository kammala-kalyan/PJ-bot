"""Downloads past messages from SOURCE_CHATS into data/history.jsonl (read-only, posts nothing).

Usage: python export_history.py [days]     (default 90)
Used for one-off sorting of history; main.py handles new messages.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from telethon import utils
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

load_dotenv(".env", override=True)
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 90
OUT = "data/history.jsonl"
cutoff = datetime.now(timezone.utc) - timedelta(days=DAYS)


def media_label(m):
    if not m.media or m.web_preview:
        return None
    if m.photo:
        return "photo"
    if m.document:
        return f"file:{m.file.name or m.file.ext or ''}"
    return "other"


os.makedirs("data", exist_ok=True)
total = 0
with TelegramClient(StringSession(os.environ["SESSION_STRING"]), int(os.environ["API_ID"]),
                    os.environ["API_HASH"]) as client, open(OUT, "w", encoding="utf-8") as f:
    for s in os.environ["SOURCE_CHATS"].split(","):
        chat = client.get_entity(int(s.strip()))
        n = 0
        for m in client.iter_messages(chat):  # newest first
            if m.date < cutoff:
                break
            if m.action:  # "X joined the group" etc.
                continue
            f.write(json.dumps({
                "chat_id": utils.get_peer_id(chat), "chat": chat.title, "id": m.id,
                "date": m.date.isoformat(), "text": m.message or "", "media": media_label(m),
                "grouped_id": m.grouped_id,
            }, ensure_ascii=False) + "\n")
            n += 1
        total += n
        print(f"{n:>6}  {chat.title}")
print(f"{total:>6}  saved to {OUT}")
