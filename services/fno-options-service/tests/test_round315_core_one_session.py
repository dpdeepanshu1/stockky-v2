"""Round 315: CORE scores only the latest session's intraday bars; a feed that also carries an earlier day does not blend the days."""
from __future__ import annotations

from datetime import datetime

from fno import signals, strategies as S
from fno.params import Params
from test_strategies import IST, FLAT, day_bars, quiet

YESTERDAY = day_bars("2026-11-09", [(24000.0 + 40 * k, 24000.0 + 40 * k + 50, 24000.0 + 40 * k - 5, 24000.0 + 40 * k + 45, 1000) for k in range(60)])
TODAY = day_bars("2026-11-10", quiet(14) + [(24001.0, 24050.0, 24000.0, 24046.0, 0)])


def _core(bars):
    now = datetime.fromtimestamp(bars[-1]["t"] + 300, IST)
    return S.core_bias(S.build_ctx("NIFTY", FLAT(), bars, None, now, Params()))


def test_an_earlier_day_in_the_feed_does_not_change_the_score():
    assert _core(YESTERDAY + TODAY)["parts"]["intraday"] == signals.intraday_score(TODAY)[0]
    assert _core(YESTERDAY + TODAY) == _core(TODAY)


def test_a_single_session_feed_is_unchanged():
    assert _core(TODAY)["parts"]["intraday"] == signals.intraday_score(TODAY)[0]


def test_the_signals_page_path_agrees_with_core():
    now = datetime.fromtimestamp(TODAY[-1]["t"] + 300, IST)
    x = S.build_ctx("NIFTY", FLAT(), YESTERDAY + TODAY, None, now, Params())
    assert S.core_bias(x) == signals.index_bias(FLAT(), YESTERDAY + TODAY, None, Params())
