"""group 174: the 'skipping re-import' INFO line logs once per (symbol, closed_at), then every 30 min."""
import pytest

from portfolio import portfolio


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(portfolio, "_recent_close_skip_logged", {})
    monkeypatch.delenv("RECENT_CLOSE_SKIP_LOG_EVERY_S", raising=False)


def _clock(monkeypatch, t):
    monkeypatch.setattr(portfolio.time, "monotonic", lambda: t[0])


def test_logs_once_then_quiet(monkeypatch):
    t = [100.0]
    _clock(monkeypatch, t)
    assert portfolio._should_log_recent_close_skip("LATENTVIEW", "2026-10-06 04:00") is True
    for _ in range(5):
        t[0] += 10
        assert portfolio._should_log_recent_close_skip("LATENTVIEW", "2026-10-06 04:00") is False


def test_logs_again_after_interval(monkeypatch):
    t = [0.0]
    _clock(monkeypatch, t)
    assert portfolio._should_log_recent_close_skip("A", "x") is True
    t[0] = 1799
    assert portfolio._should_log_recent_close_skip("A", "x") is False
    t[0] = 1801
    assert portfolio._should_log_recent_close_skip("A", "x") is True


def test_other_symbol_or_new_close_logs(monkeypatch):
    _clock(monkeypatch, [0.0])
    assert portfolio._should_log_recent_close_skip("A", "x") is True
    assert portfolio._should_log_recent_close_skip("B", "x") is True
    assert portfolio._should_log_recent_close_skip("A", "y") is True


def test_zero_means_every_cycle(monkeypatch):
    monkeypatch.setenv("RECENT_CLOSE_SKIP_LOG_EVERY_S", "0")
    _clock(monkeypatch, [0.0])
    assert portfolio._should_log_recent_close_skip("A", "x") is True
    assert portfolio._should_log_recent_close_skip("A", "x") is True


@pytest.mark.parametrize("raw", ["", "  ", "oops", "-1"])
def test_blank_or_invalid_uses_default(monkeypatch, raw):
    monkeypatch.setenv("RECENT_CLOSE_SKIP_LOG_EVERY_S", raw)
    t = [0.0]
    _clock(monkeypatch, t)
    assert portfolio._should_log_recent_close_skip("A", "x") is True
    t[0] = 1000
    assert portfolio._should_log_recent_close_skip("A", "x") is False
