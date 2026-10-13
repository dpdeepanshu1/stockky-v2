"""group349: ms_engine_switch picks legacy by default, the adapter when flagged, and falls back on error."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from screening import ms_engine_switch as sw
from screening import market_structure as ms
from tests.test_group349_compat_board import _walk


def test_default_is_legacy():
    config.BOARD_USE_MS_ENGINE = False
    assert sw.engine_active() is False
    b = _walk(1)
    z, s = sw.zones_and_swings(b, 1.5, False)
    assert z == ms.find_zones(b, atr_mult=1.5, full_range=False) and s == ms.swings(b)


def test_flag_uses_adapter_and_same_swings():
    config.BOARD_USE_MS_ENGINE = True
    try:
        assert sw.engine_active() is True
        bars = _walk(2)
        z, s = sw.zones_and_swings(bars, 1.5, False)
        z0 = ms.find_zones(bars, atr_mult=1.5, full_range=False)
        assert s == ms.swings(bars)          # swings identical to legacy
        assert len(z) <= len(z0)             # adapter never adds a zone legacy would not find
    finally:
        config.BOARD_USE_MS_ENGINE = False


def test_error_in_adapter_falls_back():
    config.BOARD_USE_MS_ENGINE = True
    real = sw._cb

    class Boom:
        def find_zones(self, *a, **k): raise RuntimeError("x")
        def swings(self, *a, **k): raise RuntimeError("x")
    sw._cb = Boom()
    try:
        b = _walk(3)
        z, s = sw.zones_and_swings(b, 1.5, False)
        assert z == ms.find_zones(b, atr_mult=1.5, full_range=False) and s == ms.swings(b)
    finally:
        sw._cb = real
        config.BOARD_USE_MS_ENGINE = False


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f(); print("ok", n)
