#!/usr/bin/env python3
"""guard_log_summary.py - after a session, count the round 590 / 591 guard outcomes in the container logs (round 596, read-only).

  python3 scripts/guard_log_summary.py                 # last 8 hours of real-trade + position-stocks logs (docker logs)
  python3 scripts/guard_log_summary.py --since 24h     # a longer window
  python3 scripts/guard_log_summary.py --file rt.log --file ps.log     # logs you already saved (no docker needed)

For each token it prints how many log lines contain it and up to 3 raw sample lines (trimmed). It does not parse symbols or
times, so a changed log format cannot make it lie: it only counts and shows the lines. Zero is shown as 0, never hidden.
Tokens: WINDOW_WAIT, WINDOW_NO_SETUP, no_setup_window, PENNY_TICK, MIN_NOTIONAL. Exit 0 always (it is a report, not a gate).
Stdlib only; reads logs, changes nothing.
"""
from __future__ import annotations

import argparse
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since", default="8h", help="docker logs --since value (default 8h)")
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
    for name, c in CONTAINERS:
        lines, err = docker_lines(c, a.since)
        print(err and f"== {name} ==\n  {err}" or format_block(name, summarize(lines)))
    print("\nA line count is not a trade count: one candidate can log the same refusal every cycle. Compare with daily_trade_report.py.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
