# 🏸 OnePA Badminton Court Slot Tracker

Automatically monitors [onepa.gov.sg](https://www.onepa.gov.sg) for available badminton court slots and **emails you instantly** when one opens up.

---

## Features
- 📡 **Polls the OnePA API** every N minutes (configurable)
- ✉️ **Email alerts** with a beautiful HTML table + direct "Book Now" links
- 🕐 **Time-of-day filtering** — only get alerted for evenings, weekends, etc.
- 🏟️ **Multi-facility** — watch multiple Community Clubs at once
- 📋 **Live dashboard** — open `dashboard.html` in your browser for a visual overview

---

## Quick Start

### 1 — Install Python dependencies
```bash
pip install -r requirements.txt
```

### 2 — Configure
```bash
cp .env.example .env
```
Edit `.env` and fill in:

| Variable | Description |
|---|---|
| `EMAIL_SENDER` | Your Gmail address |
| `EMAIL_PASSWORD` | Gmail **App Password** (see below) |
| `EMAIL_RECIPIENT` | Where alerts are sent (can be same) |
| `FACILITY_IDS` | Comma-separated facility IDs from OnePA |
| `PREFERRED_HOURS` | Hours to alert for (e.g. `18,19,20,21,22`) |
| `CHECK_INTERVAL_MIN` | How often to poll (default `5`) |
| `DAYS_AHEAD` | Days to scan ahead (default `7`, max `15`) |

### 3 — Get a Gmail App Password
1. Go to [myaccount.google.com](https://myaccount.google.com)
2. Security → 2-Step Verification → **App passwords**
3. Create one called "OnePA Tracker"
4. Paste the 16-character password into `.env`

### 4 — Find your Facility IDs
Browse to: `https://www.onepa.gov.sg/facilities/availability?facilityId=bedokcc_badmintoncourts`

The bit after `facilityId=` is the ID. Examples:
- `bedokcc_badmintoncourts`
- `tampinescc_badmintoncourts`
- `toaPayohcc_badmintoncourts`
- `woodlandscc_badmintoncourts`
- `amkcc_badmintoncourts`
- `jurongwestcc_badmintoncourts`

### 5 — Run
```bash
python monitor.py
```

The monitor will:
- Check immediately on launch
- Then check every `CHECK_INTERVAL_MIN` minutes
- Email you whenever new preferred slots appear
- Write `latest_slots.json` so the dashboard can show live data

### 6 — View the Dashboard (optional)
Open `dashboard.html` in your browser. It auto-refreshes every 60 seconds from `latest_slots.json`.

---

## Pro Tips

### 🕙 New slots open at 10 PM daily
OnePA releases the booking window for +15 days each night at 10 PM. Run with a short interval around then:
```bash
CHECK_INTERVAL_MIN=1 python monitor.py
```

### 🖥 Keep it running overnight (Linux/Mac)
```bash
nohup python monitor.py > monitor.log 2>&1 &
```

### 🪟 Windows Task Scheduler
Set up a scheduled task to run `python monitor.py` at startup so it always runs in the background.

### ☁️ Deploy to the cloud (free)
- **Railway / Render** — push this folder as a repo, set env vars, deploy
- **PythonAnywhere** — free tier works great for this kind of polling script

---

## How it Works

```
Every N minutes:
  For each facility × each of the next X days:
    → Call OnePA API: /pacesapi/facility/getavailability
    → Filter for available + preferred-hour slots
    → Any new slot not yet notified?
        → Send email
        → Mark as notified (won't spam you)
```

---

## File Structure
```
onepa_tracker/
├── monitor.py        ← Main script (run this)
├── .env.example      ← Config template
├── .env              ← Your actual config (git-ignored)
├── requirements.txt  ← Python deps
├── dashboard.html    ← Live visual dashboard
├── latest_slots.json ← Written by monitor, read by dashboard
└── tracker.log       ← Log file
```

---

*This tool makes read-only API calls to onePA.gov.sg in a non-intrusive manner. It does not automate booking and is intended to assist in finding available slots.*
