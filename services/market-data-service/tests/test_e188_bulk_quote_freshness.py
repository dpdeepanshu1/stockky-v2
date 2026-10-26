"""
E188: /quotes/bulk rows built from a live-feed tick or from AngelOne REST.

Three defects in `_get_quotes_bulk_core`:
  1. A live-feed hit (a tick up to 15 s old) was stamped fetched_at = the time of the READ, so a 14 s old price was
     reported as 0 s old and then cached for another 15 s with that stamp. The tick's own time (`ts`) is used now.
  2. Rows from the live feeds and from AngelOne REST carried no previous_close / day_change_pct (the single /quote path
     always did), so callers reading them from bulk saw "no previous close". Filled when the source has it, never invented.
  3. The AngelOne REST stage had no overall deadline: each 50-token chunk may wait up to 20 s for a rate token, so one
     call could hold a worker thread for ~40 s after its 12 s caller had given up. QUOTE_BULK_ANGELONE_DEADLINE_S bounds it.

Run from services/market-data-service:
    python3 -m pytest tests/test_e188_bulk_quote_freshness.py -v
"""
from __future__ import annotations
import asyncio
import os
import sys
import time
import types
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _k in ("BULK_CACHE_MAX_AGE_SEC", "QUOTE_BULK_LIVE_TICK_TS", "QUOTE_BULK_ANGELONE_DEADLINE_S"):
    os.environ.pop(_k, None)

import pytest

import main


def _age(row) -> float:
    return main._quote_row_age_s(row)


@pytest.fixture()
def harness(monkeypatch):
    """Cache is a dict; both live feeds answer from `live` / `ylive`; AngelOne REST is a stub session; yfinance is
    'saturated' so the core returns whatever it has without a download."""
    cache: dict = {}
    monkeypatch.setattr(main, "_cache_get", lambda k: cache.get(k))
    monkeypatch.setattr(main, "_cache_set", lambda k, v, ttl=None: cache.__setitem__(k, v))
    monkeypatch.setattr(main, "normalize_symbol", lambda s: f"{str(s).upper()}.NS")
    monkeypatch.setattr(main, "is_known_delisted", lambda s: False)
    monkeypatch.setattr(main, "_quote_market_closed", lambda: False)
    monkeypatch.setattr(main, "_quote_preopen", lambda: False)
    monkeypatch.setattr(main, "_dhan_quote_position", lambda: "off")
    monkeypatch.setattr(main, "_angelone_quote_cooling", lambda: False)
    monkeypatch.setattr(main._rl, "would_block", lambda *a, **k: True)

    state = types.SimpleNamespace(live={}, ylive={}, ao_rows=[], ao_calls=0, ao_delay=0.0, ao_tokens=[])

    live = types.ModuleType("angelone_ws_feed")
    live.get_live_quotes_bulk = lambda syms, max_age_sec=15.0: {s: state.live[s] for s in syms if s in state.live}
    monkeypatch.setitem(sys.modules, "angelone_ws_feed", live)
    ylive = types.ModuleType("yahoo_ws_feed")
    ylive.get_live_quotes_bulk = lambda syms, max_age_sec=15.0: {s: state.ylive[s] for s in syms if s in state.ylive}
    monkeypatch.setitem(sys.modules, "yahoo_ws_feed", ylive)

    class _Session:
        def is_configured(self):
            return True

        async def get_quotes_batch(self, exchange, tokens, lane=None):
            state.ao_calls += 1
            state.ao_tokens.append(list(tokens))
            if state.ao_delay:
                await asyncio.sleep(state.ao_delay)
            return [r for r in state.ao_rows if str(r.get("symbolToken")) in {str(t) for t in tokens}]

    aoc = types.ModuleType("angelone_client")
    aoc.get_session = lambda: _Session()
    monkeypatch.setitem(sys.modules, "angelone_client", aoc)
    aosm = types.ModuleType("angelone_scrip_master")
    aosm.get_token = lambda sym: f"T{str(sym).upper()}"
    monkeypatch.setitem(sys.modules, "angelone_scrip_master", aosm)
    return cache, state


def _bulk(symbols):
    stale: dict = {}
    out = main._get_quotes_bulk_core(main.BulkQuoteRequest(symbols=list(symbols)), stale)
    return out, stale


def _tick(sym, price, age_s, **extra):
    t = {"symbol": sym, "price": price, "high": price + 1, "low": price - 1, "volume": 1000,
         "source": "angelone_ws", "ts": time.time() - age_s}
    t.update(extra)
    return t


