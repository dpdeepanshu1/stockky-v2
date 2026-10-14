"""group352 (F7): the structure engine runs on the untrimmed, closed-bar frame; params override; counters; fail-open."""
from __future__ import annotations

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_group349_structure_helper import _df, _load_helpers  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


def _ist(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=IST)


def _df_ending(day, n=150):
    return _df(n, end=pd.Timestamp(day))


def test_forming_bar_dropped_during_session():
    ns = _load_helpers()
    df = _df_ending("2026-10-14")
    fr, dropped = ns["_drop_forming_bar"](df, _ist(2026, 10, 14, 11, 0))
    assert dropped and len(fr) == len(df) - 1


def test_bar_kept_after_close_plus_buffer():
    ns = _load_helpers()
    df = _df_ending("2026-10-14")
    _, d1 = ns["_drop_forming_bar"](df, _ist(2026, 10, 14, 15, 40))      # inside the 20 min buffer
    _, d2 = ns["_drop_forming_bar"](df, _ist(2026, 10, 14, 15, 55))
    assert d1 is True and d2 is False


def test_previous_day_bar_never_dropped():
    ns = _load_helpers()
    df = _df_ending("2026-10-13")
    fr, dropped = ns["_drop_forming_bar"](df, _ist(2026, 10, 14, 11, 0))
    assert not dropped and len(fr) == len(df)


def test_buffer_env_override():
    ns = _load_helpers()
    os.environ["STRUCTURE_BAR_CLOSE_BUFFER_MIN"] = "0"
    try:
        _, d = ns["_drop_forming_bar"](_df_ending("2026-10-14"), _ist(2026, 10, 14, 15, 31))
        assert d is False
    finally:
        del os.environ["STRUCTURE_BAR_CLOSE_BUFFER_MIN"]


def test_untrimmed_frame_used_when_last_bar_matches():
    ns = _load_helpers()
    full = _df_ending("2026-10-13", 300)
    trimmed = full.iloc[-120:]
    ns["_remember_full_frame"]("ABC", full, "dhan")
    fr, meta = ns["_structure_frame"]("ABC", trimmed, _ist(2026, 10, 14, 11, 0))
    assert meta["full_history"] is True and len(fr) == 300 and meta["source"] == "dhan"


def test_full_frame_ignored_when_stale_mismatch():
    ns = _load_helpers()
    ns["_remember_full_frame"]("ABC", _df_ending("2026-10-10", 300), "dhan")
    trimmed = _df_ending("2026-10-13", 120)
    fr, meta = ns["_structure_frame"]("ABC", trimmed, _ist(2026, 10, 14, 11, 0))
    assert meta["full_history"] is False and len(fr) == 120


def test_full_frame_store_is_bounded():
    ns = _load_helpers()
    for i in range(100):
        ns["_remember_full_frame"](f"S{i}", _df_ending("2026-10-13", 30))
    assert len(ns["_FULL_FRAMES"]) <= 64


def test_block_carries_frame_meta_and_counters():
    ns = _load_helpers()
    full = _df_ending("2026-10-13", 300)
    ns["_remember_full_frame"]("XYZ", full, "angelone")
    blk, summ = ns["_structure_for_df"](full.iloc[-120:], {"ema20": 100.0}, "XYZ")
    assert blk["frame"]["full_history"] is True and blk["frame"]["source"] == "angelone"
    assert ns["_STRUCT_COUNTERS"]["computed"] >= 1 and ns["_STRUCT_COUNTERS"]["full_frame"] >= 1


def test_params_override_and_bad_json():
    ns = _load_helpers()
    os.environ["STRUCTURE_PARAMS_JSON"] = '{"swing_min_amp_atr": 0.9, "nope": 1}'
    try:
        p = ns["_structure_params"]()
        assert p is not None and p.swing_min_amp_atr == 0.9
        os.environ["STRUCTURE_PARAMS_JSON"] = "{bad"
        assert ns["_structure_params"]() is None
    finally:
        del os.environ["STRUCTURE_PARAMS_JSON"]


def test_engine_error_counts_and_fails_open():
    ns = _load_helpers()
    def boom(*a, **k): raise RuntimeError("x")
    ns["_ms_analyze"] = boom
    before = ns["_STRUCT_COUNTERS"]["errors"]
    assert ns["_structure_for_df"](_df(), {}, "Q") == (None, None)
    assert ns["_STRUCT_COUNTERS"]["errors"] == before + 1
