"""group487 (2026-10-21 log): /quotes/bulk sent the few symbols Dhan could not price to yf.download (18 s hard timeout,
then reported as a 502 rate-limit event).

1. When Dhan priced at least QUOTE_BULK_SMALL_LEFTOVER_MIN_PRICED_PCT (default 90) of the request and at most
   QUOTE_BULK_SMALL_LEFTOVER_MAX (default 8) symbols are left, those get a cached price (<= QUOTE_BULK_LEFTOVER_MAX_AGE_S,
   default 180 s, tagged stale_leftover(<source>)) and, with none, are left out. No yf.download. Indices and held symbols
   keep the normal path. QUOTE_BULK_SMALL_LEFTOVER_SKIP=0 restores the old behaviour.
2. A yfinance hard timeout / open circuit in /quotes/bulk is not reported as a rate-limit event any more.

Run from services/market-data-service:  python3 -m pytest tests/test_group487_bulk_small_leftover.py -v
"""
import os
import sys
import types
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

try:
    import main
except Exception:   # pragma: no cover - needs fastapi etc.
    main = None

needs_main = pytest.mark.skipif(main is None, reason="market-data main.py not importable here")

_ENV = ("QUOTE_BULK_SMALL_LEFTOVER_SKIP", "QUOTE_BULK_SMALL_LEFTOVER_MAX", "QUOTE_BULK_SMALL_LEFTOVER_MIN_PRICED_PCT",
        "QUOTE_BULK_LEFTOVER_MAX_AGE_S")


def _row(sym, price=100.0, age_s=30.0, source="angelone_rest"):
    return {"symbol": sym, "price": price, "cmp": price, "source": source,
            "fetched_at": (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=age_s)).isoformat()}


@pytest.fixture()
def bulk(monkeypatch):
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    cache, fallback = {}, {}
    st = {"held": set(), "yf": [], "dhan_prices": {}, "reported": []}
    monkeypatch.setattr(main, "_cache_get", lambda k: cache.get(k))
    monkeypatch.setattr(main, "_cache_set", lambda k, v, ttl=None: cache.__setitem__(k, v))
    monkeypatch.setattr(main, "_fallback_get", lambda k: fallback.get(k))
    monkeypatch.setattr(main, "normalize_symbol", lambda s: s if str(s).startswith("^") else f"{str(s).upper().replace('.NS', '')}.NS")
    monkeypatch.setattr(main, "is_known_delisted", lambda s: False)
    monkeypatch.setattr(main, "_quote_market_closed", lambda: False)
    monkeypatch.setattr(main, "_quote_preopen", lambda: False)
    monkeypatch.setattr(main, "_angelone_quote_cooling", lambda: False)
    monkeypatch.setattr(main, "_cooldown_symbol_held", lambda s: str(s).upper().replace(".NS", "") in st["held"])
    monkeypatch.setattr(main, "_dhan_quote_position", lambda: "first")
    st["unmapped"] = set()
    monkeypatch.setattr(main, "_dhan_can_price", lambda base: base not in st["unmapped"])

    def _dhan(tickers, symbol_map, results):
        left = []
        for t in tickers:
            base = symbol_map.get(t, t)
            px = st["dhan_prices"].get(base)
            if px is None:
                left.append(t)
                continue
            q = main._pad_quote_response(base, {"price": px, "cmp": px})
            q["source"] = "dhan"
            results.append(q)
        return left

    monkeypatch.setattr(main, "_dhan_bulk_stage", _dhan)
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
    monkeypatch.setattr(main, "_report_rate_limit", lambda status, path="", detail="", symbol="": st["reported"].append(status))
    main._LEFTOVER_STALE_STATS.update(served=0, unpriced=0)
    return cache, fallback, st


def _breq(*syms):
    return main.BulkQuoteRequest(symbols=list(syms))


def _names(n, prefix="S"):
    return [f"{prefix}{i}" for i in range(n)]


