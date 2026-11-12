"""E494: Hot Picks / Premarket jobs, the Surprise stream + notify routes, `/market/trending` and `/data-feed/run`
(`gateway_support/bound_routes_j.py`, `gateway_support/bound_routes_k.py`, `main.py`).

1. A process that died mid-run (OOM kill, SIGKILL after a hung stop, a deploy) left the Hot Picks / Premarket job record at `running` in the durable
   kv (7 days for the premarket one), and both routes answer `already_running` while it says so: neither button could start a run again for hours
   or days. `_heal_stuck_hot_jobs_on_boot` (startup hook) marks any `running` record `error` ("interrupted"); no run can be in progress at boot.
2. `stockky_hot_premarket`'s AngelOne overlay wrote an infinite quote price (`"inf"` passed `float(x) > 0`) into the feed row as price / ltp / close,
   and one non-dict entry in a quotes reply raised and dropped the rest of its 50-symbol chunk.
3. `api_hotpicks_notify_top_picks` sorted on `-(score or 0)`: a text score made the "Send Top 5 to Telegram" button answer 500; a NaN price printed
   "₹nan"; a non-dict pick raised on `.get`.
4. `stockky_hot_run` deleted the hot-stocks cache key on the event loop (it can be a durable DB call); it now goes through `asyncio.to_thread`.
5. `api_surprise_scan_stream`: a failing static-cache load, one failing `score_stock` or a NaN score cut / corrupted the NDJSON stream (no `done` line,
   or a line the browser's JSON.parse rejects).
6. `api_surprise_notify_top_picks`: a non-dict pick raised on `.get`; NaN / inf price, change, target printed "₹nan" / "+nan%".
7. `market_trending`: `list(set(...))` lost the movers' ranking so `[:10]` kept a random ten on every call; an infinite price became a 500 at
   response time; a text `day_change_pct` left a text change in the row.
8. `data_feed_run`'s per-symbol loop called `store.job()` / `store.set_job()` (durable reads / writes) straight from the event loop.

Run from services/api-gateway:  python3 -m pytest tests/test_e494_hot_jobs_surprise_stream_and_trending.py -v"""
from __future__ import annotations

import asyncio
import json
import math
import sys
import threading
import types

import pytest

from test_main_hot_premarket import hp, run_worker, quote, FakeResp, gw   # noqa: F401  (hp is a fixture)
from test_main_data_feed_run import fr, ok_routes, seq, start, execute   # noqa: F401  (fr is a fixture)
import httpx
from fastapi import BackgroundTasks


def _run(coro):
    return asyncio.run(coro)


# ── 1. boot heal of dead hot jobs ────────────────────────────────────────────

@pytest.fixture
def jobs(monkeypatch):
    j = types.SimpleNamespace(hot={"status": "running", "processed": 5}, pm={"status": "running"}, sets=[], raises=set())

    def mk(name, store_attr):
        def get(_g):
            if name in j.raises:
                raise RuntimeError("db down")
            return dict(getattr(j, store_attr))

        def set_(_s, _g, **kw):
            if name in j.raises:
                raise RuntimeError("db down")
            j.sets.append((name, kw))
            setattr(j, store_attr, {**getattr(j, store_attr), **kw})
        return get, set_

    hg, hs = mk("hot", "hot")
    pg, ps = mk("premarket", "pm")
    monkeypatch.setattr(gw, "hot_job_get", hg)
    monkeypatch.setattr(gw, "hot_job_set", hs)
    monkeypatch.setattr(gw, "hot_premarket_job_get", pg)
    monkeypatch.setattr(gw, "hot_premarket_job_set", ps)
    return j


def test_boot_heal_marks_both_dead_running_jobs_as_error(jobs):
    out = gw._heal_stuck_hot_jobs_sync()
    assert out == {"hot": True, "premarket": True}
    assert jobs.hot["status"] == "error" and jobs.pm["status"] == "error"
    assert "restart" in jobs.hot["message"] and jobs.hot["current_symbol"] is None and jobs.hot["finished_at"]
    assert jobs.hot["processed"] == 5                                   # counters kept, only the state changes


def test_boot_heal_leaves_finished_and_idle_jobs_alone(jobs):
    jobs.hot, jobs.pm = {"status": "done"}, {"status": "idle"}
    assert gw._heal_stuck_hot_jobs_sync() == {"hot": False, "premarket": False}
    assert jobs.sets == []


def test_one_failing_record_does_not_skip_the_other(jobs):
    jobs.raises = {"hot"}
    assert gw._heal_stuck_hot_jobs_sync() == {"hot": False, "premarket": True}
    assert jobs.pm["status"] == "error"


