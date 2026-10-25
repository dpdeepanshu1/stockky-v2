"""E148 (F-16, F-17): a broken STOPOUT_DAY_PAUSE_N or REGIME_RULES_TABLE_JSON still fails open / falls back, but now logs one warning.
Run from services/real-trade-service: python3 -m pytest tests/test_e148_silent_guards_warn.py -v"""
import logging

import config
from entry_engine import regime_rules, stopout_guard


def test_bad_day_pause_setting_warns_once_and_stays_fail_open(monkeypatch, caplog):
    monkeypatch.setattr(config, "STOPOUT_DAY_PAUSE_N", "three", raising=False)
    stopout_guard._warned.clear()
    with caplog.at_level(logging.WARNING, logger="stopout-guard"):
        assert stopout_guard.pause_reason_from(5) is None
        assert stopout_guard.pause_reason_from(6) is None
    msgs = [r.getMessage() for r in caplog.records if "day-pause" in r.getMessage()]
    assert len(msgs) == 1 and "OFF" in msgs[0]


def test_good_settings_unchanged_and_silent(monkeypatch, caplog):
    monkeypatch.setattr(config, "STOPOUT_DAY_PAUSE_N", 3, raising=False)
    monkeypatch.setattr(config, "STOPOUT_SYMBOL_BLOCK", True, raising=False)
    stopout_guard._warned.clear()
    with caplog.at_level(logging.WARNING, logger="stopout-guard"):
        assert stopout_guard.pause_reason_from(2) is None
        assert "PAUSED" in stopout_guard.pause_reason_from(3)
        assert "SYMBOL" in stopout_guard.symbol_reason("abc", {"ABC"})
        assert stopout_guard.symbol_reason("xyz", {"ABC"}) is None
    assert not caplog.records


def test_malformed_regime_override_warns_once_and_default_table_stays(monkeypatch, caplog):
    monkeypatch.setattr(config, "REGIME_RULES_TABLE_JSON", "{not json", raising=False)
    regime_rules._warned_override.clear()
    with caplog.at_level(logging.WARNING, logger="real-trade-regime-rules"):
        t1 = regime_rules.table()
        t2 = regime_rules.table()
    assert t1 == t2 == {k: dict(v) for k, v in regime_rules.DEFAULT_TABLE.items()}
    assert len([r for r in caplog.records if "REGIME_RULES_TABLE_JSON" in r.getMessage()]) == 1
