"""E367: the daily loss check in reserve_capital also counts the open positions' unrealized P&L (fresh ticks only); it only blocks an entry."""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class _Q:
    def __init__(self, rows): self.rows = rows
    def filter(self, *a, **k): return self
    def all(self): return list(self.rows)


class _DB:
    def __init__(self, rows): self.rows = rows
    def query(self, *_): return _Q(self.rows)


def _pos(sym, side, entry, qty):
    return types.SimpleNamespace(symbol=sym, side=side, entry_price=entry, quantity=qty)


def _run(rows, ticks, fn, *args):
    fake_models = types.SimpleNamespace(ScalpPosition=types.SimpleNamespace(status="x"))
    with mock.patch.dict(sys.modules, {"models": fake_models}):
        from capital import open_pnl
        with mock.patch.object(open_pnl, "_tick", lambda s: ticks.get(s, (None, None))):
            return getattr(open_pnl, fn)(_DB(rows), *args)


def test_long_and_short_unrealized_are_summed_with_the_right_sign():
    rows = [_pos("A", "LONG", 100.0, 10), _pos("B", "SHORT", 200.0, 5)]
    assert _run(rows, {"A": (95.0, 1.0), "B": (190.0, 1.0)}, "open_unrealized_pnl") == -50.0 + 50.0


def test_stale_or_missing_ticks_count_as_zero():
    rows = [_pos("A", "LONG", 100.0, 10), _pos("B", "LONG", 100.0, 10)]
    assert _run(rows, {"A": (50.0, 99.0)}, "open_unrealized_pnl") == 0.0


def test_bad_rows_are_skipped_and_a_failing_read_is_zero():
    rows = [_pos("A", "LONG", float("nan"), 10), _pos("B", "LONG", 100.0, 0), _pos("C", "LONG", 100.0, 1)]
    assert _run(rows, {"A": (1.0, 1.0), "B": (1.0, 1.0), "C": (90.0, 1.0)}, "open_unrealized_pnl") == -10.0
    from capital import open_pnl
    assert open_pnl.open_unrealized_pnl(object()) == 0.0


def test_day_loss_adds_realized_and_open_and_only_a_net_loss_counts():
    rows = [_pos("A", "LONG", 100.0, 100)]
    assert abs(_run(rows, {"A": (97.0, 1.0)}, "day_loss_pct_with_open", -1000.0, 100000.0) - 1.3) < 1e-9
    assert _run(rows, {"A": (110.0, 1.0)}, "day_loss_pct_with_open", -500.0, 100000.0) == 0.0
    assert _run(rows, {}, "day_loss_pct_with_open", -500.0, 0.0) == 0.0


def test_ledger_check_is_wired_flagged_and_does_not_trip_the_kill_switch():
    src = (ROOT / "capital" / "ledger.py").read_text(encoding="utf-8")
    i = src.index("E367: the day's loss counts")
    seg = src[i:i + 900]
    assert "DAILY_LOSS_COUNTS_UNREALIZED" in seg and "return None" in seg
    assert "daily_loss_kill_switch_tripped" not in seg
    cfg = (ROOT / "config.py").read_text(encoding="utf-8")
    assert 'DAILY_LOSS_COUNTS_UNREALIZED = _get_bool("DAILY_LOSS_COUNTS_UNREALIZED", True)' in cfg
