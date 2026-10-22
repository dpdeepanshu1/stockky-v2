"""E47: the Real Money Calibration page's risk-per-trade switch now reaches the engine for a row nobody saved by hand."""
from types import SimpleNamespace
from unittest import mock

import config
from risk_engine.effective_risk import SYSTEM_TAGS, effective_risk_pct, is_system_owned


def _row(value, by):
    return SimpleNamespace(risk_per_trade_pct=value, updated_by=by)


def test_system_owned_row_follows_the_live_config_value():
    for tag in SYSTEM_TAGS:
        with mock.patch.object(config, "DEFAULT_RISK_PER_TRADE_PCT", 1.5):
            assert effective_risk_pct(_row(5.0, tag)) == 1.5


def test_admin_saved_row_wins_even_when_config_differs():
    with mock.patch.object(config, "DEFAULT_RISK_PER_TRADE_PCT", 1.5):
        assert effective_risk_pct(_row(4.0, "admin")) == 4.0
        assert effective_risk_pct(_row(2.5, "demo-user")) == 2.5


def test_untagged_legacy_row_keeps_its_stored_value():
    with mock.patch.object(config, "DEFAULT_RISK_PER_TRADE_PCT", 1.5):
        assert effective_risk_pct(_row(1.0, None)) == 1.0
        assert effective_risk_pct(_row(3.3, "")) == 3.3
        assert not is_system_owned(_row(1.0, None))


def test_bad_config_value_falls_back_to_the_stored_number():
    row = _row(2.0, "system-seed")
    for bad in (0, -1.0, None, "x"):
        with mock.patch.object(config, "DEFAULT_RISK_PER_TRADE_PCT", bad):
            assert effective_risk_pct(row) == 2.0


def test_row_without_the_attribute_does_not_raise():
    assert effective_risk_pct(SimpleNamespace()) is None


def test_call_sites_use_the_helper():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    for rel in ("entry_engine/entry.py", "manual_engine.py", "main.py"):
        src = (root / rel).read_text(encoding="utf-8")
        assert "effective_risk_pct(" in src, rel
        assert "risk_per_trade_pct=risk.risk_per_trade_pct,\n" not in src and "risk_per_trade_pct=risk_row.risk_per_trade_pct," not in src, rel
