#!/usr/bin/env python3
"""fno_rolling_fetch.py - download EXPIRED option candles (Dhan "rolling option" API) so the F&O strategies can be tested on REAL premiums. First live run 9 Nov 2026 (round 255): Dhan takes ONE day apart per call, not 30 - see rolling.py.

  DHAN_CLIENT_ID=... DHAN_ACCESS_TOKEN=... python3 scripts/fno_rolling_fetch.py --und NIFTY --from 2026-05-01 --to 2026-11-07 --strikes ATM,ATM+1,ATM-1 --out data/fno_rolling

Read-only data calls (no orders). Needs the Dhan Data API plan. Writes one JSON file per (underlying, CALL/PUT, strike label, expiry code): rows of {t, o, h, l, c, v, oi, iv, spot, strike}.
First run it for ONE week and open the file: check the timestamps are IST market hours, that `spot` matches the index, and that the ATM strike moves with spot. Then widen the range.
Request / reply shape and its limits (measured live: toDate - fromDate at most 1 day, toDate inclusive; about 5 years back): services/fno-options-service/backtest/rolling.py.
A six-month run is about 80 calls per (CALL/PUT, strike) series. A run that ended with failed windows is finished with the same command plus --resume (only the failed windows are asked again); the same flag with a later --to adds only the new days to the existing files. `--span-days` widens the window once scripts/fno_rolling_probe.py shows Dhan accepts more.

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
                 pause: float = 0.5, retries: int = 3, sleep: Callable[[float], None] = time.sleep, say: Callable[[str], None] = print, span_days: int = rolling.MAX_DAYS, only: Optional[List[Tuple[date, date]]] = None) -> Dict[str, Any]:
    """Round 255: every call needed to cover the weekdays of [start, end) for one (CALL/PUT, strike) -> {"rows", "failed_windows", "empty_windows"}; rows are trimmed to [start, end)."""
    chunks: List[List[Dict[str, Any]]] = []
    failed: List[List[str]] = []
    empty: List[List[str]] = []
    for w0, w1 in (only if only is not None else rolling.windows(start, end, span_days)):
        body = rolling.build_request(und, ot, strike, w0, w1, interval, flag, code, span_days)
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
    return {"rows": rolling.in_range(rolling.merge_rows(chunks), start, end), "failed_windows": failed, "empty_windows": empty}


SAME_KEYS = ("underlying", "type", "strike", "interval", "expiry_flag", "expiry_code", "span_days")      # round 258: the range may differ (--resume extends it)


def load_prior(path: Path, meta: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    """Round 257 (--resume): the file of an earlier run of THIS series (same underlying, type, strike, interval, expiry, range, span) -> (doc, "complete" | "partial"), else (None, why).
    The complete file wins; a .partial is used to refetch only its failed windows. A file made for another span, strike or interval is never reused (its windows would not line up). Round 258: its range may differ; plan_extension() says what is missing."""
    found: Dict[str, Dict[str, Any]] = {}
    why = "no earlier file: fetching the whole series"
    for target, kind in ((path, "complete"), (path.with_name(path.name + ".partial"), "partial")):
        if not target.exists():
            continue
        try:
            doc = json.loads(target.read_text())
        except (OSError, ValueError):
            why = f"{target.name} is unreadable: fetching the whole series"
            continue
        if any(doc.get(k) != meta.get(k) for k in SAME_KEYS) or not _iso(doc.get("from")) or not _iso(doc.get("to")):
            why = f"{target.name} was made for another span / strike / interval: fetching the whole series"
            continue
        if kind == "complete" and doc.get("complete") is not True:
            continue
        found[kind] = doc
    part, full = found.get("partial"), found.get("complete")
    if part is not None and (full is None or (part["from"] <= full["from"] and part["to"] >= full["to"])):
        return part, "partial"                       # a partial of an EXTENSION already holds the complete file's rows plus its own progress
    if full is not None:
        return full, "complete"
    return None, why


def _iso(v: Any) -> bool:
    try:
        date.fromisoformat(str(v))
        return True
    except ValueError:
        return False


def plan_extension(prior: Dict[str, Any], start: date, end: date) -> Tuple[date, date, List[Tuple[date, date]]]:
    """Round 258: (new_from, new_to, ranges still to fetch). The file will cover the UNION of what it holds and what is asked for, so a gap between them is fetched too (a hole must never hide
    inside a range the file claims). Nothing to fetch when the file already covers the request."""
    pf, pt = date.fromisoformat(prior["from"]), date.fromisoformat(prior["to"])
    lo, hi = min(pf, start), max(pt, end)
    todo: List[Tuple[date, date]] = []
    if lo < pf:
        todo.append((lo, pf))
    if hi > pt:
        todo.append((pt, hi))
    return lo, hi, todo


def combine(parts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Several fetch_series results (one per range) -> one."""
    return {"rows": rolling.merge_rows([p["rows"] for p in parts]), "failed_windows": [w for p in parts for w in p["failed_windows"]], "empty_windows": [w for p in parts for w in p["empty_windows"]]}


