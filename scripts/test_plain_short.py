"""E114 behaviour test for orders/plain_short.py, runnable anywhere: no SQLAlchemy, no network, no pytest. Fakes the broker, ledger, lock and eod module in sys.modules,
so it MUST run in its own interpreter. Run:  cd services/position-stocks-service && python3 ../../scripts/test_plain_short.py   (prints PASS/FAIL lines and 'ALL n OK')."""
import sys, types, os
from unittest import mock
sys.path.insert(0, '.')
os.environ.setdefault("CONTROL_DIR", os.path.join(os.environ.get("TMPDIR", "/tmp"), "plain_short_ctl"))
for n in ("sqlalchemy", "sqlalchemy.orm", "sqlalchemy.exc", "sqlalchemy.pool"):
    sys.modules[n] = mock.MagicMock()
NOTES, REL, LOCK, COVERS = [], [], [], []
nt = types.ModuleType("notifier"); nt.notify_fire_and_forget = lambda t: NOTES.append(t); sys.modules["notifier"] = nt
cap = types.ModuleType("capital"); cap.__path__ = []
ledger = types.ModuleType("capital.ledger"); ledger.release_capital = lambda db, position_value, realized_pnl: REL.append((position_value, realized_pnl))
ssl = types.ModuleType("capital.shared_symbol_lock"); ssl.release = lambda db, s: LOCK.append(s)
sys.modules.update({"capital": cap, "capital.ledger": ledger, "capital.shared_symbol_lock": ssl}); cap.ledger, cap.shared_symbol_lock = ledger, ssl
ex = types.ModuleType("execution"); ex.__path__ = []; dc = types.ModuleType("execution.dhan_client")
sys.modules.update({"execution": ex, "execution.dhan_client": dc}); ex.dhan_client = dc
class Ambig(Exception): pass
dc.is_ambiguous_placement_error = lambda e: isinstance(e, Ambig)
for nme in ("is_security_intraday_restricted_error", "is_circuit_limit_error", "is_intraday_cutoff_error", "is_insufficient_funds_error"):
    setattr(dc, nme, (lambda key: (lambda m: key in (m or "").lower()))({"is_security_intraday_restricted_error": "not allowed to be traded in intraday", "is_circuit_limit_error": "circuit",
                                                                           "is_intraday_cutoff_error": "cannot be placed at this time", "is_insufficient_funds_error": "insufficient funds"}[nme]))
dc.round_stop_to_tick = lambda p: round(round(p / 0.05) * 0.05, 2); dc.round_to_tick = dc.round_stop_to_tick
orders = types.ModuleType("orders"); orders.__path__ = ["orders"]; sys.modules["orders"] = orders
rc = types.ModuleType("orders.reconcile"); rc.note_stop_hit = lambda *a, **k: None; sys.modules["orders.reconcile"] = rc; orders.reconcile = rc
sd = types.ModuleType("orders.side"); sd.pnl = lambda pos, e, x, q: round((e - x) * q, 2); sd.pnl_pct = lambda pos, e, x: round((e - x) / e * 100, 4); sys.modules["orders.side"] = sd; orders.side = sd
sw = types.ModuleType("orders.stop_watchdog"); TICK = {"v": (None, None)}
sw._last_tick = lambda sym: TICK["v"]
sw.is_breached = lambda ltp, stop, short=False: bool(ltp and stop and ltp >= stop + 0.05)
sys.modules["orders.stop_watchdog"] = sw; orders.stop_watchdog = sw
eod = types.ModuleType("orders.eod_squareoff"); EOD = {"fail": None}
def close_position_now(db, pos, exit_reason="MANUAL_EXIT"):
    if EOD["fail"]: raise EOD["fail"]
    COVERS.append((pos.symbol, exit_reason)); pos.status = exit_reason
    return {}
eod.close_position_now = close_position_now; sys.modules["orders.eod_squareoff"] = eod; orders.eod_squareoff = eod
tzu = types.ModuleType("tz_utils"); DAY = {"d": "2026-10-26"}; tzu.ist_today_str = lambda: DAY["d"]; sys.modules["tz_utils"] = tzu
import config
config.SHORT_PLAIN_FALLBACK = True; config.SHORT_PLAIN_FILL_WAIT_S = 2.0; config.SHORT_PLAIN_STOP_GRACE_S = 6.0; config.SCALP_EXCHANGE_SEGMENT = "NSE_EQ"
config.STOP_WATCHDOG_RETRY_S = 30.0; config.STOP_WATCHDOG_MAX_TICK_AGE_S = 15.0; config.MANUAL_EXIT_CANCEL_WAIT_S = 0.0
import orders.plain_short as ps

