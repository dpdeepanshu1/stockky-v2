"""group 434: entry_engine/timesfm_gate.py (Gate 4g, LOG ONLY) - the pure verdict, the cache-only fetch, and the entry.py wiring (DEMO mode, same
harness as test_group425_kronos_entry_wiring.py). The service is replaced by a fake `fetch_features`, so nothing touches the network."""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
import models
from entry_engine import entry, timesfm_gate
from tests.test_entry_evaluate_mode import approve_all, db, make_candidate, pin, quotes, run, tick  # noqa: F401  (fixtures)


def _ft(**kw):
    f = {"symbol": "GOODCO", "as_of": "2026-10-16", "vol_ratio": 1.0, "p_stop_close_max": 0.10, "volume_pace": 1.0, "fan_range_pct": 4.0}
    f.update(kw)
    return f


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    for k, v in (("TIMESFM_GATE_MODE", "off"), ("TIMESFM_GATE_DEMO_MODE", "off"), ("TIMESFM_MAX_VOL_RATIO", 1.6), ("TIMESFM_MAX_P_STOP", 0.35),
                 ("TIMESFM_MIN_VOLUME_PACE", 0.6), ("TIMESFM_URL", "http://tsfm.invalid:1"), ("TIMESFM_TIMEOUT_S", 0.5),
                 ("TIMESFM_CACHE_TTL_S", 600.0), ("TIMESFM_FAIL_TTL_S", 120.0)):
        monkeypatch.setattr(config, k, v, raising=False)
    timesfm_gate.reset()


# ── pure verdict ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_quiet_forecast_has_no_reasons():
    v = timesfm_gate.evaluate(_ft())
    assert v["opinion"] == "ok" and v["would_reject"] is False and v["reasons"] == []


@pytest.mark.parametrize("kw,word", [({"vol_ratio": 1.8}, "volatility"), ({"p_stop_close_max": 0.5}, "stop"), ({"volume_pace": 0.3}, "volume")])
def test_each_hypothetical_reason(kw, word):
    v = timesfm_gate.evaluate(_ft(**kw))
    assert v["would_reject"] is True and len(v["reasons"]) == 1 and word in v["reasons"][0]


def test_thresholds_of_zero_disable_a_rule(monkeypatch):
    monkeypatch.setattr(config, "TIMESFM_MAX_VOL_RATIO", 0.0)
    assert timesfm_gate.evaluate(_ft(vol_ratio=9.0))["would_reject"] is False


@pytest.mark.parametrize("ft", [None, {}, "x", {"vol_ratio": "nan", "p_stop_close_max": None, "volume_pace": None}])
def test_no_usable_forecast_is_no_opinion(ft):
    v = timesfm_gate.evaluate(ft)
    assert v["opinion"] == "no_opinion" and v["would_reject"] is False and v["no_data"]


def test_partial_forecast_still_gives_an_opinion_on_what_it_has():
    v = timesfm_gate.evaluate({"vol_ratio": 2.0})
    assert v["opinion"] == "ok" and v["would_reject"] is True


# ── modes: there is no enforce ──────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,expect", [("log", "log"), ("enforce", "off"), ("ON", "off"), ("", "off"), ("off", "off")])
def test_mode_values(monkeypatch, raw, expect):
    monkeypatch.setattr(config, "TIMESFM_GATE_MODE", raw)           # even a raw enforce reaching mode_for() is off
    assert timesfm_gate.mode_for("REAL") == expect


def test_config_parser_never_returns_enforce(monkeypatch):
    import importlib
    monkeypatch.setenv("TIMESFM_GATE_MODE", "enforce")
    monkeypatch.delenv("TIMESFM_GATE_DEMO_MODE", raising=False)
    try:
        importlib.reload(config)
        assert config.TIMESFM_GATE_MODE == "off" and config.TIMESFM_GATE_DEMO_MODE == "off" and config.TIMESFM_GATE_REQUESTED == "enforce"
        monkeypatch.setenv("TIMESFM_GATE_MODE", "log")
        importlib.reload(config)
        assert config.TIMESFM_GATE_MODE == "log" and config.TIMESFM_GATE_DEMO_MODE == "log"
    finally:
        monkeypatch.delenv("TIMESFM_GATE_MODE", raising=False)
        importlib.reload(config)


# ── fetch: cache-only, fail-open, cached ────────────────────────────────────────────────────────────────────────────────────────────
def _fake_httpx(monkeypatch, status=200, payload=None, seen=None):
    import httpx

    class _R:
        status_code = status

        def json(self): return payload if payload is not None else {}

    class _C:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def get(self, url, params=None, timeout=None):
            if seen is not None:
                seen.append((url, params, timeout))
            return _R()
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _C())


