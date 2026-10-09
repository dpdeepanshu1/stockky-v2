"""group275: follow-ups found while checking the Dhan data path against a live status output.

1. /history period=1d means the latest trading session (it used to fall to the 180-day default, then Dhan's 60-day cap).
2. dhan_data/live_store writes `live_quotes` in small chunks, keeps going after a failed chunk, and coalesces a backlog.
3. dhan_data/quotes status says WHICH symbols Dhan did not price.
4. /angelone/movers asks Dhan first (3 calls) and only sends the unpriced remainder to AngelOne.
5. ANGELONE_WS_FEED_ENABLED / YAHOO_WS_FEED_ENABLED switch the redundant feeds off (default on).

Run from services/market-data-service:   python3 -m pytest tests/test_group275_dhan_followups.py -v
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import main as m
from dhan_data import client, config, history, live_store, quotes, scrip_master

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
IST = ZoneInfo("Asia/Kolkata")


# ── 1. period=1d ──────────────────────────────────────────────────────────────────────────────────────────────────
def _hourly(sessions: int):
    """`sessions` trading days of 7 hourly bars each, newest last, dated like the Dhan parser ('YYYY-MM-DD HH:MM')."""
    today = datetime.now(IST).date()
    out = []
    for b in range(sessions - 1, -1, -1):
        d = (today - timedelta(days=b)).isoformat()
        for h in (9, 10, 11, 12, 13, 14, 15):
            out.append({"date": f"{d} {h:02d}:15", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10})
    return out


class TestTrimLatestSession:
    def test_keeps_only_the_last_session_for_hourly_bars(self):
        c = _hourly(30)
        out = m._history_trim_latest_session(c, "1d", "60m")
        assert len(out) == 7 and {x["date"][:10] for x in out} == {c[-1]["date"][:10]}

    def test_daily_interval_keeps_the_last_bar(self):
        c = _hourly(5)
        assert m._history_trim_latest_session(c, "1d", "1d") == [c[-1]]

    @pytest.mark.parametrize("period", ["5d", "1mo", "1y", "", None])
    def test_other_periods_untouched(self, period):
        c = _hourly(10)
        assert m._history_trim_latest_session(c, period, "60m") is c

    def test_empty_and_bad_input_pass_through(self):
        assert m._history_trim_latest_session([], "1d", "60m") == []
        assert m._history_trim_latest_session(None, "1d", "60m") is None
        bad = [{"nodate": 1}]
        assert m._history_trim_latest_session(bad, "1d", "60m") == bad

    def test_period_table_for_other_logic_is_unchanged(self):
        assert "1d" not in m._HISTORY_PERIOD_DAYS      # it also drives slicing from longer cached series


@pytest.fixture()
def hist_env(monkeypatch):
    monkeypatch.delenv("HISTORY_PROVIDER_ORDER", raising=False)
    monkeypatch.setenv("DHAN_DATA_ENABLED", "1")
    monkeypatch.setattr(client, "_breaker", lambda: None)
    client._reset_for_tests()
    scrip_master._set_for_tests({"RAYMOND": 123, "TCS": 11536}, {})
    m._mem._d.clear()
    m._history_flights.clear()
    monkeypatch.setattr(m, "cache", None)
    monkeypatch.setattr(m, "_neg_blocked", lambda s: False)
    monkeypatch.setattr(m, "_HISTORY_FORCE_REUSE_S", 60.0)
    monkeypatch.setattr(m, "_history_candle_cooling", lambda: False)
    monkeypatch.setattr(m, "_history_last_good_get", lambda k: None)
    monkeypatch.setattr(m, "_in_cooldown", lambda name="yfinance": False)
    yield
    m._mem._d.clear()
    m._history_flights.clear()
    client._reset_for_tests()


class TestDhanHistoryOneDay:
    def test_hourly_1d_is_one_session_and_a_short_window(self, monkeypatch, hist_env):
        calls = []

        def fake(symbol, interval, frm, to):
            calls.append((symbol, interval, frm, to))
            return _hourly(40)           # what the old 60-day window returned: ~7 weeks
        monkeypatch.setattr(history, "fetch_candles", fake)
        r = m._get_history_impl("RAYMOND", "1d", "60m", False, None)
        assert r["source"] == "dhan" and r["period"] == "1d" and r["interval"] == "60m"
        assert len(r["candles"]) == 7
        assert len({c["date"][:10] for c in r["candles"]}) == 1
        assert len(calls) == 1
        _, iv, frm, to = calls[0]
        assert iv == "60m" and (to - frm).days == m._HISTORY_LATEST_SESSION_LOOKBACK_DAYS + 1    # `to` is tomorrow

    def test_5d_window_is_not_affected(self, monkeypatch, hist_env):
        calls = []

        def fake(symbol, interval, frm, to):
            calls.append((interval, frm, to))
            return _hourly(10)
        monkeypatch.setattr(history, "fetch_candles", fake)
        r = m._get_history_impl("RAYMOND", "5d", "60m", False, None)
        assert len(r["candles"]) == 70            # all ten sessions: no trimming for 5d
        assert (calls[0][2] - calls[0][1]).days == m._HISTORY_PERIOD_DAYS["5d"] + 1

    def test_an_explicit_days_window_is_never_trimmed(self, monkeypatch, hist_env):
        monkeypatch.setattr(history, "fetch_candles", lambda *a, **k: _hourly(5))
        r = m._get_history_impl("RAYMOND", "1d", "60m", False, 10)
        assert len(r["candles"]) == 35


# ── 2. live_store ─────────────────────────────────────────────────────────────────────────────────────────────────
class _Conn:
    def __init__(self, eng):
        self.eng = eng

    def execute(self, stmt, params):
        self.eng.executed.append(len(params))
        if self.eng.fail_on and len(self.eng.executed) in self.eng.fail_on:
            raise RuntimeError("DPY-4024: call timeout of 8000 ms exceeded")


class _Begin:
    def __init__(self, eng):
        self.eng = eng

    def __enter__(self):
        return _Conn(self.eng)

    def __exit__(self, *a):
        return False


class _Engine:
    def __init__(self, fail_on=()):
        self.executed = []
        self.fail_on = set(fail_on)

    def begin(self):
        return _Begin(self)


def _rows(n, price=100.0):
    return [{"symbol": f"S{i}", "price": price + i, "previous_close": price, "open": price, "day_high": price + 5,
             "day_low": price - 5, "volume": 10} for i in range(n)]


@pytest.fixture()
def store(monkeypatch):
    import angelone_ws_feed
    eng = _Engine()
    monkeypatch.setattr(angelone_ws_feed, "_get_live_quotes_engine", lambda: (eng, "oracle"))
    monkeypatch.setattr(angelone_ws_feed, "_ensure_schema", lambda e, d: None)
    monkeypatch.setenv("DHAN_LIVE_WRITE_CHUNK", "100")
    for k in list(live_store._stats):
        live_store._stats[k] = None if k == "last_error" else 0
    live_store._last_warn = 0.0
    return eng


class TestLiveStoreChunks:
    def test_rows_are_written_in_chunks(self, store):
        assert live_store._write_sync(_rows(250)) == 250
        assert store.executed == [100, 100, 50]

    def test_a_failed_chunk_does_not_lose_the_rest(self, store):
        store.fail_on = {2}
        assert live_store._write_sync(_rows(250)) == 150
        assert store.executed == [100, 100, 50]
        st = live_store.status()
        assert st["failed_chunks"] == 1 and st["timeouts"] == 1 and "DPY-4024" in st["last_error"]

    def test_chunk_size_is_blank_safe(self, monkeypatch):
        for raw, want in (("", 150), ("abc", 150), ("0", 10), ("40", 40)):
            monkeypatch.setenv("DHAN_LIVE_WRITE_CHUNK", raw)
            assert config.live_write_chunk() == want

    def test_no_engine_writes_nothing(self, monkeypatch):
        import angelone_ws_feed
        monkeypatch.setattr(angelone_ws_feed, "_get_live_quotes_engine", lambda: (None, None))
        assert live_store._write_sync(_rows(5)) == 0

    def test_index_rows_are_skipped(self, store):
        rows = _rows(3) + [{"symbol": "^NSEI", "price": 24000.0}]
        assert live_store._write_sync(rows) == 3


class TestCoalesce:
    def test_newest_row_per_symbol_wins(self):
        a = [{"symbol": "AAA", "price": 1.0}, {"symbol": "BBB", "price": 2.0}]
        b = [{"symbol": "AAA", "price": 1.5}]
        out = {r["symbol"]: r["price"] for r in live_store.coalesce([a, b])}
        assert out == {"AAA": 1.5, "BBB": 2.0}

    def test_empty_and_symbolless_rows_are_ignored(self):
        assert live_store.coalesce([[], [{"price": 1.0}], None]) == []

    def test_loop_merges_the_backlog_into_one_write(self, store, monkeypatch):
        written = []

        class _Stop(BaseException):          # BaseException so the loop's `except Exception` cannot swallow it
            pass

        def fake_write(rows):
            written.append(sorted(r["symbol"] for r in rows))
            raise _Stop()
        monkeypatch.setattr(live_store, "_write_sync", fake_write)
        while not live_store._q.empty():
            live_store._q.get_nowait()
        live_store._q.put_nowait([{"symbol": "AAA", "price": 1.0}])
        live_store._q.put_nowait([{"symbol": "BBB", "price": 2.0}])
        live_store._q.put_nowait([{"symbol": "AAA", "price": 3.0}])
        with pytest.raises(_Stop):
            live_store._loop()
        assert written == [["AAA", "BBB"]]
        assert live_store.status()["coalesced_batches"] == 2


# ── 3. which symbols Dhan did not price ───────────────────────────────────────────────────────────────────────────
class TestEmptySample:
    @pytest.fixture(autouse=True)
    def fresh(self, monkeypatch):
        monkeypatch.setattr(client, "_breaker", lambda: None)
        client._reset_for_tests()
        quotes._reset_for_tests()
        scrip_master._set_for_tests({"TCS": 11536, "INFY": 1594, "NODATA": 777}, {})
        monkeypatch.setattr(client, "post", lambda *a, **k: {"data": {"NSE_EQ": {
            "11536": {"last_price": 3500.0, "ohlc": {"close": 3490.0}}}}})
        yield
        quotes._reset_for_tests()
        client._reset_for_tests()

    def test_status_lists_unmapped_and_unanswered_symbols(self):
        quotes.run_one_batch(["TCS", "NODATA", "GHOST"])      # GHOST has no scrip id, NODATA gets no quote back
        st = quotes.status()
        assert st["empty_symbols"] == 2
        assert st["empty_no_scrip_sample"] == ["GHOST"] and st["empty_no_scrip_distinct"] == 1
        assert st["empty_no_data_sample"] == ["NODATA"] and st["empty_no_data_distinct"] == 1

    def test_a_symbol_that_later_gets_priced_leaves_the_list(self):
        quotes.run_one_batch(["TCS", "INFY"])
        assert quotes.status()["empty_no_data_sample"] == ["INFY"]
        scrip_master._set_for_tests({"TCS": 11536, "INFY": 11536}, {})   # INFY now maps to the id the fake answers
        quotes.run_one_batch(["INFY"])
        assert quotes.status()["empty_no_data_sample"] == []

    def test_sample_is_bounded_and_most_recent_first(self):
        for i in range(60):
            quotes.run_one_batch([f"X{i}"])
        st = quotes.status()
        assert len(st["empty_no_scrip_sample"]) == 25 and st["empty_no_scrip_sample"][0] == "X59"
        assert st["empty_no_scrip_distinct"] == 60

    def test_counters_stay_plain_numbers(self):
        quotes.run_one_batch(["GHOST"])
        assert isinstance(quotes._stats["empty_symbols"], int)
        quotes._reset_for_tests()
        assert quotes.status()["empty_no_scrip_sample"] == []


# ── 4. /angelone/movers asks Dhan first ───────────────────────────────────────────────────────────────────────────
class _FakeSession:
    def __init__(self, configured):
        self._c = configured
        self.batches = []

    def is_configured(self):
        return self._c

    async def ensure_session(self):
        return None

    async def get_quotes_batch(self, exch, tokens, lane=None):
        self.batches.append(list(tokens))
        return [{"symbolToken": t, "ltp": 106.0, "close": 100.0} for t in tokens]


@pytest.fixture()
def movers_env(monkeypatch):
    import angelone_client
    import angelone_scrip_master as sm
    monkeypatch.setenv("QUOTE_PROVIDER_ORDER", "dhan,angelone,yfinance")
    monkeypatch.setenv("DHAN_DATA_ENABLED", "1")
    monkeypatch.delenv("DHAN_MOVERS_VIA_DHAN", raising=False)
    m._mem._d.clear()
    monkeypatch.setattr(m, "cache", None)
    monkeypatch.setattr(sm, "get_all_symbols", lambda: {"AAA": "1", "BBB": "2", "CCC": "3", "DDD": "4"})
    monkeypatch.setattr(m, "drop_etfs", lambda tm: (tm, 0))
    sess = _FakeSession(True)
    monkeypatch.setattr(angelone_client, "get_session", lambda: sess)
    yield sess
    m._mem._d.clear()


def _dhan_rows(**pr):
    return {k: {"price": p, "previous_close": c} for k, (p, c) in pr.items()}


class TestMoversViaDhan:
    def test_dhan_prices_everything_and_angelone_is_not_asked(self, monkeypatch, movers_env):
        monkeypatch.setattr(quotes, "get_quotes", lambda syms, **k: _dhan_rows(
            AAA=(108.0, 100.0), BBB=(101.0, 100.0), CCC=(90.0, 100.0), DDD=(100.0, 100.0)))
        out = m.angelone_movers()
        assert out["status"] == "ok" and movers_env.batches == []
        assert {r["symbol"]: r["pct_change"] for r in out["data"]} == {"AAA": 8.0, "CCC": -10.0}
        assert out["universe_size"] == 4 and out["quotes_fetched"] == 4 and out["dhan_priced"] == 4
        assert not out.get("partial")

    def test_only_the_unpriced_remainder_goes_to_angelone(self, monkeypatch, movers_env):
        monkeypatch.setattr(quotes, "get_quotes", lambda syms, **k: _dhan_rows(AAA=(108.0, 100.0), BBB=(101.0, 100.0)))
        out = m.angelone_movers()
        assert movers_env.batches == [["3", "4"]]                   # CCC, DDD tokens only
        got = {r["symbol"]: r["pct_change"] for r in out["data"]}
        assert got == {"AAA": 8.0, "CCC": 6.0, "DDD": 6.0}
        assert out["universe_size"] == 4 and out["quotes_fetched"] == 4

    def test_a_row_without_previous_close_counts_as_unpriced(self, monkeypatch, movers_env):
        monkeypatch.setattr(quotes, "get_quotes", lambda syms, **k: {
            "AAA": {"price": 108.0, "previous_close": None}, "BBB": {"price": 101.0, "previous_close": 100.0}})
        out = m.angelone_movers()
        assert movers_env.batches == [["1", "3", "4"]]
        assert out["dhan_priced"] == 1

    def test_dhan_failure_changes_nothing(self, monkeypatch, movers_env):
        monkeypatch.setattr(quotes, "get_quotes", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        out = m.angelone_movers()
        assert movers_env.batches == [["1", "2", "3", "4"]]
        assert out["status"] == "ok" and "dhan_priced" not in out
        assert {r["symbol"] for r in out["data"]} == {"AAA", "BBB", "CCC", "DDD"}

    def test_angelone_first_order_keeps_the_old_sweep(self, monkeypatch, movers_env):
        monkeypatch.setenv("QUOTE_PROVIDER_ORDER", "angelone,yfinance,dhan")
        monkeypatch.setattr(quotes, "get_quotes", lambda *a, **k: (_ for _ in ()).throw(AssertionError("not asked")))
        out = m.angelone_movers()
        assert movers_env.batches == [["1", "2", "3", "4"]] and "dhan_priced" not in out

    def test_switch_off(self, monkeypatch, movers_env):
        monkeypatch.setenv("DHAN_MOVERS_VIA_DHAN", "0")
        monkeypatch.setattr(quotes, "get_quotes", lambda *a, **k: (_ for _ in ()).throw(AssertionError("not asked")))
        m.angelone_movers()
        assert movers_env.batches == [["1", "2", "3", "4"]]

    def test_works_without_angelone_credentials_when_dhan_prices_all(self, monkeypatch, movers_env):
        movers_env._c = False
        monkeypatch.setattr(quotes, "get_quotes", lambda syms, **k: _dhan_rows(
            AAA=(108.0, 100.0), BBB=(101.0, 100.0), CCC=(90.0, 100.0), DDD=(100.0, 100.0)))
        out = m.angelone_movers()
        assert out["status"] == "ok" and len(out["data"]) == 2 and movers_env.batches == []

    def test_not_configured_when_neither_source_is_available(self, monkeypatch, movers_env):
        movers_env._c = False
        monkeypatch.setenv("QUOTE_PROVIDER_ORDER", "angelone,yfinance,dhan")
        assert m.angelone_movers()["status"] == "not_configured"

    def test_partial_coverage_is_flagged_on_the_combined_count(self, monkeypatch, movers_env):
        movers_env._c = False
        monkeypatch.setattr(quotes, "get_quotes", lambda syms, **k: _dhan_rows(AAA=(108.0, 100.0)))
        out = m.angelone_movers()
        assert out["partial"] is True and out["missing_quotes"] == 3


# ── 5. feed switches ──────────────────────────────────────────────────────────────────────────────────────────────
class TestFeedSwitches:
    @pytest.mark.parametrize("raw,on", [("", True), ("  ", True), ("1", True), ("true", True), ("garbage", True),
                                        ("0", False), ("false", False), ("OFF", False), ("no", False)])
    def test_flag_parsing_is_blank_safe(self, monkeypatch, raw, on):
        monkeypatch.setenv("ANGELONE_WS_FEED_ENABLED", raw)
        assert m._live_feed_enabled("ANGELONE_WS_FEED_ENABLED") is on

    def test_unset_means_on(self, monkeypatch):
        monkeypatch.delenv("YAHOO_WS_FEED_ENABLED", raising=False)
        assert m._live_feed_enabled("YAHOO_WS_FEED_ENABLED") is True

    def test_off_means_the_boot_start_does_not_touch_the_feed(self, monkeypatch):
        import angelone_ws_feed
        import yahoo_ws_feed
        monkeypatch.setenv("ANGELONE_WS_FEED_ENABLED", "0")
        monkeypatch.setenv("YAHOO_WS_FEED_ENABLED", "0")
        boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("feed must not start"))   # noqa: E731
        monkeypatch.setattr(angelone_ws_feed, "start_feed_background", boom)
        monkeypatch.setattr(yahoo_ws_feed, "start_feed_background", boom)
        m._boot_start_angelone_feed()
        m._boot_start_yahoo_feed()

    def test_on_means_the_yahoo_boot_start_runs(self, monkeypatch):
        import yahoo_ws_feed
        import surprise_premarket
        monkeypatch.delenv("YAHOO_WS_FEED_ENABLED", raising=False)
        started = []
        monkeypatch.setattr(surprise_premarket, "default_universe_from_env", lambda: ["TCS"])
        monkeypatch.setattr(yahoo_ws_feed, "start_feed_background", lambda u: started.append(list(u)))
        m._boot_start_yahoo_feed()
        assert started == [["TCS"]]


@pytest.mark.parametrize("envfile", [".env.example", ".env.oracle.recommended"])
def test_group275_variables_are_documented(envfile):
    text = open(os.path.join(ROOT, envfile), encoding="utf-8").read()
    for name in ("DHAN_LIVE_WRITE_CHUNK", "DHAN_MOVERS_VIA_DHAN", "ANGELONE_WS_FEED_ENABLED", "YAHOO_WS_FEED_ENABLED",
                 "GATEWAY_MOVERS_VIA_MARKET_DATA", "GATEWAY_MOVERS_QUOTE_MAX_AGE_S"):
        assert re.search(rf"^#?\s*{name}=", text, re.M), f"{envfile} does not mention {name}"
