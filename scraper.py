import time
import os
import smtplib
import logging
import base64
import re
import random
import requests as req
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
from PIL import Image

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium_stealth import stealth
from openai import OpenAI

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

# Anti-spam state
notified_slots: set = set()
last_notified:  dict = {}

# OnePA API
ONEPA_BASE = "https://www.onepa.gov.sg"
AVAIL_API  = f"{ONEPA_BASE}/pacesapi/facility/getavailability"
API_HEADERS = {
    "Accept":           "application/json, text/plain, */*",
    "Accept-Language":  "en-SG,en;q=0.9",
    "Referer":          f"{ONEPA_BASE}/facilities/availability",
    "User-Agent":       "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "X-Requested-With": "XMLHttpRequest",
}


# ── METHOD 1: Raw API ─────────────────────────────────────────────────────────

def api_fetch_slots(facility_id: str, date_str: str):
    try:
        resp = req.get(
            AVAIL_API,
            params={"facilityId": facility_id, "selectedDate": date_str},
            headers=API_HEADERS,
            timeout=8,
        )
        resp.raise_for_status()
        data   = resp.json()
        slots  = data.get("response", {}).get("listSlot", [])
        logger.info(f"  API OK [{facility_id} / {date_str}] — {len(slots)} total")
        return slots
    except Exception as exc:
        logger.info(f"  API blocked [{facility_id} / {date_str}]: {exc}")
        return None


def parse_api_slots(raw_slots: list) -> list:
    return [s.get("startTime", "") for s in raw_slots if s.get("isAvailable") and s.get("startTime")]


# ── METHOD 2: Playwright + GPT-4o ────────────────────────────────────────────

def setup_driver():
    options = webdriver.ChromeOptions()
    options.add_argument("--headless")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    stealth(driver, languages=["en-US", "en"], vendor="Google Inc.",
            platform="Win32", webgl_vendor="Intel Inc.",
            renderer="Intel Iris OpenGL Engine", fix_hairline=True)
    return driver


def human_scroll(driver):
    total = int(driver.execute_script("return document.body.scrollHeight"))
    for i in range(1, total, random.randint(300, 500)):
        driver.execute_script(f"window.scrollTo(0, {i});")
        time.sleep(random.uniform(0.05, 0.2))
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")


def crop_image(path):
    try:
        img = Image.open(path)
        w, h = img.size
        img  = img.crop((0, 250, w, max(350, h - 200)))
        out  = "scan_cropped.png"
        img.save(out)
        return out
    except Exception:
        return path


def encode_image(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def gpt_analyze(image_path, facility_name, date_str) -> list:
    try:
        b64    = encode_image(crop_image(image_path))
        prompt = """You are analysing a screenshot of the OnePA badminton court booking page.

Each time slot cell is either:
- AVAILABLE: bright white/light background, no X, no grey fill, not labelled Booked/N/A/Closed
- UNAVAILABLE: grey/dimmed, crossed out, has X, labelled Booked/N/A/Closed

YOUR TASK:
1. Look at every time slot cell carefully.
2. Return ONLY times of TRULY AVAILABLE (white/bright) cells.
3. If ALL slots are grey/booked return [].
4. Be strict — when in doubt do NOT include the slot.

Return ONLY a Python list of time strings. Example: ['7:00 PM', '8:00 PM']
If nothing available return exactly: []"""

        resp    = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
            ]}],
            max_tokens=400,
        )
        content = resp.choices[0].message.content.strip()
        logger.info(f"  GPT-4o [{facility_name} / {date_str}]: {content}")
        times   = re.findall(r'\d{1,2}:\d{2}\s*(?:AM|PM)', content, re.IGNORECASE)
        return sorted(set(times))
    except Exception as e:
        logger.error(f"  GPT-4o error: {e}")
        return []


def playwright_fetch_slots(driver, base_url, date_str, facility_name) -> list:
    try:
        parsed = urlparse(base_url)
        params = parse_qs(parsed.query)
        params["date"] = [date_str]
        url = urlunparse(parsed._replace(query=urlencode(params, doseq=True)))

        driver.get(url)
        time.sleep(random.uniform(4, 6))
        human_scroll(driver)
        time.sleep(1.5)

        total_h = driver.execute_script("return document.body.scrollHeight")
        driver.set_window_size(1920, total_h + 200)
        path = f"scan_{date_str.replace('/', '-')}.png"
        driver.save_screenshot(path)
        return gpt_analyze(path, facility_name, date_str)
    except Exception as e:
        logger.error(f"  Playwright error [{facility_name} / {date_str}]: {e}")
        return []