def test_fetch_asks_for_cached_only_and_caches_the_answer(monkeypatch):
    seen = []
    _fake_httpx(monkeypatch, 200, _ft(), seen)
    a = asyncio.run(timesfm_gate.fetch_features("goodco", 3.0, 6.0))
    b = asyncio.run(timesfm_gate.fetch_features("GOODCO", 3.0, 6.0))
    assert a == b == _ft() and len(seen) == 1
    assert seen[0][0].endswith("/features/GOODCO") and seen[0][1]["cached_only"] == "true" and seen[0][2] == 0.5
    assert timesfm_gate.stats()["fetch_ok"] == 1 and timesfm_gate.stats()["fetch_cache_hit"] == 1


@pytest.mark.parametrize("status", [404, 500])
def test_fetch_failure_is_none_and_remembered(monkeypatch, status):
    seen = []
    _fake_httpx(monkeypatch, status, {}, seen)
    assert asyncio.run(timesfm_gate.fetch_features("GOODCO", 3.0, 6.0)) is None
    assert asyncio.run(timesfm_gate.fetch_features("GOODCO", 3.0, 6.0)) is None
    assert len(seen) == 1 and timesfm_gate.stats()["fetch_fail"] == 1


def test_fetch_unreachable_service_and_bad_args_are_none():
    assert asyncio.run(timesfm_gate.fetch_features("GOODCO", 3.0, 6.0)) is None         # tsfm.invalid cannot resolve
    assert asyncio.run(timesfm_gate.fetch_features("", 3.0, 6.0)) is None
    assert asyncio.run(timesfm_gate.fetch_features("GOODCO", -1, 6.0)) is None


# ── entry.py wiring ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def _service(monkeypatch, features=None, raises=False):
    calls = []

    async def _fetch(symbol, stop_pct, target_pct):
        calls.append((symbol, stop_pct, target_pct))
        if raises:
            raise RuntimeError("boom")
        return features
    monkeypatch.setattr(timesfm_gate, "fetch_features", _fetch)
    return calls


def _setup(db, monkeypatch):
    make_candidate(db, symbol="GOODCO", conviction=80.0)
    quotes(monkeypatch, {"GOODCO": tick(100.0, symbol="GOODCO")})
    approve_all(monkeypatch)


def test_off_never_asks_the_service_and_enters(db, monkeypatch):
    calls = _service(monkeypatch, _ft(vol_ratio=9.0))
    _setup(db, monkeypatch)
    assert run(entry.evaluate_mode(db, "DEMO", gate_armed=True))["entered"] == 1
    assert calls == [] and timesfm_gate.stats()["evaluated"] == 0


def test_log_mode_records_a_would_reject_but_never_blocks(db, monkeypatch):
    monkeypatch.setattr(config, "TIMESFM_GATE_DEMO_MODE", "log")
    calls = _service(monkeypatch, _ft(vol_ratio=9.0, p_stop_close_max=0.9, volume_pace=0.1))
    _setup(db, monkeypatch)
    assert run(entry.evaluate_mode(db, "DEMO", gate_armed=True))["entered"] == 1
    st = timesfm_gate.stats()
    assert len(calls) == 1 and st["evaluated"] == 1 and st["would_reject"] == 1
    assert calls[0][1] == pytest.approx(3.0, abs=0.5) and calls[0][2] > calls[0][1]          # ATR stop / target percentages were passed
    row = st["recent"][0]
    assert row["symbol"] == "GOODCO" and row["mode"] == "DEMO" and len(row["reasons"]) == 3 and row["price"] == 100.0
    assert db.query(models.TradeDecision).filter_by(symbol="GOODCO").one().gate_tag != "timesfm"


def test_enforce_request_is_treated_as_off(db, monkeypatch):
    monkeypatch.setattr(config, "TIMESFM_GATE_DEMO_MODE", "enforce")             # what an old / typo'd env could carry: mode_for() maps it to off
    calls = _service(monkeypatch, _ft(vol_ratio=9.0))
    _setup(db, monkeypatch)
    assert run(entry.evaluate_mode(db, "DEMO", gate_armed=True))["entered"] == 1 and calls == []


@pytest.mark.parametrize("ft,raises", [(None, False), ({}, False), (None, True)])
def test_log_mode_without_an_opinion_enters(db, monkeypatch, ft, raises):
    monkeypatch.setattr(config, "TIMESFM_GATE_DEMO_MODE", "log")
    _service(monkeypatch, ft, raises=raises)
    _setup(db, monkeypatch)
    assert run(entry.evaluate_mode(db, "DEMO", gate_armed=True))["entered"] == 1


def test_record_writes_one_greppable_line(caplog):
    import logging
    caplog.set_level(logging.INFO, logger="real-trade-timesfm-gate")
    timesfm_gate.record("GOODCO", "DEMO", timesfm_gate.evaluate(_ft(vol_ratio=2.0)), "log", 100.0, 3.0, 6.0)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("TIMESFM_GATE ")]
    assert len(lines) == 1 and '"symbol":"GOODCO"' in lines[0] and '"would_reject":true' in lines[0]
