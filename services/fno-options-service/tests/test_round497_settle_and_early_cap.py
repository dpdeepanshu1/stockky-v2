"""Round 497 (E517): review items 7 and 8 of the 13 Nov review. Item 8: no entry in the first minutes after the entry window opens (FNO_ENTRY_SETTLE_MIN, default 10: nothing before 09:40).
Item 7: the day's cap of FNO_MAX_TRADES_PER_DAY is not used up by the morning (FNO_EARLY_TRADES_MAX entries before FNO_EARLY_UNTIL, default 2 before 12:00). Pure gate tests, no network."""
from __future__ import annotations

from dataclasses import fields
from datetime import date, datetime, timedelta, timezone

import config
from control.catalog import CATALOG
from fno import gates, liveparams
from fno.params import CONFIG_NAME, Params

IST = timezone(timedelta(hours=5, minutes=30))
DAY = date(2026, 11, 13)           # a Friday
EXPIRY = date(2026, 11, 19)


def at(hh, mm):
    return datetime(2026, 11, 13, hh, mm, tzinfo=IST)


def gate(now, state=None, p=None, **kw):
    return gates.entry_gates(now, EXPIRY, 14.0, 0.0, state or gates.DayState(), p or Params(), **kw)


# ── item 8: settle after the open ──
def test_nothing_is_entered_in_the_first_ten_minutes_of_the_window():
    for mm in (30, 31, 39):
        ok, why = gate(at(9, mm))
        assert not ok and any("settling after the open" in w and "09:40" in w for w in why), (mm, why)


def test_the_window_edge_and_later_are_free():
    assert gate(at(9, 40))[0]
    assert gate(at(10, 5))[0]


def test_before_the_window_only_the_window_is_reported():
    ok, why = gate(at(9, 20))
    assert not ok and any("outside the entry window" in w for w in why) and not any("settling" in w for w in why)


def test_zero_turns_it_off_and_the_length_follows_the_setting():
    assert gate(at(9, 30), p=Params().with_(entry_settle_min=0))[0]
    p = Params().with_(entry_settle_min=20)
    assert not gate(at(9, 49), p=p)[0] and gate(at(9, 50), p=p)[0]


def test_it_counts_from_the_window_start_not_from_09_15():
    p = Params().with_(entry_start="10:00", entry_settle_min=10)
    assert not gate(at(10, 5), p=p)[0] and gate(at(10, 10), p=p)[0]


def test_expiry_day_strategies_obey_it_too():
    ok, why = gate(at(9, 35), p=Params().with_(expiry_real_ok=True), expiry_strategy=True, strategy="EXPIRY_MOMENTUM")
    # (the contract does not expire today here, so other reasons appear as well; the settle reason must be among them)
    assert not ok and any("settling after the open" in w for w in why)


def test_a_settle_that_leaves_no_window_is_a_problem_not_a_silent_no_trade():
    bad = gates.param_problems(Params().with_(entry_start="14:25", entry_end="14:30", entry_settle_min=10))
    assert any("FNO_ENTRY_SETTLE_MIN" in w and "nothing could ever be entered" in w for w in bad)
    assert gates.param_problems(Params()) == []


# ── item 7: the daily cap is kept for later ──
def test_two_morning_entries_leave_the_third_slot_for_later():
    s = gates.DayState(trades_today=2)
    ok, why = gate(at(10, 30), s)
    assert not ok and any("kept for later" in w and "1 of today's 3" in w for w in why)
    assert not any("trades today (max" in w for w in why)           # not reported twice


def test_after_the_early_time_the_third_slot_is_open():
    assert gate(at(12, 0), gates.DayState(trades_today=2))[0]
    assert gate(at(13, 15), gates.DayState(trades_today=2))[0]


def test_before_the_early_limit_the_first_two_are_free():
    assert gate(at(10, 30), gates.DayState(trades_today=0))[0]
    assert gate(at(10, 30), gates.DayState(trades_today=1))[0]


def test_the_real_cap_still_wins_after_noon():
    ok, why = gate(at(13, 0), gates.DayState(trades_today=3))
    assert not ok and any("3 trades today (max 3)" in w for w in why)


def test_zero_turns_the_early_limit_off_and_a_cap_that_is_not_below_the_daily_cap_changes_nothing():
    assert gate(at(10, 30), gates.DayState(trades_today=2), p=Params().with_(early_trades_max=0))[0]
    assert gate(at(10, 30), gates.DayState(trades_today=2), p=Params().with_(early_trades_max=3))[0]
    p = Params().with_(early_trades_max=1, early_until="11:00")
    assert not gate(at(10, 30), gates.DayState(trades_today=1), p=p)[0] and gate(at(11, 0), gates.DayState(trades_today=1), p=p)[0]


def test_an_unreadable_early_time_falls_back_to_noon():
    p = Params().with_(early_until="soon")
    assert not gate(at(11, 59), gates.DayState(trades_today=2), p=p)[0] and gate(at(12, 0), gates.DayState(trades_today=2), p=p)[0]


# ── wiring ──
def test_the_three_settings_are_wired_everywhere():
    names = {f.name for f in fields(Params)}
    for f, env, shipped in (("entry_settle_min", "FNO_ENTRY_SETTLE_MIN", "10"), ("early_trades_max", "FNO_EARLY_TRADES_MAX", "2"), ("early_until", "FNO_EARLY_UNTIL", "12:00")):
        assert f in names and CONFIG_NAME[f] == env and hasattr(config, env)
        flag = next(x for x in CATALOG if x.key == env)
        assert flag.live and flag.shipped == shipped == str(getattr(Params(), f)).lower()
    live = liveparams.current()
    assert live.entry_settle_min == 10 and live.early_trades_max == 2 and live.early_until == "12:00"
    assert Params.from_config(config).entry_settle_min == 10


def test_a_blank_or_bad_env_value_keeps_the_default():
    import importlib, os
    old = {k: os.environ.get(k) for k in ("FNO_ENTRY_SETTLE_MIN", "FNO_EARLY_TRADES_MAX", "FNO_EARLY_UNTIL")}
    try:
        os.environ.update({"FNO_ENTRY_SETTLE_MIN": "abc", "FNO_EARLY_TRADES_MAX": "", "FNO_EARLY_UNTIL": ""})
        c = importlib.reload(config)
        assert c.FNO_ENTRY_SETTLE_MIN == 10 and c.FNO_EARLY_TRADES_MAX == 2 and c.FNO_EARLY_UNTIL == "12:00"
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(config)
