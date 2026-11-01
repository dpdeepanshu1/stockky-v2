"""E272: scan_event_candidates. Plain runner: PYTHONPATH=. python3 tests/test_e272_event_candidates.py"""
from __future__ import annotations
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import types
import config
import feed
try:
    from feed import ws_client                     # E277: use the real client when it imports (it does in the Docker test image). Replacing feed.ws_client at import
except Exception:                                  # time left the stand-in in sys.modules for the whole pytest run and broke every later test that reads it
    _ws = types.ModuleType("feed.ws_client")       # bare sandbox only: the real client needs websockets / httpx, which the test never uses
    _ws.get_last_ltp = lambda s: None; _ws.get_tick_buffer = lambda s: []; _ws.get_day_stats = lambda s: None
    sys.modules["feed.ws_client"] = _ws; feed.ws_client = _ws
    from feed import ws_client
from screening import engine

EV = {"AAA": {"polarity": 1, "strength": 0.8, "age_min": 30.0}, "BBB": {"polarity": -1, "strength": 0.6, "age_min": 60.0},
      "CCC": {"polarity": 1, "strength": 0.1, "age_min": 5.0}, "DDD": {"polarity": 1, "strength": 0.9, "age_min": 5000.0},
      "EEE": {"polarity": 1, "strength": 0.7, "age_min": 10.0}, "FFF": {"polarity": 1, "strength": 0.5, "age_min": 10.0}}

def _run(side, held=None, events=EV):
    prices = {"AAA": 101.0, "BBB": 99.0, "CCC": 50.0, "DDD": 70.0, "EEE": 20.0}   # FFF has no price
    orig, orig_buf, orig_ds = ws_client.get_last_ltp, ws_client.get_tick_buffer, ws_client.get_day_stats
    ws_client.get_last_ltp = lambda s: prices.get(s); ws_client.get_tick_buffer = lambda s: [1, 2, 3]; ws_client.get_day_stats = lambda s: None
    try:
        return engine.scan_event_candidates(side, held, events)
    finally:
        ws_client.get_last_ltp, ws_client.get_tick_buffer, ws_client.get_day_stats = orig, orig_buf, orig_ds

def test_long_and_short_sides():
    assert [c.symbol for c in _run("LONG")] == ["AAA", "EEE"]          # CCC weak, DDD too old, FFF unpriced, BBB negative
    assert [c.symbol for c in _run("SHORT")] == ["BBB"]
    c = _run("LONG")[0]; assert c.window_label == "event" and c.composite_score == 8.0 and c.current_ltp == 101.0

def test_held_switch_and_failsafe():
    assert [c.symbol for c in _run("LONG", {"AAA.NS"})] == ["EEE"]
    config.EVENT_CANDIDATES_ENABLED = False
    try:
        assert _run("LONG") == []
    finally:
        config.EVENT_CANDIDATES_ENABLED = True
    assert _run("LONG", events={}) == []
    assert engine.scan_event_candidates("LONG") == []                    # no plan built = empty

if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"): f(); print("ok", n)
