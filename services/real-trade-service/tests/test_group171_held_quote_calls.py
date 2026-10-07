"""
group171 (item 3 of the 2026-10-06 list): fewer market-data calls for held symbols.

The exit cycle priced each open position with GET /live-quote and then GET /quote (~10 requests per cycle for
5 positions, from four callers), and the large non-priority batch sent every symbol bulk could not price through
the same two-call cascade. Pinned here:
  * priority lane is bulk-first: one POST /quotes/bulk for all held symbols, accepted only when no older than
    FEED_PRIORITY_BULK_MAX_AGE_S; the per-symbol cascade runs only for symbols bulk did not price
  * a failed bulk-first call pauses bulk-first for FEED_PRIORITY_BULK_COOLDOWN_S, then it is tried again
  * FEED_PRIORITY_BULK_FIRST=0 restores the old order
  * ticks priced by a priority call are shared for FEED_PRIORITY_SHARE_S between callers (and across spellings)
  * large non-priority batches: bulk leftovers skip /live-quote (FEED_LEFTOVER_SKIP_LIVE=0 restores it), unless
    bulk itself failed; the log line says why bulk left symbols unpriced
  * get_quote(skip_live_quote=True) never calls /live-quote
Everything upstream is faked (no sockets), so these run in any environment where market_feed.feed imports.
"""
import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import market_feed.feed as f  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _tick(sym, price=50.0, source="t"):
    return f.Tick(symbol=sym, price=price, as_of=datetime.now(timezone.utc), atr=None, source=source)


class _Dummy:
    """Stands in for httpx.AsyncClient so no connection pool is ever opened."""

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@pytest.fixture()
def env(monkeypatch):
    f.clear_priority_share()
    f.clear_dead_symbols()
    monkeypatch.setattr(f.httpx, "AsyncClient", _Dummy)
    monkeypatch.setattr(f, "FEED_PRIORITY_BULK_FIRST", True)
    monkeypatch.setattr(f, "FEED_PRIORITY_BULK_MAX_AGE_S", 10.0)
    monkeypatch.setattr(f, "FEED_PRIORITY_BULK_TIMEOUT_S", 4.0)
    monkeypatch.setattr(f, "FEED_PRIORITY_BULK_COOLDOWN_S", 30.0)
    monkeypatch.setattr(f, "FEED_PRIORITY_SHARE_S", 3.0)
    monkeypatch.setattr(f, "FEED_LEFTOVER_SKIP_LIVE", True)
    calls = {"bulk": [], "single": []}

    async def fake_bulk(client, symbols, *, timeout=None, max_age_s=None, stats=None):
        calls["bulk"].append({"symbols": list(symbols), "timeout": timeout, "max_age_s": max_age_s})
        if stats is not None:
            stats.setdefault("failed", 0)
            stats.setdefault("reasons", {})
        miss = calls.get("bulk_miss", set())
        if calls.get("bulk_fail"):
            if stats is not None:
                stats["failed"] += 1
            return {}
        return {f._clean_sym(s): _tick(f._clean_sym(s), 77.0, "bulk(t)") for s in symbols if s not in miss}

    async def fake_get_quote(client, symbol, **kw):
        calls["single"].append((symbol, kw))
        if symbol in calls.get("single_miss", set()):
            return None
        return _tick(symbol, 50.0, "single")

    monkeypatch.setattr(f, "_bulk_ticks", fake_bulk)
    monkeypatch.setattr(f, "get_quote", fake_get_quote)
    yield calls
    f.clear_priority_share()


# ── priority lane: bulk-first ───────────────────────────────────────────────

def test_priority_lane_prices_held_symbols_with_one_bulk_request(env):
    out = _run(f.get_quotes(["A", "B", "C", "D", "E"], priority=True))
    assert set(out) == {"A", "B", "C", "D", "E"}
    assert len(env["bulk"]) == 1 and env["bulk"][0]["symbols"] == ["A", "B", "C", "D", "E"]
    assert env["single"] == []                       # no /live-quote or /quote at all
    assert out["A"].price == 77.0


