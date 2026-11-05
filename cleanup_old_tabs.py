"""One-off: deletes the 14 tabs (and the bot's posts in them) made by the old version.

Only touches tabs listed in data/state.db (the old version's memory). Your own
messages and the new 🎯 / 🌐 tabs are not affected. Run: python cleanup_old_tabs.py
"""
import os
import sqlite3

import httpx
from dotenv import load_dotenv

load_dotenv(override=True)
token, group = os.environ["BOT_TOKEN"], os.environ["TARGET_GROUP_ID"]
db = sqlite3.connect("data/state.db")
tabs = db.execute("SELECT category, thread_id FROM topics").fetchall()
if not tabs:
    raise SystemExit("No old tabs to delete.")
print("This deletes these old tabs and everything the bot posted in them:")
for name, _ in tabs:
    print("  -", name)
if input("Type YES to continue: ").strip() != "YES":
    raise SystemExit("Cancelled.")
for name, thread_id in tabs:
    r = httpx.post(f"https://api.telegram.org/bot{token}/deleteForumTopic",
                   data={"chat_id": group, "message_thread_id": thread_id}).json()
    print(f"{name}: {'deleted' if r.get('ok') else r.get('description')}")
    if r.get("ok") or "not found" in str(r.get("description", "")).lower():
        db.execute("DELETE FROM topics WHERE category=?", (name,))
db.commit()
print("Done.")
