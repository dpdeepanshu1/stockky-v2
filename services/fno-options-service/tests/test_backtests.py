"""F&O round 4: metrics, the SIMULATED, EOD and REPLAY backtests, the CSV loaders and the report. Offline, synthetic data."""
from __future__ import annotations

import csv
import json
import os
from datetime import date, datetime, timedelta

import pytest

from backtest import cli, eod, metrics, replay, report, sim
from fno import paper, recorder
from fno.models import ContractRef
from fno.params import Params
from fno.provider import IST
from helpers import make_chain


def weekdays(n: int, end: date = date(2026, 10, 30)):
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


def build_world(n_days=160, n_intraday=40, daily_drift=0.0015, intraday_step=5.0, start_px=21000.0):
    """Daily candles for n_days and 5-minute candles (09:15-15:30) for the last n_intraday days, all trending the same way."""
    days = weekdays(n_days)
    daily, intr, px = [], [], start_px
    for i, d in enumerate(days):
        o = px
        c = o * (1 + daily_drift)
        t0 = datetime(d.year, d.month, d.day, 9, 15, tzinfo=IST).timestamp()
        daily.append({"t": t0, "o": o, "h": max(o, c) * 1.002, "l": min(o, c) * 0.998, "c": c, "v": 1e6})
        if i >= n_days - n_intraday:
            p, step = o, (c - o) / 75.0
            for b in range(75):
                bo, bc = p, p + step
                intr.append({"t": t0 + b * 300, "o": bo, "h": max(bo, bc) + 3, "l": min(bo, bc) - 3, "c": bc, "v": 1000 + b})
                p = bc
        px = c
    return daily, intr


# ---- metrics -----------------------------------------------------------------------------------------------------------------
def closed(net, gross=None, reason="TARGET", day=0, und="NIFTY", ot="CE", exp=None):
    c = ContractRef(und, exp or "2026-11-24", 24500, ot, 65, 1, "NSE_FNO")
    base = datetime(2026, 10, 1, 10, tzinfo=IST).timestamp() + day * 86400
    p = paper.open_position(c, 1, 100.0, base, Params())
    p.status, p.exit_ts, p.exit_reason, p.exit_premium = "CLOSED", base + 3600, reason, 100.0
    p.net_pnl, p.gross_pnl, p.charges = net, net + 50 if gross is None else gross, 50.0
    return p


def test_metrics_block_drawdown_streak_and_folds():
    ps = [closed(x, day=i) for i, x in enumerate([500, -200, -300, 400, 600, -100, 300, 200, -50, 450, 100, 250])]
    m = metrics.summarize(ps, capital=100000)
    o = m["overall"]
    assert o["trades"] == 12 and o["net"] == 2150 and o["wins"] == 8 and o["longest_losing_streak"] == 2
    assert o["max_drawdown"] == 500 and o["profit_factor"] == pytest.approx(2800 / 650, abs=0.01) and o["return_pct"] == 2.15
    assert len(m["folds"]) == 3 and m["days"]["traded_days"] == 12 and len(m["equity_curve"]) == 12


def test_verdict_never_says_safe():
    few = metrics.verdict(metrics.summarize([closed(100)], 100000))
    assert few["status"] == "NOT_PROVEN" and any("too few" in r for r in few["reasons"])
    good = metrics.verdict(metrics.summarize([closed(300 + i, day=i) for i in range(40)], 100000))
    assert good["status"] == "PASSED_THIS_TEST_ONLY"
    bad = metrics.verdict(metrics.summarize([closed(-100, day=i) for i in range(40)], 100000))
    assert bad["status"] == "NOT_PROVEN" and any("not positive" in r for r in bad["reasons"])


def test_metrics_empty_is_safe():
    m = metrics.summarize([], 100000)
    assert m["overall"]["trades"] == 0 and m["folds"] == [] and m["equity_curve"] == []


# ---- SIM ---------------------------------------------------------------------------------------------------------------------
def test_sim_trades_an_uptrend_with_calls_only():
    daily, intr = build_world()
    r = sim.run("NIFTY", daily, intr, p=Params(min_score=30, max_trades_per_day=2))
    assert r["kind"] == "SIM" and r["days"] >= 30 and r["metrics"]["overall"]["trades"] > 0
    assert {t["opt_type"] for t in r["trades"]} == {"CE"}
    assert all(t["status"] == "CLOSED" and t["exit_reason"] for t in r["trades"])
    assert all(t["lots"] >= 1 and t["charges"] > 0 for t in r["trades"])


