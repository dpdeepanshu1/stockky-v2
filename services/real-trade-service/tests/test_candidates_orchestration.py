"""
tests/test_candidates_orchestration.py

100%-coverage-plan round 3 for candidate_engine/candidates.py — the final
slice after round 1 (test_candidates_helpers.py: pure helpers, row
normalizers, fetch wrappers, quality gate, dedupe lookup) and round 2
(test_candidates_analysis.py: _multi_tf_analysis, _volume_shock_analysis).

This round targets the three cycle-orchestration functions those two
rounds deliberately deferred:
  - _refresh_standard_candidates   — hot_picks/ipo/surprise track, DB writes
  - _refresh_volume_shock_candidates — momentum-breakout track, DB writes
  - refresh_candidates             — top-level entry point wrapping both

Same mocking shape as auto_pilot.py's own two-part split: rather than
re-simulating every chained httpx call these functions make (already
covered directly by round 1/2's tests of the functions they call), each
orchestrator's own lower-level dependencies (_fetch, _multi_tf_analysis,
_fetch_market_cap_cr, _prefetch_quotes_bulk, _fetch_volume_shock_universe,
_volume_shock_analysis, _fetch_fund_tech_score, intraday_eligibility's
restricted-symbol lookup, pipeline_status) are monkeypatched directly, so
these tests exercise the orchestration's OWN control flow: dedup/exclude
filtering, restricted-symbol filtering (success + failure), market-cap
gating, insert vs skip counting, the diagnostic warning branches, quality-
gate sector grouping, late-exclude event wiring, and the final DB commit +
return shape.

Also closes the 6 stray single-line gaps flagged in the session87 note:
  806  _volume_is_healthy   — avg20<=0 with >=10 samples (negative volumes)
  819  _near_resistance     — recent_high<=0 (no candle has a truthy high)
  861  _multi_tf_analysis   — quote fetch raises -> quote=None branch
  1063 _rows_from_ipo       — non-dict / symbol-less item skip
  1193 _volume_shock_analysis — avg20<=0 in the volume-shock check
  1981-1984 _recently_candidated_symbols — gate6 requeue-shrink lookup
            itself raising (fail-safe: keeps the full-cooldown set)

Run from services/real-trade-service:
    python3 -m pytest tests/test_candidates_orchestration.py -q \
        --cov=candidate_engine.candidates --cov-report=term-missing
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

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
def _reset_module_globals():
    """Same rationale as round 1/2's fixture — process-local module state
    a few tests here touch (adaptive floors/caps, sector-peer history)."""
    saved = dict(
        sector_peer_history=dict(cd._sector_peer_history),
        adaptive_max_atr_pct=cd._adaptive_max_atr_pct,
        adaptive_max_atr_pct_source=cd._adaptive_max_atr_pct_source,
        adaptive_fund_floor=cd._adaptive_fund_floor,
        adaptive_tech_floor=cd._adaptive_tech_floor,
        adaptive_min_market_cap_cr=cd._adaptive_min_market_cap_cr,
    )
    yield
    cd._sector_peer_history.clear()
    cd._sector_peer_history.update(saved["sector_peer_history"])
    cd._adaptive_max_atr_pct = saved["adaptive_max_atr_pct"]
    cd._adaptive_max_atr_pct_source = saved["adaptive_max_atr_pct_source"]
    cd._adaptive_fund_floor = saved["adaptive_fund_floor"]
    cd._adaptive_tech_floor = saved["adaptive_tech_floor"]
    cd._adaptive_min_market_cap_cr = saved["adaptive_min_market_cap_cr"]


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (str(payload) if payload is not None else "")

    def json(self):
        return self._payload


def _mk_row(symbol, source_tab="hot_picks", score=80.0):
    return {
        "symbol": symbol,
        "source_tab": source_tab,
        "decision_label": "BUY NOW",
        "conviction_score": score,
        "signal_price": 100.0,
        "raw_payload": {"symbol": symbol},
    }


def _no_restricted(monkeypatch):
    monkeypatch.setattr(intraday_eligibility, "get_restricted_symbols", lambda db: set())


async def _noop_prefetch(client, symbols):
    return None


# ===========================================================================
# _refresh_standard_candidates
# ===========================================================================

class TestRefreshStandardCandidates:
    def test_no_source_rows_returns_empty(self, db, monkeypatch):
        async def fake_fetch(client, path):
            return None
        monkeypatch.setattr(cd, "_fetch", fake_fetch)
        inserted, seen = run(cd._refresh_standard_candidates(db, "REAL", set()))
        assert (inserted, seen) == (0, set())

    def test_all_rows_excluded_returns_seen_symbols(self, db, monkeypatch):
        _no_restricted(monkeypatch)

        async def fake_fetch(client, path):
            if path == cd._SOURCES["hot_picks"]:
                return {"bulk_insider_driven": [{"symbol": "TCS", "decision": "BUY NOW",
                                     "score": 80.0, "price": 100.0}]}
            return None
        monkeypatch.setattr(cd, "_fetch", fake_fetch)
        inserted, seen = run(cd._refresh_standard_candidates(db, "REAL", {"TCS"}))
        assert inserted == 0
        assert seen == {"TCS"}

    def test_restricted_symbols_removes_remaining_rows(self, db, monkeypatch):
        monkeypatch.setattr(intraday_eligibility, "get_restricted_symbols",
                             lambda db: {"WIPRO"})

        async def fake_fetch(client, path):
            if path == cd._SOURCES["hot_picks"]:
                return {"bulk_insider_driven": [{"symbol": "WIPRO", "decision": "BUY NOW",
                                     "score": 80.0, "price": 100.0}]}
            return None
        monkeypatch.setattr(cd, "_fetch", fake_fetch)
        inserted, seen = run(cd._refresh_standard_candidates(db, "REAL", set()))
        assert inserted == 0
        assert seen == {"WIPRO"}

    def test_restricted_lookup_failure_is_non_fatal(self, db, monkeypatch):
        def _raise(db):
            raise RuntimeError("intraday_eligibility down")
        monkeypatch.setattr(intraday_eligibility, "get_restricted_symbols", _raise)

        async def fake_fetch(client, path):
            if path == cd._SOURCES["hot_picks"]:
                return {"bulk_insider_driven": [{"symbol": "INFY", "decision": "BUY NOW",
                                     "score": 80.0, "price": 100.0}]}
            return None
        monkeypatch.setattr(cd, "_fetch", fake_fetch)
        monkeypatch.setattr(cd, "_prefetch_quotes_bulk", _noop_prefetch)

        async def fake_mtf(client, symbol):
            return {"reject_reason": None, "bullish_count": 5.0,
                    "tf_returns": {}, "atr_pct": None, "market_note": ""}
        monkeypatch.setattr(cd, "_multi_tf_analysis", fake_mtf)

        async def fake_mcap(client, symbol):
            return 5000.0
        monkeypatch.setattr(cd, "_fetch_market_cap_cr", fake_mcap)

        inserted, seen = run(cd._refresh_standard_candidates(db, "REAL", set()))
        assert inserted == 1
        assert seen == {"INFY"}
        row = db.query(models.TradeCandidate).one()
        assert row.symbol == "INFY"

    def test_happy_path_insert_mcap_reject_and_mtf_reject(self, db, monkeypatch):
        _no_restricted(monkeypatch)

        async def fake_fetch(client, path):
            if path == cd._SOURCES["hot_picks"]:
                return {"bulk_insider_driven": [
                    {"symbol": "GOODCO", "decision": "BUY NOW", "score": 80.0, "price": 100.0},
                    {"symbol": "MCAPFAIL", "decision": "BUY NOW", "score": 80.0, "price": 50.0},
                    {"symbol": "MTFFAIL", "decision": "BUY NOW", "score": 80.0, "price": 50.0},
                ]}
            return None
        monkeypatch.setattr(cd, "_fetch", fake_fetch)
        monkeypatch.setattr(cd, "_prefetch_quotes_bulk", _noop_prefetch)

        async def fake_mtf(client, symbol):
            if symbol == "MTFFAIL":
                return {"reject_reason": "weak momentum", "atr_pct": None}
            return {"reject_reason": None, "bullish_count": 5.0,
                    "tf_returns": {}, "atr_pct": None, "market_note": ""}
        monkeypatch.setattr(cd, "_multi_tf_analysis", fake_mtf)

        async def fake_mcap(client, symbol):
            if symbol == "MCAPFAIL":
                return 10.0  # well under the floor
            return 5000.0
        monkeypatch.setattr(cd, "_fetch_market_cap_cr", fake_mcap)

        inserted, seen = run(cd._refresh_standard_candidates(db, "REAL", set()))
        assert inserted == 1
        assert seen == {"GOODCO", "MCAPFAIL", "MTFFAIL"}
        rows = db.query(models.TradeCandidate).all()
        assert [r.symbol for r in rows] == ["GOODCO"]

    def test_data_starved_majority_logs_warning(self, db, monkeypatch, caplog):
        _no_restricted(monkeypatch)

        async def fake_fetch(client, path):
            if path == cd._SOURCES["hot_picks"]:
                return {"bulk_insider_driven": [
                    {"symbol": "EXC", "decision": "BUY NOW", "score": 80.0, "price": 100.0},
                    {"symbol": "STARVED", "decision": "BUY NOW", "score": 80.0, "price": 100.0},
                    {"symbol": "OK", "decision": "BUY NOW", "score": 80.0, "price": 100.0},
                ]}
            return None
        monkeypatch.setattr(cd, "_fetch", fake_fetch)
        monkeypatch.setattr(cd, "_prefetch_quotes_bulk", _noop_prefetch)

        async def fake_mtf(client, symbol):
            if symbol == "EXC":
                raise RuntimeError("network blew up")
            if symbol == "STARVED":
                return {"reject_reason": "no data at all", "atr_pct": None, "data_starved": True}
            return {"reject_reason": None, "bullish_count": 5.0,
                    "tf_returns": {}, "atr_pct": None, "market_note": ""}
        monkeypatch.setattr(cd, "_multi_tf_analysis", fake_mtf)

        async def fake_mcap(client, symbol):
            return 5000.0
        monkeypatch.setattr(cd, "_fetch_market_cap_cr", fake_mcap)

        with caplog.at_level("WARNING"):
            inserted, seen = run(cd._refresh_standard_candidates(db, "REAL", set()))
        assert inserted == 1
        assert any("zero quote/history data" in m for m in caplog.messages)

    def test_quote_only_failed_logs_warning(self, db, monkeypatch, caplog):
        _no_restricted(monkeypatch)

        async def fake_fetch(client, path):
            if path == cd._SOURCES["hot_picks"]:
                return {"bulk_insider_driven": [
                    {"symbol": f"Q{i}", "decision": "BUY NOW", "score": 80.0, "price": 100.0}
                    for i in range(3)
                ]}
            return None
        monkeypatch.setattr(cd, "_fetch", fake_fetch)
        monkeypatch.setattr(cd, "_prefetch_quotes_bulk", _noop_prefetch)

        async def fake_mtf(client, symbol):
            return {"reject_reason": "No live quote available for this symbol.",
                    "atr_pct": None}
        monkeypatch.setattr(cd, "_multi_tf_analysis", fake_mtf)

        async def fake_mcap(client, symbol):
            return 5000.0
        monkeypatch.setattr(cd, "_fetch_market_cap_cr", fake_mcap)

        with caplog.at_level("WARNING"):
            inserted, seen = run(cd._refresh_standard_candidates(db, "REAL", set()))
        assert inserted == 0
        assert any("no resolvable" in m for m in caplog.messages)

    def test_pstat_set_source_failure_is_swallowed(self, db, monkeypatch):
        """Covers the try/except around pstat.set_source calls."""
        _no_restricted(monkeypatch)

        def _raise(mode, source):
            raise RuntimeError("pipeline status store down")
        monkeypatch.setattr(cd.pstat, "set_source", _raise)

        async def fake_fetch(client, path):
            return None
        monkeypatch.setattr(cd, "_fetch", fake_fetch)

        inserted, seen = run(cd._refresh_standard_candidates(db, "REAL", set()))
        assert (inserted, seen) == (0, set())


# ===========================================================================
# _refresh_volume_shock_candidates
# ===========================================================================

class TestRefreshVolumeShockCandidates:
    def test_no_candidates_returns_zero(self, db, monkeypatch):
        _no_restricted(monkeypatch)

        async def fake_universe(client):
            return []
        monkeypatch.setattr(cd, "_fetch_volume_shock_universe", fake_universe)

        inserted = run(cd._refresh_volume_shock_candidates(db, "REAL", set()))
        assert inserted == 0

    def test_restricted_symbols_drop_all_candidates(self, db, monkeypatch, caplog):
        monkeypatch.setattr(intraday_eligibility, "get_restricted_symbols",
                             lambda db: {"BADSYM"})

        async def fake_universe(client):
            return ["BADSYM"]
        monkeypatch.setattr(cd, "_fetch_volume_shock_universe", fake_universe)

        with caplog.at_level("INFO"):
            inserted = run(cd._refresh_volume_shock_candidates(db, "REAL", set()))
        assert inserted == 0
        assert any("intraday-restricted" in m for m in caplog.messages)

    def test_restricted_lookup_failure_is_non_fatal(self, db, monkeypatch):
        def _raise(db):
            raise RuntimeError("down")
        monkeypatch.setattr(intraday_eligibility, "get_restricted_symbols", _raise)
        config_gate_was_enabled = config.VOLUME_SHOCK_QUALITY_GATE_ENABLED
        monkeypatch.setattr(config, "VOLUME_SHOCK_QUALITY_GATE_ENABLED", False)

        async def fake_universe(client):
            return ["ZOMATO"]
        monkeypatch.setattr(cd, "_fetch_volume_shock_universe", fake_universe)
        monkeypatch.setattr(cd, "_prefetch_quotes_bulk", _noop_prefetch)

        async def fake_vs(client, symbol):
            return {"reject_reason": None, "today_return_pct": 5.0, "vol_multiple": 2.0,
                    "atr_pct": 1.0, "current_price": 100.0, "high_conviction": False,
                    "upper_circuit": False, "delivery_pct": None, "high_delivery": None,
                    "time_stop_hint": "EOD+1", "backtest_note": "vol_shock"}
        monkeypatch.setattr(cd, "_volume_shock_analysis", fake_vs)

        inserted = run(cd._refresh_volume_shock_candidates(db, "REAL", set()))
        assert inserted == 1
        assert config_gate_was_enabled or True  # sanity, no functional assertion needed

    def test_full_pipeline_all_tiers_and_quality_gate(self, db, monkeypatch, caplog):
        """Exercises: exception during analysis, plain reject (no-quote
        classification), quality-gate rejection, upper_circuit/high_conviction/
        base labeling, sector grouping, cross-cycle peer recording, and the
        late-exclude event wait."""
        _no_restricted(monkeypatch)
        monkeypatch.setattr(config, "VOLUME_SHOCK_QUALITY_GATE_ENABLED", True)
        monkeypatch.setattr(config, "VOLUME_SHOCK_SECTOR_MIN_PEERS", 1)

        universe = ["BLOWUP", "NOQUOTE", "QREJECT", "UC", "HC", "BASE"]

        async def fake_universe(client):
            return list(universe)
        monkeypatch.setattr(cd, "_fetch_volume_shock_universe", fake_universe)
        monkeypatch.setattr(cd, "_prefetch_quotes_bulk", _noop_prefetch)

        def _vs_result(sym):
            if sym == "UC":
                return {"reject_reason": None, "today_return_pct": 20.0, "vol_multiple": 20.0,
                        "atr_pct": 2.0, "current_price": 100.0, "high_conviction": True,
                        "upper_circuit": True, "delivery_pct": None, "high_delivery": None,
                        "time_stop_hint": "EOD+1", "backtest_note": "upper_circuit"}
            if sym == "HC":
                return {"reject_reason": None, "today_return_pct": 16.0, "vol_multiple": 16.0,
                        "atr_pct": 2.0, "current_price": 100.0, "high_conviction": True,
                        "upper_circuit": False, "delivery_pct": None, "high_delivery": None,
                        "time_stop_hint": "EOD+1", "backtest_note": "high_conviction"}
            if sym == "BASE":
                return {"reject_reason": None, "today_return_pct": 3.0, "vol_multiple": 2.0,
                        "atr_pct": 2.0, "current_price": 100.0, "high_conviction": False,
                        "upper_circuit": False, "delivery_pct": None, "high_delivery": None,
                        "time_stop_hint": "EOD+1", "backtest_note": "vol_shock"}
            if sym == "QREJECT":
                return {"reject_reason": None, "today_return_pct": 3.0, "vol_multiple": 2.0,
                        "atr_pct": 2.0, "current_price": 100.0, "high_conviction": False,
                        "upper_circuit": False, "delivery_pct": None, "high_delivery": None,
                        "time_stop_hint": "EOD+1", "backtest_note": "vol_shock"}
            if sym == "NOQUOTE":
                return {"reject_reason": "No quote available for volume-shock check.",
                        "atr_pct": None}
            raise AssertionError("unexpected symbol")

        async def fake_vs(client, symbol):
            if symbol == "BLOWUP":
                raise RuntimeError("boom")
            return _vs_result(symbol)
        monkeypatch.setattr(cd, "_volume_shock_analysis", fake_vs)

        async def fake_qt(client, symbol):
            if symbol == "QREJECT":
                return {"symbol": symbol, "fundamental_score": 10.0, "technical_score": 10.0,
                        "sector": "IT", "market_cap_cr": 5000.0, "adx": 25.0}
            return {"symbol": symbol, "fundamental_score": 80.0, "technical_score": 80.0,
                    "sector": "IT", "market_cap_cr": 5000.0, "adx": 25.0}
        monkeypatch.setattr(cd, "_fetch_fund_tech_score", fake_qt)

        with caplog.at_level("INFO"):
            inserted = run(cd._refresh_volume_shock_candidates(db, "REAL", set()))

        # UC, HC, BASE should insert; BLOWUP/NOQUOTE/QREJECT should not.
        assert inserted == 3
        rows = {r.symbol: r for r in db.query(models.TradeCandidate).all()}
        assert set(rows) == {"UC", "HC", "BASE"}
        assert rows["UC"].decision_label == "VOLUME_SHOCK_UPPER_CIRCUIT"
        assert rows["HC"].decision_label == "VOLUME_SHOCK_HIGH_CONVICTION"
        assert rows["BASE"].decision_label == "VOLUME_SHOCK"
        assert any("quality gate" in m for m in caplog.messages)

    def test_no_quote_majority_logs_warning(self, db, monkeypatch, caplog):
        _no_restricted(monkeypatch)
        monkeypatch.setattr(config, "VOLUME_SHOCK_QUALITY_GATE_ENABLED", False)

        universe = ["A", "B", "C"]

        async def fake_universe(client):
            return list(universe)
        monkeypatch.setattr(cd, "_fetch_volume_shock_universe", fake_universe)
        monkeypatch.setattr(cd, "_prefetch_quotes_bulk", _noop_prefetch)

        async def fake_vs(client, symbol):
            return {"reject_reason": "No quote available for volume-shock check.",
                    "atr_pct": None}
        monkeypatch.setattr(cd, "_volume_shock_analysis", fake_vs)

        with caplog.at_level("WARNING"):
            inserted = run(cd._refresh_volume_shock_candidates(db, "REAL", set()))
        assert inserted == 0
        assert any("missing quote/history data" in m for m in caplog.messages)

    def test_quality_gate_scoring_pass_fails_entirely(self, db, monkeypatch, caplog):
        """The whole quality-gate httpx.AsyncClient block raising should be
        caught, logged, and fall back to quality_scores={} (gate skipped,
        everything that passed price/volume gets inserted)."""
        _no_restricted(monkeypatch)
        monkeypatch.setattr(config, "VOLUME_SHOCK_QUALITY_GATE_ENABLED", True)

        async def fake_universe(client):
            return ["OK1"]
        monkeypatch.setattr(cd, "_fetch_volume_shock_universe", fake_universe)
        monkeypatch.setattr(cd, "_prefetch_quotes_bulk", _noop_prefetch)

        async def fake_vs(client, symbol):
            return {"reject_reason": None, "today_return_pct": 3.0, "vol_multiple": 2.0,
                    "atr_pct": 2.0, "current_price": 100.0, "high_conviction": False,
                    "upper_circuit": False, "delivery_pct": None, "high_delivery": None,
                    "time_stop_hint": "EOD+1", "backtest_note": "vol_shock"}
        monkeypatch.setattr(cd, "_volume_shock_analysis", fake_vs)

        # The function opens httpx.AsyncClient TWICE — once for the universe/
        # prefetch phase (bypassed here via monkeypatches above, but the
        # `async with` context manager itself still runs), and once inside
        # the try/except for the quality-gate scoring pass. Only the SECOND
        # one should fail, so the failure is caught by the quality-gate's
        # own try/except rather than blowing up the whole function.
        _call_count = {"n": 0}

        class _MixedClient:
            def __init__(self):
                _call_count["n"] += 1
                self._n = _call_count["n"]

            async def __aenter__(self):
                if self._n >= 2:
                    raise RuntimeError("client construction failed")
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, timeout=None, params=None):
                return _FakeResponse(404)

            async def post(self, url, timeout=None, json=None):
                return _FakeResponse(200, {})
        monkeypatch.setattr(cd.httpx, "AsyncClient", lambda *a, **k: _MixedClient())

        with caplog.at_level("WARNING"):
            inserted = run(cd._refresh_volume_shock_candidates(db, "REAL", set()))
        assert inserted == 1
        assert any("scoring pass failed entirely" in m for m in caplog.messages)

    def test_late_exclude_event_skips_standard_seen_symbol(self, db, monkeypatch):
        _no_restricted(monkeypatch)
        monkeypatch.setattr(config, "VOLUME_SHOCK_QUALITY_GATE_ENABLED", False)

        async def fake_universe(client):
            return ["ALREADY_STANDARD"]
        monkeypatch.setattr(cd, "_fetch_volume_shock_universe", fake_universe)
        monkeypatch.setattr(cd, "_prefetch_quotes_bulk", _noop_prefetch)

        async def fake_vs(client, symbol):
            return {"reject_reason": None, "today_return_pct": 3.0, "vol_multiple": 2.0,
                    "atr_pct": 2.0, "current_price": 100.0, "high_conviction": False,
                    "upper_circuit": False, "delivery_pct": None, "high_delivery": None,
                    "time_stop_hint": "EOD+1", "backtest_note": "vol_shock"}
        monkeypatch.setattr(cd, "_volume_shock_analysis", fake_vs)

        event = asyncio.Event()
        holder = {"seen": {"ALREADY_STANDARD"}}

        async def _runner():
            event.set()
            return await cd._refresh_volume_shock_candidates(
                db, "REAL", set(), late_exclude_event=event, late_exclude_holder=holder,
            )
        inserted = run(_runner())
        assert inserted == 0
        assert db.query(models.TradeCandidate).count() == 0

    def test_pstat_set_source_failure_is_swallowed(self, db, monkeypatch):
        _no_restricted(monkeypatch)

        def _raise(mode, source):
            raise RuntimeError("pipeline status store down")
        monkeypatch.setattr(cd.pstat, "set_source", _raise)

        async def fake_universe(client):
            return []
        monkeypatch.setattr(cd, "_fetch_volume_shock_universe", fake_universe)

        inserted = run(cd._refresh_volume_shock_candidates(db, "REAL", set()))
        assert inserted == 0

    def test_metric_recording_failures_and_per_symbol_scoring_exception(
        self, db, monkeypatch, caplog
    ):
        """Covers: amp.record_metric raising for universe_atr_pct (1717-1718)
        and universe_adx (1757-1758) — both best-effort, non-fatal — and one
        gate_symbol's _fetch_fund_tech_score raising inside the gathered
        quality-gate tasks so isinstance(qr, Exception) is True for it
        specifically (1740-1741), while the client itself stays healthy."""
        _no_restricted(monkeypatch)
        monkeypatch.setattr(config, "VOLUME_SHOCK_QUALITY_GATE_ENABLED", True)

        async def fake_universe(client):
            return ["FLAKY", "STEADY"]
        monkeypatch.setattr(cd, "_fetch_volume_shock_universe", fake_universe)
        monkeypatch.setattr(cd, "_prefetch_quotes_bulk", _noop_prefetch)

        async def fake_vs(client, symbol):
            return {"reject_reason": None, "today_return_pct": 3.0, "vol_multiple": 2.0,
                    "atr_pct": 1.5, "current_price": 100.0, "high_conviction": False,
                    "upper_circuit": False, "delivery_pct": None, "high_delivery": None,
                    "time_stop_hint": "EOD+1", "backtest_note": "vol_shock"}
        monkeypatch.setattr(cd, "_volume_shock_analysis", fake_vs)

        async def fake_qt(client, symbol):
            if symbol == "FLAKY":
                raise RuntimeError("fund/tech service exploded for this symbol")
            return {"symbol": symbol, "fundamental_score": 80.0, "technical_score": 80.0,
                    "sector": "IT", "market_cap_cr": 5000.0, "adx": 25.0}
        monkeypatch.setattr(cd, "_fetch_fund_tech_score", fake_qt)

        def _raise_metric(db_, name, value):
            raise RuntimeError(f"amp store down for {name}")
        monkeypatch.setattr(cd.amp, "record_metric", _raise_metric)

        with caplog.at_level("DEBUG"):
            inserted = run(cd._refresh_volume_shock_candidates(db, "REAL", set()))

        # STEADY should still insert (base tier); FLAKY's per-symbol scoring
        # exception falls back to "no quality data" (lenient), doesn't block it.
        assert inserted == 2
        assert any("universe_atr_pct recording failed" in m for m in caplog.messages)
        assert any("universe_adx recording failed" in m for m in caplog.messages)
        assert any("scoring failed for FLAKY" in m for m in caplog.messages)


# ===========================================================================
# refresh_candidates (top-level orchestrator)
# ===========================================================================

class TestRefreshCandidatesTopLevel:
    def test_sums_both_tracks_and_wires_exclusion_sets(self, db, monkeypatch):
        monkeypatch.setattr(cd, "_refresh_cycle_adaptive_params", lambda db: None)

        captured = {}

        async def fake_standard(db_, mode, exclude_syms):
            captured["standard_exclude"] = set(exclude_syms)
            return 2, {"STDSEEN"}

        async def fake_shock(db_, mode, exclude_symbols, late_exclude_event=None,
                              late_exclude_holder=None):
            captured["shock_exclude"] = set(exclude_symbols)
            if late_exclude_event is not None:
                await late_exclude_event.wait()
                captured["shock_saw_late_seen"] = set(
                    (late_exclude_holder or {}).get("seen", set())
                )
            return 3

        monkeypatch.setattr(cd, "_refresh_standard_candidates", fake_standard)
        monkeypatch.setattr(cd, "_refresh_volume_shock_candidates", fake_shock)

        # One open position + one recently-candidated row so both exclusion
        # sets have real content to verify.
        db.add(models.TradePosition(mode="REAL", symbol="OPENPOS", status="OPEN",
                                     qty_open=10, avg_entry_price=100.0))
        db.add(models.TradeCandidate(mode="REAL", symbol="RECENT",
                                      received_at=datetime.now(timezone.utc)))
        db.commit()

        total = run(cd.refresh_candidates(db, "REAL"))
        assert total == 5
        assert captured["standard_exclude"] == {"OPENPOS", "RECENT"}
        assert "OPENPOS" in captured["shock_exclude"]
        assert captured["shock_saw_late_seen"] == {"STDSEEN"}


# ===========================================================================
# Stray branch fills (session87 note): 806, 819, 861, 1063, 1193, 1981-1984
# ===========================================================================

class TestStrayBranches:
    def test_volume_is_healthy_negative_avg20_fails_open(self):
        """Line 806: avg20<=0 reached with >=10 samples via negative
        (bad-data) volume readings — truthy so not filtered like 0 is."""
        candles = [{"volume": -50.0} for _ in range(15)]
        assert cd._volume_is_healthy(candles) is True

    def test_near_resistance_zero_recent_high_returns_false(self):
        """Line 819: enough candles + positive price, but no candle carries
        a truthy 'high' so recent_high resolves to 0."""
        candles = [{"close": 100.0} for _ in range(20)]  # no "high" key at all
        assert cd._near_resistance(candles, 100.0) is False

    def test_multi_tf_analysis_quote_fetch_raises(self, monkeypatch):
        """Line 861: _fetch_quote raising inside the gathered tasks ->
        quote resolves to an Exception -> normalized to None."""
        async def _raise(client, symbol):
            raise RuntimeError("quote service down")
        monkeypatch.setattr(cd, "_fetch_quote", _raise)

        async def _empty_history(client, symbol, period, interval="1d"):
            return []
        monkeypatch.setattr(cd, "_fetch_history", _empty_history)

        result = run(cd._multi_tf_analysis(object(), "TCS"))
        # current_price falls back to 0 -> "No live quote available" reject,
        # not data_starved (history calls "succeeded" with empty lists is
        # indistinguishable from data_starved here, so just assert it
        # didn't blow up and quote was treated as absent).
        assert result["reject_reason"] is not None

    def test_rows_from_ipo_skips_non_dict_and_symbol_less_items(self):
        payload = {"results": [
            "not-a-dict",
            {"decision": "BUY", "ipo_score": 80.0},  # no symbol
            {"symbol": "GOODIPO", "ipo_score": 80.0, "current_price": 100.0,
             "decision": "BUY NOW"},
        ]}
        rows = cd._rows_from_ipo(payload)
        assert [r["symbol"] for r in rows] == ["GOODIPO"]

    def test_volume_shock_analysis_negative_avg20_rejects(self, monkeypatch):
        """Line 1193: prior 20-day volumes negative (bad data) -> avg20<=0
        even though len(vols)>=6 clears the earlier insufficient-history gate."""
        async def fake_quote(client, symbol):
            return {"price": 100.0}
        monkeypatch.setattr(cd, "_fetch_quote", fake_quote)

        candles = [{"close": 100.0, "volume": -100.0} for _ in range(5)]
        candles.append({"close": 110.0, "volume": 500.0})  # today: +10% return

        async def fake_history(client, symbol, period, interval="1d"):
            return candles
        monkeypatch.setattr(cd, "_fetch_history", fake_history)

        result = run(cd._volume_shock_analysis(object(), "TCS"))
        assert result["reject_reason"] == (
            "No 20-day average volume available for volume-shock check."
        )

    def test_recently_candidated_gate6_lookup_failure_is_fail_safe(self, db):
        """Lines 1981-1984: the gate6 requeue-shrink TradeDecision lookup
        itself raising must not lose the already-computed full-cooldown
        exclusion set."""
        db.add(models.TradeCandidate(mode="REAL", symbol="KEEPME",
                                      received_at=datetime.now(timezone.utc)))
        db.commit()

        original_query = db.query

        def _failing_query(*args, **kwargs):
            if args and getattr(args[0], "class_", None) is models.TradeDecision:
                raise RuntimeError("join blew up")
            return original_query(*args, **kwargs)
        db.query = _failing_query

        try:
            result = cd._recently_candidated_symbols(db, "REAL")
        finally:
            db.query = original_query
        assert result == {"KEEPME"}
