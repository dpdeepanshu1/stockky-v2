"""tests/test_group331_multipass_global_cues.py - multi-pass pre-open snapshot, global cues for the carry decision, /preopen/status (group 331)."""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from entry_engine import carry_context, event_risk, global_cues, preopen, vix_gate
from tz_utils import IST


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    for n in ("PREOPEN_SNAPSHOT_ENABLED", "GLOBAL_CUES_ENABLED", "CARRY_CONTEXT_GATE_ENABLED"):
        monkeypatch.setattr(config, n, True)
    monkeypatch.setattr(config, "PREOPEN_SNAPSHOT_PASSES_IST", "", raising=False)
    for m in (preopen, global_cues, carry_context, vix_gate, event_risk):
        m.reset()
    yield
    for m in (preopen, global_cues, carry_context, vix_gate, event_risk):
        m.reset()


def at(h, m):
    return datetime(2026, 10, 12, h, m, tzinfo=IST)


class _Tk:
    def __init__(self, price, prev, base, age_s=10):
        self.price, self.prev_close, self.age_known = price, prev, True
        self.as_of = base - timedelta(seconds=age_s)


class TestMultiPass:
    def test_default_pass_times(self):
        assert preopen._pass_times() == ["09:08", "09:11", "09:14"]

    def test_override_and_blank_safe(self, monkeypatch):
        monkeypatch.setattr(config, "PREOPEN_SNAPSHOT_PASSES_IST", " 09:09 , 09:13 ")
        assert preopen._pass_times() == ["09:09", "09:13"]
        monkeypatch.setattr(config, "PREOPEN_SNAPSHOT_PASSES_IST", " , ")
        assert preopen._pass_times() == ["09:08", "09:11", "09:14"] or preopen._pass_times() == ["09:08"]

    def test_due_progresses_pass_by_pass(self):
        assert preopen.due(at(9, 7)) is False
        assert preopen.due(at(9, 8)) is True
        preopen.record({"A": _Tk(106, 100, at(9, 8))}, now=at(9, 8))
        assert preopen.due(at(9, 9)) is False            # first pass done, next one not yet reached
        assert preopen.due(at(9, 11)) is True            # second pass is due
        preopen.record({"B": _Tk(97, 100, at(9, 11))}, now=at(9, 11))
        assert preopen.due(at(9, 12)) is False
        assert preopen.due(at(9, 14)) is True
        preopen.record({}, now=at(9, 14))
        assert preopen.due(at(9, 14)) is False
        assert preopen.due(at(9, 16)) is False           # never after the bell

    def test_late_start_catches_up_in_one_pass(self):
        assert preopen.due(at(9, 12)) is True            # restarted at 09:12: two passes are overdue, one run covers them
        preopen.record({"A": _Tk(106, 100, at(9, 12))}, now=at(9, 12))
        assert preopen.status()["passes_done"] == ["09:08", "09:11"]
        assert preopen.due(at(9, 13)) is False
        assert preopen.due(at(9, 14)) is True

    def test_later_pass_adds_and_overrides_but_never_drops(self):
        preopen.record({"A": _Tk(106.5, 100, at(9, 8)), "B": _Tk(104, 100, at(9, 8))}, now=at(9, 8))
        preopen.record({"B": _Tk(96, 100, at(9, 11)), "C": _Tk(107, 100, at(9, 11))}, now=at(9, 11))
        gaps = preopen.status()["gaps"]
        assert set(gaps) == {"A", "B", "C"}
        assert round(gaps["B"], 6) == -4.0               # the later reading replaced the earlier one
        assert preopen.gap_reason("A", at(9, 30)).startswith("PREOPEN_GAP_UP")
        assert preopen.gap_reason("B", at(9, 30)).startswith("PREOPEN_GAP_DOWN")

    def test_new_day_starts_clean(self):
        preopen.record({"A": _Tk(106, 100, at(9, 8))}, now=at(9, 8))
        preopen._snap["coverage"].append({"queued": 1}); preopen._snap["warm_done"] = True
        nxt = datetime(2026, 10, 13, 9, 8, tzinfo=IST)
        assert preopen.due(nxt) is True
        preopen.record({"Z": _Tk(103, 100, nxt)}, now=nxt)
        st = preopen.status()
        assert set(st["gaps"]) == {"Z"} and st["passes_done"] == ["09:08"] and st["coverage"] == []

    def test_run_warms_once_and_records_coverage(self, monkeypatch):
        import market_feed.feed as feed
        calls = []

        async def fake_quotes(syms, **kw):
            calls.append(list(syms))
            return {s: _Tk(106.5, 100, datetime.now(timezone.utc)) for s in syms}
        monkeypatch.setattr(feed, "get_quotes", fake_quotes)
        warmed = []
        monkeypatch.setattr(preopen, "_warm_blocking", lambda syms: warmed.append(list(syms)) or len(syms))
        st1 = asyncio.run(preopen.run(["A", "B"]))
        assert warmed == [["A", "B"]] and st1["warmed"] == 2
        st2 = asyncio.run(preopen.run(["A", "B", "C"]))
        assert warmed == [["A", "B"]], "caches are warmed on the first pass only"
        assert st2["warmed"] == 2 and len(st2["coverage"]) == 2
        assert st2["coverage"][1]["queued"] == 3 and st2["coverage"][1]["priced"] == 3
        assert len(calls) == 2

    def test_low_coverage_is_logged_loudly(self, monkeypatch, caplog):
        import market_feed.feed as feed

        async def nothing(syms, **kw):
            return {}
        monkeypatch.setattr(feed, "get_quotes", nothing)
        monkeypatch.setattr(preopen, "_warm_blocking", lambda syms: 0)
        with caplog.at_level("WARNING", logger="real-trade-preopen"):
            asyncio.run(preopen.run(["A", "B", "C", "D", "E"]))
        assert any("LOW COVERAGE" in r.message for r in caplog.records)

    def test_failed_run_does_not_retry_the_same_pass_every_tick(self, monkeypatch):
        import market_feed.feed as feed

        async def boom(syms, **kw):
            raise RuntimeError("feed down")
        monkeypatch.setattr(feed, "get_quotes", boom)
        monkeypatch.setattr(preopen, "_reached", lambda now=None: ["09:08"])
        st = asyncio.run(preopen.run(["A"]))
        assert st["passes_done"] == ["09:08"] and st["symbols_with_gap"] == 0


