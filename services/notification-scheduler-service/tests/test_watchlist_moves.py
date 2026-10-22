"""E28: scheduler/watchlist_moves.py - server-side baseline + "moved N % since added" alert (alert only). No network, no clock, temp state dir.
Plain functions: pytest runs them unchanged; `python3 tests/test_watchlist_moves.py` runs them without pytest."""
from __future__ import annotations

import importlib.util
import itertools
import os
import sys
import tempfile
from datetime import datetime

try:
    import httpx  # noqa: F401
except ImportError:
    import types
    sys.modules["httpx"] = types.ModuleType("httpx")

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scheduler", "watchlist_moves.py")
_n = itertools.count()
T0 = datetime(2026, 10, 23, 10, 0)


def _load():
    os.environ["SCHEDULER_STATE_DIR"] = tempfile.mkdtemp()
    spec = importlib.util.spec_from_file_location(f"watchlist_moves_t{next(_n)}", _PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


m = _load()


def test_first_sight_records_a_baseline_and_never_alerts():
    st, al = m.evaluate({}, ["AAA", "BBB"], {"AAA": 100.0, "BBB": None}, T0, 5.0)
    assert al == [] and st["AAA"]["price"] == 100.0 and st["AAA"]["source"] == "first_seen_server" and "BBB" not in st


def test_move_past_threshold_alerts_once_per_direction():
    st = {"AAA": {"price": 100.0, "ts": "2026-10-22T10:00:00", "source": "first_seen_server", "alerted": None}}
    st, al = m.evaluate(st, ["AAA"], {"AAA": 105.0}, T0, 5.0)
    assert [(a["symbol"], a["side"], a["pct"]) for a in al] == [("AAA", "up", 5.0)]
    st["AAA"]["alerted"] = "up"                                   # what run_once stores after a 200
    assert m.evaluate(st, ["AAA"], {"AAA": 110.0}, T0, 5.0)[1] == []        # still up: quiet
    assert m.evaluate(st, ["AAA"], {"AAA": 102.0}, T0, 5.0)[1] == []        # between half and full: quiet, not re-armed
    st2, al2 = m.evaluate(st, ["AAA"], {"AAA": 101.0}, T0, 5.0)             # inside half: re-armed
    assert al2 == [] and st2["AAA"]["alerted"] is None
    assert [a["side"] for a in m.evaluate(st, ["AAA"], {"AAA": 94.0}, T0, 5.0)[1]] == ["down"]   # the other side alerts at once


def test_removed_symbols_forget_their_baseline():
    st = {"AAA": {"price": 100.0, "ts": "x", "source": "s", "alerted": None}, "ZZZ": {"price": 5.0, "ts": "x", "source": "s", "alerted": None}}
    new, _ = m.evaluate(st, ["AAA"], {"AAA": 100.0}, T0, 5.0)
    assert set(new) == {"AAA"}


def test_bad_prices_and_garbage_never_raise():
    st = {"AAA": {"price": 100.0, "ts": "x", "source": "s", "alerted": None}, "BBB": "junk", "CCC": {"price": -1}}
    new, al = m.evaluate(st, ["AAA", "BBB", "CCC"], {"AAA": float("nan"), "BBB": 10, "CCC": 7}, T0, 5.0)
    assert al == [] and new["AAA"]["price"] == 100.0 and new["BBB"]["price"] == 10.0 and new["CCC"]["price"] == 7.0
    assert m.evaluate(None, ["A"], {}, T0, 5.0)[1] == []
    assert m.pct_since(0, 5) is None and m.pct_since(100, None) is None and m.pct_since(100, 110) == 10.0


def test_format_sorts_by_size_and_says_alert_only():
    d = m.format_alert([{"symbol": "A", "pct": 5.1, "price": 105.1, "base": 100.0, "since": "2026-10-20T10:00:00", "side": "up"},
                        {"symbol": "B", "pct": -9.0, "price": 91.0, "base": 100.0, "since": None, "side": "down"}])
    assert d["message"].index("B:") < d["message"].index("A:") and "does not trade" in d["message"] and "2 watchlist stocks" in d["title"]
    assert m.format_alert([]) is None


def test_market_hours_and_switches():
    assert m.market_open(datetime(2026, 10, 23, 10, 0)) and not m.market_open(datetime(2026, 10, 23, 16, 0))
    assert not m.market_open(datetime(2026, 10, 24, 10, 0))      # Saturday
    os.environ["WATCHLIST_MOVE_ALERT_PCT"] = "abc"
    assert m.threshold_pct() == 5.0
    os.environ["WATCHLIST_MOVE_ALERT_PCT"] = "999"
    assert m.threshold_pct() == 50.0
    os.environ["WATCHLIST_MOVE_ALERT_PCT"] = ""
    os.environ["WATCHLIST_MOVE_ALERTS_ENABLED"] = "off"
    assert m.enabled() is False
    os.environ.pop("WATCHLIST_MOVE_ALERTS_ENABLED")
    assert m.enabled() is True


def test_run_once_saves_baseline_then_marks_alerted_only_after_a_200():
    mm = _load()
    mm.fetch_watchlist = lambda: ["AAA"]
    px = {"AAA": 100.0}
    mm.fetch_price = lambda s: px[s]
    sent = []
    mm.send = lambda t, msg: (sent.append(msg) or True)
    mm.time.sleep = lambda s: None
    r = mm.run_once(now=T0)
    assert r["ok"] and mm.load_state()["AAA"]["price"] == 100.0 and not sent
    px["AAA"] = 106.0
    assert mm.run_once(dry_run=True, now=T0)["alerts"] and not sent and mm.load_state()["AAA"]["alerted"] is None
    mm.send = lambda t, msg: False                                  # notification service down
    r = mm.run_once(now=T0)
    assert r["error"] and mm.load_state()["AAA"]["alerted"] is None
    mm.send = lambda t, msg: (sent.append(msg) or True)
    r = mm.run_once(now=T0)
    assert r["sent"] and len(sent) == 1 and mm.load_state()["AAA"]["alerted"] == "up"
    assert mm.run_once(now=T0)["alerts"] == [] and len(sent) == 1   # no repeat


if __name__ == "__main__":
    import traceback
    ok = bad = 0
    for k, f in sorted(globals().items()):
        if k.startswith("test_") and callable(f):
            try:
                f(); ok += 1
            except Exception:
                bad += 1; print(k); traceback.print_exc()
    print(f"{ok} passed, {bad} failed")
    sys.exit(1 if bad else 0)