def test_boot_heal_is_a_registered_startup_hook_and_never_raises(jobs):
    assert gw._heal_stuck_hot_jobs_on_boot in gw._STARTUP_HOOKS
    jobs.raises = {"hot", "premarket"}
    _run(gw._heal_stuck_hot_jobs_on_boot())                             # a down DB is logged, boot continues


def test_healed_job_can_be_started_again(hp):
    # the guard in the premarket route is `status == "running"`: a dead "running" record blocks it, the boot heal frees it
    hp.pm_job = {"status": "running"}
    assert _run(gw.stockky_hot_premarket(BackgroundTasks())).get("already_running") is True
    assert gw._heal_stuck_hot_jobs_sync()["premarket"] is True and hp.pm_job["status"] == "error"
    out = _run(gw.stockky_hot_premarket(BackgroundTasks()))
    assert out["started"] is True


# ── 2. premarket AngelOne overlay ────────────────────────────────────────────

def test_an_infinite_quote_price_is_never_written_to_the_feed_row(hp):
    hp.post_handler = lambda u, j, t: FakeResp(200, {"quotes": [
        quote("AAA", price=float("inf"), ltp=101.5), quote("BBB", price=float("inf")), quote("CCC", price=float("nan"))]})
    run_worker(hp, phase="open", universe=["AAA", "BBB", "CCC"])
    rows = {s: r for s, r, _ in hp.store.puts}
    assert set(rows) == {"AAA"} and rows["AAA"]["price"] == 101.5 and rows["AAA"]["close"] == 101.5   # next finite key wins; no finite key = no write
    assert all(math.isfinite(r["price"]) and math.isfinite(r["ltp"]) and math.isfinite(r["close"]) for r in rows.values())


def test_a_non_dict_quote_entry_no_longer_drops_the_rest_of_the_chunk(hp):
    hp.post_handler = lambda u, j, t: FakeResp(200, {"quotes": ["junk", None, 5, quote("AAA"), quote("BBB")]})
    run_worker(hp, phase="open")
    assert [s for s, _, _ in hp.store.puts] == ["AAA", "BBB"]
    assert hp.pm_sets[-1]["status"] == "done"


# ── 3. hot picks notify ──────────────────────────────────────────────────────

class _Resp:
    status_code = 200

    def json(self):
        return {"delivered": True}


@pytest.fixture
def posts(monkeypatch):
    sent = []
    monkeypatch.setattr(httpx, "post", lambda url, json=None, timeout=None: sent.append(json) or _Resp())
    monkeypatch.setattr(gw, "NOTIFICATION_URL", "http://n")
    return sent


def _hot_notify(monkeypatch, cache, top_n=5):
    monkeypatch.setattr(gw, "_redis_get", lambda k: cache)
    return _run(gw.api_hotpicks_notify_top_picks(top_n=top_n))


def test_text_and_nan_scores_no_longer_make_the_hot_picks_button_answer_500(monkeypatch, posts):
    cache = {"news_driven": [
        {"symbol": "AAA", "decision": "BUY NOW", "score": "high", "price": float("nan"), "close": 101.5, "section": "news_driven"},
        {"symbol": "BBB", "decision": "BUY NOW", "score": 70, "price": 250.0, "section": "news_driven"},
        "junk",
        {"symbol": "CCC", "decision": "BUY NOW", "score": float("nan"), "price": float("inf"), "section": "news_driven"},
        {"symbol": "DDD", "decision": None, "score": 99, "price": 10.0, "section": "news_driven"},
    ]}
    out = _hot_notify(monkeypatch, cache)
    assert out["ok"] is True and out["sent"] is True
    assert out["symbols"] == ["BBB", "AAA", "CCC", "DDD"]               # BUY NOW by real score (text / NaN = 0), then the rest; junk dropped
    msg = posts[0]["message"]
    low = msg.lower()
    assert "nan" not in low and "inf" not in low
    assert "AAA — BUY NOW (score high/100)" in msg and "₹101.5" in msg   # NaN price falls through to close
    assert "CCC" in msg and "price n/a" in msg


def test_good_hot_picks_keep_their_format_and_order(monkeypatch, posts):
    cache = {"results_driven": [
        {"symbol": "S1", "decision": "PREPARE TO BUY", "score": 90, "price": 100, "section": "results_driven"},
        {"symbol": "S2", "decision": "BUY NOW", "score": 60, "price": 55.5, "section": "results_driven"}]}
    out = _hot_notify(monkeypatch, cache)
    assert out["symbols"] == ["S2", "S1"]
    assert "S2 — BUY NOW (score 60/100)\n   ₹55.5 · results driven" in posts[0]["message"]


def test_a_cache_of_only_non_dict_entries_is_no_picks_not_a_crash(monkeypatch, posts):
    out = _hot_notify(monkeypatch, {"news_driven": ["junk", None]})
    assert out["ok"] is False and out["sent"] is False and posts == []


