
#!/usr/bin/env python3
# NEXUS appointment watcher — Blaine, WA (locationId=5020)
# - Checks the public TTP scheduler API for new Blaine NEXUS slots
# - Supports INCLUDE_START/INCLUDE_END (include-only) or EXCLUDE_START/EXCLUDE_END date filters (YYYY-MM-DD)
# - Optional notifications:
#     * WhatsApp via Twilio (recommended for instant phone alerts)
#     * Telegram bot
#     * Email via SMTP
# - Designed to run once per execution (use a scheduler like GitHub Actions on a 5-min cron)
import json
import os
import smtplib
import sys
from datetime import datetime, timezone, date
from email.mime.text import MIMEText
from pathlib import Path
from typing import List, Dict
import urllib.parse
import urllib.request

# ---------- CONFIG / ENV ----------
LOCATION_ID = int(os.getenv("LOCATION_ID", "5020"))  # Blaine NEXUS default
POLL_LIMIT = int(os.getenv("POLL_LIMIT", "20"))

# Date filters
INCLUDE_START = os.getenv("INCLUDE_START", None)
INCLUDE_END   = os.getenv("INCLUDE_END", None)
EXCLUDE_START = os.getenv("EXCLUDE_START", None)
EXCLUDE_END   = os.getenv("EXCLUDE_END", None)

# Telegram (optional)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# Email via SMTP (optional)
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
EMAIL_TO  = os.getenv("EMAIL_TO", "")

# Twilio WhatsApp (optional)
TWILIO_SID   = os.getenv("TWILIO_SID", "")
TWILIO_TOKEN = os.getenv("TWILIO_TOKEN", "")
WHATSAPP_FROM = os.getenv("WHATSAPP_FROM", "")  # e.g., "whatsapp:+14155238886"
WHATSAPP_TO   = os.getenv("WHATSAPP_TO", "")    # e.g., "whatsapp:+1XXXXXXXXXX"

# Persistent state file to avoid duplicate alerts
STATE_FILE = os.getenv("STATE_FILE", str(Path.home() / ".nexus_blaine_state.json"))
# ---------- END CONFIG ----------

API_URL = "https://ttp.cbp.dhs.gov/schedulerapi/slots?orderBy=soonest&limit={limit}&locationId={loc}&minimum=1"
SCHEDULER_UI = "https://ttp.cbp.dhs.gov/schedulerui/schedule-interview/location?service=NH&vo=true"

def parse_iso(dt_str: str) -> datetime:
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))

def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()

def passes_date_filters(d: date) -> bool:
    # Include-only takes precedence
    if INCLUDE_START and INCLUDE_END:
        try:
            s = _parse_date(INCLUDE_START)
            e = _parse_date(INCLUDE_END)
        except ValueError:
            pass
        else:
            if s > e:
                s, e = e, s
            return s <= d <= e

    # Otherwise, apply exclude window
    if EXCLUDE_START and EXCLUDE_END:
        try:
            s = _parse_date(EXCLUDE_START)
            e = _parse_date(EXCLUDE_END)
        except ValueError:
            return True
        if s > e:
            s, e = e, s
        return not (s <= d <= e)

    return True

def load_state() -> Dict:
    p = Path(STATE_FILE)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}

def save_state(state: Dict) -> None:
    try:
        Path(STATE_FILE).write_text(json.dumps(state, indent=2))
    except Exception as e:
        print(f"[warn] could not save state: {e}", file=sys.stderr)

def http_get_json(url: str):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; NexusWatcher/1.2)",
        "Accept": "application/json, text/plain, */*",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))

def notify(message: str) -> None:
    sent = False

    # WhatsApp via Twilio
    if TWILIO_SID and TWILIO_TOKEN and WHATSAPP_FROM and WHATSAPP_TO:
        try:
            from twilio.rest import Client
            client = Client(TWILIO_SID, TWILIO_TOKEN)
            client.messages.create(from_=WHATSAPP_FROM, to=WHATSAPP_TO, body=message)
            print("[ok] WhatsApp notification sent via Twilio.")
            sent = True
        except Exception as e:
            print(f"[warn] WhatsApp send failed: {e}", file=sys.stderr)

    # Telegram
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            data = urllib.parse.urlencode({
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "disable_web_page_preview": "true",
            }).encode("utf-8")
            tg_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            with urllib.request.urlopen(urllib.request.Request(tg_url, data=data), timeout=15) as _:
                pass
            print("[ok] Telegram notification sent.")
            sent = True
        except Exception as e:
            print(f"[warn] Telegram send failed: {e}", file=sys.stderr)

    # Email
    if SMTP_HOST and SMTP_USER and SMTP_PASS and EMAIL_TO:
        try:
            msg = MIMEText(message, "plain", "utf-8")
            msg["Subject"] = "NEXUS Blaine: New Appointment"
            msg["From"] = SMTP_USER
            msg["To"] = EMAIL_TO
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
                s.starttls()
                s.login(SMTP_USER, SMTP_PASS)
                s.sendmail(SMTP_USER, [EMAIL_TO], msg.as_string())
            print("[ok] Email notification sent.")
            sent = True
        except Exception as e:
            print(f"[warn] Email send failed: {e}", file=sys.stderr)

    print(message)
    if not sent:
        print("[info] no notifier configured; message printed only.")

def main() -> int:
    state = load_state()
    last_seen = state.get("last_seen_iso")

    url = API_URL.format(limit=POLL_LIMIT, loc=LOCATION_ID)
    try:
        slots = http_get_json(url) or []
    except Exception as e:
        print(f"[error] fetch failed: {e}", file=sys.stderr)
        return 1

    filtered = []
    for s in slots:
        try:
            start_dt = parse_iso(s["startTimestamp"])
        except Exception:
            continue
        if passes_date_filters(start_dt.date()):
            filtered.append(s)

    if not filtered:
        print("[info] no qualifying slots found after date filters.")
        return 0

    earliest = min(parse_iso(s["startTimestamp"]) for s in filtered)

    last_seen_dt = None
    if last_seen:
        try:
            last_seen_dt = parse_iso(last_seen)
        except Exception:
            pass

    if (last_seen_dt is None) or (earliest < last_seen_dt):
        preview = sorted(filtered, key=lambda x: x["startTimestamp"])[:5]
        lines = ["New NEXUS appointment(s) at Blaine (date-filtered):"]
        for item in preview:
            dt = parse_iso(item["startTimestamp"]).astimezone(timezone.utc)
            lines.append(f"• {dt.strftime('%Y-%m-%d %H:%M UTC')}  (duration {item.get('duration', 'N/A')} min)")
        lines.append("Book now (log in required): " + SCHEDULER_UI)
        notify("\n".join(lines))

        state["last_seen_iso"] = earliest.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
        save_state(state)
    else:
        print("[info] earliest qualifying slot is not newer than last seen; no alert.")

    return 0

if __name__ == "__main__":
    sys.exit(main())
