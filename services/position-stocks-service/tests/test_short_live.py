"""Short selling step 8/9: orders/short_live.py. Two parts:
 1. the behaviour script scripts/test_short_live.py, run in a clean interpreter (it fakes sys.modules, so it must not share this pytest process);
 2. static rails read from the source: the plain-order fallback lives only in orders/plain_short.py (E114), a broker stop is mandatory, the long entry is untouched, the size cap and the mode are
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


def test_the_only_order_calls_are_a_sell_super_order_and_the_plain_short_module():
    # E114: short_live.py itself still places exactly one order (the SELL Super Order). The plain-order fallback lives in orders/plain_short.py, the only other place.
    code = re.sub(r'''""".*?"""''', "", SRC, flags=re.S)
    code = "\n".join(l for l in code.splitlines() if not l.strip().startswith("#"))
    assert code.count("place_super_order(") == 1
    assert "place_order(" not in code, "plain orders are placed only by orders/plain_short.py (it also places the broker stop)"
    assert code.count("plain_short.open_plain_short(") == 1
    assert 'transaction_type="SELL"' in code and 'product_type="INTRADAY"' in code and 'transaction_type="BUY"' not in code
    assert "convert_position" not in code and "CNC" not in code


def test_a_stop_is_always_sent_and_the_fallback_is_gated():
    assert "stop_loss_price=float(levels.stop_price)" in SRC and "target_price=float(levels.target_price)" in SRC
    assert "if not config.USE_SUPER_ORDER and not plain_short.entry_allowed():" in SRC
    ps = (ROOT / "orders" / "plain_short.py").read_text(encoding="utf-8")
    code = re.sub(r'''""".*?"""''', "", ps, flags=re.S)
    assert code.count("place_intraday_buy_stop(") >= 2 and "INTRADAY" in code and "CNC" not in code.replace("cancel_cnc_stop_loss_order", "")
    assert "plain_short.refusal_allows_fallback(error_msg, ambiguous)" in SRC and "ambiguous" in code
    assert "if not entry_allowed() or ambiguous:" in code, "an ambiguous error, a switched-off fallback and a tripped day breaker must never reach a plain SELL"
    assert "_tripped_day[0] = _today()" in code, "a stop that could not be placed must trip the day breaker"
    assert "plain_short.watch_orphan(" in SRC, "a SELL whose state is unknown must be put on the watch list"


def test_the_plain_short_guard_is_wired_and_the_cover_releases_the_stop_first():
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "plain_short.run_plain_short_guard" in main
    eod = (ROOT / "orders" / "eod_squareoff.py").read_text(encoding="utf-8")
    assert "plain_short.release_stop_before_cover(db, pos)" in eod
    assert eod.index("release_stop_before_cover") < eod.index("exit_side = _side.exit_transaction_type(pos)"), "the resting stop must be released BEFORE the cover BUY is built"


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
    assert re.search(r'Flag\("SHORT_LIVE_QTY_CAP".*?shipped="1000"', cat, re.S)
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


def test_e221_no_order_id_is_ambiguous_and_no_phantom_position():
    # E221: a 2xx Super Order answer without an id is looked up at Dhan (force=True) and never retried as a plain SELL; no SELL sent = refusal, not a saved position.
    assert "force=True" in SRC and "force: bool = False" in SRC and "if not force and not dhan_client.is_ambiguous_placement_error(exc):" in SRC
    i = SRC.index('error_msg = "Dhan answered without an order id"')
    assert "ambiguous = True" in SRC[i:i + 700]
    assert "if error_msg is None and not super_id and plain is None:" in SRC
    assert SRC.index("if error_msg is None and not super_id and plain is None:") < SRC.index("# the order is out: count it, then record the position")


def test_e222_live_short_obeys_service_and_autopilot_switches():
    # E222: a live short is an automatic entry; it must read the same REAL gate row and the same three switches as main._run_cycle.
    assert 'filter_by(mode="REAL")' in SRC and "db.query(ScalpGateState).first()" not in SRC
    assert 'getattr(gate, "service_enabled", True)' in SRC and 'getattr(gate, "auto_pilot_enabled", True)' in SRC
    assert "off = _switches_off(gate)" in SRC, "_live_guards must call the switch check"
    se = (ROOT / "orders" / "short_entry.py").read_text(encoding="utf-8")
    assert "short_live as _sl_off" in se and "automatic_entry_off" in se, "the short cycle must skip its scan while the switches say no"
