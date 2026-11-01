"""E283: the pre-market plan keeps refreshing after 09:20 (every PREMARKET_REFRESH_MIN minutes until 10:30) so S2 / S3 read fresh events and sector strength."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from screening import premarket_plan as pm  # noqa: E402
from tz_utils import IST  # noqa: E402


def at(h, m):
    return datetime(2026, 11, 2, h, m, tzinfo=IST)          # a Monday


def _built(minute):
    pm.reset()
    pm.due(at(8, 30))                                         # sets today's day
    pm._plan["builds"] = 3
    pm._plan["last_minute"] = minute


def test_after_0920_every_five_minutes_until_1030(monkeypatch):
    monkeypatch.setattr(config, "PREMARKET_REFRESH_MIN", 5, raising=False)
    monkeypatch.setattr(config, "PREMARKET_PLAN_ENABLED", True, raising=False)
    _built(9 * 60 + 19)
    due = [f"{h:02d}:{m:02d}" for h in (9, 10, 11) for m in range(60) if (pm._plan.update(last_minute=None) or pm.due(at(h, m))) and (h, m) >= (9, 20)]
    assert due[0] == "09:20" and due[1] == "09:25" and due[-1] == "10:25"
    assert "10:30" not in due and "11:00" not in due and len(due) == 14


def test_once_per_minute_marker_still_blocks_a_second_build(monkeypatch):
    monkeypatch.setattr(config, "PREMARKET_REFRESH_MIN", 5, raising=False)
    _built(9 * 60 + 25)
    assert not pm.due(at(9, 25))                              # already built this minute
    assert not pm.due(at(9, 26)) and pm.due(at(9, 30))


def test_zero_keeps_the_old_stop_at_0920(monkeypatch):
    monkeypatch.setattr(config, "PREMARKET_REFRESH_MIN", 0, raising=False)
    _built(9 * 60 + 19)
    assert not pm.due(at(9, 20)) and not pm.due(at(9, 30)) and pm.due(at(9, 6))


def test_bounds_and_junk(monkeypatch):
    monkeypatch.setattr(config, "PREMARKET_REFRESH_MIN", 500, raising=False)
    assert pm.refresh_minutes() == 30
    monkeypatch.setattr(config, "PREMARKET_REFRESH_MIN", -3, raising=False)
    assert pm.refresh_minutes() == 0
    monkeypatch.setattr(config, "PREMARKET_REFRESH_MIN", "x", raising=False)
    assert pm.refresh_minutes() == 5


def test_weekend_and_switch_off(monkeypatch):
    monkeypatch.setattr(config, "PREMARKET_REFRESH_MIN", 5, raising=False)
    pm.reset()
    assert not pm.due(datetime(2026, 11, 1, 9, 30, tzinfo=IST))       # Sunday
    monkeypatch.setattr(config, "PREMARKET_PLAN_ENABLED", False, raising=False)
    assert not pm.due(at(9, 30))
    monkeypatch.setattr(config, "PREMARKET_PLAN_ENABLED", True, raising=False)


def test_switch_is_on_the_calibration_page_and_protected():
    cat = (ROOT / "control" / "catalog.py").read_text(encoding="utf-8")
    auto = (ROOT.parent / "calibration-runner-service" / "autocalib.py").read_text(encoding="utf-8")
    cfg = (ROOT / "config.py").read_text(encoding="utf-8")
    import re
    assert 'Flag("PREMARKET_REFRESH_MIN"' in cat and '"PREMARKET_REFRESH_MIN":' in auto and re.search(r"^PREMARKET_REFRESH_MIN = ", cfg, re.M)
