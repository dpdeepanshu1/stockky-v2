"""2026-10-22 orders/live_anchor.py: stop / target follow the live price at order time (IONEXCHANG: signal 354.60, fill 352.30, stop 0.15 % under the fill)."""
from __future__ import annotations

import dataclasses
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from orders import live_anchor  # noqa: E402


@dataclasses.dataclass(frozen=True)
class Lv:
    target_pct: float
    stop_pct: float
    target_price: float
    stop_price: float
    basis: str = "bars"


def _tick(p):          # 0.05 tick, like a Rs 100-1000 stock
    return round(round(p / 0.05) * 0.05, 2)


def test_ionexchange_case_stop_is_planned_distance_from_the_live_price():
    sig, live = 354.60, 352.30
    lv = Lv(target_pct=1.6, stop_pct=0.8, target_price=_tick(sig * 1.016), stop_price=_tick(sig * 0.992))
    assert (live - lv.stop_price) / live * 100 < 0.2                       # the old, signal-anchored stop: 0.15 % under the fill
    out, drift = live_anchor.reanchor(lv, sig, live, enabled=True, round_fn=_tick)
    assert round(drift, 2) == -0.65
    assert abs((live - out.stop_price) / live * 100 - 0.8) < 0.05
    assert abs((out.target_price - live) / live * 100 - 1.6) < 0.05
    assert (out.stop_pct, out.target_pct, out.basis) == (0.8, 1.6, "bars")   # percentages and basis untouched, so reward:risk is unchanged


def test_price_above_signal_moves_legs_up():
    lv = Lv(1.6, 0.8, _tick(100 * 1.016), _tick(100 * 0.992))
    out, drift = live_anchor.reanchor(lv, 100.0, 100.2, enabled=True, round_fn=_tick)
    assert drift > 0 and out.stop_price > lv.stop_price and out.target_price > lv.target_price


def test_unchanged_when_off_no_tick_or_tiny_drift():
    lv = Lv(1.6, 0.8, 101.6, 99.2)
    assert live_anchor.reanchor(lv, 100.0, 99.0, enabled=False) == (lv, None)
    assert live_anchor.reanchor(lv, 100.0, None, enabled=True) == (lv, None)
    assert live_anchor.reanchor(lv, 100.0, 100.01, enabled=True, round_fn=_tick) == (lv, None)


def test_bad_input_never_raises():
    for bad in (None, NS(), NS(stop_pct=0, target_pct=1), NS(stop_pct="x", target_pct=1)):
        assert live_anchor.reanchor(bad, 100.0, 99.0, enabled=True, round_fn=_tick)[1] is None
    lv = Lv(1.6, 0.8, 101.6, 99.2)
    for s, l in ((0, 99.0), (-1, 99.0), (100.0, 0), (100.0, -3), (100.0, float("nan"))):
        assert live_anchor.reanchor(lv, s, l, enabled=True, round_fn=_tick)[0] == lv


def test_live_price_reads_the_last_fresh_tick():
    import types
    now = time.time()
    fake = types.ModuleType("feed.ws_client")
    pkg = types.ModuleType("feed"); pkg.ws_client = fake
    saved = {k: sys.modules.get(k) for k in ("feed", "feed.ws_client")}
    sys.modules["feed"], sys.modules["feed.ws_client"] = pkg, fake
    try:
        fake.get_tick_buffer = lambda sym: [(now - 100, 10.0), (now - 2, 352.3)]
        assert live_anchor.live_price("X", max_age_s=45.0) == 352.3
        fake.get_tick_buffer = lambda sym: [(now - 100, 352.3)]
        assert live_anchor.live_price("X", max_age_s=45.0) is None            # stale tick is not "the price now"
        assert live_anchor.live_price("X", max_age_s=0) == 352.3              # 0 = no age limit
        fake.get_tick_buffer = lambda sym: []
        assert live_anchor.live_price("X", max_age_s=45.0) is None
        fake.get_tick_buffer = lambda sym: (_ for _ in ()).throw(RuntimeError("feed down"))
        assert live_anchor.live_price("X", max_age_s=45.0) is None            # fails open
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _src(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_entry_anchors_after_all_gates_and_before_the_order():
    s = _src("orders/entry.py")
    i_anchor = s.index("live_anchor.reanchor(levels, candidate.current_ltp")
    i_rr = s.index("_rr_reject = rr_floor.reject_reason(levels)")
    i_order = s.index("    # Place the order\n    dhan_super_order_id = None")
    i_row = s.index("pos = ScalpPosition(\n        symbol=candidate.symbol")
    assert i_rr < i_anchor < i_order < i_row
    assert "price=_ref_price," in s and "entry_price=_ref_price," in s
    assert "signal_price=candidate.current_ltp," in s                          # slippage vs the SIGNAL is still recorded
    assert "live_anchor" in s.split("from orders.adaptive")[0]


def test_default_on_and_catalogued():
    assert re.search(r'ENTRY_ANCHOR_TO_LIVE\s*=\s*_get_bool\("ENTRY_ANCHOR_TO_LIVE",\s*True\)', _src("config.py"))
    m = re.search(r'Flag\("ENTRY_ANCHOR_TO_LIVE".*?shipped="([^"]+)"', _src("control/catalog.py"), re.S)
    assert m and m.group(1) == "1"


if __name__ == "__main__":
    import traceback
    ok = bad = 0
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            try:
                f(); ok += 1
            except Exception:  # noqa: BLE001
                bad += 1; print("FAIL", n); traceback.print_exc()
    print(f"{ok} passed, {bad} failed"); sys.exit(1 if bad else 0)
