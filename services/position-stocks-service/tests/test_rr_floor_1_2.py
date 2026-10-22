"""2026-10-22 reward:risk 1:2 in position-stocks: orders/rr_floor.py (pure) + the shipped defaults + the entry wiring (source level).
No sqlalchemy / httpx needed for the pure parts. Runs with pytest or `python3 tests/test_rr_floor_1_2.py`."""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from orders import rr_floor  # noqa: E402


def _lv(t, s):
    return NS(target_pct=t, stop_pct=s)


def test_two_to_one_passes_and_below_is_rejected():
    assert rr_floor.reject_reason(_lv(4.0, 2.0), 2.0) is None
    assert rr_floor.reject_reason(_lv(5.0, 2.0), 2.0) is None
    r = rr_floor.reject_reason(_lv(2.5, 1.5), 2.0)          # the case a target cap + stop floor can leave: 1.67:1
    assert r and r.startswith("RR_FLOOR") and "1.67" in r
    assert rr_floor.reject_reason(_lv(3.0, 2.0), 2.0)        # 1.5:1


def test_rounding_tolerance_and_off_switch():
    assert rr_floor.reject_reason(_lv(1.9962, 1.0), 2.0) is None   # 4-dp rounding must not reject a 2:1 level
    assert rr_floor.reject_reason(_lv(1.9, 1.0), 2.0)
    assert rr_floor.reject_reason(_lv(1.0, 2.0), 0.0) is None       # 0 = off


def test_bad_levels_never_raise_and_never_block():
    for bad in (None, NS(), NS(target_pct="x", stop_pct=1), NS(target_pct=2, stop_pct=0), NS(target_pct=float("nan"), stop_pct=1)):
        assert rr_floor.reject_reason(bad, 2.0) is None


def _src(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_shipped_defaults_are_one_to_two():
    cfg = _src("config.py")
    assert re.search(r'ENTRY_MIN_REWARD_RISK\s*=\s*max\(0\.0,\s*_get_float\("ENTRY_MIN_REWARD_RISK",\s*2\.0\)\)', cfg)
    assert re.search(r'ADAPTIVE_BAR_TARGET_RR\s*=\s*_get_float\("ADAPTIVE_BAR_TARGET_RR",\s*2\.0\)', cfg)
    assert re.search(r'SCALP_TARGET_CAP_MIN_RR\s*=\s*_get_float\("SCALP_TARGET_CAP_MIN_RR",\s*2\.0\)', cfg)
    assert 'MIN_REWARD_RISK = 2.0' in _src("orders/adaptive.py")


def test_entry_skips_on_the_final_levels_before_any_capital_is_reserved():
    src = _src("orders/entry.py")
    ast.parse(src)
    i_levels = src.index("levels: AdaptiveLevels = compute_levels(\n        candidate.pct_change")
    i_rr = src.index("_rr_reject = rr_floor.reject_reason(levels)")
    i_depth = src.index("_depth_reject = depth_gate.reject_reason(candidate.symbol)")
    i_reserve = src.index("reserve_capital(", i_levels)
    assert i_levels < i_rr < i_depth and i_rr < i_reserve
    block = src[i_rr: i_rr + 400]
    assert "shared_symbol_lock.release(db, candidate.symbol)" in block and '"SKIPPED"' in block and "return None" in block
    assert "rr_floor" in src.split("from orders.adaptive")[0]


def test_bar_level_floor_follows_the_entry_floor():
    s = _src("orders/adaptive.py")
    assert 'max(1.2, float(getattr(config, "ENTRY_MIN_REWARD_RISK", 0.0) or 0.0))' in s


def test_catalog_flag_matches_config_default():
    cat = _src("control/catalog.py")
    m = re.search(r'Flag\("ENTRY_MIN_REWARD_RISK".*?shipped="([^"]+)"', cat, re.S)
    assert m and float(m.group(1)) == 2.0


if __name__ == "__main__":
    import traceback
    ok = bad = 0
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            try:
                f(); ok += 1
            except Exception:  # noqa: BLE001
                bad += 1; print("FAIL", n); traceback.print_exc()
    print(f"{ok} passed {bad} failed"); sys.exit(1 if bad else 0)
