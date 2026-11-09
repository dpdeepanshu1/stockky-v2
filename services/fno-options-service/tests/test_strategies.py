"""F&O round 232: the named strategies (fno/strategies.py), their data rules, the engine wiring and the per-strategy backtests. Synthetic candles only."""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backtest import portfolio, sim
from fno import gates, signals, store as st, strategies as S
from fno.params import Params
from helpers import make_daily
from test_engine_paper import FakeProvider, engine, make_cfg

IST = timezone(timedelta(hours=5, minutes=30))


def daily(kind, before):
    end = datetime.fromisoformat(before).replace(tzinfo=IST).timestamp()
    dr, start = {"up": (0.0015, 24000.0), "down": (-0.0015, 26000.0), "flat": (0.0, 24000.0)}[kind]
    return make_daily(drift=dr, start=start, t0=int(end - 119 * 86400))


def day_bars(day, rows):
    t0 = datetime.fromisoformat(day + "T09:15:00").replace(tzinfo=IST).timestamp()
    return [{"t": t0 + 300 * k, "o": o, "h": h, "l": l, "c": c, "v": v} for k, (o, h, l, c, v) in enumerate(rows)]


def ctx(d, b, holidays=(), now=None):
    now = now or datetime.fromtimestamp(b[-1]["t"] + 300, IST)
    return S.build_ctx("NIFTY", d, b, None, now, Params(), holidays)


def quiet(n, base=24000.0, amp=5.0):
    rows, px = [], base
    for k in range(n):
        o, c = px, base + (amp if k % 2 else -amp) * 0.4
        rows.append((o, max(o, c) + amp / 2, min(o, c) - amp / 2, c, 0))
        px = c
    return rows


FLAT = lambda: daily("flat", "2026-11-10")  # noqa: E731


# ---- each pattern fires on its own picture, and not on the wrong one -------------------------------------------------------------
def test_momentum_surge_up_and_down_and_not_on_a_normal_bar():
    up = S.momentum_surge(ctx(FLAT(), day_bars("2026-11-10", quiet(12) + [(24001.0, 24050.0, 24000.0, 24046.0, 0)])))
    dn = S.momentum_surge(ctx(FLAT(), day_bars("2026-11-10", quiet(12) + [(23999.0, 24000.0, 23950.0, 23954.0, 0)])))
    none = S.momentum_surge(ctx(FLAT(), day_bars("2026-11-10", quiet(13))))
    assert up["direction"] == "UP" and dn["direction"] == "DOWN" and none is None


def test_momentum_surge_needs_volume_when_the_feed_has_volume():
    rows = [(o, h, l, c, 1000) for o, h, l, c, _ in quiet(12)] + [(24001.0, 24050.0, 24000.0, 24046.0, 1100)]    # a big bar on ordinary volume
    assert S.momentum_surge(ctx(FLAT(), day_bars("2026-11-10", rows))) is None
    rows[-1] = (24001.0, 24050.0, 24000.0, 24046.0, 2500)
    assert S.momentum_surge(ctx(FLAT(), day_bars("2026-11-10", rows)))["direction"] == "UP"


def test_trend_rider_needs_daily_and_intraday_together():
    up = [(24400 + 6 * k, 24400 + 6 * k + 8, 24400 + 6 * k - 2, 24400 + 6 * k + 6, 1000 + k) for k in range(40)]
    dn = [(24400 - 6 * k, 24400 - 6 * k + 2, 24400 - 6 * k - 8, 24400 - 6 * k - 6, 1000 + k) for k in range(40)]
    assert S.trend_rider(ctx(daily("up", "2026-11-10"), day_bars("2026-11-10", up)))["direction"] == "UP"
    assert S.trend_rider(ctx(daily("down", "2026-11-10"), day_bars("2026-11-10", dn)))["direction"] == "DOWN"
    assert S.trend_rider(ctx(FLAT(), day_bars("2026-11-10", up))) is None                       # intraday up but the daily is not strong
    assert S.trend_rider(ctx(daily("up", "2026-11-10"), day_bars("2026-11-10", dn))) is None    # they disagree


