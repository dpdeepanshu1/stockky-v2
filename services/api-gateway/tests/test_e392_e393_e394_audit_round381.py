"""Round 381: E392 (price alerts + partial bulk cache), E393 (sync feed reads kept off the event loop), E394 (Hot Picks repair write-back)."""
import ast
import asyncio
import os
import sys
import threading

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
GW = os.path.dirname(HERE)
sys.path.insert(0, GW)

import data_feed as df  # noqa: E402
import hotpicks_store as hs  # noqa: E402


def _src(rel):
    with open(os.path.join(GW, rel), encoding="utf-8") as f:
        return f.read()


# ── E392 ─────────────────────────────────────────────────────────────────────
class _Store:
    def __init__(self, rows):
        self.rows = rows
        self.asked = []

    def get_symbol(self, sym):
        self.asked.append(sym)
        return self.rows.get(sym)


def _alert(aid, sym, target, direction="above", enabled=True):
    return {"id": aid, "symbol": sym, "target_price": target, "direction": direction, "enabled": enabled,
            "last_triggered_at": None, "trigger_count": 0}


@pytest.fixture
def alerts_env(monkeypatch):
    saved = []
    monkeypatch.setattr(df, "save_price_alerts", lambda a: saved.append([dict(x) for x in a]) or a)
    return saved


def test_e392_partial_cache_falls_back_to_the_feed_row_per_symbol(monkeypatch, alerts_env):
    alerts = [_alert("a", "TCS", 100), _alert("b", "INFY", 50, "below")]
    monkeypatch.setattr(df, "list_price_alerts", lambda: alerts)
    monkeypatch.setattr(df, "get_bulk_quote_cache", lambda: {"quotes": {"TCS": {"price": 150}}})   # warm but INFY is missing
    store = _Store({"INFY": {"price": 40}})
    monkeypatch.setattr(df, "get_data_feed_store", lambda: store)
    out = df.evaluate_price_alerts()
    assert {a["id"]: a["current_price"] for a in out} == {"a": 150.0, "b": 40.0}
    assert store.asked == ["INFY"]                                    # TCS came from the cache; only the missing symbol hits the store


def test_e392_cache_covering_every_alert_never_touches_the_store(monkeypatch, alerts_env):
    monkeypatch.setattr(df, "list_price_alerts", lambda: [_alert("a", "TCS", 100)])
    monkeypatch.setattr(df, "get_bulk_quote_cache", lambda: {"quotes": {"TCS": {"price": 150}}})
    monkeypatch.setattr(df, "get_data_feed_store", lambda: pytest.fail("store must not be consulted"))
    assert len(df.evaluate_price_alerts()) == 1


def test_e392_disabled_alerts_do_not_trigger_a_store_lookup_and_duplicates_are_asked_once(monkeypatch, alerts_env):
    alerts = [_alert("a", "ZZ", 1, enabled=False), _alert("b", "YY", 1), _alert("c", "YY", 2)]
    monkeypatch.setattr(df, "list_price_alerts", lambda: alerts)
    monkeypatch.setattr(df, "get_bulk_quote_cache", lambda: {})
    store = _Store({"YY": {"price": 10}, "ZZ": {"price": 10}})
    monkeypatch.setattr(df, "get_data_feed_store", lambda: store)
    out = df.evaluate_price_alerts()
    assert store.asked == ["YY"] and {a["id"] for a in out} == {"b", "c"}


def test_e392_store_failures_are_swallowed(monkeypatch, alerts_env):
    monkeypatch.setattr(df, "list_price_alerts", lambda: [_alert("a", "TCS", 1)])
    monkeypatch.setattr(df, "get_bulk_quote_cache", lambda: {})

    def boom():
        raise RuntimeError("neon down")
    monkeypatch.setattr(df, "get_data_feed_store", boom)
    assert df.evaluate_price_alerts() == []

    class Bad:
        def get_symbol(self, s):
            raise RuntimeError("row read")
    monkeypatch.setattr(df, "get_data_feed_store", lambda: Bad())
    assert df.evaluate_price_alerts() == []


def test_e392_caller_supplied_map_is_used_exactly_as_given(monkeypatch, alerts_env):
    monkeypatch.setattr(df, "list_price_alerts", lambda: [_alert("a", "TCS", 100), _alert("b", "INFY", 1)])
    monkeypatch.setattr(df, "get_bulk_quote_cache", lambda: pytest.fail("cache must not be read"))
    monkeypatch.setattr(df, "get_data_feed_store", lambda: pytest.fail("store must not be read"))
    out = df.evaluate_price_alerts({"TCS": 120.0})
    assert [a["id"] for a in out] == ["a"]


# ── E393 ─────────────────────────────────────────────────────────────────────
def _async_fn(rel, name):
    for n in ast.walk(ast.parse(_src(rel))):
        if isinstance(n, ast.AsyncFunctionDef) and n.name == name:
            return n
    raise AssertionError(f"{name} not found in {rel}")


