"""E285 round 160 (plan item 20): ORB hit / miss / stale counts are logged at INFO. Stdlib + the service's own modules; runs with pytest or as a plain script."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent / "scripts"))

from entry_engine import orb_bars as OB  # noqa: E402


def _fresh(monkeypatch=None):
    OB._reset_for_tests()
    if monkeypatch is not None:
        monkeypatch.delenv("ORB_STATS_LOG_EVERY_S", raising=False)


def test_first_call_starts_the_window_and_logs_nothing():
    _fresh()
    OB._stats["hit"] = 3
    assert OB.log_stats_if_due(1000.0) is None


def test_line_after_the_interval_counts_only_the_window():
    _fresh()
    OB._stats["hit"] = 5                                   # before the window starts
    OB.log_stats_if_due(1000.0)
    OB._stats.update(hit=5 + 6, miss=3, stale=1, ok=4, failed=2)
    assert OB.log_stats_if_due(1100.0) is None             # 100 s < 300 s
    line = OB.log_stats_if_due(1300.0)
    assert line == ("ORB bars, last 300s: 10 decision(s): hit 6, miss 3, stale 1 (60% carried the close-trigger fields); "
                    "bar fetches ok 4, failed 2")
    assert OB.log_stats_if_due(1700.0) is None             # nothing new in the next window: no line


def test_zero_turns_it_off_and_bad_value_means_default(monkeypatch):
    _fresh(monkeypatch)
    monkeypatch.setenv("ORB_STATS_LOG_EVERY_S", "0")
    OB.log_stats_if_due(1000.0)
    OB._stats["hit"] = 9
    assert OB.log_stats_if_due(9999.0) is None
    monkeypatch.setenv("ORB_STATS_LOG_EVERY_S", "nope")
    assert OB.log_every_s() == 300.0
    monkeypatch.setenv("ORB_STATS_LOG_EVERY_S", "-5")
    assert OB.log_every_s() == 300.0


def test_line_is_written_at_info_level(caplog):
    _fresh()
    OB.log_stats_if_due(1000.0)
    OB._stats["miss"] = 2
    with caplog.at_level(logging.INFO, logger="real-trade-orb-bars"):
        OB.log_stats_if_due(1400.0)
    assert any(r.levelno == logging.INFO and "ORB bars, last 400s" in r.getMessage() for r in caplog.records)


def test_fields_for_still_counts_and_returns_as_before():
    _fresh()
    assert OB.fields_for("TCS", 100.0, bars=[]) == {}
    assert OB.stats()["miss"] == 1


if __name__ == "__main__":
    test_first_call_starts_the_window_and_logs_nothing(); test_line_after_the_interval_counts_only_the_window(); test_fields_for_still_counts_and_returns_as_before()
    print("ok")