N = [0]
def check(name, cond):
    N[0] += 1
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: sys.exit(1)

BK = {}
def fresh(**kw):
    ps.reset_state(); NOTES.clear(); REL.clear(); LOCK.clear(); COVERS.clear(); TICK["v"] = (None, None); EOD["fail"] = None; config.SHORT_PLAIN_FALLBACK = True
    BK.clear(); BK.update(orders=[], stops=[], cancelled=[], rows={}, list_error=False, stop_error=None, cover_error=None, sell_error=None, replace_stop=None, unreadable=False, **kw)
    def place_order(db, **k):
        BK["orders"].append(k)
        if k["transaction_type"] == "SELL":
            if BK["sell_error"]: raise BK["sell_error"]
            return {"orderId": "E1"}
        if BK["cover_error"]: raise BK["cover_error"]
        return {"orderId": "C1"}
    def stop(db, **k):
        BK["stops"].append(k)
        if BK["stop_error"]: raise BK["stop_error"]
        return {"orderId": "S%d" % len(BK["stops"])}
    def get_row(db, oid):
        if BK["unreadable"]: raise RuntimeError("book down")
        return BK["rows"].get(oid)
    def get_list(db):
        if BK["list_error"] or BK["unreadable"]: raise RuntimeError("book down")
        return list(BK["rows"].values())
    dc.place_order, dc.place_intraday_buy_stop, dc.get_order_row, dc.get_order_list = place_order, stop, get_row, get_list
    dc.cancel_cnc_stop_loss_order = lambda db, order_id: BK["cancelled"].append(order_id)
    dc.find_recent_buy_order = lambda db, sid, qty, **k: BK.get("found")
SOLD = {"orderId": "E1", "orderStatus": "TRADED", "filledQty": 1, "averageTradedPrice": 100.0}
def op(**kw):
    ps_kw = dict(is_armed=True, security_id="77", sym="X", qty=1, live=100.0, stop_price=101.0, target_price=98.0, stop_pct=1.0, target_pct=2.0, sleep=lambda s: None)
    ps_kw.update(kw); return ps.open_plain_short(None, **ps_kw)

