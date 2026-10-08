"""group256 (2026-10-08 ~14:15 IST log, after group 255).

1. The log said "candle calls are answered again, 1s after the first 403" under a 30 s cooldown. A candle call already in flight
   when the 403 arrived came back normally a second later and was taken for the recovery. A call now counts only when it was
   SENT after the candle cooldown ended.
2. A quote 403 (30 s cooldown) sent every symbol AngelOne-first could not price to the Yahoo path (saturated yfinance, an 18 s
   yf.download timeout, ~100 ReadTimeouts in real-trade-service). While the quote cooldown runs, /quote and the leftovers of
   /quotes/bulk now serve an unheld symbol's cached price (<= QUOTE_COOLDOWN_STALE_MAX_AGE_S, default 180 s, tagged
   stale_cooldown(<source>), real fetched_at kept) and, with none, answer "no price" at once (source cooldown_unpriced, not
   negative-cached) instead of calling Yahoo. Held symbols are untouched.

Run from services/market-data-service:  python3 -m pytest tests/test_group256_recovery_inflight_and_cooldown_stale.py -v
"""
import asyncio
import logging
import os
import sys
import types
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import angelone_budget as b
import angelone_client as ac
import rate_limiter as rl


# ───────────────────────── part 1: recovery measurement ─────────────────────────
class _Clock:
    def __init__(self, t=2_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(b.time, "time", c)
    b._reset()
    yield c
    b._reset()


def test_call_in_flight_during_the_cooldown_is_not_the_recovery(clock, caplog):
    sent = clock.t
    b.trip("getCandleData")                            # cooldown 30 s
    clock.t += 1
    with caplog.at_level(logging.INFO, logger="angelone-budget"):
        b.note_candle_ok(sent)
    assert [r for r in caplog.records if "answered again" in r.getMessage()] == []
    assert b.stats()["candle_calls"]["last_block_lasted_s"] is None
    assert b._candle_series_start != 0.0


def test_call_sent_after_the_cooldown_is_the_recovery(clock, caplog):
    b.trip("getCandleData")
    clock.t += 35
    sent = clock.t
    clock.t += 1
    with caplog.at_level(logging.INFO, logger="angelone-budget"):
        b.note_candle_ok(sent)
    lines = [r.getMessage() for r in caplog.records if "answered again" in r.getMessage()]
    assert len(lines) == 1 and "36s after the first 403" in lines[0]
    assert b.stats()["candle_calls"]["last_block_lasted_s"] == 36.0


def test_call_sent_before_the_cooldown_ended_but_answered_after_is_not_the_recovery(clock):
    b.trip("getCandleData")
    clock.t += 20
    sent = clock.t
    clock.t += 20
    b.note_candle_ok(sent)
    assert b.stats()["candle_calls"]["last_block_lasted_s"] is None


def test_without_a_send_time_the_answer_must_arrive_after_the_cooldown(clock):
    b.trip("getCandleData")
    clock.t += 5
    b.note_candle_ok()
    assert b.stats()["candle_calls"]["last_block_lasted_s"] is None
    clock.t += 30
    b.note_candle_ok()
    assert b.stats()["candle_calls"]["last_block_lasted_s"] == 35.0


def test_a_later_ok_after_an_ignored_one_still_closes_the_run(clock):
    sent = clock.t
    b.trip("getCandleData")
    clock.t += 1
    b.note_candle_ok(sent)                             # ignored
    clock.t += 40
    b.note_candle_ok(clock.t)
    assert b.stats()["candle_calls"]["last_block_lasted_s"] == 41.0


class _Resp:
    def __init__(self, status=200, body=None, text="ok"):
        self.status_code = status
        self._body = body if body is not None else {"data": [["t", 1, 2, 3, 4, 5]]}
        self.text = text

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class _FakeHttp:
    resp = _Resp()

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        return _FakeHttp.resp


def _patch_client(monkeypatch, http_cls):
    async def _noop(self):
        return None

    monkeypatch.setattr(ac.AngelOneSession, "ensure_session", _noop)
    monkeypatch.setattr(ac.AngelOneSession, "_headers", lambda self: {})
    monkeypatch.setattr(ac.httpx, "AsyncClient", http_cls)
    monkeypatch.setattr(ac, "_rl_try_acquire", lambda *a, **k: True)


def _get_candles():
    sess = ac.AngelOneSession.__new__(ac.AngelOneSession)
    return asyncio.run(sess.get_candles("NSE", "1", "ONE_DAY", "a", "b"))


def test_get_candles_passes_its_send_time(monkeypatch):
    got = []
    monkeypatch.setattr(ac, "_budget_note_candle_ok", lambda sent_at=None: got.append(sent_at))
    _FakeHttp.resp = _Resp()
    _patch_client(monkeypatch, _FakeHttp)
    assert _get_candles()
    assert len(got) == 1 and isinstance(got[0], float) and got[0] > 1_000_000_000


def test_get_candles_call_in_flight_when_another_call_gets_403_keeps_the_run_open(monkeypatch):
    """The first call's 403 trips the cooldown while this one is still on the wire; it then comes back normally.
    That answer must not close the run (the 2026-10-08 log: 'answered again, 1s after the first 403')."""
    b._reset()

    class _Race(_FakeHttp):
        async def post(self, *a, **k):
            b.trip("getCandleData")
            return _Resp()

    try:
        _patch_client(monkeypatch, _Race)
        assert _get_candles()
        assert b._candle_series_start != 0.0
        assert b.stats()["candle_calls"]["last_block_lasted_s"] is None
    finally:
        b._reset()


# ───────────────────────── part 2: /quote during a quote cooldown ─────────────────────────
try:
    import main
except Exception:   # pragma: no cover - needs fastapi etc.
    main = None

needs_main = pytest.mark.skipif(main is None, reason="market-data main.py not importable here")

_ENV = ("QUOTE_COOLDOWN_SERVE_STALE", "QUOTE_COOLDOWN_SKIP_YAHOO", "QUOTE_COOLDOWN_STALE_MAX_AGE_S")


def _row(sym, price=100.0, age_s=30.0, source="angelone_rest"):
    return {"symbol": sym, "price": price, "cmp": price, "source": source,
            "fetched_at": (datetime.utcnow() - timedelta(seconds=age_s)).isoformat()}


@pytest.fixture
def cd(monkeypatch):
    """AngelOne quote cooldown running, nobody holds any symbol, market open, no fresh cache, Yahoo calls recorded."""
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    st = {"cooling": True, "held": set(), "yahoo": []}
    monkeypatch.setattr(main, "_angelone_quote_cooling", lambda: st["cooling"])
    monkeypatch.setattr(main, "_cooldown_symbol_held", lambda s: str(s).upper().replace(".NS", "") in st["held"])
    monkeypatch.setattr(main, "_quote_market_closed", lambda: False)
    monkeypatch.setattr(main, "_angelone_rest_quote_first", lambda s: None)
    monkeypatch.setattr(main, "_neg_blocked", lambda s: False)
    monkeypatch.setattr(main, "_in_cooldown", lambda name="yfinance": False)

    def _yahoo(sym):
        st["yahoo"].append(sym)
        return None

    monkeypatch.setattr(main, "_yahoo_ohlcv_quote", _yahoo)
    monkeypatch.setattr(main, "_cache_get", lambda k: None)
    monkeypatch.setattr(main, "_fallback_get", lambda k: None)
    main._COOLDOWN_STALE_STATS.update(served=0, unpriced=0)
    return st


@needs_main
def test_cfg_defaults_and_blank_safe(monkeypatch):
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    assert main._quote_cooldown_cfg() == (True, True, 180.0)
    monkeypatch.setenv("QUOTE_COOLDOWN_STALE_MAX_AGE_S", "  ")
    monkeypatch.setenv("QUOTE_COOLDOWN_SERVE_STALE", " ")
    assert main._quote_cooldown_cfg() == (True, True, 180.0)
    monkeypatch.setenv("QUOTE_COOLDOWN_STALE_MAX_AGE_S", "nonsense")
    assert main._quote_cooldown_cfg()[2] == 180.0
    monkeypatch.setenv("QUOTE_COOLDOWN_STALE_MAX_AGE_S", "0")
    assert main._quote_cooldown_cfg()[2] == 180.0
    monkeypatch.setenv("QUOTE_COOLDOWN_STALE_MAX_AGE_S", "90")
    monkeypatch.setenv("QUOTE_COOLDOWN_SERVE_STALE", "off")
    monkeypatch.setenv("QUOTE_COOLDOWN_SKIP_YAHOO", "0")
    assert main._quote_cooldown_cfg() == (False, False, 90.0)


@needs_main
def test_stale_row_helper_serves_recent_and_tags_source(cd, monkeypatch):
    monkeypatch.setattr(main, "_fallback_get", lambda k: _row("ABC", 55.5, age_s=100))
    r = main._cooldown_stale_row("ABC")
    assert r["price"] == 55.5 and r["source"] == "stale_cooldown(angelone_rest)"
    assert 95 < main._quote_row_age_s(r) < 140          # the REAL fetched_at is kept


@needs_main
def test_stale_row_helper_rejects_old_priceless_and_untimed(cd, monkeypatch):
    monkeypatch.setattr(main, "_fallback_get", lambda k: _row("ABC", age_s=400))
    assert main._cooldown_stale_row("ABC") is None
    monkeypatch.setattr(main, "_fallback_get", lambda k: _row("ABC", price=0.0, age_s=10))
    assert main._cooldown_stale_row("ABC") is None
    monkeypatch.setattr(main, "_fallback_get", lambda k: {"price": 10.0, "source": "x"})
    assert main._cooldown_stale_row("ABC") is None


@needs_main
def test_stale_row_helper_prefers_cache_and_never_double_tags(cd, monkeypatch):
    monkeypatch.setattr(main, "_cache_get", lambda k: _row("ABC", 60.0, age_s=20, source="stale_cooldown(yahoo_ws)"))
    monkeypatch.setattr(main, "_fallback_get", lambda k: _row("ABC", 50.0, age_s=100))
    r = main._cooldown_stale_row("ABC")
    assert r["price"] == 60.0 and r["source"] == "stale_cooldown(yahoo_ws)"


@needs_main
def test_stale_row_helper_never_raises(cd, monkeypatch):
    def boom(k):
        raise RuntimeError("cache down")

    monkeypatch.setattr(main, "_cache_get", boom)
    monkeypatch.setattr(main, "_fallback_get", boom)
    assert main._cooldown_stale_row("ABC") is None


@needs_main
def test_quote_serves_recent_cached_price_and_never_calls_yahoo(cd, monkeypatch):
    monkeypatch.setattr(main, "_fallback_get", lambda k: _row("JKTYRE", 345.0, age_s=60))
    r = main._get_quote_inner("JKTYRE")
    assert r["price"] == 345.0 and r["source"].startswith("stale_cooldown(")
    assert cd["yahoo"] == [] and main._COOLDOWN_STALE_STATS["served"] == 1


@needs_main
def test_quote_without_a_recent_price_answers_at_once_without_yahoo(cd):
    r = main._get_quote_inner("NEWNAME")
    assert r["price"] is None and r["source"] == "cooldown_unpriced"
    assert cd["yahoo"] == [] and main._COOLDOWN_STALE_STATS["unpriced"] == 1


@needs_main
def test_cooldown_unpriced_is_not_negative_cached(cd, monkeypatch):
    recorded = []
    monkeypatch.setattr(main, "_neg_record_failure", lambda s: recorded.append(s))
    r = main.get_quote("NEWNAME")
    assert r["source"] == "cooldown_unpriced" and recorded == []


@needs_main
def test_old_row_is_not_served_and_falls_to_unpriced(cd, monkeypatch):
    monkeypatch.setattr(main, "_fallback_get", lambda k: _row("OLD", age_s=900))
    r = main._get_quote_inner("OLD")
    assert r["source"] == "cooldown_unpriced" and cd["yahoo"] == []


@needs_main
def test_held_symbol_keeps_the_old_path(cd, monkeypatch):
    cd["held"].add("HELD")
    monkeypatch.setattr(main, "_fallback_get", lambda k: _row("HELD", age_s=10))
    r = main._get_quote_inner("HELD")
    assert len(cd["yahoo"]) == 1 and cd["yahoo"][0].startswith("HELD")
    assert not str(r.get("source") or "").startswith(("stale_cooldown", "cooldown_unpriced"))


@needs_main
def test_no_cooldown_means_no_change(cd, monkeypatch):
    cd["cooling"] = False
    monkeypatch.setattr(main, "_fallback_get", lambda k: _row("ABC", age_s=10))
    main._get_quote_inner("ABC")
    assert len(cd["yahoo"]) == 1 and cd["yahoo"][0].startswith("ABC")


@needs_main
def test_indices_are_not_touched(cd, monkeypatch):
    monkeypatch.setattr(main, "_fallback_get", lambda k: _row("^NSEI", age_s=10))
    main._get_quote_inner("^NSEI")
    assert cd["yahoo"] != []


@needs_main
def test_off_switch_restores_the_yahoo_path(cd, monkeypatch):
    monkeypatch.setenv("QUOTE_COOLDOWN_SERVE_STALE", "0")
    monkeypatch.setattr(main, "_fallback_get", lambda k: _row("ABC", age_s=10))
    main._get_quote_inner("ABC")
    assert len(cd["yahoo"]) == 1 and cd["yahoo"][0].startswith("ABC")


@needs_main
def test_skip_yahoo_off_still_serves_a_recent_row_but_lets_the_rest_through(cd, monkeypatch):
    monkeypatch.setenv("QUOTE_COOLDOWN_SKIP_YAHOO", "0")
    rows = {"quote:HAS.NS": _row("HAS", 10.0, age_s=20), "quote:HAS": _row("HAS", 10.0, age_s=20)}
    monkeypatch.setattr(main, "_fallback_get", lambda k: rows.get(k))
    assert main._get_quote_inner("HAS")["source"].startswith("stale_cooldown(")
    main._get_quote_inner("NOPE")
    assert len(cd["yahoo"]) == 1 and "NOPE" in cd["yahoo"][0]


@needs_main
def test_cooling_check_reads_the_quote_cooldown_only(monkeypatch):
    b._reset()
    assert main._angelone_quote_cooling() is False
    b.trip("quote")
    assert main._angelone_quote_cooling() is True
    b._reset()
    b.trip("getCandleData")                            # a candle cooldown must not count
    assert main._angelone_quote_cooling() is False
    b._reset()


@needs_main
def test_held_check_fails_safe(monkeypatch):
    import angelone_budget
    monkeypatch.setattr(angelone_budget, "lane_for", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert main._cooldown_symbol_held("ABC") is True
    monkeypatch.setattr(angelone_budget, "lane_for", lambda *a, **k: angelone_budget.POSITION)
    assert main._cooldown_symbol_held("ABC") is True
    monkeypatch.setattr(angelone_budget, "lane_for", lambda *a, **k: angelone_budget.CANDIDATE)
    assert main._cooldown_symbol_held("ABC") is False


# ───────────────────────── part 3: /quotes/bulk leftovers ─────────────────────────
@pytest.fixture()
def bulk(monkeypatch):
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    cache, fallback, st = {}, {}, {"cooling": True, "held": set(), "yf": []}
    monkeypatch.setattr(main, "_cache_get", lambda k: cache.get(k))
    monkeypatch.setattr(main, "_cache_set", lambda k, v, ttl=None: cache.__setitem__(k, v))
    monkeypatch.setattr(main, "_fallback_get", lambda k: fallback.get(k))
    monkeypatch.setattr(main, "normalize_symbol", lambda s: f"{str(s).upper().replace('.NS', '')}.NS")
    monkeypatch.setattr(main, "is_known_delisted", lambda s: False)
    monkeypatch.setattr(main, "_quote_market_closed", lambda: False)
    monkeypatch.setattr(main, "_quote_preopen", lambda: False)
    monkeypatch.setattr(main, "_angelone_quote_cooling", lambda: st["cooling"])
    monkeypatch.setattr(main, "_cooldown_symbol_held", lambda s: str(s).upper().replace(".NS", "") in st["held"])
    live = types.ModuleType("angelone_ws_feed")
    live.get_live_quotes_bulk = lambda syms, max_age_sec=15.0: {}
    monkeypatch.setitem(sys.modules, "angelone_ws_feed", live)
    ylive = types.ModuleType("yahoo_ws_feed")
    ylive.get_live_quotes_bulk = lambda syms, max_age_sec=15.0: {}
    monkeypatch.setitem(sys.modules, "yahoo_ws_feed", ylive)
    monkeypatch.setattr(main._rl, "would_block", lambda *a, **k: False)

    def _yf(*a, **k):
        st["yf"].append(k.get("tickers"))
        raise RuntimeError("no Yahoo in tests")

    monkeypatch.setattr(main.yf, "download", _yf)
    main._COOLDOWN_STALE_STATS.update(served=0, unpriced=0)
    return cache, fallback, st


def _breq(*syms):
    return main.BulkQuoteRequest(symbols=list(syms))


@needs_main
def test_bulk_leftovers_get_recent_cached_prices_and_skip_yfinance(bulk):
    cache, fb, st = bulk
    fb["quote:TCS.NS"] = _row("TCS", 4100.0, age_s=90)
    cache["quote:INFY.NS"] = _row("INFY", 1500.0, age_s=150)
    out = main._get_quotes_bulk_core(_breq("TCS", "INFY"), {})
    got = {q["symbol"]: (q["price"], q["source"]) for q in out["quotes"]}
    assert got["TCS"][0] == 4100.0 and got["TCS"][1].startswith("stale_cooldown(")
    assert got["INFY"][0] == 1500.0
    assert st["yf"] == [] and not out.get("degraded")


@needs_main
def test_bulk_symbol_without_a_recent_price_is_left_out_not_sent_to_yahoo(bulk):
    cache, fb, st = bulk
    fb["quote:TCS.NS"] = _row("TCS", 4100.0, age_s=60)
    fb["quote:OLD.NS"] = _row("OLD", 10.0, age_s=2000)
    out = main._get_quotes_bulk_core(_breq("TCS", "OLD", "NEWNAME"), {})
    assert [q["symbol"] for q in out["quotes"]] == ["TCS"]
    assert st["yf"] == []
    assert main._COOLDOWN_STALE_STATS == {"served": 1, "unpriced": 2}


@needs_main
def test_bulk_held_symbol_still_goes_to_yfinance(bulk):
    cache, fb, st = bulk
    st["held"].add("HELD")
    fb["quote:TCS.NS"] = _row("TCS", 4100.0, age_s=60)
    fb["quote:HELD.NS"] = _row("HELD", 10.0, age_s=10)
    main._get_quotes_bulk_core(_breq("TCS", "HELD"), {})
    assert len(st["yf"]) == 1 and "HELD.NS" in st["yf"][0] and "TCS.NS" not in st["yf"][0]


@needs_main
def test_bulk_no_cooldown_means_no_change(bulk):
    cache, fb, st = bulk
    st["cooling"] = False
    fb["quote:TCS.NS"] = _row("TCS", 4100.0, age_s=60)
    main._get_quotes_bulk_core(_breq("TCS"), {})
    assert len(st["yf"]) == 1


@needs_main
def test_bulk_off_switch_and_skip_yahoo_off(bulk, monkeypatch):
    cache, fb, st = bulk
    fb["quote:TCS.NS"] = _row("TCS", 4100.0, age_s=60)
    monkeypatch.setenv("QUOTE_COOLDOWN_SERVE_STALE", "0")
    main._get_quotes_bulk_core(_breq("TCS"), {})
    assert len(st["yf"]) == 1
    st["yf"].clear()
    monkeypatch.setenv("QUOTE_COOLDOWN_SERVE_STALE", "1")
    monkeypatch.setenv("QUOTE_COOLDOWN_SKIP_YAHOO", "0")
    main._get_quotes_bulk_core(_breq("TCS", "NEWNAME"), {})
    assert len(st["yf"]) == 1 and "NEWNAME.NS" in st["yf"][0] and "TCS.NS" not in st["yf"][0]


@needs_main
def test_bulk_logs_one_summary_line(bulk, caplog):
    cache, fb, st = bulk
    fb["quote:TCS.NS"] = _row("TCS", 4100.0, age_s=60)
    with caplog.at_level(logging.INFO, logger="market-data-service"):
        main._get_quotes_bulk_core(_breq("TCS", "NEWNAME"), {})
    lines = [r.getMessage() for r in caplog.records if "AngelOne quote cooldown running" in r.getMessage()]
    assert len(lines) == 1 and "1 leftover symbol(s) served" in lines[0] and "1 left unpriced" in lines[0]


# ───────────────────────── group257: 403 diagnostics (headers + session age) ─────────────────────────
class _H(dict):
    def get(self, k, d=None):
        return super().get(k.lower(), d)


class _R403:
    status_code = 403
    text = "Access denied because of exceeding access rate"

    def __init__(self, headers=None):
        self.headers = _H({k.lower(): v for k, v in (headers or {}).items()})


def test_denied_context_lists_known_headers_and_session_age(monkeypatch):
    monkeypatch.setattr(ac, "_login_at", ac.time.time() - 125)
    s = ac._denied_context(_R403({"Retry-After": "60", "Server": "AkamaiGHost", "X-Other": "ignored"}))
    assert "retry-after=60" in s and "server=AkamaiGHost" in s and "x-other" not in s
    assert "session_age=125s" in s or "session_age=126s" in s


def test_denied_context_without_login_or_headers_and_never_raises(monkeypatch):
    monkeypatch.setattr(ac, "_login_at", 0.0)
    assert ac._denied_context(_R403()) == "session_age=unknown"

    class _Boom:
        @property
        def headers(self):
            raise RuntimeError("x")

    assert isinstance(ac._denied_context(_Boom()), str)


def test_log_denied_line_carries_the_context(monkeypatch, caplog):
    monkeypatch.setattr(ac, "_denied_last_logged", {})
    monkeypatch.setattr(ac, "_login_at", ac.time.time() - 10)
    with caplog.at_level(logging.WARNING, logger="angelone-client"):
        ac._log_denied("getCandleData", _R403({"Retry-After": "30"}))
    lines = [r.getMessage() for r in caplog.records if "getCandleData returned HTTP 403" in r.getMessage()]
    assert len(lines) == 1 and "retry-after=30" in lines[0] and "session_age=" in lines[0]
