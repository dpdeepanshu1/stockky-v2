"""E273 round 12: the rest of the opening gate is a soft penalty. Plain runner: python3 tests/test_e273_opening_parts_penalty.py"""
from __future__ import annotations

import os
import sys
import types
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _m in ("httpx", "sqlalchemy", "sqlalchemy.orm"):
    if _m not in sys.modules:
        try:
            __import__(_m)
        except Exception:  # noqa: BLE001
            sys.modules[_m] = types.ModuleType(_m)
if not hasattr(sys.modules["sqlalchemy.orm"], "Session"):
    sys.modules["sqlalchemy.orm"].Session = object

import config
from screening import opening_gate as og, prev_day

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = datetime(2026, 11, 2, 9, 40)                       # 25 minutes after the open: the 15-minute opening range is complete
ST = {"stats": (100.0, 104.0, 99.0, 99.0), "ticks": None, "nifty": 0.2, "nprev": 0.3, "close_pos": 0.8}


def _setup(mode="penalty"):
    fw = types.ModuleType("feed.ws_client")
    fw.get_day_stats = lambda s: ST["stats"]
    base = datetime(2026, 11, 2, 9, 15).timestamp()
    default_ticks = [(base + 30 * i, 100.0 + (i % 5)) for i in range(30)]    # OR 100..104
    fw.get_tick_buffer = lambda s: ST["ticks"] if ST["ticks"] is not None else default_ticks
    feed = sys.modules.get("feed") or types.ModuleType("feed")
    feed.ws_client = fw
    sys.modules["feed"], sys.modules["feed.ws_client"] = feed, fw
    tg = types.ModuleType("screening.trade_gates")
    tg.last_nifty_change_pct = lambda: ST["nifty"]
    tg.last_nifty_prev_close_pct = lambda: ST["nprev"]
    import screening
    screening.trade_gates = tg
    sys.modules["screening.trade_gates"] = tg
    og.is_active = lambda now=None: True
    og.is_market_open_ist = lambda now=None: True
    og.ist_now = lambda now=None: NOW
    pdv = types.SimpleNamespace(close_pos=ST["close_pos"], atr_pct=2.0, date="2026-10-30", low=95.0, high=101.0, close=100.0)
    prev_day.peek = lambda s: pdv
    prev_day.get = lambda s: pdv
    config.OPENING_PARTS_MODE = mode
    config.OPENING_BELOW_OPEN_MODE = "penalty"      # E283: tests/conftest.py pins "veto" for older tests; BELOW_OPEN would fire before the checks pinned here
    config.MARKET_GATE_ENABLED = True
    config.SOFT_GATE_MAX_PENALTY = 3.0
    config.OPENING_NIFTY_FULL_PENALTY_PCT = 0.5
    ST.update(stats=(100.0, 104.0, 99.0, 99.0), ticks=None, nifty=0.2, nprev=0.3, close_pos=0.8)
    pdv.close_pos = 0.8
    return pdv


def test_shortfall_penalty_scale():
    assert og.shortfall_penalty(0.5, 0.5, 1.0) is None and og.shortfall_penalty(0.9, 0.5, 1.0) is None
    assert og.shortfall_penalty(0.49, 0.5, 1.0) is not None and 1.0 < og.shortfall_penalty(0.49, 0.5, 1.0) < 1.05
    assert og.shortfall_penalty(-0.5, 0.5, 1.0) == 3.0                    # a full range under the mid = the cap
    assert og.shortfall_penalty(-9.0, 0.5, 1.0) == 3.0                    # capped
    assert og.shortfall_penalty(None, 0.5, 1.0) is None and og.shortfall_penalty(0.1, 0.5, 0) is None


def test_clean_candidate_has_no_penalty():
    _setup()
    assert og.parts_penalties("A", 102.5, NOW) == {}


def test_day_high_position_and_the_old_refusal():
    _setup()
    r = og.parts_penalties("A", 103.9, NOW)                               # range_pos 0.98 of 99..104
    assert set(r) == {"near_day_high", "extended_or"} or "near_day_high" in r
    assert 2.0 < r["near_day_high"]["penalty"] <= 3.0
    _setup("veto")
    assert og.parts_penalties("A", 103.9, NOW) == {}
    assert str(og.reject_reason("A", 103.9, NOW)).startswith("OPENING_GATE:NEAR_DAY_HIGH")


def test_opening_range_position_and_extension():
    _setup()
    r = og.parts_penalties("A", 100.4, NOW)                               # OR 100..104, price in the lower quarter: or_pos 0.1
    assert "below_or_mid" in r and 1.0 < r["below_or_mid"]["penalty"] < 3.0
    _setup()
    ST["stats"] = (100.0, 110.0, 99.0, 99.0)                              # keep the day-range test quiet
    r = og.parts_penalties("A", 105.5, NOW)                               # 1.44 % over the OR high 104, limit 1.0 %
    assert abs(r["extended_or"]["penalty"] - 1.4423) < 0.01