# ---- entry
fresh(); BK["rows"]["E1"] = dict(SOLD)
r = op()
check("entry: SELL filled, stop placed -> ok + protected", r["ok"] and r["protected"] and r["stop_order_id"] == "S1" and not r["critical"])
check("entry: the SELL is a plain INTRADAY MARKET SELL, the stop a BUY trigger at 101.00", BK["orders"][0]["transaction_type"] == "SELL" and BK["orders"][0]["order_type"] == "MARKET" and BK["orders"][0]["product_type"] == "INTRADAY" and BK["stops"][0]["trigger_price"] == 101.0)
fresh(); BK["rows"]["E1"] = dict(SOLD, averageTradedPrice=99.0); r = op()
check("entry: levels are re-anchored on the real fill (99.00 -> stop 99.99/100.00, target 97.02)", abs(r["stop_price"] - 100.0) < 0.06 and abs(r["target_price"] - 97.0) < 0.06 and BK["stops"][0]["trigger_price"] == r["stop_price"])
fresh(); BK["rows"]["E1"] = dict(SOLD); BK["stop_error"] = RuntimeError("trigger in the past"); r = op()
check("entry: stop refused -> the short is covered at once (BUY MARKET INTRADAY), ok False, covered True", (not r["ok"]) and r["covered"] and [o["transaction_type"] for o in BK["orders"]] == ["SELL", "BUY"] and BK["orders"][1]["product_type"] == "INTRADAY")
check("entry: ... and the day breaker is tripped, so no new plain short today", ps.breaker_tripped() and not ps.entry_allowed() and not ps.refusal_allows_fallback("some refusal", False))
DAY["d"] = "2026-10-27"; check("entry: the breaker ends with the day", not ps.breaker_tripped() and ps.entry_allowed()); DAY["d"] = "2026-10-26"
fresh(); BK["rows"]["E1"] = dict(SOLD); BK["stop_error"] = RuntimeError("x"); BK["cover_error"] = RuntimeError("down"); r = op()
check("entry: stop AND cover fail -> the short is adopted without a stop, critical", r["ok"] and not r["protected"] and r["critical"] and any("WITHOUT A STOP" in n for n in NOTES))
check("entry: three cover attempts, no more", sum(1 for o in BK["orders"] if o["transaction_type"] == "BUY") == 3)
fresh(); BK["rows"]["E1"] = dict(SOLD); BK["stop_error"] = RuntimeError("x"); BK["cover_error"] = Ambig("timeout"); r = op()
check("entry: an ambiguous cover error is never repeated blind", sum(1 for o in BK["orders"] if o["transaction_type"] == "BUY") == 1 and r["critical"])
fresh(); BK["sell_error"] = RuntimeError("Dhan: not enough margin"); r = op()
check("entry: SELL refused -> no short, no stop, no BUY", (not r["ok"]) and not r["critical"] and not BK["stops"] and len(BK["orders"]) == 1)
fresh(); BK["rows"]["E1"] = {"orderId": "E1", "orderStatus": "REJECTED"}; r = op()
check("entry: SELL ends REJECTED -> no short, no stop", (not r["ok"]) and not r["critical"] and not BK["stops"] and "REJECTED" in r["reason"])
fresh(); BK["unreadable"] = True; r = op()
check("entry: the book cannot be read -> state unknown: critical, NO stop, NO BUY", (not r["ok"]) and r["critical"] and r["entry_order_id"] == "E1" and not BK["stops"] and len(BK["orders"]) == 1)
fresh(); BK["rows"]["E1"] = {"orderId": "E1", "orderStatus": "PENDING", "filledQty": 0}; r = op()
check("entry: a SELL that never fills is cancelled; if the state stays unclear it is critical, nothing bought", (not r["ok"]) and r["critical"] and "E1" in BK["cancelled"] and not BK["stops"])
fresh(); BK["sell_error"] = Ambig("timeout"); BK["found"] = None; r = op()
check("entry: ambiguous SELL error and the book cannot confirm it -> critical, no stop, no BUY", (not r["ok"]) and r["critical"] and r["entry_order_id"] is None and not BK["stops"])
fresh(); BK["sell_error"] = Ambig("timeout"); BK["found"] = {"orderId": "E1"}; BK["rows"]["E1"] = dict(SOLD); r = op()
check("entry: ambiguous SELL error but the book has the order -> adopted and protected", r["ok"] and r["protected"])
fresh(); BK["rows"]["E1"] = {"orderId": "E1", "orderStatus": "PART_TRADED", "filledQty": 1, "averageTradedPrice": 100.0}; r = op(qty=3)
check("entry: part fill (1 of 3) -> the rest is cancelled and the stop covers the filled 1 share", r["ok"] and r["qty"] == 1 and BK["stops"][0]["quantity"] == 1 and "E1" in BK["cancelled"])

# ---- refusal rules
fresh()
check("refusal: a plain refusal may fall back", ps.refusal_allows_fallback("Dhan API error: Super order not supported", False))
check("refusal: an ambiguous error never falls back", not ps.refusal_allows_fallback("timeout", True))
for m in ("not allowed to be traded in intraday", "Rate Not Within circuit limit", "order cannot be placed at this time", "insufficient funds"):
    check("refusal: '%s' is not retried as a plain SELL" % m, not ps.refusal_allows_fallback(m, False))
config.SHORT_PLAIN_FALLBACK = False; check("refusal: switch off -> no fallback and no entry", not ps.refusal_allows_fallback("x", False) and not ps.entry_allowed()); config.SHORT_PLAIN_FALLBACK = True

# ---- release before a cover
P = types.SimpleNamespace
def pos(**kw):
    d = dict(id=1, symbol="X", side="SHORT", status="OPEN", quantity=1, entry_price=100.0, stop_price=101.0, target_price=98.0, plain_stop_order_id="S1", dhan_security_id="77",
             dhan_super_order_id=None, dhan_entry_order_id="E1", capital_risked=100.0, error_message=None, closed_at=None, exit_price=None, overnight_converted_to_cnc=False)
    d.update(kw); return P(**d)