class TestLiveTickFetchedAt:
    def test_uses_the_tick_time(self):
        got = main._live_tick_fetched_at({"ts": time.time() - 12.0})
        assert 11.0 <= _age({"fetched_at": got}) <= 14.0

    def test_missing_or_unusable_ts_falls_back_to_now(self):
        for bad in ({}, {"ts": None}, {"ts": "x"}, {"ts": -5}, {"ts": 0}, {"ts": float("nan")},
                    {"ts": float("inf")}, None, "not a dict"):
            got = main._live_tick_fetched_at(bad)
            assert _age({"fetched_at": got}) < 2.0, bad

    def test_a_future_ts_is_clamped_to_now(self):
        got = main._live_tick_fetched_at({"ts": time.time() + 600})
        assert _age({"fetched_at": got}) < 2.0
        assert datetime.fromisoformat(got) <= datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=1)

    def test_switch_off_restores_read_time_stamping(self, monkeypatch):
        monkeypatch.setenv("QUOTE_BULK_LIVE_TICK_TS", "0")
        got = main._live_tick_fetched_at({"ts": time.time() - 12.0})
        assert _age({"fetched_at": got}) < 2.0
        monkeypatch.setenv("QUOTE_BULK_LIVE_TICK_TS", "")
        got = main._live_tick_fetched_at({"ts": time.time() - 12.0})
        assert _age({"fetched_at": got}) >= 11.0


class TestPrevCloseFields:
    def test_computes_the_change_from_price(self):
        assert main._prev_close_fields(95, 100) == {"previous_close": 95.0, "day_change_pct": 5.26}
        assert main._prev_close_fields(100, 90) == {"previous_close": 100.0, "day_change_pct": -10.0}

    def test_a_given_change_wins(self):
        assert main._prev_close_fields(95, 100, 4.0) == {"previous_close": 95.0, "day_change_pct": 4.0}

    def test_never_invented(self):
        for bad in (None, 0, -3, "abc", "", float("nan"), float("inf")):
            assert main._prev_close_fields(bad, 100) == {}, bad

    def test_no_price_gives_the_previous_close_only(self):
        assert main._prev_close_fields(95, None) == {"previous_close": 95.0}
        assert main._prev_close_fields(95, 0) == {"previous_close": 95.0}

    def test_live_tick_sources(self):
        # Yahoo's feed carries previous_close / day_change_pct
        assert main._live_prev_close_fields(
            {"source": "yahoo_ws", "previous_close": 50, "day_change_pct": 2.0}, 51) == {"previous_close": 50.0, "day_change_pct": 2.0}
        # AngelOne's `close` is the previous session's close
        assert main._live_prev_close_fields({"source": "angelone_ws", "close": 95}, 100) == {
            "previous_close": 95.0, "day_change_pct": 5.26}
        # an explicit previous_close beats `close`
        assert main._live_prev_close_fields({"source": "angelone_ws", "previous_close": 90, "close": 95}, 99) == {
            "previous_close": 90.0, "day_change_pct": 10.0}
        # an unknown source's `close` is not assumed to be the previous close
        assert main._live_prev_close_fields({"source": "mystery", "close": 95}, 100) == {}
        assert main._live_prev_close_fields(None, 100) == {}


class TestLiveFeedRowsInBulk:
    def test_row_carries_the_tick_age_and_previous_close(self, harness):
        cache, st = harness
        st.live["AAA"] = _tick("AAA", 100.0, 10.0, close=95.0)
        out, _ = _bulk(["AAA"])
        row = out["quotes"][0]
        assert row["source"] == "angelone_ws"
        assert 9.0 <= _age(row) <= 13.0          # the tick's age, not 0
        assert row["previous_close"] == 95.0
        assert row["day_change_pct"] == 5.26
        assert 9.0 <= row["age_s"] <= 13.0       # group289's derived field follows the stamp

    def test_a_tick_is_not_served_as_fresh_after_its_real_age_passes_the_limit(self, harness, monkeypatch):
        cache, st = harness
        st.live["AAA"] = _tick("AAA", 100.0, 14.0)
        first, _ = _bulk(["AAA"])
        assert first["quotes"] and _age(first["quotes"][0]) >= 13.0
        # the feed has nothing newer; a 5 s limit now makes the cached 14 s old row a refresh candidate, not a hit
        monkeypatch.setenv("BULK_CACHE_MAX_AGE_SEC", "5")
        st.live.clear()
        second, stale = _bulk(["AAA"])
        assert second["quotes"] == []
        assert "AAA" in stale                    # kept only as the last-resort stale row

    def test_switch_off_keeps_the_old_read_time_stamp(self, harness, monkeypatch):
        cache, st = harness
        monkeypatch.setenv("QUOTE_BULK_LIVE_TICK_TS", "0")
        st.live["AAA"] = _tick("AAA", 100.0, 14.0)
        out, _ = _bulk(["AAA"])
        assert _age(out["quotes"][0]) < 2.0

    def test_a_tick_without_previous_close_adds_no_fields(self, harness):
        cache, st = harness
        st.live["AAA"] = _tick("AAA", 100.0, 1.0)         # no `close`
        out, _ = _bulk(["AAA"])
        row = out["quotes"][0]
        assert row["previous_close"] is None and row["day_change_pct"] is None


