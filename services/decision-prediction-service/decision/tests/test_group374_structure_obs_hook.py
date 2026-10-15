"""group374: decision-service side of the all-decision observation log (decision/structure_obs_hook.py + its wiring in decision/main.py).
main.py needs fastapi, so it is checked as source and the queue helper is pulled out with ast. Plain asserts; pytest or `python3 <file>`."""
from __future__ import annotations

import ast
import os
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import structure_obs_hook as h  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 10, 15, 11, 0, tzinfo=IST)
RESP = {"decision": "WAIT", "close": 101.5, "combined_score": 63.0, "technical_score": 70.0,
        "structure": {"mode": "full", "applied": True, "original_decision": "BUY NOW", "policy_decision": "WAIT",
                      "rules": [{"id": "P2_CHASE", "to": "WAIT", "reason": "chase"}, {"nope": 1}],
                      "structural_levels": {"stop": 96.0, "target": 112.0, "rr": 1.9},
                      "features": {"quality": "ok", "trend": "bullish", "chase_atr": 2.1, "atr": 2.0, "asof": "2026-10-14", "ver": "1.0.0", "rr": 1.9,
                                   "junk": 1}}}


def _fresh():
    h._reset_for_tests()


def test_payload_for_a_demoted_pick_keeps_the_legacy_decision_and_rules():
    p = h.build_payload("abc", RESP, NOW)
    assert p["symbol"] == "ABC" and p["obs_date"] == "2026-10-15" and p["asof_date"] == "2026-10-14" and p["engine_version"] == "1.0.0"
    assert p["final_decision"] == "WAIT" and p["legacy_decision"] == "BUY NOW" and p["policy_applied"] is True and p["rules"] == ["P2_CHASE"]
    assert p["price"] == 101.5 and p["atr"] == 2.0 and p["combined_score"] == 63.0 and p["technical_score"] == 70.0
    assert p["levels"] == {"stop": 96.0, "target": 112.0, "rr": 1.9} and "junk" not in p["features"]


def test_utc_clock_is_converted_to_the_ist_day():
    late_utc = datetime(2026, 10, 14, 20, 0, tzinfo=timezone.utc)   # 01:30 IST on the 15th
    assert h.build_payload("A", RESP, late_utc)["obs_date"] == "2026-10-15"


def test_no_usable_structure_means_no_row():
    for bad in (None, {}, {"decision": "WAIT"}, {"decision": "WAIT", "structure": "x"}, {"structure": RESP["structure"]}):
        assert h.build_payload("A", bad, NOW) is None
    for q in ("unavailable", "insufficient", "stale", None):
        r = {**RESP, "structure": {**RESP["structure"], "features": {"quality": q}}}
        assert h.build_payload("A", r, NOW) is None
    assert h.build_payload("", RESP, NOW) is None
    assert h.build_payload("A", {**RESP, "structure": {**RESP["structure"], "features": {"quality": "limited"}}}, NOW) is not None


def test_levels_fall_back_to_features_and_unapplied_policy_keeps_decision():
    st = {"mode": "full", "applied": False, "features": {"quality": "ok", "stop": 95.0, "target": 110.0, "rr": 1.7}}
    p = h.build_payload("A", {"decision": "BUY NOW", "close": 100, "structure": st}, NOW)
    assert p["legacy_decision"] == "BUY NOW" and p["policy_applied"] is False and p["levels"] == {"stop": 95.0, "target": 110.0, "rr": 1.7}


def test_dedupe_is_per_symbol_day_decision_and_bounded():
    _fresh()
    p = h.build_payload("A", RESP, NOW)
    assert h.should_send(p) is True and h.should_send(dict(p)) is False
    assert h.should_send({**p, "final_decision": "BUY NOW"}) is True      # the day's decision changed -> send again
    assert h.should_send({**p, "symbol": "B"}) is True
    _fresh()
    for i in range(h._MAX_SEEN + 5):
        h.should_send({"symbol": f"S{i}", "obs_date": "d"})
    assert len(h._SEEN) <= h._MAX_SEEN


def test_enabled_switch():
    os.environ.pop("STRUCTURE_OBS_ENABLED", None)
    assert h.enabled() is True
    for off in ("0", "false", "off", "no"):
        os.environ["STRUCTURE_OBS_ENABLED"] = off
        assert h.enabled() is False
    os.environ.pop("STRUCTURE_OBS_ENABLED", None)


def test_garbage_never_raises():
    assert h.build_payload("A", {"decision": "WAIT", "structure": {"features": {"quality": "ok"}, "rules": 5, "structural_levels": 3}}, NOW) is not None
    assert h.should_send(None) is False


# ── wiring ───────────────────────────────────────────────────────────────────
_SRC = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()


class _BG:
    def __init__(self):
        self.calls = []

    def add_task(self, fn, *a, **k):
        self.calls.append((fn, a, k))


def _queue_fn(hook):
    tree = ast.parse(_SRC)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_queue_structure_observation")
    ns = {"_so_hook": hook, "_post_structure_observation": lambda p: None}
    exec(compile(ast.Module([fn], []), "main_snip", "exec"), ns)
    return ns["_queue_structure_observation"]


def test_queue_helper_queues_once_and_never_raises():
    _fresh()
    q, bg = _queue_fn(h), _BG()
    assert q(bg, "ABC", RESP) is True and len(bg.calls) == 1 and bg.calls[0][1][0]["symbol"] == "ABC"
    assert q(bg, "ABC", RESP) is False and len(bg.calls) == 1            # deduped
    assert q(None, "ABC", RESP) is False and q(bg, "ABC", None) is False
    assert _queue_fn(None)(bg, "ABC", RESP) is False
    os.environ["STRUCTURE_OBS_ENABLED"] = "0"
    try:
        _fresh()
        assert q(bg, "ZZZ", RESP) is False
    finally:
        os.environ.pop("STRUCTURE_OBS_ENABLED", None)


def test_main_calls_the_hook_on_the_final_response_before_the_cache():
    ast.parse(_SRC)
    i_q = _SRC.index("_queue_training_record(background_tasks, _train_args, response)")
    i_o = _SRC.index("_queue_structure_observation(background_tasks, symbol, response)")
    i_c = _SRC.index("_cache_set_decide(symbol, response)", i_o)
    assert i_q < i_o < i_c
    assert "/api/structure/observations" in _SRC and "import structure_obs_hook as _so_hook" in _SRC


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
            n += 1
    print(f"{n} passed 0 failed")