fresh(); p = pos(plain_stop_order_id=None); check("release: no stop on the row -> 'none'", ps.release_stop_before_cover(None, p, sleep=lambda s: None)[0] == "none")
fresh(); BK["rows"]["S1"] = {"orderId": "S1", "orderStatus": "PENDING"}; p = pos()
def after_cancel(db, order_id): BK["cancelled"].append(order_id); BK["rows"]["S1"]["orderStatus"] = "CANCELLED"
dc.cancel_cnc_stop_loss_order = after_cancel
st = ps.release_stop_before_cover(None, p, sleep=lambda s: None)
check("release: live stop is cancelled and read back -> 'cancelled', id cleared", st[0] == "cancelled" and p.plain_stop_order_id is None and BK["cancelled"] == ["S1"])
fresh(); BK["rows"]["S1"] = {"orderId": "S1", "orderStatus": "TRADED", "averageTradedPrice": 101.0, "filledQty": 1}; p = pos()
check("release: the stop already filled -> 'filled' (no BUY may follow)", ps.release_stop_before_cover(None, p, sleep=lambda s: None)[0] == "filled" and not BK["cancelled"])
fresh(); BK["rows"]["S1"] = {"orderId": "S1", "orderStatus": "PENDING"}; p = pos()
def fill_on_cancel(db, order_id): BK["rows"]["S1"]["orderStatus"] = "TRADED"; BK["rows"]["S1"]["filledQty"] = 1
dc.cancel_cnc_stop_loss_order = fill_on_cancel
check("release: the stop fills while being cancelled -> 'filled', never 'cancelled'", ps.release_stop_before_cover(None, p, sleep=lambda s: None)[0] == "filled")
fresh(); BK["rows"]["S1"] = {"orderId": "S1", "orderStatus": "PART_TRADED", "filledQty": 1}; p = pos(quantity=3)
check("release: a stop filled in part -> 'error' before any cancel (a full-size BUY would oversell into a long)", ps.release_stop_before_cover(None, p, sleep=lambda s: None)[0] == "error" and not BK["cancelled"])
fresh(); BK["rows"]["S1"] = {"orderId": "S1", "orderStatus": "CANCELLED"}; p = pos()
check("release: an already dead stop -> 'cancelled'", ps.release_stop_before_cover(None, p, sleep=lambda s: None)[0] == "cancelled")
fresh(); BK["unreadable"] = True; p = pos()
check("release: cancel fails and the book is unreadable -> 'error', id kept (no BUY may go)", ps.release_stop_before_cover(None, p, sleep=lambda s: None)[0] == "error" and p.plain_stop_order_id == "S1")

# ---- the guard
def guard(p, now=1000.0):
    ps._plain_shorts = lambda db: [p] if p.status == "OPEN" else []
    class DB:
        def commit(s): pass
        def rollback(s): pass
    return ps.run_plain_short_guard(DB(), now)
