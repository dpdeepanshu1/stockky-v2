"""E372: control/kit.py puts a switch back when its live apply fails half way (overlay_set and set); nothing is left on the new value."""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _kit():
    spec = importlib.util.spec_from_file_location("kit_e372", ROOT / "control" / "kit.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["kit_e372"] = m
    spec.loader.exec_module(m)
    return m


class _Cfg(types.ModuleType):
    fail_b = False

    def __setattr__(self, k, v):
        if k == "B" and self.fail_b:
            raise RuntimeError("cannot set B")
        super().__setattr__(k, v)


def _rig():
    for k in ("E372_LIMIT", "E372_MARKET_STATE_MODE"):   # set() writes os.environ: keep the tests independent
        os.environ.pop(k, None)
    kit = _kit()
    cfg = _Cfg("fakecfg_e372")
    cfg.A, cfg.B, cfg.MODE = 10, 10, "live"
    sys.modules["fakecfg_e372"] = cfg
    catalog = [
        kit.Flag(key="E372_MARKET_STATE_MODE", title="mode", group="g", kind="enum", help="", choices=("off", "shadow", "live"),
                 targets=(("fakecfg_e372", "MODE"),), live=True, shipped="live"),
        kit.Flag(key="E372_LIMIT", title="limit", group="g", kind="int", help="", lo=0, hi=100, looser="higher", live=True, overlay=True,
                 targets=(("fakecfg_e372", "A"), ("fakecfg_e372", "B")), shipped="10"),
    ]
    kit.MODE_KEY = "E372_MARKET_STATE_MODE"
    ctl = kit.Controller("svc", catalog, store=kit.Store(tempfile.mkdtemp()))
    return kit, cfg, ctl


def test_overlay_failure_restores_the_switch_that_failed_too():
    from datetime import datetime, timedelta, timezone
    kit, cfg, ctl = _rig()
    cfg.fail_b = True
    until = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    with pytest.raises(kit.ControlError) as e:
        ctl.overlay_set({"E372_LIMIT": 5}, until, "choppy", "v1", "tester")
    assert e.value.status == 500
    assert (cfg.A, cfg.B) == (10, 10)                      # A was rewritten before B failed: it must be back
    assert ctl._overlays == {} and not kit.ACTIVE


def test_set_with_a_failed_live_apply_leaves_the_running_value_alone():
    kit, cfg, ctl = _rig()
    cfg.fail_b = True
    out = ctl.set("E372_LIMIT", 20, "tester", confirm=True)
    assert out["applied_live"] is False and out["restart_required"] is True
    assert (cfg.A, cfg.B) == (10, 10)                      # the page says "after restart": the process must still run the old value


def test_a_clean_live_set_still_applies_both_targets():
    kit, cfg, ctl = _rig()
    out = ctl.set("E372_LIMIT", 20, "tester", confirm=True)
    assert out["applied_live"] is True and (cfg.A, cfg.B) == (20, 20)
