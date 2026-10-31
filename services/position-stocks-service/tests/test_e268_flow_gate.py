"""E268: position-stocks order-flow tape gate (orders/flow_gate.py)."""
import json

import config
from orders import flow_gate as fg

BLOCK = {"available": True, "reliable": True, "block": True, "reason": "ORDER_FLOW:3_of_6_against_long(delta,footprint,imbalance)",
         "against": 3, "for_count": 0, "checks": {"delta": "against"}, "coverage_pct": 60.0}
OK = {"available": True, "reliable": True, "block": False, "reason": "ok", "against": 1, "for_count": 2, "checks": {}}
UNRELIABLE = {"available": True, "reliable": False, "block": False, "reason": "too_few_intervals"}


class _Mode:
    def __init__(self, m, verdict):
        self.m, self.v = m, verdict

    def __enter__(self):
        self.old_mode, self.old_fetch = config.FLOW_GATE_MODE, fg.fetch_verdict
        config.FLOW_GATE_MODE = self.m
        fg.fetch_verdict = lambda symbol, side="LONG": self.v
        fg._reset_for_tests()

    def __exit__(self, *a):
        config.FLOW_GATE_MODE, fg.fetch_verdict = self.old_mode, self.old_fetch
        fg._reset_for_tests()


def test_decide_blocks_only_on_reliable_block():
    fg._reset_for_tests()
    assert fg.decide(BLOCK).startswith("ORDER_FLOW:")
    assert fg.decide(OK) is None and fg.decide(UNRELIABLE) is None and fg.decide(None) is None
    assert fg.decide({**UNRELIABLE, "block": True}) is None


def test_entry_check_live_refuses_and_stamps():
    with _Mode("live", BLOCK):
        reason, fj = fg.entry_check("ABC", "LONG", json.dumps({"imbalance_5": 0.2}))
        assert reason.startswith("ORDER_FLOW:")
        d = json.loads(fj)
        assert d["imbalance_5"] == 0.2 and d["tape"]["block"] is True


def test_entry_check_log_records_without_blocking():
    with _Mode("log", BLOCK):
        reason, fj = fg.entry_check("ABC", "LONG", None)
        assert reason is None and json.loads(fj)["tape"]["against"] == 3


def test_entry_check_off_changes_nothing():
    with _Mode("off", BLOCK):
        assert fg.entry_check("ABC", "LONG", "x") == (None, "x")


def test_no_tape_means_no_opinion():
    with _Mode("live", None):
        assert fg.entry_check("ABC", "LONG", "x") == (None, "x")
    with _Mode("live", UNRELIABLE):
        assert fg.entry_check("ABC", "LONG", None)[0] is None


def test_entry_check_never_raises():
    old = fg.fetch_verdict
    def boom(*a, **k):
        raise RuntimeError("down")
    fg.fetch_verdict = boom
    old_mode = config.FLOW_GATE_MODE
    config.FLOW_GATE_MODE = "live"
    try:
        assert fg.entry_check("ABC", "LONG", "x") == (None, "x")
    finally:
        fg.fetch_verdict, config.FLOW_GATE_MODE = old, old_mode


def test_record_switch_off_leaves_flow_json_alone():
    old = config.FLOW_RECORD
    config.FLOW_RECORD = False
    try:
        assert fg.attach('{"a":1}', OK) == '{"a":1}'
    finally:
        config.FLOW_RECORD = old


def test_typo_mode_is_off():
    old = config.FLOW_GATE_MODE
    config.FLOW_GATE_MODE = "lvie"
    try:
        assert fg.mode() == "off"
    finally:
        config.FLOW_GATE_MODE = old


def test_unreachable_market_data_is_no_opinion_and_cached_short():
    old = fg.fetch_verdict
    def boom(*a, **k):
        raise RuntimeError("down")
    fg.fetch_verdict = boom
    old_mode = config.FLOW_GATE_MODE
    config.FLOW_GATE_MODE = "live"
    try:
        assert fg.entry_check("ABC", "LONG", None) == (None, None)
        assert fg.stats()["errors"] == 1
    finally:
        fg.fetch_verdict, config.FLOW_GATE_MODE = old, old_mode


def test_gate_is_wired_into_attempt_entry_before_the_liquidity_gate():
    import inspect
    from orders import entry
    src = inspect.getsource(entry.attempt_entry)
    idx_flow = src.index("E268: intraday order-flow tape")
    idx_liq = src.index("# group333: volume / turnover floor")
    assert idx_flow < idx_liq


def test_e269_short_side_is_asked_for_and_refused_like_a_long():
    asked = []
    SHORT_BLOCK = {**BLOCK, "reason": "ORDER_FLOW:3_of_6_against_short(delta,footprint,imbalance)"}
    old_mode, old_fetch = config.FLOW_GATE_MODE, fg.fetch_verdict
    try:
        config.FLOW_GATE_MODE = "live"
        fg.fetch_verdict = lambda symbol, side="LONG": (asked.append(side) or SHORT_BLOCK)
        reason, fj = fg.entry_check("ABC", "SHORT", None)
        assert asked == ["SHORT"] and reason.endswith("_against_short(delta,footprint,imbalance)")
        assert json.loads(fj)["tape"]["block"] is True
    finally:
        config.FLOW_GATE_MODE, fg.fetch_verdict = old_mode, old_fetch
        fg._reset_for_tests()


def test_e269_merge_tape_copies_only_the_tape_and_never_raises():
    tape_json = json.dumps({"imbalance_5": 0.3, "tape": {"against": 1, "reliable": True}})
    out = json.loads(fg.merge_tape('{"trade_odds":{"p":0.5}}', tape_json))
    assert out["trade_odds"] == {"p": 0.5} and out["tape"] == {"against": 1, "reliable": True} and "imbalance_5" not in out
    assert json.loads(fg.merge_tape(None, tape_json))["tape"]["against"] == 1
    assert json.loads(fg.merge_tape({"a": 1}, {"tape": {"against": 2}}))["tape"]["against"] == 2
    for tj in (None, "", "not json", "{}", '{"tape": null}', 5):
        assert fg.merge_tape("keep", tj) == "keep"
    assert fg.merge_tape("keep", tape_json) == "keep"          # an unreadable existing flow_json is left alone, never overwritten
