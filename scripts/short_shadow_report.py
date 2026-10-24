#!/usr/bin/env python3
"""short_shadow_report.py - E98 P0 item 7b: do the shadow shorts have a test like every other switch? (read-only, changes nothing)

  TOKEN=<admin session token, optional> python3 scripts/short_shadow_report.py [--ps-url http://127.0.0.1:8006] [--min-trades 100] [--min-days 20]

Reads position-stocks GET /short/shadow (the would-be shorts: what Stockky would have shorted and what happened next) and judges the NUMBERS:
  * the endpoint answers and the counters agree (closed = target + stop + eod, open + closed + expired <= total);
  * NOT_ENOUGH_DATA while fewer than --min-trades closed would-be shorts or fewer than --min-days days with results (the S2 evidence, plan Part 2);
  * with enough data: PASS when the net P&L (after charges) is above zero, FAIL when it is zero or below.
Prints one line per check and ends with `SHORT SHADOW: PASS | FAIL - <reason> | NOT_ENOUGH_DATA - <progress>`.
Exit 0 = PASS or NOT_ENOUGH_DATA (nothing to act on), 1 = FAIL, 2 = bad usage. Stdlib only. It never places or changes anything, and it does not touch SHORT_MODE.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Callable, List, Optional, Tuple

Fetch = Callable[[str], Tuple[int, Any]]


def http_fetch(token: str, timeout: float = 30.0) -> Fetch:
    def fetch(url: str) -> Tuple[int, Any]:
        h = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:  # noqa: S310 - operator-supplied URL
                return r.status, json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            return e.code, None
        except Exception:  # noqa: BLE001
            return 0, None
    return fetch


def _i(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def evaluate(doc: Any, min_trades: int = 100, min_days: int = 20) -> Tuple[str, str, List[str]]:
    """Pure. (verdict, reason, lines) with verdict in PASS | FAIL | NOT_ENOUGH_DATA."""
    lines: List[str] = []
    if not isinstance(doc, dict):
        return "FAIL", "GET /short/shadow gave no usable answer", lines
    lines.append(f"OK   /short/shadow answers, mode {doc.get('mode')!r}, {doc.get('total')} rows")
    n = {k: _i(doc.get(k)) for k in ("total", "open", "closed", "target", "stop", "eod", "expired")}
    missing = [k for k, v in n.items() if v is None]
    if missing:
        return "FAIL", f"counter(s) missing or not numbers: {', '.join(missing)}", lines
    if n["closed"] != n["target"] + n["stop"] + n["eod"]:
        return "FAIL", f"closed {n['closed']} is not target {n['target']} + stop {n['stop']} + eod {n['eod']}", lines
    if n["open"] + n["closed"] + n["expired"] > n["total"]:
        return "FAIL", f"open {n['open']} + closed {n['closed']} + expired {n['expired']} is more than total {n['total']}", lines
    lines.append("OK   counters agree")
    days = _i(doc.get("days_with_results")) or 0
    if n["closed"] < min_trades or days < min_days:
        return ("NOT_ENOUGH_DATA", f"{n['closed']} of {min_trades} closed would-be shorts, {days} of {min_days} days with results", lines)
    try:
        net = float(doc.get("net_pnl"))
    except (TypeError, ValueError):
        return "FAIL", "net_pnl missing or not a number", lines
    lines.append(f"OK   enough data: {n['closed']} closed over {days} days, net {net:+.2f}, win rate {doc.get('win_rate_pct')}%")
    if net > 0:
        return "PASS", f"net {net:+.2f} over {n['closed']} would-be shorts", lines
    return "FAIL", f"net {net:+.2f} over {n['closed']} would-be shorts: the shadow shorts do not make money after charges", lines


def run(fetch: Fetch, ps_url: str, min_trades: int = 100, min_days: int = 20) -> Tuple[int, str]:
    code, doc = fetch(f"{ps_url.rstrip('/')}/short/shadow")
    if code != 200:
        verdict, reason, lines = "FAIL", f"GET /short/shadow returned HTTP {code}", []
    else:
        verdict, reason, lines = evaluate(doc, min_trades, min_days)
    out = "\n".join(lines + [f"SHORT SHADOW: {verdict}" + ("" if verdict == "PASS" and not reason else f" - {reason}")])
    return (1 if verdict == "FAIL" else 0), out


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ps-url", default=os.environ.get("POSITION_STOCKS_URL", "http://127.0.0.1:8006"))
    ap.add_argument("--token", default=os.environ.get("TOKEN", ""))
    ap.add_argument("--min-trades", type=int, default=100)
    ap.add_argument("--min-days", type=int, default=20)
    a = ap.parse_args(argv)
    if a.min_trades < 1 or a.min_days < 1:
        print("--min-trades and --min-days must be at least 1", file=sys.stderr)
        return 2
    rc, text = run(http_fetch(a.token), a.ps_url, a.min_trades, a.min_days)
    print(text)
    return rc


if __name__ == "__main__":
    sys.exit(main())