# ── Email ─────────────────────────────────────────────────────────────────────

def send_notification(facility_url, facility_name, date_str, recipient_email):
    sender_email    = os.getenv("SENDER_EMAIL")
    sender_password = os.getenv("SENDER_PASSWORD")
    if not sender_email or not sender_password:
        logger.warning("Email credentials missing")
        return

    body = f"""Hi,

Badminton court slots are available at {facility_name} on {date_str}.

Book here before they're gone:
{facility_url}

— Court Hunter
"""
    msg = MIMEMultipart()
    msg["From"]    = f"Court Hunter <{sender_email}>"
    msg["To"]      = recipient_email
    msg["Subject"] = f"🏸 Slots open at {facility_name} — {date_str}"
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(sender_email, sender_password)
            server.send_message(msg)
        logger.info(f"📧 Email sent → {recipient_email} ({facility_name} / {date_str})")
    except Exception as e:
        logger.error(f"Email failed: {e}")


# ── Dedup + cooldown ──────────────────────────────────────────────────────────

def notify_if_new(available_times, facility_url, facility_name, date_str, recipient_email) -> int:
    new_times = []
    for t in available_times:
        key = f"{facility_url}|{date_str}|{t}"
        if key not in notified_slots:
            new_times.append(t)
            notified_slots.add(key)

    if not new_times:
        return 0

    now  = datetime.now()
    last = last_notified.get(facility_url)
    if last and (now - last).seconds < 3600:
        logger.info(f"  ⏳ Cooldown active for {facility_name}, skipping email")
    else:
        send_notification(facility_url, facility_name, date_str, recipient_email)
        last_notified[facility_url] = now

    return len(new_times)


# ── Peak window check ─────────────────────────────────────────────────────────

def is_peak_window() -> bool:
    from pytz import timezone
    sg_now = datetime.now(timezone('Asia/Singapore'))
    return (sg_now.hour == 21 and sg_now.minute >= 55) or \
           (sg_now.hour == 22 and sg_now.minute <= 10)


# ── Main ──────────────────────────────────────────────────────────────────────

def check_facility_availability(base_url: str, recipient_email: str) -> int:
    fid           = base_url.split("facilityId=")[-1].split("&")[0] if "facilityId=" in base_url else "Unknown"
    facility_name = fid.replace("_", " ").replace("cc", " CC").title()
    total_found   = 0
    today         = datetime.now()
    peak_window   = is_peak_window()

    logger.info(f"🚀 Scanning {facility_name} ({'PEAK WINDOW ⚡' if peak_window else 'passive'})…")

    # Test API on first date
    test_date  = today.strftime("%d/%m/%Y")
    test_slots = api_fetch_slots(fid, test_date)
    use_api    = test_slots is not None

    logger.info(f"  Mode: {'⚡ API (fast)' if use_api else '🌐 Playwright + GPT-4o (fallback)'}")

    driver = None
    if not use_api:
        try:
            driver = setup_driver()
        except Exception as e:
            logger.error(f"  Browser setup failed: {e}")
            return 0

    try:
        for day_offset in range(15):
            target_date = today + timedelta(days=day_offset)
            date_str    = target_date.strftime("%d/%m/%Y")
            logger.info(f"  📅 Day {day_offset + 1}/15: {date_str}")

            if use_api:
                raw = test_slots if day_offset == 0 else api_fetch_slots(fid, date_str)

                # API got blocked mid-scan — switch to Playwright
                if raw is None:
                    logger.info(f"  API blocked mid-scan, switching to Playwright")
                    use_api = False
                    if driver is None:
                        driver = setup_driver()
                    times = playwright_fetch_slots(driver, base_url, date_str, facility_name)
                    total_found += notify_if_new(times, base_url, facility_name, date_str, recipient_email)
                    time.sleep(random.uniform(3, 5))
                    continue

                times = parse_api_slots(raw)
                if times:
                    logger.info(f"  🎉 Available: {times}")
                else:
                    logger.info(f"  ❌ Fully booked")

                total_found += notify_if_new(times, base_url, facility_name, date_str, recipient_email)
                # Small delay — faster during peak window
                time.sleep(0.3 if peak_window else 1.0)

            else:
                times = playwright_fetch_slots(driver, base_url, date_str, facility_name)
                total_found += notify_if_new(times, base_url, facility_name, date_str, recipient_email)
                time.sleep(random.uniform(2, 4))

    finally:
        if driver:
            driver.quit()

    logger.info(f"🏁 {facility_name} done. New slots: {total_found}")
    return total_found