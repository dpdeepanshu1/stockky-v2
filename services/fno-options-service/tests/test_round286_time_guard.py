"""Round 286: the Calibration page refuses a time that cannot work with the other times, instead of saving it ("applied now") and leaving the engine to skip every entry. No network."""
from __future__ import annotations

import pytest

import config
from control import kit
from control.catalog import CATALOG
from control.guard import FnoController, new_time_problems


@pytest.fixture()
def ctl(tmp_path, monkeypatch):
    for key, val in (("FNO_ENTRY_START", "09:30"), ("FNO_ENTRY_END", "14:30"), ("FNO_SQUAREOFF_AT", "15:15")):
        monkeypatch.setattr(config, key, val)
        monkeypatch.setenv(key, val)                         # the kit writes os.environ: monkeypatch puts it back
    return FnoController("fno-options-service", CATALOG, kit.Store(str(tmp_path)))


def test_entry_end_after_the_squareoff_is_refused_and_nothing_is_saved(ctl):
    with pytest.raises(kit.ControlError) as e:
        ctl.set("FNO_ENTRY_END", "15:20", "t", confirm=True)
    assert e.value.status == 400 and "NOT saved" in e.value.detail and "FNO_SQUAREOFF_AT" in e.value.detail
    assert config.FNO_ENTRY_END == "14:30" and "FNO_ENTRY_END" not in ctl.store.overrides()


def test_squareoff_before_the_last_entry_is_refused(ctl):
    with pytest.raises(kit.ControlError) as e:
        ctl.set("FNO_SQUAREOFF_AT", "14:00", "t", confirm=True)
    assert e.value.status == 400 and config.FNO_SQUAREOFF_AT == "15:15"


def test_entry_start_after_the_entry_end_is_refused(ctl):
    with pytest.raises(kit.ControlError) as e:
        ctl.set("FNO_ENTRY_START", "14:45", "t", confirm=True)
    assert e.value.status == 400 and "opens" in e.value.detail and config.FNO_ENTRY_START == "09:30"


def test_a_squareoff_before_the_expiry_day_last_entry_is_refused(ctl):
    # the expiry-day last entry is 13:30: a regular square-off at 13:10 (with the entry window closed at 13:00) would close expiry-day trades before the window ends
    ctl.set("FNO_ENTRY_END", "13:00", "t", confirm=True)
    with pytest.raises(kit.ControlError) as e:
        ctl.set("FNO_SQUAREOFF_AT", "13:10", "t", confirm=True)
    assert e.value.status == 400 and "expiry-day" in e.value.detail and config.FNO_SQUAREOFF_AT == "15:15"


def test_changes_that_fit_are_applied_live(ctl):
    r = ctl.set("FNO_ENTRY_END", "14:00", "t", confirm=True)
    assert r["applied_live"] and config.FNO_ENTRY_END == "14:00"
    r = ctl.set("FNO_ENTRY_START", "09:45", "t", confirm=True)
    assert r["applied_live"] and config.FNO_ENTRY_START == "09:45"


def test_a_bad_value_still_gets_the_kits_own_message(ctl):
    with pytest.raises(kit.ControlError) as e:
        ctl.set("FNO_ENTRY_END", "25:99", "t", confirm=True)
    assert e.value.status == 400 and "HH:MM" in e.value.detail


def test_an_already_bad_state_can_be_repaired_from_the_page(ctl, monkeypatch):
    monkeypatch.setattr(config, "FNO_ENTRY_END", "15:20")                    # e.g. set through the environment at boot
    assert new_time_problems("FNO_ENTRY_END", "14:30") == []                 # moving it back fixes it: allowed
    assert new_time_problems("FNO_ENTRY_START", "09:40") == []               # an unrelated change adds no problem: allowed
    assert new_time_problems("FNO_SQUAREOFF_AT", "15:25") == []              # moving the other end out: allowed
    assert new_time_problems("FNO_ENTRY_END", "15:18")                       # still impossible: refused


def test_other_switches_are_not_touched_by_the_guard(ctl):
    assert new_time_problems("FNO_SL_PCT", "25") == []
    r = ctl.set("FNO_SL_PCT", "20", "t")
    assert r["ok"] and config.FNO_SL_PCT == 20.0
    ctl.clear("FNO_SL_PCT", "t")


def test_the_service_mounts_the_guarded_controller(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FNO_RUNNER", "0")
    import main
    assert isinstance(main._control, FnoController)
