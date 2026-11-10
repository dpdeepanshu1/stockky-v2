"""Round 288: "clear" (back to the .env / default value) on a time switch is refused when that value cannot work with the other times; the recommended preset moves no time. No network."""
from __future__ import annotations

import pytest

import config
from control import kit
from control.catalog import CATALOG
from control.guard import FnoController, restored_time


@pytest.fixture()
def ctl(tmp_path, monkeypatch):
    for key, val in (("FNO_ENTRY_START", "09:30"), ("FNO_ENTRY_END", "15:00"), ("FNO_SQUAREOFF_AT", "15:15")):
        monkeypatch.setattr(config, key, val)
        monkeypatch.setenv(key, val)                         # the environment value the page will later be asked to go back to
    return FnoController("fno-options-service", CATALOG, kit.Store(str(tmp_path)))


def _squeeze(ctl):
    ctl.set("FNO_ENTRY_END", "14:00", "t", confirm=True)
    ctl.set("FNO_SQUAREOFF_AT", "14:30", "t", confirm=True)  # fits: entry closes 14:00, out at 14:30


def test_clear_that_would_break_the_times_is_refused_and_nothing_changes(ctl):
    _squeeze(ctl)
    with pytest.raises(kit.ControlError) as e:
        ctl.clear("FNO_ENTRY_END", "t")                      # env value 15:00 is after the 14:30 square-off
    assert e.value.status == 400 and "NOT done" in e.value.detail and "FNO_SQUAREOFF_AT" in e.value.detail
    assert config.FNO_ENTRY_END == "14:00" and "FNO_ENTRY_END" in ctl.store.overrides()


def test_clear_that_fits_is_applied(ctl):
    _squeeze(ctl)
    r = ctl.clear("FNO_SQUAREOFF_AT", "t")                   # back to 15:15: fits with entry end 14:00
    assert r["cleared"] and config.FNO_SQUAREOFF_AT == "15:15"


def test_clear_order_that_repairs_works(ctl):
    _squeeze(ctl)
    ctl.clear("FNO_SQUAREOFF_AT", "t")                       # out first, then the entry end may go back
    r = ctl.clear("FNO_ENTRY_END", "t")
    assert r["cleared"] and config.FNO_ENTRY_END == "15:00"


def test_clear_of_an_already_bad_state_repairs_but_never_stays_impossible(ctl, monkeypatch):
    _squeeze(ctl)
    monkeypatch.setattr(config, "FNO_ENTRY_END", "15:10")    # already impossible next to the 14:30 square-off (set from some other route)
    with pytest.raises(kit.ControlError):                    # going back to 15:00 is still after 14:30: nothing repaired, refused
        ctl.clear("FNO_ENTRY_END", "t")
    r = ctl.clear("FNO_SQUAREOFF_AT", "t")                   # back to 15:15: now 15:10 fits, so this repairs it
    assert r["cleared"] and config.FNO_SQUAREOFF_AT == "15:15"


def test_no_override_means_no_refusal(ctl):
    assert ctl.clear("FNO_ENTRY_START", "t")["cleared"] is False


def test_restored_value_falls_back_to_the_shipped_default():
    f = next(x for x in CATALOG if x.key == "FNO_ENTRY_END")
    assert restored_time(f, {"orig_env": None}) == "14:30"
    assert restored_time(f, {"orig_env": "15:00"}) == "15:00"
    assert restored_time(f, {"orig_env": "bogus"}) == "14:30"


def test_other_switches_clear_as_before(ctl):
    ctl.set("FNO_SL_PCT", "20", "t")
    assert ctl.clear("FNO_SL_PCT", "t")["cleared"]


def test_the_recommended_preset_moves_no_time_switch(ctl):
    plan = ctl.preset_recommended("t", dry_run=True)["changes"]
    assert not [p for p in plan if p["key"] in ("FNO_ENTRY_START", "FNO_ENTRY_END", "FNO_SQUAREOFF_AT")]
    assert not [f.key for f in CATALOG if f.kind == "time" and f.recommended is not None]
