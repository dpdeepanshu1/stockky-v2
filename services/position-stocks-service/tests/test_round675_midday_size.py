"""round 675: midday half size (orders/midday_size.py) - 12:00-13:59 automatic entries are sized at MIDDAY_SIZE_MULT; it only shrinks, never blocks, never stacks."""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from orders import midday_size

IST = timezone(timedelta(hours=5, minutes=30))
MON = lambda h, m: datetime(2026, 10, 12, h, m, tzinfo=IST)      # an ordinary Monday
SAT = lambda h, m: datetime(2026, 10, 10, h, m, tzinfo=IST)
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def on(monkeypatch):
    monkeypatch.setattr(config, "MIDDAY_SIZE_ENABLED", True)
    monkeypatch.setattr(config, "MIDDAY_SIZE_MULT", 0.5)
    monkeypatch.setattr(config, "MIDDAY_SIZE_FROM_IST", "12:00")
    monkeypatch.setattr(config, "MIDDAY_SIZE_UNTIL_IST", "14:00")


def test_window_is_12_to_14_on_trading_days_only():
    assert midday_size.size_mult(MON(12, 0)) == 0.5 and midday_size.size_mult(MON(13, 59)) == 0.5
    assert midday_size.size_mult(MON(11, 59)) == 1.0 and midday_size.size_mult(MON(14, 0)) == 1.0
    assert midday_size.size_mult(MON(9, 30)) == 1.0
    assert midday_size.size_mult(SAT(12, 30)) == 1.0


def test_switch_off_gives_full_size(monkeypatch):
    monkeypatch.setattr(config, "MIDDAY_SIZE_ENABLED", False)
    assert midday_size.size_mult(MON(12, 30)) == 1.0 and not midday_size.active(MON(12, 30))


def test_multiplier_is_clamped_and_bad_values_fail_open(monkeypatch):
    monkeypatch.setattr(config, "MIDDAY_SIZE_MULT", 0.0)
    assert midday_size.size_mult(MON(12, 30)) == 0.1              # never a block: floor 0.1
    monkeypatch.setattr(config, "MIDDAY_SIZE_MULT", 3.0)
    assert midday_size.size_mult(MON(12, 30)) == 1.0               # never bigger than normal
    monkeypatch.setattr(config, "MIDDAY_SIZE_MULT", float("nan"))
    assert midday_size.size_mult(MON(12, 30)) == 1.0
    monkeypatch.setattr(config, "MIDDAY_SIZE_MULT", "abc")
    assert midday_size.size_mult(MON(12, 30)) == 1.0


def test_empty_or_reversed_window_shrinks_nothing(monkeypatch):
    monkeypatch.setattr(config, "MIDDAY_SIZE_FROM_IST", "14:00")
    monkeypatch.setattr(config, "MIDDAY_SIZE_UNTIL_IST", "12:00")
    assert midday_size.size_mult(MON(13, 0)) == 1.0
    monkeypatch.setattr(config, "MIDDAY_SIZE_FROM_IST", "13:00")
    monkeypatch.setattr(config, "MIDDAY_SIZE_UNTIL_IST", "13:00")
    assert midday_size.size_mult(MON(13, 0)) == 1.0


def test_garbage_times_fall_back_to_the_defaults(monkeypatch):
    monkeypatch.setattr(config, "MIDDAY_SIZE_FROM_IST", "noon")
    monkeypatch.setattr(config, "MIDDAY_SIZE_UNTIL_IST", "")
    assert midday_size.size_mult(MON(12, 30)) == 0.5 and midday_size.size_mult(MON(14, 30)) == 1.0


def test_config_defaults():
    src = (ROOT / "config.py").read_text(encoding="utf-8")
    assert re.search(r'MIDDAY_SIZE_ENABLED = _get_bool\("MIDDAY_SIZE_ENABLED", True\)', src)
    assert re.search(r'MIDDAY_SIZE_MULT = _get_float\("MIDDAY_SIZE_MULT", 0\.5\)', src)
    assert '"MIDDAY_SIZE_FROM_IST", "12:00"' in src and '"MIDDAY_SIZE_UNTIL_IST", "14:00"' in src


def test_both_entry_paths_use_it_and_it_never_blocks():
    long_src = (ROOT / "orders" / "entry.py").read_text(encoding="utf-8")
    short_src = (ROOT / "orders" / "short_entry.py").read_text(encoding="utf-8")
    assert "min(opening_protect.size_mult(), midday_size.size_mult())" in long_src          # lower of the two windows, no stacking
    assert "midday_size.size_mult()" in short_src and "_mid_mult / _sm" in short_src         # short: only the part the setup size does not already cover
    mod = (ROOT / "orders" / "midday_size.py").read_text(encoding="utf-8")
    assert "return None" not in mod                                                          # no skip reason: it can only shrink


def test_ps_has_a_read_only_db_latency_route():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    i = src.index('@app.get("/ops/db-latency")')
    body = src[i:i + 900]
    assert "def ops_db_latency()" in body and "latency_snapshot()" in body and "@app.post" not in body.split("def ops_db_latency()")[0]
