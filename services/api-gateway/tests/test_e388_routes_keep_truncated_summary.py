"""E388: the structure route and the sniper keep the flat summary of an over-size structure block (follow-up to E384)."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GW = os.path.dirname(HERE)
sys.path.insert(0, GW)

import ms_passthrough as mp  # noqa: E402


def _src(rel):
    with open(os.path.join(GW, rel), encoding="utf-8") as f:
        return f.read()


def test_e388_routes_keep_the_truncated_summary():
    assert "truncated_summary" in _src("gateway_support/bound_routes_c.py")
    assert "truncated_summary" in _src("gateway_routers/sniper.py")
    flat = {k: None for k in mp.SUMMARY_KEYS}
    flat["ms_quality"] = "ok"
    got = mp.truncated_summary({"structure_summary": flat, "structure_truncated": True})
    assert got is not None and got["ms_quality"] == "ok"
    assert mp.truncated_summary({"structure_summary": flat}) is None          # no truncation flag -> as before
    assert mp.truncated_summary({"structure_summary": {"ms_quality": "unavailable"}, "structure_truncated": True}) is None
