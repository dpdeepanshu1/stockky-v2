#!/usr/bin/env python3
"""monday_check.py - one read-only pass over the live checks that group 322 could not do offline.

  python3 scripts/monday_check.py --label 0930          # 09:30 IST: depth20 / order-WS layout, quote age, health
  python3 scripts/monday_check.py --label 1015          # 10:15 IST: same, plus the session verdict MUST read `open`
  python3 scripts/monday_check.py --label 0930 --no-docker --out-dir /tmp/x

It only does GETs against the loopback ports of the compose stack (nothing is changed, no token is read or printed) and
writes the report to ~/check_logs/<date>_<label>ist_live.log (the `_live` suffix keeps it apart from any
09:30 log your own cron already writes) as well as to the screen. Every problem is one line that starts
with `FAIL` or `WARN`, so `grep -E 'FAIL|WARN|suspect|CRITICAL' ~/check_logs/*.log` is the whole review.

Exit code: 0 = nothing FAIL, 1 = at least one FAIL (so a cron mail / `&&` chain can react).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

# service name -> (default loopback URL, env var that overrides it)
SERVICES = {
    "api-gateway": ("http://127.0.0.1:8000", "GATEWAY_LOCAL_URL"),
    "market-data": ("http://127.0.0.1:8001", "MARKET_DATA_LOCAL_URL"),
    "analysis": ("http://127.0.0.1:8002", "ANALYSIS_LOCAL_URL"),
    "decision": ("http://127.0.0.1:8004", "DECISION_LOCAL_URL"),
    "real-trade": ("http://127.0.0.1:8005", "REAL_TRADE_LOCAL_URL"),
    "position-stocks": ("http://127.0.0.1:8006", "POSITION_STOCKS_LOCAL_URL"),
    "notification": ("http://127.0.0.1:8008", "NOTIFICATION_LOCAL_URL"),
}
QUOTE_SYMBOL = "RELIANCE"
QUOTE_MAX_AGE_S = 20.0
LOG_PATTERNS = ("FAIL", "suspect", "CRITICAL")
LOG_LINES_SHOWN = 40


def base_url(name: str) -> str:
    default, env = SERVICES[name]
    return (os.environ.get(env) or default).rstrip("/")


def get_json(url: str, timeout: float = 8.0):
    """(http_status, parsed_json_or_None, error_text). Never raises."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(body), ""
            except ValueError:
                return r.status, None, "not JSON"
    except urllib.error.HTTPError as e:
        return e.code, None, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001 - a dead service is a finding, not a crash
        return 0, None, type(e).__name__


def market_is_open_now(now: datetime) -> bool:
    """Plain clock rule (Mon-Fri 09:15-15:30 IST). Holidays are what the session verdict is for."""
    ist = now.astimezone(IST)
    return ist.weekday() < 5 and (9, 15) <= (ist.hour, ist.minute) < (15, 30)


# ---- each check returns a list of (level, text); level in OK / INFO / WARN / FAIL -------------------------------------
def check_health(fetch=get_json):
    out = []
    for name in SERVICES:
        code, _, err = fetch(base_url(name) + "/health")
        out.append(("OK", f"{name} /health 200") if code == 200 else ("FAIL", f"{name} /health -> {code or err}"))
    return out


def check_session(label: str, fetch=get_json, now: datetime | None = None):
    # group470: the session verdict is only meaningful while the market is open. Run at 16:11 (or at the weekend) it read `unknown`
    # and the 10:15 label turned that into a FAIL. Outside Mon-Fri 09:15-15:30 IST the check is skipped, not failed.
    if not market_is_open_now(now or datetime.now(IST)):
        return [("INFO", f"session verdict not checked: the clock says the market is closed ({label} label); run it again during market hours")]
    code, js, err = fetch(base_url("market-data") + "/market/session")
    if not isinstance(js, dict):
        return [("FAIL", f"market-data /market/session unreadable ({code or err})")]
    status = str(js.get("status") or js.get("phase") or "?")
    detail = (f"day={js.get('day')} observed={js.get('observed_symbols')} moved={js.get('moved_symbols')} "
              f"nifty_seen={js.get('nifty_seen')} nifty_moved={js.get('nifty_moved')}")
    line = f"session verdict = {status} ({detail})"
    if label == "1015":
        if status == "open":
            return [("OK", line)]
        if js.get("reason") == "weekend":
            return [("INFO", line + " - weekend")]
        return [("FAIL", line + " - expected `open` at 10:15 IST on a trading day")]
    return [("OK" if status == "open" else "INFO", line + ("" if status == "open" else " - not yet proven; 10:15 is the real check"))]


