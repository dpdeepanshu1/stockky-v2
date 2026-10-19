"""group446: scripts/service_onoff_advice.py. Plain asserts (python3 scripts/test_service_onoff_advice.py) or pytest."""
from __future__ import annotations

import csv
import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import service_onoff_advice as s  # noqa: E402

IST = s.IST


def row(service, sym, day, pnl, hh=10, entry_day=None):
    ex = datetime.strptime(day, "%Y-%m-%d").replace(hour=14, minute=30, tzinfo=IST)
    en = datetime.strptime(entry_day or day, "%Y-%m-%d").replace(hour=hh, minute=0, tzinfo=IST)
    return {"service": service, "id": None, "symbol": sym, "entry": en, "exit": ex, "day": day, "held_min": 60.0, "pnl": float(pnl), "ret": None,
            "reason": "x", "qty": 1.0, "origin": "system"}


DAYS = [f"2026-10-{d:02d}" for d in (12, 13, 14, 15, 16)]


def test_thin_sample_makes_no_call():
    rows = [row("real-trade", "A", "2026-10-12", -500), row("real-trade", "B", "2026-10-13", -500)]
    j = s.judge(rows)
    assert j["verdict"] == s.THIN and "needs at least 8" in j["why"][0]


def test_consistent_loser_is_turned_off():
    rows = [row("real-trade", f"S{i}", DAYS[i % 5], -100 if i % 4 else 30) for i in range(12)]
    j = s.judge(rows)
    assert j["net"] < 0 and j["net_without_best"] < 0 and j["verdict"] == s.OFF