SWEEP = [(24040, 24060, 24035, 24055, 0), (24055, 24070, 24050, 24060, 0), (24060, 24068, 24045, 24050, 0), (24050, 24066, 24048, 24062, 0), (24062, 24070, 24055, 24066, 0),
         (24075, 24100, 24072, 24085, 0), (24085, 24092, 24078, 24080, 0), (24082, 24083, 24055, 24060, 0)]


def test_bear_sweep_is_put_only_and_skipped_in_a_strong_uptrend():
    s = S.bear_sweep(ctx(FLAT(), day_bars("2026-11-10", SWEEP)))
    assert s and s["direction"] == "DOWN" and s["score"] < 0
    assert S.bear_sweep(ctx(daily("up", "2026-11-10"), day_bars("2026-11-10", SWEEP))) is None
    held_above = SWEEP[:-1] + [(24082, 24120, 24078, 24110, 0)]                                  # it kept going up: that was a breakout, not a sweep
    assert S.bear_sweep(ctx(FLAT(), day_bars("2026-11-10", held_above))) is None


def test_bear_pivot_fires_once_on_the_first_close_below_the_pivot():
    rows = [(24010, 24018, 24005, 24012, 0)] * 9 + [(24012, 24013, 24002, 24004, 0), (24004, 24005, 23980, 23985, 0)]
    assert S.bear_pivot(ctx(FLAT(), day_bars("2026-11-10", rows)))["direction"] == "DOWN"
    later = rows + [(23985, 23990, 23975, 23978, 0)]                                              # the NEXT bar is also below: no second signal
    assert S.bear_pivot(ctx(FLAT(), day_bars("2026-11-10", later))) is None
    assert S.bear_pivot(ctx(daily("up", "2026-11-10"), day_bars("2026-11-10", rows))) is None      # bullish daily


def _drift(sign):
    rows, px = [], 24000.0
    for k in range(17):
        o, c = px, px + sign * 6
        rows.append((o, max(o, c) + 1, min(o, c) - 1, c, 0))
        px = c
    vw = S._vwap_series([(0, 0, r[1], r[2], r[3], 0) for r in rows])[-1]
    o = px
    dip_c = vw + sign * 12
    rows.append((o, max(o, dip_c) + 1, min(o, vw - sign * 2 if sign > 0 else o) if sign > 0 else max(o, vw + 2), dip_c, 0))   # pulls back onto the VWAP
    rows.append((dip_c, dip_c + sign * 6, dip_c, dip_c + sign * 5, 0))
    top = max(r[3] for r in rows[-4:-1]) if sign > 0 else min(r[3] for r in rows[-4:-1])
    rows.append((rows[-1][3], top + sign * 12, rows[-1][3], top + sign * 10, 0))
    return rows


@pytest.mark.parametrize("sign,want", [(1, "UP"), (-1, "DOWN")])
def test_drift_edge_enters_on_the_resumption_after_a_pullback_to_vwap(sign, want):
    s = S.drift_edge(ctx(FLAT(), day_bars("2026-11-10", _drift(sign))))
    assert s and s["direction"] == want
    assert S.drift_edge(ctx(FLAT(), day_bars("2026-11-10", quiet(20)))) is None


def test_weekly_opener_only_on_the_first_trading_day_of_the_week_with_the_daily_trend():
    rows = [(24000, 24010, 23990, 24002, 0)] * 6 + [(24002, 24030, 24000, 24025, 0)]
    mon = ctx(daily("up", "2026-11-16"), day_bars("2026-11-16", rows))                           # Monday 16 Nov 2026
    assert S.weekly_opener(mon)["direction"] == "UP"
    assert S.weekly_opener(ctx(daily("up", "2026-11-17"), day_bars("2026-11-17", rows))) is None           # Tuesday, Monday was a trading day
    assert S.weekly_opener(ctx(daily("up", "2026-11-17"), day_bars("2026-11-17", rows), holidays=("2026-11-16",)))["direction"] == "UP"   # Monday holiday
    assert S.weekly_opener(ctx(daily("down", "2026-11-16"), day_bars("2026-11-16", rows))) is None          # against the daily trend


