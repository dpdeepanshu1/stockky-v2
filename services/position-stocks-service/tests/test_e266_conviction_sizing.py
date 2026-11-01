"""E266 (2026-10-30): conviction-based trade size in Position Stocks.

  * capital/conviction_sizing.py   the pure maths (and: it is byte-identical to the real-trade-service copy)
  * capital/ledger.py              conviction_budget / conviction_mult_in_force / reserve_capital(conviction_mult=...)
  * orders/conviction.py           the 0-100 score of one entry
  * orders/entry.py                a sure setup really buys more shares, a switched-off feature changes nothing

Run from services/position-stocks-service:  python3 -m pytest tests/test_e266_conviction_sizing.py -q
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import models
from capital import conviction_sizing as cs
from capital import ledger
from orders import conviction
from orders import entry
from screening import shortlist
from screening.quality_gate import QualitySignal
from tests.test_entry import cand, env  # noqa: F401  (env is a fixture: the same fake broker / DB the entry tests use)

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT.parent.parent


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    """The conftest switches the feature off for the older tests; every test here runs it ON with the shipped numbers."""
    for k, v in (("CONVICTION_SIZING_ENABLED", True), ("CONVICTION_SCORE_LOW", 35.0), ("CONVICTION_SCORE_MID", 50.0), ("CONVICTION_SCORE_HIGH", 80.0),
                 ("CONVICTION_MIN_MULT", 0.5), ("CONVICTION_MAX_MULT", 3.0), ("CONVICTION_SHORT_MAX_MULT", 2.0), ("CONVICTION_MAX_POOL_PCT", 50.0),
                 ("CONVICTION_NO_UPSIZE_ON_LOSS_DAY", True), ("RISK_PER_TRADE_PCT", 2.0), ("MAX_CONCURRENT_SCALP_POSITIONS", 5),
                 ("SCALP_SLOT_NOTIONAL_CAP_MULT", 1.5), ("ACCOUNT_MAX_DAILY_LOSS_PCT", 0.0), ("MARKET_STATE_RISK_MULT", 1.0)):
        monkeypatch.setattr(config, k, v, raising=False)
    shortlist.reset_recent()


# ---------------------------------------------------------------- the pure maths
def test_the_two_copies_of_the_maths_are_byte_identical():
    a = (ROOT / "capital" / "conviction_sizing.py").read_bytes()
    b = (REPO / "services" / "real-trade-service" / "entry_engine" / "conviction_sizing.py").read_bytes()
    assert a == b, "run: python3 scripts/check_shared_copies.py --sync conviction_sizing"


def test_scale_is_linear_clamped_and_none_when_unknown():
    assert cs.scale(40, 40, 90) == 0.0 and cs.scale(90, 40, 90) == 100.0 and cs.scale(65, 40, 90) == 50.0
    assert cs.scale(10, 40, 90) == 0.0 and cs.scale(500, 40, 90) == 100.0
    assert cs.scale(None, 40, 90) is None and cs.scale("x", 40, 90) is None and cs.scale(float("nan"), 40, 90) is None
    assert cs.scale(5, 10, 10) is None


def test_blend_unknown_is_neutral_and_needs_two_known_parts():
    parts = [("a", 100.0, 0.5), ("b", None, 0.5)]
    assert cs.blend(parts, min_known=2) is None                       # one known part = no opinion
    score, detail = cs.blend([("a", 100.0, 0.5), ("b", 100.0, 0.25), ("c", None, 0.25)], min_known=2)
    assert score == 87.5 and detail["c"] is None                      # the unknown quarter counts as 50, it can never lift the score
    score, _ = cs.blend([("a", 50.0, 1.0), ("b", 50.0, 1.0)], min_known=2)
    assert score == 50.0


def test_multiplier_endpoints_and_bad_settings():
    kw = dict(low=35, mid=50, high=80, min_mult=0.5, max_mult=3.0)
    assert cs.multiplier(0, **kw) == 0.5 and cs.multiplier(35, **kw) == 0.5
    assert cs.multiplier(50, **kw) == 1.0
    assert cs.multiplier(65, **kw) == 2.0 and cs.multiplier(80, **kw) == 3.0 and cs.multiplier(100, **kw) == 3.0
    assert cs.multiplier(42.5, **kw) == 0.75
    assert cs.multiplier(None, **kw) == 1.0
    assert cs.multiplier(90, low=60, mid=50, high=80, min_mult=0.5, max_mult=3.0) == 1.0     # low >= mid: inconsistent = the old size
    assert cs.multiplier(90, low=35, mid=50, high=80, min_mult=2.0, max_mult=0.5) == 1.0       # min is held <= 1 and max >= 1: backwards settings never shrink a sure trade
    assert cs.multiplier(10, low=35, mid=50, high=80, min_mult=2.0, max_mult=0.5) == 1.0       # ... nor grow a doubtful one


def test_upsized_value_rules():
    assert cs.upsized_value(1000, 0.5) == 500.0                        # a smaller multiple needs no ceiling
    assert cs.upsized_value(1000, 3.0) == 1000.0                       # an upsize without a known ceiling is not made
    assert cs.upsized_value(1000, 3.0, ceiling=2500) == 2500.0
    assert cs.upsized_value(1000, 3.0, ceiling=500) == 1000.0          # a ceiling below the old size never cuts the old size
    assert cs.upsized_value(1000, 3.0, ceiling=5000, risk_cap=1800) == 1800.0
    assert cs.upsized_value(1000, 3.0, ceiling=5000, available=1200) == 1200.0
    assert cs.upsized_value(1000, 3.0, ceiling=5000, available=300) == 1000.0   # free cash below the old size: the old size stands (the ledger then refuses it)
    assert cs.upsized_value("x", 3.0, ceiling=5000) == 0.0


def test_hold_back_only_blocks_an_upsize():
    assert cs.hold_back(2.0, no_upsize=True) == 1.0
    assert cs.hold_back(0.6, no_upsize=True) == 0.6                    # a downsize is never held back
    assert cs.hold_back(2.0, no_upsize=False) == 2.0
    assert cs.hold_back(None, no_upsize=False) == 1.0


# ---------------------------------------------------------------- the ledger
@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


def _pool(db, *, base=7000.0, avail=None, pnl=0.0):
    row = ledger._get_or_create(db)
    row.total_allocated_capital = base
    row.available_capital = base if avail is None else avail
    row.realized_pnl_today = pnl
    db.commit()
    return row


def test_without_a_multiple_the_ledger_sizes_exactly_as_before(db):
    _pool(db)
    plain = ledger.reserve_capital(db, adaptive_stop_pct=1.5)
    assert plain == pytest.approx(7000 * 0.02 / 5 / 0.015)            # Rs 1,866.67: risk / slots / stop (under the 30 % slot cap of Rs 2,100)
    _pool(db)
    assert ledger.reserve_capital(db, adaptive_stop_pct=1.5, conviction_mult=None) == pytest.approx(plain)


def test_a_sure_setup_takes_up_to_half_of_the_pool_a_doubtful_one_half_the_old_size(db):
    _pool(db)
    assert ledger.reserve_capital(db, adaptive_stop_pct=1.5, conviction_mult=3.0) == pytest.approx(3500.0)      # 50 % of the Rs 7,000 pool
    _pool(db)
    assert ledger.reserve_capital(db, adaptive_stop_pct=1.5, conviction_mult=0.5) == pytest.approx(7000 * 0.02 / 5 / 0.015 * 0.5)
    _pool(db)
    assert ledger.reserve_capital(db, adaptive_stop_pct=1.5, conviction_mult=1.0) == pytest.approx(7000 * 0.02 / 5 / 0.015)


def test_the_requested_multiple_is_limited_to_the_configured_range(db):
    row = _pool(db)
    assert ledger.conviction_mult_in_force(row, 9.0) == 3.0
    assert ledger.conviction_mult_in_force(row, 0.01) == 0.5
    assert ledger.conviction_mult_in_force(row, 9.0, "SHORT") == 2.0   # a short is capped lower
    assert ledger.conviction_mult_in_force(row, None) == 1.0
    assert ledger.conviction_mult_in_force(row, float("nan")) == 1.0
    assert ledger.conviction_mult_in_force(row, -2.0) == 1.0


def test_switched_off_means_the_old_size_whatever_is_asked(db, monkeypatch):
    monkeypatch.setattr(config, "CONVICTION_SIZING_ENABLED", False)
    _pool(db)
    assert ledger.reserve_capital(db, adaptive_stop_pct=1.5, conviction_mult=3.0) == pytest.approx(7000 * 0.02 / 5 / 0.015)


def test_a_losing_day_holds_an_upsize_but_never_a_downsize(db):
    _pool(db, pnl=-40.0)
    assert ledger.reserve_capital(db, adaptive_stop_pct=1.5, conviction_mult=3.0) == pytest.approx(7000 * 0.02 / 5 / 0.015)
    _pool(db, pnl=-40.0)
    assert ledger.reserve_capital(db, adaptive_stop_pct=1.5, conviction_mult=0.5) == pytest.approx(7000 * 0.02 / 5 / 0.015 * 0.5)


def test_the_loss_day_hold_can_be_switched_off(db, monkeypatch):
    monkeypatch.setattr(config, "CONVICTION_NO_UPSIZE_ON_LOSS_DAY", False)
    _pool(db, pnl=-40.0)
    assert ledger.reserve_capital(db, adaptive_stop_pct=1.5, conviction_mult=3.0) == pytest.approx(3500.0)


def test_an_upsize_is_bounded_by_the_pool_share_the_per_trade_risk_and_the_free_cash(db, monkeypatch):
    row = _pool(db)
    assert ledger.conviction_budget(row, 1000.0, 3.0, 1.5) == 3000.0                        # 3 x 1000, under the 3,500 pool ceiling
    monkeypatch.setattr(config, "CONVICTION_MAX_POOL_PCT", 100.0)
    assert ledger.conviction_budget(row, 1000.0, 3.0, 5.0) == pytest.approx(2800.0)         # risk cap: 2 % of 7,000 = Rs 140 at a 5 % stop = Rs 2,800
    row = _pool(db, avail=1200.0)
    assert ledger.conviction_budget(row, 1000.0, 3.0, 1.5) == 1200.0                         # free cash
    monkeypatch.setattr(config, "CONVICTION_MAX_POOL_PCT", 10.0)
    row = _pool(db)
    assert ledger.conviction_budget(row, 2000.0, 3.0, 1.5) == 2000.0                         # a ceiling below the old size never cuts it


def test_market_weather_lowers_the_risk_cap_too(db, monkeypatch):
    monkeypatch.setattr(config, "CONVICTION_MAX_POOL_PCT", 100.0)
    monkeypatch.setattr(config, "MARKET_STATE_RISK_MULT", 0.5)
    row = _pool(db)
    assert ledger.conviction_budget(row, 1000.0, 3.0, 5.0) == pytest.approx(1400.0)         # Rs 70 risk at a 5 % stop


def test_the_kill_switch_and_pool_checks_still_apply_to_an_upsize(db):
    row = _pool(db)
    row.daily_loss_kill_switch_tripped = True
    db.commit()
    assert ledger.reserve_capital(db, adaptive_stop_pct=1.5, conviction_mult=3.0) is None
    row.daily_loss_kill_switch_tripped = False
    row.available_capital = 100.0
    db.commit()
    assert ledger.reserve_capital(db, adaptive_stop_pct=1.5, conviction_mult=3.0) is None   # the old size does not fit the free cash: refused, as before


def test_a_broken_ledger_row_falls_back_to_the_fixed_size():
    row = SimpleNamespace(total_allocated_capital=None, available_capital=None, realized_pnl_today=None)
    assert ledger.conviction_budget(row, 1000.0, 3.0, 1.5) == 1000.0


# ---------------------------------------------------------------- the score of one entry
def _levels(stop=1.0, target=2.0):
    return SimpleNamespace(stop_pct=stop, target_pct=target)


def _q(**kw):
    base = dict(technical_score=None, has_positive_catalyst=False, bulk_deal_flag=False)
    base.update(kw)
    return SimpleNamespace(**base)


def _c(rvol=None, symbol="ABC"):
    return SimpleNamespace(symbol=symbol, rvol=rvol)


def test_a_sure_long_scores_high_and_gets_the_biggest_multiple():
    shortlist._remember("ABC", "LONG", 90.0, 100.0)
    d = conviction.long_decision(_c(rvol=3.0), _q(technical_score=90, has_positive_catalyst=True), _levels(stop=1.0, target=3.5))
    assert d and d["score"] == 100.0 and d["mult"] == 3.0 and d["side"] == "LONG"
    assert "conviction 100 -> x3.00 size" == conviction.note("ABC", "LONG", d)


def test_a_doubtful_long_gets_the_smallest_multiple():
    shortlist._remember("ABC", "LONG", 40.0, 0.0)
    d = conviction.long_decision(_c(rvol=1.0), _q(technical_score=40), _levels(stop=1.0, target=2.0))
    assert d and d["mult"] == 0.5 and d["score"] < 35


def test_nothing_known_is_no_opinion_and_two_neutral_facts_are_the_old_size():
    assert conviction.long_decision(_c(rvol=None), _q(), _levels(stop=1.0, target=2.0)) is None        # rr at the 1:2 floor is unknown too: fewer than two known
    d = conviction.long_decision(_c(rvol=2.0), _q(technical_score=65), _levels(stop=1.0, target=2.0))
    assert d and d["score"] == 50.0 and d["mult"] == 1.0                                               # unknown = neutral, so 50 = exactly the old size


def test_reward_risk_at_the_floor_is_neutral_only_a_wider_target_raises_the_score():
    base = conviction.long_decision(_c(rvol=2.0), _q(technical_score=65), _levels(stop=1.0, target=2.0))
    wide = conviction.long_decision(_c(rvol=2.0), _q(technical_score=65), _levels(stop=1.0, target=3.0))
    assert wide["score"] > base["score"]
    tight = conviction.long_decision(_c(rvol=2.0), _q(technical_score=65), _levels(stop=1.0, target=1.5))   # below the floor cannot lower it (the RR floor gate refuses it earlier)
    assert tight["score"] == base["score"]


def test_the_board_reading_is_only_used_when_it_is_recent(monkeypatch):
    shortlist._remember("ABC", "LONG", 90.0, 100.0)
    assert shortlist.recent("ABC", "LONG")["board_conviction"] == 90.0
    t0 = shortlist._RECENT[("ABC", "LONG")][0]
    monkeypatch.setattr(shortlist.time, "monotonic", lambda: t0 + 1000.0)
    assert shortlist.recent("ABC", "LONG") is None


def test_a_short_scores_on_bearish_inputs_and_is_capped_lower():
    row = {"conviction": 90, "zone": {"score": 100}}
    d = conviction.short_decision(_c(rvol=3.0), _q(technical_score=10), row, _levels(stop=1.0, target=3.5))
    assert d and d["side"] == "SHORT" and d["score"] == 100.0 and d["mult"] == 2.0        # CONVICTION_SHORT_MAX_MULT
    assert conviction.short_decision(_c(), _q(), None, _levels()) is None


def test_switched_off_there_is_no_decision(monkeypatch):
    monkeypatch.setattr(config, "CONVICTION_SIZING_ENABLED", False)
    shortlist._remember("ABC", "LONG", 90.0, 100.0)
    assert conviction.long_decision(_c(rvol=3.0), _q(technical_score=90), _levels(target=3.5)) is None
    assert conviction.note("ABC", "LONG", None) == ""


def test_a_broken_input_never_raises():
    class Boom:
        def __getattr__(self, n):
            raise RuntimeError("boom")
    assert conviction.long_decision(Boom(), Boom(), Boom()) is None
    assert conviction.short_decision(Boom(), Boom(), {"conviction": object()}, Boom()) is None


# ---------------------------------------------------------------- the entry
def _entry_qty(env, *, tech, catalyst, rvol):
    db, _, sent = env
    q = QualitySignal(symbol="ABC", fundamental_score=70.0, technical_score=tech, market_cap_cr=8000.0, has_positive_catalyst=catalyst)
    c = cand(ltp=500.0)
    c.rvol = rvol
    pos = entry.attempt_entry(db, c, quality=q)
    assert pos is not None
    return pos, sent


def test_attempt_entry_buys_more_shares_for_a_sure_setup_and_tells_you_why(env):
    shortlist._remember("ABC", "LONG", 90.0, 100.0)
    pos, sent = _entry_qty(env, tech=90.0, catalyst=True, rvol=3.0)
    assert pos.quantity > 40                                          # the fixed size buys 40 in these tests (tests/test_entry.py)
    assert any("conviction" in str(m) and "size" in str(m) for m in sent.get("info", [])), sent


def test_attempt_entry_is_unchanged_when_the_feature_is_off(env, monkeypatch):
    monkeypatch.setattr(config, "CONVICTION_SIZING_ENABLED", False)
    shortlist._remember("ABC", "LONG", 90.0, 100.0)
    pos, sent = _entry_qty(env, tech=90.0, catalyst=True, rvol=3.0)
    assert pos.quantity == 40
    assert not any("conviction" in str(m) for m in sent.get("info", []))


def test_attempt_entry_makes_a_doubtful_setup_smaller(env):
    shortlist._remember("ABC", "LONG", 40.0, 0.0)
    pos, _ = _entry_qty(env, tech=40.0, catalyst=False, rvol=1.0)
    assert 0 < pos.quantity < 40


def test_entry_reads_the_multiple_before_it_reserves_capital():
    src = (ROOT / "orders" / "entry.py").read_text(encoding="utf-8")
    assert src.index("conviction.long_decision(") < src.index("ledger.reserve_capital(db, adaptive_stop_pct=levels.stop_pct, conviction_mult=")


def test_every_conviction_switch_is_on_the_calibration_page_and_protected_from_the_calibrator():
    cat = (ROOT / "control" / "catalog.py").read_text(encoding="utf-8")
    auto = (REPO / "services" / "calibration-runner-service" / "autocalib.py").read_text(encoding="utf-8")
    for k in ("CONVICTION_SIZING_ENABLED", "CONVICTION_MAX_MULT", "CONVICTION_SHORT_MAX_MULT", "CONVICTION_MIN_MULT", "CONVICTION_MAX_POOL_PCT",
              "CONVICTION_SCORE_LOW", "CONVICTION_SCORE_MID", "CONVICTION_SCORE_HIGH", "CONVICTION_NO_UPSIZE_ON_LOSS_DAY"):
        assert f'Flag("{k}"' in cat, k
        assert f'"{k}":' in auto, k
        assert hasattr(config, k), k
