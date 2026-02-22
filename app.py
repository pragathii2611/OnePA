#!/usr/bin/env python3
"""
OnePA Badminton Tracker — Web App
===================================
FastAPI app that:
- Serves the dashboard UI
- Runs the slot monitor in the background
- Exposes API endpoints to configure settings and view slots
- Sends email alerts when new slots open

Run:
    uvicorn app:app --host 0.0.0.0 --port 8000
"""

import asyncio
import json
import logging
import os
import smtplib
import threading
import time
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import List, Optional

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("tracker.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ─── Config file (persists settings across restarts) ─────────────────────────
CONFIG_FILE = Path("config.json")

DEFAULT_CONFIG = {
    "facility_ids": [
        "bedokcc_badmintoncourts",
        "tampinescc_badmintoncourts",
        "toaPayohcc_badmintoncourts",
    ],
    "preferred_hours": [],
    "check_interval_min": 5,
    "days_ahead": 7,
    "email_sender": os.getenv("EMAIL_SENDER", ""),
    "email_password": os.getenv("EMAIL_PASSWORD", ""),
    "email_recipients": [os.getenv("EMAIL_RECIPIENT", "")],
}

KNOWN_FACILITIES = [
    {"id": "bedokcc_badmintoncourts",       "name": "Bedok CC"},
    {"id": "tampinescc_badmintoncourts",     "name": "Tampines CC"},
    {"id": "toaPayohcc_badmintoncourts",     "name": "Toa Payoh CC"},
    {"id": "woodlandscc_badmintoncourts",    "name": "Woodlands CC"},
    {"id": "amkcc_badmintoncourts",          "name": "Ang Mo Kio CC"},
    {"id": "jurongwestcc_badmintoncourts",   "name": "Jurong West CC"},
    {"id": "clementicc_badmintoncourts",     "name": "Clementi CC"},
    {"id": "yishuncc_badmintoncourts",       "name": "Yishun CC"},
    {"id": "pasirriscc_badmintoncourts",     "name": "Pasir Ris CC"},
    {"id": "serangooncc_badmintoncourts",    "name": "Serangoon CC"},
]


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                saved = json.load(f)
            # merge with defaults so new keys always exist
            cfg = {**DEFAULT_CONFIG, **saved}
            return cfg
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()


def save_config(cfg: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


# ─── Shared state ─────────────────────────────────────────────────────────────
config = load_config()
notified_slots: set[str] = set()
latest_slots: list[dict] = []
last_checked: Optional[str] = None
monitor_running = False

# ─── OnePA API ────────────────────────────────────────────────────────────────
ONEPA_BASE = "https://www.onepa.gov.sg"
AVAIL_API  = f"{ONEPA_BASE}/pacesapi/facility/getavailability"
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-SG,en;q=0.9",
    "Referer": f"{ONEPA_BASE}/facilities/availability",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "X-Requested-With": "XMLHttpRequest",
}


def pretty_name(facility_id: str) -> str:
    for f in KNOWN_FACILITIES:
        if f["id"] == facility_id:
            return f["name"]
    name = facility_id.split("_")[0].replace("cc", " CC").title()
    return name


def fetch_slots(facility_id: str, date_str: str) -> list[dict]:
    try:
        resp = requests.get(
            AVAIL_API,
            params={"facilityId": facility_id, "selectedDate": date_str},
            headers=HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("response", {}).get("listSlot", [])
    except Exception as exc:
        log.warning(f"  ↳ [{facility_id} / {date_str}]: {exc}")
        return []


def is_preferred(start_time: str, preferred_hours: list[int]) -> bool:
    if not preferred_hours:
        return True
    try:
        return int(start_time.split(":")[0]) in preferred_hours
    except (ValueError, IndexError):
        return True


# ─── Monitor loop ─────────────────────────────────────────────────────────────

def run_check():
    global latest_slots, last_checked, config
    config = load_config()  # reload in case settings changed

    log.info(f"🔍 Checking {len(config['facility_ids'])} facility/ies for {config['days_ahead']} days…")
    new_slots = []
    today = datetime.now()

    for fid in config["facility_ids"]:
        for delta in range(config["days_ahead"]):
            target = today + timedelta(days=delta)
            date_str = target.strftime("%d/%m/%Y")
            raw = fetch_slots(fid, date_str)

            for slot in raw:
                if not slot.get("isAvailable", False):
                    continue
                start = slot.get("startTime", "")
                end   = slot.get("endTime", "")
                if not is_preferred(start, config["preferred_hours"]):
                    continue

                court = str(slot.get("resourceId", "") or slot.get("courtNumber", "") or "?")
                key   = f"{fid}|{date_str}|{court}|{start}"

                if key in notified_slots:
                    continue

                new_slots.append({
                    "facility_id":   fid,
                    "facility_name": pretty_name(fid),
                    "date":          date_str,
                    "day":           target.strftime("%A"),
                    "court":         court,
                    "start":         start,
                    "end":           end,
                    "is_peak":       slot.get("isPeak", False),
                    "key":           key,
                    "booking_url":   f"{ONEPA_BASE}/facilities/availability?facilityId={fid}",
                })

    last_checked = datetime.now().isoformat()

    if new_slots:
        log.info(f"🎉 {len(new_slots)} new slot(s) found!")
        send_email(new_slots)
        for s in new_slots:
            notified_slots.add(s["key"])
        latest_slots = new_slots
    else:
        log.info("😴 No new slots this round.")

    # Save for persistence
    with open("latest_slots.json", "w") as f:
        json.dump({"checked_at": last_checked, "slots": latest_slots}, f, indent=2)


def monitor_loop():
    global monitor_running
    monitor_running = True
    log.info("Monitor thread started.")
    run_check()
    while True:
        interval = config.get("check_interval_min", 5) * 60
        time.sleep(interval)
        run_check()


# ─── Email ────────────────────────────────────────────────────────────────────

def send_email(slots: list[dict]):
    cfg = load_config()
    sender    = cfg.get("email_sender", "")
    password  = cfg.get("email_password", "")
    recipients = [r for r in cfg.get("email_recipients", []) if r.strip()]

    if not (sender and password and recipients):
        log.warning("Email not configured — skipping notification.")
        return

    rows = ""
    for s in slots:
        badge = (
            '<span style="background:#fff3ed;color:#c2410c;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600;">PEAK</span>'
            if s["is_peak"] else
            '<span style="background:#f0fdf4;color:#15803d;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600;">OFF-PEAK</span>'
        )
        rows += f"""
        <tr>
          <td style="padding:11px 14px;border-bottom:1px solid #f0f0f0">{s['facility_name']}</td>
          <td style="padding:11px 14px;border-bottom:1px solid #f0f0f0">{s['day']}, {s['date']}</td>
          <td style="padding:11px 14px;border-bottom:1px solid #f0f0f0;font-family:monospace">{s['start']} – {s['end']}</td>
          <td style="padding:11px 14px;border-bottom:1px solid #f0f0f0">Court {s['court']}</td>
          <td style="padding:11px 14px;border-bottom:1px solid #f0f0f0">{badge}</td>
          <td style="padding:11px 14px;border-bottom:1px solid #f0f0f0">
            <a href="{s['booking_url']}" style="background:#2563eb;color:#fff;padding:6px 14px;border-radius:6px;text-decoration:none;font-size:12px;font-weight:600;">Book Now →</a>
          </td>
        </tr>"""

    html = f"""
    <html><body style="font-family:-apple-system,sans-serif;background:#f5f5f5;margin:0;padding:0;">
      <div style="max-width:680px;margin:30px auto;background:#fff;border-radius:12px;border:1px solid #e8e8e8;overflow:hidden;">
        <div style="background:#2563eb;padding:26px 32px;color:#fff;">
          <div style="font-size:22px;margin-bottom:6px;">🏸 Badminton Slots Available!</div>
          <div style="font-size:14px;opacity:.85;">Found <strong>{len(slots)}</strong> open slot(s) — book before they're gone.</div>
        </div>
        <div style="padding:24px 32px;">
          <table width="100%" cellspacing="0" style="border-collapse:collapse;font-size:13.5px;">
            <thead>
              <tr style="background:#fafafa;border-bottom:1px solid #e8e8e8;">
                <th style="padding:10px 14px;text-align:left;font-size:11px;color:#999;text-transform:uppercase;">CC</th>
                <th style="padding:10px 14px;text-align:left;font-size:11px;color:#999;text-transform:uppercase;">Date</th>
                <th style="padding:10px 14px;text-align:left;font-size:11px;color:#999;text-transform:uppercase;">Time</th>
                <th style="padding:10px 14px;text-align:left;font-size:11px;color:#999;text-transform:uppercase;">Court</th>
                <th style="padding:10px 14px;text-align:left;font-size:11px;color:#999;text-transform:uppercase;">Rate</th>
                <th style="padding:10px 14px;text-align:left;font-size:11px;color:#999;text-transform:uppercase;">Action</th>
              </tr>
            </thead>
            <tbody>{rows}</tbody>
          </table>
        </div>
        <div style="padding:14px 32px;background:#fafafa;border-top:1px solid #f0f0f0;font-size:11px;color:#bbb;text-align:center;">
          OnePA Badminton Tracker • {datetime.now().strftime('%d %b %Y, %H:%M')}
        </div>
      </div>
    </body></html>"""

    plain = f"Found {len(slots)} slot(s):\n\n"
    for s in slots:
        plain += f"• {s['facility_name']} | {s['day']} {s['date']} | {s['start']}–{s['end']} | Court {s['court']}\n  {s['booking_url']}\n\n"

    for recipient in recipients:
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = f"🏸 {len(slots)} Badminton Slot(s) Open on OnePA!"
            msg["From"]    = sender
            msg["To"]      = recipient
            msg.attach(MIMEText(plain, "plain"))
            msg.attach(MIMEText(html, "html"))

            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
                smtp.login(sender, password)
                smtp.sendmail(sender, recipient, msg.as_string())
            log.info(f"✉  Email sent → {recipient}")
        except Exception as exc:
            log.error(f"Email failed for {recipient}: {exc}")


# ─── FastAPI app ──────────────────────────────────────────────────────────────
app = FastAPI(title="OnePA Badminton Tracker")


@app.on_event("startup")
async def startup():
    # Load any previously found slots
    global latest_slots, last_checked
    if Path("latest_slots.json").exists():
        try:
            with open("latest_slots.json") as f:
                data = json.load(f)
                latest_slots = data.get("slots", [])
                last_checked = data.get("checked_at")
        except Exception:
            pass
    # Start monitor in background thread
    t = threading.Thread(target=monitor_loop, daemon=True)
    t.start()


# ─── API routes ───────────────────────────────────────────────────────────────

class ConfigUpdate(BaseModel):
    facility_ids: Optional[List[str]] = None
    preferred_hours: Optional[List[int]] = None
    check_interval_min: Optional[int] = None
    days_ahead: Optional[int] = None
    email_sender: Optional[str] = None
    email_password: Optional[str] = None
    email_recipients: Optional[List[str]] = None


@app.get("/api/slots")
def get_slots():
    return {
        "checked_at": last_checked,
        "slots": latest_slots,
        "monitor_running": monitor_running,
    }


@app.get("/api/config")
def get_config():
    cfg = load_config()
    # Don't expose password in UI
    cfg["email_password"] = "••••••••••••••••" if cfg.get("email_password") else ""
    return cfg


@app.post("/api/config")
def update_config(update: ConfigUpdate):
    cfg = load_config()
    if update.facility_ids is not None:
        cfg["facility_ids"] = update.facility_ids
    if update.preferred_hours is not None:
        cfg["preferred_hours"] = update.preferred_hours
    if update.check_interval_min is not None:
        cfg["check_interval_min"] = max(1, update.check_interval_min)
    if update.days_ahead is not None:
        cfg["days_ahead"] = min(15, max(1, update.days_ahead))
    if update.email_sender is not None:
        cfg["email_sender"] = update.email_sender
    if update.email_password is not None and update.email_password != "••••••••••••••••":
        cfg["email_password"] = update.email_password
    if update.email_recipients is not None:
        cfg["email_recipients"] = [r.strip() for r in update.email_recipients if r.strip()]
    save_config(cfg)
    return {"ok": True, "config": {**cfg, "email_password": "••••••••••••••••"}}


@app.get("/api/facilities")
def get_facilities():
    return KNOWN_FACILITIES


@app.post("/api/check-now")
def check_now():
    """Trigger an immediate check (runs in background)."""
    t = threading.Thread(target=run_check, daemon=True)
    t.start()
    return {"ok": True, "message": "Check triggered"}


@app.post("/api/test-email")
def test_email():
    """Send a test email to verify credentials."""
    cfg = load_config()
    sender   = cfg.get("email_sender", "")
    password = cfg.get("email_password", "")
    recipients = [r for r in cfg.get("email_recipients", []) if r.strip()]

    if not (sender and password and recipients):
        raise HTTPException(400, "Email not fully configured")

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "🏸 OnePA Tracker — Test Email"
        msg["From"]    = sender
        msg["To"]      = recipients[0]
        msg.attach(MIMEText("Your OnePA Badminton Tracker is set up correctly!", "plain"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(sender, password)
            smtp.sendmail(sender, recipients[0], msg.as_string())
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(400, str(exc))


# ─── Serve the dashboard HTML ─────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def dashboard():
    with open("templates/index.html") as f:
        return f.read()