def resume_series(prior: Dict[str, Any], redo: Dict[str, Any], start: date, end: date) -> Dict[str, Any]:
    """The earlier partial's rows + the refetched windows -> one series. Failed windows that failed again stay listed; empty windows are the union."""
    empty = [list(w) for w in (prior.get("empty_windows") or [])]
    for w in redo["empty_windows"]:
        if list(w) not in empty:
            empty.append(list(w))
    return {"rows": rolling.in_range(rolling.merge_rows([prior.get("rows") or [], redo["rows"]]), start, end), "failed_windows": redo["failed_windows"], "empty_windows": sorted(empty)}


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
    ap.add_argument("--span-days", type=int, default=rolling.MAX_DAYS, help=f"toDate - fromDate per call, 1 to {rolling.MAX_SPAN_CLI} (default {rolling.MAX_DAYS}: the most Dhan accepted on 9 Nov 2026; wider answers HTTP 400 DH-905)")
    ap.add_argument("--resume", action="store_true", help="skip a series whose complete file already covers the range; extend one that covers less (only the missing days are fetched); for a .partial, refetch only its failed windows")
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
    if not 1 <= a.span_days <= rolling.MAX_SPAN_CLI:
        print(f"--span-days must be 1 to {rolling.MAX_SPAN_CLI}", file=sys.stderr)
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
            name = out / f"{a.und}_{ot}_{strike.replace('+', 'p').replace('-', 'm')}_{a.expiry_flag}{a.expiry_code}_{a.interval}m.json"
            meta = {"underlying": a.und, "type": ot, "strike": strike, "interval": a.interval, "expiry_flag": a.expiry_flag, "expiry_code": a.expiry_code, "from": a.start, "to": a.end, "span_days": a.span_days}
            do = lambda body: call(a.base, cid, token, body)
            prior, kind = (None, "") if not a.resume else load_prior(name, meta)
            if a.resume and prior is None:
                print(f"{name.name}: {kind}")
            if prior is not None:
                lo, hi, ranges = plan_extension(prior, start, end)
                todo = [(date.fromisoformat(w[0]), date.fromisoformat(w[1])) for w in prior.get("failed_windows") or []] if kind == "partial" else []
                if kind == "complete" and not ranges:
                    print(f"{name.name}: already complete ({len(prior.get('rows') or [])} candles, {prior['from']}..{prior['to']}), skipped")
                    continue
                meta = dict(meta, **{"from": lo.isoformat(), "to": hi.isoformat()})
                print(f"{name.name}: resuming ({prior['from']}..{prior['to']} held, {len(prior.get('rows') or [])} candles): refetching {len(todo)} failed window(s)"
                      + (f", fetching {', '.join(f'{x}..{y}' for x, y in ranges)}" if ranges else ""))
                parts = [fetch_series(do, a.und, ot, strike, lo, hi, a.interval, a.expiry_flag, a.expiry_code, a.pause, a.retries, span_days=a.span_days, only=todo)] if todo else []
                parts += [fetch_series(do, a.und, ot, strike, x, y, a.interval, a.expiry_flag, a.expiry_code, a.pause, a.retries, span_days=a.span_days) for x, y in ranges]
                series = resume_series(prior, combine(parts) if parts else {"rows": [], "failed_windows": [], "empty_windows": []}, lo, hi)
            else:
                series = fetch_series(do, a.und, ot, strike, start, end, a.interval, a.expiry_flag, a.expiry_code, a.pause, a.retries, span_days=a.span_days)
            path, what = write_series(name, meta, series)
            bad += 1 if series["failed_windows"] or path is None else 0
            print(f"{name.name}: {len(series['rows'])} candles, {what}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