# ---- data rules ------------------------------------------------------------------------------------------------------------------
def test_a_forming_bar_is_dropped_and_stale_bars_give_no_signal():
    rows = quiet(12) + [(24001.0, 24050.0, 24000.0, 24046.0, 0)]
    b = day_bars("2026-11-10", rows)
    forming = datetime.fromtimestamp(b[-1]["t"] + 120, IST)                                       # the surge bar is only 2 minutes old
    c = ctx(FLAT(), b, now=forming)
    assert len(c.bars) == 12 and S.momentum_surge(c) is None
    late = datetime.fromtimestamp(b[-1]["t"] + 300 + 3600, IST)                                   # an hour after the bar closed
    assert S.candidates(ctx(FLAT(), b, now=late), ["MOMENTUM_SURGE"]) == []


def test_a_signal_never_depends_on_bars_that_have_not_closed_yet():
    rows = quiet(12) + [(24001.0, 24050.0, 24000.0, 24046.0, 0)]
    b = day_bars("2026-11-10", rows)
    now = datetime.fromtimestamp(b[-1]["t"] + 300, IST)
    future = day_bars("2026-11-10", rows + [(24046, 24400, 23000, 23100, 0)] * 3)                  # bars after `now`: wild, and must not matter
    assert S.momentum_surge(ctx(FLAT(), b, now=now)) == S.momentum_surge(ctx(FLAT(), future, now=now))


def test_chain_only_vetoes():
    rows = quiet(12) + [(24001.0, 24050.0, 24000.0, 24046.0, 0)]
    c = ctx(FLAT(), day_bars("2026-11-10", rows))
    c.__dict__["chain_parts"] = (-20.0, ["calls written"])
    assert S.momentum_surge(c) is None
    c2 = ctx(FLAT(), day_bars("2026-11-10", rows))
    c2.__dict__["chain_parts"] = (0.0, [])
    assert S.momentum_surge(c2)["direction"] == "UP"


def test_core_through_the_strategy_layer_equals_the_original_signal():
    pv = FakeProvider()
    x = S.build_ctx("NIFTY", pv.daily("NIFTY"), pv.intraday("NIFTY"), {"pcr_oi": 1.3, "spot": 24500.0}, datetime(2026, 11, 10, 10, 30, tzinfo=IST), Params())
    assert S.core_bias(x) == signals.index_bias(pv.daily("NIFTY"), pv.intraday("NIFTY"), {"pcr_oi": 1.3, "spot": 24500.0}, Params())


def test_enabled_keys_defaults_and_reads_the_switches_at_call_time():
    assert S.enabled_keys(SimpleNamespace()) == ["CORE"]
    cfg = SimpleNamespace(FNO_STRAT_CORE=False, FNO_STRAT_BEAR_SWEEP=True, FNO_STRAT_DRIFT_EDGE=True)
    assert S.enabled_keys(cfg) == ["BEAR_SWEEP", "DRIFT_EDGE"]
    cfg.FNO_STRAT_CORE = True
    assert S.enabled_keys(cfg)[0] == "CORE"


def test_a_broken_strategy_does_not_stop_the_others(monkeypatch):
    monkeypatch.setitem(S.STRATEGIES["TREND_RIDER"], "fn", lambda x: 1 / 0)
    rows = quiet(12) + [(24001.0, 24050.0, 24000.0, 24046.0, 0)]
    out = S.candidates(ctx(FLAT(), day_bars("2026-11-10", rows)), ["TREND_RIDER", "MOMENTUM_SURGE"])
    assert [o["strategy"] for o in out] == ["MOMENTUM_SURGE"]


def test_every_ad_name_is_either_built_or_listed_with_a_reason():
    built = {s["ad"].split(" (")[0] for s in S.STRATEGIES.values() if s["ad"]}
    listed = {n["ad"].split(" (")[0] for n in S.NOT_BUILT}
    assert len(built) == 8 and len(listed) == 4 and not built & listed                  # round 236: IA-3 and IA-5 (expiry day) are now built
    assert {x.split()[0] for x in built | listed} == {f"IA-{i}" for i in range(1, 13)}