def _direct_calls(fn, names):
    """Calls to `names` made by fn itself (not inside nested defs and not the reference handed to asyncio.to_thread)."""
    hits = []

    def rec(node):
        for c in ast.iter_child_nodes(node):
            if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(c, ast.Call):
                f = c.func
                nm = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                if nm in ("to_thread", "run_in_executor"):
                    for a in c.args[1:]:
                        rec(a)
                    continue
                if nm in names:
                    hits.append(nm)
            rec(c)
    rec(fn)
    return hits


def test_e393_bulk_cache_route_reads_on_a_worker_thread():
    fn = _async_fn("gateway_routers/feed_jobs.py", "api_bulk_quote_cache")
    assert _direct_calls(fn, {"get_bulk_quote_cache", "bulk_cache_age_sec", "should_refresh_bulk_cache"}) == []
    assert "to_thread" in _src("gateway_routers/feed_jobs.py")


def test_e393_list_alerts_route_reads_on_a_worker_thread():
    fn = _async_fn("gateway_routers/price_alerts.py", "api_list_price_alerts")
    assert _direct_calls(fn, {"list_price_alerts"}) == []
    assert "asyncio.to_thread(list_price_alerts)" in _src("gateway_routers/price_alerts.py")


def test_e393_boot_heal_is_threaded_and_bounded():
    fn = _async_fn("main.py", "_start_shared_http")
    assert _direct_calls(fn, {"clear_stuck_feed_job_on_boot"}) == []
    src = _src("main.py")
    assert "asyncio.wait_for(asyncio.to_thread(clear_stuck_feed_job_on_boot)" in src
    assert "BOOT_HEAL_TIMEOUT_S" in src and "except asyncio.TimeoutError" in src


def test_e393_no_async_handler_calls_the_sync_feed_helpers_directly():
    """Pins the AST sweep that found these three: async handlers in main / gateway_support / gateway_routers go through a thread hop."""
    sync = {"get_bulk_quote_cache", "bulk_cache_age_sec", "should_refresh_bulk_cache", "list_price_alerts", "save_price_alerts",
            "delete_price_alert", "add_price_alert", "evaluate_price_alerts", "list_feed_symbols_from_neon_under_max_price",
            "run_bulk_yahoo_price_feed", "run_bulk_yahoo_price_feed_cached", "clear_stuck_feed_job_on_boot", "set_bulk_quote_cache"}
    files = ["main.py"]
    for d in ("gateway_support", "gateway_routers"):
        files += [f"{d}/{f}" for f in sorted(os.listdir(os.path.join(GW, d))) if f.endswith(".py")]
    bad = []
    for rel in files:
        for n in ast.walk(ast.parse(_src(rel))):
            if isinstance(n, ast.AsyncFunctionDef):
                for h in _direct_calls(n, sync):
                    bad.append(f"{rel}:{n.name}->{h}")
    assert bad == []


def test_e393_bulk_cache_route_behaviour_runs_off_the_loop(monkeypatch):
    pytest.importorskip("fastapi")
    from gateway_routers import feed_jobs
    seen = []
    main_thread = threading.get_ident()

    def fake_cache():
        seen.append(threading.get_ident())
        return {"_meta": {"m": 1}, "quotes": {"A": {"price": 1}}}
    monkeypatch.setattr(df, "get_bulk_quote_cache", fake_cache)
    monkeypatch.setattr(df, "bulk_cache_age_sec", lambda: 3.0)
    monkeypatch.setattr(df, "should_refresh_bulk_cache", lambda: False)
    out = asyncio.run(feed_jobs.api_bulk_quote_cache())
    assert out == {"ok": True, "age_sec": 3.0, "should_refresh": False, "meta": {"m": 1}, "count": 1, "quotes": {"A": {"price": 1}}}
    assert seen and all(t != main_thread for t in seen)


# ── E394 ─────────────────────────────────────────────────────────────────────
def test_e394_dump_item_json_fits_drops_bulky_keys_or_refuses():
    import json
    small = {"a": 1, "reasons": ["x"]}
    assert json.loads(hs._dump_item_json(small, 1000)) == small
    big = {"keep": 1, "reasons": ["r" * 500], "headlines": ["h" * 500]}
    out = json.loads(hs._dump_item_json(big, 300))
    assert out == {"keep": 1}                                           # reasons then headlines dropped, nothing cut mid-string
    assert hs._dump_item_json({"keep": "k" * 500}, 300) is None          # cannot shrink -> caller leaves the row alone
    assert hs._dump_item_json({"x": object()}, 1000) is not None          # default=str keeps odd values serialisable
    assert hs._dump_item_json({"s": "é" * 100}, 150) is None             # json escapes é to 6 bytes, so 100 of them do not fit in 150
    assert json.loads(hs._dump_item_json({"s": "é" * 10}, 150)) == {"s": "é" * 10}


def test_e394_json_cap_follows_the_schema_and_ignores_junk():
    class HP:
        JSON_MAX_BYTES = 123
    assert hs._json_cap(HP) == 123
    assert hs._json_cap(object()) == 30000

    class Bad:
        JSON_MAX_BYTES = "nope"
    assert hs._json_cap(Bad) == 30000

    class Zero:
        JSON_MAX_BYTES = 0
    assert hs._json_cap(Zero) == 30000


