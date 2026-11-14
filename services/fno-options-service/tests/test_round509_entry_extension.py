"""Round 509 (E528): every trade records how far the index had already moved when it was opened (observation only), the report shows it and buckets results by it."""
from __future__ import annotations

from datetime import date, datetime

from backtest import replay, report, sim
from fno import signals
from fno.params import Params
from fno.provider import IST
from test_backtests import build_world, record_day

T0 = datetime(2026, 11, 12, 9, 40, tzinfo=IST).timestamp()


def _bars(closes, day=date(2026, 11, 12)):
    t0 = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST).timestamp()
    return [{"t": t0 + 300 * i, "o": c - 1, "h": c + 2, "l": c - 3, "c": c, "v": 0} for i, c in enumerate(closes)]


def test_extension_is_the_move_from_the_open_and_the_place_in_the_range():
    b = _bars([100, 101, 102, 104, 103, 105])                       # first open 99, last close 105, high 107, low 97
    e = signals.entry_extension(b)
    assert e["from_open_pct"] == round((105 / 99 - 1) * 100, 3) and e["bars"] == 6.0
    assert e["pos_in_range"] == round((105 - 97) / (107 - 97), 3) and e["range_pct"] == round(10 / 99 * 100, 3)


def test_extension_ignores_an_earlier_day_and_is_empty_with_too_little_data():
    old = _bars([50, 60, 70], day=date(2026, 11, 11))
    assert signals.entry_extension(old + _bars([100])) == {}                     # only one bar of today
    assert signals.entry_extension([]) == {} and signals.entry_extension(old + _bars([100, 101]))["bars"] == 2.0


def _t(opt, moved, pos, net, **k):
    t = {"entry_ts": T0, "exit_ts": T0 + 600, "underlying": "NIFTY", "opt_type": opt, "strike": 25000.0, "strategy": "CORE", "signal_score": 80, "lots": 1, "entry_premium": 100.0,
         "exit_premium": 90.0, "max_premium": 100.0, "min_premium": 90.0, "sl_price": 75.0, "exit_reason": "SL", "net_pnl": net, "entry_ctx": {"from_open_pct": moved, "pos_in_range": pos}}
    t.update(k)
    return t


def test_a_put_is_extended_when_the_market_has_FALLEN_and_sits_at_the_low():
    mv, rp = report._directional(_t("PE", -0.6, 0.05, 0))
    assert round(mv, 2) == 0.6 and round(rp, 2) == 0.95
    assert report._directional(_t("CE", 0.3, 0.9, 0)) == (0.3, 0.9) and report._directional({"opt_type": "CE"}) == (None, None)


def test_the_buckets_split_by_move_and_say_how_many_trades_have_no_field():
    trades = [_t("CE", 0.15, 0.6, 1000.0), _t("CE", 0.33, 0.95, -4000.0), _t("PE", -0.7, 0.02, -3000.0), _t("CE", 0.9, 1.0, -500.0), {"opt_type": "CE", "net_pnl": 5.0}]
    txt = "\n".join(report.extension_lines(trades))
    assert "moved < 0.2% from the open" in txt and "trades    1  win 100%  net      1000.00" in txt
    assert "moved 0.2% - 0.5%" in txt and "net     -4000.00" in txt
    assert "moved >= 0.5%" in txt and "trades    2  win 0%  net     -3500.00" in txt and "1 trade(s) without the field" in txt
    assert report.extension_lines([{"opt_type": "CE"}]) == []


def test_the_replay_report_shows_the_columns_and_the_bucket_block():
    rp = {"kind": "REPLAY", "metrics": {"overall": {}}, "verdict": {}, "warnings": [], "trades": [_t("CE", 0.33, 0.95, -4000.0)]}
    txt = report.render_text(rp)
    line = [x for x in txt.splitlines() if "25000" in x][0]
    assert line.rstrip().endswith("+0.33%    95%") and "BY MOVE FROM THE OPEN AT ENTRY" in txt and txt.index("BY MOVE") < txt.index("VERDICT")
    assert "BY MOVE" not in report.render_text({"kind": "REPLAY", "metrics": {"overall": {}}, "verdict": {}, "trades": [{"entry_ts": T0, "opt_type": "CE"}]})


def test_the_live_engine_path_and_the_simulation_both_record_it(tmp_path):
    daily, _ = build_world(n_days=160, n_intraday=0, start_px=24000.0)
    day = date(2026, 11, 10)
    record_day(str(tmp_path), day, [24500 + i * 2 for i in range(60)] + [24620 + (i * 8) for i in range(60)])
    r = replay.run(str(tmp_path), ["2026-11-10"], {"NIFTY": [c for c in daily if datetime.fromtimestamp(c["t"], IST).date() < day]}, {"FNO_SIGNAL_MIN_SCORE": 30.0}, ["NIFTY"], vix_default=14.0)
    assert r["trades"] and all(t["entry_ctx"].get("from_open_pct") is not None and t["entry_ctx"]["bars"] >= 2 for t in r["trades"])
    d2, intr = build_world(n_days=160, n_intraday=10)
    s = sim.run("NIFTY", d2, intr, p=Params(min_score=30, max_trades_per_day=2))
    assert s["trades"] and all("from_open_pct" in t["entry_ctx"] for t in s["trades"])


def test_a_suspect_day_trade_is_left_out_of_the_buckets_and_the_report_says_so():
    trades = [_t("CE", 0.27, 0.56, 362.2, suspect_day=True), _t("CE", 0.33, 1.0, -4000.0)]
    txt = "\n".join(report.extension_lines(trades))
    assert "moved 0.2% - 0.5%            trades    1  win 0%  net     -4000.00" in txt and "1 trade(s) on a late-start / holed day (*) left out" in txt
    assert report.extension_lines([_t("CE", 0.27, 0.56, 362.2, suspect_day=True)]) == []
