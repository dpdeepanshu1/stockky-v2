"""F&O round 236: expiry-day strategies (DEMO), interval-independent strategy windows, chain-veto log, Dhan field names checked against Dhan's published docs, verified charge rates.
Synthetic data only. The Dhan JSON below is the docs' own sample / field list (dhanhq.co/docs/v2/orders, /portfolio, checked 8 Nov 2026), not an answer from a live call."""
from __future__ import annotations

import io
import json
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from backtest import portfolio, sim
from fno import execution, gates, paper, strategies as S
from fno.params import Params
from test_engine_paper import FakeProvider, NOW, engine, make_cfg
from test_strategies import FLAT, SurgeProvider, day_bars, daily, quiet

IST = timezone(timedelta(hours=5, minutes=30))
EXP_DAY = datetime(2026, 11, 10, 10, 30, tzinfo=IST)           # a Tuesday


@pytest.fixture
def db():
    from fno import store as st
    s = st.Store(":memory:")
    yield s
    s.close()


def ctx(d, rows, day="2026-11-10", expiry_day=True, vix=None):
    b = day_bars(day, rows)
    return S.build_ctx("NIFTY", d, b, None, datetime.fromtimestamp(b[-1]["t"] + 300, IST), Params(), (), vix, expiry_day=expiry_day)


SURGE = quiet(14) + [(24001.0, 24050.0, 24000.0, 24046.0, 0)]


# ---- the two expiry-day strategies -------------------------------------------------------------------------------------------------
def test_expiry_momentum_fires_only_on_an_expiry_day():
    on = S.expiry_momentum(ctx(FLAT(), SURGE))
    assert on and on["direction"] == "UP" and "expiry day" in on["reasons"][0]
    assert S.expiry_momentum(ctx(FLAT(), SURGE, expiry_day=False)) is None            # same picture, not an expiry day: nothing
    down = quiet(14) + [(23999.0, 24000.0, 23950.0, 23954.0, 0)]
    assert S.expiry_momentum(ctx(FLAT(), down))["direction"] == "DOWN"
    assert S.expiry_momentum(ctx(FLAT(), quiet(15))) is None                          # an ordinary bar is not an expansion


def test_expiry_momentum_respects_a_strong_daily_trend_against_it():
    down = quiet(14) + [(23999.0, 24000.0, 23950.0, 23954.0, 0)]
    assert S.expiry_momentum(ctx(daily("up", "2026-11-10"), down)) is None            # a PUT against a strong uptrend (daily >= 40)


def _flag_rows(sign):
    """A trend day: 20 bars climbing (or falling) 9 points a bar, 6 bars of a tight pause, then a bar that closes beyond the session extreme."""
    rows, px = [], 24000.0
    for _ in range(20):
        o, c = px, px + sign * 9
        rows.append((o, max(o, c) + 1, min(o, c) - 1, c, 0))
        px = c
    for k in range(6):
        o, c = px, px + (1 if k % 2 else -1)
        rows.append((o, max(o, c) + 1, min(o, c) - 1, c, 0))
        px = c
    top = max(r[1] for r in rows) if sign > 0 else min(r[2] for r in rows)
    rows.append((px, top + sign * 12, px, top + sign * 10, 0))
    return rows


@pytest.mark.parametrize("sign,want", [(1, "UP"), (-1, "DOWN")])
def test_expiry_alpha_trend_day_continuation(sign, want):
    s = S.expiry_alpha(ctx(FLAT(), _flag_rows(sign)))
    assert s and s["direction"] == want
    assert S.expiry_alpha(ctx(FLAT(), _flag_rows(sign), expiry_day=False)) is None
    assert S.expiry_alpha(ctx(FLAT(), quiet(27))) is None                              # no big move first: nothing


def test_expiry_alpha_needs_ninety_minutes_of_bars():
    assert S.expiry_alpha(ctx(FLAT(), _flag_rows(1)[-12:])) is None


