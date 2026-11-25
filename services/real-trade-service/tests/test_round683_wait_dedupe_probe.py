"""Round 683: the insert probe counts a WAIT row written without passing wait_dedupe.is_repeat, and stays quiet for a checked row."""
import wait_dedupe as wd


class _Row:
    def __init__(self, action="WAIT", decision_type="ENTRY", symbol="ABC"):
        self.action, self.decision_type, self.symbol = action, decision_type, symbol
        self.mode, self.gate_tag, self.reasoning, self.risk_verdict, self.risk_verdict_reason = "REAL", "structure", "r", None, None


def test_unchecked_wait_is_counted():
    wd.reset_for_tests()
    wd._probe_insert(None, None, _Row())
    st = wd.status()
    assert st["unchecked"] == 1 and st["unchecked_samples"][0]["symbol"] == "ABC"


def test_checked_wait_is_not_counted():
    wd.reset_for_tests()
    r = _Row()
    wd.is_repeat(r, now=1.0)
    wd._probe_insert(None, None, r)
    assert wd.status()["unchecked"] == 0


def test_enter_and_non_entry_rows_ignored():
    wd.reset_for_tests()
    wd._probe_insert(None, None, _Row(action="ENTER"))
    wd._probe_insert(None, None, _Row(decision_type="EXIT"))
    assert wd.status()["unchecked"] == 0


def test_samples_are_capped():
    wd.reset_for_tests()
    for i in range(100):
        wd._probe_insert(None, None, _Row(symbol=f"S{i}"))
    st = wd.status()
    assert st["unchecked"] == 100 and len(st["unchecked_samples"]) == wd._MAX_SAMPLES
