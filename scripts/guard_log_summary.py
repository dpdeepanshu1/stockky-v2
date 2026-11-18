#!/usr/bin/env python3
"""guard_log_summary.py - after a session, count the round 590 / 591 guard outcomes (round 596; round 609 added the decisions tables). Read-only.

  python3 scripts/guard_log_summary.py                 # last 8 hours: the decisions tables AND the container logs
  python3 scripts/guard_log_summary.py --since 24h     # a longer window (30m, 8h, 2d)
  python3 scripts/guard_log_summary.py --source db     # only the decisions tables (real-trade trade_decisions, position-stocks scalp_candidate_log)
  python3 scripts/guard_log_summary.py --source logs   # only the container logs (the old behaviour)
  python3 scripts/guard_log_summary.py --file rt.log --file ps.log     # logs you already saved (no docker needed)

Why the tables: these refusals are written to the decisions tables (gate_tag / reasoning / reason), not to the container log, so a log-only count read 0
on a day with 127 no_setup_window rows (18 Nov 2026). The table count shows ROWS (a refusal row is written every cycle) and DISTINCT SYMBOLS (the number
that compares with a trade count), plus up to 3 sample rows. The log count is lines containing the token, up to 3 raw lines. Neither parses the format
beyond the token, so a changed format cannot make a count lie. Zero is shown as 0, never hidden; a table that cannot be read says why instead of showing 0.
Tokens: WINDOW_WAIT, WINDOW_NO_SETUP, no_setup_window, PENNY_TICK, MIN_NOTIONAL. Exit 0 always (it is a report, not a gate).
Stdlib only (the table read runs inside the service container through `docker compose exec`); reads, changes nothing.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from typing import Dict, List, Tuple

TOKENS = ("WINDOW_WAIT", "WINDOW_NO_SETUP", "no_setup_window", "PENNY_TICK", "MIN_NOTIONAL")
CONTAINERS = (("real-trade", "stockky-v2-real-trade-service-1"), ("position-stocks", "stockky-v2-position-stocks-service-1"))


def summarize(lines: List[str], samples: int = 3, width: int = 200) -> Dict[str, Tuple[int, List[str]]]:
    """{token: (number of lines containing it, first `samples` of those lines trimmed to `width`)}; every token is present, zero included."""
    out: Dict[str, Tuple[int, List[str]]] = {t: (0, []) for t in TOKENS}
    for ln in lines:
        for t in TOKENS:
            if t in ln:
                n, s = out[t]
                out[t] = (n + 1, s + [ln.strip()[:width]] if len(s) < samples else s)
    return out


def format_block(name: str, res: Dict[str, Tuple[int, List[str]]]) -> str:
    L = [f"== {name} =="]
    for t in TOKENS:
        n, s = res[t]
        L.append(f"  {t:<17}{n:>5}")
        L += [f"      {x}" for x in s]
    return "\n".join(L)


def docker_lines(container: str, since: str) -> Tuple[List[str], str]:
    try:
        p = subprocess.run(["docker", "logs", "--since", since, container], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        return [], f"could not run docker logs for {container}: {e}"
    if p.returncode != 0:
        return [], f"docker logs {container} failed: {(p.stderr or '').strip()[:160]}"
    return (p.stdout + "\n" + p.stderr).splitlines(), ""


# ---- round 609: the decisions tables ---------------------------------------------------------------------------------------------------------
DB_SERVICES = (("real-trade", "real-trade-service"), ("position-stocks", "position-stocks-service"))
MAX_DB_ROWS = 20000

# Runs INSIDE the service container (python3 - on stdin). Prints ONE json line: {"rows": [[symbol, text, iso_time], ...]} or {"error": "..."}.
# real-trade: REAL ENTRY rows of trade_decisions (gate_tag, reasoning, risk_verdict_reason). position-stocks: scalp_candidate_log (reason).
_DB_SNIPPET = r"""
import json, os
from datetime import datetime, timedelta
try:
    from db import get_session_factory
    from sqlalchemy import MetaData, Table, select, or_
    toks = json.loads(os.environ["GLS_TOKENS"])
    cut = datetime.utcnow() - timedelta(seconds=int(os.environ["GLS_SECONDS"]))
    lim = int(os.environ["GLS_LIMIT"])
    s = get_session_factory()()
    bind = s.get_bind()
    if os.environ["GLS_KIND"] == "real":
        t = Table("trade_decisions", MetaData(), autoload_with=bind)
        cols = [t.c["gate_tag"], t.c["reasoning"], t.c["risk_verdict_reason"]]
        q = select(t.c["symbol"], t.c["created_at"], *cols).where(t.c["mode"] == "REAL", t.c["decision_type"] == "ENTRY", t.c["created_at"] >= cut)
    else:
        t = Table("scalp_candidate_log", MetaData(), autoload_with=bind)
        cols = [t.c["reason"]]
        q = select(t.c["symbol"], t.c["created_at"], *cols).where(t.c["created_at"] >= cut)
    q = q.where(or_(*[c.like("%" + k + "%") for c in cols for k in toks])).order_by(t.c["created_at"]).limit(lim)
    rows = []
    for r in s.execute(q):
        rows.append([r[0], " | ".join(str(x) for x in r[2:] if x), r[1].isoformat() if r[1] else ""])
    print(json.dumps({"rows": rows}))
