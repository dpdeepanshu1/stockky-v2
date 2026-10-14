#!/usr/bin/env python3
"""gen_technical_baseline.py - freeze the CLASSIC /analyze field set of the technical service as a golden file.

  python3 scripts/gen_technical_baseline.py            # check: regenerate in memory, compare with tests/golden/technical_baseline.json (exit 1 on drift)
  python3 scripts/gen_technical_baseline.py --update   # rewrite the golden file (ONLY when the classic scoring is changed on purpose)

35 cases: 25 seeded daily frames, 8 threshold/weight override variants, 2 thin-history paths. Everything except the structure keys
(market_structure / structure_summary / structure_truncated) is frozen, so adding the structure block can never change a classic field.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AIS = ROOT / "services" / "analysis-intelligence-service"
GOLDEN = AIS / "tests" / "golden" / "technical_baseline.json"
sys.path.insert(0, str(AIS / "tests"))

import _analyze_harness as H  # noqa: E402


def build() -> dict:
    logging.disable(logging.CRITICAL)
    m = H.load_main()
    out = {}
    for c in H.cases():
        out[c["name"]] = H.legacy_fields(H.run(c, structure=True, main=m))
    return out


def main(argv: list[str]) -> int:
    cur = build()
    text = json.dumps(cur, indent=1, sort_keys=True) + "\n"
    if "--update" in argv:
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(text, encoding="utf-8")
        print(f"wrote {GOLDEN.relative_to(ROOT)} ({len(cur)} cases)")
        return 0
    if not GOLDEN.exists():
        print(f"DRIFT: {GOLDEN} is missing - run with --update")
        return 1
    if json.loads(GOLDEN.read_text(encoding="utf-8")) != json.loads(text):
        print(f"DRIFT: legacy /analyze fields differ from {GOLDEN.relative_to(ROOT)}")
        return 1
    print("ok: legacy /analyze fields match the golden")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