# ── E395 ─────────────────────────────────────────────────────────────────────
def test_e395_non_finite_or_text_scores_do_not_break_top_picks():
    import pick_ranking as pr
    rows = [
        {"symbol": "NAN", "combined_score": float("nan"), "close": 100},
        {"symbol": "TXT", "combined_score": "n/a", "close": "x"},
        {"symbol": "INF", "combined_score": float("inf"), "close": float("nan"), "fundamental_score": "bad"},
        {"symbol": "OK", "combined_score": 70, "close": 1000, "fundamental_score": 60},
        {"symbol": "LOW", "combined_score": 40, "close": None},
    ]
    top = pr._select_top_picks(rows, limit=5)
    assert [r["symbol"] for r in top][:2] == ["OK", "LOW"] and len(top) == 5
    assert pr._value_adjusted_score({"combined_score": 50, "close": 1000, "fundamental_score": 60}) == (54.0, True)
    assert pr._value_adjusted_score({"combined_score": float("nan")}) == (0, True)


# ── E396 ─────────────────────────────────────────────────────────────────────
def test_e396_ipo_status_scan_and_universe_stale_reads_are_threaded():
    for rel, fn_name, bad in [
        ("gateway_routers/ipo_routes.py", "api_ipo_scan", {"get_ipo_scan_progress"}),
        ("gateway_routers/surprise_jobs.py", "api_ipo_scan_status", {"get_ipo_scan_progress"}),
        ("gateway_support/bound_routes_d.py", "api_ipo_notify_top_picks", {"get_ipo_list"}),
        ("gateway_support/bound_routes_c.py", "get_scan_universe", {"get_stale"}),
    ]:
        fn = _async_fn(rel, fn_name)
        assert _direct_calls(fn, bad) == [], f"{rel}:{fn_name} still calls {bad} on the loop"


# ── E397 ─────────────────────────────────────────────────────────────────────
def test_e397_qstash_publish_is_threaded_and_tolerates_non_object_bodies():
    fn = _async_fn("gateway_support/bound_routes.py", "ops_qstash_publish")
    assert _direct_calls(fn, {"publish", "schedule_gateway_tick"}) == []
    assert "if not isinstance(body, dict)" in _src("gateway_support/bound_routes.py")


# ── E398 ─────────────────────────────────────────────────────────────────────
def test_e398_scan_seeding_outcomes_and_nifty_fallback_are_off_the_loop():
    for rel, fn_name, bad in [
        ("main.py", "_run_scan_parallel_impl", {"_record_symbol_outcomes", "_batch_result_cache_get", "_batch_result_cache_set"}),
        ("main.py", "stockky_hot_stocks", {"_get_nifty_indices", "_load_searched"}),
        ("gateway_support/bound_routes_k.py", "start_bulk_feed", {"_get_nifty_indices"}),
        ("gateway_support/bound_routes_k.py", "data_feed_run", {"_get_nifty_indices"}),
    ]:
        assert _direct_calls(_async_fn(rel, fn_name), bad) == [], f"{rel}:{fn_name}"


def test_e398_seed_helper_matches_the_old_inline_loop(monkeypatch):
    pytest.importorskip("fastapi")
    import main
    store = {}
    monkeypatch.setattr(main, "_batch_result_cache_get", lambda s, lite=False: store.get((s, lite)))
    monkeypatch.setattr(main, "_batch_result_cache_set", lambda s, r, lite=False: store.__setitem__((s, lite), r))
    rows = [
        {"symbol": "aaa.ns", "decision": "BUY NOW"},
        {"symbol": "BBB", "decision": "ERROR"},          # skipped
        {"symbol": "CCC", "decision": "SELL", "lite": True},   # lite row never seeds a full scan
        {"symbol": "DDD", "decision": "SELL"},
        "junk", {"decision": "SELL"},                    # non-dict / no symbol
    ]
    assert main._seed_batch_cache_from_rows(rows, False) == 2
    assert set(store) == {("AAA", False), ("DDD", False)}
    assert main._seed_batch_cache_from_rows(rows, False) == 0      # already cached -> nothing new
    assert main._seed_batch_cache_from_rows(rows, True) == 3       # lite scan accepts the lite row too
    assert main._seed_batch_cache_from_rows(None, False) == 0


# ── E399 ─────────────────────────────────────────────────────────────────────
def test_e399_holding_period_estimate_tolerates_bad_prices():
    pytest.importorskip("fastapi")
    import main
    f = main._estimate_holding_period
    for bad in (None, 0, "", "abc", float("nan"), float("inf"), -5):
        assert f(bad, 110, "BUY NOW") is None and f(100, bad, "BUY NOW") is None
    ok = f("100", 110.0, "BUY NOW")
    assert ok["min_days"] == 12 and ok["max_days"] == 25
    assert f(100, 110, "BUY NOW") == ok
