"""One-time setup of the shared group for friends (campus + off-campus jobs only).

Before running, in Telegram:
  1. Create a new group (any name) and turn Topics ON (Group settings -> Topics).
  2. Add your bot and make it admin with "Manage topics" and "Post messages".
Then run:  python setup_friends_group.py

It creates the two tabs, hides the General tab, makes the group read-only for members,
saves the ids to .env and GitHub Secrets, and prints an invite link to share.
"""
import os
import subprocess

import httpx
from dotenv import load_dotenv

load_dotenv(".env", override=True)
TOKEN = os.environ["BOT_TOKEN"]
OWN_GROUP = int(os.environ["TARGET_GROUP_ID"])
TABS = {"campus": "🎯 Campus Jobs", "offcampus": "🌐 Off-Campus Jobs"}


def call(method, **data):
    r = httpx.post(f"https://api.telegram.org/bot{TOKEN}/{method}", json=data, timeout=60).json()
    if not r.get("ok"):
        raise SystemExit(f"{method}: {r.get('description')}")
    return r["result"]


def find_group():
    """The newest group (other than your own) that the bot was added to."""
    found = {}
    for u in call("getUpdates", allowed_updates=["my_chat_member", "message"]):
        chat = (u.get("my_chat_member") or u.get("message") or {}).get("chat", {})
        if chat.get("type") == "supergroup" and chat["id"] != OWN_GROUP:
            found[chat["id"]] = chat.get("title")
    if not found:
        raise SystemExit("Couldn't find the new group. Check the bot is an admin there and Topics are on, then retry.")
    chat_id = list(found)[-1]
    print(f"Found group: {found[chat_id]}")
    return chat_id


def save(values):
    lines = [ln for ln in open(".env", encoding="utf-8").read().splitlines() if ln.split("=", 1)[0] not in values]
    lines += [f"{k}={v}" for k, v in values.items()]
    open(".env", "w", encoding="utf-8").write("\n".join(lines) + "\n")
    for k, v in values.items():
        ok = subprocess.run(["gh", "secret", "set", k], input=str(v), text=True, capture_output=True).returncode == 0
        print(f"{'✓' if ok else '✗ (run: python set_secrets.py)'} {k}")


group = int(os.getenv("FRIENDS_GROUP_ID") or 0) or find_group()
if not call("getChat", chat_id=group).get("is_forum"):
    raise SystemExit("Turn Topics ON in the new group (Group settings -> Topics), then run this again.")
values = {"FRIENDS_GROUP_ID": group}
for key, name in TABS.items():
    existing = os.getenv(f"FRIENDS_TOPIC_{key.upper()}")
    values[f"FRIENDS_TOPIC_{key.upper()}"] = existing or call("createForumTopic", chat_id=group, name=name)[
        "message_thread_id"]
try:
    call("hideGeneralForumTopic", chat_id=group)
except SystemExit as e:
    print("Note: couldn't hide the General tab:", e)
# Members read and react, only admins (you and the bot) post
call("setChatPermissions", chat_id=group, permissions={"can_send_messages": False, "can_send_other_messages": False,
                                                        "can_add_web_page_previews": False})
save(values)
print("\nInvite link for friends:", call("createChatInviteLink", chat_id=group, name="Friends")["invite_link"])