def test_sim_downtrend_buys_puts():
    daily, intr = build_world(daily_drift=-0.0015, intraday_step=-5, start_px=26000)
    r = sim.run("NIFTY", daily, intr, p=Params(min_score=30))
    assert r["metrics"]["overall"]["trades"] > 0 and {t["opt_type"] for t in r["trades"]} == {"PE"}


def test_sim_respects_daily_trade_limit_and_squareoff():
    daily, intr = build_world()
    r = sim.run("NIFTY", daily, intr, p=Params(min_score=30, max_trades_per_day=1))
    per_day = {}
    for t in r["trades"]:
        d = datetime.fromtimestamp(t["entry_ts"], IST).date()
        per_day[d] = per_day.get(d, 0) + 1
        assert datetime.fromtimestamp(t["exit_ts"], IST).time() <= datetime(2026, 1, 1, 15, 31).time()
    assert max(per_day.values()) == 1


def test_sim_has_no_lookahead():
    """Changing the LAST day's data must not change any earlier trade."""
    daily, intr = build_world()
    r1 = sim.run("NIFTY", daily, intr, p=Params(min_score=30))
    last_day = datetime.fromtimestamp(intr[-1]["t"], IST).date()
    intr2 = [dict(b) if datetime.fromtimestamp(b["t"], IST).date() != last_day else {**b, "o": b["o"] * 0.9, "h": b["h"] * 0.9, "l": b["l"] * 0.9, "c": b["c"] * 0.9} for b in intr]
    daily2 = [c for c in daily]
    r2 = sim.run("NIFTY", daily2, intr2, p=Params(min_score=30))
    cut = lambda r: [(t["entry_ts"], t["strike"], t["exit_reason"], t["net_pnl"]) for t in r["trades"] if datetime.fromtimestamp(t["entry_ts"], IST).date() != last_day]
    assert cut(r1) == cut(r2)


def test_sim_vix_gate_blocks_everything():
    daily, intr = build_world()
    days = {datetime.fromtimestamp(b["t"], IST).strftime("%Y-%m-%d"): 40.0 for b in intr}
    days["2000-01-03"] = 40.0                                         # the close BEFORE the first simulated day (the gate reads the previous close)
    assert sim.run("NIFTY", daily, intr, vix_by_day=days, p=Params(min_score=30))["metrics"]["overall"]["trades"] == 0


def test_sim_result_is_labelled_simulated():
    daily, intr = build_world(n_intraday=15)
    r = sim.run("NIFTY", daily, intr, p=Params(min_score=30))
    txt = report.render_text(r)
    assert "SIMULATED" in txt and "NOT real prices" in txt and "not a forecast" in txt and "VERDICT" in txt


# ---- EOD ---------------------------------------------------------------------------------------------------------------------
HEAD = "TradDt,BizDt,Sgmt,FinInstrmTp,TckrSymb,FininstrmActlXpryDt,StrkPric,OptnTp,OpnPric,HghPric,LwPric,ClsPric,OpnIntrst,TtlTradgVol\n"