def test_registry_keeps_expiry_strategies_apart_from_the_rest():
    cfg = type("C", (), {"FNO_STRAT_CORE": True, "FNO_STRAT_TREND_RIDER": True, "FNO_STRAT_EXPIRY_MOMENTUM": True})()
    assert S.enabled_keys(cfg, expiry=False) == ["CORE", "TREND_RIDER"]
    assert S.enabled_keys(cfg, expiry=True) == ["EXPIRY_MOMENTUM"]
    assert S.enabled_keys(cfg) == ["CORE", "TREND_RIDER", "EXPIRY_MOMENTUM"]
    assert S.is_expiry("EXPIRY_ALPHA") and not S.is_expiry("CORE") and not S.is_expiry("DRIFT_EDGE")


def test_expiry_params_are_half_risk_and_one_lot_other_strategies_unchanged():
    p = Params(risk_per_trade_pct=1.0, max_lots=3)
    e = S.params_for("EXPIRY_ALPHA", p)
    assert e.risk_per_trade_pct == 0.5 and e.max_lots == 1
    assert S.params_for("CORE", p) is p and S.params_for("DRIFT_EDGE", p) is p


# ---- gates -------------------------------------------------------------------------------------------------------------------------
def test_gates_expiry_day_rules():
    p, today = Params(), EXP_DAY.date()
    ok, why = gates.entry_gates(EXP_DAY, today, 14.0, 1.0, gates.DayState(), p, key="NIFTY:CE", strategy="EXPIRY_MOMENTUM", expiry_strategy=True)
    assert ok, why
    assert not gates.entry_gates(EXP_DAY, today, 14.0, 1.0, gates.DayState(), p, key="NIFTY:CE", strategy="CORE")[0]                  # every other strategy: still blocked on expiry day
    ok, why = gates.entry_gates(EXP_DAY, today + timedelta(days=7), 14.0, 1.0, gates.DayState(), p, key="NIFTY:CE", strategy="EXPIRY_ALPHA", expiry_strategy=True)
    assert not ok and any("expires today" in w for w in why)                                                                           # next week's contract is not an expiry-day trade
    late = EXP_DAY.replace(hour=13, minute=45)
    ok, why = gates.entry_gates(late, today, 14.0, 1.0, gates.DayState(), p, key="NIFTY:CE", strategy="EXPIRY_ALPHA", expiry_strategy=True)
    assert not ok and any("expiry-day entry window" in w for w in why)
    ok, why = gates.entry_gates(EXP_DAY, today, 14.0, 1.0, gates.DayState(), p, key="NIFTY:CE", strategy="EXPIRY_ALPHA", expiry_strategy=True, mode="REAL")
    assert not ok and any("DEMO-only" in w for w in why)
    assert gates.entry_gates(EXP_DAY, today, 14.0, 1.0, gates.DayState(), p.with_(expiry_real_ok=True), key="NIFTY:CE", strategy="EXPIRY_ALPHA", expiry_strategy=True, mode="REAL")[0]


def test_squareoff_is_earlier_only_for_a_contract_that_expires_today():
    p = Params()
    t = EXP_DAY.replace(hour=14, minute=45)
    assert gates.after_squareoff(t, p, "2026-11-10") and gates.after_squareoff(t, p, EXP_DAY.date())
    assert not gates.after_squareoff(t, p, "2026-11-17") and not gates.after_squareoff(t, p)
    assert not gates.after_squareoff(EXP_DAY.replace(hour=14, minute=44), p, "2026-11-10")
    assert gates.after_squareoff(EXP_DAY.replace(hour=15, minute=15), p, "2026-11-17")


# ---- engine: a whole expiry-day cycle (DEMO) ---------------------------------------------------------------------------------------
class ExpiryDayProvider(SurgeProvider):
    def __init__(self, expiries=("2026-11-10", "2026-11-17")):
        super().__init__()
        self._exp = list(expiries)

    def expiries(self, u):
        return self._exp

    def chain(self, u, e):
        return FakeProvider.chain(self, u, e)


def _cfg(**kw):
    return make_cfg(FNO_UNDERLYINGS="NIFTY", FNO_STRAT_CORE=False, FNO_STRAT_EXPIRY_MOMENTUM=True, **kw)


