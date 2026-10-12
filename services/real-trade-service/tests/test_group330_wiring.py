"""tests/test_group330_wiring.py - the group 330 gates really sit on the evaluate_mode path and in the carry selection."""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

import config
from entry_engine import entry, event_risk, preopen, vix_gate
from risk_engine.engine import RiskResult, RiskVerdict
from tests.test_entry_evaluate_mode import (                               # noqa: F401
    pin, db, make_candidate, quotes, approve_all, run, tick as _base_tick,
)


@pytest.fixture(autouse=True)
def _reset():
    for m in (vix_gate, event_risk, preopen):
        m.reset()
    yield
    for m in (vix_gate, event_risk, preopen):
        m.reset()


def _tick_with(**kw):
    t = _base_tick(100.0)
    for k, v in kw.items():
        setattr(t, k, v)
    return t


def _vix(monkeypatch, level, change=0.0):
    monkeypatch.setattr(config, "VIX_GATE_ENABLED", True)
    monkeypatch.setattr(vix_gate, "_fetch", lambda: {"level": level, "change_pct": change})


def _first(db, monkeypatch, **tick_kw):
    make_candidate(db)
    quotes(monkeypatch, {"TESTCO": _tick_with(**tick_kw)})
    approve_all(monkeypatch)
    tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
    return tally, tally["entry_details"][0]


class TestVixOnEntryPath:
    def test_high_vix_blocks_new_entry(self, db, monkeypatch):
        _vix(monkeypatch, 26.0)
        tally, d = _first(db, monkeypatch)
        assert tally["entered"] == 0 and d["action"] == "WAIT" and d["gate_tag"] == "vix_high" and d["reasoning"].startswith("VIX_HIGH")

    def test_calm_vix_does_not_block(self, db, monkeypatch):
        _vix(monkeypatch, 13.0)
        _, d = _first(db, monkeypatch)
        assert d.get("gate_tag") != "vix_high"

    def test_unreachable_vix_does_not_block(self, db, monkeypatch):
        monkeypatch.setattr(config, "VIX_GATE_ENABLED", True)

        def down():
            raise TimeoutError("market-data down")
        monkeypatch.setattr(vix_gate, "_fetch", down)
        _, d = _first(db, monkeypatch)
        assert d.get("gate_tag") != "vix_high"

    def test_elevated_vix_sizes_the_position_down(self, db, monkeypatch):
        seen = []

        def capture(intent, account_state):
            seen.append(intent.qty)
            return RiskResult(verdict=RiskVerdict.APPROVED, check_name="ok", reason="ok", approved_qty=intent.qty)

        def one_run(level):
            seen.clear()
            vix_gate.reset()
            _vix(monkeypatch, level)
            make_candidate(db)
            quotes(monkeypatch, {"TESTCO": _tick_with()})
            monkeypatch.setattr(entry, "risk_evaluate", capture)
            run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
            return seen[0] if seen else None

        calm = one_run(13.0)
        caution = one_run(19.0)
        assert calm and caution and caution < calm
        assert abs(caution / calm - config.VIX_CAUTION_SIZE_MULT) < 0.05


class TestVwapOnEntryPath:
    def test_below_vwap_waits(self, db, monkeypatch):
        tally, d = _first(db, monkeypatch, vwap=100.8)
        assert tally["entered"] == 0 and d["gate_tag"] == "vwap_below" and d["reasoning"].startswith("VWAP_BELOW")

    def test_above_vwap_is_fine(self, db, monkeypatch):
        _, d = _first(db, monkeypatch, vwap=99.0)
        assert d.get("gate_tag") != "vwap_below"

    def test_unknown_vwap_is_fine(self, db, monkeypatch):
        _, d = _first(db, monkeypatch)
        assert d.get("gate_tag") != "vwap_below"

    def test_switch_off(self, db, monkeypatch):
        monkeypatch.setattr(config, "ENTRY_VWAP_GATE_ENABLED", False)
        _, d = _first(db, monkeypatch, vwap=100.8)
        assert d.get("gate_tag") != "vwap_below"


class TestPreopenGapOnEntryPath:
    def test_big_gap_snapshot_blocks(self, db, monkeypatch):
        monkeypatch.setattr(config, "PREOPEN_SNAPSHOT_ENABLED", True)
        monkeypatch.setattr(preopen, "gap_reason", lambda sym, now=None: "PREOPEN_GAP_UP: test")
        tally, d = _first(db, monkeypatch)
        assert tally["entered"] == 0 and d["gate_tag"] == "preopen_gap"

    def test_no_snapshot_means_no_block(self, db, monkeypatch):
        monkeypatch.setattr(config, "PREOPEN_SNAPSHOT_ENABLED", True)
        _, d = _first(db, monkeypatch)
        assert d.get("gate_tag") != "preopen_gap"


