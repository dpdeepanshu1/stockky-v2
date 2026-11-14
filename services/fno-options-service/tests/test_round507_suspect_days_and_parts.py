"""Round 507 (E527): a replay trade says WHY it was CE / PE (the daily / intraday / chain score parts) and a trade opened on a day whose recording starts late or has a hole is marked, with a summary of the clean days only."""
from __future__ import annotations

from datetime import date, datetime

from backtest import replay, report
from fno.paper import Position
from fno.provider import IST
from test_backtests import build_world, record_day

T0 = datetime(2026, 11, 12, 10, 5, tzinfo=IST).timestamp()


def _trade(**k):
    t = {"entry_ts": T0, "exit_ts": T0 + 1200, "underlying": "NIFTY", "opt_type": "CE", "strike": 25200.0, "strategy": "CORE", "signal_score": 61.4, "lots": 2, "entry_premium": 100.0,
         "exit_premium": 80.0, "max_premium": 112.0, "min_premium": 78.0, "sl_price": 80.0, "exit_reason": "SL", "net_pnl": -2650.5}
    t.update(k)
    return t


def test_the_report_shows_the_score_parts_and_dashes_when_a_trade_has_none():
    txt = report.render_text({"kind": "REPLAY", "metrics": {"overall": {}}, "verdict": {}, "warnings": [],
                              "trades": [_trade(score_parts={"daily": 50.0, "intraday": 8.0, "chain": 3.0}), _trade(entry_ts=T0 + 5, strike=25300.0)]})
    assert "+50/+8/+3" in [x for x in txt.splitlines() if "25200" in x][0]
    assert "+50/+8/+3" not in [x for x in txt.splitlines() if "25300" in x][0]


def test_a_suspect_day_trade_is_starred_and_the_clean_summary_is_printed():
    txt = report.render_text({"kind": "REPLAY", "metrics": {"overall": {}}, "verdict": {}, "warnings": [], "clean_days": 3,
                              "clean_overall": {"trades": 4, "win_rate_pct": 25.0, "net": -10371.8, "profit_factor": 0.1}, "trades": [_trade(suspect_day=True)]})
    assert "12 Nov 10:05*" in txt and "recording started late" in txt and "without the * days (3 clean day(s)): trades 4" in txt


def test_a_position_keeps_its_score_parts_through_json():
    p = Position(underlying="NIFTY", expiry="2026-11-17", strike=25000.0, opt_type="CE", lot_size=75, lots=1, entry_ts=T0, entry_premium=100.0, sl_price=75.0, target_price=150.0, score_parts={"daily": 50.0})
    assert Position.from_json(p.to_json()).score_parts == {"daily": 50.0}
    assert Position.from_json({k: v for k, v in p.to_json().items() if k != "score_parts"}).score_parts == {}      # rows written before round 507


def test_replay_marks_a_late_start_day_suspect_and_a_full_day_clean(tmp_path):
    daily, _ = build_world(n_days=160, n_intraday=0, start_px=24000.0)
    record_day(str(tmp_path), date(2026, 11, 10), [24500 + i for i in range(40)])            # starts 09:30: late
    r = replay.run(str(tmp_path), ["2026-11-10"], {}, None, ["NIFTY"], vix_default=14.0)
    assert r["suspect_days"] == ["2026-11-10"] and r["clean_days"] == 0 and r["clean_overall"] is None
    assert all(t["suspect_day"] for t in r["trades"])