def test_priority_bulk_first_uses_the_tight_age_limit_and_timeout(env):
    _run(f.get_quotes(["A"], priority=True))
    assert env["bulk"][0]["max_age_s"] == 10.0
    assert env["bulk"][0]["timeout"] == 4.0


def test_symbols_bulk_could_not_price_use_the_per_symbol_cascade(env):
    env["bulk_miss"] = {"B"}
    out = _run(f.get_quotes(["A", "B"], priority=True))
    assert out["A"].source == "bulk(t)" and out["B"].source == "single"
    assert [s for s, _ in env["single"]] == ["B"]
    assert env["single"][0][1]["timeout_scale"] == f.FEED_PRIORITY_TIMEOUT_SCALE
    assert len(env["bulk"]) == 1


def test_priority_still_returns_what_it_can_when_everything_fails(env):
    env["bulk_fail"] = True
    env["single_miss"] = {"A", "B"}
    out = _run(f.get_quotes(["A", "B"], priority=True))
    assert out == {}
    # bulk-first, then the per-symbol cascade, then the scaled bulk fallback
    assert len(env["bulk"]) == 2 and len(env["single"]) == 2


def test_priority_fallback_bulk_recovers_symbols_the_cascade_missed(env, monkeypatch):
    # first bulk answer skips A, the per-symbol path misses A too, the last-resort bulk can price it
    env["bulk_miss"] = {"A"}
    env["single_miss"] = {"A"}
    orig = f._bulk_ticks

    async def second_bulk_prices_everything(client, symbols, **kw):
        if len(env["bulk"]) >= 1:
            env["bulk_miss"] = set()
        return await orig(client, symbols, **kw)

    monkeypatch.setattr(f, "_bulk_ticks", second_bulk_prices_everything)
    out = _run(f.get_quotes(["A"], priority=True))
    assert out["A"].price == 77.0
    assert len(env["bulk"]) == 2 and [s for s, _ in env["single"]] == ["A"]


def test_bulk_first_result_is_mapped_back_to_the_requested_spelling(env):
    out = _run(f.get_quotes(["marine.NS"], priority=True))
    assert list(out) == ["marine.NS"] and out["marine.NS"].price == 77.0
    assert env["bulk"][0]["symbols"] == ["MARINE"]


def test_bulk_first_can_be_switched_off(env, monkeypatch):
    monkeypatch.setattr(f, "FEED_PRIORITY_BULK_FIRST", False)
    out = _run(f.get_quotes(["A", "B"], priority=True))
    assert {s for s, _ in env["single"]} == {"A", "B"}
    assert env["bulk"] == []
    assert all(t.source == "single" for t in out.values())


# ── bulk-first cool-down ────────────────────────────────────────────────────

