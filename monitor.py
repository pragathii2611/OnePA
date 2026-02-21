#!/usr/bin/env python3
"""
OnePA Badminton Court Slot Tracker
====================================
Monitors onePA.gov.sg for available badminton court slots
and sends email notifications when slots open up.

Usage:
    python monitor.py

Requirements:
    pip install requests schedule python-dotenv

Setup:
    1. Copy .env.example to .env and fill in your details
    2. Run: python monitor.py
"""

import requests
import schedule
import time
import smtplib
import json
import logging
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("tracker.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ─── Configuration (loaded from .env) ───────────────────────────────────────
EMAIL_SENDER       = os.getenv("EMAIL_SENDER")          # e.g. yourname@gmail.com
EMAIL_PASSWORD     = os.getenv("EMAIL_PASSWORD")        # Gmail App Password
EMAIL_RECIPIENT    = os.getenv("EMAIL_RECIPIENT")       # where to send alerts
CHECK_INTERVAL_MIN = int(os.getenv("CHECK_INTERVAL_MIN", "5"))   # how often to poll (minutes)
DAYS_AHEAD         = int(os.getenv("DAYS_AHEAD", "7"))           # how many days to scan ahead

# Which CCs to monitor — add/remove as you like
# Full list: https://www.onepa.gov.sg/facilities
FACILITY_IDS = os.getenv("FACILITY_IDS", (
    "bedokcc_badmintoncourts,"
    "tampinescc_badmintoncourts,"
    "toaPayohcc_badmintoncourts,"
    "woodlandscc_badmintoncourts,"
    "jurongwestcc_badmintoncourts"
)).split(",")

# Only alert for these time windows (24-hr format, inclusive). Leave empty [] to get all.
PREFERRED_HOURS = os.getenv("PREFERRED_HOURS", "18,19,20,21,22")  # e.g. evenings
PREFERRED_HOURS = [int(h) for h in PREFERRED_HOURS.split(",") if h.strip()]

# ─── OnePA API ───────────────────────────────────────────────────────────────
ONEPA_BASE    = "https://www.onepa.gov.sg"
AVAIL_API     = f"{ONEPA_BASE}/pacesapi/facility/getavailability"
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": "https://www.onepa.gov.sg/facilities/availability",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "X-Requested-With": "XMLHttpRequest",
}

# ─── State — track what we've already notified about ────────────────────────
notified_slots: set[str] = set()