# ---- gates -----------------------------------------------------------------------------------------------------------------------
def test_gates_block_the_opposite_side_and_a_second_entry_of_a_named_strategy():
    now, exp = datetime(2026, 11, 10, 10, 30, tzinfo=IST), datetime(2026, 11, 17).date()
    p = Params()
    s = gates.DayState(open_same_side={"NIFTY:CE": 1})
    ok, why = gates.entry_gates(now, exp, 14.0, 1.0, s, p, key="NIFTY:PE")
    assert not ok and any("opposite side" in w for w in why)
    s2 = gates.DayState(entries_by_strategy={"BEAR_SWEEP": 1})
    ok, why = gates.entry_gates(now, exp, 14.0, 1.0, s2, p, key="NIFTY:PE", strategy="BEAR_SWEEP")
    assert not ok and any("BEAR_SWEEP already entered" in w for w in why)
    assert gates.entry_gates(now, exp, 14.0, 1.0, s2, p, key="NIFTY:PE", strategy="CORE")[0]      # CORE has no per-strategy cap


# ---- engine ----------------------------------------------------------------------------------------------------------------------
class SurgeProvider(FakeProvider):
    def __init__(self):
        super().__init__()
        self._rows = day_bars("2026-11-10", [(o + 500, h + 500, l + 500, c + 500, v) for o, h, l, c, v in quiet(14) + [(24001.0, 24050.0, 24000.0, 24046.0, 0)]])

    def daily(self, und):
        return daily("flat", "2026-11-10")

    def intraday(self, und):
        return self._rows


NOW = datetime(2026, 11, 10, 10, 30, tzinfo=IST)


@pytest.fixture
def db():
    s = st.Store(":memory:")
    yield s
    s.close()


def test_a_named_strategy_opens_a_tagged_position_and_is_capped_per_day(db):
    cfg = make_cfg(FNO_UNDERLYINGS="NIFTY", FNO_STRAT_CORE=False, FNO_STRAT_MOMENTUM_SURGE=True)
    e = engine(db, SurgeProvider(), cfg)
    out = e.tick(NOW)
    assert len(out["entries"]) == 1 and out["decisions"]["NIFTY"]["strategies"] == ["MOMENTUM_SURGE"]
    pos = db.get_position(out["entries"][0])
    assert pos.strategy == "MOMENTUM_SURGE" and pos.opt_type == "CE"
    assert any(x["kind"] == "entry" and x.get("strategy") == "MOMENTUM_SURGE" for x in db.events())
    e.squareoff_all(now=NOW + timedelta(seconds=20))
    e.tick(NOW + timedelta(seconds=30))                                                           # the same picture again: capped at one entry a day
    assert len(db.positions("DEMO")) == 1
    assert any("already entered" in " ".join(x.get("why", [])) for x in db.events() if x["kind"] == "skip")
    assert db.day_summary("2026-11-10", "DEMO")["by_strategy"]["MOMENTUM_SURGE"]["entries"] == 1


def test_with_the_named_strategy_off_nothing_changes(db):
    e = engine(db, SurgeProvider(), make_cfg(FNO_UNDERLYINGS="NIFTY", FNO_STRAT_CORE=False))
    assert e.tick(NOW)["entries"] == []


