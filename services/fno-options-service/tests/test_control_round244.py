"""Round 244 (calibration audit): the control kit's store re-probes after a failed write and keeps the reason; the CAPITAL_ALLOC_MODE switch is a valid catalog entry that matches config."""
from __future__ import annotations

import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from control import kit
from control.catalog import CATALOG


def test_catalog_entries_are_valid_and_capital_switch_matches_config():
    for f in CATALOG:
        assert kit.validate_flag(f) == [], f.key
    f = next(x for x in CATALOG if x.key == "CAPITAL_ALLOC_MODE")
    assert kit.canonical(f, f.shipped) == kit.canonical(f, config.CAPITAL_ALLOC_MODE) or os.environ.get("CAPITAL_ALLOC_MODE")
    assert f.choices == ("off", "shadow", "live") and f.live


def test_capital_switch_loosening_needs_confirm_and_applies_live(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CAPITAL_ALLOC_MODE", "shadow")
    c = kit.Controller("fno-options-service", CATALOG, kit.Store(str(tmp_path)))
    with pytest.raises(kit.ControlError) as e:
        c.set("CAPITAL_ALLOC_MODE", "live", "t")                       # shadow -> live changes sizing: a LOOSENING, so it asks
    assert e.value.status == 409
    r = c.set("CAPITAL_ALLOC_MODE", "live", "t", confirm=True)
    assert r["applied_live"] and config.CAPITAL_ALLOC_MODE == "live"
    r = c.set("CAPITAL_ALLOC_MODE", "off", "t")                        # tightening needs no confirm
    assert config.CAPITAL_ALLOC_MODE == "off"
    c.clear("CAPITAL_ALLOC_MODE", "t")
    monkeypatch.delenv("CAPITAL_ALLOC_MODE", raising=False)


def test_store_keeps_the_reason_and_saves_again_once_the_disk_recovers(tmp_path):
    d = tmp_path / "ctl"
    d.mkdir()
    s = kit.Store(str(d))
    assert s.persistent
    os.chmod(d, stat.S_IRUSR | stat.S_IXUSR)                           # the folder becomes read-only
    if os.access(d, os.W_OK):                                          # running as root: permissions do not bite, simulate the failure instead
        pytest.skip("cannot make a folder unwritable as root")
    s.put("A", {"value": "1"})
    assert s.persistent is False and s.last_error
    os.chmod(d, stat.S_IRWXU)                                          # the disk recovers
    s.put("B", {"value": "2"})
    assert s.persistent is True and os.path.exists(d / "overrides.json")


def test_store_reprobe_after_a_failed_save_without_chmod(tmp_path, monkeypatch):
    s = kit.Store(str(tmp_path))
    assert s.persistent
    real = kit.tempfile.mkstemp
    boom = {"on": True}
    def flaky(*a, **k):
        if boom["on"]:
            raise OSError("No space left on device")
        return real(*a, **k)
    monkeypatch.setattr(kit.tempfile, "mkstemp", flaky)
    s.put("A", {"value": "1"})
    assert s.persistent is False and "No space" in (s.last_error or "")
    boom["on"] = False
    s.put("B", {"value": "2"})
    assert s.persistent is True
    assert set(kit.Store(str(tmp_path)).overrides()) == {"A", "B"}      # both are on disk now (the first was kept in memory and saved with the second)


def test_unwritable_state_dir_at_boot_shows_its_reason(tmp_path):
    f = tmp_path / "afile"
    f.write_text("x")
    s = kit.Store(str(f / "sub"))                                      # a path below a file can never be created
    assert s.persistent is False and s.last_error