def bhav_text(day: date, spot: float, exp: date, move: float = 1.0):
    rows = ""
    for k in range(int(spot // 50 * 50) - 300, int(spot // 50 * 50) + 301, 50):
        for ot in ("CE", "PE"):
            base = max(5.0, (spot - k if ot == "CE" else k - spot) + 120)
            o = base
            c = base * (1 + 0.2 * move)
            rows += f"{day},{day},FO,IDO,NIFTY,{exp},{k},{ot},{o},{max(o, c) * 1.05},{min(o, c) * 0.95},{c},100000,5000\n"
    rows += f"{day},{day},FO,STO,RELIANCE,{exp},1500,CE,10,11,9,10,1000,100\n"
    return HEAD + rows


def test_parse_bhavcopy_keeps_nifty_index_options_only():
    rows = eod.parse_bhavcopy(bhav_text(date(2026, 10, 5), 24500, date(2026, 10, 13)), "NIFTY")
    assert rows and {r["ot"] for r in rows} == {"CE", "PE"} and all(r["expiry"] == date(2026, 10, 13) for r in rows)
    assert eod.parse_bhavcopy("garbage", "NIFTY") == [] and eod.parse_bhavcopy(bhav_text(date(2026, 10, 5), 24500, date(2026, 10, 13)), "SENSEX") == []


def test_parse_bhavcopy_old_layout():
    old = "INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,CLOSE,OPEN_INT,CONTRACTS,TIMESTAMP\nOPTIDX,NIFTY,13-Oct-2026,24500,CE,100,120,90,110,5000,800,05-Oct-2026\n"
    r = eod.parse_bhavcopy(old, "NIFTY")
    assert len(r) == 1 and r[0]["strike"] == 24500 and r[0]["day"] == date(2026, 10, 5)


def test_eod_run_trades_next_day_open_and_closes_same_day():
    daily, _ = build_world(n_days=160, n_intraday=0)
    days = weekdays(40)
    by = {}
    for d in days:
        spot = next(c["c"] for c in daily if datetime.fromtimestamp(c["t"], IST).date() == d)
        exp = eod.xp.next_weekly_expiry("NIFTY", d + timedelta(days=1))
        by[d] = eod.parse_bhavcopy(bhav_text(d, spot, exp), "NIFTY")
    r = eod.run(daily, by, Params(min_score=20, min_premium=10), lot_size=65)     # daily-only score tops out lower than live (no intraday part)
    assert r["kind"] == "EOD" and r["metrics"]["overall"]["trades"] > 0
    for t in r["trades"]:
        assert t["opt_type"] == "CE" and t["exit_reason"] in ("TARGET", "SL", "TRAIL", "SQUAREOFF")
        assert datetime.fromtimestamp(t["exit_ts"], IST).date() == datetime.fromtimestamp(t["entry_ts"], IST).date()


# ---- expiry helper -----------------------------------------------------------------------------------------------------------
def test_weekly_expiry_weekday_and_holiday_shift():
    from fno import expiry as xp
    assert xp.next_weekly_expiry("NIFTY", date(2026, 11, 10)) == date(2026, 11, 10)             # Tuesday
    assert xp.next_weekly_expiry("NIFTY", date(2026, 11, 11)) == date(2026, 11, 17)
    assert xp.next_weekly_expiry("SENSEX", date(2026, 11, 12)) == date(2026, 11, 12)            # Thursday
    assert xp.next_weekly_expiry("NIFTY", date(2026, 11, 8), holidays=["2026-11-10"]) == date(2026, 11, 9)   # holiday: previous trading day
    assert xp.expiry_for_entry("NIFTY", date(2026, 11, 10)) == date(2026, 11, 17)               # expiry-day entry is blocked: next week
    assert xp.expiry_for_entry("NIFTY", date(2026, 11, 10), block_expiry_day=False, min_days=0) == date(2026, 11, 10)


# ---- REPLAY ------------------------------------------------------------------------------------------------------------------
def record_day(root, day: date, spots, und="NIFTY"):
    """One snapshot per minute from 09:30, with the spot walking through `spots` (one per snapshot)."""
    t0 = datetime(day.year, day.month, day.day, 9, 30, tzinfo=IST)
    for i, sp in enumerate(spots):
        ts = (t0 + timedelta(minutes=i)).timestamp()
        ch = make_chain(spot=sp, expiry="2026-11-17", underlying=und, step=50.0, n_each=24, now=(t0 + timedelta(minutes=i)).replace(tzinfo=None), fetched_at=ts)
        recorder.record(root, ch, ts, {"vix": 14.0})


def test_replay_runs_the_live_engine_on_recorded_chains(tmp_path):
    daily, _ = build_world(n_days=160, n_intraday=0, start_px=24000.0)
    day = date(2026, 11, 10)
    spots = [24500 + i * 2 for i in range(60)] + [24620 + (i * 8) for i in range(60)]          # drifts up, then a strong rally
    record_day(str(tmp_path), day, spots)
    r = replay.run(str(tmp_path), ["2026-11-10"], {"NIFTY": [c for c in daily if datetime.fromtimestamp(c["t"], IST).date() < day]},
                   {"FNO_SIGNAL_MIN_SCORE": 30.0}, ["NIFTY"], vix_default=14.0)
    assert r["kind"] == "REPLAY" and r["days"] == 1
    assert r["metrics"]["overall"]["trades"] >= 1
    assert all(t["status"] == "CLOSED" for t in r["trades"])


def test_replay_with_nothing_recorded_says_so(tmp_path):
    r = replay.run(str(tmp_path), ["2026-11-10"], {}, None, ["NIFTY"])
    assert r["days"] == 0 and "no recorded snapshots" in r["notes"][0]


# ---- CLI loaders and report --------------------------------------------------------------------------------------------------
def test_csv_loaders_and_save(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text("date,open,high,low,close,volume\n2026-11-02,100,110,95,105,10\nbad,1,1,1,1,1\n2026-11-03 09:15,105,111,100,108,\n")
    c = cli.load_candles(str(p))
    assert len(c) == 2 and c[0]["c"] == 105 and c[1]["v"] == 0
    assert cli.load_vix(str(p)) == {"2026-11-02": 105.0, "2026-11-03": 108.0}
    res = {"kind": "SIM", "metrics": metrics.summarize([], 1), "verdict": metrics.verdict(metrics.summarize([], 1))}
    path = report.save(str(tmp_path), "t", res)
    assert os.path.exists(path) and json.load(open(path))["kind"] == "SIM"


def test_cli_end_to_end_sim(tmp_path, capsys):
    daily, intr = build_world(n_intraday=12)
    for name, rows in (("d.csv", daily), ("i.csv", intr)):
        with open(tmp_path / name, "w", newline="") as fh:
            w = csv.writer(fh); w.writerow(["date", "open", "high", "low", "close", "volume"])
            for c in rows:
                w.writerow([datetime.fromtimestamp(c["t"], IST).strftime("%Y-%m-%d %H:%M"), c["o"], c["h"], c["l"], c["c"], c["v"]])
    rc = cli.main_sim(["--daily", str(tmp_path / "d.csv"), "--intraday", str(tmp_path / "i.csv"), "--out", str(tmp_path), "--set", "min_score=30"])
    assert rc == 0 and "F&O OPTIONS BACKTEST" in capsys.readouterr().out
    assert any(f.endswith(".txt") for f in os.listdir(tmp_path / "backtests"))


def test_replay_warns_when_the_recording_starts_late_or_has_a_hole(tmp_path):
    day = date(2026, 11, 10)
    record_day(str(tmp_path), day, [24500 + i for i in range(40)])                 # starts 09:30: after the open
    r = replay.run(str(tmp_path), ["2026-11-10"], {}, None, ["NIFTY"], vix_default=14.0)
    assert any("starts 09:30" in w for w in r["warnings"])
    assert not any("gap" in w for w in r["warnings"])
    # now remove the middle: 09:50..10:09 missing
    path = recorder.snapshot_path(str(tmp_path), "2026-11-10", "NIFTY")
    lines = open(path).read().splitlines()
    open(path, "w").write("\n".join(lines[:20] + lines[40 - 0:]) + "\n") if len(lines) > 40 else open(path, "w").write("\n".join(lines[:10] + lines[30:]) + "\n")
    r2 = replay.run(str(tmp_path), ["2026-11-10"], {}, None, ["NIFTY"], vix_default=14.0)
    assert any("gap" in w for w in r2["warnings"])


def test_eod_cli_refuses_sensex_instead_of_an_empty_result(tmp_path, capsys):
    """The EOD backtest reads the NSE bhavcopy only. SENSEX must be refused loudly (exit 2), not run on BSE files and report '0 trades' as if no signal came."""
    rc = cli.main_eod(["--underlying", "SENSEX", "--daily", str(tmp_path / "d.csv"), "--bhav-dir", str(tmp_path), "--out", str(tmp_path)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "NIFTY" in err and "SENSEX" in err and "sim" in err
    assert not list(tmp_path.glob("**/*.json"))


# ---- round 243: replay audit ------------------------------------------------------------------------------------------------
def test_replay_provider_never_shows_the_replayed_days_own_daily_candle():
    """A daily candle is stamped at the start of its day. The old cut-off (`t < now - 6h`) let TODAY's full-day candle in from 09:15, so the strategies saw the day's final high/low/close early."""
    day = date(2026, 11, 10)
    def candle(d, c):
        return {"t": datetime(d.year, d.month, d.day, tzinfo=IST).timestamp(), "o": c, "h": c + 10, "l": c - 10, "c": c, "v": 1.0}
    daily = [candle(date(2026, 11, 6), 24400.0), candle(date(2026, 11, 9), 24450.0), candle(day, 99999.0), candle(date(2026, 11, 11), 11111.0)]
    prov = replay.ReplayProvider({"NIFTY": []}, {"NIFTY": daily})
    for hh, mm in ((9, 15), (12, 0), (15, 29)):
        prov.set_time(datetime(2026, 11, 10, hh, mm, tzinfo=IST).timestamp())
        assert [c["c"] for c in prov.daily("NIFTY")] == [24400.0, 24450.0]      # yesterday's yes, today's and tomorrow's never
    prov.set_time(datetime(2026, 11, 11, 9, 15, tzinfo=IST).timestamp())
    assert [c["c"] for c in prov.daily("NIFTY")] == [24400.0, 24450.0, 99999.0]


def test_replay_result_does_not_change_when_todays_daily_candle_is_added(tmp_path):
    daily, _ = build_world(n_days=160, n_intraday=0, start_px=24000.0)
    day = date(2026, 11, 10)
    record_day(str(tmp_path), day, [24500 + i * 2 for i in range(60)] + [24620 + (i * 8) for i in range(60)])
    past = [c for c in daily if datetime.fromtimestamp(c["t"], IST).date() < day]
    leak = past + [{"t": datetime(2026, 11, 10, tzinfo=IST).timestamp(), "o": 24000.0, "h": 26000.0, "l": 23000.0, "c": 26000.0, "v": 1.0}]      # a wild same-day candle
    a = replay.run(str(tmp_path), ["2026-11-10"], {"NIFTY": past}, {"FNO_SIGNAL_MIN_SCORE": 30.0}, ["NIFTY"], vix_default=14.0)
    b = replay.run(str(tmp_path), ["2026-11-10"], {"NIFTY": leak}, {"FNO_SIGNAL_MIN_SCORE": 30.0}, ["NIFTY"], vix_default=14.0)
    assert [(t["entry_ts"], t["opt_type"], t["strike"], t["signal_score"]) for t in a["trades"]] == [(t["entry_ts"], t["opt_type"], t["strike"], t["signal_score"]) for t in b["trades"]]


def test_replay_vix_is_the_most_recent_recorded_one_not_the_last_underlying_iterated():
    t = 1_700_000_000.0
    snaps = {"NIFTY": [{"ts": t + 600, "vix": 18.0, "chain": {}}], "SENSEX": [{"ts": t, "vix": 12.0, "chain": {}}]}
    prov = replay.ReplayProvider(snaps, {})
    prov.set_time(t + 900)
    assert prov.vix() == 18.0                                          # NIFTY's later figure, although SENSEX comes last in the dict
    prov.set_time(t + 100)
    assert prov.vix() == 12.0


def test_sim_decisions_see_the_previous_days_vix_close_not_the_days_own(monkeypatch):
    """The day's own VIX close is not known at 09:30. The gate (and the strategies) get the last close BEFORE the day; pricing may still use the day's level."""
    daily, intr = build_world()
    ds = sorted({datetime.fromtimestamp(b["t"], IST).strftime("%Y-%m-%d") for b in intr})
    v = {d: 12.0 for d in ds}
    v[ds[-1]] = 40.0
    seen = {}
    real = sim.gates.entry_gates
    def spy(now, expiry, vix, *a, **k):
        seen.setdefault(now.strftime("%Y-%m-%d"), set()).add(vix)
        return real(now, expiry, vix, *a, **k)
    monkeypatch.setattr(sim.gates, "entry_gates", spy)
    sim.run("NIFTY", daily, intr, vix_by_day=v, p=Params(min_score=30))
    assert seen[ds[-1]] == {12.0}                                      # the spike day is judged on the calm previous close
    assert seen[ds[5]] == {12.0}
    assert seen[ds[0]] == {15.0}                                       # no earlier close known: the old default
