"""E448 (round 428): hotpicks_db_payload(hours=...) - a fraction below one hour is one hour (it used to truncate to 0 = an empty window);
NaN / inf / non-numbers / negatives fall back to the default window instead of raising or selecting nothing."""
import pytest

from test_hotpicks_store import _reader, env, m, rec  # noqa: F401  (fixtures + helpers)


@pytest.mark.parametrize("given,expected", [(0.5, 1), (0.01, 1), (1, 1), (6.7, 6), (0, 0), (24, 24)])
def test_window_hours(m, env, given, expected):
    _reader(env, [rec()])
    m.hotpicks_db_payload(hours=given)
    assert env.hp.shared.calls[-1][1] == {"hours": expected}


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), -3, "x", [1]])
def test_junk_window_falls_back_to_the_default_not_an_error(m, env, bad):
    _reader(env, [rec()])
    out = m.hotpicks_db_payload(hours=bad)
    assert out is not None
    assert env.hp.shared.calls[-1][1] == {"hours": int(m.HOTPICKS_TABLE_HOURS)}
