"""Round 543 (E568, S3): the auto-calibration settings (master switch, dry-run, pinned list) get ONE audit line per real change. Offline, stdlib only.
Plain asserts or pytest (python tests/test_round543_autocalib_audit.py)."""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests"))
import autocalib as A  # noqa: E402
from test_autocalib import FakeEvidence, TEST_RULES, TEST_RULE_KEY, make, row, RT, PS  # noqa: E402


def _ac(**kw):
    fl = {RT: [row(TEST_RULE_KEY, effective="true", looser="on")], PS: []}
    return make(fl, FakeEvidence(), rules=TEST_RULES, **kw)


def test_a_change_writes_one_audit_line_and_a_no_op_writes_none():
    ac, _, clock = _ac(enabled=True, dry_run=True)
    assert ac.status(with_coverage=False)["audit"] == []
    ac.configure(dry_run=True, by="admin")                         # already true: nothing changed
    ac.configure(enabled=True, by="admin")
    ac.configure(pinned=[], by="admin")
    assert ac.status(with_coverage=False)["audit"] == [], "a call that changes nothing leaves no line"
    ac.configure(dry_run=False, by="admin")
    a = ac.status(with_coverage=False)["audit"]
    assert len(a) == 1 and a[0]["by"] == "admin" and a[0]["reset"] is False and a[0]["changes"] == {"dry_run": {"from": True, "to": False}}
    assert a[0]["at_ist"].startswith("2025-") or a[0]["at_ist"].startswith("2026-") or a[0]["at_ist"][:2] == "20"


def test_pin_and_unpin_are_audited_per_switch_newest_first():
    ac, _, clock = _ac(enabled=True, dry_run=True)
    ac.configure(pinned=[TEST_RULE_KEY], by="admin")
    clock["t"] += 60
    ac.configure(pinned=[], by="admin")
    a = ac.status(with_coverage=False)["audit"]
    assert [x["changes"] for x in a] == [{f"pin {TEST_RULE_KEY}": {"from": "pinned", "to": "unpinned"}}, {f"pin {TEST_RULE_KEY}": {"from": "unpinned", "to": "pinned"}}], "newest first"
    assert a[0]["at"] > a[1]["at"]


def test_several_changes_in_one_call_are_one_line_and_a_refused_call_writes_nothing():
    ac, _, _ = _ac(enabled=True, dry_run=True)
    ac.configure(enabled=False, dry_run=False, pinned=[TEST_RULE_KEY], by="x")
    a = ac.status(with_coverage=False)["audit"]
    assert len(a) == 1 and set(a[0]["changes"]) == {"enabled", "dry_run", f"pin {TEST_RULE_KEY}"}
    try:
        ac.configure(pinned=["BROKER_STOP_ENABLED"], by="x")
        raise AssertionError("pinned an unknown switch")
    except ValueError:
        pass
    assert len(ac.status(with_coverage=False)["audit"]) == 1


def test_audit_survives_a_restart_and_is_capped():
    ac, _, clock = _ac(enabled=True, dry_run=True)
    for i in range(A.AUDIT_KEEP + 7):
        clock["t"] += 1
        ac.configure(dry_run=bool(i % 2), by="admin")
    assert len(ac.state["audit"]) == A.AUDIT_KEEP
    ac2 = A.AutoCalibrator(ac.path, ac.control, ac.evidence, rules=TEST_RULES)
    assert ac2.state["audit"] == ac.state["audit"]
    assert len(ac2.status(with_coverage=False)["audit"]) == 15, "the status shows the newest 15"


def test_status_still_carries_protected_and_an_old_state_file_has_no_audit():
    ac, _, _ = _ac(enabled=True, dry_run=True)
    st = ac.status(with_coverage=False)
    assert "BROKER_STOP_ENABLED" in st["protected"] and st["audit"] == []
    ac.state.pop("audit"); ac._save()                                    # a state file written before round 543
    ac2 = A.AutoCalibrator(ac.path, ac.control, ac.evidence, rules=TEST_RULES)
    assert ac2.status(with_coverage=False)["audit"] == []
    ac2.configure(dry_run=False, by="a")
    assert len(ac2.status(with_coverage=False)["audit"]) == 1


def test_the_old_positional_call_still_works():
    ac, _, _ = _ac(enabled=True, dry_run=True)
    assert ac.configure(True, False, [TEST_RULE_KEY])["pinned"] == [TEST_RULE_KEY]
    assert ac.status(with_coverage=False)["audit"][0]["by"] == "-"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok -", name)
