"""Round 314: CORE ignores the intraday part when the intraday bars are stale (the named strategies already skipped them); fresh bars are scored exactly as before."""
from __future__ import annotations

from datetime import datetime, timedelta

from fno import signals, strategies as S
from fno.params import Params
from test_strategies import IST, FLAT, day_bars, quiet

ROWS = quiet(14) + [(24001.0, 24050.0, 24000.0, 24046.0, 0)]


def _x(now):
    return S.build_ctx("NIFTY", FLAT(), day_bars("2026-11-10", ROWS), None, now, Params())


def test_fresh_bars_are_scored_as_before():
    bars = day_bars("2026-11-10", ROWS)
    now = datetime.fromtimestamp(bars[-1]["t"] + 300, IST)
    b = S.core_bias(_x(now))
    assert b["parts"]["intraday"] == signals.intraday_score(bars)[0]


def test_stale_bars_do_not_vote():
    bars = day_bars("2026-11-10", ROWS)
    now = datetime.fromtimestamp(bars[-1]["t"] + 300, IST) + timedelta(minutes=45)
    b = S.core_bias(_x(now))
    assert b["parts"]["intraday"] == 0.0
    assert any("stale" in r for r in b["reasons"])
