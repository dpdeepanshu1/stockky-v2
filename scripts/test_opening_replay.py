"""Tests for scripts/opening_replay.py (group 393). Plain asserts: python3 scripts/test_opening_replay.py  or  pytest."""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import opening_replay as orp  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
_n = [0]


def t(h, m, status, pnl, qty=10, close_min=5, day=12, exit_price=100.0, **kw):
    _n[0] += 1
    o = datetime(2026, 10, day, h, m, tzinfo=IST)
    r = {"id": _n[0], "symbol": f"S{_n[0]}", "status": status, "quantity": qty, "realized_pnl": pnl, "exit_price": exit_price,
         "opened_at": o.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
         "closed_at": (o + timedelta(minutes=close_min)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}
    r.update(kw)
    return r


def test_only_settled_window_trades_are_replayed():
    rows = [t(9, 20, "TARGET_HIT", 50), t(9, 50, "TARGET_HIT", 50), t(9, 10, "STOP_HIT", -20), t(9, 30, "ERROR", 0),
            t(9, 30, "STOP_HIT", -9, exit_price=None), t(9, 30, "STOP_HIT", -9, error_message="STOP_HIT_PENDING_RECONCILE"),
            t(9, 30, "STOP_HIT", -9, day=10)]       # Saturday
    rep = orp.replay(rows)
    assert rep["opening_trades"] == 1


def test_size_cut_scales_pnl_and_leaves_one_share_alone():
    rows = [t(9, 20, "TARGET_HIT", 100, qty=10), t(9, 21, "STOP_HIT", -30, qty=1, close_min=1)]
    s = orp.replay(rows)["scenarios"]
    assert s["actual"]["pnl"] == 70.0
    assert s["size_only"]["pnl"] == 50.0 + -30.0


def test_pause_skips_after_two_closed_stopouts_only():
    rows = [t(9, 16, "STOP_HIT", -10, close_min=2), t(9, 18, "STOP_HIT", -10, close_min=2),
            t(9, 21, "TARGET_HIT", 40),                       # opens after both stops closed -> skipped
            t(9, 40, "TARGET_HIT", 5, day=13)]                # another day, unaffected
    s = orp.replay(rows, limit=2)["scenarios"]
    assert s["pause_only"]["skipped"] == 1 and s["pause_only"]["pnl"] == -15.0
    assert s["pause_only"]["skipped_pnl_if_taken"] == 40.0
    assert s["actual"]["pnl"] == 25.0


def test_stopout_still_open_does_not_pause():
    rows = [t(9, 16, "STOP_HIT", -10, close_min=30), t(9, 18, "STOP_HIT", -10, close_min=30), t(9, 20, "TARGET_HIT", 40)]
    assert orp.replay(rows, limit=2)["scenarios"]["pause_only"]["skipped"] == 0


def test_verdicts():
    rows = [t(9, 16, "STOP_HIT", -10, close_min=2), t(9, 18, "STOP_HIT", -10, close_min=2), t(9, 19, "STOP_HIT", -30)]
    assert orp.replay(rows, min_n=30)["verdict"] == "insufficient_sample"
    assert orp.replay(rows, min_n=1)["verdict"] == "protection_helped"


def test_load_merges_files_by_id_and_accepts_bare_list():
    a, b = t(9, 20, "TARGET_HIT", 10), t(9, 21, "TARGET_HIT", 10)
    with tempfile.TemporaryDirectory() as d:
        p1, p2 = Path(d, "a.json"), Path(d, "b.json")
        p1.write_text(json.dumps({"trades": [a, b]})); p2.write_text(json.dumps([b]))
        assert len(orp.load([str(p1), str(p2)])) == 2
        assert orp.main([str(p1), "--out", d]) == 0
        assert Path(d, "opening_replay.json").exists() and Path(d, "opening_replay.md").exists()


def st(h, m, status, pnl, stop_pct, gain, dd, basis="opening_plan", qty=10, entry=100.0, **kw):
    """A trade row that carries its own stop (group 395 export fields)."""
    return t(h, m, status, pnl, qty=qty, entry_price=entry, adaptive_stop_pct=stop_pct, max_gain_pct=gain, max_drawdown_pct=dd, stop_basis=basis, **kw)


def test_old_exports_without_stop_data_say_so():
    rep = orp.replay([t(9, 20, "TARGET_HIT", 50), t(9, 30, "STOP_HIT", -20)])
    assert rep["stops"]["with_stop_data"] == 0 and rep["stops"]["opening_trades"] == 2
    md = orp.to_markdown(rep)
    assert "## Stops" in md and "predate group 395" in md


def test_stop_indicators_by_basis():
    rows = [st(9, 20, "STOP_HIT", -10, 1.0, gain=0.6, dd=-1.0),
            st(9, 21, "STOP_HIT", -10, 1.0, gain=0.1, dd=-1.0),
            st(9, 22, "TARGET_HIT", 30, 1.0, gain=2.0, dd=-0.85),
            st(9, 23, "TARGET_HIT", 30, 1.0, gain=2.0, dd=-0.1),
            st(9, 24, "STOP_HIT", -20, 2.0, gain=0.0, dd=-2.0, basis="legacy")]
    g = orp.replay(rows)["stops"]["groups"]
    assert g["all"]["trades"] == 5 and g["all"]["stop_outs"] == 3
    assert g["opening_plan"]["trades"] == 4 and g["opening_plan"]["stop_outs"] == 2
    assert g["opening_plan"]["stopped_after_running_half_r"] == 1 and g["opening_plan"]["survivors_within_02r_of_stop"] == 1
    assert g["legacy"]["trades"] == 1 and g["legacy"]["median_stop_pct"] == 2.0 and g["legacy"]["pnl_actual"] == -20.0


def test_tighter_stop_what_if_is_computed_from_the_recorded_drawdown():
    # entry 100, qty 10, stop 1% -> at 0.5x the tighter stop is 0.5% = Rs 0.5 x 10 = Rs 5 loss
    rows = [st(9, 20, "STOP_HIT", -10, 1.0, gain=0.0, dd=-1.0),
            st(9, 21, "TARGET_HIT", 30, 1.0, gain=2.0, dd=-0.6),
            st(9, 22, "TARGET_HIT", 30, 1.0, gain=2.0, dd=-0.3)]
    w = orp.replay(rows, stop_mults=(0.5,))["stops"]["groups"]["all"]["tighter"][0]
    assert w["mult"] == 0.5 and w["stopped_at_tighter_level"] == 2
    assert w["pnl_tighter"] == -5.0 + -5.0 + 30.0 and w["pnl_difference"] == (-5.0 - 5.0 + 30.0) - (-10.0 + 30.0 + 30.0)


def test_tighter_level_boundary_counts_and_rows_without_extremes_are_unused():
    rows = [st(9, 20, "TARGET_HIT", 10, 1.0, gain=1.0, dd=-0.5),
            t(9, 21, "TARGET_HIT", 10, entry_price=100.0, adaptive_stop_pct=1.0),
            st(9, 22, "TARGET_HIT", 10, 0.0, gain=1.0, dd=-0.5)]
    s = orp.replay(rows, stop_mults=(0.5,))["stops"]
    assert s["with_stop_data"] == 1 and s["groups"]["all"]["tighter"][0]["stopped_at_tighter_level"] == 1


def test_stop_section_in_markdown_and_blank_mults_skip_the_what_if():
    rows = [st(9, 20, "STOP_HIT", -10, 1.0, gain=0.6, dd=-1.0), st(9, 21, "TARGET_HIT", 30, 1.0, gain=2.0, dd=-0.3, basis="bars")]
    md = orp.to_markdown(orp.replay(rows))
    assert "| opening_plan | 1 |" in md and "| bars | 1 |" in md and "Tighter stop what-if" in md and "A WIDER stop cannot be replayed" in md
    assert "Tighter stop what-if" not in orp.to_markdown(orp.replay(rows, stop_mults=()))


def test_parse_mults_keeps_only_sane_fractions():
    assert orp.parse_mults("0.5, 0.75,1.0,0.05,x,0.5") == [0.5, 0.75]
    assert orp.parse_mults("") == []


def test_cli_stop_mults_flag():
    rows = [st(9, 20, "STOP_HIT", -10, 1.0, gain=0.6, dd=-1.0)]
    with tempfile.TemporaryDirectory() as d:
        p = Path(d, "a.json")
        p.write_text(json.dumps({"trades": rows}))
        assert orp.main([str(p), "--stop-mults", "0.6", "--out", d]) == 0
        rep = json.loads(Path(d, "opening_replay.json").read_text())
        assert rep["stops"]["mults"] == [0.6]


def rt(h, m, reason, pnl, qty=10, entry=100.0, close_min=5, day=12, mode="REAL", pid=None, **kw):
    """A real-trade per-trade record (GET /positions/{mode}/records) opened at h:m IST."""
    _n[0] += 1
    o = datetime(2026, 10, day, h, m, tzinfo=IST)
    r = {"position_id": pid if pid is not None else _n[0], "mode": mode, "symbol": f"R{_n[0]}", "exit_reason": reason, "gross_pnl": pnl, "quantity": qty,
         "entry_price": entry, "exit_price": None, "stop_pct": None, "stop_basis": None, "max_gain_pct": None, "max_drawdown_pct": None,
         "opened_at": o.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
         "closed_at": (o + timedelta(minutes=close_min)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}
    r.update(kw)
    return r


def test_rt_losing_stop_is_a_stop_out_but_a_profitable_stop_exit_is_not():
    lose, win = orp.from_rt_record(rt(9, 20, "stop_hit", -30)), orp.from_rt_record(rt(9, 21, "stop_hit", 12))
    assert lose["status"] == "STOP_HIT" and win["status"] == "STOP_EXIT_NOT_LOSING" and win["status"] not in orp.STOP_STATUSES
    assert orp.from_rt_record(rt(9, 25, "stop_hit", 0))["status"] == "STOP_EXIT_NOT_LOSING"
    assert orp.from_rt_record(rt(9, 22, "broker_stop", -5))["status"] == "STOP_HIT"
    assert orp.from_rt_record(rt(9, 22, "emergency_gap_down", -5))["status"] == "STOP_HIT"
    assert orp.from_rt_record(rt(9, 23, "target_hit", 40))["status"] == "TARGET_HIT"
    assert orp.from_rt_record(rt(9, 24, None, 1))["status"] == "CLOSED"


def test_rt_exit_price_is_derived_when_unknown_and_kept_when_known():
    assert orp.from_rt_record(rt(9, 20, "target_hit", 50, qty=10, entry=100.0))["exit_price"] == 105.0
    assert orp.from_rt_record(rt(9, 20, "target_hit", 50, exit_price=104.9))["exit_price"] == 104.9
    assert orp.from_rt_record(rt(9, 20, "target_hit", 50, quantity=None))["exit_price"] is None


def test_rt_records_replay_like_ledger_rows():
    rows = [rt(9, 16, "stop_hit", -10, close_min=2), rt(9, 18, "stop_hit", -10, close_min=2), rt(9, 21, "target_hit", 40), rt(9, 40, "target_hit", 5, day=13)]
    with tempfile.TemporaryDirectory() as d:
        pth = Path(d, "rt.json")
        pth.write_text(json.dumps({"mode": "REAL", "records": rows}))
        rep = orp.replay(orp.load([str(pth)]), limit=2)
    assert rep["opening_trades"] == 4 and rep["scenarios"]["pause_only"]["skipped"] == 1 and rep["scenarios"]["actual"]["pnl"] == 25.0


def test_rt_records_without_size_or_time_are_not_replayed():
    rows = [rt(9, 20, "target_hit", 50, quantity=None), rt(9, 21, "target_hit", 50, opened_at=None), rt(9, 22, "target_hit", 50)]
    with tempfile.TemporaryDirectory() as d:
        pth = Path(d, "rt.json")
        pth.write_text(json.dumps({"records": rows}))
        assert orp.replay(orp.load([str(pth)]))["opening_trades"] == 1


def test_rt_stop_fields_feed_the_stops_section():
    rows = [rt(9, 20, "stop_hit", -20, stop_pct=2.0, max_gain_pct=1.2, max_drawdown_pct=-2.0, stop_basis="atr"),
            rt(9, 21, "target_hit", 30, stop_pct=2.0, max_gain_pct=4.0, max_drawdown_pct=-1.7, stop_basis="atr")]
    with tempfile.TemporaryDirectory() as d:
        pth = Path(d, "rt.json")
        pth.write_text(json.dumps({"records": rows}))
        g = orp.replay(orp.load([str(pth)]), stop_mults=(0.5,))["stops"]["groups"]
    assert g["atr"]["trades"] == 2 and g["atr"]["stop_outs"] == 1 and g["atr"]["stopped_after_running_half_r"] == 1
    assert g["atr"]["survivors_within_02r_of_stop"] == 1 and g["atr"]["tighter"][0]["stopped_at_tighter_level"] == 2


def test_same_position_id_in_two_files_is_merged_but_modes_stay_apart():
    a, b = rt(9, 20, "target_hit", 10, pid=7), rt(9, 20, "target_hit", 10, pid=7, mode="DEMO")
    with tempfile.TemporaryDirectory() as d:
        p1, p2 = Path(d, "a.json"), Path(d, "b.json")
        p1.write_text(json.dumps({"records": [a]})); p2.write_text(json.dumps({"records": [a, b]}))
        assert len(orp.load([str(p1), str(p2)])) == 2


def test_mixing_ledger_and_real_trade_files_is_refused():
    with tempfile.TemporaryDirectory() as d:
        p1, p2 = Path(d, "a.json"), Path(d, "b.json")
        p1.write_text(json.dumps({"trades": [t(9, 20, "TARGET_HIT", 10)]})); p2.write_text(json.dumps({"records": [rt(9, 21, "target_hit", 10)]}))
        try:
            orp.load([str(p1), str(p2)])
            raise AssertionError("should refuse")
        except ValueError as e:
            assert "separate runs" in str(e)
        assert orp.main([str(p1), str(p2)]) == 2


def test_cli_reads_a_real_trade_export():
    with tempfile.TemporaryDirectory() as d:
        pth = Path(d, "rt.json")
        pth.write_text(json.dumps({"records": [rt(9, 20, "target_hit", 10)]}))
        assert orp.main([str(pth), "--out", d]) == 0 and Path(d, "opening_replay.md").exists()


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
    print("ok")
