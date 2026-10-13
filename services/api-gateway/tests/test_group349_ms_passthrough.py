"""group349: gateway passthrough of the market_structure block."""
from __future__ import annotations

import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ms_passthrough as mp
from stockky_ms.engine import analyze
from stockky_ms.summary import SUMMARY_KEYS


def _bars(n=120):
    random.seed(5)
    px, out = 100.0, []
    for i in range(n):
        o = px; c = px + random.gauss(0.1, 1.2)
        out.append((1_700_000_000 + i * 86400, o, max(o, c) + .5, min(o, c) - .5, c, 1000.0)); px = c
    return out


def test_valid_block_merges_and_flattens():
    blk = analyze(_bars())
    t = {}
    assert mp.merge_into(t, {"market_structure": blk}) is True
    assert t["market_structure"] is blk and set(t["structure_summary"]) == set(SUMMARY_KEYS)


def test_unknown_schema_is_dropped_safe_default():
    blk = dict(analyze(_bars())); blk["schema_version"] = 99
    t = {}
    assert mp.merge_into(t, {"market_structure": blk}) is False and t == {}
    assert mp.safe_summary({"market_structure": blk})["ms_quality"] == "unavailable"


def test_garbage_never_raises():
    for bad in (None, 1, "x", [], {"market_structure": 5}, {"market_structure": None}):
        assert mp.safe_block(bad) is None
        assert mp.safe_summary(bad)["ms_quality"] == "unavailable"
        assert mp.merge_into({}, bad) is False
    assert mp.merge_into(None, {"market_structure": analyze(_bars())}) is False


def test_feed_keys_only_non_none_and_flag_off():
    blk = analyze(_bars())
    k = mp.feed_keys({"market_structure": blk})
    assert k and all(v is not None for v in k.values()) and k["ms_quality"] == blk["quality"]
    os.environ["STRUCTURE_PASSTHROUGH"] = "0"
    try:
        assert mp.feed_keys({"market_structure": blk}) == {} and mp.merge_into({}, {"market_structure": blk}) is False
    finally:
        del os.environ["STRUCTURE_PASSTHROUGH"]


def test_main_wiring_present():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py"), encoding="utf-8").read()
    assert src.count("import ms_passthrough") == 1
    assert "ms_passthrough.merge_into(result, td)" in src and "ms_passthrough.feed_keys(t)" in src
    assert '"/stock/{symbol}/structure"' in src and src.index('"/stock/{symbol}/structure"') < src.index('@app.get("/stock/{symbol}")')


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f(); print("ok", n)