class TestAngelOneRestRows:
    def test_previous_close_and_change_are_filled(self, harness):
        cache, st = harness
        st.ao_rows = [{"symbolToken": "TBBB", "ltp": 100.0, "close": 95.0, "high": 101, "low": 94, "tradeVolume": 500}]
        out, _ = _bulk(["BBB"])
        row = out["quotes"][0]
        assert row["source"] == "angelone_rest"
        assert row["price"] == 100.0
        assert row["previous_close"] == 95.0 and row["day_change_pct"] == 5.26

    def test_a_row_without_a_close_stays_empty_not_zero(self, harness):
        cache, st = harness
        st.ao_rows = [{"symbolToken": "TBBB", "ltp": 100.0}]
        out, _ = _bulk(["BBB"])
        row = out["quotes"][0]
        assert row["previous_close"] is None and row["day_change_pct"] is None

    def test_zero_ltp_is_not_served_as_a_live_price(self, harness, monkeypatch):
        # E218: this used to assert the close fallback (a previous close published as a fresh live price). Now such a row is left
        # unpriced by this stage; QUOTE_BULK_ANGELONE_CLOSE_FALLBACK=1 brings the old behaviour back (next test).
        monkeypatch.delenv("QUOTE_BULK_ANGELONE_CLOSE_FALLBACK", raising=False)
        cache, st = harness
        st.ao_rows = [{"symbolToken": "TBBB", "ltp": 0, "close": 95.0}]
        out, _ = _bulk(["BBB"])
        assert out["quotes"] == []

    def test_zero_ltp_close_fallback_switch_restores_old_row(self, harness, monkeypatch):
        monkeypatch.setenv("QUOTE_BULK_ANGELONE_CLOSE_FALLBACK", "1")
        cache, st = harness
        st.ao_rows = [{"symbolToken": "TBBB", "ltp": 0, "close": 95.0}]
        out, _ = _bulk(["BBB"])
        row = out["quotes"][0]
        assert row["price"] == 95.0 and row["previous_close"] == 95.0 and row["day_change_pct"] == 0.0


class TestAngelOneStageDeadline:
    def test_parsing(self, monkeypatch):
        monkeypatch.delenv("QUOTE_BULK_ANGELONE_DEADLINE_S", raising=False)
        assert main._bulk_angelone_deadline_s() == 10.0
        monkeypatch.setenv("QUOTE_BULK_ANGELONE_DEADLINE_S", "4.5")
        assert main._bulk_angelone_deadline_s() == 4.5
        for off in ("0", "-2"):
            monkeypatch.setenv("QUOTE_BULK_ANGELONE_DEADLINE_S", off)
            assert main._bulk_angelone_deadline_s() == 0.0
        for bad in ("abc", " ", "", "nan"):
            monkeypatch.setenv("QUOTE_BULK_ANGELONE_DEADLINE_S", bad)
            assert main._bulk_angelone_deadline_s() == 10.0

    def test_a_slow_first_chunk_stops_the_stage(self, harness, monkeypatch):
        cache, st = harness
        st.ao_delay = 5.0                         # every chunk would wait 5 s for a rate token
        monkeypatch.setenv("QUOTE_BULK_ANGELONE_DEADLINE_S", "0.3")
        syms = [f"S{i}" for i in range(60)]       # two 50-token chunks
        t0 = time.monotonic()
        out, _ = _bulk(syms)
        took = time.monotonic() - t0
        assert took < 2.5, took
        assert st.ao_calls == 1                   # the second chunk was never asked
        assert out["quotes"] == []                # nothing priced; the rest of the pipeline answered (yfinance saturated)

    def test_a_fast_stage_is_unchanged(self, harness, monkeypatch):
        cache, st = harness
        monkeypatch.setenv("QUOTE_BULK_ANGELONE_DEADLINE_S", "5")
        syms = [f"S{i}" for i in range(60)]
        st.ao_rows = [{"symbolToken": f"T{s}", "ltp": 10.0 + i, "close": 9.0} for i, s in enumerate(syms)]
        out, _ = _bulk(syms)
        assert st.ao_calls == 2
        assert len(out["quotes"]) == 60

    def test_deadline_off_waits_for_every_chunk(self, harness, monkeypatch):
        cache, st = harness
        st.ao_delay = 0.05
        monkeypatch.setenv("QUOTE_BULK_ANGELONE_DEADLINE_S", "0")
        syms = [f"S{i}" for i in range(60)]
        _bulk(syms)
        assert st.ao_calls == 2