def test_expiry_strategy_enters_todays_contract_small_and_is_out_by_1445(db):
    e = engine(db, ExpiryDayProvider(), _cfg())
    out = e.tick(NOW)
    assert len(out["entries"]) == 1 and out["decisions"]["NIFTY"]["strategies"] == ["EXPIRY_MOMENTUM"]
    pos = db.get_position(out["entries"][0])
    assert pos.strategy == "EXPIRY_MOMENTUM" and pos.expiry == "2026-11-10" and pos.lots == 1 and pos.mode == "DEMO"
    e.tick(NOW.replace(hour=14, minute=44))
    assert db.get_position(pos.id).status == paper.OPEN
    out2 = e.tick(NOW.replace(hour=14, minute=45))
    p2 = db.get_position(pos.id)
    assert p2.status == paper.CLOSED and p2.exit_reason == paper.SQUAREOFF and pos.id in out2["exits"]


def test_expiry_strategy_does_nothing_when_today_is_not_an_expiry_day(db):
    e = engine(db, ExpiryDayProvider(("2026-11-17", "2026-11-24")), _cfg())
    assert e.tick(NOW)["entries"] == []


def test_expiry_strategy_is_demo_only_in_real_mode(db):
    e = engine(db, ExpiryDayProvider(), _cfg(FNO_MODE="REAL", FNO_REAL_ENABLED=True))
    out = e.tick(NOW)
    assert out["entries"] == []
    assert any(x["kind"] == "skip" and "DEMO-only" in " ".join(x.get("why", [])) for x in db.events())


def test_a_normal_strategy_still_never_trades_the_expiry_day_contract(db):
    cfg = make_cfg(FNO_UNDERLYINGS="NIFTY", FNO_STRAT_CORE=False, FNO_STRAT_MOMENTUM_SURGE=True)
    out = engine(db, ExpiryDayProvider(), cfg).tick(NOW)
    pos = db.get_position(out["entries"][0])
    assert pos.strategy == "MOMENTUM_SURGE" and pos.expiry == "2026-11-17"             # next week's contract, as before round 236


# ---- the chain veto is recorded ----------------------------------------------------------------------------------------------------
def test_a_chain_veto_is_counted_not_silent():
    x = ctx(FLAT(), SURGE, expiry_day=False)
    x.__dict__["chain_parts"] = (-30.0, ["heavy call writing"])            # the chain leans 30 against a CALL
    assert S.candidates(x, ["MOMENTUM_SURGE"]) == []
    assert x.vetoes and x.vetoes[0]["strategy"] == "MOMENTUM_SURGE" and x.vetoes[0]["direction"] == "UP" and x.vetoes[0]["chain"] == -30.0


def test_the_engine_writes_a_veto_event(db, monkeypatch):
    monkeypatch.setattr("fno.analytics.chain_score", lambda summ: (-30.0, ["heavy call writing"]))
    cfg = make_cfg(FNO_UNDERLYINGS="NIFTY", FNO_STRAT_CORE=False, FNO_STRAT_MOMENTUM_SURGE=True)
    out = engine(db, SurgeProvider(), cfg).tick(NOW)
    assert out["entries"] == []
    assert any(x["kind"] == "veto" and x.get("strategy") == "MOMENTUM_SURGE" for x in db.events())


# ---- interval independence ---------------------------------------------------------------------------------------------------------
def _bars(minutes, rows, day="2026-11-16"):
    t0 = datetime.fromisoformat(day + "T09:15:00").replace(tzinfo=IST).timestamp()
    return [{"t": t0 + 60 * minutes * k, "o": o, "h": h, "l": l, "c": c, "v": v} for k, (o, h, l, c, v) in enumerate(rows)]