class TestGlobalCuesParse:
    def test_parse_row(self):
        assert global_cues.parse_row({"day_change_pct": -1.1}) == -1.1
        assert round(global_cues.parse_row({"price": 99.0, "previous_close": 100.0}), 6) == -1.0
        for bad in (None, {}, "x", {"day_change_pct": 40}, {"day_change_pct": float("nan")}, {"day_change_pct": -1, "age_s": 99999},
                    {"price": 0, "previous_close": 100}):
            assert global_cues.parse_row(bad) is None
        assert global_cues.parse_row({"day_change_pct": -1.0, "age_s": None}) == -1.0

    def test_parse_body(self):
        body = {"rows": {"^gdaxi": {"day_change_pct": -1.0}, "ES=F": {"day_change_pct": 30}, "NQ=F": {"price": 99, "previous_close": 100}},
                "missing": ["^FTSE"]}
        out = global_cues.parse_body(body)
        assert set(out) == {"^GDAXI", "NQ=F"} and round(out["NQ=F"], 6) == -1.0
        for bad in (None, {}, {"rows": []}, "x", {"rows": {"A": 5}}):
            assert global_cues.parse_body(bad) == {}


class TestGlobalCuesReason:
    def test_average_weak_blocks(self):
        r = global_cues.reason({"^GDAXI": -1.0, "^FTSE": -0.9, "^N225": -1.2})
        assert r.startswith("CARRY_GLOBAL_WEAK") and "average" in r

    def test_mixed_cues_do_not_block(self):
        assert global_cues.reason({"^GDAXI": -1.0, "^N225": 0.6, "^HSI": 0.2}) is None

    def test_us_future_weak_blocks_even_when_average_is_fine(self):
        r = global_cues.reason({"^GDAXI": 1.0, "^N225": 1.0, "ES=F": -1.3})
        assert r.startswith("CARRY_GLOBAL_WEAK") and "ES=F" in r

    def test_needs_minimum_readings(self):
        assert global_cues.reason({"ES=F": -3.0}) is None            # one reading is no opinion (min 2)
        assert global_cues.reason(None) is None and global_cues.reason({}) is None

    def test_zero_disables_each_test(self, monkeypatch):
        monkeypatch.setattr(config, "CARRY_GLOBAL_US_FUT_MIN_PCT", 0.0)
        monkeypatch.setattr(config, "CARRY_GLOBAL_MIN_AVG_PCT", 0.0)
        assert global_cues.reason({"^GDAXI": -3.0, "ES=F": -3.0}) is None

    def test_switch_off(self, monkeypatch):
        monkeypatch.setattr(config, "GLOBAL_CUES_ENABLED", False)
        assert global_cues.reason({"^GDAXI": -3.0, "^N225": -3.0}) is None
        assert global_cues.snapshot() is None


