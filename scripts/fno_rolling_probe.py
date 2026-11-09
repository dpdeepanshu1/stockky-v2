#!/usr/bin/env python3
"""fno_rolling_probe.py - one call per line against Dhan's rolling (expired) options API: which window / field / interval does it accept TODAY? Read-only, no orders, the token is never printed.

Written round 255 after the first live fetch failed: the docs say 30 days a call with a non-inclusive toDate, the API took ONE day apart (toDate inclusive) and answered HTTP 400 DH-905 to anything wider.
Re-run it when Dhan changes the API (or before raising `fno_rolling_fetch.py --span-days`): if S2 / S4 / S12 answer OK with 2+ days, the fetch can use wider windows.

  DHAN_CLIENT_ID=.. DHAN_ACCESS_TOKEN=.. python3 scripts/fno_rolling_probe.py        (PROBE_PAUSE=1.5 seconds between calls; the dates are fixed: edit BASEBODY / VARIANTS to move them)
"""
import copy, json, os, sys, time, urllib.error, urllib.request
from datetime import datetime, timezone, timedelta
BASE = os.getenv("DHAN_BASE_URL", "https://api.dhan.co/v2").rstrip("/")
CID, TOK = os.getenv("DHAN_CLIENT_ID", ""), os.getenv("DHAN_ACCESS_TOKEN", "")
IST = timezone(timedelta(hours=5, minutes=30))
FULL = ["open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot"]
BASEBODY = {"exchangeSegment": "NSE_FNO", "interval": 5, "securityId": 13, "instrument": "OPTIDX", "expiryFlag": "WEEK", "expiryCode": 1,
            "strike": "ATM", "drvOptionType": "CALL", "requiredData": FULL, "fromDate": "2026-11-05", "toDate": "2026-11-06"}


def v(label, f, t, **chg):
    b = copy.deepcopy(BASEBODY); b.update(fromDate=f, toDate=t); b.update(chg); return label, b


VARIANTS = [
    v("S1  11-05 to 11-06 (control, worked)", "2026-11-05", "2026-11-06"),
    v("S2  11-04 to 11-06 (2 days)", "2026-11-04", "2026-11-06"),
    v("S3  11-03 to 11-06 (3 days)", "2026-11-03", "2026-11-06"),
    v("S4  11-02 to 11-06 (4 days)", "2026-11-02", "2026-11-06"),
    v("S5  11-03 to 11-04 (other day)", "2026-11-03", "2026-11-04"),
    v("S6  11-02 to 11-03 (Monday)", "2026-11-02", "2026-11-03"),
    v("S7  11-06 to 11-07 (Fri to Sat)", "2026-11-06", "2026-11-07"),
    v("S8  11-02 to 11-07, interval 60", "2026-11-02", "2026-11-07", interval=60),
    v("S9  10-12 to 11-06, interval 60", "2026-10-12", "2026-11-06", interval=60),
    v("S10 09-21 to 09-22 (old, 1 day)", "2026-09-21", "2026-09-22"),
    v("S11 09-21 to 09-23 (old, 2 days)", "2026-09-21", "2026-09-23"),
    v("S12 09-21 to 09-28 (old, 7 days)", "2026-09-21", "2026-09-28"),
    v("S13 11-05 to 11-06 PUT", "2026-11-05", "2026-11-06", drvOptionType="PUT"),
    v("S14 11-05 to 11-06 ATM+1", "2026-11-05", "2026-11-06", strike="ATM+1"),
]


def post(body):
    h = {"Content-Type": "application/json", "Accept": "application/json", "access-token": TOK, "client-id": CID}
    req = urllib.request.Request(BASE + "/charts/rollingoption", data=json.dumps(body).encode(), method="POST", headers=h)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, OSError) as e:
        return 0, {"error": type(e).__name__}


def fmt(ts):
    return datetime.fromtimestamp(ts, IST).strftime("%m-%d %H:%M")


def summary(code, data):
    if code == 200:
        d = (data.get("data") or {}) if isinstance(data, dict) else {}
        s = d.get("ce") or d.get("pe") or {}
        ts = s.get("timestamp") or []
        if not ts:
            return "OK  candles=0"
        days = len({datetime.fromtimestamp(x, IST).date() for x in ts})
        return f"OK  candles={len(ts)} days={days} first={fmt(ts[0])} last={fmt(ts[-1])}"
    if isinstance(data, dict):
        return f"{data.get('errorCode') or data.get('error') or ''}".strip()
    return ""


def main():
    if not CID or not TOK:
        sys.exit("set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN")
    for label, body in VARIANTS:
        code, data = post(body)
        print(f"{label:40s} HTTP {code}  {summary(code, data)}")
        time.sleep(float(os.getenv("PROBE_PAUSE", "1.5")))


if __name__ == "__main__":
    main()
