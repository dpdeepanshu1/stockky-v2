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
    # E87: get_stock_decision moved to gateway_support/bound_routes_l.py, where main.py names are read as `ref.<name>`.
    _l = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gateway_support", "bound_routes_l.py"), encoding="utf-8").read().replace("ref.", "")
    assert "ms_passthrough.merge_into(result, td)" in _l and "ms_passthrough.feed_keys(t)" in src
    # E77: the structure route moved to gateway_support/bound_routes_c.py; main.py includes its router at the place the route used to be.
    fac = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gateway_support", "bound_routes_c.py"), encoding="utf-8").read()
    assert '"/stock/{symbol}/structure"' in fac and '@app.get("/stock/{symbol}/structure")' not in src
    inc = "app.include_router(_bound_routes_c.stock_structure_router)"
    # E87: the catch-all `/stock/{symbol}` moved to bound_routes_l.py too; its router must still be included after the structure router.
    assert src.count(inc) == 1 and src.index(inc) < src.index('app.include_router(_bound_routes_l.get_stock_decision_router)')


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f(); print("ok", n)


def test_feed_keys_empty_without_a_valid_block():
    # a technical reply with no (or an unsupported) structure block seeds nothing and so is not reported as a patched field
    assert mp.feed_keys({"rsi": 33}) == {}
    assert mp.feed_keys({"market_structure": {"schema_version": 99}}) == {}
    assert mp.feed_keys(None) == {}
