"""Round 222: a previous close on another price scale (split / bonus day) must not throw away the whole exchange day-stats frame."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from feed import ws_client  # noqa: E402


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(ws_client, "_day_stats", {})
    monkeypatch.setattr(ws_client, "_day_stats_accepted", 0)
    monkeypatch.setattr(ws_client, "_day_stats_rejected", 0)
    monkeypatch.setattr(ws_client, "_day_stats_prev_dropped", 0)
    monkeypatch.setattr(ws_client, "_day_stats_warned", False)


def test_off_scale_previous_close_keeps_open_high_low():
    assert ws_client._accept_day_stats("KMEW", (641.0, 647.9, 606.0, 3143.3), 608.3) is True
    assert ws_client.get_day_stats("KMEW") == (641.0, 647.9, 606.0, None)
    assert ws_client._day_stats_prev_dropped == 1 and ws_client._day_stats_rejected == 0
    assert ws_client.ws_status()["day_stats_prev_dropped"] == 1


def test_garbled_layout_is_still_rejected():
    assert ws_client._accept_day_stats("X", (1e9, 105.0, 98.0, 99.0), 100.5) is False
    assert ws_client._accept_day_stats("Y", (100.0, 97.0, 98.0, 3000.0), 100.5) is False   # high < low: layout, not scale
    assert ws_client._day_stats_rejected == 2 and ws_client._day_stats_prev_dropped == 0


def test_normal_frame_unchanged():
    assert ws_client._accept_day_stats("T", (100.0, 105.0, 98.0, 99.0), 100.5)
    assert ws_client.get_day_stats("T") == (100.0, 105.0, 98.0, 99.0)
    assert ws_client._day_stats_prev_dropped == 0


def test_engine_day_change_is_unknown_without_previous_close():
    from screening import engine
    ws_client._accept_day_stats("KMEW", (641.0, 647.9, 606.0, 3143.3), 608.3)
    assert engine._day_change_pct("KMEW", 608.3) is None