# ---- backtests -------------------------------------------------------------------------------------------------------------------
def walk(days=45, seed=7, shift_from=None):
    rnd = random.Random(seed)
    base = datetime.fromisoformat("2026-08-03").replace(tzinfo=IST)
    intraday, dly, px, d = [], [], 24000.0, 0
    while len(dly) < days:
        day = base + timedelta(days=d)
        d += 1
        if day.weekday() >= 5:
            continue
        t0 = day.replace(hour=9, minute=15).timestamp()
        o_day, hi, lo, trend = px, px, px, rnd.choice([-1, 1]) * 1.2
        for k in range(75):
            o = px
            c = o + trend * rnd.uniform(0, 3) + rnd.gauss(0, 7) * (3 if rnd.random() < 0.08 else 1)
            if shift_from is not None and t0 + 300 * k >= shift_from:
                c = o + rnd.gauss(0, 200)
            h, l = max(o, c) + rnd.uniform(0, 4), min(o, c) - rnd.uniform(0, 4)
            intraday.append({"t": t0 + 300 * k, "o": o, "h": h, "l": l, "c": c, "v": 0})
            hi, lo, px = max(hi, h), min(lo, l), c
        dly.append({"t": t0, "o": o_day, "h": hi, "l": lo, "c": px, "v": 0})
    return dly, intraday


def test_every_strategy_runs_in_the_sim_and_the_portfolio_lists_them_all():
    dly, intra = walk()
    res = portfolio.run("NIFTY", dly, intra, None, Params(), holidays=[])
    assert [r["strategy"] for r in res["by_strategy"]] == list(S.ORDER) and res["kind"] == "SIM" and res["strategy"] == "PORTFOLIO"
    assert all(t["strategy"] in S.ORDER for t in res["trades"])
    assert "NAIVE" in " ".join(res["notes"])
    with pytest.raises(ValueError):
        sim.run("NIFTY", dly, intra, None, Params(), strategy="NOPE")


@pytest.mark.parametrize("key", list(S.ORDER))
def test_sim_of_each_strategy_has_no_lookahead(key):
    dly, intra = walk()
    cut = intra[-75 * 8]["t"]                                                                      # the last 8 days are rewritten
    a = sim.run("NIFTY", dly, intra, None, Params(), strategy=key)
    dly2, intra2 = walk(shift_from=cut)
    b = sim.run("NIFTY", dly2, intra2, None, Params(), strategy=key)
    ta = [(t["entry_ts"], t["opt_type"], t["strike"]) for t in a["trades"] if t["entry_ts"] < cut - 86400]
    tb = [(t["entry_ts"], t["opt_type"], t["strike"]) for t in b["trades"] if t["entry_ts"] < cut - 86400]
    assert ta == tb


def test_the_strategies_are_not_dead_on_a_long_random_walk():
    dly, intra = walk(days=120, seed=11)
    res = portfolio.run("NIFTY", dly, intra, None, Params(), holidays=[])
    fired = {b["strategy"]: b["trades"] for b in res["by_strategy"]}
    assert fired["CORE"] > 0 and sum(1 for k, v in fired.items() if v > 0) >= 4, fired


def test_replay_honours_the_strategy_switches(tmp_path):
    from datetime import date
    from backtest import replay
    from test_backtests import build_world, record_day
    daily_, _ = build_world(n_days=160, n_intraday=0, start_px=24000.0)
    day = date(2026, 11, 10)
    record_day(str(tmp_path), day, [24500 + i * 2 for i in range(60)] + [24620 + (i * 8) for i in range(60)])
    hist = {"NIFTY": [c for c in daily_ if datetime.fromtimestamp(c["t"], IST).date() < day]}
    off = replay.run(str(tmp_path), ["2026-11-10"], hist, {"FNO_SIGNAL_MIN_SCORE": 30.0, "FNO_STRAT_CORE": False}, ["NIFTY"], vix_default=14.0)
    assert off["metrics"]["overall"]["trades"] == 0                                                # CORE off, nothing else on: nothing opens
    on = replay.run(str(tmp_path), ["2026-11-10"], hist, {"FNO_SIGNAL_MIN_SCORE": 30.0, "FNO_STRAT_CORE": False, **{f: True for f in ("FNO_STRAT_TREND_RIDER", "FNO_STRAT_MOMENTUM_SURGE", "FNO_STRAT_DRIFT_EDGE")}},
                     ["NIFTY"], vix_default=14.0)
    assert all(t["strategy"] in ("TREND_RIDER", "MOMENTUM_SURGE", "DRIFT_EDGE") for t in on["trades"])
