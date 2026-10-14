"""group351: ms_engine_switch counters and the /board/status ms_engine block."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from screening import ms_engine_switch as sw
from tests.test_group349_compat_board import _walk


def setup_function(_):
    sw.reset_stats()
    config.BOARD_USE_MS_ENGINE = False


def teardown_function(_):
    config.BOARD_USE_MS_ENGINE = False


def test_legacy_calls_counted():
    sw.zones_and_swings(_walk(1), 1.5, False)
    st = sw.stats()
    assert st["legacy_calls"] == 1 and st["engine_calls"] == 0 and st["active"] is False and st["requested"] is False


def test_engine_calls_counted_and_version_reported():
    config.BOARD_USE_MS_ENGINE = True
    sw.zones_and_swings(_walk(2), 1.5, False)
    st = sw.stats()
    assert st["engine_calls"] == 1 and st["fallbacks"] == 0 and st["active"] is True and st["engine_version"]


def test_fallback_counted_with_last_error():
    config.BOARD_USE_MS_ENGINE = True
    real = sw._cb

    class Boom:
        def find_zones(self, *a, **k): raise RuntimeError("kaboom")
        def swings(self, *a, **k): raise RuntimeError("kaboom")
    sw._cb = Boom()
    try:
        sw.zones_and_swings(_walk(3), 1.5, False)
    finally:
        sw._cb = real
    st = sw.stats()
    assert st["fallbacks"] == 1 and st["legacy_calls"] == 1 and "kaboom" in st["last_error"]


def test_requested_but_not_vendored_is_inactive():
    config.BOARD_USE_MS_ENGINE = True
    real = sw._cb
    sw._cb = None
    try:
        st = sw.stats()
        assert st["requested"] is True and st["vendored"] is False and st["active"] is False
    finally:
        sw._cb = real
