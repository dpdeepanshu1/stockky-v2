"""E484 (two findings from the read of `_build_scan_universe` and `_analyze_one_symbol_ultra`).

(1) `_build_scan_universe` merged the user's watchlist and recently searched names into a set with every other source and cut the result to
SCAN_UNIVERSE_TARGET in set (hash) order after the dynamic names. When the dynamic sources plus the base list came to more than the target, a random
share of the watchlist was dropped on every build (reproduced: 5 watchlist names, about 270 dynamic names, target 500 -> two missing per build on
average). They are now placed right after the dynamic names (watchlist first, then searched); a pruned searched name stays out, a delisted one too,
a renamed one is pinned under its new symbol.

(2) `_analyze_one_symbol_ultra` wrote the scan's bulk-prefetch snapshot over `data_feed._LOCAL_SYMBOLS[prefix + sym]` unconditionally ("warm the
local cache"). A full scan runs for minutes, so a live price patch / bulk write / structure refresh that landed after the prefetch was rewound in
memory (canonical key only; `get_symbol` reads that key first), and the next `put_symbol` / `patch_feed_price` used the older row as its merge base.
`_warm_local_feed_row` now fills an empty slot only.

No fastapi needed (functions lifted from main.py by AST; data_feed is the real module). Runs with pytest or `python3 tests/test_e484_...py`."""
from __future__ import annotations

import ast
import logging
import os
import random
import sys
import threading

_SERVICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVICE not in sys.path:
    sys.path.insert(0, _SERVICE)
_SRC = open(os.path.join(_SERVICE, "main.py"), encoding="utf-8").read()
_TREE = ast.parse(_SRC)


