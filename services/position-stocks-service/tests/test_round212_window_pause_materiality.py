"""Round 212: a window is paused for losing only when its net loss is material (live log: 2/8 wins, net Rs -8 paused the whole 'day' window)."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

import config
from orders import window_health as wh
from orders import review_stats, trade_stats


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    for k, v in dict(WINDOW_AUTODISABLE_MIN_TRADES=8, WINDOW_AUTODISABLE_MAX_WIN_RATE_PCT=35.0,
                     WINDOW_AUTODISABLE_MIN_NET_LOSS_AVG_TRADES=1.0).items():
        monkeypatch.setattr(config, k, v, raising=False)
    monkeypatch.setattr(wh, "_closed", lambda rows: list(rows))     # rows below are already-settled stand-ins


def _rows(pnls, window="day"):
    return [SimpleNamespace(realized_pnl=p, window_source=window) for p in pnls]


def test_breakeven_low_win_rate_is_not_paused():
    # 2 wins of Rs 60, 6 small losses of about Rs 21 -> 25% wins, net -6: breakeven, not a losing window
    out = wh.evaluate(_rows([60, 60, -21, -21, -21, -21, -21, -21]))
    assert out["day"]["total_pnl"] == -6 and out["day"]["disabled"] is False


def test_material_loss_still_pauses():
    # a second, profitable window so the "at least one window trades" spare-the-best rule (window_health.evaluate) does not un-pause the only window
    out = wh.evaluate(_rows([10, 10, -40, -40, -40, -40, -40, -40]) + _rows([30, 30, 10, 10, 10, 10, 10, 10], "5m"))      # day: 25% wins, net -220 on an average trade of ~33
    assert out["day"]["disabled"] is True and "2/8 wins" in out["day"]["reason"] and out["5m"]["disabled"] is False


def test_the_only_window_is_spared_even_when_it_loses_materially():
    out = wh.evaluate(_rows([10, 10, -40, -40, -40, -40, -40, -40]))      # same losing window alone: the spare rule keeps one window trading
    assert out["day"]["disabled"] is False


def test_zero_setting_restores_old_rule(monkeypatch):
    monkeypatch.setattr(config, "WINDOW_AUTODISABLE_MIN_NET_LOSS_AVG_TRADES", 0.0, raising=False)
    # a second, profitable window so the "at least one window trades" spare-the-best rule does not hide the result
    out = wh.evaluate(_rows([60, 60, -21, -21, -21, -21, -21, -21]) + _rows([30, 30, 10, 10, 10, 10, 10, 10], "5m"))
    assert out["day"]["disabled"] is True
