"""Copies your keys from .env (and the skills profile) into GitHub Secrets for the cloud bot.

Run after the GitHub repo exists:  python set_secrets.py
Values are sent straight to GitHub (encrypted there) and never printed.
"""
import json
import os
import subprocess

from dotenv import dotenv_values

NAMES = ["API_ID", "API_HASH", "SESSION_STRING", "BOT_TOKEN", "TARGET_GROUP_ID", "SOURCE_CHATS",
         "GEMINI_API_KEY", "GEMINI_MODEL", "PROFILE", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD",
         "GMAIL_ADDRESS2", "GMAIL_APP_PASSWORD2", "TOPIC_CAMPUS", "TOPIC_OFFCAMPUS", "TOPIC_UPDATES", "EXCLUDE_ROLES",
         "GMAIL_ADDRESS3", "GMAIL_APP_PASSWORD3",
         "CAMPUS_CHANNEL_ID", "OFFCAMPUS_CHANNEL_ID"]

env = dotenv_values(".env")
values = {n: (env.get(n) or "").strip() for n in NAMES}
try:
    values["SKILLS_PROFILE"] = json.load(open("data/skills_profile.json", encoding="utf-8"))["profile"]
except Exception:
    print("! data/skills_profile.json not found - run main.py once with your resumes first")

for name, value in values.items():
    if not value:
        print(f"- {name}: empty in .env, skipped")
        continue
    r = subprocess.run(["gh", "secret", "set", name], input=value, text=True, capture_output=True)
    print(f"{'✓' if r.returncode == 0 else '✗'} {name}" + ("" if r.returncode == 0 else f": {r.stderr.strip()}"))
