"""
tests/test_angelone_feed_batch_writes.py

group153 (2026-10-05, log-list item 4, market-data overload): the AngelOne poll cycle
wrote live_quotes ONE ROW PER TRANSACTION - ~491 sequential MERGE round trips per cycle
for a 491-symbol universe, on the polling thread - so a slow DB stretched a cycle past
the 20 s freshness window real-trade-service applies to live_quotes rows.

Now every AngelOne batch (<= 50 rows) is written in ONE transaction (executemany), off the
event loop; the cycle time is remembered and a slow cycle logs one rate-limited WARNING.

No network, no DB, no AngelOne credentials: the engine is a recording fake.

Run from services/market-data-service:
    python -m pytest tests/test_angelone_feed_batch_writes.py -v
"""
from __future__ import annotations

import logging
import os
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


class _Conn:
    def __init__(self, log):
        self.log = log

    def execute(self, stmt, params=None):
        self.log.append((str(stmt), params))


class _Begin:
    def __init__(self, eng):
        self.eng = eng

    def __enter__(self):
        self.eng.transactions += 1
        return _Conn(self.eng.log)

    def __exit__(self, *a):
        return False


class FakeEngine:
    def __init__(self, fail=False):
        self.log, self.transactions, self.fail = [], 0, fail

    def begin(self):
        if self.fail:
            raise RuntimeError("db down")
        return _Begin(self)


@pytest.fixture()
def feed(monkeypatch):
    mh = types.ModuleType("market_hours")
    mh.is_feed_window_ist = lambda: True
    monkeypatch.setitem(sys.modules, "market_hours", mh)
    sys.modules.pop("angelone_ws_feed", None)
    import angelone_ws_feed as f
    monkeypatch.setattr(f, "_ensure_schema", lambda *a, **k: None)
    yield f
    f._running = False
    if f._thread is not None:
        f._thread.join(timeout=5)
    sys.modules.pop("angelone_ws_feed", None)


def _row(sym, ltp=100.0):
    return (sym, ltp, 99.0, 101.0, 98.0, 99.5, 1000)


@pytest.mark.parametrize("dialect,frag", [("postgresql", "ON CONFLICT"), ("oracle", "MERGE INTO")])
def test_batch_upsert_is_one_transaction_for_all_rows(feed, monkeypatch, dialect, frag):
    eng = FakeEngine()
    monkeypatch.setattr(feed, "_get_live_quotes_engine", lambda: (eng, dialect))
    feed._upsert_ticks_batch_sync([_row("A"), _row("B"), _row("C")])
    assert eng.transactions == 1
    assert len(eng.log) == 1                       # ONE execute (executemany), not three
    sql, params = eng.log[0]
    assert frag in sql
    assert [p["s"] for p in params] == ["A", "B", "C"]
    assert params[0]["l"] == 100.0 and params[0]["v"] == 1000


def test_batch_upsert_skips_rows_without_symbol_or_price(feed, monkeypatch):
    eng = FakeEngine()
    monkeypatch.setattr(feed, "_get_live_quotes_engine", lambda: (eng, "postgresql"))
    feed._upsert_ticks_batch_sync([_row("A"), ("", 1, 1, 1, 1, 1, 1), _row("B", ltp=None), _row("C", ltp=0)])
    assert [p["s"] for p in eng.log[0][1]] == ["A"]
    eng2 = FakeEngine()
    monkeypatch.setattr(feed, "_get_live_quotes_engine", lambda: (eng2, "postgresql"))
    feed._upsert_ticks_batch_sync([_row("X", ltp=0)])
    assert eng2.transactions == 0                  # nothing to write -> no transaction at all


def test_batch_upsert_no_engine_and_db_failure_do_not_raise(feed, monkeypatch):
    monkeypatch.setattr(feed, "_get_live_quotes_engine", lambda: (None, "postgresql"))
    feed._upsert_ticks_batch_sync([_row("A")])     # no durable DB configured: silent no-op
    monkeypatch.setattr(feed, "_get_live_quotes_engine", lambda: (FakeEngine(fail=True), "postgresql"))
    feed._upsert_ticks_batch_sync([_row("A")])     # DB error is swallowed (debug log only)