class TestNegativeNewsOnEntryPath:
    def test_negative_news_waits(self, db, monkeypatch):
        monkeypatch.setattr(config, "NEWS_NEGATIVE_BLOCK_ENABLED", True)
        monkeypatch.setattr(event_risk, "_fetch", lambda s: {"event_score": 22})
        tally, d = _first(db, monkeypatch)
        assert tally["entered"] == 0 and d["gate_tag"] == "negative_news" and d["reasoning"].startswith("NEWS_NEGATIVE")

    def test_neutral_news_passes(self, db, monkeypatch):
        monkeypatch.setattr(config, "NEWS_NEGATIVE_BLOCK_ENABLED", True)
        monkeypatch.setattr(event_risk, "_fetch", lambda s: {"event_score": 55})
        _, d = _first(db, monkeypatch)
        assert d.get("gate_tag") != "negative_news"

    def test_event_service_down_passes(self, db, monkeypatch):
        monkeypatch.setattr(config, "NEWS_NEGATIVE_BLOCK_ENABLED", True)

        def down(s):
            raise TimeoutError("x")
        monkeypatch.setattr(event_risk, "_fetch", down)
        _, d = _first(db, monkeypatch)
        assert d.get("gate_tag") != "negative_news"


import models                                                                  # noqa: E402
import market_feed.feed as feed_module                                         # noqa: E402
from market_feed.feed import Tick                                              # noqa: E402
from execution.auto_pilot import _select_overnight_holds                       # noqa: E402
from entry_engine import carry_context                                         # noqa: E402
from sqlalchemy import create_engine                                           # noqa: E402
from sqlalchemy.orm import sessionmaker                                        # noqa: E402

_eng = create_engine("sqlite:///:memory:")
_Sess = sessionmaker(bind=_eng)


def _cdb():
    models.Base.metadata.drop_all(_eng)
    models.Base.metadata.create_all(_eng)
    d = _Sess()
    d.add(models.TradeAccount(mode="REAL", starting_capital=100_000.0, current_equity=100_000.0, cash_available=100_000.0))
    d.flush()
    return d


def _carry(monkeypatch, symbol="CARRYCO"):
    monkeypatch.setattr(config, "OVERNIGHT_HOLD_MIN_GAIN_PCT", 0.0, raising=False)
    monkeypatch.setattr(config, "OVERNIGHT_HOLD_PROFITABLE_NET_OF_COSTS", False, raising=False)
    d = _cdb()
    p = models.TradePosition(mode="REAL", symbol=symbol, status="OPEN", qty_open=10, avg_entry_price=100.0,
                             opened_at=datetime.now(timezone.utc), entry_decision_label="VOLUME_SHOCK_HIGH_CONVICTION",
                             entry_conviction_score=70)
    d.add(p)
    d.flush()

    async def _q(symbols, **_kw):
        return {symbol: Tick(symbol=symbol, price=102.0, as_of=datetime.now(timezone.utc), atr=None, source="t",
                             day_high=105.0, day_low=98.0)}
    monkeypatch.setattr(feed_module, "get_quotes", _q)
    keep, _ = asyncio.run(_select_overnight_holds(d, "REAL", [p]))
    return p, keep


class TestCarryContextWiring:
    @pytest.fixture(autouse=True)
    def _on(self, monkeypatch):
        monkeypatch.setattr(config, "CARRY_CONTEXT_GATE_ENABLED", True)
        monkeypatch.setattr(config, "NEWS_NEGATIVE_BLOCK_ENABLED", True)
        monkeypatch.setattr(config, "VIX_GATE_ENABLED", True)
        carry_context.reset()
        vix_gate.reset()
        event_risk.reset()
        monkeypatch.setattr(carry_context, "_fetch_nifty", lambda: 0.3)
        monkeypatch.setattr(vix_gate, "_fetch", lambda: {"level": 13.0, "change_pct": 0.0})
        monkeypatch.setattr(event_risk, "_fetch", lambda s: {"event_score": 55})

    def test_calm_context_still_carries(self, monkeypatch):
        p, keep = _carry(monkeypatch)
        assert p.id in keep

    def test_market_selling_into_the_close_squares_off(self, monkeypatch):
        monkeypatch.setattr(carry_context, "_fetch_nifty", lambda: -1.4)
        p, keep = _carry(monkeypatch)
        assert keep == set()

    def test_vix_spike_squares_off(self, monkeypatch):
        monkeypatch.setattr(vix_gate, "_fetch", lambda: {"level": 15.0, "change_pct": 9.0})
        p, keep = _carry(monkeypatch)
        assert keep == set()

    def test_negative_news_squares_off(self, monkeypatch):
        monkeypatch.setattr(event_risk, "_fetch", lambda s: {"event_score": 20})
        p, keep = _carry(monkeypatch)
        assert keep == set()

    def test_every_source_down_still_carries(self, monkeypatch):
        def down(*a):
            raise TimeoutError("x")
        monkeypatch.setattr(carry_context, "_fetch_nifty", down)
        monkeypatch.setattr(vix_gate, "_fetch", down)
        monkeypatch.setattr(event_risk, "_fetch", down)
        p, keep = _carry(monkeypatch)
        assert p.id in keep

    def test_switch_off_ignores_all_context(self, monkeypatch):
        monkeypatch.setattr(config, "CARRY_CONTEXT_GATE_ENABLED", False)
        monkeypatch.setattr(carry_context, "_fetch_nifty", lambda: -5.0)
        p, keep = _carry(monkeypatch)
        assert p.id in keep