def test_previous_day_close_scales_to_the_cap():
    pdv = _setup()
    pdv.close_pos = 0.4
    assert abs(og.parts_penalties("A", 102.5, NOW)["prev_day_close"]["penalty"] - 1.4) < 0.01
    pdv.close_pos = 0.0
    assert og.parts_penalties("A", 102.5, NOW)["prev_day_close"]["penalty"] == 3.0
    pdv.close_pos = None
    assert "prev_day_close" not in og.parts_penalties("A", 102.5, NOW)    # no reading = no opinion here


def test_nifty_penalties_in_the_markets_own_units():
    _setup()
    ST["nifty"], ST["nprev"] = -0.25, -0.5                                # min 0.0 each; full scale 0.5 points
    r = og.parts_penalties("A", 102.5, NOW)
    assert abs(r["nifty_open"]["penalty"] - 2.0) < 0.01 and r["nifty_prev"]["penalty"] == 3.0
    config.MARKET_GATE_ENABLED = False
    assert og.parts_penalties("A", 102.5, NOW) == {}


def test_reject_reason_penalty_mode_still_refuses_hard_failures():
    _setup()
    pdv_ok = og.reject_reason("A", 102.5, NOW)
    assert pdv_ok is None
    assert og.reject_reason("A", 100.4, NOW) is None                      # below the OR mid: a penalty now, not a refusal
    ST["nifty"], ST["nprev"] = -0.4, -0.4
    assert og.reject_reason("A", 102.5, NOW) is None                      # Nifty down: a penalty now
    assert str(og.reject_reason("A", 96.5, NOW)).startswith("OPENING_GATE:BELOW_PREV_CLOSE")      # past the 2 % depth under the previous close still refuses (round 30)
    ST["nifty"], ST["nprev"] = None, None
    assert og.reject_reason("A", 102.5, NOW) == "OPENING_GATE:NO_NIFTY_DATA"                       # missing data still fails closed
    _setup(); ST["ticks"] = [(datetime(2026, 11, 2, 9, 16).timestamp(), 101.0)] * 3
    assert og.reject_reason("A", 102.5, NOW) == "OPENING_GATE:NO_OPENING_RANGE_DATA"


def test_veto_mode_refuses_as_before():
    _setup("veto")
    assert str(og.reject_reason("A", 100.4, NOW)).startswith("OPENING_GATE:BELOW_OR_MID")
    _setup("veto"); ST["nprev"] = -0.4
    assert str(og.reject_reason("A", 102.5, NOW)).startswith("OPENING_GATE:NIFTY_BELOW_PREV_CLOSE")
    _setup("veto"); pdv = prev_day.peek("A"); pdv.close_pos = 0.2
    assert str(og.reject_reason("A", 102.5, NOW)).startswith("OPENING_GATE:PREV_DAY_WEAK_CLOSE")


def test_ignore_gap_runs_the_other_tests():
    _setup()
    ST["stats"] = (104.0, 106.0, 103.0, 99.0)                             # gap +5.05 %
    assert str(og.reject_reason("A", 105.0, NOW)).startswith("OPENING_GATE:GAP_UP")
    assert og.reject_reason("A", 105.0, NOW, ignore_gap=True) is None
    assert str(og.reject_reason("A", 96.5, NOW, ignore_gap=True)).startswith("OPENING_GATE:BELOW_PREV_CLOSE")   # a waived gap does not waive the rest
    _setup(); ST["nprev"] = None
    assert og.reject_reason("A", 102.5, NOW, ignore_gap=True) == "OPENING_GATE:NO_NIFTY_DATA"


def test_mode_parsing_and_defaults():
    config.OPENING_PARTS_MODE = "weird"; assert og.parts_mode() == "penalty"
    config.OPENING_PARTS_MODE = " VETO "; assert og.parts_mode() == "veto"
    config.OPENING_PARTS_MODE = "penalty"
    assert config.OPENING_NIFTY_FULL_PENALTY_PCT > 0


def test_entry_wiring_by_source():
    src = open(os.path.join(ROOT, "orders", "entry.py"), encoding="utf-8").read()
    a = src.index("opening_gate.reject_reason(candidate.symbol, candidate.current_ltp)")
    b = src.index("ignore_gap=True", a)
    c = src.index("opening_gate.parts_penalties(", b)
    d = src.index("_price_guard_reject(candidate.symbol", c)
    e = src.index("opening_gate.below_open_penalty(")
    assert a < b < e < c < d                                              # reject, gap re-run, below-open, parts penalties, then the price guard
    assert '"extended_or"' in src and "soft_gates.total_skip_reason(_penalties" in src[c:d]
    assert "if rearm and _k in (\"near_day_high\", \"extended_or\")" in src


def test_catalog_and_conftest():
    cat = open(os.path.join(ROOT, "control", "catalog.py"), encoding="utf-8").read()
    assert 'Flag("OPENING_PARTS_MODE"' in cat and 'Flag("OPENING_NIFTY_FULL_PENALTY_PCT"' in cat
    assert 'OPENING_PARTS_MODE", "veto"' in open(os.path.join(ROOT, "tests", "conftest.py"), encoding="utf-8").read()


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v(); n += 1; print("ok", k)
    print(n, "passed")
