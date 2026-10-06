"""
group 214 (audit item A6, "quality-gate fail-open") - volume-shock quality gate vs candidates it could not score.

Before: both analysis-intelligence lookups timing out left every score None and the gate passed the symbol
("floor check skipped"). Now a symbol inside the scored batch whose fundamental AND technical lookups both failed
is skipped this cycle (retried next cycle). Unchanged: a service that ANSWERED with no score stays lenient, one
working lookup is enough to apply its floor, candidates beyond VOLUME_SHOCK_QUALITY_GATE_MAX_SYMBOLS stay
ungated, and VOLUME_SHOCK_QUALITY_FAIL_CLOSED=0 restores the old behaviour.

Run from services/real-trade-service:
    python3 -m pytest tests/test_group214_quality_gate_unscored.py -q
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import models
import intraday_eligibility
from candidate_engine import candidates as cd

_engine = create_engine("sqlite:///:memory:")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db():
    models.Base.metadata.drop_all(_engine)
    models.Base.metadata.create_all(_engine)
    s = sessionmaker(bind=_engine)()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("VOLUME_SHOCK_QUALITY_FAIL_CLOSED", raising=False)
    saved = dict(cd._sector_peer_history)
    yield
    cd._sector_peer_history.clear()
    cd._sector_peer_history.update(saved)


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _Client:
    def __init__(self, fund=None, tech=None):
        # each is a _Resp, an Exception to raise, or None (404)
        self.fund, self.tech = fund, tech

    async def get(self, url, timeout=None, params=None):
        item = self.fund if config.FUNDAMENTAL_URL in url else self.tech
        if isinstance(item, Exception):
            raise item
        return item if item is not None else _Resp(404)


# ---------------------------------------------------------------------------
# _fetch_fund_tech_score reports whether each lookup answered
# ---------------------------------------------------------------------------

class TestFetchFlags:
    def test_both_answered(self):
        r = run(cd._fetch_fund_tech_score(
            _Client(_Resp(200, {"fundamental_score": 70}), _Resp(200, {"technical_score": 65})), "TCS"))
        assert r["fund_fetched"] is True and r["tech_fetched"] is True

    def test_both_raise(self):
        r = run(cd._fetch_fund_tech_score(_Client(TimeoutError(), RuntimeError("x")), "TCS"))
        assert r["fund_fetched"] is False and r["tech_fetched"] is False
        assert r["fundamental_score"] is None and r["technical_score"] is None

    def test_non_200_is_not_fetched(self):
        r = run(cd._fetch_fund_tech_score(_Client(_Resp(503), _Resp(500)), "TCS"))
        assert r["fund_fetched"] is False and r["tech_fetched"] is False

    def test_answered_without_score_counts_as_fetched(self):
        r = run(cd._fetch_fund_tech_score(_Client(_Resp(200, {}), _Resp(200, {})), "TCS"))
        assert r["fund_fetched"] is True and r["tech_fetched"] is True
        assert r["fundamental_score"] is None

    def test_one_side_only(self):
        r = run(cd._fetch_fund_tech_score(_Client(TimeoutError(), _Resp(200, {"technical_score": 60})), "TCS"))
        assert r["fund_fetched"] is False and r["tech_fetched"] is True


# ---------------------------------------------------------------------------
# _quality_unscored / _quality_fail_closed
# ---------------------------------------------------------------------------

class TestUnscoredHelper:
    def test_none_is_unscored(self):
        assert cd._quality_unscored(None) is True

    def test_both_failed_is_unscored(self):
        assert cd._quality_unscored({"fund_fetched": False, "tech_fetched": False,
                                     "fundamental_score": None, "technical_score": None}) is True

    def test_one_answered_is_scored(self):
        assert cd._quality_unscored({"fund_fetched": False, "tech_fetched": True,
                                     "fundamental_score": None, "technical_score": None}) is False

    def test_answered_with_no_score_is_scored(self):
        assert cd._quality_unscored({"fund_fetched": True, "tech_fetched": True,
                                     "fundamental_score": None, "technical_score": None}) is False

    def test_result_without_flags_keeps_old_lenient_behaviour(self):
        assert cd._quality_unscored({"fundamental_score": None, "technical_score": None}) is False

    def test_a_score_present_is_never_unscored(self):
        assert cd._quality_unscored({"fund_fetched": False, "tech_fetched": False,
                                     "fundamental_score": 50, "technical_score": None}) is False

    @pytest.mark.parametrize("val, expected", [
        (None, True), ("", True), ("1", True), ("true", True), ("garbage", True),
        ("0", False), ("false", False), ("No", False), ("OFF", False), (" 0 ", False),
    ])
    def test_fail_closed_env(self, monkeypatch, val, expected):
        if val is None:
            monkeypatch.delenv("VOLUME_SHOCK_QUALITY_FAIL_CLOSED", raising=False)
        else:
            monkeypatch.setenv("VOLUME_SHOCK_QUALITY_FAIL_CLOSED", val)
        assert cd._quality_fail_closed() is expected


# ---------------------------------------------------------------------------
# _refresh_volume_shock_candidates end to end
# ---------------------------------------------------------------------------

def _wire(monkeypatch, symbols, qt):
    monkeypatch.setattr(intraday_eligibility, "get_restricted_symbols", lambda db: set())
    monkeypatch.setattr(config, "VOLUME_SHOCK_QUALITY_GATE_ENABLED", True)

    async def universe(client):
        return list(symbols)

    async def prefetch(client, syms):
        return None

    async def vs(client, symbol):
        return {"reject_reason": None, "today_return_pct": 3.0, "vol_multiple": 2.0, "atr_pct": 1.5,
                "current_price": 100.0, "high_conviction": False, "upper_circuit": False,
                "delivery_pct": None, "high_delivery": None, "time_stop_hint": "EOD+1", "backtest_note": "x"}

    monkeypatch.setattr(cd, "_fetch_volume_shock_universe", universe)
    monkeypatch.setattr(cd, "_prefetch_quotes_bulk", prefetch)
    monkeypatch.setattr(cd, "_volume_shock_analysis", vs)
    monkeypatch.setattr(cd, "_fetch_fund_tech_score", qt)


def _res(symbol, fund_fetched, tech_fetched, fs=None, ts=None):
    return {"symbol": symbol, "fundamental_score": fs, "technical_score": ts, "sector": "IT",
            "market_cap_cr": None, "adx": None, "fund_fetched": fund_fetched, "tech_fetched": tech_fetched}


class TestOrchestration:
    def test_both_lookups_failed_is_skipped_and_logged(self, db, monkeypatch, caplog):
        async def qt(client, symbol):
            return _res(symbol, False, False)
        _wire(monkeypatch, ["DOWN"], qt)
        with caplog.at_level("INFO"):
            inserted = run(cd._refresh_volume_shock_candidates(db, "REAL", set()))
        assert inserted == 0
        assert db.query(models.TradeCandidate).count() == 0
        assert any("could not score it" in m and "DOWN" in m for m in caplog.messages)
        assert any("quality_unscored=1" in m for m in caplog.messages)

    def test_env_off_restores_old_pass(self, db, monkeypatch):
        monkeypatch.setenv("VOLUME_SHOCK_QUALITY_FAIL_CLOSED", "0")

        async def qt(client, symbol):
            return _res(symbol, False, False)
        _wire(monkeypatch, ["DOWN"], qt)
        assert run(cd._refresh_volume_shock_candidates(db, "REAL", set())) == 1

    def test_service_answered_without_score_is_still_lenient(self, db, monkeypatch):
        async def qt(client, symbol):
            return _res(symbol, True, True)
        _wire(monkeypatch, ["NODATA"], qt)
        assert run(cd._refresh_volume_shock_candidates(db, "REAL", set())) == 1

    def test_one_working_lookup_is_enough_and_floor_still_applies(self, db, monkeypatch):
        async def qt(client, symbol):
            if symbol == "WEAKTECH":
                return _res(symbol, False, True, ts=5.0)
            return _res(symbol, False, True, ts=90.0)
        _wire(monkeypatch, ["WEAKTECH", "GOODTECH"], qt)
        assert run(cd._refresh_volume_shock_candidates(db, "REAL", set())) == 1
        assert [r.symbol for r in db.query(models.TradeCandidate).all()] == ["GOODTECH"]

    def test_mixed_batch_only_unscored_symbol_skipped(self, db, monkeypatch):
        async def qt(client, symbol):
            if symbol == "DOWN":
                return _res(symbol, False, False)
            return _res(symbol, True, True, fs=80.0, ts=80.0)
        _wire(monkeypatch, ["DOWN", "FINE"], qt)
        assert run(cd._refresh_volume_shock_candidates(db, "REAL", set())) == 1
        assert [r.symbol for r in db.query(models.TradeCandidate).all()] == ["FINE"]

    def test_beyond_cap_symbols_stay_ungated(self, db, monkeypatch):
        monkeypatch.setattr(config, "VOLUME_SHOCK_QUALITY_GATE_MAX_SYMBOLS", 1)
        scored = []

        async def qt(client, symbol):
            scored.append(symbol)
            return _res(symbol, False, False)
        _wire(monkeypatch, ["FIRST", "SECOND"], qt)
        monkeypatch.setattr(config, "VOLUME_SHOCK_QUALITY_GATE_MAX_SYMBOLS", 1)
        inserted = run(cd._refresh_volume_shock_candidates(db, "REAL", set()))
        assert scored == ["FIRST"]                       # only the first was sent to the scorer
        assert inserted == 1                             # FIRST skipped (unscored), SECOND ungated by the cost cap
        assert [r.symbol for r in db.query(models.TradeCandidate).all()] == ["SECOND"]

    def test_gate_disabled_inserts_everything(self, db, monkeypatch):
        async def qt(client, symbol):
            raise AssertionError("scorer must not run when the gate is disabled")
        _wire(monkeypatch, ["A", "B"], qt)
        monkeypatch.setattr(config, "VOLUME_SHOCK_QUALITY_GATE_ENABLED", False)
        assert run(cd._refresh_volume_shock_candidates(db, "REAL", set())) == 2