def fetch_availability(facility_id: str, date_str: str) -> list[dict]:
    """Call the OnePA availability API for a given facility and date."""
    params = {
        "facilityId": facility_id,
        "selectedDate": date_str,   # format: DD/MM/YYYY
    }
    try:
        resp = requests.get(AVAIL_API, params=params, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        # The API returns a nested structure; slots live under data.response.listSlot
        return data.get("response", {}).get("listSlot", [])
    except requests.RequestException as e:
        log.warning(f"Request failed for {facility_id} on {date_str}: {e}")
        return []
    except (ValueError, KeyError) as e:
        log.warning(f"Unexpected API response for {facility_id} on {date_str}: {e}")
        return []


def is_preferred(slot_time: str) -> bool:
    """Return True if the slot falls within our preferred hours."""
    if not PREFERRED_HOURS:
        return True
    try:
        hour = int(slot_time.split(":")[0])
        return hour in PREFERRED_HOURS
    except (ValueError, IndexError):
        return True


def pretty_facility(facility_id: str) -> str:
    return facility_id.replace("_", " ").replace("cc", " CC").title()


def check_slots() -> list[dict]:
    """Scan all configured facilities for the next DAYS_AHEAD days."""
    found = []
    today = datetime.now()

    for facility_id in FACILITY_IDS:
        facility_id = facility_id.strip()
        if not facility_id:
            continue
        for delta in range(DAYS_AHEAD):
            check_date = today + timedelta(days=delta)
            date_str = check_date.strftime("%d/%m/%Y")

            slots = fetch_availability(facility_id, date_str)

            for slot in slots:
                # Slot structure (typical): {startTime, endTime, isAvailable, isPeak, courtNumber, ...}
                if not slot.get("isAvailable", False):
                    continue

                start = slot.get("startTime", "")
                end   = slot.get("endTime", "")
                court = slot.get("resourceId") or slot.get("courtNumber") or "?"

                if not is_preferred(start):
                    continue

                slot_key = f"{facility_id}|{date_str}|{court}|{start}"
                if slot_key in notified_slots:
                    continue

                found.append({
                    "facility_id":   facility_id,
                    "facility_name": pretty_facility(facility_id),
                    "date":          date_str,
                    "day":           check_date.strftime("%A"),
                    "court":         court,
                    "start":         start,
                    "end":           end,
                    "is_peak":       slot.get("isPeak", False),
                    "key":           slot_key,
                    "booking_url":   f"{ONEPA_BASE}/facilities/availability?facilityId={facility_id}",
                })
    return found


def build_email_html(slots: list[dict]) -> str:
    rows = ""
    for s in slots:
        peak_badge = (
            '<span style="background:#ff6b35;color:#fff;padding:2px 6px;border-radius:9px;font-size:11px;">Peak</span>'
            if s["is_peak"] else
            '<span style="background:#4caf50;color:#fff;padding:2px 6px;border-radius:9px;font-size:11px;">Off-peak</span>'
        )
        rows += f"""
        <tr>
          <td style="padding:10px 8px;border-bottom:1px solid #eee;">{s['facility_name']}</td>
          <td style="padding:10px 8px;border-bottom:1px solid #eee;">{s['day']}, {s['date']}</td>
          <td style="padding:10px 8px;border-bottom:1px solid #eee;">{s['start']} – {s['end']}</td>
          <td style="padding:10px 8px;border-bottom:1px solid #eee;">Court {s['court']}</td>
          <td style="padding:10px 8px;border-bottom:1px solid #eee;">{peak_badge}</td>
          <td style="padding:10px 8px;border-bottom:1px solid #eee;">
            <a href="{s['booking_url']}" style="background:#1a73e8;color:#fff;padding:6px 14px;border-radius:6px;text-decoration:none;font-size:13px;">Book Now →</a>
          </td>
        </tr>"""

    return f"""
    <html><body style="font-family:Arial,sans-serif;background:#f5f7fa;margin:0;padding:0;">
    <div style="max-width:700px;margin:30px auto;background:#fff;border-radius:12px;box-shadow:0 2px 16px rgba(0,0,0,.1);overflow:hidden;">
      <div style="background:linear-gradient(135deg,#1a73e8,#0d47a1);padding:28px 32px;color:#fff;">
        <h1 style="margin:0;font-size:22px;">🏸 Badminton Slots Available!</h1>
        <p style="margin:6px 0 0;opacity:.85;font-size:14px;">
          Found {len(slots)} open slot(s) on onePA — book before they're gone!
        </p>
      </div>
      <div style="padding:24px 32px;">
        <table width="100%" cellspacing="0" style="border-collapse:collapse;font-size:14px;">
          <thead>
            <tr style="background:#f0f4ff;">
              <th style="padding:10px 8px;text-align:left;color:#555;">Community Club</th>
              <th style="padding:10px 8px;text-align:left;color:#555;">Date</th>
              <th style="padding:10px 8px;text-align:left;color:#555;">Time</th>
              <th style="padding:10px 8px;text-align:left;color:#555;">Court</th>
              <th style="padding:10px 8px;text-align:left;color:#555;">Rate</th>
              <th style="padding:10px 8px;text-align:left;color:#555;">Action</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
      <div style="padding:16px 32px;background:#f8f9fa;font-size:12px;color:#999;text-align:center;">
        Sent by your OnePA Badminton Tracker • {datetime.now().strftime('%d %b %Y %H:%M')}
      </div>
    </div></body></html>"""


def send_email(slots: list[dict]):
    if not EMAIL_SENDER or not EMAIL_PASSWORD or not EMAIL_RECIPIENT:
        log.error("Email credentials not configured. Slots found but not notified:")
        for s in slots:
            log.info(f"  {s['facility_name']} | {s['date']} | {s['start']}–{s['end']} | Court {s['court']}")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🏸 {len(slots)} Badminton Slot(s) Open on OnePA — Book Now!"
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = EMAIL_RECIPIENT

    # Plain text fallback
    plain = f"Found {len(slots)} available badminton slot(s):\n\n"
    for s in slots:
        plain += f"• {s['facility_name']} | {s['day']} {s['date']} | {s['start']}–{s['end']} | Court {s['court']}\n  Book: {s['booking_url']}\n\n"

    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(build_email_html(slots), "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(EMAIL_SENDER, EMAIL_PASSWORD)
            smtp.sendmail(EMAIL_SENDER, EMAIL_RECIPIENT, msg.as_string())
        log.info(f"✉️  Email sent to {EMAIL_RECIPIENT} with {len(slots)} slot(s).")
    except smtplib.SMTPException as e:
        log.error(f"Failed to send email: {e}")


def run_check():
    log.info(f"🔍 Checking {len(FACILITY_IDS)} facilities for the next {DAYS_AHEAD} days…")
    new_slots = check_slots()

    if new_slots:
        log.info(f"🎉 Found {len(new_slots)} new slot(s)! Sending notification…")
        send_email(new_slots)
        for s in new_slots:
            notified_slots.add(s["key"])
        # Save found slots to JSON for the dashboard
        with open("latest_slots.json", "w") as f:
            json.dump({
                "checked_at": datetime.now().isoformat(),
                "slots": new_slots
            }, f, indent=2)
    else:
        log.info("😴 No new preferred slots found this round.")
        # Update the dashboard with empty result
        existing = []
        if os.path.exists("latest_slots.json"):
            try:
                with open("latest_slots.json") as f:
                    existing = json.load(f).get("slots", [])
            except Exception:
                pass
        with open("latest_slots.json", "w") as f:
            json.dump({
                "checked_at": datetime.now().isoformat(),
                "slots": existing   # keep last known slots visible
            }, f, indent=2)


def main():
    log.info("=" * 60)
    log.info("  OnePA Badminton Slot Tracker — Starting Up")
    log.info("=" * 60)
    log.info(f"Monitoring facilities : {', '.join(FACILITY_IDS)}")
    log.info(f"Preferred hours       : {PREFERRED_HOURS or 'All hours'}")
    log.info(f"Check interval        : every {CHECK_INTERVAL_MIN} minute(s)")
    log.info(f"Days to scan ahead    : {DAYS_AHEAD}")
    log.info("")

    # Run immediately on start, then on schedule
    run_check()
    schedule.every(CHECK_INTERVAL_MIN).minutes.do(run_check)

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