def check_depth20(fetch=get_json):
    out = []
    code, js, err = fetch(base_url("market-data") + "/internal/dhan-status")
    d20 = js.get("depth20") if isinstance(js, dict) else None
    if not isinstance(d20, dict):
        out.append(("FAIL", f"market-data /internal/dhan-status has no depth20 section ({code or err})"))
    else:
        verdict = d20.get("layout_verdict", "?")
        extra = (f"suspect_packets={d20.get('suspect_packets')} crossed_books={d20.get('crossed_books')} "
                 f"enabled={d20.get('enabled')}")
        if verdict == "ok":
            out.append(("OK", f"depth20 layout_verdict=ok ({extra})"))
        elif verdict == "suspect":
            out.append(("FAIL", f"depth20 layout_verdict=suspect ({extra}) - paste frame_samples from /internal/dhan-status"))
        else:
            out.append(("WARN", f"depth20 layout_verdict={verdict} ({extra}) - nothing parsed yet; is DHAN_DEPTH20_ENABLED=1 and was /depth asked for?"))
    code, js, err = fetch(base_url("market-data") + f"/depth/{QUOTE_SYMBOL}?wait_s=5")
    if not isinstance(js, dict):
        out.append(("FAIL", f"/depth/{QUOTE_SYMBOL} unreadable ({code or err})"))
    elif js.get("available"):
        out.append(("OK", f"/depth/{QUOTE_SYMBOL} available (spread_pct={js.get('spread_pct')})"))
    else:
        out.append(("WARN", f"/depth/{QUOTE_SYMBOL} not available: {js.get('reason')}"))
    return out


def check_order_ws(fetch=get_json):
    code, js, err = fetch(base_url("position-stocks") + "/orders/ws-status")
    if not isinstance(js, dict):
        return [("FAIL", f"position-stocks /orders/ws-status unreadable ({code or err})")]
    if not js.get("enabled"):
        return [("INFO", "order WebSocket is off (DHAN_ORDER_WS_ENABLED not 1) - nothing to verify")]
    verdict = js.get("layout_verdict", "?")
    line = f"order-WS layout_verdict={verdict} running={js.get('running')} unparsed={js.get('unparsed')}"
    if verdict == "suspect":
        return [("FAIL", line + " - paste `samples` from /orders/ws-status")]
    return [("OK" if verdict == "ok" else "WARN", line)]


def check_quote_age(is_open: bool, fetch=get_json):
    code, js, err = fetch(base_url("market-data") + f"/quote/{QUOTE_SYMBOL}")
    if not isinstance(js, dict):
        return [("FAIL" if is_open else "WARN", f"/quote/{QUOTE_SYMBOL} unreadable ({code or err})")]
    age, src = js.get("age_s"), js.get("source")
    if age is None:
        return [("WARN", f"/quote/{QUOTE_SYMBOL} has no age_s (source={src})")]
    try:
        age = float(age)
    except (TypeError, ValueError):
        return [("WARN", f"/quote/{QUOTE_SYMBOL} age_s unreadable: {age!r}")]
    line = f"/quote/{QUOTE_SYMBOL} age_s={age} source={src}"
    if is_open and age > QUOTE_MAX_AGE_S:
        return [("FAIL", line + f" - older than {QUOTE_MAX_AGE_S:.0f}s during market hours")]
    return [("OK", line)]


def check_data_sources(fetch=get_json):
    code, js, err = fetch(base_url("market-data") + "/internal/data-sources")
    if not isinstance(js, dict):
        return [("WARN", f"/internal/data-sources unreadable ({code or err})")]
    serving = js.get("serving") or js.get("quote_serving") or js.get("active")
    return [("INFO", f"data sources: serving={serving if serving is not None else 'see /internal/data-sources'}")]


