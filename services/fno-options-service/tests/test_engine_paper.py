"""F&O rounds 5 and 8: the paper position lifecycle, the store, the engine cycle (DEMO) and the REAL locks. A fake provider and a fake broker; no network."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import config as real_config
from fno import analytics, engine as eng, execution, paper, recorder, store as st
from fno.models import ContractRef
from fno.params import Params
from fno.provider import IST, Provider, convert_candles, label_to_epoch
from helpers import make_chain, make_daily, make_intraday

NOW = datetime(2026, 11, 10, 10, 30, tzinfo=IST)           # a Tuesday, inside the entry window


def make_cfg(**kw):
    ns = SimpleNamespace(**{k: getattr(real_config, k) for k in dir(real_config) if k.isupper()})
    ns.FNO_ENGINE_ENABLED, ns.FNO_SIGNAL_MIN_SCORE = True, 30.0
    ns.FNO_REAL_REQUIRES_READINESS = False      # round 262: these tests are about the engine's REAL path, not the readiness gate (tests/test_round262_readiness.py turns it on)
    # round 242: REAL sizing comes from the capital board, never from FNO_DEMO_CAPITAL. Every test config gets a fresh board giving F&O Rs 5,00,000 (what REAL used to size from).
    import tempfile
    from time import time as _now
    import capital_board
    ns.CAPITAL_ALLOC_MODE, ns.CAPITAL_ALLOC_DIR = "shadow", tempfile.mkdtemp(prefix="fno_board_")
    capital_board.write_json(ns.CAPITAL_ALLOC_DIR, capital_board.ALLOC_FILE, {"ts": _now(), "mode": "shadow", "pcts": {"fno": 25.0}, "amounts": {"fno": 500000.0}})
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


class FakeProvider(Provider):
    def __init__(self, spot=24500.0, drift=0.002, step=6.0, vix=14.0):
        self.spot, self.drift, self.step, self.vixv = spot, drift, step, vix
        self.now_ts = NOW.timestamp()

    def expiries(self, u): return ["2026-11-17", "2026-11-24"]
    def chain(self, u, e):
        c = make_chain(spot=self.spot, expiry=e, underlying=u, step=100.0 if u == "SENSEX" else 50.0, now=NOW.replace(tzinfo=None), lot=20 if u == "SENSEX" else 65, fetched_at=self.now_ts, n_each=24)
        return c
    def daily(self, u): return make_daily(drift=self.drift)
    def intraday(self, u): return make_intraday(step=self.step)
    def vix(self): return self.vixv
    def lookup(self, u, e, k, o): return None


class FakeBroker(execution.Broker):
    def __init__(self, ok=True):
        self.sent, self.ok = [], ok
    def place(self, payload):
        self.sent.append(payload)
        return execution.OrderResult(self.ok, order_id="OID%d" % len(self.sent) if self.ok else "", status="TRANSIT" if self.ok else "HTTP 400", message="" if self.ok else "rejected")


@pytest.fixture
def db():
    s = st.Store(":memory:")
    yield s
    s.close()


def engine(db, prov=None, cfg=None, broker=None):
    return eng.Engine(db, prov or FakeProvider(), cfg or make_cfg(FNO_UNDERLYINGS="NIFTY"), broker=broker)


# ---- paper lifecycle ---------------------------------------------------------------------------------------------------------
C = ContractRef("NIFTY", "2026-11-17", 24500.0, "CE", 65, 111, "NSE_FNO", 0.05)
P = Params()


def pos(fill=100.0):
    return paper.open_position(C, 2, fill, NOW.timestamp(), P)


def test_open_sets_stop_and_target_on_the_tick():
    p = pos(100.0)
    assert p.sl_price == 75.0 and p.target_price == 150.0 and p.qty == 130


def test_fills_include_slippage_and_never_flatter():
    assert paper.buy_fill(100.0, 99.0, P) >= 100.0
    assert paper.sell_fill(100.0, 101.0, P) <= 100.0
    assert paper.buy_fill(None, None, P) is None


def test_tick_exit_order_sl_target_trail_squareoff():
    p = pos(100.0)
    assert paper.evaluate_tick(p, 110.0, P) is None
    assert paper.evaluate_tick(p, 74.0, P)[0] == paper.SL
    p2 = pos(100.0)
    assert paper.evaluate_tick(p2, 151.0, P)[0] == paper.TARGET
    p3 = pos(100.0)
    assert paper.evaluate_tick(p3, 135.0, P) is None and p3.trail_armed          # armed at +30 %
    assert paper.evaluate_tick(p3, 107.0, P)[0] == paper.TRAIL                    # 135 x 0.8 = 108
    assert paper.evaluate_tick(pos(100.0), 100.0, P, squareoff=True)[0] == paper.SQUAREOFF


def test_trail_stop_only_moves_up():
    p = pos(100.0)
    paper.evaluate_tick(p, 140.0, P); t1 = p.trail_stop
    paper.evaluate_tick(p, 130.0, P)
    assert p.trail_stop == t1
    paper.evaluate_tick(p, 145.0, P)
    assert p.trail_stop > t1


def test_bar_is_pessimistic_and_gap_fills_at_open():
    p = pos(100.0)
    assert paper.evaluate_bar(p, 100, 160, 70, 120, P) == (paper.SL, 75.0)        # touches both: the stop is assumed first
    p2 = pos(100.0)
    assert paper.evaluate_bar(p2, 60, 70, 55, 65, P) == (paper.SL, 60)            # gapped through the stop: filled at the open
    p3 = pos(100.0)
    assert paper.evaluate_bar(p3, 100, 155, 98, 150, P) == (paper.TARGET, 150.0)


def test_close_computes_gross_charges_net():
    p = paper.close_position(pos(100.0), 120.0, NOW.timestamp() + 600, paper.TARGET, P)
    assert p.gross_pnl == 2600.0 and p.charges > 40 and p.net_pnl == pytest.approx(p.gross_pnl - p.charges)
    assert p.status == paper.CLOSED and paper.unrealized(p, 130) is None


def test_position_json_round_trip():
    p = pos()
    assert paper.Position.from_json(p.to_json()).to_json() == p.to_json()


# ---- store -------------------------------------------------------------------------------------------------------------------
def test_store_positions_events_kv_summary(db):
    p = pos(); db.insert_position(p)
    assert p.id and db.open_positions()[0].id == p.id
    paper.close_position(p, 70.0, NOW.timestamp() + 60, paper.SL, P); db.update_position(p)
    s = db.day_summary("2026-11-10", "DEMO")
    assert s["trades"] == 1 and s["open"] == 0 and s["losses"] == 1 and s["realized_net"] < 0 and s["last_stopout_ts"]
    db.add_event("signal", {"x": 1}, underlying="NIFTY"); assert db.events("signal")[0]["x"] == 1
    db.kv_set("a", {"b": 2}); assert db.kv_get("a") == {"b": 2} and db.kv_get("missing", 7) == 7


def test_store_day_is_ist_not_utc(db):
    late = datetime(2026, 11, 10, 23, 30, tzinfo=timezone.utc).timestamp()      # 05:00 IST on the 11th
    assert st.day_of(late) == "2026-11-11"


# ---- engine: DEMO ------------------------------------------------------------------------------------------------------------
def test_engine_off_does_nothing(db):
    e = engine(db, cfg=make_cfg(FNO_UNDERLYINGS="NIFTY", FNO_ENGINE_ENABLED=False))
    r = e.tick(NOW)
    assert r["entries"] == [] and db.open_positions() == []


def test_engine_enters_a_call_in_an_uptrend(db):
    e = engine(db)
    r = e.tick(NOW)
    assert len(r["entries"]) == 1, r
    p = db.open_positions()[0]
    assert p.opt_type == "CE" and p.mode == "DEMO" and p.lots >= 1 and p.entry_premium > 0
    assert any(ev["kind"] == "entry" for ev in db.events())


def test_engine_enters_a_put_in_a_downtrend(db):
    e = engine(db, prov=FakeProvider(drift=-0.002, step=-6.0))
    e.provider.daily = lambda u: make_daily(drift=-0.002, start=26000)
    e.tick(NOW)
    assert db.open_positions() and db.open_positions()[0].opt_type == "PE"


def test_no_second_entry_on_the_same_side_and_max_open_respected(db):
    e = engine(db)
    e.tick(NOW); e.tick(NOW + timedelta(seconds=10))
    assert len(db.open_positions()) == 1


def test_gates_block_entry_on_high_vix(db):
    e = engine(db, prov=FakeProvider(vix=40.0))
    r = e.tick(NOW)
    assert r["entries"] == [] and any(ev["kind"] == "skip" for ev in db.events())


def test_target_exit_books_a_net_profit(db):
    e = engine(db)
    e.tick(NOW)
    p = db.open_positions()[0]
    e.provider.spot = 24500 + 400                                   # a big move up: the call premium jumps
    e.tick(NOW + timedelta(minutes=20))
    q = db.get_position(p.id)
    assert q.status == paper.CLOSED and q.exit_reason in (paper.TARGET, paper.TRAIL) and q.gross_pnl > 0


def test_stop_loss_exit_books_a_loss(db):
    e = engine(db)
    e.tick(NOW)
    p = db.open_positions()[0]
    e.provider.spot = 24500 - 500
    e.provider.drift, e.provider.step = 0.0, 0.0
    e.tick(NOW + timedelta(minutes=20))
    q = db.get_position(p.id)
    assert q.status == paper.CLOSED and q.exit_reason in (paper.SL, paper.SIGNAL_FLIP) and q.net_pnl < 0


def test_squareoff_closes_everything_at_the_time(db):
    e = engine(db)
    e.tick(NOW)
    e.tick(NOW.replace(hour=15, minute=16))
    assert db.open_positions() == []
    assert db.positions()[0].exit_reason == paper.SQUAREOFF


def test_squareoff_all_manual(db):
    e = engine(db); e.tick(NOW)
    assert len(e.squareoff_all(now=NOW + timedelta(minutes=5))) == 1 and db.open_positions() == []


def test_weekend_and_holiday_do_nothing(db):
    e = engine(db)
    assert e.tick(datetime(2026, 11, 14, 10, 30, tzinfo=IST))["entries"] == []
    e2 = eng.Engine(st.Store(":memory:"), FakeProvider(), make_cfg(FNO_UNDERLYINGS="NIFTY"), holidays=("2026-11-10",))
    assert e2.tick(NOW)["entries"] == []


def test_expiry_pick_skips_expiry_day(db):
    e = engine(db)
    assert e.pick_expiry("NIFTY", datetime(2026, 11, 17).date(), Params()) == "2026-11-24"
    assert e.pick_expiry("NIFTY", datetime(2026, 11, 10).date(), Params()) == "2026-11-17"


def test_tick_never_raises(db):
    class Boom(FakeProvider):
        def chain(self, u, e): raise RuntimeError("upstream down")
    e = engine(db, prov=Boom())
    r = e.tick(NOW)
    assert "error" in r and e.last_tick["ok"] is False


# ---- engine: REAL locks ------------------------------------------------------------------------------------------------------
def test_real_mode_is_blocked_without_the_locks_and_sends_nothing(db):
    b = FakeBroker()
    e = engine(db, cfg=make_cfg(FNO_UNDERLYINGS="NIFTY", FNO_MODE="REAL"), broker=b)
    e.tick(NOW)
    assert b.sent == [] and db.open_positions() == []
    ev = [x for x in db.events() if x["kind"] == "real_blocked"]
    assert ev and any("FNO_REAL_ENABLED" in l for l in ev[0]["locks"])


def test_real_orders_go_out_only_with_every_lock_open(db):
    b = FakeBroker()
    cfg = make_cfg(FNO_UNDERLYINGS="NIFTY", FNO_MODE="REAL", FNO_REAL_ENABLED=True, DHAN_CLIENT_ID="1000", DHAN_ACCESS_TOKEN="tok")
    e = engine(db, cfg=cfg, broker=b)
    db.kv_set("fno_armed_day", "2026-11-09")                        # armed YESTERDAY: not valid today
    e.tick(NOW)
    assert b.sent == []
    db.kv_set("fno_armed_day", "2026-11-10")
    e.tick(NOW + timedelta(seconds=5))
    assert len(b.sent) == 2                                          # the BUY and, right after it, the resting stop-loss
    assert b.sent[1]["orderType"] == "STOP_LOSS" and b.sent[1]["transactionType"] == "SELL" and b.sent[1]["triggerPrice"] > 0
    o = b.sent[0]
    assert o["transactionType"] == "BUY" and o["productType"] == "INTRADAY" and o["orderType"] == "LIMIT" and o["exchangeSegment"] == "NSE_FNO"
    assert o["quantity"] % 65 == 0 and o["price"] > 0
    p = db.open_positions()[0]
    assert p.mode == "REAL" and p.broker_order_ids["entry"] == "OID1" and "unconfirmed" in p.note


def test_failed_real_order_opens_no_position(db):
    b = FakeBroker(ok=False)
    cfg = make_cfg(FNO_UNDERLYINGS="NIFTY", FNO_MODE="REAL", FNO_REAL_ENABLED=True, DHAN_CLIENT_ID="1000", DHAN_ACCESS_TOKEN="tok")
    e = engine(db, cfg=cfg, broker=b)
    db.kv_set("fno_armed_day", "2026-11-10")
    e.tick(NOW)
    assert db.open_positions() == [] and any(x["kind"] == "order_failed" for x in db.events())


def test_build_order_validates():
    with pytest.raises(ValueError):
        execution.build_order("1", 5, "NSE_EQ", "BUY", 65, 100)
    with pytest.raises(ValueError):
        execution.build_order("1", 5, "NSE_FNO", "BUY", 0, 100)
    assert execution.build_order("1", 5, "BSE_FNO", "SELL", 20, 101.234)["price"] == 101.23


def test_dhan_broker_maps_http_errors_without_raising():
    import io, urllib.error
    def opener(req, timeout=0):
        raise urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(b'{"errorMessage":"Invalid IP"}'))
    r = execution.DhanBroker("https://x/v2", "1", "t", opener=opener).place({"a": 1})
    assert not r.ok and r.status == "HTTP 400" and "Invalid IP" in r.message


# ---- provider conversions and recorder --------------------------------------------------------------------------------------
def test_candle_conversion():
    raw = [{"date": "2026-11-10 09:15", "open": 1, "high": 2, "low": 1, "close": 2, "volume": 5}, {"date": "bad", "open": 1, "high": 2, "low": 1, "close": 2}, {"date": "2026-11-10", "open": 1}]
    out = convert_candles(raw)
    assert len(out) == 1 and out[0]["t"] == label_to_epoch("2026-11-10 09:15") and out[0]["v"] == 5


def test_recorder_round_trip(tmp_path):
    ch = make_chain(fetched_at=NOW.timestamp())
    path = recorder.record(str(tmp_path), ch, NOW.timestamp())
    recorder.record(str(tmp_path), ch, NOW.timestamp() + 60)
    rows = list(recorder.read_day(str(tmp_path), "2026-11-10", "NIFTY"))
    assert path and len(rows) == 2 and rows[0]["chain"]["underlying"] == "NIFTY" and recorder.list_days(str(tmp_path)) == ["2026-11-10"]


# ── borrowed Dhan credentials (fno/creds.py) ──
import io, json as _json, types as _types, urllib.error as _ue
from fno import creds as _creds


class _Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_creds_fetch_apply_drop():
    seen = {}

    def opener(req, timeout=0):
        seen["auth"] = req.get_header("Authorization"); seen["url"] = req.full_url
        return _Resp(_json.dumps({"client_id": "C1", "access_token": "T1", "base_url": "https://api.dhan.co/v2/", "dhan_env": "live"}).encode())
    c = _creds.fetch("http://rt:8005/", "tok", opener=opener)
    assert seen["url"] == "http://rt:8005/dhan/fno-credentials" and seen["auth"] == "Bearer tok"
    cfg = _types.SimpleNamespace(DHAN_CLIENT_ID="", DHAN_ACCESS_TOKEN="", DHAN_BASE_URL="x")
    prev = _creds.apply(cfg, c)
    assert (cfg.DHAN_CLIENT_ID, cfg.DHAN_ACCESS_TOKEN, cfg.DHAN_BASE_URL) == ("C1", "T1", "https://api.dhan.co/v2") and prev == ("", "")
    _creds.drop(cfg, prev)
    assert cfg.DHAN_CLIENT_ID == "" and cfg.DHAN_ACCESS_TOKEN == ""


def test_creds_errors_are_plain():
    import pytest

    def http(code):
        def op(req, timeout=0):
            raise _ue.HTTPError("u", code, "m", {}, io.BytesIO(b'{"detail":"No valid Dhan token"}'))
        return op
    with pytest.raises(_creds.CredsError, match="log in"):
        _creds.fetch("http://rt", "t", opener=http(401))
    with pytest.raises(_creds.CredsError, match="No valid Dhan token"):
        _creds.fetch("http://rt", "t", opener=http(409))
    with pytest.raises(_creds.CredsError):
        _creds.fetch("http://rt", "", opener=http(200))
    def empty(req, timeout=0): return _Resp(b"{}")
    with pytest.raises(_creds.CredsError, match="no credentials"):
        _creds.fetch("http://rt", "t", opener=empty)


# ── REAL: fill confirmation, broker-side stop, reconcile (a smarter fake broker) ──
class StatusBroker(execution.Broker):
    """Orders sit in `states` (oid -> OrderStatus). `auto` decides what a new order becomes: TRADED at its limit, or PENDING."""
    def __init__(self, auto="TRADED", net=None):
        self.sent, self.states, self.cancelled, self.auto, self.net = [], {}, [], auto, net
    def place(self, payload):
        self.sent.append(payload); oid = "O%d" % len(self.sent)
        if payload.get("orderType") == "STOP_LOSS":
            self.states[oid] = execution.OrderStatus("PENDING")
        elif self.auto == "TRADED":
            self.states[oid] = execution.OrderStatus("TRADED", payload["price"] - 0.5, payload["quantity"])
        else:
            self.states[oid] = execution.OrderStatus(self.auto)
        return execution.OrderResult(True, order_id=oid, status="TRANSIT")
    def order_status(self, oid): return self.states.get(oid)
    def cancel(self, oid):
        self.cancelled.append(oid)
        if self.states.get(oid) and self.states[oid].state == "PENDING":
            self.states[oid] = execution.OrderStatus("CANCELLED")
        return execution.OrderResult(True, order_id=oid)
    def net_positions(self): return self.net


def real_engine(db, broker):
    cfg = make_cfg(FNO_UNDERLYINGS="NIFTY", FNO_MODE="REAL", FNO_REAL_ENABLED=True, DHAN_CLIENT_ID="1000", DHAN_ACCESS_TOKEN="tok")
    e = engine(db, cfg=cfg, broker=broker)
    e.sleep = lambda s: None
    db.kv_set("fno_armed_day", "2026-11-10")
    return e


def test_real_entry_uses_the_confirmed_fill_and_places_the_stop_on_it(db):
    b = StatusBroker("TRADED")
    e = real_engine(db, b)
    e.tick(NOW)
    p = db.open_positions("REAL")[0]
    buy, sl = b.sent[0], b.sent[1]
    assert p.entry_premium == round(buy["price"] - 0.5, 2) or abs(p.entry_premium - (buy["price"] - 0.5)) < 1e-9
    assert "unconfirmed" not in p.note and p.broker_order_ids["sl"] == "O2"
    assert abs(sl["triggerPrice"] - p.sl_price) < 1e-9 and sl["price"] < sl["triggerPrice"]


def test_real_entry_that_never_fills_is_cancelled_and_no_position_is_kept(db):
    b = StatusBroker("PENDING")
    e = real_engine(db, b)
    e.tick(NOW)
    assert db.open_positions() == [] and b.cancelled == ["O1"]
    assert any(x["kind"] == "order_failed" for x in db.events())


def test_broker_stop_that_traded_closes_the_position_at_its_fill(db):
    b = StatusBroker("TRADED")
    e = real_engine(db, b)
    e.tick(NOW)
    p = db.open_positions("REAL")[0]
    b.states[p.broker_order_ids["sl"]] = execution.OrderStatus("TRADED", p.sl_price - 0.3, p.qty)
    e.tick(NOW + timedelta(seconds=5))
    q = db.get_position(p.id)
    assert q.status == "CLOSED" and q.exit_reason == "SL" and abs(q.exit_premium - (p.sl_price - 0.3)) < 1e-9 and "broker-side stop" in q.note
    assert len([o for o in b.sent if o["transactionType"] == "SELL" and o["orderType"] != "STOP_LOSS"]) == 0     # no second sell


def test_exit_cancels_the_stop_first_then_sells_and_uses_the_real_fill(db):
    b = StatusBroker("TRADED")
    e = real_engine(db, b)
    e.tick(NOW)
    p = db.open_positions("REAL")[0]
    sl_id = p.broker_order_ids["sl"]
    e.squareoff_all(now=NOW + timedelta(seconds=10))
    q = db.get_position(p.id)
    assert q.status == "CLOSED" and sl_id in b.cancelled
    sells = [o for o in b.sent if o["transactionType"] == "SELL" and o["orderType"] == "LIMIT"]
    assert len(sells) == 1 and abs(q.exit_premium - (sells[0]["price"] - 0.5)) < 1e-9


def test_exit_that_does_not_fill_keeps_the_position_open_and_restores_the_stop(db):
    b = StatusBroker("TRADED")
    e = real_engine(db, b)
    e.tick(NOW)
    p = db.open_positions("REAL")[0]
    b.auto = "PENDING"                                               # the SELL will not fill
    e.squareoff_all(now=NOW + timedelta(seconds=10))
    q = db.get_position(p.id)
    assert q.status == "OPEN" and q.broker_order_ids.get("sl") and q.broker_order_ids["sl"] != p.broker_order_ids["sl"]
    assert any("exit not filled" in str(x.get("message", "")) for x in db.events())


def test_reconcile_reports_a_difference_and_never_trades(db):
    b = StatusBroker("TRADED", net={})
    e = real_engine(db, b)
    e.tick(NOW)
    p = db.open_positions("REAL")[0]
    n = len(b.sent)
    r = e.reconcile(NOW + timedelta(seconds=120), force=True)
    assert r["ok"] is False and r["differences"][0]["ours"] == p.qty and r["differences"][0]["broker"] == 0
    b.net = {str(p.security_id): p.qty}
    assert e.reconcile(NOW + timedelta(seconds=240), force=True)["ok"] is True
    assert len(b.sent) == n


def test_dhan_broker_status_cancel_positions_parse():
    import io, json as j
    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False
    calls = []
    def op(req, timeout=0):
        calls.append((req.get_method(), req.full_url))
        if req.full_url.endswith("/positions"):
            return R(j.dumps([{"securityId": "123", "netQty": 65, "exchangeSegment": "NSE_FNO"}, {"securityId": "9", "netQty": 5, "exchangeSegment": "NSE_EQ"}]).encode())
        return R(j.dumps({"orderStatus": "TRADED", "averageTradedPrice": 101.5, "filledQty": 65}).encode())
    br = execution.DhanBroker("https://x/v2", "1", "t", opener=op)
    st = br.order_status("77")
    assert st.state == "TRADED" and st.avg_price == 101.5 and st.filled_qty == 65
    assert br.cancel("77").ok and calls[-1][0] == "DELETE"
    assert br.net_positions() == {"123": 65}
    assert execution.parse_order_status({"orderStatus": "TRIGGER_PENDING"}).state == "PENDING"
    assert execution.parse_order_status({"orderStatus": "weird"}).state == "UNKNOWN"
