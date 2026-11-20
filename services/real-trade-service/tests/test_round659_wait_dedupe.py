"""Round 659 (D3): one WAIT decision row per symbol + reason per window; changes and ENTER rows are always written."""
from types import SimpleNamespace as NS

import pytest

import wait_dedupe as wd
import write_behind as wb


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("WAIT_DEDUPE_MINUTES", raising=False)
    wd.reset_for_tests()
    yield
    wd.reset_for_tests()


def row(symbol="AAA", reason="no_setup_window: score 41 below 55", action="WAIT", tag=None, mode="REAL", verdict_reason=None):
    return NS(mode=mode, symbol=symbol, action=action, reasoning=reason, gate_tag=tag, risk_verdict=None, risk_verdict_reason=verdict_reason)


def test_first_row_is_kept_and_a_repeat_is_dropped():
    assert wd.is_repeat(row(), now=0.0) is False
    assert wd.is_repeat(row(), now=180.0) is True
    assert wd.is_repeat(row(), now=360.0) is True


def test_numbers_in_the_reason_are_not_a_change():
    assert wd.is_repeat(row(reason="chase 0.83% above vwap, room 1.2%"), now=0.0) is False
    assert wd.is_repeat(row(reason="chase 0.85% above vwap, room 1.1%"), now=60.0) is True


def test_a_different_reason_or_gate_is_written_and_a_to_b_to_a_is_three_rows():
    assert wd.is_repeat(row(reason="liquidity too thin"), now=0.0) is False
    assert wd.is_repeat(row(reason="late_day after 14:00"), now=10.0) is False
    assert wd.is_repeat(row(reason="liquidity too thin"), now=20.0) is False
    assert wd.is_repeat(row(reason="liquidity too thin", tag="cost_model"), now=30.0) is False


def test_written_again_after_the_window():
    assert wd.is_repeat(row(), now=0.0) is False
    assert wd.is_repeat(row(), now=899.0) is True
    assert wd.is_repeat(row(), now=900.0) is False          # 15 minutes
    assert wd.is_repeat(row(), now=1000.0) is True           # the window restarts from the last written row


def test_symbols_and_modes_are_independent():
    assert wd.is_repeat(row("AAA"), now=0.0) is False
    assert wd.is_repeat(row("BBB"), now=1.0) is False
    assert wd.is_repeat(row("AAA", mode="DEMO"), now=2.0) is False


def test_an_enter_row_is_never_dropped_and_resets_the_symbol():
    assert wd.is_repeat(row(), now=0.0) is False
    assert wd.is_repeat(row(action="ENTER"), now=5.0) is False
    assert wd.is_repeat(row(), now=10.0) is False             # first WAIT after the ENTER is written


def test_off_switch_and_bad_value(monkeypatch):
    monkeypatch.setenv("WAIT_DEDUPE_MINUTES", "0")
    assert wd.is_repeat(row(), now=0.0) is False and wd.is_repeat(row(), now=1.0) is False
    monkeypatch.setenv("WAIT_DEDUPE_MINUTES", "oops")
    assert wd.window_s() == 900.0
    monkeypatch.setenv("WAIT_DEDUPE_MINUTES", "5")
    assert wd.window_s() == 300.0


def test_never_raises_on_junk():
    assert wd.is_repeat(object(), now=0.0) is False
    assert wd.is_repeat(None) is False


def test_tracking_is_bounded(monkeypatch):
    monkeypatch.setattr(wd, "_MAX_KEYS", 50)
    for i in range(200):
        wd.is_repeat(row(f"S{i}"), now=float(i))
    assert wd.status()["tracked"] <= 50


# ── through write_behind.divert_pending_waits ───────────────────────────────────────────────────────────────────────
class Col:
    def __init__(self, key):
        self.key = key


def _pending(symbol="AAA", reason="no setup", action="WAIT", divertable=False):
    cols = ["id", "mode", "symbol", "decision_type", "action", "reasoning", "gate_tag", "risk_verdict", "risk_verdict_reason", "created_at"]
    return NS(id=None, mode="REAL", symbol=symbol, decision_type="ENTRY", action=action, reasoning=reason, gate_tag=None, risk_verdict=None,
              risk_verdict_reason=None, created_at=None, _wb_divertable=divertable, __table__=NS(columns=[Col(c) for c in cols]))


class FakeDB:
    def __init__(self, new):
        self.new, self.expunged = list(new), []

    def expunge(self, o):
        self.expunged.append(o)


def test_cycle_after_cycle_only_the_first_row_survives(monkeypatch):
    monkeypatch.setenv("WRITE_BEHIND_ENABLED", "0")           # rows that survive stay in the session (written synchronously as before)
    wb.reset_for_tests()
    first = FakeDB([_pending("AAA"), _pending("BBB")])
    wb.divert_pending_waits(first)
    assert first.expunged == []
    second = FakeDB([_pending("AAA"), _pending("BBB"), _pending("CCC")])
    wb.divert_pending_waits(second)
    assert [o.symbol for o in second.expunged] == ["AAA", "BBB"]      # repeats dropped, the new symbol stays
    assert wb.status()["wait_dedupe"]["dropped"] == 2


def test_an_unflushed_enter_row_and_exit_rows_are_never_dropped(monkeypatch):
    monkeypatch.setenv("WRITE_BEHIND_ENABLED", "0")
    wb.reset_for_tests()
    enter, flushed = _pending(action="ENTER"), _pending()
    flushed.id = 7
    for _ in range(2):
        db = FakeDB([enter, flushed])
        wb.divert_pending_waits(db)
        assert db.expunged == []
