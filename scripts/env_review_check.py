#!/usr/bin/env python3
"""env_review_check.py - read-only check of the VM .env for values that undo the 20 Oct review fixes (groups 470-472).

  python3 scripts/env_review_check.py ~/stockky-v2/.env [--strict]

A value in .env beats the shipped default (docker compose env_file), so an old line such as OVERNIGHT_HOLD_ENABLED=true or BROKER_STOP_ENABLED=false
silently undoes a fix. Prints one line per such value:  LOOSENS (less protection than the default)  |  TIGHTENS  |  OVERRIDE (different, neutral).
Also warns when BROKER_STOP_ENABLED is absent (the code default is true since group 377, but confirm with GET /review-fixes/status).
Exit code 0 normally; with --strict, 1 when any LOOSENS / TIGHTENS / OVERRIDE line is printed. Nothing is changed. Secrets are never printed.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Callable, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from env_structure_check import parse_env  # noqa: E402

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def _bool(default_true: bool, note: str) -> Callable[[str], Optional[Tuple[str, str]]]:
    def f(v: str):
        x = v.strip().lower()
        if x in _TRUE:
            return None if default_true else ("LOOSENS", note)
        if x in _FALSE:
            return ("LOOSENS", note) if default_true else None
        return ("OVERRIDE", "not a recognised true/false value")
    return f


def _num_min(default: float, note: str) -> Callable[[str], Optional[Tuple[str, str]]]:
    """Lower than the default = LOOSENS, higher = TIGHTENS."""
    def f(v: str):
        try:
            x = float(v.strip())
        except ValueError:
            return ("OVERRIDE", "not a number")
        if x == default:
            return None
        return ("LOOSENS", note) if x < default else ("TIGHTENS", note)
    return f


def _cutoff(default: str, note: str) -> Callable[[str], Optional[Tuple[str, str]]]:
    def f(v: str):
        x = v.strip()
        if x == default:
            return None
        if not x:
            return ("LOOSENS", "blank = no general entry cut-off")
        try:
            h, m = x.split(":")
            a, b = int(h) * 60 + int(m), int(default[:2]) * 60 + int(default[3:])
        except ValueError:
            return ("OVERRIDE", "unreadable time, the code falls back to the default")
        return ("LOOSENS", note) if a > b else ("TIGHTENS", note)
    return f


def _orb(v: str):
    x = v.strip().lower()
    return None if x == "shadow" else (("OVERRIDE", "off = the opening-range shadow record is not written") if x == "off" else ("OVERRIDE", "unknown value, falls back to shadow"))


# name -> (shown default, classifier)
RULES: Dict[str, Tuple[str, Callable[[str], Optional[Tuple[str, str]]]]] = {
    "OVERNIGHT_HOLD_ENABLED": ("false", _bool(False, "group470: one-day holds cost Rs16.8 charges each vs Rs2.0 same-day")),
    "ENTRY_LAST_TIME_IST": ("14:00", _cutoff("14:00", "group470: later = entries in the thin last hour")),
    "STOP_OVERSHOOT_ALLOWANCE_PCT": ("1.0", _num_min(1.0, "group470: sizing allowance for fills beyond the stop")),
    "ENTRY_MIN_QTY": ("3", _num_min(3, "group472: minimum shares per REAL entry")),
    "STOPOUT_GUARD_ENABLED": ("true", _bool(True, "group471: whole-day stop-out guard")),
    "STOPOUT_SYMBOL_BLOCK": ("true", _bool(True, "group471: no same-day re-buy after a stop-out")),
    "STOPOUT_DAY_PAUSE_N": ("3", lambda v: (None if v.strip() == "3" else _num_min_pause(v))),
    "BROKER_STOP_ENABLED": ("true", _bool(True, "no resting broker stop: only the 8 s software loop guards REAL positions")),
    "ORB_MODE": ("shadow", _orb),
}


def _num_min_pause(v: str):
    """STOPOUT_DAY_PAUSE_N: 0 = pause off (LOOSENS); a bigger number = later pause (LOOSENS); smaller positive = TIGHTENS."""
    try:
        x = int(float(v.strip()))
    except ValueError:
        return ("OVERRIDE", "not a number")
    if x <= 0 or x > 3:
        return ("LOOSENS", "group471: pause after N losing stop-outs (0 = off)")
    return ("TIGHTENS", "group471: pause after N losing stop-outs")


def check(env: Dict[str, str]) -> List[str]:
    out: List[str] = []
    for k, (default, cls) in RULES.items():
        if k not in env:
            if k == "BROKER_STOP_ENABLED":
                out.append("INFO  BROKER_STOP_ENABLED is not in .env (code default true): confirm with GET /review-fixes/status that broker_stop_enabled is true")
            continue
        r = cls(env[k])
        if r:
            out.append(f"{r[0]}  {k}={env[k]}  (default {default}; {r[1]})")
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("env_file")
    ap.add_argument("--strict", action="store_true")
    a = ap.parse_args(argv)
    try:
        with open(a.env_file, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as e:
        print(f"FAIL cannot read {a.env_file}: {e}")
        return 2
    findings = check(parse_env(text))
    if not findings:
        print("OK  nothing in .env overrides the group 470-472 defaults")
        return 0
    for f in findings:
        print(f)
    return 1 if (a.strict and any(not f.startswith("INFO") for f in findings)) else 0


if __name__ == "__main__":
    sys.exit(main())