def test_windows_are_minutes_not_bar_counts():
    b1 = _bars(1, [(24000, 24010, 23990, 24002, 0)] * 30)
    x1 = S.build_ctx("NIFTY", FLAT(), b1, None, datetime.fromtimestamp(b1[-1]["t"] + 60, IST), Params())
    assert x1.interval == 60 and x1.nb(15) == 15 and x1.nb(30) == 30
    b5 = _bars(5, [(24000, 24010, 23990, 24002, 0)] * 8)
    x5 = S.build_ctx("NIFTY", FLAT(), b5, None, datetime.fromtimestamp(b5[-1]["t"] + 300, IST), Params())
    assert x5.nb(15) == 3 and x5.nb(30) == 6 and x5.nb(60) == 12


def test_weekly_opener_gives_the_same_answer_on_one_minute_bars():
    rows = [(24000, 24010, 23990, 24002, 0)] * 30 + [(24002, 24030, 24000, 24025, 0)]           # 30 minutes of range, then the break
    b = _bars(1, rows)
    x = S.build_ctx("NIFTY", daily("up", "2026-11-16"), b, None, datetime.fromtimestamp(b[-1]["t"] + 60, IST), Params())     # Monday 16 Nov 2026
    assert S.weekly_opener(x)["direction"] == "UP"
    early = _bars(1, rows[:20])                                                                  # only 20 minutes: not enough to define the range
    assert S.weekly_opener(S.build_ctx("NIFTY", daily("up", "2026-11-16"), early, None, datetime.fromtimestamp(early[-1]["t"] + 60, IST), Params())) is None


# ---- SIM ---------------------------------------------------------------------------------------------------------------------------
def _sim_day(day):
    rows = [(o + 500, h + 500, l + 500, c + 500, v) for o, h, l, c, v in quiet(14)] + [(24501.0, 24550.0, 24500.0, 24546.0, 0)] + [(24546 + 3 * k, 24552 + 3 * k, 24544 + 3 * k, 24549 + 3 * k, 0) for k in range(8)]
    return day_bars(day, rows)


def test_sim_runs_an_expiry_strategy_only_on_expiry_days_and_small():
    dly = daily("flat", "2026-11-10")
    tue = sim.run("NIFTY", dly, _sim_day("2026-11-10"), None, Params(), strategy="EXPIRY_MOMENTUM")          # Tuesday = Nifty weekly expiry
    assert tue["days"] == 1 and len(tue["trades"]) == 1
    t = tue["trades"][0]
    assert t["strategy"] == "EXPIRY_MOMENTUM" and t["expiry"] == "2026-11-10" and t["lots"] == 1
    wed = sim.run("NIFTY", dly, _sim_day("2026-11-11"), None, Params(), strategy="EXPIRY_MOMENTUM")          # Wednesday: nothing to do
    assert wed["days"] == 0 and wed["trades"] == []


def test_sim_portfolio_lists_every_strategy_with_its_day_count():
    dly = daily("flat", "2026-11-10")
    res = portfolio.run("NIFTY", dly, _sim_day("2026-11-10"), None, Params(), holidays=[])
    rows = {r["strategy"]: r for r in res["by_strategy"]}
    assert set(rows) == set(S.ORDER) and rows["EXPIRY_MOMENTUM"]["days"] == 1 and rows["CORE"]["days"] == 1


# ---- Dhan: field names from the published docs ----------------------------------------------------------------------------------------
def test_order_status_parses_the_docs_sample_and_a_part_traded_order():
    pending = execution.parse_order_status({"orderId": "112111182198", "orderStatus": "PENDING", "quantity": 5, "remainingQuantity": 5, "averageTradedPrice": 0, "filledQty": 0})
    assert (pending.state, pending.filled_qty, pending.avg_price) == ("PENDING", 0, 0.0)
    part = execution.parse_order_status({"orderStatus": "PART_TRADED", "quantity": 130, "filledQty": 65, "remainingQuantity": 65, "averageTradedPrice": 101.55})
    assert (part.state, part.filled_qty, part.avg_price) == ("PENDING", 65, 101.55)            # still working, 65 filled: the engine's partial-fill path reads filled_qty
    done = execution.parse_order_status({"orderStatus": "TRADED", "quantity": 65, "filledQty": 65, "averageTradedPrice": 99.9})
    assert (done.state, done.filled_qty) == ("TRADED", 65)
    assert execution.parse_order_status({"orderStatus": "TRANSIT"}).state == "PENDING"
    assert execution.parse_order_status({"orderStatus": "EXPIRED"}).state == "CANCELLED"


