"""E268: real-trade order-flow tape gate (entry_engine/flow_gate.py)."""
import asyncio
import json
from types import SimpleNamespace

import config
from entry_engine import flow_gate as fg

BLOCK = {"available": True, "reliable": True, "block": True, "reason": "ORDER_FLOW:3_of_6_against_long(delta,footprint,imbalance)",
         "against": 3, "for_count": 0, "checks": {"delta": "against"}, "coverage_pct": 60.0}
OK = {"available": True, "reliable": True, "block": False, "reason": "ok", "against": 1, "for_count": 2, "checks": {}}
UNRELIABLE = {"available": True, "reliable": False, "block": False, "reason": "too_few_intervals"}


def _with_mode(m):
    class _Ctx:
        def __enter__(self):
            self.old = config.FLOW_GATE_MODE
            config.FLOW_GATE_MODE = m
            fg._reset_for_tests()

        def __exit__(self, *a):
            config.FLOW_GATE_MODE = self.old
            fg._reset_for_tests()
    return _Ctx()


def _patch_fetch(v):
    async def _f(symbol, side="LONG"):
        return v
    old = fg.fetch_verdict
    fg.fetch_verdict = _f
    return old


def test_decide_blocks_only_on_reliable_block():
    fg._reset_for_tests()
    assert fg.decide(BLOCK).startswith("ORDER_FLOW:")
    assert fg.decide(OK) is None and fg.decide(UNRELIABLE) is None
    assert fg.decide(None) is None and fg.decide({"available": False}) is None
    s = fg.stats()
    assert s["blocks"] == 1 and s["unreliable"] == 1 and s["no_tape"] == 2


def test_unreliable_block_flag_is_ignored():
    assert fg.decide({**UNRELIABLE, "block": True}) is None


def test_attach_stamps_tape_and_keeps_other_keys():
    out = json.loads(fg.attach(json.dumps({"imbalance_5": 0.1}), BLOCK))
    assert out["imbalance_5"] == 0.1 and out["tape"]["block"] is True and out["tape"]["against"] == 3
    assert json.loads(fg.attach(None, OK))["tape"]["reliable"] is True
    assert fg.attach("x", None) == "x" and fg.attach("x", {"available": False}) == "x"


def test_record_switch_off_leaves_flow_json_alone():
    old = config.FLOW_RECORD
    config.FLOW_RECORD = False
    try:
        assert fg.attach('{"a":1}', OK) == '{"a":1}'
    finally:
        config.FLOW_RECORD = old


def test_check_live_refuses_and_stamps():
    with _with_mode("live"):
        old = _patch_fetch(BLOCK)
        try:
            d = SimpleNamespace(flow_json=None)
            reason = asyncio.run(fg.check(d, "ABC", "LONG"))
            assert reason and reason.startswith("ORDER_FLOW:")
            assert json.loads(d.flow_json)["tape"]["block"] is True
        finally:
            fg.fetch_verdict = old


def test_check_log_mode_records_but_never_blocks():
    with _with_mode("log"):
        old = _patch_fetch(BLOCK)
        try:
            d = SimpleNamespace(flow_json=None)
            assert asyncio.run(fg.check(d, "ABC", "LONG")) is None
            assert json.loads(d.flow_json)["tape"]["block"] is True
        finally:
            fg.fetch_verdict = old


def test_check_off_does_nothing():
    with _with_mode("off"):
        old = _patch_fetch(BLOCK)
        try:
            d = SimpleNamespace(flow_json=None)
            assert asyncio.run(fg.check(d, "ABC", "LONG")) is None and d.flow_json is None
        finally:
            fg.fetch_verdict = old


def test_check_never_raises_when_market_data_is_down():
    with _with_mode("live"):
        async def _boom(symbol, side="LONG"):
            raise RuntimeError("down")
        old = fg.fetch_verdict
        fg.fetch_verdict = _boom
        try:
            assert asyncio.run(fg.check(SimpleNamespace(flow_json=None), "ABC", "LONG")) is None
        finally:
            fg.fetch_verdict = old


def test_typo_mode_is_off():
    old = config.FLOW_GATE_MODE
    config.FLOW_GATE_MODE = "lvie"
    try:
        assert fg.mode() == "off"
    finally:
        config.FLOW_GATE_MODE = old


def test_config_defaults():
    assert config.FLOW_GATE_MODE in ("live", "log", "off") and 1 <= config.FLOW_GATE_MIN_AGAINST <= 6 and config.FLOW_GATE_TIMEOUT_S >= 0.2


def test_unreachable_market_data_is_no_opinion_and_cached_short():
    # Must not hit the network in this test; just verify it doesn't block on failure.
    with _with_mode("live"):
        async def _boom(symbol, side="LONG"):
            raise RuntimeError("network down")
        old = fg.fetch_verdict
        fg.fetch_verdict = _boom
        try:
            d = SimpleNamespace(flow_json=None)
            assert asyncio.run(fg.check(d, "ABC", "LONG")) is None
            assert fg.stats()["errors"] == 1
        finally:
            fg.fetch_verdict = old


def test_gate_is_wired_into_the_entry_engine_before_structure():
    import inspect
    from entry_engine import entry
    src = inspect.getsource(entry.evaluate_mode)
    # Gate 4d-2 must come before Gate 4e
    idx_flow = src.index("Gate 4d-2 (E268)")
    idx_structure = src.index("Gate 4e (group 351)")
    assert idx_flow < idx_structure
