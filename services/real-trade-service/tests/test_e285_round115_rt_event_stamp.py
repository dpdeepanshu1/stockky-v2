"""E285 round 115: a Real Trade entry records the typed event it saw in flow_json["setup"]["event"] (the same shape Position Stocks keeps). Record only.
Plain-function runner: python3 tests/test_e285_round115_rt_event_stamp.py (also collected by pytest)."""
from __future__ import annotations

import json
import os
import sys
import types
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from entry_engine import setup_stamp

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = datetime(2026, 11, 2, 9, 40)
PEV = {"polarity": 1, "type": "Results", "strength": 0.8123, "age_min": 12}        # what watchlist_engine.intraday_news.positive_event returns


def test_event_part_reads_the_intraday_news_shape_and_the_position_stocks_shape():
    assert setup_stamp.event_part(PEV) == {"type": "results", "pol": 1, "strength": 0.812, "age_min": 12.0}
    assert setup_stamp.event_part({"type": "deal", "pol": -1, "strength": 0.5}) == {"type": "deal", "pol": -1, "strength": 0.5}
    q = types.SimpleNamespace(event_type="order", event_polarity=1, event_strength=0.4, event_age_min=5)
    assert setup_stamp.event_part(q) == {"type": "order", "pol": 1, "strength": 0.4, "age_min": 5.0}


def test_event_part_gives_none_for_anything_unreadable():
    for bad in (None, "junk", 5, {}, {"polarity": 1}, {"type": "results"}, {"type": "results", "polarity": 0}, {"type": "results", "polarity": 2}, types.SimpleNamespace()):
        assert setup_stamp.event_part(bad) is None, bad
    assert setup_stamp.event_part({"type": "deal", "polarity": 1, "strength": "x", "age_min": float("nan")}) == {"type": "deal", "pol": 1}


def test_build_records_the_event_only_when_there_is_one():
    assert setup_stamp.build(event=PEV, now=NOW)["event"]["type"] == "results"
    assert "event" not in setup_stamp.build(now=NOW)                               # the old call is unchanged
    assert "event" not in setup_stamp.build(event={"type": "x", "polarity": 0}, now=NOW)


def test_the_stamp_reads_back_and_a_first_stamp_still_wins():
    first = setup_stamp.build(setup_id="S1_CATALYST_UP", event=PEV, now=NOW)
    out = setup_stamp.attach("{}", first)
    out = setup_stamp.attach(out, setup_stamp.build(trigger="shortlist", now=NOW))      # the legacy stamp must not replace the matched one
    st = setup_stamp.read(out)
    assert st["id"] == "S1_CATALYST_UP" and st["event"]["pol"] == 1
    json.loads(out)


def test_the_entry_path_carries_the_event_into_both_stamps():
    src = open(os.path.join(_HERE, "entry_engine", "entry.py"), encoding="utf-8").read()
    assert "_stamp_event = _pev" in src
    assert "setup_stamp.event_part(_stamp_event)" in src and 'side="LONG", event=_stamp_event)' in src
    assert src.index("_stamp_event = None") < src.index("_stamp_event = _pev") < src.index("setup_stamp.event_part(_stamp_event)")


def _run_all() -> int:
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as e:  # noqa: BLE001
                fails += 1
                import traceback
                print("FAIL", name, repr(e))
                traceback.print_exc()
    print("failed:", fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