class _Resp:
    def __init__(self, body):
        self._b = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._b


# the docs' GET /positions sample, plus an F&O long, a short and a closed row (field names exactly as the docs list them)
POSITIONS = [
    {"dhanClientId": "1000000009", "tradingSymbol": "TCS", "securityId": "11536", "positionType": "LONG", "exchangeSegment": "NSE_EQ", "productType": "CNC", "buyAvg": 3345.8, "buyQty": 40,
     "costPrice": 3215.0, "sellAvg": 0.0, "sellQty": 0, "netQty": 40, "realizedProfit": 0.0, "unrealizedProfit": 6122.0, "drvExpiryDate": "0001-01-01", "drvOptionType": None, "drvStrikePrice": 0.0},
    {"securityId": "55001", "positionType": "LONG", "exchangeSegment": "NSE_FNO", "productType": "INTRADAY", "buyQty": 65, "sellQty": 0, "netQty": 65, "drvExpiryDate": "2026-11-17", "drvOptionType": "CALL", "drvStrikePrice": 24500.0},
    {"securityId": "55002", "positionType": "SHORT", "exchangeSegment": "BSE_FNO", "productType": "INTRADAY", "buyQty": 0, "sellQty": 20, "netQty": -20, "drvOptionType": "PUT"},
    {"securityId": "55003", "positionType": "CLOSED", "exchangeSegment": "NSE_FNO", "productType": "INTRADAY", "buyQty": 65, "sellQty": 65, "netQty": 0},
]


def test_net_positions_reads_only_open_fno_rows_with_a_sign():
    calls = []

    def opener(req, timeout=0):
        calls.append((req.get_method(), req.full_url))
        return _Resp(POSITIONS)

    b = execution.DhanBroker("https://api.dhan.co/v2", "1000000009", "tok", opener=opener)
    assert b.net_positions() == {"55001": 65, "55002": -20}                                    # equity ignored, closed (0) ignored, a short stays negative
    assert calls == [("GET", "https://api.dhan.co/v2/positions")]


def test_cancel_reads_the_docs_response():
    b = execution.DhanBroker("https://api.dhan.co/v2", "c", "t", opener=lambda req, timeout=0: _Resp({"orderId": "112111182045", "orderStatus": "CANCELLED"}))
    r = b.cancel("112111182045")
    assert r.ok and r.status == "CANCELLED"


def test_the_stop_loss_order_has_the_docs_fields_and_a_limit_below_the_trigger():
    o = execution.build_sl_order("1000000003", 55001, "NSE_FNO", 65, 80.0)
    assert o["orderType"] == "STOP_LOSS" and o["triggerPrice"] == 80.0 and o["transactionType"] == "SELL" and o["productType"] == "INTRADAY" and o["validity"] == "DAY"
    assert o["price"] < o["triggerPrice"] and len(o["correlationId"]) <= 30 and o["exchangeSegment"] == "NSE_FNO"


def test_a_dhan_error_keeps_its_code_and_explains_a_static_ip_problem():
    body = {"errorType": "Invalid_IP", "errorCode": "DH-905", "errorMessage": "Invalid IP"}
    assert execution.describe_error(body).startswith("DH-905:") and "whitelist" in execution.describe_error(body)
    assert execution.describe_error({"remarks": "weird"}) == "weird" and execution.describe_error(None) == "rejected"

    def opener(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(json.dumps(body).encode()))

    res = execution.DhanBroker("https://api.dhan.co/v2", "c", "t", opener=opener).place({"x": 1})
    assert not res.ok and res.status == "HTTP 401" and "DH-905" in res.message


# ---- charges: the rates confirmed in round 236 ------------------------------------------------------------------------------------------
def test_verified_charge_defaults():
    p = Params()
    assert p.stt_sell_pct == 0.15 and p.exch_txn_pct_nse == 0.03553 and p.exch_txn_pct_bse == 0.0325 and p.stamp_buy_pct == 0.003 and p.brokerage_per_order == 20.0