# ── 4. hot run cache delete off the event loop ───────────────────────────────

def test_forced_hot_run_deletes_its_cache_key_off_the_event_loop(monkeypatch):
    seen = {}

    class _Redis:
        def delete(self, key):
            seen["key"], seen["main"] = key, threading.current_thread() is threading.main_thread()

    async def hot_stocks(force=True, progress_cb=None):
        return {"universe_size": 1, "processed_symbols": 1, "news_driven": []}

    jobs = []
    monkeypatch.setitem(sys.modules, "hotpicks_store", types.SimpleNamespace(
        clear_hotpicks_stop=lambda: None, ensure_hotpicks_schema=lambda: None))
    monkeypatch.setattr(gw, "_redis", _Redis())
    monkeypatch.setattr(gw, "_redis_set", lambda *a, **k: None)
    monkeypatch.setattr(gw, "hot_job_get", lambda g: {"status": "idle"})
    monkeypatch.setattr(gw, "hot_job_set", lambda s, g, **kw: jobs.append(kw))
    monkeypatch.setattr(gw, "stockky_hot_stocks", hot_stocks)
    bt = BackgroundTasks()
    assert _run(gw.stockky_hot_run(bt, force=True))["started"] is True
    t = bt.tasks[0]
    _run(t.func(*t.args, **t.kwargs))
    assert seen["key"] == gw.HOT_STOCKS_CACHE_KEY and seen["main"] is False
    assert jobs[-1]["status"] == "done"


# ── 5. surprise stream ───────────────────────────────────────────────────────

def _strict(line):
    def bad(c):
        raise ValueError(f"non-JSON constant {c}")
    return json.loads(line, parse_constant=bad)


@pytest.fixture
def engine(monkeypatch):
    import surprise_scanner

    class Eng:
        static_cache = {"AAA": {"is_liquid": True}, "BBB": {"is_liquid": True}, "CCC": {"is_liquid": True}, "DDD": {"is_liquid": True}}
        load_raises = None
        scan_result = {}

        def load_static_cache(self, force=False):
            if self.load_raises:
                raise self.load_raises
            return len(self.static_cache)

        async def prime_bulk_ticks(self, client, url, keys):
            return 0

        async def _fetch_quote(self, client, url, sym):
            return "not-a-dict" if sym == "DDD" else {"price": 100.0}

        def score_stock(self, sym, tick):
            if sym == "BBB":
                raise RuntimeError("bad tick")
            return {"symbol": sym, "score": float("nan") if sym == "CCC" else 80}

        async def scan(self, **kw):
            return self.scan_result

    e = Eng()
    monkeypatch.setattr(surprise_scanner, "surprise_engine", e)
    monkeypatch.setattr(gw, "_get_http_client", lambda: object())
    return e


def _stream(**kw):
    async def go():
        resp = await gw.api_surprise_scan_stream(force_reload=False, symbols=None, **kw)
        return [c if isinstance(c, str) else c.decode() async for c in resp.body_iterator]
    return "".join(_run(go())).splitlines()


def test_one_bad_symbol_or_nan_score_no_longer_breaks_the_stream(engine):
    lines = _stream()
    rows = [_strict(l) for l in lines]                                   # every line is strict JSON (NaN became null)
    stocks = [r for r in rows if not r.get("_meta")]
    assert [s["symbol"] for s in stocks] == ["AAA", "CCC"]               # BBB's scoring error and DDD's non-dict tick are skipped
    assert stocks[1]["score"] is None
    done = rows[-1]
    assert done["event"] == "done" and done["hits"] == 2 and done["universe"] == 4 and done["quotes_ok"] == 3 and done["quotes_failed"] == 1


def test_a_failing_static_cache_load_ends_the_stream_with_error_and_done(engine):
    engine.load_raises = RuntimeError("db down")
    rows = [_strict(l) for l in _stream()]
    assert [r["event"] for r in rows] == ["error", "done"] and "db down" in rows[0]["error"]


# ── 6. surprise notify ───────────────────────────────────────────────────────

def test_surprise_notify_skips_non_dict_picks_and_never_prints_nan(engine, monkeypatch, posts):
    engine.scan_result = {"stocks": ["junk", None, {
        "symbol": "AAA", "score": 80, "tier": "a", "price": float("nan"), "change_pct": float("inf"),
        "target_1": float("nan"), "trailing_stop": 95.5, "trigger_type": "vol"}]}
    out = _run(gw.api_surprise_notify_top_picks(top_n=5))
    assert out["ok"] is True and out["sent"] is True and out["symbols"] == ["AAA"]
    msg = posts[0]["message"]
    assert "nan" not in msg.lower() and "inf" not in msg.lower() and "Stop ₹95.5" in msg