class TestGlobalCuesSnapshot:
    def test_cache_and_failure_retry(self, monkeypatch):
        n = {"c": 0}

        def ok():
            n["c"] += 1
            return {"^GDAXI": -1.0, "^N225": -1.0}
        monkeypatch.setattr(global_cues, "_fetch", ok)
        assert global_cues.snapshot() == {"^GDAXI": -1.0, "^N225": -1.0}
        assert global_cues.snapshot() is not None and n["c"] == 1

    def test_failure_is_cached_briefly_and_fails_open(self, monkeypatch):
        n = {"c": 0}

        def down():
            n["c"] += 1
            raise TimeoutError("x")
        monkeypatch.setattr(global_cues, "_fetch", down)
        assert global_cues.snapshot() is None and global_cues.snapshot() is None
        assert n["c"] == 1
        global_cues._state["ts"] -= global_cues.FAILURE_RETRY_S + 1
        assert global_cues.snapshot() is None and n["c"] == 2

    def test_empty_result_is_none(self, monkeypatch):
        monkeypatch.setattr(global_cues, "_fetch", lambda: {})
        assert global_cues.snapshot() is None

    def test_fetch_calls_the_global_cues_route_once(self, monkeypatch):
        import httpx
        seen = {}

        class Resp:
            status_code = 200

            def json(self):
                return {"rows": {"^GDAXI": {"day_change_pct": -1.4}, "ES=F": {"day_change_pct": -0.2}}}

        class Client:
            def __init__(self, **kw):
                seen["timeout"] = kw.get("timeout")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, params=None):
                seen.update(url=url, params=params)
                return Resp()
        monkeypatch.setattr(httpx, "Client", Client)
        out = global_cues._fetch()
        assert out == {"^GDAXI": -1.4, "ES=F": -0.2}
        assert seen["url"].endswith("/global-cues") and "ES=F" in seen["params"]["symbols"]
        assert seen["timeout"] == config.GLOBAL_CUE_TIMEOUT_S

    def test_fetch_non_200_raises_and_snapshot_swallows(self, monkeypatch):
        import httpx

        class Resp:
            status_code = 503

        class Client:
            def __init__(self, **kw): ...
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get(self, url, params=None): return Resp()
        monkeypatch.setattr(httpx, "Client", Client)
        with pytest.raises(RuntimeError):
            global_cues._fetch()
        assert global_cues.snapshot() is None

    def test_no_symbols_configured(self, monkeypatch):
        monkeypatch.setattr(config, "GLOBAL_CUE_SYMBOLS", " , ")
        assert global_cues._fetch() == {}


