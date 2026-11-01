"""E274 round 45: negative typed event on a held stock (exit_engine/event_exit.py + intraday_news.negative_event). Plain runner: python3 tests/test_e274_event_exit.py (pytest also collects it)."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from exit_engine import event_exit as ee

IST = timezone(timedelta(hours=5, minutes=30))
T = datetime(2026, 11, 2, 10, 0, tzinfo=IST)


def _ev(s=0.85, pol=-1, typ="results"):
    return {"polarity": pol, "type": typ, "strength": s}


def _run(ev, stop=95.0, ltp=100.0, atr=2.0, now=T, mode_=None):
    return ee.evaluate(ev, entry_price=98.0, ltp=ltp, current_stop=stop, atr=atr, now_ist=now, tick=0.05, mode_=mode_)


def test_strong_sells():
    ee.reset()
    assert _run(_ev(0.85))["action"] == "exit"


def test_weak_tightens_under_price_rounded_down():
    v = _run(_ev(0.45))
    assert v["action"] == "tighten" and v["new_stop"] == 99.5 and v["new_stop"] < 100.0


def test_strong_in_tighten_mode_tightens():
    assert _run(_ev(0.85), mode_="tighten")["action"] == "tighten"


def test_below_minimum_ignored():
    assert _run(_ev(0.30))["action"] == "hold"


def test_unknown_or_positive_polarity_never_acts():
    assert _run(_ev(0.9, pol=0))["action"] == "hold"
    assert _run(_ev(0.9, pol=1))["action"] == "hold"
    assert _run(None)["action"] == "hold"


def test_not_before_0920():
    assert _run(_ev(0.9), now=datetime(2026, 11, 2, 9, 19, tzinfo=IST))["action"] == "hold"
    assert _run(_ev(0.9), now=datetime(2026, 11, 2, 9, 20, tzinfo=IST))["action"] == "exit"


def test_ratchet_only():
    v = _run(_ev(0.45), stop=99.8)          # candidate 99.5 is below the existing stop
    assert v["action"] == "hold" and v["new_stop"] is None


def test_no_atr_uses_half_percent():
    v = _run(_ev(0.45), atr=None)
    assert v["action"] == "tighten" and abs(v["new_stop"] - 99.5) < 1e-9


def test_off_and_error_safe():
    assert _run(_ev(0.9), mode_="off")["action"] == "hold"
    assert _run(_ev(0.9), now="bad")["action"] == "hold"


def test_limit_price_only_for_afterhours():
    ev = {"polarity": -1, "strength": 0.8, "source": "afterhours"}
    assert ee.limit_price_for(100.0, ev, 0.05) == 99.5
    assert ee.limit_price_for(100.0, dict(ev, source="intraday"), 0.05) is None
    assert ee.limit_price_for(0, ev) is None and ee.limit_price_for(100.0, None) is None
    config.EVENT_EXIT_LIMIT_ENABLED = False
    try:
        assert ee.limit_price_for(100.0, ev, 0.05) is None
    finally:
        config.EVENT_EXIT_LIMIT_ENABLED = True


if __name__ == "__main__":
    n = 0
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f(); n += 1
    print(f"{n} passed")
