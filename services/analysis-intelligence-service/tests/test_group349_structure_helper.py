"""group349: the technical service's structure helper is additive and fail-open (helper source is exec'd without fastapi)."""
from __future__ import annotations

import json
import logging
import os
import random
import sys

import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)


def _load_helpers():
    src = open(os.path.join(HERE, "technical", "main.py"), encoding="utf-8").read()
    a = src.index("# ── Market-structure engine")
    b = src.index('@app.get("/sector-strength/{symbol}")')
    store = {}
    import threading, time
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ns = {"os": os, "pd": pd, "json": json, "threading": threading, "time": time, "datetime": datetime, "ZoneInfo": ZoneInfo, "logger": logging.getLogger("t"), "get_cache_ttl": lambda: 300,
          "_cache_get": lambda k: store.get(k), "_cache_set": lambda k, v, ttl=None: store.__setitem__(k, v), "_store": store}
    exec(compile(src[a:b], "helpers", "exec"), ns)
    return ns


def _df(n=150, end=None):
    random.seed(3)
    end = end or pd.Timestamp.now(tz="UTC").normalize()
    idx = pd.date_range(end=end, periods=n, freq="D")
    px, rows = 100.0, []
    for _ in range(n):
        o = px; c = px + random.gauss(0.1, 1.2)
        rows.append((o, max(o, c) + .5, min(o, c) - .5, c, 1000.0)); px = c
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=idx)


def test_block_and_summary_returned():
    ns = _load_helpers()
    blk, summ = ns["_structure_for_df"](_df(), {"ema20": 100.0})
    assert blk is not None and blk["schema_version"] == 1 and summ["ms_quality"] != "unavailable"


def test_flag_off_and_thin_and_missing_engine():
    ns = _load_helpers()
    os.environ["STRUCTURE_ENABLED"] = "0"
    try:
        assert ns["_structure_for_df"](_df()) == (None, None)
    finally:
        del os.environ["STRUCTURE_ENABLED"]
    assert ns["_structure_for_df"](_df(3)) == (None, None)
    assert ns["_structure_for_df"](None) == (None, None)
    ns["_ms_analyze"] = None
    assert ns["_structure_for_df"](_df()) == (None, None)


def test_engine_error_is_swallowed():
    ns = _load_helpers()
    def boom(*a, **k): raise RuntimeError("x")
    ns["_ms_analyze"] = boom
    assert ns["_structure_for_df"](_df()) == (None, None)


def test_stale_flag_marks_old_history():
    ns = _load_helpers()
    old = _df(end=pd.Timestamp.now(tz="UTC").normalize() - pd.Timedelta(days=30))
    blk, _ = ns["_structure_for_df"](old)
    assert blk is not None and blk["quality"] in ("stale", "limited", "insufficient", "unavailable")


def test_naive_index_ok():
    ns = _load_helpers()
    df = _df(); df.index = df.index.tz_localize(None)
    assert ns["_structure_for_df"](df)[0] is not None


def test_cache_hit_skips_engine_and_new_bar_recomputes():
    ns = _load_helpers()
    calls = []
    real = ns["_ms_analyze"]
    def counting(*a, **k):
        calls.append(1); return real(*a, **k)
    ns["_ms_analyze"] = counting
    df = _df()
    b1, s1 = ns["_structure_cached"]("X", df, {"ema20": 100.0})
    b2, s2 = ns["_structure_cached"]("X", df, {"ema20": 100.0})
    assert b1 is not None and b1 == b2 and s1 == s2 and len(calls) == 1
    ns["_structure_cached"]("X", df, {"ema20": 100.0}, force=True)
    assert len(calls) == 2                      # force bypasses
    ns["_structure_cached"]("X", _df(151), {"ema20": 100.0})
    assert len(calls) == 3                      # different frame -> recompute
    ns["_structure_cached"]("X", df, {"extended": True})
    assert len(calls) == 4                      # different flags -> separate entry


def test_cache_ttl_zero_disables_and_failure_not_cached():
    ns = _load_helpers()
    os.environ["STRUCTURE_CACHE_TTL_S"] = "0"
    try:
        ns["_structure_cached"]("X", _df())
        assert not ns["_store"]
    finally:
        del os.environ["STRUCTURE_CACHE_TTL_S"]
    def boom(*a, **k): raise RuntimeError("x")
    ns["_ms_analyze"] = boom
    assert ns["_structure_cached"]("Y", _df()) == (None, None) and not ns["_store"]


def test_cache_layer_errors_fail_open():
    ns = _load_helpers()
    def bad(*a, **k): raise RuntimeError("redis down")
    ns["_cache_get"] = bad
    blk, _ = ns["_structure_cached"]("X", _df())
    assert blk is not None


def test_size_guard_drops_block_keeps_summary():
    ns = _load_helpers()
    blk, summ = ns["_structure_for_df"](_df())
    r = {}
    assert ns["_attach_structure"](r, blk, summ) is True and r["market_structure"] is blk and "structure_truncated" not in r
    os.environ["STRUCTURE_MAX_BYTES"] = "10"
    try:
        r = {}
        assert ns["_attach_structure"](r, blk, summ) is False
        assert "market_structure" not in r and r["structure_summary"] == summ and r["structure_truncated"] is True
    finally:
        del os.environ["STRUCTURE_MAX_BYTES"]
    os.environ["STRUCTURE_MAX_BYTES"] = "0"      # 0 disables the guard
    try:
        r = {}
        assert ns["_attach_structure"](r, blk, summ) is True
    finally:
        del os.environ["STRUCTURE_MAX_BYTES"]
    assert ns["_attach_structure"]({}, None, None) is False


def test_default_payload_is_within_cap():
    ns = _load_helpers()
    blk, _ = ns["_structure_for_df"](_df(250))
    assert len(json.dumps(blk, default=str, separators=(",", ":"))) < 60000


def test_analyze_has_lite_param_and_wiring():
    src = open(os.path.join(HERE, "technical", "main.py"), encoding="utf-8").read()
    assert "lite: bool = False" in src and "_structure_cached(sym, df, _ms_ctx" in src and "_attach_structure(result" in src


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f(); print("ok", n)