except Exception as e:
    print(json.dumps({"error": type(e).__name__ + ": " + str(e)[:200]}))
"""


def parse_since(text: str) -> int:
    """Seconds in a --since value like 30m / 8h / 2d (a bare number = hours). Bad value -> 8 hours."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([mhd]?)\s*", str(text or ""))
    if not m:
        return 8 * 3600
    n, unit = float(m.group(1)), m.group(2) or "h"
    return max(60, int(n * {"m": 60, "h": 3600, "d": 86400}[unit]))


def _ist_hhmmss(iso: str) -> str:
    """HH:MM:SS IST of a UTC ISO time from the table (created_at is stored in UTC); the raw text when it cannot be read."""
    try:
        from datetime import datetime, timedelta
        return (datetime.fromisoformat(iso) + timedelta(hours=5, minutes=30)).strftime("%H:%M:%S")
    except (TypeError, ValueError):
        return (iso or "")[11:19]


def summarize_rows(rows: List[list], samples: int = 3, width: int = 200) -> Dict[str, Tuple[int, int, List[str]]]:
    """rows = [[symbol, text, time], ...] -> {token: (rows containing it, distinct symbols among them, first `samples` rows trimmed)}. Every token is present."""
    out: Dict[str, Tuple[int, int, List[str]]] = {t: (0, 0, []) for t in TOKENS}
    seen: Dict[str, set] = {t: set() for t in TOKENS}
    for r in rows or []:
        try:
            sym, text, ts = str(r[0] or ""), str(r[1] or ""), str(r[2] or "")
        except (IndexError, TypeError):
            continue
        for t in TOKENS:
            if t in text:
                n, _d, smp = out[t]
                seen[t].add(sym)
                if len(smp) < samples:
                    smp = smp + [f"{_ist_hhmmss(ts)} {sym}: {text.strip()}"[:width]]
                out[t] = (n + 1, len(seen[t]), smp)
    return out


def format_db_block(name: str, res: Dict[str, Tuple[int, int, List[str]]], err: str = "") -> str:
    L = [f"== {name} (decisions table) =="]
    if err:
        return "\n".join(L + [f"  {err}"])
    for t in TOKENS:
        n, d, s = res[t]
        L.append(f"  {t:<17}{n:>5} rows {d:>4} symbols")
        L += [f"      {x}" for x in s]
    return "\n".join(L)


def db_rows(service: str, kind: str, seconds: int) -> Tuple[List[list], str]:
    """(rows, error) from the service's decisions table via `docker compose exec`. Never raises."""
    try:
        p = subprocess.run(
            ["docker", "compose", "exec", "-T", "-e", f"GLS_TOKENS={json.dumps(list(TOKENS))}", "-e", f"GLS_SECONDS={seconds}",
             "-e", f"GLS_KIND={kind}", "-e", f"GLS_LIMIT={MAX_DB_ROWS}", service, "python3", "-"],
            input=_DB_SNIPPET, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as e:
        return [], f"could not read the table through docker compose exec {service}: {e}"
    out = (p.stdout or "").strip().splitlines()
    if p.returncode != 0 or not out:
        return [], f"docker compose exec {service} failed: {(p.stderr or '').strip()[:160] or 'no output'} (run from ~/stockky-v2)"
    try:
        body = json.loads(out[-1])
    except ValueError:
        return [], f"unreadable answer from {service}: {out[-1][:120]}"
    if body.get("error"):
        return [], f"table read failed in {service}: {body['error']}"
    return list(body.get("rows") or []), ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since", default="8h", help="window, e.g. 30m, 8h, 2d (default 8h; docker logs --since for the log count)")
    ap.add_argument("--source", choices=("both", "db", "logs"), default="both", help="decisions tables, container logs, or both (default)")
    ap.add_argument("--file", action="append", help="a saved log file instead of docker (repeat for several)")
    a = ap.parse_args(argv)
    if a.file:
        for f in a.file:
            try:
                lines = open(f, encoding="utf-8", errors="replace").read().splitlines()
            except OSError as e:
                print(f"== {f} ==\n  cannot read: {e}")
                continue
            print(format_block(f, summarize(lines)))
        return 0
    if a.source in ("both", "db"):
        secs = parse_since(a.since)
        for (name, svc), kind in zip(DB_SERVICES, ("real", "position")):
            rows, err = db_rows(svc, kind, secs)
            print(format_db_block(name, summarize_rows(rows), err))
        print()
    if a.source in ("both", "logs"):
        for name, c in CONTAINERS:
            lines, err = docker_lines(c, a.since)
            print(err and f"== {name} (container log) ==\n  {err}" or format_block(f"{name} (container log)", summarize(lines)))
    print("\nA row or line count is not a trade count: one candidate can log the same refusal every cycle. 'symbols' is the closer number. Compare with daily_trade_report.py.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
