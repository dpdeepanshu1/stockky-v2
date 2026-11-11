"""E389: a missing technical score re-fetches technicals even when a close exists; wiring + helper."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GW = os.path.dirname(HERE)
sys.path.insert(0, GW)

from stock_enrich import tech_refetch_needed  # noqa: E402


def test_e389_helper_rules():
    assert tech_refetch_needed({"close": 100.0, "technical_score": None, "data_insufficient": False}, False) is True
    assert tech_refetch_needed({"close": 100.0, "technical_score": 61.0}, False) is False
    assert tech_refetch_needed({"close": 100.0, "technical_score": 50}, False) is False      # neutral 50 alone is not a trigger
    assert tech_refetch_needed({"technical_score": 72.0}, True) is True                       # decide reported the pillar failed
    assert tech_refetch_needed(None, False) is False or True                                  # never raises


def test_e389_route_wiring():
    with open(os.path.join(GW, "gateway_support", "bound_routes_l.py"), encoding="utf-8") as f:
        src = f.read()
    assert "tech_refetch_needed(result, _pillar_failed(tech_blob))" in src
    assert 'result.get("technical_score") is None and result.get("data_insufficient")' not in src
    assert '"Technical unavailable"' in src
    compile(src, "bound_routes_l.py", "exec")