@needs_main
def test_cfg_defaults_and_blank_safe(monkeypatch):
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    assert main._bulk_leftover_cfg() == (True, 8, 90.0, 180.0)
    for k in _ENV:
        monkeypatch.setenv(k, "  ")
    assert main._bulk_leftover_cfg() == (True, 8, 90.0, 180.0)
    for k in _ENV[1:]:
        monkeypatch.setenv(k, "nonsense")
    assert main._bulk_leftover_cfg() == (True, 8, 90.0, 180.0)
    monkeypatch.setenv("QUOTE_BULK_SMALL_LEFTOVER_MAX", "3")
    monkeypatch.setenv("QUOTE_BULK_SMALL_LEFTOVER_MIN_PRICED_PCT", "95")
    monkeypatch.setenv("QUOTE_BULK_LEFTOVER_MAX_AGE_S", "60")
    monkeypatch.setenv("QUOTE_BULK_SMALL_LEFTOVER_SKIP", "off")
    assert main._bulk_leftover_cfg() == (False, 3, 95.0, 60.0)
    monkeypatch.setenv("QUOTE_BULK_SMALL_LEFTOVER_MAX", "9999")
    monkeypatch.setenv("QUOTE_BULK_SMALL_LEFTOVER_MIN_PRICED_PCT", "10")
    assert main._bulk_leftover_cfg()[1:3] == (8, 90.0)


@needs_main
def test_leftover_gets_a_recent_cached_price_and_skips_yfinance(bulk):
    cache, fb, st = bulk
    names = _names(30)
    st["dhan_prices"] = {n: 50.0 for n in names[:28]}
    fb["quote:S28.NS"] = _row("S28", 77.0, age_s=60)
    out = main._get_quotes_bulk_core(_breq(*names), {})
    got = {q["symbol"]: (q["price"], q["source"]) for q in out["quotes"]}
    assert st["yf"] == []
    assert len(got) == 29 and "S29" not in got
    assert got["S28"][0] == 77.0 and got["S28"][1].startswith("stale_leftover(")
    assert got["S0"][1] == "dhan"
    assert main._LEFTOVER_STALE_STATS == {"served": 1, "unpriced": 1}


@needs_main
def test_old_cached_row_is_not_served(bulk):
    cache, fb, st = bulk
    names = _names(20)
    st["dhan_prices"] = {n: 50.0 for n in names[:19]}
    fb["quote:S19.NS"] = _row("S19", 77.0, age_s=2000)
    out = main._get_quotes_bulk_core(_breq(*names), {})
    assert "S19" not in {q["symbol"] for q in out["quotes"]}
    assert st["yf"] == []


@needs_main
def test_too_many_leftovers_keep_the_yfinance_path(bulk):
    cache, fb, st = bulk
    names = _names(30)
    st["dhan_prices"] = {n: 50.0 for n in names[:20]}          # 10 left > 8
    main._get_quotes_bulk_core(_breq(*names), {})
    assert len(st["yf"]) == 1


@needs_main
def test_low_priced_share_keeps_the_yfinance_path(bulk):
    cache, fb, st = bulk
    st["dhan_prices"] = {"A": 1.0}                              # 1 of 2 priced = 50% < 90%
    main._get_quotes_bulk_core(_breq("A", "B"), {})
    assert len(st["yf"]) == 1


@needs_main
def test_without_any_dhan_price_the_gate_does_not_apply(bulk):
    cache, fb, st = bulk
    cache["quote:A.NS"] = _row("A", 5.0, age_s=1)
    main._get_quotes_bulk_core(_breq("A", "B"), {})
    assert len(st["yf"]) == 1               # nothing priced by Dhan: old behaviour, B still goes to yfinance
    assert main._LEFTOVER_STALE_STATS == {"served": 0, "unpriced": 0}


@needs_main
def test_held_symbol_and_index_keep_the_old_path(bulk):
    cache, fb, st = bulk
    names = _names(30)
    st["dhan_prices"] = {n: 50.0 for n in names[:28]}
    st["held"] = {"S28"}
    out = main._get_quotes_bulk_core(_breq(*names[:29], "^NSEI"), {})
    assert len(st["yf"]) == 1
    sent = st["yf"][0].split()
    assert "S28.NS" in sent and "^NSEI" in sent