def test_on_tick_write_db_false_updates_memory_and_returns_row(feed, monkeypatch):
    calls = []
    monkeypatch.setattr(feed, "_upsert_tick_sync", lambda *a: calls.append(a))
    out = feed._on_tick_sync({"tradingSymbol": "INFY.NS", "ltp": 50, "open": 1, "high": 2, "low": 0.5,
                              "close": 49, "tradeVolume": 7}, write_db=False)
    assert out == ("INFY", 50, 1, 2, 0.5, 49, 7)
    assert calls == []                             # no per-row DB write
    assert feed.get_live_quote("INFY")["price"] == 50   # the in-memory cache is still updated


def test_on_tick_default_still_writes_the_row(feed, monkeypatch):
    calls = []
    monkeypatch.setattr(feed, "_upsert_tick_sync", lambda *a: calls.append(a))
    assert feed._on_tick_sync({"tradingSymbol": "TCS", "ltp": 10}) is None
    assert len(calls) == 1 and calls[0][0] == "TCS"
    assert feed._on_tick_sync({"tradingSymbol": "", "ltp": 10}) is None   # no symbol: ignored


class _Session:
    def __init__(self, n):
        self.n, self.batches = n, []

    async def ensure_session(self):
        return None

    async def get_quotes_batch(self, exchange, tokens, lane=None):
        self.batches.append(list(tokens))
        return [{"symbolToken": t, "ltp": 10.0 + int(t), "open": 1, "high": 2, "low": 1, "close": 1,
                 "tradeVolume": 5} for t in tokens]


def _run_one_cycle(feed, monkeypatch, n_symbols, *, batch_writes=True):
    symbols = [f"S{i}" for i in range(n_symbols)]
    sess = _Session(n_symbols)
    ac = types.ModuleType("angelone_client")
    ac.get_session = lambda: sess
    sm = types.ModuleType("angelone_scrip_master")
    sm.get_tokens_bulk = lambda syms, wait_s=0: {x: str(i + 1) for i, x in enumerate(syms)}
    sm.status = lambda: {"loaded_symbols": 10}
    monkeypatch.setitem(sys.modules, "angelone_client", ac)
    monkeypatch.setitem(sys.modules, "angelone_scrip_master", sm)
    monkeypatch.setattr(feed, "BATCH_GAP_S", 0.0)
    monkeypatch.setattr(feed, "POLL_INTERVAL_S", 30.0)    # one cycle, then a long sleep
    monkeypatch.setattr(feed, "DB_BATCH_WRITES", batch_writes)
    batch_calls, single_calls = [], []
    monkeypatch.setattr(feed, "_upsert_ticks_batch_sync", lambda rows: batch_calls.append(list(rows)))
    monkeypatch.setattr(feed, "_upsert_tick_sync", lambda *a: single_calls.append(a))
    feed.start_feed_background(symbols)
    end = time.time() + 5
    while time.time() < end and feed._last_cycle_s is None:
        time.sleep(0.02)
    feed._running = False
    return sess, batch_calls, single_calls


def test_poll_cycle_writes_one_batch_per_angelone_batch_not_one_row_per_symbol(feed, monkeypatch):
    sess, batch_calls, single_calls = _run_one_cycle(feed, monkeypatch, 120)
    assert len(sess.batches) == 3                          # 50 + 50 + 20 tokens
    assert [len(b) for b in batch_calls] == [50, 50, 20]   # 3 DB writes, not 120
    assert single_calls == []
    assert len(feed.get_live_quotes_bulk([f"S{i}" for i in range(120)])) == 120
    assert feed.feed_status()["last_cycle_s"] is not None


def test_off_switch_restores_per_row_writes(feed, monkeypatch):
    sess, batch_calls, single_calls = _run_one_cycle(feed, monkeypatch, 60, batch_writes=False)
    assert batch_calls == []
    assert len(single_calls) == 60


def test_slow_cycle_logs_one_rate_limited_warning(feed, caplog):
    feed._last_slow_cycle_log = 0.0
    with caplog.at_level(logging.WARNING):
        feed._note_cycle(5.0, 491)                         # fast: nothing
        assert not [r for r in caplog.records if "poll cycle" in r.getMessage()]
        feed._note_cycle(22.5, 491)                        # slow: one warning
        feed._note_cycle(30.0, 491)                        # slow again within 5 min: suppressed
    msgs = [r.getMessage() for r in caplog.records if "poll cycle" in r.getMessage()]
    assert len(msgs) == 1 and "491 symbols" in msgs[0] and "22.5s" in msgs[0]
    assert feed._last_cycle_s == 30.0
