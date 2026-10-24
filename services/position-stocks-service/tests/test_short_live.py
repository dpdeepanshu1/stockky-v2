"""Short selling step 8/9: orders/short_live.py. Two parts:
 1. the behaviour script scripts/test_short_live.py, run in a clean interpreter (it fakes sys.modules, so it must not share this pytest process);
 2. static rails read from the source: no plain-order fallback, a broker stop is mandatory, the long entry is untouched, the size cap and the mode are
    checked, SELL only, INTRADAY only. These run anywhere (no SQLAlchemy)."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT.parent.parent
SRC = (ROOT / "orders" / "short_live.py").read_text(encoding="utf-8")


def test_behaviour_script_passes_in_a_clean_interpreter():
    r = subprocess.run([sys.executable, "-I", str(REPO / "scripts" / "test_short_live.py")], cwd=str(ROOT), capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, (r.stdout + r.stderr)[-3000:]
    assert re.search(r"ALL \d+ OK", r.stdout), r.stdout[-1500:]
    assert "FAIL" not in r.stdout


def test_the_only_order_call_is_a_sell_super_order_with_intraday_product():
    code = re.sub(r'""".*?"""', "", SRC, flags=re.S)
    code = "\n".join(l for l in code.splitlines() if not l.strip().startswith("#"))
    assert code.count("place_super_order(") == 1
    assert "place_order(" not in code, "no plain-order short: it would have no broker stop"
    assert 'transaction_type="SELL"' in code and 'product_type="INTRADAY"' in code and 'transaction_type="BUY"' not in code
    assert "convert_position" not in code and "CNC" not in code


def test_a_stop_leg_is_always_sent_and_super_order_is_required():
    assert "stop_loss_price=float(levels.stop_price)" in SRC and "target_price=float(levels.target_price)" in SRC
    assert "if not config.USE_SUPER_ORDER:" in SRC


def test_size_cap_mode_and_ledger_are_enforced():
    assert "min(int(plan[\"qty\"]), qty_cap())" in SRC
    assert 'ledger.reserve_capital(db, adaptive_stop_pct=float(plan.get("budget_stop_pct") or levels.stop_pct), side="SHORT")' in SRC
    assert "if not live_mode():" in SRC
    assert re.search(r'return max\(1, v\)', SRC) and re.search(r'except Exception:  # noqa: BLE001\n\s+return 1', SRC)      # junk cap = 1 share, never "no cap"


def test_the_long_entry_is_untouched_and_does_not_know_the_live_short():
    entry = (ROOT / "orders" / "entry.py").read_text(encoding="utf-8")
    assert "short_live" not in entry
    assert 'transaction_type="SELL"' not in entry


def test_only_the_cycle_hands_candidates_to_the_live_entry():
    users = [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*.py") if re.search(r"from orders import[^\n]*short_live|import short_live|from orders\.short_live", p.read_text(encoding="utf-8", errors="ignore"))
             and not p.relative_to(ROOT).as_posix().startswith("tests/") and p.name != "short_live.py"]
    assert users == ["orders/short_entry.py"], users


def test_shadow_mode_view_still_reads_live_as_off():
    src = (ROOT / "orders" / "short_entry.py").read_text(encoding="utf-8")
    assert 'return "shadow" if m == "shadow" else "off"' in src       # attempt_short_shadow can never place an order, whatever the switch says


def test_the_cap_switch_is_in_the_catalog_and_protected_from_the_calibrator():
    cat = (ROOT / "control" / "catalog.py").read_text(encoding="utf-8")
    assert re.search(r'Flag\("SHORT_LIVE_QTY_CAP".*?shipped="1"', cat, re.S)
    auto = (REPO / "services" / "calibration-runner-service" / "autocalib.py").read_text(encoding="utf-8")
    assert '"SHORT_LIVE_QTY_CAP":' in auto


def test_step12_size_comes_from_the_normal_stop_and_the_catalog_has_the_two_switches():
    se = (ROOT / "orders" / "short_entry.py").read_text(encoding="utf-8")
    assert "value = _short_position_value(db, budget_stop_pct)" in se and "budget_stop_pct = float(levels.stop_pct)" in se
    assert se.index("budget_stop_pct = float(levels.stop_pct)") < se.index("tighten_short_levels(levels, live, mult)")      # captured BEFORE the tightening
    cat = (ROOT / "control" / "catalog.py").read_text(encoding="utf-8")
    auto = (REPO / "services" / "calibration-runner-service" / "autocalib.py").read_text(encoding="utf-8")
    for k, v in (("SHORT_STOP_MULT_STRESSED", "0.8"), ("SHORT_STOP_MULT_PANIC", "0.6")):
        assert re.search(r'Flag\("%s".*?lo=0.3, hi=1.0, looser="higher".*?shipped="%s"' % (k, v), cat, re.S), k
        assert f'"{k}":' in auto, k
