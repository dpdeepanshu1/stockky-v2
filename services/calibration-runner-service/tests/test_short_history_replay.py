"""E107 P2 item 18: scripts/short_history_replay.py (offline, fake market-data) plus the runner job wiring. Plain asserts."""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "scripts"))

import jobs as J  # noqa: E402
import short_history_replay as R  # noqa: E402


def candles(day, closes_opens):
    """(hh, mm, o, h, l, c) tuples -> market-data rows."""
    return [{"date": f"{day} {hh:02d}:{mm:02d}", "open": o, "high": h, "low": l, "close": c, "volume": 1000} for hh, mm, o, h, l, c in closes_opens]


def session(day, fn):
    """A full 09:15..15:25 session of 5-minute bars; fn(minute_of_day) -> (o, h, l, c)."""
    rows = []
    for m in range(555, 930, 5):
        o, h, l, c = fn(m)
        rows.append({"date": f"{day} {m // 60:02d}:{m % 60:02d}", "open": o, "high": h, "low": l, "close": c, "volume": 1})
    return rows


def flat(m):
    return 100.0, 100.2, 99.8, 100.0


def falling(m):
    p = 100.0 - (m - 555) * 0.02
    return p, p + 0.1, p - 0.1, p


def rising(m):
    p = 100.0 + (m - 555) * 0.02
    return p, p + 0.1, p - 0.1, p


def fake(rows_by_symbol):
    return lambda url: (200, {"candles": rows_by_symbol[next(s for s in rows_by_symbol if f"/history/{s}?" in url)]})


def test_parse_drops_bad_rows_and_keeps_session_hours():
    got = R.parse_candles([{"date": "2026-10-01 09:15", "open": 1, "high": 2, "low": 1, "close": 1.5}, {"date": "2026-10-01 08:00", "open": 1, "high": 2, "low": 1, "close": 1},
                           {"date": "2026-10-01 10:00", "open": 1, "high": 0.5, "low": 1, "close": 1}, {"date": "bad"}, {"date": "2026-10-01 15:30", "open": 1, "high": 2, "low": 1, "close": 1}])
    assert list(got) == ["2026-10-01"] and [b[0] for b in got["2026-10-01"]] == [555]


def test_short_hits_target_in_a_falling_market_and_stop_in_a_rising_one():
    fall = R.replay({"d": R.parse_candles(session("2026-10-01", falling))["2026-10-01"]}, "SELL", [600], 1.0, 2.0, 0.08)
    rise = R.replay({"d": R.parse_candles(session("2026-10-01", rising))["2026-10-01"]}, "SELL", [600], 1.0, 2.0, 0.08)
    assert fall[0]["outcome"] == "target" and fall[0]["net_pct"] > 1.9
    assert rise[0]["outcome"] == "stop" and rise[0]["net_pct"] < -1.0


def test_bar_touching_both_counts_as_the_stop():
    bars = [(600, 100.0, 100.0, 100.0, 100.0), (605, 100.0, 101.5, 97.0, 100.0)]
    t = R.replay({"d": bars}, "SELL", [600], 1.0, 2.0, 0.0)[0]
    assert t["outcome"] == "stop"


def test_unresolved_trade_is_covered_at_1455_not_held():
    bars = [(m, 100.0, 100.1, 99.9, 100.0) for m in range(555, 930, 5)]
    t = R.replay({"d": bars}, "SELL", [600], 1.0, 2.0, 0.08)[0]
    assert t["outcome"] == "eod" and abs(t["net_pct"] + 0.08) < 1e-9


def test_buy_control_is_the_mirror():
    s = {"d": R.parse_candles(session("2026-10-01", falling))["2026-10-01"]}
    assert R.replay(s, "BUY", [600], 1.0, 2.0, 0.0)[0]["outcome"] == "stop"


def test_not_enough_data_when_the_source_has_no_bars():
    rc, out = R.run(lambda url: (404, None), "http://x", ["AAA"], [600], 1.0, 2.0, 0.08)
    assert rc == 0 and "SHORT REPLAY: NOT_ENOUGH_DATA" in out and "no 5-minute bars for: AAA" in out


def test_pass_in_a_falling_market_and_fail_with_drift_note_in_a_rising_one():
    days = ["2026-10-%02d" % d for d in range(1, 13)]
    fall = {"AAA": sum((session(d, falling) for d in days), [])}
    rc, out = R.run(fake(fall), "http://x", ["AAA"], [600, 660, 720], 1.0, 2.0, 0.08, min_sessions=10, min_trades=20)
    assert rc == 0 and "SHORT REPLAY: PASS" in out
    rise = {"AAA": sum((session(d, rising) for d in days), [])}
    rc, out = R.run(fake(rise), "http://x", ["AAA"], [600, 660, 720], 1.0, 2.0, 0.08, min_sessions=10, min_trades=20)
    assert rc == 1 and "SHORT REPLAY: FAIL" in out and "rising market" in out


def test_break_even_is_one_over_one_plus_rr():
    s = R.summarize([{"day": "d", "outcome": "stop", "net_pct": -1.0}], 1.0, 2.0)
    assert abs(s["be_win"] - 1 / 3) < 1e-9


def test_bad_usage_is_exit_2():
    assert R.main(["--stop-pct", "0"]) == 2 and R.main(["--entry-times", "nonsense"]) == 2


def test_runner_job_is_registered_read_only_and_builds_the_command():
    job = J.JOBS["short_history_replay"]
    ctx = {"python": "py", "scripts": "/s", "md": "http://md", "ps": "http://ps", "run": "/r"}
    steps = job["builder"](ctx, {})
    argv = steps[0]["argv"]
    assert argv[1].endswith("short_history_replay.py") and "--base" in argv and "http://md" in argv and steps[0]["ok"] == [0]
