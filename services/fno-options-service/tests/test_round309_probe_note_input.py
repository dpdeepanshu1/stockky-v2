"""Round 309: a NaN / Infinity / negative contract-note line is refused before it is stored, and an old stored one is dropped when read."""
from __future__ import annotations

import json

from fno import probecheck as pc


def test_good_lines_pass_and_none_is_skipped():
    out, bad = pc.clean_note_lines({"brokerage": 40.0, "stt": None, "gst": 7.2, "total": 55.5})
    assert bad is None and out == {"brokerage": 40.0, "gst": 7.2, "total": 55.5}


def test_nan_inf_and_negative_are_refused():
    for v in (float("nan"), float("inf"), float("-inf")):
        out, bad = pc.clean_note_lines({"stt": v})
        assert out == {} and "stt" in bad
    out, bad = pc.clean_note_lines({"sebi": -0.1})
    assert out == {} and "negative" in bad


def test_an_old_stored_nan_line_is_dropped_on_read():
    saved = {"lines": {"stt": float("nan"), "gst": 7.2, "stamp": float("inf")}, "ts": 1.0}
    c = pc.clean_saved(saved)
    assert c["lines"] == {"gst": 7.2} and c["ts"] == 1.0
    json.dumps(c, allow_nan=False)
    assert pc.clean_saved(None) is None and pc.clean_saved("x") is None
    assert pc.clean_saved({"ts": 1})["lines"] == {}