class TestCarryContextUsesGlobal:
    def test_global_snap_blocks_after_market_and_vix_pass(self, monkeypatch):
        monkeypatch.setattr(config, "CARRY_BLOCK_NEGATIVE_NEWS", False)
        r = carry_context.reason("ABC", 0.2, {"level": 13.0, "change_pct": 0.0}, global_snap={"^GDAXI": -1.5, "^N225": -1.5})
        assert r.startswith("CARRY_GLOBAL_WEAK")

    def test_no_global_snap_means_no_global_opinion(self, monkeypatch):
        monkeypatch.setattr(config, "CARRY_BLOCK_NEGATIVE_NEWS", False)
        assert carry_context.reason("ABC", 0.2, {"level": 13.0, "change_pct": 0.0}) is None

    def test_market_reason_still_comes_first(self, monkeypatch):
        r = carry_context.reason("ABC", -1.5, None, global_snap={"^GDAXI": -1.5, "^N225": -1.5})
        assert r.startswith("CARRY_MARKET_WEAK")


import models                                                                  # noqa: E402
import market_feed.feed as feed_module                                         # noqa: E402
from market_feed.feed import Tick                                              # noqa: E402
from execution.auto_pilot import _select_overnight_holds                       # noqa: E402
from sqlalchemy import create_engine                                           # noqa: E402
from sqlalchemy.orm import sessionmaker                                        # noqa: E402

_eng = create_engine("sqlite:///:memory:")
_Sess = sessionmaker(bind=_eng)


def _carry(monkeypatch, symbol="GLOBCO"):
    monkeypatch.setattr(config, "OVERNIGHT_HOLD_MIN_GAIN_PCT", 0.0, raising=False)
    monkeypatch.setattr(config, "OVERNIGHT_HOLD_PROFITABLE_NET_OF_COSTS", False, raising=False)
    models.Base.metadata.drop_all(_eng)
    models.Base.metadata.create_all(_eng)
    d = _Sess()
    d.add(models.TradeAccount(mode="REAL", starting_capital=100_000.0, current_equity=100_000.0, cash_available=100_000.0))
    d.flush()
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


class TestAutoPilotCarryWiring:
    @pytest.fixture(autouse=True)
    def _ctx(self, monkeypatch):
        monkeypatch.setattr(config, "NEWS_NEGATIVE_BLOCK_ENABLED", True)
        monkeypatch.setattr(config, "VIX_GATE_ENABLED", True)
        monkeypatch.setattr(carry_context, "_fetch_nifty", lambda: 0.3)
        monkeypatch.setattr(vix_gate, "_fetch", lambda: {"level": 13.0, "change_pct": 0.0})
        monkeypatch.setattr(event_risk, "_fetch", lambda s: {"event_score": 55})

    def test_calm_world_still_carries(self, monkeypatch):
        monkeypatch.setattr(global_cues, "_fetch", lambda: {"^GDAXI": 0.3, "^N225": 0.1, "ES=F": 0.2})
        p, keep = _carry(monkeypatch)
        assert p.id in keep

    def test_weak_overseas_squares_off(self, monkeypatch):
        monkeypatch.setattr(global_cues, "_fetch", lambda: {"^GDAXI": -1.4, "^N225": -1.2, "^HSI": -1.0})
        p, keep = _carry(monkeypatch)
        assert keep == set()

    def test_us_futures_drop_squares_off(self, monkeypatch):
        monkeypatch.setattr(global_cues, "_fetch", lambda: {"^GDAXI": 0.5, "^N225": 0.5, "NQ=F": -1.6})
        p, keep = _carry(monkeypatch)
        assert keep == set()

    def test_global_source_down_still_carries(self, monkeypatch):
        def down():
            raise TimeoutError("x")
        monkeypatch.setattr(global_cues, "_fetch", down)
        p, keep = _carry(monkeypatch)
        assert p.id in keep

    def test_global_switch_off_ignores_it(self, monkeypatch):
        monkeypatch.setattr(config, "GLOBAL_CUES_ENABLED", False)
        monkeypatch.setattr(global_cues, "_fetch", lambda: {"^GDAXI": -9.0, "^N225": -9.0})
        p, keep = _carry(monkeypatch)
        assert p.id in keep


class TestPreopenStatusRoute:
    def test_route_returns_status(self):
        import main
        preopen.record({"A": _Tk(106, 100, at(9, 8))}, now=at(9, 8))
        body = main.preopen_status()
        assert body["symbols_with_gap"] == 1 and body["passes_done"] == ["09:08"] and "coverage" in body
