"""group426: per-event structure counters (bos / choch / sweep / fvg / ob) on the technical service's /health block."""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tests"))

from test_group349_structure_helper import _load_helpers, _df  # noqa: E402


def _block(**kw):
    b = {"quality": "ok", "bos": {"direction": "bullish"}, "choch": {"direction": None},
         "liquidity": {"sweep": {"direction": "bullish"}}, "fvg": {"side": "bullish"}, "order_block": None}
    b.update(kw)
    return b


def test_counters_exist_and_start_at_zero():
    ns = _load_helpers()
    for k in ("bos", "choch", "sweep", "fvg", "ob"):
        assert ns["_STRUCT_COUNTERS"][k] == 0


def test_counts_only_events_that_are_present():
    ns = _load_helpers()
    ns["_count_structure_events"](_block())
    c = ns["_STRUCT_COUNTERS"]
    assert (c["bos"], c["choch"], c["sweep"], c["fvg"], c["ob"]) == (1, 0, 1, 1, 0)
    ns["_count_structure_events"](_block(order_block={"side": "bullish"}, choch={"direction": "bearish"}))
    assert (c["choch"], c["ob"]) == (1, 1)


def test_unavailable_blocks_and_junk_are_ignored_and_never_raise():
    ns = _load_helpers()
    for blk in (None, "x", {}, _block(quality="unavailable"), _block(quality="insufficient")):
        ns["_count_structure_events"](blk)
    c = ns["_STRUCT_COUNTERS"]
    assert all(c[k] == 0 for k in ("bos", "choch", "sweep", "fvg", "ob"))
    ns["_count_structure_events"](_block(liquidity="bad", bos="bad"))      # malformed parts: skipped, the rest still counted
    assert c["bos"] == 0 and c["sweep"] == 0 and c["fvg"] == 1


def test_real_compute_bumps_counters_consistently_with_computed():
    ns = _load_helpers()
    blk, _ = ns["_structure_for_df"](_df(200), {}, "TEST")
    c = ns["_STRUCT_COUNTERS"]
    assert blk is not None and c["computed"] == 1
    for key, part, field in (("bos", blk.get("bos"), "direction"), ("choch", blk.get("choch"), "direction"),
                             ("fvg", blk.get("fvg"), "side"), ("ob", blk.get("order_block"), "side")):
        assert c[key] == (1 if isinstance(part, dict) and part.get(field) else 0), key