fresh(); BK["rows"]["S1"] = {"orderId": "S1", "orderStatus": "TRADED", "averageTradedPrice": 101.2, "filledQty": 1}; p = pos()
n = guard(p)
check("guard: the broker stop filled -> STOP_HIT booked at the real fill, P&L = short math", n == 1 and p.status == "STOP_HIT" and p.exit_price == 101.2 and p.realized_pnl == -1.2)
check("guard: ... capital released WITH the P&L, symbol lock released, alert sent, no BUY sent", REL == [(100.0, -1.2)] and LOCK == ["X"] and any("STOP_HIT" in x for x in NOTES) and not COVERS)
fresh(); BK["rows"]["S1"] = {"orderId": "S1", "orderStatus": "TRADED", "filledQty": 1}; p = pos(); guard(p)
check("guard: a stop fill without a price is booked at the stop price and flagged", p.exit_price == 101.0 and "no average traded price" in (p.error_message or ""))
fresh(); BK["rows"]["S1"] = {"orderId": "S1", "orderStatus": "PENDING"}; p = pos(); TICK["v"] = (99.5, 1.0)
check("guard: a healthy short (stop resting, price between target and stop) is left alone", guard(p) == 0 and p.status == "OPEN" and not COVERS and not BK["stops"])
fresh(); BK["rows"]["S1"] = {"orderId": "S1", "orderStatus": "CANCELLED"}; p = pos(); n = guard(p)
check("guard: the stop died -> ONE new stop at the stored stop price, saved, alert", p.plain_stop_order_id == "S1" and len(BK["stops"]) == 1 and BK["stops"][0]["trigger_price"] == 101.0 and p.status == "OPEN")
fresh(); BK["rows"]["S1"] = {"orderId": "S1", "orderStatus": "REJECTED"}; p = pos(); guard(p, 1000.0)
BK["rows"][p.plain_stop_order_id] = {"orderStatus": "REJECTED"}; guard(p, 1100.0)
check("guard: a stop is re-placed at most once per position; the second death is covered by hand", len(BK["stops"]) == 1 and COVERS == [("X", "STOP_BREACH_EXIT")])
fresh(); BK["rows"]["S1"] = {"orderId": "S1", "orderStatus": "CANCELLED"}; BK["stop_error"] = RuntimeError("no"); p = pos(); guard(p)
check("guard: the stop died and cannot be re-placed -> cover by hand at once", COVERS == [("X", "STOP_BREACH_EXIT")])
fresh(); p = pos(plain_stop_order_id=None); guard(p)
check("guard: no stop on record -> cover by hand (STOP_BREACH_EXIT), critical alert once", COVERS == [("X", "STOP_BREACH_EXIT")] and sum("no stop" in n.lower() for n in NOTES) == 1)
fresh(); p = pos(plain_stop_order_id=None); EOD["fail"] = RuntimeError("Dhan refused"); guard(p, 1000.0); guard(p, 1010.0)
check("guard: a refused cover is backed off STOP_WATCHDOG_RETRY_S (30 s) ...", not COVERS and sum("no stop" in n.lower() for n in NOTES) == 1)
EOD["fail"] = None; guard(p, 1031.0); check("guard: ... and retried after it", COVERS == [("X", "STOP_BREACH_EXIT")])
fresh(); BK["rows"]["S1"] = {"orderId": "S1", "orderStatus": "PENDING"}; p = pos(); TICK["v"] = (97.9, 1.0); guard(p)
check("guard: price at or under the target -> TARGET_EXIT cover by hand (software target)", COVERS == [("X", "TARGET_EXIT")])
fresh(); BK["rows"]["S1"] = {"orderId": "S1", "orderStatus": "PENDING"}; p = pos(); TICK["v"] = (97.0, 60.0); guard(p)
check("guard: a stale tick never triggers anything", not COVERS)
fresh(); BK["rows"]["S1"] = {"orderId": "S1", "orderStatus": "PENDING"}; p = pos(); TICK["v"] = (101.3, 1.0); guard(p, 1000.0)
check("guard: price past the stop, the broker stop silent: the grace (6 s) comes first, no cover yet", not COVERS)
guard(p, 1007.0); check("guard: ... then the cover by hand (STOP_BREACH_EXIT)", COVERS == [("X", "STOP_BREACH_EXIT")] and any("did not fire" in n for n in NOTES))
fresh(); BK["rows"]["S1"] = {"orderId": "S1", "orderStatus": "PENDING"}; p = pos(); TICK["v"] = (101.3, 1.0); guard(p, 1000.0); TICK["v"] = (100.0, 1.0); guard(p, 1004.0); TICK["v"] = (101.3, 1.0); guard(p, 1008.0)
check("guard: a price that comes back under the stop resets the grace clock", not COVERS)
fresh(); BK["rows"]["S1"] = {"orderId": "S1", "orderStatus": "PENDING"}; p = pos(); TICK["v"] = (101.0, 1.0); guard(p, 1000.0); guard(p, 1100.0)
check("guard: price right at the stop (not a tick past it) never covers", not COVERS)
fresh(); config.SHORT_PLAIN_FALLBACK = False; BK["rows"]["S1"] = {"orderId": "S1", "orderStatus": "PENDING"}; p = pos(); TICK["v"] = (97.0, 1.0); guard(p)
check("guard: the SHORT_PLAIN_FALLBACK switch off does NOT stop the care of an already open plain short (target cover still runs)", COVERS == [("X", "TARGET_EXIT")])
fresh(); BK["rows"]["S1"] = {"orderId": "S1", "orderStatus": "PENDING"}; p = pos(side="LONG"); TICK["v"] = (97.0, 1.0)
ps._plain_shorts = lambda db: []
check("guard: a LONG is never touched (the query keeps shorts only)", ps.run_plain_short_guard(None, 1000.0) == 0 and not COVERS)
fresh(); BK["unreadable"] = True; p = pos(); TICK["v"] = (97.0, 1.0); guard(p)
check("guard: an unreadable book -> no BUY blind (the cover waits for a readable book)", not COVERS)
fresh(); BK["rows"]["S1"] = {"orderId": "S1", "orderStatus": "PART_TRADED", "filledQty": 1}; p = pos(quantity=3); TICK["v"] = (97.0, 1.0); guard(p)
check("guard: a live part-filled stop is never covered on top of", not COVERS and p.status == "OPEN")
fresh(); BK["rows"]["S1"] = {"orderId": "S1", "orderStatus": "CANCELLED", "filledQty": 1}; p = pos(quantity=3); guard(p); guard(p, 1010.0)
check("guard: a part-filled stop that then died is alerted once (critical), nothing is sent", not COVERS and not BK["stops"] and sum("filled in part" in n for n in NOTES) == 1)
fresh(); BK["rows"]["S1"] = {"orderId": "S1", "orderStatus": "CANCELLED"}; BK["rows"]["C9"] = {"orderId": "C9", "orderStatus": "TRADED", "transactionType": "BUY", "securityId": "77", "quantity": 1, "tag": "SCALPCOVER"}
p = pos(); n = guard(p)
check("guard: a cover BUY that already exists at Dhan is adopted, never doubled (no new stop, no second cover)", p.status == "STOP_BREACH_EXIT" and p.dhan_exit_order_id == "C9" and not COVERS and not BK["stops"] and REL == [(100.0, 0.0)] and LOCK == ["X"])
check("guard: ... the adopted cover is left PENDING_RECONCILE for reconcile to price", "PENDING_RECONCILE" in (p.error_message or ""))
fresh(); BK["rows"]["S1"] = {"orderId": "S1", "orderStatus": "PENDING", "orderType": "STOP_LOSS_MARKET", "transactionType": "BUY", "securityId": "77", "quantity": 1}; p = pos(); TICK["v"] = (97.0, 1.0); guard(p)
check("guard: the resting STOP is not mistaken for a cover", COVERS == [("X", "TARGET_EXIT")])