def _lift(names, ns):
    body = [n for n in _TREE.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    assert {n.name for n in body} == set(names), set(names) - {n.name for n in body}
    exec(compile(ast.Module(body=body, type_ignores=[]), "main.py", "exec"), ns)
    return ns


# ── (1) the universe ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def _universe_env(watch, searched, pruned=(), clean=None, target=500, n_dyn=90):
    calls = {"watch": 0, "searched": 0}

    def load_watch():
        calls["watch"] += 1
        return list(watch)

    def load_searched():
        calls["searched"] += 1
        return list(searched)

    ns = dict(
        random=random, os=os, threading=threading, logger=logging.getLogger("e484"), List=list, Dict=dict, Set=set, Optional=None,
        SCAN_UNIVERSE_TARGET=target, SCAN_UNIVERSE_HARD_CAP=5000, SCAN_UNIVERSE_KEY="k", SCAN_UNIVERSE_STALE_KEY="s",
        SCAN_UNIVERSE_MIN_REBUILD_SYMBOLS=50, SCAN_UNIVERSE_THIN_TTL_S=120, MAX_UNIVERSE_PRICE=0,
        _universe_refresh_ctx=threading.local(), _kv_cache=None, SYMBOL_ALIASES={},
    )
    ns["_redis_get"] = lambda k: None
    ns["_redis_set"] = lambda *a, **k: None
    ns["_filter_equities"] = lambda x: x
    ns["_filter_symbols_under_max_price"] = lambda x: list(x)
    ns["_get_all_nse_securities"] = lambda: [f"SEC{i}" for i in range(2000)]
    ns["_get_nifty_indices"] = lambda: []
    ns["_get_momentum_movers"] = lambda: [f"MOV{i}" for i in range(n_dyn)]
    ns["_get_bulk_deal_symbols"] = lambda: [f"BULK{i}" for i in range(n_dyn // 3)]
    ns["_get_52w_extreme_symbols"] = lambda: [f"WK{i}" for i in range(n_dyn // 3)]
    ns["_get_news_mentioned_symbols"] = lambda: [f"NEWS{i}" for i in range(n_dyn // 3)]
    ns["_get_recent_ipos"] = lambda: []
    ns["_get_event_symbols"] = lambda: [f"EVT{i}" for i in range(n_dyn // 3)]
    ns["_load_watchlist"] = load_watch
    ns["_load_searched"] = load_searched
    ns["_clean_equity_symbol"] = clean or (lambda s: str(s).strip().upper() or None)
    ns["_is_symbol_pruned"] = lambda s: s in set(pruned)
    ns["is_nse_holiday"] = lambda d: False
    _lift({"_build_scan_universe", "_cut_universe_under_price_cap", "_price_cut_first_enabled", "_price_headroom_pct"}, ns)
    return ns, calls


WATCH = ["WATCH1", "WATCH2", "WATCH3", "WATCH4", "WATCH5"]
SEARCHED = ["SRCH1", "SRCH2", "SRCH3"]


def test_watchlist_and_searched_names_survive_the_cut_when_dynamic_names_fill_the_universe():
    ns, _ = _universe_env(WATCH, SEARCHED)
    for seed in range(40):                                  # the old order was hash / shuffle dependent: one build proves little
        random.seed(seed)
        out = ns["_build_scan_universe"]()
        assert len(out) == 500
        assert set(WATCH + SEARCHED) <= set(out), (seed, sorted(set(WATCH + SEARCHED) - set(out)))


def test_order_is_dynamic_names_then_watchlist_then_searched_then_the_rest():
    ns, _ = _universe_env(WATCH, SEARCHED)
    random.seed(1)
    out = ns["_build_scan_universe"]()
    dyn_end = max(i for i, s in enumerate(out) if s.startswith(("MOV", "BULK", "WK", "NEWS", "EVT")))
    first_pin = out.index("WATCH1")
    assert dyn_end < first_pin
    assert out[first_pin:first_pin + 8] == WATCH + SEARCHED
    assert all(s.startswith("SEC") for s in out[first_pin + 8:])


def test_a_name_that_is_both_dynamic_and_on_the_watchlist_appears_once_in_the_dynamic_block():
    ns, _ = _universe_env(["MOV3", "WATCH1"], [])
    random.seed(2)
    out = ns["_build_scan_universe"]()
    assert out.count("MOV3") == 1 and out.count("WATCH1") == 1
    assert out.index("MOV3") < out.index("WATCH1") < min(i for i, s in enumerate(out) if s.startswith("SEC"))


def test_a_pruned_searched_name_stays_out_but_a_pruned_watchlist_name_stays_in():
    ns, _ = _universe_env(["WATCH1"], ["SRCH1", "SRCH2"], pruned={"WATCH1", "SRCH1"})
    random.seed(3)
    out = ns["_build_scan_universe"]()
    assert "WATCH1" in out and "SRCH2" in out and "SRCH1" not in out


def test_delisted_names_are_not_pinned_and_renamed_ones_are_pinned_under_the_new_symbol():
    def clean(s):
        s = str(s).strip().upper()
        return {"OLDNAME": "NEWNAME", "GONE": None}.get(s, s) or None

    ns, _ = _universe_env(["OLDNAME", "GONE", "WATCH2"], [], clean=clean)
    random.seed(4)
    out = ns["_build_scan_universe"]()
    assert "NEWNAME" in out and "WATCH2" in out and "GONE" not in out and "OLDNAME" not in out


def test_the_watchlist_and_searched_lists_are_read_once_per_build():
    ns, calls = _universe_env(WATCH, SEARCHED)
    ns["_build_scan_universe"]()
    assert calls == {"watch": 1, "searched": 1}


def test_a_thin_universe_still_contains_every_pinned_name_once():
    ns, _ = _universe_env(WATCH, SEARCHED, n_dyn=0)
    ns["_get_all_nse_securities"] = lambda: []
    ns["_get_nifty_indices"] = lambda: ["IDX1"]
    out = ns["_build_scan_universe"]()
    assert [s for s in out if s in set(WATCH + SEARCHED)].count("WATCH1") == 1
    assert set(WATCH + SEARCHED) <= set(out)


# ── (2) the local-cache warm ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def _warm():
    return _lift({"_warm_local_feed_row"}, {})["_warm_local_feed_row"]


def _fresh_symbol(name):
    import data_feed as df
    for k in (df.DATA_FEED_PREFIX + name, df.FEED_ALIAS_PREFIX + name, df.FEED_LEGACY_PREFIX + name):
        df._LOCAL_SYMBOLS.pop(k, None)
    df._LOCAL_INDEX.discard(name)
    return df


def test_an_empty_slot_is_filled_and_the_symbol_is_indexed():
    df = _fresh_symbol("E484EMPTY")
    _warm()("E484EMPTY", {"sector": "IT"})
    assert df._LOCAL_SYMBOLS[df.DATA_FEED_PREFIX + "E484EMPTY"] == {"sector": "IT"}
    assert "E484EMPTY" in df._LOCAL_INDEX
    _fresh_symbol("E484EMPTY")


def test_a_row_written_after_the_prefetch_is_not_rewound():
    df = _fresh_symbol("E484NEWER")
    newer = {"sector": "IT", "last_price": 105.0, "price_refreshed_at": "T1"}
    older = {"sector": "IT", "last_price": 100.0, "price_refreshed_at": "T0"}
    df._LOCAL_SYMBOLS[df.DATA_FEED_PREFIX + "E484NEWER"] = newer
    _warm()("E484NEWER", older)
    assert df._LOCAL_SYMBOLS[df.DATA_FEED_PREFIX + "E484NEWER"] is newer
    assert "E484NEWER" in df._LOCAL_INDEX
    _fresh_symbol("E484NEWER")


def test_end_to_end_a_live_price_patch_after_the_prefetch_survives_the_warm_and_the_next_write():
    """Real DataFeedStore over an in-memory durable layer: prefetch -> price patch lands -> scan worker warms with the prefetch row -> next write."""
    df = _fresh_symbol("E484E2E")
    durable = {}
    store = df.DataFeedStore(lambda k: durable.get(k), lambda k, v, ttl=None: durable.__setitem__(k, v), None)
    store.put_symbol("E484E2E", {"sector": "IT", "fundamental_score": 60, "last_price": 100.0, "price_refreshed_at": "T0"})
    snapshot = store.get_symbols_bulk(["E484E2E"])["E484E2E"]           # what the scan prefetched
    assert snapshot["last_price"] == 100.0
    patched = dict(store.get_symbol("E484E2E"))                          # what patch_feed_price does: read, set the price fields, put
    patched.update({"last_price": 105.0, "current_price": 105.0, "price_refreshed_at": "T1"})
    store.put_symbol("E484E2E", patched)
    _warm()("E484E2E", snapshot)                                         # the worker reaches this symbol minutes later
    assert store.get_symbol("E484E2E")["last_price"] == 105.0            # old code: 100.0 (the canonical key was rewound)
    store.put_symbol("E484E2E", {"volume": 12345})                       # an unrelated write merges over get_symbol's row
    assert durable[df.DATA_FEED_PREFIX + "E484E2E"]["last_price"] == 105.0
    assert durable[df.DATA_FEED_PREFIX + "E484E2E"]["price_refreshed_at"] == "T1"
    _fresh_symbol("E484E2E")


def test_a_failing_import_is_swallowed():
    import data_feed as df
    saved = df._LOCAL_SYMBOLS
    del df._LOCAL_SYMBOLS                                                # `from data_feed import _LOCAL_SYMBOLS` now fails
    try:
        _warm()("E484X", {"sector": "IT"})                               # must not raise
    finally:
        df._LOCAL_SYMBOLS = saved


if __name__ == "__main__":
    import traceback
    failed = 0
    for _n, _f in sorted(globals().items()):
        if _n.startswith("test_") and callable(_f):
            try:
                _f()
                print("ok  ", _n)
            except Exception:
                failed += 1
                print("FAIL", _n)
                traceback.print_exc()
    raise SystemExit(1 if failed else 0)
