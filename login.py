"""Run once on your own laptop to create SESSION_STRING.

It asks for your phone number, the login code Telegram sends you, and your
2-step password if you have one. Copy the printed string into .env.
Keep it secret: anyone with it can use your Telegram account.
"""
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

api_id = int(input("API ID (from my.telegram.org): ").strip())
api_hash = input("API hash: ").strip()

with TelegramClient(StringSession(), api_id, api_hash) as client:
    print("\nLogged in as", client.get_me().first_name)
    print("\nSESSION_STRING (paste into .env, never share it):\n")
    print(client.session.save())