@needs_main
def test_off_switch_restores_the_yfinance_path(bulk, monkeypatch):
    cache, fb, st = bulk
    monkeypatch.setenv("QUOTE_BULK_SMALL_LEFTOVER_SKIP", "0")
    names = _names(20)
    st["dhan_prices"] = {n: 50.0 for n in names[:19]}
    fb["quote:S19.NS"] = _row("S19", 77.0, age_s=10)
    main._get_quotes_bulk_core(_breq(*names), {})
    assert len(st["yf"]) == 1


@needs_main
def test_stale_helper_never_raises_and_never_double_tags(monkeypatch):
    monkeypatch.setattr(main, "_cache_get", lambda k: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(main, "_fallback_get", lambda k: _row("T", 9.0, age_s=5, source="stale_leftover(dhan)"))
    r = main._leftover_stale_row("T.NS", "T", 180.0)
    assert r["source"] == "stale_leftover(dhan)"
    monkeypatch.setattr(main, "_fallback_get", lambda k: 1 / 0)
    assert main._leftover_stale_row("T.NS", "T", 180.0) is None


# ───────────── part 2: a yfinance timeout is not a rate limit ─────────────
def _run_with_yf_error(bulk, monkeypatch, exc):
    cache, fb, st = bulk

    def _yf(*a, **k):
        raise exc

    monkeypatch.setattr(main.yf, "download", _yf)
    return main._get_quotes_bulk_core(_breq("AAA", "BBB"), {}), st


@needs_main
def test_hard_timeout_is_not_reported_as_a_rate_limit(bulk, monkeypatch):
    out, st = _run_with_yf_error(bulk, monkeypatch, TimeoutError("yfinance call exceeded 18s hard timeout"))
    assert st["reported"] == []
    assert out["degraded"] is True and "TimeoutError" in out["error"]


@needs_main
def test_open_circuit_is_not_reported_as_a_rate_limit(bulk, monkeypatch):
    out, st = _run_with_yf_error(bulk, monkeypatch, RuntimeError("yfinance circuit open; retry in 20s"))
    assert st["reported"] == [] and out["degraded"] is True


@needs_main
def test_real_429_is_still_reported(bulk, monkeypatch):
    out, st = _run_with_yf_error(bulk, monkeypatch, RuntimeError("HTTP Error 429: Too Many Requests"))
    assert st["reported"] == [429]


@needs_main
def test_other_failures_still_report_502(bulk, monkeypatch):
    out, st = _run_with_yf_error(bulk, monkeypatch, RuntimeError("boom"))
    assert st["reported"] == [502]


@needs_main
def test_symbol_missing_from_dhans_scrip_map_keeps_the_yfinance_path(bulk):
    cache, fb, st = bulk
    names = _names(30)
    st["dhan_prices"] = {n: 50.0 for n in names[:28]}
    st["unmapped"] = {"S29"}                                   # only yfinance can price it
    out = main._get_quotes_bulk_core(_breq(*names), {})
    assert len(st["yf"]) == 1 and st["yf"][0].split() == ["S29.NS"]
    assert main._LEFTOVER_STALE_STATS == {"served": 0, "unpriced": 1}      # S28 (mapped, unanswered) was dropped


@needs_main
def test_unmapped_symbol_with_a_recent_cached_row_is_still_served_from_cache(bulk):
    cache, fb, st = bulk
    names = _names(20)
    st["dhan_prices"] = {n: 50.0 for n in names[:19]}
    st["unmapped"] = {"S19"}
    fb["quote:S19.NS"] = _row("S19", 8.0, age_s=40)
    out = main._get_quotes_bulk_core(_breq(*names), {})
    assert st["yf"] == []
    assert {q["symbol"]: q["price"] for q in out["quotes"]}["S19"] == 8.0


@needs_main
def test_dhan_can_price_fails_safe_to_false(monkeypatch):
    import dhan_data.scrip_master as dsm
    monkeypatch.setattr(dsm, "security_id", lambda s: ("NSE_EQ", 1) if s == "TCS" else None)
    assert main._dhan_can_price("TCS") is True
    assert main._dhan_can_price("NOPE") is False
    monkeypatch.setattr(dsm, "security_id", lambda s: 1 / 0)
    assert main._dhan_can_price("TCS") is False
