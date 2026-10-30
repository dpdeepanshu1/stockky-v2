"""E266 (2026-10-30): conviction-based trade size in Real Trade.

  * entry_engine/conviction_sizing.py  the pure maths (byte-identical to the position-stocks-service copy)
  * entry_engine/conviction_size.py    the 0-100 score of one entry and the reasons an upsize is held
  * risk_engine/engine.py              size_mult moves the two RUPEE caps only; every other limit still applies

Run from services/real-trade-service:  python3 -m pytest tests/test_e266_conviction_sizing.py -q
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import config
from entry_engine import conviction_size as cz
from entry_engine import conviction_sizing as cs
from risk_engine import engine
from tests.test_risk_engine import make_account, make_intent  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT.parent.parent


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    for k, v in (("CONVICTION_SIZING_ENABLED", True), ("CONVICTION_SCORE_LOW", 35.0), ("CONVICTION_SCORE_MID", 50.0), ("CONVICTION_SCORE_HIGH", 80.0),
                 ("CONVICTION_MIN_MULT", 0.5), ("CONVICTION_MAX_MULT", 3.0), ("CONVICTION_MAX_POSITION_PCT", 50.0), ("CONVICTION_NO_UPSIZE_ON_LOSS_DAY", True)):
        monkeypatch.setattr(config, k, v, raising=False)
    monkeypatch.setattr(engine, "MIN_STOCK_PRICE", 20.0)
    monkeypatch.setattr(engine, "MAX_STOCK_PRICE", 3000.0)
    monkeypatch.setattr(engine, "MAX_STOCK_PRICE_EXPLICITLY_SET", False)
    monkeypatch.setattr(engine, "RISK_MAX_STOCK_PRICE_ADAPTIVE", True)
    monkeypatch.setattr(engine, "ADAPTIVE_MAX_PRICE_STOP_PCT", 6.0)
    monkeypatch.setattr(engine, "HARD_FLOOR_LIQUIDITY", 5_000_000.0)
    monkeypatch.setattr(engine, "CAPITAL_SHARE_PCT", 50.0)
    monkeypatch.setattr(engine, "MAX_POSITION_CONCENTRATION_PCT", 25.0)


def test_the_two_copies_of_the_maths_are_byte_identical():
    a = (ROOT / "entry_engine" / "conviction_sizing.py").read_bytes()
    b = (REPO / "services" / "position-stocks-service" / "capital" / "conviction_sizing.py").read_bytes()
    assert a == b


# ---------------------------------------------------------------- the score
def _cand(conv):
    return SimpleNamespace(symbol="ABC", conviction_score=conv)


STRONG = dict(rr=3.5, rr_floor=2.0, market_score=75, threshold=50, regime_override=False, drift_pct=0.0, max_drift_pct=2.0,
              board_row={"side": "bull", "conviction": 90})


def test_a_sure_entry_gets_the_biggest_multiple():
    d = cz.long_decision(_cand(95), **STRONG)
    assert d and d["score"] == 100.0 and d["mult"] == 3.0 and d["held"] == []
    assert cz.note(d) == "conviction 100 -> x3.00 size"


def test_a_doubtful_entry_gets_the_smallest_multiple():
    d = cz.long_decision(_cand(55), rr=2.0, market_score=50, threshold=50, drift_pct=1.9, max_drift_pct=2.0, board_row={"side": "bull", "conviction": 40})
    assert d and d["mult"] == 0.5


def test_nothing_known_is_the_old_size_or_no_opinion():
    assert cz.long_decision(SimpleNamespace(symbol="X", conviction_score=None), rr=2.0) is None          # < 2 known parts
    d = cz.long_decision(_cand(75), rr=2.0, market_score=62.5, threshold=50)                             # conviction 50, market 50, the rest unknown
    assert d and d["score"] == 50.0 and d["mult"] == 1.0


def test_a_regime_override_gets_no_market_credit_and_holds_an_upsize():
    d = cz.long_decision(_cand(95), **{**STRONG, "regime_override": True}, hold=cz.hold_reasons(regime_override=True))
    assert d["detail"]["market"] == 0.0 and d["mult"] == 1.0 and d["raw_mult"] > 1.0 and "regime override" in d["held"]
    assert "held: regime override" in cz.note(d)


def test_hold_reasons_cover_a_losing_day_and_every_size_down():
    assert cz.hold_reasons() == []
    assert cz.hold_reasons(realized_pnl_today=-1.0) == ["losing day"]
    assert cz.hold_reasons(vix_mult=0.8, calendar_mult=0.9, opening_mult=0.5) == ["VIX size-down", "calendar size-down", "opening-window size-down"]
    assert cz.hold_reasons(realized_pnl_today=5.0) == []


def test_a_downsize_is_never_held():
    d = cz.long_decision(_cand(55), rr=2.0, market_score=50, threshold=50, drift_pct=1.9, max_drift_pct=2.0, board_row={"side": "bull", "conviction": 40},
                         hold=["losing day"])
    assert d["mult"] == 0.5 and d["held"] == []


def test_a_bearish_board_row_is_not_credit_and_the_feature_can_be_off(monkeypatch):
    d = cz.long_decision(_cand(95), **{**STRONG, "board_row": {"side": "bear", "conviction": 95}})
    assert d["detail"]["board"] is None
    monkeypatch.setattr(config, "CONVICTION_SIZING_ENABLED", False)
    assert cz.long_decision(_cand(95), **STRONG) is None and cz.note(None) == ""


def test_a_broken_input_never_raises():
    class Boom:
        def __getattr__(self, n):
            raise RuntimeError("boom")
    assert cz.long_decision(Boom(), rr=Boom()) is None


# ---------------------------------------------------------------- the risk engine
def _eval(size_mult=None, ceiling=50.0, **acct):
    acct = {"equity": 7000.0, "risk_per_trade_pct": 2.0, "cash_available": 7000.0, "broker_cash_available": 14000.0, "max_trade_value": 3000.0, **acct}
    it = make_intent(mode="REAL", qty=100, entry_price=100.0, stop_price=98.5, target_price=104.5, size_mult=size_mult, size_ceiling_pct=ceiling)
    return engine.evaluate(it, make_account(**acct))


def test_without_a_multiple_the_fixed_caps_apply_exactly_as_before():
    r = _eval()
    assert r.approved_qty == 17                       # 25 % of Rs 7,000 = Rs 1,750 -> 17 shares at Rs 100
    assert _eval(size_mult=1.0).approved_qty == 17


def test_a_sure_entry_lifts_both_caps_up_to_the_equity_ceiling():
    r = _eval(size_mult=3.0)
    assert r.approved_qty == 35                       # Rs 3,500 = 50 % of equity (3 x 1,750 = 5,250 and 3 x 3,000 = 9,000 are both above it)


def test_a_doubtful_entry_shrinks_the_caps():
    assert _eval(size_mult=0.5).approved_qty == 8     # 0.5 x 1,750 = Rs 875


def test_an_upsize_without_a_ceiling_is_not_made_but_a_downsize_is():
    assert _eval(size_mult=3.0, ceiling=None).approved_qty == 17
    assert _eval(size_mult=3.0, ceiling=0.0).approved_qty == 17
    assert _eval(size_mult=0.5, ceiling=None).approved_qty == 8


def test_the_ceiling_never_cuts_below_the_old_caps():
    assert _eval(size_mult=3.0, ceiling=10.0).approved_qty == 17        # 10 % of equity = Rs 700 < the old Rs 1,750


def test_the_per_trade_risk_cap_still_binds_an_upsized_trade():
    # 1 % risk of Rs 7,000 = Rs 70; at a Rs 1.50 stop that is 46 shares, but the caps allow 35 -> risk wins only when it is lower:
    assert _eval(size_mult=3.0, risk_per_trade_pct=0.5).approved_qty == 23      # Rs 35 / Rs 1.50
    assert _eval(size_mult=3.0, risk_per_trade_pct=0.5).check_name == "sized_down"


def test_cash_and_the_capital_share_still_bind_an_upsized_trade():
    assert _eval(size_mult=3.0, cash_available=1000.0).approved_qty == 10
    r = _eval(size_mult=3.0, broker_cash_available=2000.0, open_positions_market_value=0.0)    # share = 50 % of Rs 2,000 = Rs 1,000
    assert r.verdict.value.upper() == "REJECTED" and r.check_name == "capital_share_cap"


def test_the_flat_max_trade_value_is_moved_too(monkeypatch):
    # a big account: the % cap is not what binds, the flat Rs 3,000 is
    big = dict(equity=100_000.0, cash_available=100_000.0, broker_cash_available=200_000.0, risk_per_trade_pct=2.0, max_trade_value=3000.0)
    it = lambda m: make_intent(mode="REAL", qty=500, entry_price=100.0, stop_price=98.5, target_price=104.5, size_mult=m, size_ceiling_pct=50.0)
    assert engine.evaluate(it(None), make_account(**big)).approved_qty == 30
    assert engine.evaluate(it(2.0), make_account(**big)).approved_qty == 60
    assert engine.evaluate(it(0.5), make_account(**big)).approved_qty == 15


def test_bad_multiples_leave_the_fixed_caps():
    for m in (0.0, -1.0):                         # a non-positive multiple is nonsense: the fixed caps stand
        assert _eval(size_mult=m).approved_qty == 17
    assert _eval(size_mult=float("nan")).approved_qty == 17
    assert _eval(size_mult=float("inf")).approved_qty == 17


# ---------------------------------------------------------------- wiring
def test_the_entry_cycle_passes_the_multiple_to_the_risk_engine_for_REAL_only():
    src = (ROOT / "entry_engine" / "entry.py").read_text(encoding="utf-8")
    assert 'if mode == "REAL":\n            try:\n                _cv_hold' in src
    assert src.index("_conviction_size.long_decision(") < src.index("size_mult=(_cv or {}).get(\"mult\")") < src.index("risk_evaluate(intent, account_state)")
    for other in ("manual_engine.py", "main.py"):
        assert "size_ceiling_pct" not in (ROOT / other).read_text(encoding="utf-8"), other


def test_every_conviction_switch_is_on_the_calibration_page_and_protected():
    cat = (ROOT / "control" / "catalog.py").read_text(encoding="utf-8")
    auto = (REPO / "services" / "calibration-runner-service" / "autocalib.py").read_text(encoding="utf-8")
    for k in ("CONVICTION_SIZING_ENABLED", "CONVICTION_MAX_MULT", "CONVICTION_MIN_MULT", "CONVICTION_MAX_POSITION_PCT", "CONVICTION_SCORE_LOW",
              "CONVICTION_SCORE_MID", "CONVICTION_SCORE_HIGH", "CONVICTION_NO_UPSIZE_ON_LOSS_DAY"):
        assert f'Flag("{k}"' in cat, k
        assert f'"{k}":' in auto, k
        assert hasattr(config, k), k
