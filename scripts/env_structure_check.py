#!/usr/bin/env python3
"""env_structure_check.py - read-only check of the VM .env for STRUCTURE_* / 52-week values that override the shipped defaults.

  python3 scripts/env_structure_check.py ~/stockky-v2/.env [--strict]

Replaces the manual `grep STRUCTURE_ .env` (docs/GROUP415). Prints every STRUCTURE_* / CANDIDATE_52W_* line it finds (values are not secrets, but
anything whose name contains KEY / TOKEN / SECRET / PASSWORD is never printed), then one line per value that differs from the intended default:
  OVERRIDE  the .env value beats the default (and is shown with what the default would give)
  LOOSENS   the override makes live gating looser than the shipped default
  TIGHTENS  stricter / more live than the default
Exit code: 0 normally; with --strict, 1 when any LOOSENS/TIGHTENS/OVERRIDE line is printed. Nothing is changed.

INTENDED holds the defaults as of group 434 (docker-compose / code). Update it with the default, not the other way round.
"""
from __future__ import annotations

import argparse
import re
import sys
from typing import Dict, List, Optional, Tuple

# name -> (intended default, {value: "LOOSENS"|"TIGHTENS"|"OVERRIDE"}, note). Values are compared lower-case.
INTENDED: Dict[str, Tuple[str, Dict[str, str], str]] = {
    "STRUCTURE_POLICY_MODE": ("record", {"full": "TIGHTENS", "off": "LOOSENS"}, "group434 compose default; full = rules live, off = kill switch"),
    "STRUCTURE_PREPARE_UPGRADE": ("0", {"1": "LOOSENS", "true": "LOOSENS", "on": "LOOSENS", "yes": "LOOSENS"}, "group415: P9 PREPARE -> BUY NOW off until G2"),
    "STRUCTURE_STOP_MODE": ("off", {"on": "TIGHTENS", "enforce": "TIGHTENS"}, "group415: structural stop off until G3"),
    "STRUCTURE_EXIT_MODE": ("log", {"exit": "TIGHTENS", "tighten": "TIGHTENS", "off": "OVERRIDE"}, "group415: record only"),
    "STRUCTURE_MAX_AGE_DAYS": ("5", {}, "group415: 5 clears every 2026 trading day (3 would stale every Monday)"),
    "STRUCTURE_MIN_RR": ("0", {}, "group424b: R:R floor off; a positive value turns the ratio test back on"),
    "STRUCTURE_RETEST_ENTRY": ("0", {"1": "LOOSENS", "true": "LOOSENS", "on": "LOOSENS", "yes": "LOOSENS"}, "group413: retest branch off, no replay evidence"),
    "STRUCTURE_REQUIRE_FOR_BUY_NOW": ("1", {"0": "LOOSENS", "false": "LOOSENS", "off": "LOOSENS", "no": "LOOSENS"}, "group377: BUY NOW needs usable structure"),
    "STRUCTURE_GATE_MODE": ("log", {"enforce": "TIGHTENS", "off": "OVERRIDE"}, "A9 (2026-10-22): entry structure gate is record-only until G2 benefit is proven out-of-sample; enforce = blocks entries, off = kill switch"),
    "CANDIDATE_52W_RULE_ENABLED": ("1", {"0": "OVERRIDE", "false": "OVERRIDE", "off": "OVERRIDE", "no": "OVERRIDE"}, "group440: rule hard-off (group420 recommended line) = nothing rejected AND nothing counted; compose default is log-only"),
    "CANDIDATE_52W_LOG_ONLY": ("1", {"0": "TIGHTENS", "false": "TIGHTENS", "off": "TIGHTENS", "no": "TIGHTENS"}, "group440 compose default: log-only (rule evaluated + counted, never rejects); 0 = the rule rejects again"),
    "CANDIDATE_52W_FRESH_HIGH_EXEMPT": ("1", {"0": "TIGHTENS", "false": "TIGHTENS", "off": "TIGHTENS", "no": "TIGHTENS"}, "group379 exemption on"),
}
_SECRET = re.compile(r"KEY|TOKEN|SECRET|PASSWORD", re.I)
_WATCH = re.compile(r"^(STRUCTURE_|CANDIDATE_52W_)")


def parse_env(text: str) -> Dict[str, str]:
    """Last assignment wins (as docker compose's env_file does). Strips `export `, quotes and trailing ` # comment` on unquoted values."""
    out: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        else:
            v = re.split(r"\s+#", v, maxsplit=1)[0].strip()
        out[k] = v
    return out


def check(env: Dict[str, str]) -> Tuple[List[str], List[str]]:
    """Returns (listing lines, finding lines)."""
    listing, findings = [], []
    for k in sorted(env):
        if _WATCH.match(k):
            listing.append(f"{k}={'<hidden>' if _SECRET.search(k) else env[k]}")
    for k, (default, kinds, note) in INTENDED.items():
        if k not in env:
            continue
        v = env[k]
        if v.strip().lower() == default.lower():
            continue
        kind = kinds.get(v.strip().lower(), "OVERRIDE")
        findings.append(f"{kind}  {k}={v}  (default {default}; {note})")
    for k in sorted(env):                                  # watched keys we have no intended value for: listed, not judged
        if _WATCH.match(k) and k not in INTENDED:
            findings.append(f"INFO  {k}={'<hidden>' if _SECRET.search(k) else env[k]}  (no intended default recorded here; compare with docs/ENV_REFERENCE.txt)")
    return listing, findings


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
    listing, findings = check(parse_env(text))
    print(f"{len(listing)} STRUCTURE_* / CANDIDATE_52W_* line(s) in {a.env_file}")
    for l in listing:
        print("  " + l)
    if not findings:
        print("OK  nothing overrides the shipped defaults")
        return 0
    for f in findings:
        print(f)
    return 1 if (a.strict and any(not f.startswith("INFO") for f in findings)) else 0


if __name__ == "__main__":
    sys.exit(main())