def check_host():
    out = []
    try:
        st = os.statvfs("/")
        free_pct = 100.0 * st.f_bavail / max(1, st.f_blocks)
        out.append(("OK" if free_pct >= 15 else "WARN", f"disk free {free_pct:.0f}%"))
    except Exception as e:  # noqa: BLE001
        out.append(("WARN", f"disk check failed: {type(e).__name__}"))
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for ln in f:
                k, _, v = ln.partition(":")
                mem[k] = int(v.split()[0])
        avail = mem.get("MemAvailable", 0) / 1024
        out.append(("OK" if avail >= 400 else "WARN", f"memory available {avail:.0f} MiB, swap used {(mem.get('SwapTotal', 0) - mem.get('SwapFree', 0)) / 1024:.0f} MiB"))
    except Exception:  # noqa: BLE001 - not Linux: skip silently, it is an extra
        pass
    return out


def scan_log_text(text: str, patterns=LOG_PATTERNS, limit: int = LOG_LINES_SHOWN):
    """Lines of `text` that contain any pattern, newest last, capped. Pure, so it is unit-tested."""
    hits = [ln.rstrip() for ln in text.splitlines() if any(p in ln for p in patterns)]
    return hits[-limit:], len(hits)


def check_docker_logs(since: str = "20m", runner=subprocess.run):
    try:
        r = runner(["docker", "compose", "logs", "--no-color", "--since", since], capture_output=True, text=True, timeout=60)
    except Exception as e:  # noqa: BLE001
        return [("WARN", f"docker logs not read: {type(e).__name__} (run from ~/stockky-v2, or pass --no-docker)")]
    if getattr(r, "returncode", 1) != 0:
        return [("WARN", f"docker compose logs failed: {(r.stderr or '').strip()[:120]}")]
    hits, total = scan_log_text(r.stdout or "")
    if not total:
        return [("OK", f"docker logs (last {since}): no FAIL / suspect / CRITICAL lines")]
    out = [("WARN", f"docker logs (last {since}): {total} line(s) with FAIL / suspect / CRITICAL, last {len(hits)} below")]
    out += [("LOG", ln[:300]) for ln in hits]
    return out


def run(label: str, use_docker: bool = True, now: datetime | None = None, fetch=get_json, runner=subprocess.run):
    now = now or datetime.now(timezone.utc)
    is_open = market_is_open_now(now)
    lines = [("INFO", f"stockky monday_check label={label} at {now.astimezone(IST):%Y-%m-%d %H:%M:%S} IST, clock says market {'OPEN' if is_open else 'CLOSED'}")]
    lines += check_health(fetch)
    lines += check_session(label, fetch, now)
    lines += check_quote_age(is_open, fetch)
    lines += check_depth20(fetch)
    lines += check_order_ws(fetch)
    lines += check_data_sources(fetch)
    lines += check_host()
    if use_docker:
        lines += check_docker_logs(runner=runner)
    return lines


def render(lines) -> str:
    fails = sum(1 for lv, _ in lines if lv == "FAIL")
    warns = sum(1 for lv, _ in lines if lv == "WARN")
    body = [f"{lv:<4} {tx}" if lv != "LOG" else f"     | {tx}" for lv, tx in lines]
    body.append(f"SUMMARY fail={fails} warn={warns}" + ("  -> paste every FAIL / WARN line here" if fails or warns else "  -> nothing to paste"))
    return "\n".join(body) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--label", default="manual", help="0930 | 1015 | anything (used in the log file name)")
    ap.add_argument("--out-dir", default=os.path.expanduser("~/check_logs"))
    ap.add_argument("--no-docker", action="store_true", help="skip the docker compose log scan")
    a = ap.parse_args(argv)
    lines = run(a.label, use_docker=not a.no_docker)
    text = render(lines)
    sys.stdout.write(text)
    try:
        os.makedirs(a.out_dir, exist_ok=True)
        path = os.path.join(a.out_dir, f"{datetime.now(IST):%Y-%m-%d}_{a.label}ist_live.log")
        with open(path, "w") as f:
            f.write(text)
        print(f"(saved to {path})")
    except OSError as e:
        print(f"(could not save the log: {e})")
    return 1 if any(lv == "FAIL" for lv, _ in lines) else 0


if __name__ == "__main__":
    sys.exit(main())
