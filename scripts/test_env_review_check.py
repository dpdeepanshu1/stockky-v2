"""scripts/test_env_review_check.py - plain-assert tests for env_review_check.py (group 475). Run: python3 scripts/test_env_review_check.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import env_review_check as c


def kinds(text):
    from env_structure_check import parse_env
    return [f.split()[0] + " " + f.split()[1].split("=")[0] for f in c.check(parse_env(text)) if not f.startswith("INFO")]


def test_defaults_are_clean():
    assert kinds("OVERNIGHT_HOLD_ENABLED=false\nENTRY_LAST_TIME_IST=14:00\nENTRY_MIN_QTY=3\nSTOP_OVERSHOOT_ALLOWANCE_PCT=1.0\n"
                 "STOPOUT_GUARD_ENABLED=true\nSTOPOUT_DAY_PAUSE_N=3\nBROKER_STOP_ENABLED=true\nORB_MODE=shadow\n") == []


def test_loosening_values():
    k = kinds("OVERNIGHT_HOLD_ENABLED=true\nENTRY_LAST_TIME_IST=\nENTRY_MIN_QTY=1\nSTOP_OVERSHOOT_ALLOWANCE_PCT=0\n"
              "STOPOUT_GUARD_ENABLED=false\nSTOPOUT_DAY_PAUSE_N=0\nBROKER_STOP_ENABLED=false\n")
    assert k == ["LOOSENS OVERNIGHT_HOLD_ENABLED", "LOOSENS ENTRY_LAST_TIME_IST",
                 "LOOSENS STOP_OVERSHOOT_ALLOWANCE_PCT", "LOOSENS ENTRY_MIN_QTY", "LOOSENS STOPOUT_GUARD_ENABLED", "LOOSENS STOPOUT_DAY_PAUSE_N",
                 "LOOSENS BROKER_STOP_ENABLED"]


def test_tightening_and_cutoff_direction():
    assert kinds("ENTRY_LAST_TIME_IST=13:30\nENTRY_MIN_QTY=5\nSTOPOUT_DAY_PAUSE_N=2\n") == [
        "TIGHTENS ENTRY_LAST_TIME_IST", "TIGHTENS ENTRY_MIN_QTY", "TIGHTENS STOPOUT_DAY_PAUSE_N"]
    assert kinds("ENTRY_LAST_TIME_IST=14:45\n") == ["LOOSENS ENTRY_LAST_TIME_IST"]


def test_broker_stop_missing_is_info_and_strict_exit(tmp=None):
    assert any(f.startswith("INFO  BROKER_STOP_ENABLED") for f in c.check({}))
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
        fh.write("OVERNIGHT_HOLD_ENABLED=true\n")
    assert c.main([fh.name, "--strict"]) == 1
    assert c.main([fh.name]) == 0
    assert c.main(["/nonexistent/.env"]) == 2
    os.unlink(fh.name)


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("PASS", n)