# ---- Dhan expired-options (rolling) data: request builder and parser, from the docs' own sample --------------------------------------------
def test_rolling_request_matches_the_docs_shape():
    from datetime import date as D
    from backtest import rolling
    r = rolling.build_request("NIFTY", "CALL", "ATM", D(2026, 10, 1), D(2026, 10, 31), 5, "WEEK", 1)
    assert r["exchangeSegment"] == "NSE_FNO" and r["securityId"] == 13 and r["instrument"] == "OPTIDX" and r["drvOptionType"] == "CALL" and r["toDate"] == "2026-10-31"
    assert set(r["requiredData"]) == {"open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot"}
    for bad in (dict(start=D(2026, 10, 1), end=D(2026, 11, 5)), dict(start=D(2026, 10, 5), end=D(2026, 10, 5)), dict(start=D(2026, 10, 1), end=D(2026, 10, 3), code=0), dict(start=D(2026, 10, 1), end=D(2026, 10, 3), iv=7)):
        with pytest.raises(ValueError):
            rolling.build_request("NIFTY", "CALL", "ATM", bad["start"], bad["end"], bad.get("iv", 5), "WEEK", bad.get("code", 1))
    assert rolling.windows(D(2026, 5, 1), D(2026, 7, 5)) == [(D(2026, 5, 1), D(2026, 5, 31)), (D(2026, 5, 31), D(2026, 6, 30)), (D(2026, 6, 30), D(2026, 7, 5))]


def test_rolling_response_parses_parallel_arrays_and_a_null_side():
    from backtest import rolling
    body = {"data": {"ce": {"iv": [14.1, 14.3], "oi": [1000, 1100], "strike": [24500, 24500], "spot": [24510.5, 24520.0], "open": [354, 360.3], "high": [361, 365], "low": [350, 359], "close": [360, 364],
                            "volume": [900, 1200], "timestamp": [1756698300, 1756698000]}, "pe": None}}
    rows = rolling.parse_response(body, "CALL")
    assert [r["t"] for r in rows] == [1756698000.0, 1756698300.0] and rows[1]["c"] == 360.0 and rows[0]["spot"] == 24520.0     # sorted by time, columns stay aligned
    assert rolling.parse_response(body, "PUT") == [] and rolling.parse_response(None, "CALL") == [] and rolling.parse_response({"data": {"ce": {"timestamp": [1], "close": [None]}}}, "CALL") == []


# ---- round 237: strategy thresholds are Params (env-tunable), defaults unchanged ---------------------------------------------------------
def test_threshold_defaults_are_the_old_constants():
    p = Params()
    assert (p.strat_surge_range_mult, p.strat_expiry_range_mult, p.strat_drift_closes, p.strat_drift_slope_pct, p.strat_pivot_break_pct) == (1.5, 1.3, 10, 0.04, 0.03)


def test_a_stricter_surge_threshold_removes_the_signal_and_config_reaches_params():
    x = lambda p: S.build_ctx("SENSEX", FLAT(), day_bars("2026-11-10", SURGE), None, datetime(2026, 11, 10, 10, 30, tzinfo=IST), p)
    assert S.momentum_surge(x(Params())) is not None
    assert S.momentum_surge(x(Params(strat_surge_range_mult=9.0))) is None
    cfg = type("C", (), {"FNO_STRAT_SURGE_RANGE_MULT": 2.2, "FNO_STRAT_DRIFT_CLOSES": 11})()
    got = Params.from_config(cfg)
    assert got.strat_surge_range_mult == 2.2 and got.strat_drift_closes == 11 and got.strat_pivot_break_pct == 0.03   # unset ones keep the default


def test_live_params_read_the_new_config_values():
    import config
    from fno import liveparams
    lp = liveparams.current()
    assert (lp.strat_surge_range_mult, lp.strat_drift_closes, lp.strat_expiry_range_mult) == (config.FNO_STRAT_SURGE_RANGE_MULT, config.FNO_STRAT_DRIFT_CLOSES, config.FNO_STRAT_EXPIRY_RANGE_MULT)


