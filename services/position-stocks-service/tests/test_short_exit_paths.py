"""Short selling P1 item 10 (E102): the REAL exit code run against short positions.
 1. scripts/test_short_exit_paths.py in a clean interpreter (it fakes the database, ledger and broker in sys.modules, so it must not share this process);
 2. static rails: the 14:55 short cover is wired into both trading loops and reads its time at call time."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT.parent.parent


def test_behaviour_script_passes_in_a_clean_interpreter():
    r = subprocess.run([sys.executable, "-I", str(REPO / "scripts" / "test_short_exit_paths.py")], cwd=str(ROOT), capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, (r.stdout + r.stderr)[-3000:]
    assert re.search(r"ALL \d+ OK", r.stdout), r.stdout[-1500:]
    assert "FAIL" not in r.stdout


def test_short_cover_is_wired_into_both_loops_before_the_eod_sweep():
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    assert main.count("eod_squareoff.run_short_squareoff") == 2          # the fast reconcile loop and the trading cycle
    assert main.index("eod_squareoff.run_short_squareoff") < main.index("eod_squareoff.run_eod_squareoff")


def test_short_cover_time_is_read_at_call_time_and_only_shorts_are_covered():
    src = (ROOT / "orders" / "eod_squareoff.py").read_text(encoding="utf-8")
    body = src[src.index("def run_short_squareoff"):src.index("def run_stagnation_exit")]
    assert 'getattr(config, "SHORT_SQUAREOFF_TIME_IST"' in body and "if not _side.is_short(pos):" in body
    assert 'close_position_now(db, pos, exit_reason="EOD_SQUAREOFF")' in body
    assert "place_order(" not in body            # no order of its own: the one proven close path only