def test_surprise_notify_with_only_junk_is_no_picks(engine, posts):
    engine.scan_result = {"stocks": ["junk"]}
    out = _run(gw.api_surprise_notify_top_picks(top_n=5))
    assert out["ok"] is False and out["sent"] is False and posts == []


def test_surprise_notify_keeps_the_good_format(engine, posts):
    engine.scan_result = {"stocks": [{"symbol": "AAA", "score": 80, "tier": "a", "price": 100.5, "change_pct": 2.5,
                                      "target_1": 110, "trailing_stop": 95, "trigger_type": "vol"}]}
    _run(gw.api_surprise_notify_top_picks(top_n=5))
    assert "1. AAA — A (score 80/100)\n   ₹100.5 (+2.50%) · Target ₹110 · Stop ₹95\n   vol" in posts[0]["message"]


# ── 7. market trending ───────────────────────────────────────────────────────

def _trending(monkeypatch, movers, news, quotes):
    monkeypatch.setattr(gw, "_get_momentum_movers", lambda: list(movers))
    monkeypatch.setattr(gw, "_get_news_mentioned_symbols", lambda: list(news))
    monkeypatch.setattr(gw, "_trending_yf_row", lambda sym: None)
    monkeypatch.setattr(gw, "MARKET_DATA_URL", "http://md.t")
    monkeypatch.setattr(httpx, "post", lambda url, json=None, timeout=None: FakeResp(200, {"quotes": quotes}))
    return _run(gw.market_trending())


def test_trending_keeps_the_movers_rank_and_is_stable(monkeypatch):
    movers = [f"M{i}" for i in range(12)]
    quotes = [{"symbol": s, "price": 100.0 + i, "previous_close": 99.0} for i, s in enumerate(movers + ["N1"])]
    first = _trending(monkeypatch, movers, ["M3", "N1"], quotes)
    assert [r["symbol"] for r in first["data"]] == movers[:10]           # top ten movers in rank order, news names only after them
    for _ in range(5):                                                   # the same answer on every call (set order used to shuffle it)
        assert _trending(monkeypatch, movers, ["M3", "N1"], quotes) == first


def test_trending_news_names_follow_the_movers_and_duplicates_are_dropped(monkeypatch):
    quotes = [{"symbol": s, "price": 10.0} for s in ("A", "B", "N1")]
    out = _trending(monkeypatch, ["A", "B"], ["B", "N1"], quotes)
    assert [r["symbol"] for r in out["data"]] == ["A", "B", "N1"]


def test_trending_non_finite_price_is_skipped_not_a_response_500(monkeypatch):
    quotes = [{"symbol": "A", "price": float("inf")}, {"symbol": "B", "price": float("nan")}, {"symbol": "C", "price": 50, "previous_close": 40}]
    out = _trending(monkeypatch, ["A", "B", "C"], [], quotes)
    assert [r["symbol"] for r in out["data"]] == ["C"]
    assert out["data"][0]["change"] == 10.0 and out["data"][0]["change_pct"] == 25.0
    json.dumps(out, allow_nan=False)                                     # what the JSON response renders


def test_trending_text_day_change_pct_is_dropped_not_stored(monkeypatch):
    quotes = [{"symbol": "A", "price": 50, "day_change_pct": "abc"}, {"symbol": "B", "price": 50, "day_change_pct": "2"}]
    out = _trending(monkeypatch, ["A", "B"], [], quotes)
    rows = {r["symbol"]: r for r in out["data"]}
    assert rows["A"]["price"] == 50.0 and rows["A"]["change_pct"] is None and rows["A"]["change"] is None
    assert rows["B"]["change_pct"] == 2.0 and rows["B"]["change"] == 1.0


# ── 8. data-feed run: job reads / writes off the event loop ──────────────────

def test_data_feed_loop_job_reads_and_writes_run_off_the_event_loop(fr):
    seq(fr)
    ok_routes(fr, "AAA", "BBB", "CCC")
    out, bt = start(fr)
    main_flags = []
    real_job, real_set = fr.store.job, fr.store.set_job

    def job():
        main_flags.append(threading.current_thread() is threading.main_thread())
        return real_job()

    def set_job(**kw):
        if str(kw.get("message") or "").startswith(("Fed ", "Batch done")):
            main_flags.append(threading.current_thread() is threading.main_thread())
        return real_set(**kw)

    fr.store.job, fr.store.set_job = job, set_job
    execute(bt)
    assert len(main_flags) >= 9 and not any(main_flags)                  # 3 symbols x (superseded check + job read + progress write)
    assert fr.store.job_val["status"] == "done" and [p[0] for p in fr.store.puts] == ["AAA", "BBB", "CCC"]