# ---- round 237: a TRADED order with no average price must still count as filled -----------------------------------------------------------
def test_a_traded_order_without_a_price_or_quantity_is_still_a_fill():
    s = execution.parse_order_status({"orderStatus": "TRADED", "quantity": 130, "price": 71.5, "averageTradedPrice": 0, "filledQty": 0})
    assert (s.state, s.filled_qty, s.avg_price) == ("TRADED", 130, 71.5) and s.raw["_avg_estimated"] and s.raw["_filled_estimated"]
    s2 = execution.parse_order_status({"orderStatus": "TRADED", "quantity": 65, "filledQty": 65, "averageTradedPrice": 70.1, "price": 71.5})
    assert s2.avg_price == 70.1 and "_avg_estimated" not in s2.raw                       # a real average is never replaced
    assert execution.parse_order_status({"orderStatus": "PENDING", "quantity": 65, "price": 71.5}).avg_price == 0.0   # only TRADED is estimated
    assert execution.parse_order_status({"orderStatus": "TRADED"}).avg_price == 0.0       # nothing to estimate from: stays "no price" (callers keep waiting and shout)


def test_a_stop_that_traded_with_no_average_closes_the_position_instead_of_leaving_it_open(db):
    from test_real_partial import RB, entered, real
    b = RB()
    e = real(db, b)
    pos = entered(db, b, e)
    sl = pos.broker_order_ids["sl"]
    b.states[sl] = execution.parse_order_status({"orderStatus": "TRADED", "quantity": pos.qty, "price": pos.sl_price * 0.95, "averageTradedPrice": 0, "filledQty": 0})
    closed = e.check_broker_stops(NOW, Params())
    p2 = db.get_position(pos.id)
    assert [c.id for c in closed] == [pos.id] and p2.status == paper.CLOSED and p2.exit_reason == paper.SL and "estimated" in p2.note


def test_every_new_threshold_defaults_to_the_old_constant_and_the_veto_one_is_live():
    p = Params()
    assert (p.strat_trend_d, p.strat_trend_i, p.strat_chain_veto, p.strat_sweep_max_d, p.strat_pivot_max_d, p.strat_opener_d) == (40.0, 15.0, 15.0, 50.0, 15.0, 15.0)
    assert (p.strat_surge_body, p.strat_surge_close, p.strat_surge_vol_mult, p.strat_expiry_body, p.strat_alpha_move, p.strat_alpha_pause, p.strat_drift_touch_pct) == (0.6, 0.7, 1.3, 0.5, 0.5, 0.4, 0.08)
    mk = lambda pp: S.build_ctx("SENSEX", FLAT(), day_bars("2026-11-10", SURGE), None, datetime(2026, 11, 10, 10, 30, tzinfo=IST), pp)
    x = mk(Params())
    x.__dict__["chain_parts"] = (-30.0, ["heavy call writing"])
    assert S.momentum_surge(x) is None
    y = mk(Params(strat_chain_veto=50.0))
    y.__dict__["chain_parts"] = (-30.0, ["heavy call writing"])
    assert S.momentum_surge(y)["direction"] == "UP"                               # -30 is no longer enough to veto
    assert S.momentum_surge(mk(Params(strat_surge_body=0.99))) is None            # a stricter body test removes the same bar
    assert S.expiry_alpha(ctx(FLAT(), _flag_rows(1))) is not None


# ---- round 237 audit: a REAL square-off with no quote must not rest a SELL at the entry price ----------------------------------------------
def test_real_squareoff_without_a_quote_sells_below_the_lowest_premium_seen(db):
    from test_real_partial import RB, entered, real, sells
    b = RB()
    e = real(db, b)
    pos = entered(db, b, e)
    pos.min_premium = 40.0
    db.update_position(pos)
    e.provider.chain = lambda u, x: None                                    # the feed has nothing at 15:16
    closed = e.manage_open(NOW.replace(hour=15, minute=16), Params(), {}, {})
    assert sells(b) and sells(b)[-1]["price"] <= 40.0 * 0.85 + 0.05 and sells(b)[-1]["price"] < pos.entry_premium
    assert db.get_position(pos.id).status == paper.CLOSED and [c.id for c in closed] == [pos.id]


