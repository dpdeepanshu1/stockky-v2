"""group442: scripts/daily_trade_report.py. Plain asserts (python3 scripts/test_daily_trade_report.py) or pytest."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import daily_trade_report as d  # noqa: E402


def _rt(pid, sym, o, c, net, ret, reason, origin="system"):
    return {"position_id": pid, "symbol": sym, "opened_at": o, "closed_at": c, "net_pnl": net, "gross_pnl": net + 20, "return_pct": ret, "exit_reason": reason, "quantity": 10, "origin": origin}


RT = {"records": [
    _rt(1, "AAA", "2026-10-15T03:50:00Z", "2026-10-15T09:20:00Z", 420.0, 1.4, "target_hit"),            # 09:20 -> 14:50 IST
    _rt(2, "BBB", "2026-10-15T04:05:00Z", "2026-10-15T05:35:00Z", -310.5, -1.1, "stop_hit"),            # 09:35 -> 11:05 IST
    _rt(3, "CCC", "2026-10-14T05:00:00Z", "2026-10-16T04:10:00", 150.0, 0.5, "eod_squareoff"),           # overnight, naive close time = UTC
    _rt(4, "IMP", "2026-10-10T05:00:00Z", "2026-10-16T05:00:00Z", 999.0, 9.0, "manual", origin="imported"),
    {"position_id": 5, "symbol": "BAD", "opened_at": None, "closed_at": "2026-10-16T05:00:00Z", "net_pnl": 1},   # no entry time -> skipped
]}
PS = {"trades": [
    {"id": 11, "symbol": "DDD", "status": "TARGET_HIT", "entry_price": 100.0, "exit_price": 101.0, "realized_pnl": 55.0, "quantity": 50, "opened_at": "2026-10-16T03:50:00Z", "closed_at": "2026-10-16T04:20:00Z", "exit_reason": "target"},
    {"id": 12, "symbol": "EEE", "status": "STOP_HIT", "entry_price": 200.0, "exit_price": 198.0, "realized_pnl": -80.0, "quantity": 40, "opened_at": "2026-10-16T04:00:00Z", "closed_at": "2026-10-16T04:10:00Z"},
    {"id": 13, "symbol": "ERR", "status": "ERROR", "entry_price": 1, "exit_price": None, "realized_pnl": None, "opened_at": "2026-10-16T04:00:00Z", "closed_at": None},
    {"id": 14, "symbol": "PND", "status": "PENDING_RECONCILE", "entry_price": 1, "exit_price": 1, "realized_pnl": 3, "opened_at": "2026-10-16T04:00:00Z", "closed_at": "2026-10-16T04:30:00Z"},
]}


def test_rows_convert_to_ist_and_skip_unsettled_or_incomplete():
    rt = d.rows_from_rt(RT)
    assert [r["symbol"] for r in rt] == ["AAA", "BBB", "CCC", "IMP"]
    a = rt[0]
    assert a["entry"].strftime("%H:%M") == "09:20" and a["exit"].strftime("%H:%M") == "14:50" and a["day"] == "2026-10-15" and abs(a["held_min"] - 330) < 1e-6
    assert rt[2]["day"] == "2026-10-16" and rt[2]["held_min"] > 24 * 60 - 1000
    ps = d.rows_from_ps(PS)
    assert [r["symbol"] for r in ps] == ["DDD", "EEE"] and ps[0]["pnl"] == 55.0 and abs(ps[0]["ret"] - 1.0) < 1e-9
    assert ps[0]["reason"] == "target" and ps[1]["reason"] == "stop_hit"          # a missing reason falls back to the lower-cased status


def test_origin_filter():
    assert [r["symbol"] for r in d.rows_from_rt(RT, "system")] == ["AAA", "BBB", "CCC"]
    assert [r["symbol"] for r in d.rows_from_rt(RT, "imported")] == ["IMP"]


def test_dedupe_and_stats():
    rows = d.rows_from_rt(RT) + d.rows_from_rt(RT)
    assert len(d.dedupe(rows)) == 4
    s = d.stats(d.rows_from_rt(RT, "system"))
    assert s == {"n": 3, "wins": 2, "losses": 1, "win_pct": 66.7, "net": 259.5, "avg": 86.5, "avg_ret": 0.267}


def test_hold_buckets_and_formats():
    assert d.hold_bucket(5) == "< 15 min" and d.hold_bucket(15) == "15-60 min" and d.hold_bucket(200) == "3 h - close" and d.hold_bucket(3000) == "overnight+"
    assert d.fmt_hold(45) == "45m" and d.fmt_hold(150) == "2.5h" and d.fmt_hold(3000) == "2.1d"
    assert d.money(1234.4) == "+1,234" and d.money(-5) == "-5" and d.money(None) == "-" and d.pct(None) == "-"


def test_report_has_the_day_table_hour_tables_and_best_worst():
    rows = d.dedupe(d.rows_from_rt(RT, "system") + d.rows_from_ps(PS))
    txt = d.build_report(rows, 10, min_n=2)
    assert "PER DAY (newest first)" in txt and "2026-10-16" in txt and "2026-10-15" in txt
    assert "AAA" in txt and "09:20" in txt and "14:50" in txt and "+420" in txt and "-310" in txt
    assert "By ENTRY hour (IST)" in txt and "By EXIT hour (IST)" in txt and "By HOLD length" in txt and "By EXIT reason" in txt
    assert "Best : AAA" in txt and "Worst: BBB" in txt
    assert txt.index("2026-10-16   ") < txt.index("2026-10-15   ")                      # newest day first
    tables = lambda t: t.split("OVER THE WHOLE WINDOW")[1].split("FLAGS")[0]  # noqa: E731
    assert " (few)" not in tables(d.build_report(rows, 10, min_n=1))
    assert " (few)" in tables(d.build_report(rows, 10, min_n=50))


def test_flags_only_appear_with_enough_trades():
    rows = []
    for i in range(9):
        rows.append(d._row("real-trade", i, f"S{i}", "2026-10-15T04:00:00Z", "2026-10-15T05:00:00Z", -50.0, -0.5, "stop_hit"))      # 09:30 IST entries, all losers
    txt = d.build_report(rows, 10, min_n=8)
    assert "entries in the 09:00 hour lost" in txt and "stop exits cost" in txt
    few = d.build_report(rows[:3], 10, min_n=8)
    assert "FLAGS (only buckets with >= 8 trades):\n  none" in few


def test_csv_export():
    rows = d.rows_from_rt(RT, "system")
    out = d.to_csv(rows).splitlines()
    assert out[0].startswith("day,service,symbol,entry_ist,exit_ist,held_min,pnl")
    assert len(out) == 4 and any("AAA" in x for x in out[1:])


def test_cli_offline_writes_txt_and_csv_and_exits_1_without_trades():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "rt.json").write_text(json.dumps(RT))
    (tmp / "ps.json").write_text(json.dumps(PS))
    rc = d.main(["--rt-file", str(tmp / "rt.json"), "--ps-file", str(tmp / "ps.json"), "--as-of", "2026-10-17T10:00:00Z", "--days", "10", "--origin", "system",
                 "--out", str(tmp / "r.txt"), "--csv", str(tmp / "r.csv"), "--min-n", "2"])
    assert rc == 0 and "DAILY TRADE REPORT" in (tmp / "r.txt").read_text() and (tmp / "r.csv").exists()
    old = d.main(["--rt-file", str(tmp / "rt.json"), "--ps-file", str(tmp / "ps.json"), "--as-of", "2026-12-31T00:00:00Z", "--days", "3", "--out", str(tmp / "o.txt")])
    assert old == 1 and "No closed trades" in (tmp / "o.txt").read_text()


def test_network_failure_is_reported_not_raised():
    rc = d.main(["--rt-url", "http://127.0.0.1:1", "--ps-url", "http://127.0.0.1:1", "--token", "x", "--days", "1"])
    assert rc == 1


def test_unbooked_zero_pnl_record_is_not_a_trade():
    """2026-10-20: HDFCLIFE-style record (pnl exactly 0, no return %, closed before the open) must not count as a trade."""
    ghost = {"position_id": 9, "symbol": "GHOST", "opened_at": "2026-10-19T07:40:00Z", "closed_at": "2026-10-20T03:27:00Z", "net_pnl": 0.0, "gross_pnl": 0.0,
             "return_pct": None, "exit_reason": "eod_squareoff", "quantity": 5, "origin": "system"}
    rows = d.rows_from_rt({"records": [ghost, _rt(1, "AAA", "2026-10-15T03:50:00Z", "2026-10-15T09:20:00Z", 420.0, 1.4, "target_hit")]})
    assert [r["symbol"] for r in rows] == ["AAA"]
    real_flat = dict(ghost, symbol="FLAT", return_pct=0.0)      # a genuine break-even trade has a return % and stays
    assert [r["symbol"] for r in d.rows_from_rt({"records": [real_flat]})] == ["FLAT"]


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(f"{n} passed")
