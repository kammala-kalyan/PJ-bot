# Placement Sorter 🤖📋

Automatically sorts **every** message from your college placement Telegram group into tabs of your own private group:

| Tab | What goes there |
|---|---|
| ⭐ For You | Copies of jobs matching your profile (`PROFILE` in `classifier.py`) |
| 🤖 AI · ML · Data Science · Analytics | AI/ML/GenAI/DS/analyst roles |
| 💻 Software · SDE · Web · QA | SDE, full stack, app, testing |
| ☁️ ServiceNow · Cloud · DevOps · Cyber | ServiceNow, cloud, DevOps, cyber |
| 🏢 Mass Hiring | TCS / Infosys / Capgemini / LTM / HCLTech… drives |
| 💼 Consulting · Big 4 · Finance · HR · Ops | EY, KPMG, Deloitte, business roles |
| 📈 Sales · Marketing · BD | Sales, marketing, business development |
| ⚙️ Core · EEE · ECE · VLSI · Mech · Civil | Core engineering roles |
| 🧩 Other Jobs | Real jobs that fit nowhere else |
| 📝 Shortlists · Tests · Interviews | Shortlists, test links, interview schedules, PPTs |
| 🎉 Final Selections | Final selects, offers |
| 🎤 Events · Hackathons · Talks | Expert talks, hackathons, competitions, courses |
| 📢 Notices | General placement-office instructions |
| 🗑 Unimportant | Greetings, thanks, spam, chit-chat — kept, never deleted |

Each job post shows **role · company #hashtag, location, pay, deadline, apply link**, the full original text (tap to expand), attached PDFs/images, and a link back to the original message. Tap a company hashtag (e.g. `#KPMG`) to see that company's whole story — opening, shortlists, interviews, selects — across all tabs. Files posted without text (shortlist sheets, JD PDFs) go into the same tab as the message just before them.

**History labels:** `data/labels.jsonl` holds hand-checked labels for past messages (made once with `export_history.py` + `build_labels.py`); `main.py` uses them instead of the AI for those messages.

**Once it's running you do nothing.** New messages are sorted within seconds, 24/7. On the first run it also sorts the group's old messages in the background.

---

## How it works

```
Placement group ──(your account, read-only)──► main.py ──► Gemini AI picks tab ──► your bot posts into that tab
```

- Your **account** reads the placement group. A bot can't read a group it isn't admin of, or old history. The script never posts or replies in the placement group.
- Your **bot** posts into your own group.
- If the AI is unavailable, keyword rules decide instead, so no message is ever skipped.
- Sorted message ids are saved in `data/state.db`, so restarts never post duplicates.

---

## One-time setup (≈15 min)

### 1. Telegram API keys
Go to **https://my.telegram.org** → log in → **API development tools** → create an app (any name). Copy **API ID** and **API hash**.

### 2. Create the bot
In Telegram, open **@BotFather** → `/newbot` → pick a name → copy the **token**.

### 3. Create your sorted group
1. New group (e.g. "Placement Sorter"), just you in it.
2. Group settings → **Topics → ON**.
3. Add your bot → make it **admin** with *Manage Topics* and *Post messages* allowed.

### 4. Free AI key
**https://aistudio.google.com/apikey** → Create API key. (Optional, but much more accurate than keywords.)

### 5. Run it once on your laptop
```bash
cd placement-sorter
pip install -r requirements.txt
python login.py          # enter phone + Telegram code → prints SESSION_STRING
cp .env.example .env     # (Windows: copy .env.example .env) then fill in all values
python list_chats.py     # find the ids of your placement group and your new group
python main.py           # tabs appear in your group and messages start flowing in
```
Stop with Ctrl+C once you see it working, then deploy it so it runs without your laptop.

---

## Run it 24/7 (pick one)

### Option A — Oracle Cloud "Always Free" VM (truly free, recommended)
1. Create an Always Free Ubuntu VM at cloud.oracle.com and SSH in.
2. Copy this folder to `/home/ubuntu/placement-sorter` (including your `.env`).
3. Run:
```bash
cd ~/placement-sorter
sudo apt update && sudo apt install -y python3-venv
python3 -m venv venv && venv/bin/pip install -r requirements.txt
sudo cp placement-sorter.service /etc/systemd/system/
sudo systemctl enable --now placement-sorter
journalctl -u placement-sorter -f      # watch the logs
```
It now starts on boot and restarts itself if it ever crashes.

### Option B — Railway / Render / any Docker host
Push this folder to a **private** GitHub repo (`.env` is not committed — add its values as environment variables in the host's dashboard). Deploy as a **background worker** using the included `Dockerfile`, and attach a volume at `/app/data`. Check the host's current pricing; most no longer offer always-on workers for free.

---

## Good to know

- **First run on a big group:** by default only the last 30 days of history are sorted (`BACKFILL_DAYS=30`), using keyword rules so the free AI quota is kept for new messages (`BACKFILL_AI=0`). Telegram lets a bot post about 20 messages a minute, so 1,000 old messages take about an hour. New messages always jump the queue.
- **Tab order:** Telegram sorts tabs by latest activity. Long-press a tab → **Pin** to keep your favourites on top.
- **Notifications:** mute 🗑 Unimportant (long-press → Mute) and keep the job tabs on.
- **Wrong tab?** Edit the category descriptions in `PROMPT` inside `classifier.py`, then restart.
- **Add / rename / remove a tab:** edit `CATEGORIES` and `PROMPT` in `classifier.py`. New tabs are created automatically on restart.
- **Deleted a tab by mistake?** It's recreated automatically on the next message for it.
- **More than one placement group?** Comma-separate them in `SOURCE_CHATS`.

## Safety

- `SESSION_STRING` gives full access to your Telegram account. Never share it, never commit `.env`, and keep the repo private. If it leaks: Telegram → Settings → Devices → terminate that session.
- The script only *reads* the placement group; everything it writes goes to your own group via the bot.
