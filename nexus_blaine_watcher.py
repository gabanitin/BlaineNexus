#!/usr/bin/env python3
"""
NEXUS Blaine watcher → SMS alerts via Twilio (no external deps, trial-safe message length).

Env vars (set in the workflow):
  INCLUDE_START: YYYY-MM-DD  (e.g., 2026-02-01)
  INCLUDE_END:   YYYY-MM-DD  (e.g., 2026-02-28)
  LOCATION_ID:   TTP location id (default 5020 = Blaine ENROLLMENT CENTER)
  LIMIT:         Max slots to fetch (default 50)

  TWILIO_SID:    Twilio Account SID (ACxxxxxxxx)
  TWILIO_TOKEN:  Twilio Auth Token
  SMS_FROM:      Your SMS-capable Twilio number, e.g. +12298002359
  SMS_TO:        One or many recipients, comma-separated, e.g. +1604..., +1778...

Behavior:
 - Fetches available slots from the public TTP scheduler API.
 - Filters to your date window (UTC).
 - Sends one SMS when NEW results appear (shortened for Twilio trial).
"""

import os, json, base64, urllib.request, urllib.parse, urllib.error, hashlib
from datetime import datetime, date, timezone

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

# --------------- HELPERS ----------------------

def shorten_for_trial(msg, limit=155):
    """Trim message to fit Twilio trial SMS limit (~160 chars)."""
    msg = " ".join(msg.split())  # collapse newlines/spaces
    return msg[:limit]

def _iso_to_dt(s: str) -> datetime:
    s = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return datetime.fromisoformat(s[:16])

def http_get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            body = r.read().decode("utf-8", "ignore")
        return json.loads(body) if body.strip().startswith("[") else []
    except Exception as e:
        print("[fetch error]", e)
        return []

def load_state():
    try:
        with open(STATE_FILE, "r") as f: return json.load(f)
    except: return {}

def save_state(state: dict):
    try:
        with open(STATE_FILE, "w") as f: json.dump(state, f)
    except: pass

def digest_slots(slots):
    lines = [f"{s.get('start')}|{s.get('end')}|{s.get('locationId')}" for s in slots]
    return hashlib.sha256("\n".join(sorted(lines)).encode()).hexdigest()

def as_recipients(raw: str): return [n.strip() for n in raw.split(",") if n.strip()]

def notify_sms(message: str):
    sid, token, from_ = TWILIO_SID, TWILIO_TOKEN, SMS_FROM
    to_list = as_recipients(SMS_TO_RAW)
    if not (sid and token and from_ and to_list):
        print("[sms] missing env vars")
        return
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    msg = shorten_for_trial(message)
    for to in to_list:
        data = urllib.parse.urlencode({"From": from_, "To": to, "Body": msg}).encode()
        req = urllib.request.Request(url, data=data, method="POST",
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            })
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                print(f"[sms ok] {to} ({r.status})")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            print(f"[sms fail] {to} {e.code} {body[:200]}")
        except Exception as e:
            print(f"[sms error] {to}: {e}")

# --------------- MAIN --------------------------
def main():
    try:
        start_d = datetime.strptime(INCLUDE_START, "%Y-%m-%d").date()
        end_d   = datetime.strptime(INCLUDE_END, "%Y-%m-%d").date()
    except Exception as e:
        print("[config error]", e)
        return 1

    url = API_URL.format(limit=LIMIT, loc=LOCATION_ID)
    print("[watcher] GET", url)
    data = http_get_json(url)
    if not isinstance(data, list):
        print("[watcher] bad payload"); return 0

    norm = []
    for s in data:
        raw = s.get("start") or s.get("startTimestamp") or s.get("startTime")
        if not raw: continue
        try:
            dt = _iso_to_dt(raw)
        except Exception: continue
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        if start_d <= dt.date() <= end_d:
            norm.append({"start": raw, "locationId": s.get("locationId", LOCATION_ID)})

    print(f"[watcher] total {len(data)}, in window {len(norm)}")
    if not norm: return 0

    state = load_state()
    dg = digest_slots(norm)
    if dg == state.get("last_digest"):
        print("[watcher] no change"); return 0

    earliest = sorted(norm, key=lambda s: s["start"])[:3]
    lines = [f"🚨 NEXUS slots at Blaine ({INCLUDE_START}–{INCLUDE_END})"]
    for s in earliest:
        try:
            dt = _iso_to_dt(s["start"])
            lines.append(dt.strftime("%b %d %H:%M"))
        except: pass
    lines.append("https://ttp.cbp.dhs.gov/")

    notify_sms(" | ".join(lines))
    state["last_digest"] = dg
    save_state(state)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())