# ---- the orphan watch list
fresh(); BK["rows"]["E1"] = {"orderId": "E1", "orderStatus": "TRADED", "filledQty": 1}; ps.watch_orphan("E1", "X", "77", 1, 100.0)
ps._plain_shorts = lambda db: []; n = ps.run_plain_short_guard(None, 1000.0)
check("orphan: a watched SELL that turned out FILLED is covered at once with a MARKET BUY INTRADAY, capital and lock released, alert", n == 1 and BK["orders"][-1]["transaction_type"] == "BUY" and BK["orders"][-1]["product_type"] == "INTRADAY" and REL == [(100.0, 0.0)] and LOCK == ["X"] and any("Unconfirmed short covered" in x for x in NOTES))
fresh(); BK["rows"]["E1"] = {"orderId": "E1", "orderStatus": "CANCELLED"}; ps.watch_orphan("E1", "X", "77", 1, 100.0); ps.run_plain_short_guard(None, 1000.0)
check("orphan: a watched SELL that ended dead with no fill releases capital and lock, no BUY", not BK["orders"] and REL == [(100.0, 0.0)] and LOCK == ["X"])
fresh(); BK["unreadable"] = True; ps.watch_orphan("E1", "X", "77", 1, 100.0); ps.run_plain_short_guard(None, 1000.0)
check("orphan: an unreadable book keeps watching, sends and releases nothing", not BK["orders"] and not REL and ps.stats()["orphans_watched"] == 1)
fresh(); BK["rows"]["E1"] = {"orderId": "E1", "orderStatus": "PENDING", "filledQty": 0}; ps.watch_orphan("E1", "X", "77", 1, 100.0); ps._orphans["E1"]["since"] = 1000.0 - 5.0; ps.run_plain_short_guard(None, 1000.0)
check("orphan: a pending SELL older than the fill wait is cancelled, still watched", "E1" in BK["cancelled"] and ps.stats()["orphans_watched"] == 1 and not REL)
fresh(); ps.watch_orphan("E1", "X", "77", 1, 100.0); ps._orphans["E1"]["since"] = 1000.0 - 300.0; ps.run_plain_short_guard(None, 1000.0)
check("orphan: a SELL missing from a readable book for 2+ minutes does not exist: released", REL == [(100.0, 0.0)] and ps.stats()["orphans_watched"] == 0)
fresh(); BK["rows"]["E1"] = {"orderId": "E1", "orderStatus": "TRADED", "filledQty": 1}; BK["cover_error"] = Ambig("timeout"); ps.watch_orphan("E1", "X", "77", 1, 100.0); ps.run_plain_short_guard(None, 1000.0)
check("orphan: an ambiguous cover error is alerted and not repeated blind", sum(1 for o in BK["orders"] if o["transaction_type"] == "BUY") == 1 and ps.stats()["orphans_watched"] == 0 and any("NOT confirmed" in x for x in NOTES))
fresh(); ps.watch_orphan(None, "X", "77", 1, 1.0); check("orphan: no order id -> nothing to watch", ps.stats()["orphans_watched"] == 0)
print("ALL %d OK" % N[0])