def test_failed_bulk_first_is_not_repeated_until_the_cooldown_ends(env, monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(f._time, "monotonic", lambda: t[0])
    f.clear_priority_share()
    monkeypatch.setattr(f, "FEED_PRIORITY_SHARE_S", 0.0)          # keep the share cache out of this test
    env["bulk_fail"] = True
    out = _run(f.get_quotes(["A"], priority=True))
    assert out["A"].source == "single"
    assert len(env["bulk"]) == 1                      # the failed bulk-first call
    _run(f.get_quotes(["A"], priority=True))
    assert len(env["bulk"]) == 1                      # paused: no second bulk-first call
    t[0] += 31
    env["bulk_fail"] = False
    out = _run(f.get_quotes(["A"], priority=True))
    assert len(env["bulk"]) == 2 and out["A"].source == "bulk(t)"


def test_a_partial_bulk_answer_does_not_start_the_cooldown(env, monkeypatch):
    monkeypatch.setattr(f, "FEED_PRIORITY_SHARE_S", 0.0)
    env["bulk_miss"] = {"B"}
    _run(f.get_quotes(["A", "B"], priority=True))
    _run(f.get_quotes(["A", "B"], priority=True))
    assert len(env["bulk"]) == 2


# ── share cache ─────────────────────────────────────────────────────────────

def test_second_priority_call_inside_the_share_window_makes_no_request(env, monkeypatch):
    t = [500.0]
    monkeypatch.setattr(f._time, "monotonic", lambda: t[0])
    first = _run(f.get_quotes(["A", "B"], priority=True))
    t[0] += 2.0
    second = _run(f.get_quotes(["A", "B"], priority=True))
    assert len(env["bulk"]) == 1 and env["single"] == []
    assert second["A"] is first["A"]                  # same Tick, original as_of kept


def test_share_window_expires(env, monkeypatch):
    t = [500.0]
    monkeypatch.setattr(f._time, "monotonic", lambda: t[0])
    _run(f.get_quotes(["A"], priority=True))
    t[0] += 3.5
    _run(f.get_quotes(["A"], priority=True))
    assert len(env["bulk"]) == 2


def test_only_the_unshared_symbols_are_requested(env, monkeypatch):
    t = [500.0]
    monkeypatch.setattr(f._time, "monotonic", lambda: t[0])
    _run(f.get_quotes(["A"], priority=True))
    t[0] += 1.0
    out = _run(f.get_quotes(["A", "B"], priority=True))
    assert env["bulk"][1]["symbols"] == ["B"]
    assert set(out) == {"A", "B"}


def test_share_cache_matches_any_spelling(env, monkeypatch):
    t = [500.0]
    monkeypatch.setattr(f._time, "monotonic", lambda: t[0])
    _run(f.get_quotes(["KOTAKBANK"], priority=True))
    out = _run(f.get_quotes(["kotakbank.ns"], priority=True))
    assert len(env["bulk"]) == 1 and list(out) == ["kotakbank.ns"]


def test_share_cache_can_be_switched_off(env, monkeypatch):
    monkeypatch.setattr(f, "FEED_PRIORITY_SHARE_S", 0.0)
    _run(f.get_quotes(["A"], priority=True))
    _run(f.get_quotes(["A"], priority=True))
    assert len(env["bulk"]) == 2


def test_symbols_nobody_could_price_are_not_shared(env):
    env["bulk_miss"] = {"A"}
    env["single_miss"] = {"A"}
    _run(f.get_quotes(["A"], priority=True))
    assert f._prio_shared_lookup(["A"]) == {}


def test_non_priority_calls_do_not_read_or_fill_the_share_cache(env):
    syms = [f"S{i}" for i in range(3)]
    _run(f.get_quotes(syms))                          # small batch: per-symbol path
    assert f._prio_shared_lookup(syms) == {}


def test_share_cache_is_bounded(env):
    f._prio_shared_store({f"S{i}": _tick(f"S{i}") for i in range(600)})
    assert len(f._PRIO_SHARED) == 600
    f._prio_shared_store({"LAST": _tick("LAST")})        # over 500 entries: dropped, then LAST stored
    assert list(f._PRIO_SHARED) == ["LAST"]


# ── non-priority leftovers skip /live-quote ─────────────────────────────────

def _big(n=40):
    return [f"S{i}" for i in range(n)]


def test_bulk_leftovers_skip_live_quote(env):
    env["bulk_miss"] = {"S3", "S4"}
    out = _run(f.get_quotes(_big()))
    assert len(out) == 40
    assert {s for s, _ in env["single"]} == {"S3", "S4"}
    assert all(kw.get("skip_live_quote") is True for _, kw in env["single"])


def test_leftover_skip_can_be_switched_off(env, monkeypatch):
    monkeypatch.setattr(f, "FEED_LEFTOVER_SKIP_LIVE", False)
    env["bulk_miss"] = {"S3"}
    _run(f.get_quotes(_big()))
    assert env["single"][0][1] == {}                   # plain get_quote(client, sym)


def test_small_batches_keep_the_full_cascade(env):
    _run(f.get_quotes(["A", "B", "C"]))
    assert all(kw == {} for _, kw in env["single"]) and len(env["single"]) == 3


def test_failed_bulk_does_not_skip_live_quote(env):
    env["bulk_fail"] = True

    async def failing_bulk(client, symbols, *, timeout=None, max_age_s=None, stats=None):
        stats["failed"] = 1
        stats["chunks"] = 1
        stats["reasons"] = {}
        return {}
    orig = f._bulk_ticks
    f._bulk_ticks = failing_bulk
    try:
        out = _run(f.get_quotes(_big(30)))
    finally:
        f._bulk_ticks = orig
    assert len(out) == 30
    assert all(kw == {} for _, kw in env["single"])


def test_log_line_explains_why_bulk_left_symbols(env, caplog):
    async def reasoned_bulk(client, symbols, *, timeout=None, max_age_s=None, stats=None):
        stats["chunks"] = 1
        stats["failed"] = 0
        stats["reasons"] = {"stale": 3, "no_price": 2}
        return {f._clean_sym(s): _tick(f._clean_sym(s)) for s in symbols[:-7]}
    orig = f._bulk_ticks
    f._bulk_ticks = reasoned_bulk
    try:
        with caplog.at_level(logging.INFO, logger=f.logger.name):
            _run(f.get_quotes(_big(40)))
    finally:
        f._bulk_ticks = orig
    line = next(r.getMessage() for r in caplog.records if "bulk-first priced" in r.getMessage())
    assert "33/40" in line and "7 left" in line
    assert "older than limit 3" in line and "no price 2" in line and "not in the answer 2" in line


# ── helpers ─────────────────────────────────────────────────────────────────

def _iso(age_s):
    return (datetime.now(timezone.utc) - timedelta(seconds=age_s)).replace(tzinfo=None).isoformat()


def test_tick_from_bulk_item_honours_a_per_call_age_limit():
    item = {"symbol": "A", "price": 10, "fetched_at": _iso(8)}
    assert f._tick_from_bulk_item(item) is not None                  # default 20 s
    assert f._tick_from_bulk_item(item, max_age_s=5.0) is None
    assert f._tick_from_bulk_item(item, max_age_s=10.0) is not None


def test_bulk_reject_reason_names_the_cause():
    assert f._bulk_reject_reason({"symbol": "A", "price": None, "fetched_at": _iso(0)}) == "no_price"
    assert f._bulk_reject_reason({"symbol": "A", "price": "x", "fetched_at": _iso(0)}) == "no_price"
    assert f._bulk_reject_reason({"symbol": "A", "price": 5}) == "no_time"
    assert f._bulk_reject_reason({"symbol": "A", "price": 5, "fetched_at": _iso(300)}) == "stale"
    assert f._bulk_reject_reason({"symbol": "A", "price": 5, "fetched_at": _iso(1)}) == "other"
    assert f._bulk_reject_reason("nope") == "other"
    assert f._bulk_reject_reason({"symbol": "A", "price": 5, "fetched_at": _iso(300)}, max_age_s=1000) == "other"


class _Resp:
    def __init__(self, code, body):
        self.status_code = code
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


class _PostClient:
    def __init__(self, handler):
        self.handler = handler
        self.posts = []

    async def post(self, url, json=None, timeout=None):
        self.posts.append((url, json, timeout))
        return self.handler(json)


def test_bulk_ticks_fills_stats_for_failed_chunks_and_rejected_rows(monkeypatch):
    monkeypatch.setattr(f, "FEED_BULK_CHUNK_SIZE", 2)
    monkeypatch.setattr(f, "_schedule_atr_refresh", lambda *a, **k: False)

    def handler(body):
        syms = body["symbols"]
        if "C" in syms:
            return _Resp(500, {"detail": "down"})
        return _Resp(200, {"quotes": [
            {"symbol": "A", "price": 10, "fetched_at": _iso(0)},
            {"symbol": "B", "price": 10, "fetched_at": _iso(60)},
        ]})
    client = _PostClient(handler)
    stats = {}
    out = _run(f._bulk_ticks(client, ["A", "B", "C", "D"], stats=stats, max_age_s=20.0))
    assert set(out) == {"A"}
    assert stats["chunks"] == 2 and stats["failed"] == 1
    assert stats["reasons"] == {"stale": 1}


def test_bulk_ticks_counts_an_exception_as_a_failed_chunk(monkeypatch):
    def handler(body):
        raise RuntimeError("boom")
    stats = {}
    out = _run(f._bulk_ticks(_PostClient(handler), ["A"], stats=stats))
    assert out == {} and stats["failed"] == 1


def test_bulk_ticks_without_stats_still_works(monkeypatch):
    monkeypatch.setattr(f, "_schedule_atr_refresh", lambda *a, **k: False)
    client = _PostClient(lambda body: _Resp(200, {"quotes": [{"symbol": "A", "price": 3, "fetched_at": _iso(0)}]}))
    out = _run(f._bulk_ticks(client, ["A"]))
    assert out["A"].price == 3.0
    assert client.posts[0][2] == f.FEED_BULK_TIMEOUT_S


# ── get_quote(skip_live_quote=True) ─────────────────────────────────────────

class _GetClient:
    def __init__(self):
        self.urls = []

    async def get(self, url, timeout=None):
        self.urls.append(url)
        if "/live-quote/" in url:
            return _Resp(200, {"ltp": 99.0, "updated_at": datetime.now(timezone.utc).isoformat(), "source": "angelone"})
        return _Resp(200, {"price": 55.0, "source": "yf"})


def test_get_quote_skip_live_quote_goes_straight_to_quote(monkeypatch):
    monkeypatch.setattr(f, "_schedule_atr_refresh", lambda *a, **k: False)
    c = _GetClient()
    t = _run(f.get_quote(c, "ABC", skip_live_quote=True))
    assert t.price == 55.0
    assert len(c.urls) == 1 and "/quote/ABC" in c.urls[0]


def test_get_quote_default_still_tries_live_quote_first(monkeypatch):
    monkeypatch.setattr(f, "_schedule_atr_refresh", lambda *a, **k: False)
    c = _GetClient()
    t = _run(f.get_quote(c, "ABC"))
    assert t.price == 99.0 and "/live-quote/ABC" in c.urls[0]


# ── env parsing ─────────────────────────────────────────────────────────────

def test_env_float_and_env_on(monkeypatch):
    monkeypatch.setenv("G171_X", " 2.5 ")
    assert f._env_float("G171_X", 1.0) == 2.5
    monkeypatch.setenv("G171_X", "abc")
    assert f._env_float("G171_X", 1.0) == 1.0
    monkeypatch.setenv("G171_X", "-3")
    assert f._env_float("G171_X", 1.0) == 1.0
    monkeypatch.setenv("G171_X", "")
    assert f._env_float("G171_X", 4.0) == 4.0
    monkeypatch.delenv("G171_X")
    assert f._env_on("G171_X") is True
    for off in ("0", "false", "False", "off", "no", " 0 "):
        monkeypatch.setenv("G171_X", off)
        assert f._env_on("G171_X") is False
    monkeypatch.setenv("G171_X", "1")
    assert f._env_on("G171_X") is True


def test_defaults_match_the_documented_values():
    src = open(os.path.join(os.path.dirname(__file__), "..", "market_feed", "feed.py")).read()
    for needle in ('_env_on("FEED_PRIORITY_BULK_FIRST")', '"FEED_PRIORITY_BULK_MAX_AGE_S", 10.0',
                   '"FEED_PRIORITY_BULK_TIMEOUT_S", 4.0', '"FEED_PRIORITY_BULK_COOLDOWN_S", 30.0',
                   '"FEED_PRIORITY_SHARE_S", 3.0', '_env_on("FEED_LEFTOVER_SKIP_LIVE")'):
        assert needle in src