def test_a_blocked_real_exit_is_not_reported_as_closed(db):
    from test_real_partial import RB, entered, real
    b = RB()
    e = real(db, b)
    pos = entered(db, b, e)
    db.kv_set("fno_armed_day", "")                                          # disarm: the exit is refused, the position stays open
    e.provider.chain = lambda u, x: None
    closed = e.manage_open(NOW.replace(hour=15, minute=16), Params(), {}, {})
    assert closed == [] and db.get_position(pos.id).status == paper.OPEN


# ---- round 238: charges pinned to a hand calculation ----------------------------------------------------------------------------------------
def test_round_trip_charges_match_the_hand_calculation():
    from fno import costs
    c = costs.round_trip("NIFTY", 100.0, 120.0, 65, Params())              # buy 6,500, sell 7,800
    assert c.brokerage == 40.0                                              # Dhan: flat Rs 20 per executed order on options, two orders
    assert c.stt == pytest.approx(11.70, abs=0.005)                         # 0.15 % of the sell premium only
    assert c.exchange == pytest.approx(5.0808, abs=0.0005)                  # 0.03553 % of both legs
    assert c.stamp == pytest.approx(0.195, abs=0.0005)                      # 0.003 % of the buy only
    assert c.gst == pytest.approx((40.0 + c.exchange + c.sebi) * 0.18, abs=0.0005)
    assert c.total == pytest.approx(65.11, abs=0.01)
    s = costs.round_trip("SENSEX", 100.0, 120.0, 20, Params())              # BSE rate
    assert s.exchange == pytest.approx((2000 + 2400) * 0.000325, abs=0.0005) and s.brokerage == 40.0


# ---- round 238: the recorder also saves the contract that expires today ---------------------------------------------------------------------
def test_recorder_saves_todays_expiring_contract_too_so_expiry_strategies_can_be_replayed(db, tmp_path):
    from fno import recorder
    from fno.runtime import Runner
    e = engine(db, ExpiryDayProvider(), _cfg())
    r = Runner(e, e.cfg, str(tmp_path))
    r.snapshot(NOW)
    rows = list(recorder.read_day(str(tmp_path), "2026-11-10", "NIFTY"))
    assert sorted(x["chain"]["expiry"] for x in rows) == ["2026-11-10", "2026-11-17"] and r.snapshots_today == 2
    # an ordinary day (no contract expires today): one chain, as before
    e2 = engine(db, ExpiryDayProvider(("2026-11-17", "2026-11-24")), _cfg())
    r2 = Runner(e2, e2.cfg, str(tmp_path / "b"))
    r2.snapshot(NOW)
    assert r2.snapshots_today == 1


# ---- round 239: the Charges page and the stored charges of a partial-exit row use one calculation ---------------------------------------------
def test_a_partial_exit_row_pays_no_second_entry_brokerage_on_the_page_either():
    from fno import costs
    from fno.signals import ContractRef
    p = Params()
    pos = paper.open_position(ContractRef("NIFTY", "2026-11-17", 24500, "CE", 65, 55001, "NSE_FNO", 0.05), 2, 100.0, 1.0, p, mode="DEMO", score=50.0, spot=24500.0)
    child = paper.split_closed(pos, 1, 120.0, 2.0, "TARGET", p)
    page = costs.round_trip("NIFTY", child.entry_premium, child.exit_premium, child.qty, p, child=True)
    assert child.charges == round(page.total, 2)
    full = costs.round_trip("NIFTY", child.entry_premium, child.exit_premium, child.qty, p)
    assert full.total - page.total == pytest.approx(20.0 * 1.18, abs=0.01)                    # exactly the entry brokerage and its GST
    assert page.brokerage == 20.0                                                               # only the exit order's Rs 20 is on this row
