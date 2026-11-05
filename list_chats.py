"""Prints your Telegram chats with their ids, so you can fill SOURCE_CHATS and
TARGET_GROUP_ID in .env. Needs API_ID, API_HASH and SESSION_STRING in .env."""
import os

from dotenv import load_dotenv
from telethon import utils
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

load_dotenv(override=True)  # .env always wins over stale terminal variables
with TelegramClient(StringSession(os.environ["SESSION_STRING"]), int(os.environ["API_ID"]),
                    os.environ["API_HASH"]) as client:
    print(f"{'ID':>16}  {'TYPE':8}  NAME")
    for d in client.iter_dialogs():
        kind = "channel" if d.is_channel and not d.is_group else "group" if d.is_group else "user"
        if kind != "user":
            print(f"{utils.get_peer_id(d.entity):>16}  {kind:8}  {d.name}")
