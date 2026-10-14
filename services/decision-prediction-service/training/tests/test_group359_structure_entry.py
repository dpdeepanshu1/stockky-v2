"""group359: entry-time market-structure snapshot on paper trades (training/structure_entry.py + its wiring in app.py).
Plain asserts; runs with pytest or `python3 tests/test_group359_structure_entry.py` (no fastapi / sqlalchemy needed: app.py is checked as source)."""
from __future__ import annotations

import ast
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import structure_entry as se  # noqa: E402

REC = {"mode": "shadow", "applied": False, "original_decision": "BUY NOW", "policy_decision": "WAIT",
       "rules": [{"id": "P2_CHASE", "to": "WAIT", "reason": "chasing: 2.1 ATR above the zone"}],
       "no_opinion": None, "structural_levels": {"stop": 96.0, "target": 112.0, "rr": 1.9},
       "features": {"quality": "ok", "trend": "bullish", "bos_dir": "bullish", "bos_bars": 2, "bos_failed": False, "chase_atr": 2.1, "rr": 1.9,
                    "asof": "2026-10-13", "ver": "1", "secret": "x", "fvg_top": 5},
       "summary_line": "Trend up · BOS up 2d", "explain": {"summary": "dropped"}}


def test_sanitize_keeps_only_the_allow_list():
    e = se.sanitize_entry(REC)
    assert e["v"] == 1 and e["mode"] == "shadow" and e["applied"] is False and e["policy_decision"] == "WAIT"
    assert e["rules"] == [{"id": "P2_CHASE", "to": "WAIT", "reason": "chasing: 2.1 ATR above the zone"}]
    assert e["levels"] == {"stop": 96.0, "target": 112.0, "rr": 1.9}
    assert e["features"] == {"bos_bars": 2.0, "chase_atr": 2.1, "rr": 1.9, "quality": "ok", "trend": "bullish", "bos_dir": "bullish",
                             "asof": "2026-10-13", "ver": "1", "bos_failed": False}
    assert "explain" not in e and "secret" not in e["features"] and e["summary_line"] == "Trend up · BOS up 2d"


def test_unusable_records_give_none():
    for bad in (None, [], "x", 5, {}, {"mode": "off"}, {"mode": "banana"}, {"mode": None, "rules": []}):
        assert se.sanitize_entry(bad) is None


def test_applied_is_strictly_true_and_garbage_fields_are_dropped():
    e = se.sanitize_entry({"mode": "full", "applied": "yes", "rules": "x", "features": [1], "structural_levels": {"stop": "x"}})
    assert e["applied"] is False and e["rules"] == [] and e["features"] == {} and e["levels"] is None
    e = se.sanitize_entry({"mode": "caps", "applied": True, "features": {"rr": float("nan"), "chase_atr": True, "tqs": "55"}})
    assert e["applied"] is True and e["features"] == {"tqs": 55.0}


def test_size_caps():
    big = {"mode": "shadow", "rules": [{"id": "R%d" % i, "to": "WAIT", "reason": "x" * 5000} for i in range(50)],
           "features": {"quality": "ok", "trend": "up", "rr": 1.0, "chase_atr": 1.0, "asof": "y" * 500}}
    e = se.sanitize_entry(big)
    assert len(e["rules"]) <= 8 and all(len(r["reason"]) <= 200 for r in e["rules"])
    assert len(json.dumps(e)) <= se.MAX_BYTES + 200


def test_attach_adds_a_copy_and_never_touches_the_input():
    fs = {"keep": 1}
    out = se.attach(fs, REC)
    assert out is not fs and fs == {"keep": 1} and out["keep"] == 1 and out["structure_entry"]["mode"] == "shadow"
    assert se.attach(None, REC)["structure_entry"]["mode"] == "shadow" and se.attach("", REC) is not None


def test_attach_reads_a_structure_key_inside_the_feature_snapshot():
    out = se.attach({"structure": REC}, None)
    assert out["structure_entry"]["policy_decision"] == "WAIT" and out["structure"] == REC


def test_attach_is_write_once_and_none_means_leave_alone():
    first = se.attach({}, REC)
    assert se.attach(first, dict(REC, mode="full")) is None          # entry-time data is never rewritten
    assert se.attach({}, None) is None and se.attach({}, {"mode": "off"}) is None
    assert se.attach(5, REC) is None and se.attach([1], REC) is None
    assert se.attach({"structure_entry": {"junk": 1}}, REC)["structure_entry"]["mode"] == "shadow"   # an unusable old value is replaced


def test_read_entry():
    e = se.attach({}, REC)
    assert se.read_entry(e)["mode"] == "shadow" and se.read_entry(json.dumps(e))["mode"] == "shadow"
    for bad in (None, {}, "x", "{bad", [], {"structure_entry": 1}, {"structure_entry": {"mode": "off"}}):
        assert se.read_entry(bad) is None


def test_garbage_never_raises():
    for a in (None, 0, "x", [], {"mode": "shadow", "rules": [None, 1, {"reason": 5}], "features": {"rr": object()}}):
        for b in (None, 0, "x", [], REC, {"mode": "shadow", "rules": [None, {"reason": object()}]}):
            se.attach(a, b)
            se.sanitize_entry(b)
            se.read_entry(a)


# ── app.py wiring, checked as source (the module needs fastapi / sqlalchemy to import) ─────────────────────────────────────

def _app_src():
    return open(os.path.join(HERE, "app.py"), encoding="utf-8").read()


def test_app_py_wiring():
    src = _app_src()
    ast.parse(src)
    assert "import structure_entry as _se" in src
    assert src.count("_se.attach(") == 3                                   # store_prediction, commit (new row), commit (backfill)
    assert "structure: Optional[dict] = None" in src                       # the pick model accepts the record
    assert '"structure": entries.get(t.prediction_id)' in src               # GET /api/trades returns it
    assert "_se.read_entry(fs)" in src
    # the commit path backfills BEFORE it opens the trade, and the entry is never part of the force-refresh field list
    assert src.index("structure entry backfill") < src.index("trades_module.open_trade(pred_id")
    refresh = src[src.index('"price", "confidence", "combined_score"'):src.index('"rsi", "macd", "ema", "volume_ratio",')]
    assert "feature_snapshot" not in refresh and "structure" not in refresh


def test_trade_list_degrades_when_the_lookup_fails():
    src = _app_src()
    seg = src[src.index("entries: Dict[str, Any] = {}"):src.index('"structure": entries.get')]
    assert "try:" in seg and "except Exception" in seg and seg.index("try:") < seg.index("except Exception")


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
            n += 1
    print(f"{n} passed 0 failed")
