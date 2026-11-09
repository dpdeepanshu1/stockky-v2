#!/usr/bin/env python3
"""fno_rolling_fetch.py - download EXPIRED option candles (Dhan "rolling option" API) so the F&O strategies can be tested on REAL premiums. NEVER RUN YET (round 236: written from Dhan's docs; round 16 audit hardened it).

  DHAN_CLIENT_ID=... DHAN_ACCESS_TOKEN=... python3 scripts/fno_rolling_fetch.py --und NIFTY --from 2026-05-01 --to 2026-11-07 --strikes ATM,ATM+1,ATM-1 --out data/fno_rolling

Read-only data calls (no orders). Needs the Dhan Data API plan. Writes one JSON file per (underlying, CALL/PUT, strike label, expiry code): rows of {t, o, h, l, c, v, oi, iv, spot, strike}.
First run it for ONE week and open the file: check the timestamps are IST market hours, that `spot` matches the index, and that the ATM strike moves with spot. Then widen the range.
Request / reply shape and its limits (30 days a call, about 5 years back): services/fno-options-service/backtest/rolling.py.

Round 16 (what a failed run used to do): a 429 / 5xx / timeout on one window was counted, the window was skipped, and the file was written anyway, so a series with a hole looked complete; an
expired token (every call 401) overwrote yesterday's good file with an empty one. Now: transient errors are retried with a growing pause; every file lists `failed_windows` (a backtest must refuse
a series that has any) and `empty_windows` (no candles: holiday or no contract); a series with a failed window is saved as <name>.partial and never replaces the complete file; a series with no
candles at all never replaces anything; files are written atomically; a candle at a window edge is kept once."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services" / "fno-options-service"))
from backtest import rolling  # noqa: E402

RETRY_HTTP = (429, 500, 502, 503, 504)       # rate limit / server trouble: worth another try. 400 / 401 / 403 are the request or the token: retrying cannot help.


def call(base: str, cid: str, token: str, body: dict, timeout: float = 30.0) -> dict:
    req = urllib.request.Request(base.rstrip("/") + "/charts/rollingoption", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "application/json", "access-token": token, "client-id": cid})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def fetch_window(do_call: Callable[[dict], dict], body: dict, option_type: str, retries: int = 3, pause: float = 0.5,
                 sleep: Callable[[float], None] = time.sleep) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """One window -> (rows, None) or ([], reason). Retries 429 / 5xx / network errors up to `retries` times with a pause that doubles (max 30 s)."""
    reason = ""
    for attempt in range(max(0, retries) + 1):
        try:
            return rolling.parse_response(do_call(body), option_type), None
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode()[:200]
            except Exception:  # noqa: BLE001
                detail = ""
            reason = f"HTTP {e.code} {detail}".strip()
            if e.code not in RETRY_HTTP:
                return [], reason
        except (urllib.error.URLError, OSError, ValueError) as e:       # ValueError: the reply was not JSON
            reason = type(e).__name__
        if attempt < retries:
            sleep(min(30.0, max(pause, 0.5) * (2 ** attempt)))
    return [], reason + f" (after {retries + 1} tries)"


def fetch_series(do_call: Callable[[dict], dict], und: str, ot: str, strike: str, start: date, end: date, interval: int, flag: str, code: int,
                 pause: float = 0.5, retries: int = 3, sleep: Callable[[float], None] = time.sleep, say: Callable[[str], None] = print) -> Dict[str, Any]:
    """Every 30-day window of [start, end) for one (CALL/PUT, strike) -> {"rows", "failed_windows", "empty_windows"}."""
    chunks: List[List[Dict[str, Any]]] = []
    failed: List[List[str]] = []
    empty: List[List[str]] = []
    for w0, w1 in rolling.windows(start, end):
        body = rolling.build_request(und, ot, strike, w0, w1, interval, flag, code)
        got, why = fetch_window(do_call, body, ot, retries, pause, sleep)
        if why:
            failed.append([w0.isoformat(), w1.isoformat(), why])
            say(f"{und} {ot} {strike} {w0}..{w1}: FAILED {why}")
        else:
            if not got:
                empty.append([w0.isoformat(), w1.isoformat()])
            chunks.append(got)
            say(f"{und} {ot} {strike} {w0}..{w1}: {len(got)} candles")
        sleep(pause)
    return {"rows": rolling.merge_rows(chunks), "failed_windows": failed, "empty_windows": empty}


def write_series(path: Path, meta: Dict[str, Any], series: Dict[str, Any]) -> Tuple[Optional[Path], str]:
    """Save one series. Returns (path written or None, what happened). Never replaces a complete file with a partial or empty one."""
    rows, failed = series["rows"], series["failed_windows"]
    if not rows:
        return None, "no candles at all: nothing written, any earlier file is kept"
    doc = dict(meta, rows=rows, failed_windows=failed, empty_windows=series["empty_windows"], complete=not failed)
    target = path if not failed else path.with_name(path.name + ".partial")
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(doc))
    os.replace(tmp, target)
    if not failed:
        try:
            os.remove(str(path) + ".partial")          # an older partial of the same series is superseded by this complete file
        except OSError:
            pass
    return target, ("complete" if not failed else f"PARTIAL: {len(failed)} window(s) failed; saved as {target.name}, the complete file (if any) was left alone")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--und", default="NIFTY", choices=["NIFTY", "SENSEX"])
    ap.add_argument("--from", dest="start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--to", dest="end", required=True, help="YYYY-MM-DD (not included)")
    ap.add_argument("--strikes", default="ATM", help="comma list, e.g. ATM,ATM+1,ATM-1")
    ap.add_argument("--interval", type=int, default=5, choices=list(rolling.INTERVALS))
    ap.add_argument("--expiry-flag", default="WEEK", choices=["WEEK", "MONTH"])
    ap.add_argument("--expiry-code", type=int, default=1)
    ap.add_argument("--out", default="data/fno_rolling")
    ap.add_argument("--pause", type=float, default=0.5, help="seconds between calls (the rate limit is not published)")
    ap.add_argument("--retries", type=int, default=3, help="extra tries for a 429 / 5xx / network error on one window")
    ap.add_argument("--base", default=os.getenv("DHAN_BASE_URL", "https://api.dhan.co/v2"))
    a = ap.parse_args()
    cid, token = os.getenv("DHAN_CLIENT_ID", ""), os.getenv("DHAN_ACCESS_TOKEN", "")
    if not cid or not token:
        print("set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN", file=sys.stderr)
        return 2
    try:
        start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    except ValueError:
        print("--from / --to must be YYYY-MM-DD", file=sys.stderr)
        return 2
    if end <= start:
        print("--to must be after --from (it is not included)", file=sys.stderr)
        return 2
    strikes = [s.strip() for s in a.strikes.split(",") if s.strip()]
    for s in strikes:
        if not rolling.STRIKE_RE.match(s):
            print(f"bad strike {s!r}: use ATM, ATM+n or ATM-n with n from 1 to 10", file=sys.stderr)
            return 2
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    bad = 0
    for strike in strikes:
        for ot in ("CALL", "PUT"):
            series = fetch_series(lambda body: call(a.base, cid, token, body), a.und, ot, strike, start, end, a.interval, a.expiry_flag, a.expiry_code, a.pause, a.retries)
            name = out / f"{a.und}_{ot}_{strike.replace('+', 'p').replace('-', 'm')}_{a.expiry_flag}{a.expiry_code}_{a.interval}m.json"
            meta = {"underlying": a.und, "type": ot, "strike": strike, "interval": a.interval, "expiry_flag": a.expiry_flag, "expiry_code": a.expiry_code, "from": a.start, "to": a.end}
            path, what = write_series(name, meta, series)
            bad += 1 if series["failed_windows"] or path is None else 0
            print(f"{name.name}: {len(series['rows'])} candles, {what}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