def test_losing_streak_turns_off_even_with_decent_profit_factor():
    rows = [row("position-stocks", "A", DAYS[0], 400), row("position-stocks", "B", DAYS[0], 300)]
    rows += [row("position-stocks", f"L{i}", DAYS[1 + i // 2], -60) for i in range(6)]
    rows += [row("position-stocks", "W", DAYS[4], 20)] * 0
    j = s.judge(rows, min_n=8, min_days=3)
    assert j["n"] == 8 and j["losing_streak_days"] == 3 and j["net"] > 0
    assert j["verdict"] == s.WATCH            # streak needs net < 0 to pause; a profitable record with a bad tail is WATCH
    rows += [row("position-stocks", "M", DAYS[4], -400)]
    assert s.judge(rows)["verdict"] == s.OFF   # now net < 0 and 4 losing days in a row


def test_steady_winner_is_kept_but_one_lucky_trade_is_only_watch():
    good = [row("real-trade", f"G{i}", DAYS[i % 5], 80 if i % 3 else -40) for i in range(12)]
    assert s.judge(good)["verdict"] == s.KEEP
    lucky = [row("real-trade", f"L{i}", DAYS[i % 5], -20) for i in range(11)] + [row("real-trade", "BIG", DAYS[2], 400)]
    j = s.judge(lucky)
    assert j["net"] > 0 and j["net_without_best"] < 0 and j["verdict"] == s.WATCH and "rests on one trade" in " ".join(j["why"])


def test_no_losing_trade_does_not_crash():
    rows = [row("real-trade", f"W{i}", DAYS[i % 5], 50) for i in range(10)]
    j = s.judge(rows)
    assert j["profit_factor"] is None and j["verdict"] == s.KEEP


def test_overlap_and_better_service():
    rt = [row("real-trade", f"R{i}", DAYS[i % 5], 60 if i % 3 else -30) for i in range(12)]
    ps = [row("position-stocks", f"P{i}", DAYS[i % 5], -50 if i % 4 else 10) for i in range(12)]
    ps[0] = row("position-stocks", "R0", DAYS[0], -20)        # same symbol, same entry day as rt[0]
    adv = s.advise(rt + ps)
    assert adv["overlap"]["symbols"] == 1 and adv["better"] == "real-trade"
    assert adv["services"]["real-trade"]["verdict"] == s.KEEP and adv["services"]["position-stocks"]["verdict"] == s.OFF
    txt = s.build_text(adv, 10)
    assert "real-trade-service" in txt and "TURN OFF" in txt and "it never changes a switch" in txt and "pause it" in txt


def test_csv_roundtrip_and_cli_exit_codes():
    with tempfile.TemporaryDirectory() as t:
        p = Path(t) / "daily_trades.csv"
        with open(p, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["day", "service", "symbol", "entry_ist", "exit_ist", "held_min", "pnl", "return_pct", "exit_reason", "quantity", "origin"])
            for i in range(12):
                dy = DAYS[i % 5]
                w.writerow([dy, "real-trade", f"S{i}", f"{dy} 10:00", f"{dy} 14:30", 270, -100 if i % 4 else 30, -1.0, "stop_hit", 1, "system"])
        out, js = Path(t) / "o.txt", Path(t) / "o.json"
        rc = s.main(["--from-csv", str(p), "--as-of", "2026-10-17T10:00:00+05:30", "--out", str(out), "--json", str(js)])
        assert rc == 1 and "TURN OFF" in out.read_text()
        assert json.loads(js.read_text())["services"]["real-trade"]["verdict"] == s.OFF
        assert s.main(["--from-csv", str(p), "--as-of", "2027-01-01T00:00:00+05:30"]) == 1          # window empty: no trades -> exit 1, no call
        assert s.rows_from_csv(str(p))[0]["entry"].strftime("%H:%M") == "10:00"


def test_unfinished_day_is_not_counted():
    rows = [row("real-trade", f"S{i}", DAYS[i % 5], -100) for i in range(10)] + [row("real-trade", "TODAY", "2026-10-19", -88)]
    mid = datetime(2026, 10, 19, 14, 11, tzinfo=IST)                 # Monday, market open
    keep, today = s.split_in_progress(rows, mid)
    assert [r["symbol"] for r in today] == ["TODAY"] and len(keep) == 10
    assert s.judge(keep)["losing_streak_days"] == 5 and s.judge(rows)["losing_streak_days"] == 6      # the old behaviour counted it
    after = datetime(2026, 10, 19, 16, 5, tzinfo=IST)
    assert s.split_in_progress(rows, after) == (rows, [])             # after 15:45 the day is complete
    assert s.split_in_progress(rows, mid, include_today=True) == (rows, [])
    assert s.split_in_progress(rows, datetime(2026, 10, 17, 11, 0, tzinfo=IST)) == (rows, [])           # Saturday: nothing is in progress


def test_since_and_today_note_in_cli():
    with tempfile.TemporaryDirectory() as t:
        p = Path(t) / "daily_trades.csv"
        with open(p, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["day", "service", "symbol", "entry_ist", "exit_ist", "held_min", "pnl", "return_pct", "exit_reason", "quantity", "origin"])
            for i in range(12):
                dy = DAYS[i % 5]
                w.writerow([dy, "real-trade", f"S{i}", f"{dy} 10:00", f"{dy} 14:30", 270, -100, -1.0, "stop_hit", 1, "system"])
            w.writerow(["2026-10-19", "real-trade", "TODAY", "2026-10-19 10:00", "2026-10-19 13:30", 210, -88, -4.9, "stop_hit", 1, "system"])
        out = Path(t) / "o.txt"
        assert s.main(["--from-csv", str(p), "--as-of", "2026-10-19T14:11:00+05:30", "--out", str(out)]) == 1
        txt = out.read_text()
        assert "still in progress" in txt and "13 trades" not in txt and "12 trades over 5 day(s)" in txt
        out2 = Path(t) / "o2.txt"
        assert s.main(["--from-csv", str(p), "--as-of", "2026-10-19T16:10:00+05:30", "--out", str(out2)]) == 1
        assert "13 trades over 6 day(s)" in out2.read_text() and "in progress" not in out2.read_text()
        out3 = Path(t) / "o3.txt"
        s.main(["--from-csv", str(p), "--since", "2026-10-15", "--as-of", "2026-10-19T16:10:00+05:30", "--out", str(out3)])
        t3 = out3.read_text()
        assert "window starts 2026-10-15" in t3 and "NOT ENOUGH DATA" in t3                 # 3 days and few trades: no call
        assert s.main(["--from-csv", str(p), "--since", "tomorrow"]) == 2


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
            print("ok", k)
