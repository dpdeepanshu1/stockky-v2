#!/usr/bin/env python3
"""intraday_state_check.py - E126: the three Monday checks that had no timer, as one read-only script.

  TOKEN=<admin session token> python3 scripts/intraday_state_check.py --phase shadow-shorts   # T5, 09:35: short shadow rows + gate counters + ORB bar counters
  TOKEN=<admin session token> python3 scripts/intraday_state_check.py --phase shorts-flat     # T7, 15:05: no short is open after the 14:55 cover
  TOKEN=<admin session token> python3 scripts/intraday_state_check.py --phase overlay-clear   # 15:35: the Market Weather overlay is gone after 15:30

Options: --rt-url http://127.0.0.1:8005  --ps-url http://127.0.0.1:8006  --md-url http://127.0.0.1:8001  [--expect-short-mode any|off|shadow|live]

Every line is OK / INFO / WARN / FAIL; the last line is `<NAME>: PASS | FAIL - <first failing line> | SKIPPED - <why>`.
Exit 0 = PASS or SKIPPED, 1 = FAIL (the runner shows that as "attention" and Telegram says LOOK), 2 = bad usage.
It only does GET requests; it places nothing and changes nothing. On an exchange holiday or a weekend it prints SKIPPED (from market-data
/market/session, `calendar_holiday`), so a timetable that fires on a holiday stays quiet. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

IST = timezone(timedelta(hours=5, minutes=30))
PHASES = ("shadow-shorts", "shorts-flat", "overlay-clear")
NAMES = {"shadow-shorts": "SHORT SHADOW (T5)", "shorts-flat": "SHORTS FLAT (T7)", "overlay-clear": "OVERLAY CLEAR"}
OPEN_STATUSES = ("OPEN", "EXIT_LEGS_REJECTED", "PARTIALLY_CLOSED", "PENDING")      # a short in any of these still holds shares or a resting order
OVERLAY_END = (15, 30)                                                              # the Market Weather overlay ends at 15:30 IST
Call = Callable[[str], Tuple[int, Any]]                                              # url -> (http status or 0, parsed json / text)


def http_get(token: str, timeout: float = 20.0) -> Call:
    def call(url: str) -> Tuple[int, Any]:
        h = {"Accept": "application/json"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:  # noqa: S310 - operator-supplied URL
                raw, status = r.read().decode("utf-8", "replace"), r.status
        except urllib.error.HTTPError as e:
            raw, status = e.read().decode("utf-8", "replace"), e.code
        except Exception as e:  # noqa: BLE001 - unreachable, refused, timed out
            return 0, f"{type(e).__name__}: {e}"[:200]
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, raw[:200]
    return call


class Report:
    def __init__(self) -> None:
        self.lines: List[Tuple[str, str]] = []

    def add(self, level: str, text: str) -> None:
        self.lines.append((level, text))

    def first_fail(self) -> Optional[str]:
        return next((t for lv, t in self.lines if lv == "FAIL"), None)


def _rows(js: Any) -> List[Dict[str, Any]]:
    """/positions may answer a bare list or {"positions": [...]}; anything else has no rows."""
    if isinstance(js, dict):
        js = js.get("positions", js.get("rows"))
    return [r for r in js if isinstance(r, dict)] if isinstance(js, list) else []


def skip_reason(call: Call, md_url: str, now: datetime) -> str:
    """'' = a normal trading day (or unknown: fail open, the checks run); otherwise the reason this day has nothing to check."""
    if now.astimezone(IST).weekday() > 4:
        return "weekend"
    st, js = call(md_url.rstrip("/") + "/market/session")
    if st == 200 and isinstance(js, dict) and js.get("calendar_holiday"):
        return "exchange holiday (market-data calendar)"
    return ""


# ---------------------------------------------------------------------------------------------------------- phases
def phase_shadow_shorts(call: Call, rt: str, ps: str, expect_mode: str, rep: Report) -> None:
    st, js = call(ps + "/short/shadow")
    if st != 200 or not isinstance(js, dict):
        rep.add("FAIL", f"position-stocks GET /short/shadow answered {st or 'no response'}: the short shadow cannot be read")
    else:
        mode = str(js.get("mode") or "?")
        if mode not in ("off", "shadow", "live"):
            rep.add("FAIL", f"SHORT_MODE reads {mode!r}, expected off / shadow / live")
        elif expect_mode != "any" and mode != expect_mode:
            rep.add("FAIL", f"SHORT_MODE is {mode}, expected {expect_mode}")
        else:
            rep.add("OK", f"SHORT_MODE = {mode}")
        gc = js.get("gate_counters")
        if isinstance(gc, dict):
            top = sorted(((k, v) for k, v in gc.items() if isinstance(v, (int, float)) and v), key=lambda kv: -kv[1])[:5]
            rep.add("OK", "gate counters present" + (": " + ", ".join(f"{k}={v:g}" for k, v in top) if top else " (all zero so far)"))
        else:
            rep.add("FAIL", "GET /short/shadow carries no gate_counters")
        rep.add("INFO", f"would-be shorts: total {js.get('total')}, open {js.get('open')}, closed {js.get('closed')}, net {js.get('net_pnl')} "
                        f"(progress {(js.get('s2_progress') or {}).get('would_be_shorts')}/{(js.get('s2_progress') or {}).get('needed')})")
        probe = js.get("sell_probe")
        verdict = (probe or {}).get("verdict") if isinstance(probe, dict) else None
        if not verdict or verdict == "NOT_RUN":
            rep.add("INFO", "T6 Dhan SELL probe has not been run yet (manual, 09:30-14:30): SHORT_MODE=live stays refused until it PASSes")
        else:
            rep.add("INFO", f"last SELL probe verdict: {verdict}")
    st, js = call(rt + "/health")
    bars = ((js.get("orb") or {}).get("bars") if isinstance(js, dict) and isinstance(js.get("orb"), dict) else None)
    if st != 200:
        rep.add("FAIL", f"real-trade GET /health answered {st or 'no response'}")
    elif isinstance(bars, dict):
        rep.add("OK", f"ORB bar counters: hit {bars.get('hit')}, miss {bars.get('miss')}, stale {bars.get('stale')}")
        hit, miss, stale = (bars.get(k) or 0 for k in ("hit", "miss", "stale"))
        if isinstance(hit, (int, float)) and hit == 0 and (miss or stale) and (miss + stale) >= 10:
            rep.add("WARN", f"every bar lookup missed or was stale so far (miss {miss}, stale {stale}): the close-trigger fields will be empty today")
    else:
        rep.add("WARN", "real-trade /health carries no orb.bars counters (E113 not running?)")


def phase_shorts_flat(call: Call, ps: str, rep: Report) -> None:
    st, js = call(ps + "/positions")
    if st != 200:
        rep.add("FAIL", f"position-stocks GET /positions answered {st or 'no response'}: cannot prove there is no open short")
    else:
        rows = _rows(js)
        shorts = [r for r in rows if str(r.get("side") or "LONG").upper() == "SHORT" and str(r.get("status") or "").upper() in OPEN_STATUSES]
        if shorts:
            names = ", ".join(f"{r.get('symbol')} x{r.get('quantity')} ({r.get('status')})" for r in shorts[:6])
            rep.add("FAIL", f"{len(shorts)} short position(s) still open after the 14:55 cover: {names}. Check Dhan now and cover by hand if needed")
        else:
            rep.add("OK", f"no open short among the last {len(rows)} position rows")
    st, js = call(ps + "/short/plain")
    if st == 200 and isinstance(js, dict):
        n = js.get("orphans_watched") or 0
        rep.add("FAIL" if n else "OK", f"plain-order shorts under watch: {n}" + (" - a SELL is still being watched, check Dhan" if n else ""))
    else:
        rep.add("WARN", f"GET /short/plain answered {st or 'no response'}: the plain-order fallback could not be read")
    st, js = call(ps + "/short/shadow")
    if st == 200 and isinstance(js, dict):
        o = js.get("open") or 0
        rep.add("WARN" if o else "OK", f"shadow shorts still marked OPEN: {o}" + (" (no order exists; they close at the next sweep)" if o else ""))


def _overlay_active(js: Any) -> Optional[str]:
    if not isinstance(js, dict):
        return None
    a = js.get("active")
    ovs = js.get("overlays") or []
    if a or ovs:
        until = (a or {}).get("until") if isinstance(a, dict) else None
        return f"state {(a or {}).get('state')}, until {until}, {len(ovs)} dial(s)"
    return ""


def phase_overlay_clear(call: Call, rt: str, ps: str, now: datetime, rep: Report) -> None:
    n = now.astimezone(IST)
    after_end = (n.hour, n.minute) > OVERLAY_END
    for svc, base in (("real-trade", rt), ("position-stocks", ps)):
        st, js = call(base + "/control/overlay")
        if st != 200:
            rep.add("FAIL", f"{svc} GET /control/overlay answered {st or 'no response'}")
            continue
        act = _overlay_active(js)
        if act is None:
            rep.add("FAIL", f"{svc} /control/overlay answered something that is not an overlay status")
        elif act and after_end:
            rep.add("FAIL", f"{svc}: an overlay is STILL active after 15:30 IST ({act}). Use 'Revert to saved' on the Market Weather card")
        elif act:
            rep.add("INFO", f"{svc}: overlay active ({act}); it ends at 15:30 IST, which has not passed yet")
        else:
            rep.add("OK", f"{svc}: no overlay active; every dial is back at its saved value")


# ---------------------------------------------------------------------------------------------------------- main
def run(phase: str, call: Call, rt: str, ps: str, md: str, expect_mode: str, now: datetime) -> Tuple[int, List[str]]:
    rep, name = Report(), NAMES[phase]
    why = skip_reason(call, md, now)
    if why:
        return 0, [f"INFO  {why}: nothing to check today", f"{name}: SKIPPED - {why}"]
    if phase == "shadow-shorts":
        phase_shadow_shorts(call, rt, ps, expect_mode, rep)
    elif phase == "shorts-flat":
        phase_shorts_flat(call, ps, rep)
    else:
        phase_overlay_clear(call, rt, ps, now, rep)
    out = [f"{lv:<5} {t}" for lv, t in rep.lines]
    bad = rep.first_fail()
    out.append(f"{name}: " + (f"FAIL - {bad}" if bad else "PASS"))
    return (1 if bad else 0), out


def main(argv: Optional[List[str]] = None, call: Optional[Call] = None, now: Optional[datetime] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--phase", required=True, choices=PHASES)
    ap.add_argument("--rt-url", default="http://127.0.0.1:8005")
    ap.add_argument("--ps-url", default="http://127.0.0.1:8006")
    ap.add_argument("--md-url", default="http://127.0.0.1:8001")
    ap.add_argument("--expect-short-mode", default="any", choices=("any", "off", "shadow", "live"))
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return 2
    code, lines = run(a.phase, call or http_get(os.environ.get("TOKEN", "")), a.rt_url.rstrip("/"), a.ps_url.rstrip("/"), a.md_url.rstrip("/"),
                      a.expect_short_mode, now or datetime.now(IST))
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main())
