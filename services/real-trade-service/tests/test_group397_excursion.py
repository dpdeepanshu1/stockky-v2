"""group 397: highest / lowest price since entry for real-trade positions (exit_engine/excursion.py), the max gain / drawdown fields on the
per-trade record, and the stop diagnostics in the expectancy report. Pure tests - fakes only (the DB-backed pieces are in test_group397_excursion_db.py)."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exit_engine import excursion as ex
import trade_records as tr

NOW = datetime(2026, 10, 16, 5, 0, tzinfo=timezone.utc)


class FakeDB:
    def __init__(self, fail=False):
        self.commits = 0
        self.rollbacks = 0
        self.fail = fail

    def commit(self):
        if self.fail:
            raise RuntimeError("db down")
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def p(entry=100.0, hi=None, lo=None):
    return NS(symbol="AAA", avg_entry_price=entry, max_price_seen=hi, min_price_seen=lo)


def tk(price, age=1.0, known=True):
    return NS(price=price, as_of=NOW - timedelta(seconds=age), age_known=known)


class TestNextExtremes:
    def test_first_price_above_entry_sets_the_high_and_keeps_entry_as_low(self):
        assert ex.next_extremes(100.0, None, None, 103.0) == (103.0, 100.0)

    def test_first_price_below_entry_sets_the_low(self):
        assert ex.next_extremes(100.0, None, None, 98.5) == (100.0, 98.5)

    def test_price_inside_the_stored_range_changes_nothing(self):
        assert ex.next_extremes(100.0, 104.0, 97.0, 101.0) is None

    def test_exact_repeat_of_an_extreme_changes_nothing(self):
        assert ex.next_extremes(100.0, 104.0, 97.0, 104.0) is None

    def test_new_high_and_new_low_widen_only_their_side(self):
        assert ex.next_extremes(100.0, 104.0, 97.0, 105.0) == (105.0, 97.0)
        assert ex.next_extremes(100.0, 104.0, 97.0, 96.0) == (104.0, 96.0)

    def test_bad_inputs_give_none(self):
        for price in (None, 0, -1, "x", float("nan"), float("inf")):
            assert ex.next_extremes(100.0, None, None, price) is None
        assert ex.next_extremes(0, None, None, 100.0) is None
        assert ex.next_extremes(None, None, None, 100.0) is None

    def test_stored_values_that_are_junk_fall_back_to_entry(self):
        assert ex.next_extremes(100.0, "bad", 0, 101.0) == (101.0, 100.0)


class TestRecord:
    def test_saves_a_new_extreme_and_commits_once(self):
        db, pos = FakeDB(), p()
        assert ex.record(db, pos, tk(102.0), NOW) is True
        assert (pos.max_price_seen, pos.min_price_seen, db.commits) == (102.0, 100.0, 1)

    def test_no_new_extreme_writes_nothing(self):
        db, pos = FakeDB(), p(hi=104.0, lo=97.0)
        assert ex.record(db, pos, tk(101.0), NOW) is False and db.commits == 0

    def test_old_tick_is_ignored(self):
        db, pos = FakeDB(), p()
        assert ex.record(db, pos, tk(150.0, age=900.0), NOW) is False
        assert pos.max_price_seen is None and db.commits == 0

    def test_unknown_age_tick_is_accepted(self):
        db, pos = FakeDB(), p()
        assert ex.record(db, pos, tk(101.0, age=9999.0, known=False), NOW) is True

    def test_age_limit_is_an_env_setting(self, monkeypatch):
        monkeypatch.setenv("EXCURSION_MAX_TICK_AGE_S", "5")
        assert ex.record(FakeDB(), p(), tk(101.0, age=30.0), NOW) is False
        monkeypatch.setenv("EXCURSION_MAX_TICK_AGE_S", "  ")
        assert ex.record(FakeDB(), p(), tk(101.0, age=30.0), NOW) is True
        monkeypatch.setenv("EXCURSION_MAX_TICK_AGE_S", "junk")
        assert ex.record(FakeDB(), p(), tk(101.0, age=30.0), NOW) is True
        monkeypatch.setenv("EXCURSION_MAX_TICK_AGE_S", "-3")
        assert ex.record(FakeDB(), p(), tk(101.0, age=30.0), NOW) is True

    def test_no_tick_or_naive_as_of(self):
        assert ex.record(FakeDB(), p(), None, NOW) is False
        naive = NS(price=101.0, as_of=datetime(2026, 10, 16, 4, 59, 59), age_known=True)
        assert ex.record(FakeDB(), p(), naive, NOW) is True

    def test_commit_failure_rolls_back_and_never_raises(self):
        db = FakeDB(fail=True)
        assert ex.record(db, p(), tk(102.0), NOW) is False and db.rollbacks == 1

    def test_a_position_without_the_columns_does_not_raise(self):
        bare = NS(symbol="AAA", avg_entry_price=100.0)
        assert ex.record(FakeDB(), bare, tk(101.0), NOW) is True and bare.max_price_seen == 101.0

    def test_garbage_position_never_raises(self):
        assert ex.record(FakeDB(), object(), tk(101.0), NOW) is False


class TestPcts:
    def test_gain_and_drawdown_against_entry(self):
        assert ex.excursion_pcts(100.0, 103.0, 98.0) == {"max_gain_pct": 3.0, "max_drawdown_pct": -2.0}

    def test_unknown_when_anything_is_missing(self):
        none = {"max_gain_pct": None, "max_drawdown_pct": None}
        assert ex.excursion_pcts(100.0, None, 98.0) == none
        assert ex.excursion_pcts(None, 103.0, 98.0) == none
        assert ex.excursion_pcts(0, 103.0, 98.0) == none


def pos(**kw):
    d = dict(id=1, mode="REAL", symbol="AAA", status="CLOSED", avg_entry_price=100.0, realized_pnl=-20.0,
             opened_at=datetime(2026, 10, 9, 4, 0, tzinfo=timezone.utc), closed_at=datetime(2026, 10, 9, 5, 0, tzinfo=timezone.utc),
             broker_imported=False, watchlist_entry_id=None, source_tab="watchlist", entry_decision_label="BUY NOW", entry_conviction_score=70.0,
             is_regime_override=False, net_realized_pnl=None, realized_cost_estimate=None)
    d.update(kw)
    return NS(**d)


def test_record_carries_max_gain_and_drawdown():
    r = tr.build_records([pos(max_price_seen=102.0, min_price_seen=97.0)], [], [], [], [], [], [])[0]
    assert r["max_gain_pct"] == 2.0 and r["max_drawdown_pct"] == -3.0


def test_record_without_excursion_has_none_not_zero():
    r = tr.build_records([pos()], [], [], [], [], [], [])[0]
    assert r["max_gain_pct"] is None and r["max_drawdown_pct"] is None


def rec(pnl, reason, stop, gain, dd, basis="atr"):
    return {"gross_pnl": pnl, "net_pnl": None, "exit_reason": reason, "stop_pct": stop, "stop_basis": basis,
            "max_gain_pct": gain, "max_drawdown_pct": dd}


class TestStopDiagnostics:
    def rows(self):
        return [rec(-10, "stop_hit", 2.0, 1.2, -2.0),                    # stopped after running 0.6R
                rec(-10, "broker_stop", 2.0, 0.2, -2.0),                 # never ran
                rec(30, "target_hit", 2.0, 4.0, -1.7),                   # survivor within 0.2R of the stop
                rec(25, "target_hit", 2.0, 4.0, -0.2),
                rec(-8, "emergency_gap_down", 3.0, 0.0, -3.0, basis="flat")]

    def test_indicators_by_basis(self):
        g = tr.stop_diagnostics(self.rows())["groups"]
        assert g["all"]["trades"] == 5 and g["all"]["stop_outs"] == 3
        assert g["atr"]["trades"] == 4 and g["atr"]["stop_outs"] == 2
        assert g["atr"]["stopped_after_running_half_r"] == 1 and g["atr"]["survivors_within_02r"] == 1
        assert g["flat"]["trades"] == 1 and g["flat"]["stop_outs"] == 1

    def test_tighter_stop_counts_winners_killed(self):
        w = tr.stop_diagnostics(self.rows(), mults=(0.5,))["groups"]["atr"]["tighter"][0]
        # at 0.5x of a 2% stop = -1.0%: both stop-outs reached it, and the -1.7% target_hit winner did too; the -0.2% one did not
        assert w == {"mult": 0.5, "would_stop": 3, "non_stop_out_trades_hit": 1, "winners_killed": 1}

    def test_rows_without_stop_or_excursion_are_counted_but_not_used(self):
        rows = self.rows() + [rec(5, "target_hit", None, None, None), {"gross_pnl": 1, "exit_reason": "x"}]
        d = tr.stop_diagnostics(rows)
        assert d["trades"] == 7 and d["with_stop_and_excursion"] == 5

    def test_missing_basis_goes_to_unknown(self):
        assert "unknown" in tr.stop_diagnostics([rec(-5, "stop_hit", 2.0, 0.0, -2.0, basis=None)])["groups"]

    def test_report_embeds_the_diagnostics_and_empty_report_has_the_key(self):
        full = tr.expectancy_report(self.rows())
        assert full["stop_diagnostics"]["groups"]["all"]["stop_outs"] == 3
        assert tr.expectancy_report([])["stop_diagnostics"] is None


if __name__ == "__main__":
    import inspect
    n = 0
    for k, v in list(globals().items()):
        if k.startswith("test_") and callable(v):
            v(); n += 1
        elif k.startswith("Test") and inspect.isclass(v):
            for m in dir(v):
                if m.startswith("test_"):
                    inst = v()
                    f = getattr(inst, m)
                    if "monkeypatch" in inspect.signature(f).parameters:
                        continue            # env-setting test needs pytest's monkeypatch; run below
                    f(); n += 1
    print("ok", n)
