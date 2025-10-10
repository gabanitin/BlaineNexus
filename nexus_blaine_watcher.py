#!/usr/bin/env python3
"""
NEXUS Blaine watcher → SMS alerts via Twilio (no external deps).

Env vars (set in the workflow):
  INCLUDE_START: YYYY-MM-DD  (e.g., 2026-02-01)
  INCLUDE_END:   YYYY-MM-DD  (e.g., 2026-02-28)
  LOCATION_ID:   TTP location id (default 5020 = Blaine ENROLLMENT CENTER)
  LIMIT:         Max slots to fetch (default 50)

  TWILIO_SID:    Twilio Account SID (ACxxxxxxxx)
  TWILIO_TOKEN:  Twilio Auth Token
  SMS_FROM:      Your SMS-capable Twilio number, e.g. +12298002359  (NO 'whatsapp:' prefix)
  SMS_TO:        One or many recipients, comma-separated, e.g. +1604..., +1778...

Behavior:
 - Fetches available slots from the public TTP scheduler API.
 - Filters to your date window (UTC).
 - Sends one SMS when NEW results appear (hash-based dedupe).
 - Exits quickly on errors (keeps Actions runtime low).
"""

import os, json, base64, urllib.request, urllib.parse, urllib.error, hashlib
from datetime import datetime, date, timedelta, timezone

# -------------------- CONFIG --------------------
API_URL = "https://ttp.cbp.dhs.gov/schedulerapi/slots?orderBy=soonest&limit={limit}&locationId={loc}&minimum=1"

INCLUDE_START = os.getenv("INCLUDE_START", "2026-02-01")
INCLUDE_END   = os.getenv("INCLUDE_END",   "2026-02-28")
LOCATION_ID   = os.getenv("LOCATION_ID",   "5020")  # Blaine
LIMIT         = int(os.getenv("LIMIT", "50"))

STATE_FILE    = os.getenv("STATE_FILE", "/tmp/nexus_state.json")

# Twilio (SMS)
TWILIO_SID   = os.getenv("TWILIO_SID", "")
TWILIO_TOKEN = os.getenv("TWILIO_TOKEN", "")
SMS_FROM     = os.getenv("SMS_FROM", "")  # +1...
SMS_TO_RAW   = os.getenv("SMS_TO", "")    # +1..., +1...

# --------------- UTILITIES ----------------------
def _iso_to_dt(s: str) -> datetime:
    # Accepts 'YYYY-MM-DDTHH:MM' or '...Z' or '...+00:00'
    s = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        # last resort: strip seconds if present
        try:
            return datetime.fromisoformat(s[:16])
        except Exception:
            raise

def http_get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            body = r.read().decode("utf-8", "ignore")
        if not body or body[0] not in "[{":
            print("[fetch] non-JSON response")
            return []
        return json.loads(body)
    except urllib.error.HTTPError as e:
        print("[fetch] HTTP", e.code)
        return []
    except Exception as e:
        print("[fetch] error", e)
        return []

def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state: dict):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception:
        pass

def digest_slots(slots):
    keylines = []
    for s in slots:
        # use utcStart datetime string as a stable key
        keylines.append(f"{s.get('start')}|{s.get('end')}|{s.get('locationId')}")
    return hashlib.sha256("\n".join(sorted(keylines)).encode()).hexdigest()

def in_date_window(slot_dt: datetime, start_d: date, end_d: date) -> bool:
    d = slot_dt.date()
    return start_d <= d <= end_d

def as_recipients(raw: str):
    return [n.strip() for n in raw.split(",") if n.strip()]

def notify_sms(message: str):
    sid, token = TWILIO_SID, TWILIO_TOKEN
    from_ = SMS_FROM
    to_list = as_recipients(SMS_TO_RAW)
    if not (sid and token and from_ and to_list):
        print("[sms] missing env (TWILIO_SID/TWILIO_TOKEN/SMS_FROM/SMS_TO)")
        return

    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"

    for to in to_list:
        data = urllib.parse.urlencode({
            "From": from_,
            "To": to,
            "Body": message,
        }).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Mozilla/5.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                print(f"[sms] ok {to} ({r.status})")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            print(f"[sms] fail {to} {e.code} {body[:300]}")
        except Exception as e:
            print(f"[sms] error {to}: {e}")

# --------------- MAIN --------------------------
def main():
    # Parse date window (treat as UTC dates)
    try:
        start_d = datetime.strptime(INCLUDE_START, "%Y-%m-%d").date()
        end_d   = datetime.strptime(INCLUDE_END,   "%Y-%m-%d").date()
    except Exception as e:
        print("[config] bad INCLUDE_START/END", e)
        return 1

    url = API_URL.format(limit=LIMIT, loc=LOCATION_ID)
    print("[watcher] GET", url)
    data = http_get_json(url)

    if not isinstance(data, list):
        print("[watcher] unexpected payload type")
        return 0

    # Normalize slots: many deployments use 'start' or 'startTimestamp'
    norm = []
    for s in data:
        start_raw = s.get("start") or s.get("startTimestamp") or s.get("startTime")
        end_raw   = s.get("end")   or s.get("endTimestamp")   or s.get("endTime")
        if not start_raw:
            continue
        try:
            dt = _iso_to_dt(start_raw)
        except Exception:
            continue
        # Assume timestamps are UTC if offset is present; otherwise set UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        if in_date_window(dt.astimezone(timezone.utc), start_d, end_d):
            norm.append({
                "start": start_raw,
                "end": end_raw,
                "locationId": s.get("locationId", LOCATION_ID),
            })

    print(f"[watcher] total: {len(data)} | in-window: {len(norm)}")

    if not norm:
        print("[watcher] nothing in window; exit")
        return 0

    state = load_state()
    dg = digest_slots(norm)
    if dg == state.get("last_digest"):
        print("[watcher] no change since last alert")
        return 0

    # Build concise SMS (first handful of slots)
    # Show up to 8 earliest
    def _parse_dt(s):
        try:
            x = _iso_to_dt(s)
            if x.tzinfo is None: x = x.replace(tzinfo=timezone.utc)
            return x.astimezone(timezone.utc)
        except Exception:
            return None

    sorted_slots = sorted(norm, key=lambda s: _parse_dt(s["start"]) or datetime.max.replace(tzinfo=timezone.utc))[:8]
    lines = ["🚨 NEXUS slots at Blaine (Feb 2026)"]
    for s in sorted_slots:
        dt = _parse_dt(s["start"])
        if dt:
            lines.append(dt.strftime("• %Y-%m-%d %H:%M UTC"))
        else:
            lines.append(f"• {s['start']}")
    lines.append("Login: https://ttp.cbp.dhs.gov/")

    notify_sms("\n".join(lines))

    state["last_digest"] = dg
    save_state(state)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())