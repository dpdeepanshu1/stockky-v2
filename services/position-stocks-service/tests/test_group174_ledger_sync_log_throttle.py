"""group 174: ledger sync INFO lines are throttled (log on change, else every LEDGER_SYNC_LOG_EVERY_S)."""
import pytest

from capital import ledger


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(ledger, "_sync_log_state", {})
    monkeypatch.delenv("LEDGER_SYNC_LOG_EVERY_S", raising=False)


def _clock(monkeypatch, t):
    monkeypatch.setattr(ledger.time, "monotonic", lambda: t[0])


def test_first_call_logs_then_same_figures_are_quiet(monkeypatch):
    t = [1000.0]
    _clock(monkeypatch, t)
    assert ledger._should_log_sync("broker", (100.0, 50.0)) is True
    t[0] += 13
    assert ledger._should_log_sync("broker", (100.0, 50.0)) is False
    t[0] += 13
    assert ledger._should_log_sync("broker", (100.0, 50.0)) is False


def test_changed_figures_log_immediately(monkeypatch):
    t = [1000.0]
    _clock(monkeypatch, t)
    assert ledger._should_log_sync("broker", (100.0, 50.0)) is True
    t[0] += 5
    assert ledger._should_log_sync("broker", (90.0, 45.0)) is True
    t[0] += 5
    assert ledger._should_log_sync("broker", (90.0, 45.0)) is False


def test_logs_again_after_interval(monkeypatch):
    t = [1000.0]
    _clock(monkeypatch, t)
    assert ledger._should_log_sync("peer_pnl", 12.5) is True
    t[0] += 299
    assert ledger._should_log_sync("peer_pnl", 12.5) is False
    t[0] += 2
    assert ledger._should_log_sync("peer_pnl", 12.5) is True


def test_keys_are_independent(monkeypatch):
    _clock(monkeypatch, [1.0])
    assert ledger._should_log_sync("broker", 1) is True
    assert ledger._should_log_sync("peer_pnl", 1) is True


def test_zero_means_always_log(monkeypatch):
    monkeypatch.setenv("LEDGER_SYNC_LOG_EVERY_S", "0")
    _clock(monkeypatch, [1.0])
    assert ledger._should_log_sync("broker", 1) is True
    assert ledger._should_log_sync("broker", 1) is True


@pytest.mark.parametrize("raw", ["", "   ", "abc", "-5"])
def test_blank_or_invalid_uses_default(monkeypatch, raw):
    monkeypatch.setenv("LEDGER_SYNC_LOG_EVERY_S", raw)
    assert ledger._sync_log_every_s() == 300.0


def test_custom_interval(monkeypatch):
    monkeypatch.setenv("LEDGER_SYNC_LOG_EVERY_S", "60")
    t = [0.0]
    _clock(monkeypatch, t)
    assert ledger._should_log_sync("broker", 1) is True
    t[0] = 59
    assert ledger._should_log_sync("broker", 1) is False
    t[0] = 61
    assert ledger._should_log_sync("broker", 1) is True
