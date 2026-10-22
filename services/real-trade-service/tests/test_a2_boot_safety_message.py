"""A2: the boot-time broker-stop safety alert text (pure helper, no I/O)."""
from execution.broker_stop import boot_safety_message


def test_quiet_when_stop_on_and_store_persistent():
    assert boot_safety_message(True, True) is None


def test_quiet_when_persistence_unknown_and_stop_on():
    assert boot_safety_message(True, None) is None


def test_warns_when_stop_off():
    m = boot_safety_message(False, True)
    assert m and "BROKER_STOP_ENABLED is OFF" in m and "NOT saved" not in m


def test_warns_when_store_not_persistent():
    m = boot_safety_message(True, False)
    assert m and "NOT saved" in m and "OFF" not in m


def test_both_warnings_together():
    m = boot_safety_message(False, False)
    assert "OFF" in m and "NOT saved" in m
