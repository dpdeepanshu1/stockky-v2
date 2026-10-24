"""Short selling step 8/9: behaviour test for orders/short_live.py (the LIVE short entry), runnable anywhere: no SQLAlchemy, no network, no pytest.

It replaces the database, ledger, broker client and helper modules with small fakes in sys.modules, so it MUST run in its own interpreter
(tests/test_short_live.py does that with subprocess). Run by hand:  cd services/position-stocks-service && python3 ../../scripts/test_short_live.py
Prints one PASS/FAIL line per case and "ALL n OK"; exit 1 on the first failure. What it does NOT cover: the real SQLAlchemy models and the real Dhan API (T6 / T7 on the VM).
"""
import sys, types, os
from unittest import mock
sys.path.insert(0, '.')
os.environ.setdefault("CONTROL_DIR", os.path.join(os.environ.get("TMPDIR", "/tmp"), "short_live_ctl"))
for n in ("sqlalchemy", "sqlalchemy.orm", "sqlalchemy.exc", "sqlalchemy.pool"):
    sys.modules[n] = mock.MagicMock()
class Row:
    def __init__(self, **kw): self.__dict__.update(kw)
class _Col:  # class-level attr stand-in supporting == / in_
    def __eq__(self, o): return True
    def in_(self, o): return True
    def isnot(self, o): return True
class Model:
    def __init_subclass__(c): pass
def mk(name, *cols):
    d = {k: _Col() for k in cols}
    return type(name, (Row,), d)
models = types.ModuleType("models")
models.ScalpGateState = mk("ScalpGateState"); models.ScalpPosition = mk("ScalpPosition", "symbol", "status", "dhan_super_order_id", "dhan_entry_order_id", "dhan_security_id")
models.ScalpCandidateLog = mk("ScalpCandidateLog"); models.ScalpShortShadow = mk("ScalpShortShadow", "symbol", "day", "status")
sys.modules["models"] = models
cap = types.ModuleType("capital"); cap.__path__ = []
ledger = types.ModuleType("capital.ledger"); sso = types.ModuleType("capital.shared_order_budget"); ssl = types.ModuleType("capital.shared_symbol_lock")
sys.modules.update({"capital": cap, "capital.ledger": ledger, "capital.shared_order_budget": sso, "capital.shared_symbol_lock": ssl})
cap.ledger, cap.shared_order_budget, cap.shared_symbol_lock = ledger, sso, ssl
ex = types.ModuleType("execution"); ex.__path__ = []; dc = types.ModuleType("execution.dhan_client")
class SecurityNotResolvedError(Exception): pass
dc.SecurityNotResolvedError = SecurityNotResolvedError
sys.modules.update({"execution": ex, "execution.dhan_client": dc}); ex.dhan_client = dc
nt = types.ModuleType("notifier"); nt.notify_fire_and_forget = lambda t: NOTES.append(t); sys.modules["notifier"] = nt
NOTES = []
# orders.* helper modules short_entry imports
import importlib
orders = types.ModuleType("orders"); orders.__path__ = ["orders"]; sys.modules["orders"] = orders
for m in ("calendar_gate", "cost_gate", "depth_gate", "entry_pause", "rr_floor", "short_gates", "sizing", "stopout_guard", "approve_first", "entry_cap", "opening_protect"):
    mod = types.ModuleType("orders." + m); sys.modules["orders." + m] = mod; setattr(orders, m, mod)
sp = types.ModuleType("orders.sell_probe"); sp.passed = lambda: True; sys.modules["orders.sell_probe"] = sp; setattr(orders, "sell_probe", sp)   # E108: the T6 PASS is on record in this script (its own tests: tests/test_sell_probe.py)
ad = types.ModuleType("orders.adaptive"); ad.compute = lambda *a, **k: None; ad.round_to_tick = lambda x: round(round(x / 0.05) * 0.05, 2); sys.modules["orders.adaptive"] = ad

import config, types
from unittest import mock
from orders import short_live as sl, short_entry as se
import capital.ledger as ledger, capital.shared_order_budget as sob, capital.shared_symbol_lock as ssl
import execution.dhan_client as dc, notifier
import orders.approve_first as af, orders.entry_cap as ec, orders.opening_protect as op, orders.short_gates as sg

calls = {"released": [], "lock_rel": [], "orders": [], "reserved": []}
def reset(mode="live", cap=1, qty=200):
    calls.update(released=[], lock_rel=[], orders=[], reserved=[]); NOTES.clear()
    config.SHORT_MODE = mode; config.SHORT_LIVE_QTY_CAP = cap; config.USE_SUPER_ORDER = True
    config.SHORT_PLAIN_FALLBACK = False; config.SHORT_PLAIN_FILL_WAIT_S = 2.0; config.SHORT_PLAIN_STOP_GRACE_S = 6.0   # E114: the older cases keep their meaning; section 11 switches the fallback on
    import orders.plain_short as _ps; _ps.reset_state()
    config.DAILY_ORDER_BUDGET = 50; config.MAX_CONCURRENT_SCALP_POSITIONS = 5; config.SHORT_MAX_POSITIONS = 2
    config.SCALP_EXCHANGE_SEGMENT = "NSE_EQ"; config.ENTRY_MARGIN_PAUSE_MINUTES = 5; config.ENTRY_ORDER_FAILED_COOLDOWN_MINUTES = 5
    af.active = lambda: False; ec.entry_reason = lambda db: None; op.pause_reason = lambda db: None
    ssl.try_claim = lambda db, s: True; ssl.release = lambda db, s: calls["lock_rel"].append(s)
    ledger.reserve_capital = lambda db, adaptive_stop_pct, side: (calls["reserved"].append(side) or 20000.0)
    ledger.release_capital = lambda db, position_value, realized_pnl: calls["released"].append(position_value)
    ledger.reserve_additional = lambda db, x: True
    sob.check_and_reserve = lambda db: True
    dc.get_security_id = lambda db, s: "1234"
    dc.is_ambiguous_placement_error = lambda e: False
    dc.place_super_order = lambda db, **k: (calls["orders"].append(k) or {"orderId": "S1"})
    lv = types.SimpleNamespace(stop_price=101.0, target_price=98.0, stop_pct=1.0, target_pct=2.0, breakeven_trigger_pct=0.5)
    se.plan_short = lambda db, c, q, b, n, extra=None: {"live": 100.0, "levels": lv, "qty": qty, "conv": 70}
    se._refuse = lambda db, c, code, why: REF.append((code, why)); se._log = lambda *a, **k: None
REF = []
class DB:
    def __init__(self, gate, opens=()): self.gate, self.opens, self.added = gate, list(opens), []
    def query(self, *a):
        outer = self
        class Q:
            def filter(s, *x): return s
            def first(s): return outer.gate
            def all(s): return outer.opens if a and a[0] is models.ScalpPosition else []
        return Q()
    def add(self, o): self.added.append(o)
    def commit(self): pass
    def rollback(self): pass
    def refresh(self, o): pass
def gate(**k):
    d = dict(is_armed=True, daily_loss_kill_switch_tripped=False, orders_placed_today=0, orders_placed_today_date="x", first_live_order_done=True); d.update(k); return models.ScalpGateState(**d)
cand = types.SimpleNamespace(symbol="X", window_label="5m", pct_change=-1.5, composite_score=3.0, current_ltp=100.0)
ok = 0
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name); ok += bool(cond)
    if not cond: sys.exit(1)

# 1 not live -> nothing
for m in ("off", "shadow"):
    reset(m); db = DB(gate()); r = sl.attempt_short_live(db, cand)
    check(f"mode {m}: no order, no reserve", r is None and not calls["orders"] and not calls["reserved"])
# 2 happy path, cap 1 shrinks the 200 share plan to 1
reset(); db = DB(gate()); r = sl.attempt_short_live(db, cand)
o = calls["orders"][0]
check("live: one SELL Super Order", len(calls["orders"]) == 1 and o["transaction_type"] == "SELL" and o["product_type"] == "INTRADAY" and o["order_type"] == "MARKET")
check("live: quantity capped to 1", o["quantity"] == 1)
check("live: broker stop and target sent", o["stop_loss_price"] == 101.0 and o["target_price"] == 98.0)
check("live: capital reserved as SHORT, unused part released", calls["reserved"] == ["SHORT"] and calls["released"] == [20000.0 - 100.0])
check("live: position saved as SHORT/OPEN with the order id", r is not None and r.side == "SHORT" and r.status == "OPEN" and r.dhan_super_order_id == "S1" and r.quantity == 1 and r.capital_risked == 100.0)
check("live: order budget counted, symbol lock kept", db.gate.orders_placed_today == 1 and not calls["lock_rel"])
check("live: telegram sent", any("SHORT placed" in n for n in NOTES))
# 3 raised cap
reset(cap=50); db = DB(gate()); sl.attempt_short_live(db, cand); check("cap 50: qty 50", calls["orders"][0]["quantity"] == 50)
reset(cap=500, qty=30); db = DB(gate()); sl.attempt_short_live(db, cand); check("cap above plan: plan qty wins", calls["orders"][0]["quantity"] == 30)
# 4 refusals: nothing claimed is left, no order
for name, g, mut in [("not armed", gate(is_armed=False), None), ("kill switch", gate(daily_loss_kill_switch_tripped=True), None),
                     ("order budget", gate(orders_placed_today=50, orders_placed_today_date=__import__("tz_utils").ist_today_str()), None),
                     ("no super order", gate(), lambda: setattr(config, "USE_SUPER_ORDER", False)),
                     ("approve round", gate(), lambda: setattr(af, "active", lambda: True)),
                     ("entry cap", gate(), lambda: setattr(ec, "entry_reason", lambda db: "ENTRY_CAP")),
                     ("opening pause", gate(), lambda: setattr(op, "pause_reason", lambda db: "OPENING_PAUSE"))]:
    reset(); 
    if mut: mut()
    db = DB(g); r = sl.attempt_short_live(db, cand)
    check(f"refused: {name}", r is None and not calls["orders"] and not calls["reserved"])
reset(); db = DB(gate(), opens=[models.ScalpPosition(side="LONG")] * 5); r = sl.attempt_short_live(db, cand)
check("refused: max concurrent positions", r is None and not calls["orders"])
reset(); db = DB(gate(), opens=[models.ScalpPosition(side="SHORT")] * 2); r = sl.attempt_short_live(db, cand)
check("refused: max live shorts", r is None and not calls["orders"])
reset(); ssl.try_claim = lambda db, s: False; r = sl.attempt_short_live(DB(gate()), cand); check("refused: symbol held elsewhere", r is None and not calls["orders"])
# 5 failures after claiming: all released
def undo_ok(): return calls["lock_rel"] == ["X"]
reset(); se.plan_short = lambda *a, **k: None; r = sl.attempt_short_live(DB(gate()), cand); check("plan refused: lock released, no capital taken", r is None and undo_ok() and not calls["reserved"])
reset(); ledger.reserve_capital = lambda db, adaptive_stop_pct, side: None; r = sl.attempt_short_live(DB(gate()), cand); check("ledger refuses: lock released, no order", r is None and undo_ok() and not calls["orders"])
reset(); sob.check_and_reserve = lambda db: False; r = sl.attempt_short_live(DB(gate()), cand); check("shared budget: capital + lock released", r is None and undo_ok() and calls["released"] and not calls["orders"])
reset(); dc.get_security_id = lambda db, s: (_ for _ in ()).throw(dc.SecurityNotResolvedError("x")); r = sl.attempt_short_live(DB(gate()), cand); check("no security id: capital + lock released", r is None and undo_ok() and calls["released"])
# 6 Dhan refuses the SELL Super Order: NO fallback order, everything released
dc.is_security_intraday_restricted_error = lambda m: False; dc.is_circuit_limit_error = lambda m: False; dc.is_intraday_cutoff_error = lambda m: False; dc.is_insufficient_funds_error = lambda m: False
import orders.entry_pause as epz; epz.cooldown_symbol = lambda *a: None; epz.pause_all = lambda *a: None
reset(); dc.place_super_order = lambda db, **k: (calls["orders"].append(k) or (_ for _ in ()).throw(RuntimeError("SELL not allowed")))
db = DB(gate()); r = sl.attempt_short_live(db, cand)
check("SELL refused with SHORT_PLAIN_FALLBACK off: one attempt only, nothing saved, capital + lock released", r is None and len(calls["orders"]) == 1 and not db.added and undo_ok() and calls["released"] == [19900.0, 100.0])
check("SELL refused: reason logged and alert sent", any(c[0] == "SHORT_ORDER_FAILED" for c in REF) and any("refused" in n for n in NOTES))
# 7 ambiguous error recovered
reset(); dc.is_ambiguous_placement_error = lambda e: True
dc.find_recent_buy_order = lambda db, sid, qty, super_order, exclude_ids, transaction_type: {"orderId": "REC9"} if transaction_type == "SELL" else None
dc.place_super_order = lambda db, **k: (_ for _ in ()).throw(TimeoutError("timeout"))
r = sl.attempt_short_live(DB(gate()), cand); check("ambiguous SELL error: recovered order tracked, not released", r is not None and r.dhan_super_order_id == "REC9" and not calls["lock_rel"])
# 8 an unexpected crash before the order never leaves a claim behind
reset(); ledger.reserve_capital = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")); r = sl.attempt_short_live(DB(gate()), cand); check("crash before order: lock released, never raises", r is None and undo_ok() and not calls["orders"])
# 9 cap reads: junk counts as 1
reset(); config.SHORT_LIVE_QTY_CAP = "junk"; check("junk cap reads as 1", sl.qty_cap() == 1)
# 11 E114: the plain-order short (orders/plain_short.py) as short_live drives it. Fake broker: orders recorded, order-book rows and failures set per case.
import orders.plain_short as ps
ps.open_plain_short.__kwdefaults__["sleep"] = lambda s_: None
dc.round_stop_to_tick = lambda p: round(round(p / 0.05) * 0.05, 2); dc.round_to_tick = dc.round_stop_to_tick
dc.is_security_intraday_restricted_error = lambda m: "not allowed to be traded in intraday" in (m or "").lower()
dc.is_circuit_limit_error = lambda m: "circuit" in (m or "").lower()
dc.is_intraday_cutoff_error = lambda m: "cannot be placed at this time" in (m or "").lower()
dc.is_insufficient_funds_error = lambda m: "insufficient funds" in (m or "").lower()
BK = {}
def fb(fallback=True, use_super=True, super_error="Super order not supported", super_ambiguous=False, sell_rows=None, stop_error=None, cover_error=None, unreadable=False, sell_error=None):
    reset(); config.SHORT_PLAIN_FALLBACK = fallback; config.USE_SUPER_ORDER = use_super; ps.reset_state()
    BK.clear(); BK.update(orders=[], stops=[], cancelled=[], supers=[],
                          rows={"E1": {"orderId": "E1", "orderStatus": "TRADED", "filledQty": 1, "averageTradedPrice": 99.9}} if sell_rows is None else sell_rows)
    def place_super(db, **k):
        BK["supers"].append(k); raise RuntimeError(super_error)
    def place_order(db, **k):
        BK["orders"].append(k)
        if k["transaction_type"] == "SELL":
            if sell_error: raise sell_error
            return {"orderId": "E1"}
        if cover_error: raise cover_error
        return {"orderId": "C1"}
    def get_row(db, oid):
        if unreadable: raise RuntimeError("book down")
        return BK["rows"].get(oid)
    def stop(db, **k):
        BK["stops"].append(k)
        if stop_error: raise stop_error
        return {"orderId": "S1"}
    dc.place_super_order, dc.place_order, dc.get_order_row, dc.place_intraday_buy_stop = place_super, place_order, get_row, stop
    dc.cancel_cnc_stop_loss_order = lambda db, order_id: BK["cancelled"].append(order_id)
    dc.is_ambiguous_placement_error = lambda e: bool(super_ambiguous) or isinstance(e, TimeoutError)
    dc.find_recent_buy_order = lambda db, sid, qty, **k: None
fb(); db = DB(gate()); r = sl.attempt_short_live(db, cand)
check("plain: Super Order refused -> a plain SELL then a BUY broker stop at the stop price", r is not None and len(BK["supers"]) == 1 and BK["orders"][0]["transaction_type"] == "SELL" and BK["orders"][0]["order_type"] == "MARKET" and BK["orders"][0]["product_type"] == "INTRADAY" and len(BK["stops"]) == 1 and abs(BK["stops"][0]["trigger_price"] - 100.9) < 0.06)
check("plain: the position row is SHORT/OPEN with NO super order id, the entry order and the stop order", r.side == "SHORT" and r.status == "OPEN" and r.dhan_super_order_id is None and r.dhan_entry_order_id == "E1" and r.plain_stop_order_id == "S1" and r.quantity == 1)
check("plain: the row carries the real fill (99.90) and the stop/target re-anchored on it", abs(r.entry_price - 99.9) < 1e-9 and abs(r.stop_price - 100.9) < 0.06 and r.target_price < 99.9 < r.stop_price)
check("plain: order budget counted once, symbol lock kept, telegram says plain order", db.gate.orders_placed_today == 1 and not calls["lock_rel"] and any("Plain order" in n for n in NOTES))
fb(fallback=False); db = DB(gate()); r = sl.attempt_short_live(db, cand)
check("plain: switch off -> a refused Super Order means no short and no plain order", r is None and not BK["orders"] and undo_ok())
for msg in ("not allowed to be traded in intraday", "Rate Not Within circuit limit", "insufficient funds"):
    fb(super_error=msg); r = sl.attempt_short_live(DB(gate()), cand)
    check("plain: '%s' never falls back" % msg, r is None and not BK["orders"] and undo_ok())
fb(super_error="timeout", super_ambiguous=True); r = sl.attempt_short_live(DB(gate()), cand)
check("plain: an ambiguous Super Order error never falls back (it may exist)", r is None and not BK["orders"])
fb(use_super=False); db = DB(gate()); r = sl.attempt_short_live(db, cand)
check("plain: USE_SUPER_ORDER off + fallback on -> straight to the plain short, no Super Order call", r is not None and not BK["supers"] and r.dhan_super_order_id is None and r.plain_stop_order_id == "S1")
fb(use_super=False, fallback=False); r = sl.attempt_short_live(DB(gate()), cand)
check("plain: USE_SUPER_ORDER off + fallback off -> refused by the live guard, nothing sent", r is None and not BK["orders"] and any(c[0] == "SHORT_NEEDS_SUPER_ORDER" for c in REF))
fb(stop_error=RuntimeError("trigger rejected")); db = DB(gate()); r = sl.attempt_short_live(db, cand)
check("plain: stop refused -> the short is covered at once, no position row, capital + lock released", r is None and not db.added and [o["transaction_type"] for o in BK["orders"]] == ["SELL", "BUY"] and undo_ok() and calls["released"] == [19900.0, 100.0])
check("plain: ... refusal logged as the stop-not-placed code and the day breaker is tripped", any(c[0] == "SHORT_PLAIN_STOP_FAILED" for c in REF) and ps.breaker_tripped())
dc.place_super_order = lambda db, **k: (_ for _ in ()).throw(RuntimeError("Super order not supported")); calls["orders"].clear(); BK["orders"].clear(); r = sl.attempt_short_live(DB(gate()), cand)
check("plain: after a stop failure no further plain short is tried today (a refused Super Order stays refused)", r is None and not BK["orders"])
config.USE_SUPER_ORDER = False; r = sl.attempt_short_live(DB(gate()), cand); check("plain: ... and with Super Orders off the live guard refuses too", r is None and not BK["orders"]); config.USE_SUPER_ORDER = True
fb(stop_error=RuntimeError("x"), cover_error=RuntimeError("down")); db = DB(gate()); r = sl.attempt_short_live(db, cand)
check("plain: stop AND cover fail -> the short is saved WITHOUT a stop id (the guard covers it), capital + lock kept", r is not None and r.plain_stop_order_id is None and r.dhan_super_order_id is None and not calls["lock_rel"])
fb(sell_error=RuntimeError("RMS: margin")); db = DB(gate()); r = sl.attempt_short_live(db, cand)
check("plain: the plain SELL itself refused -> classified like any refusal, nothing saved, capital + lock released", r is None and not db.added and not BK["stops"] and undo_ok() and any(c[0] == "SHORT_ORDER_FAILED" for c in REF))
fb(unreadable=True); db = DB(gate()); r = sl.attempt_short_live(db, cand)
check("plain: SELL state unknown -> NO stop, NO BUY, nothing saved, SELL on the watch list", r is None and not db.added and not BK["stops"] and len(BK["orders"]) == 1 and ps.stats()["orphans_watched"] == 1)
check("plain: ... and the capital + symbol lock stay HELD for the watch (released only when it resolves)", not calls["lock_rel"] and calls["released"] == [19900.0] and any(c[0] == "SHORT_PLAIN_UNKNOWN" for c in REF))
fb(sell_rows={"E1": {"orderId": "E1", "orderStatus": "PART_TRADED", "filledQty": 1, "averageTradedPrice": 99.9}}); config.SHORT_LIVE_QTY_CAP = 3
r = sl.attempt_short_live(DB(gate()), cand)
check("plain: part fill (1 of 3) -> the row and the stop cover 1 share, capital reduced to 1 share", r is not None and r.quantity == 1 and BK["stops"][0]["quantity"] == 1 and abs(r.capital_risked - 99.9) < 0.2)
# 10 step 12: stop width by Market Weather state
for st, want in (("stressed", 0.8), ("panic", 0.6), ("STRESSED", 0.8), ("normal", 1.0), ("calm", 1.0), ("choppy", 1.0), (None, 1.0), ("junk", 1.0)):
    config.SHORT_STOP_MULT_STRESSED, config.SHORT_STOP_MULT_PANIC = 0.8, 0.6
    check(f"stop mult for state {st!r} = {want}", se.stop_mult_for_state(st) == want)
config.SHORT_STOP_MULT_STRESSED = 0.1; check("mult below 0.3 is held at 0.3", se.stop_mult_for_state("stressed") == 0.3)
config.SHORT_STOP_MULT_STRESSED = 5; check("mult above 1 is held at 1 (never loosens)", se.stop_mult_for_state("stressed") == 1.0)
config.SHORT_STOP_MULT_PANIC = float("nan"); check("nan mult = 1", se.stop_mult_for_state("panic") == 1.0)
config.SHORT_STOP_MULT_PANIC = "junk"; check("junk mult = 1", se.stop_mult_for_state("panic") == 1.0)
def lv(stop=101.0, pct=1.0): return types.SimpleNamespace(stop_price=stop, stop_pct=pct, target_price=98.0, target_pct=2.0)
l = se.tighten_short_levels(lv(), 100.0, 0.6)
check("panic 1% stop -> 0.6% (100.60), target untouched", abs(l.stop_price - 100.6) < 1e-9 and abs(l.stop_pct - 0.6) < 1e-6 and l.target_price == 98.0)
l = se.tighten_short_levels(lv(), 100.0, 0.8); check("stressed 1% stop -> 0.8% (100.80)", abs(l.stop_price - 100.8) < 1e-9)
l = se.tighten_short_levels(lv(), 100.0, 1.0); check("mult 1 leaves the levels alone", l.stop_price == 101.0 and l.stop_pct == 1.0)
l = se.tighten_short_levels(lv(100.02, 0.02), 100.0, 0.5); check("a tightened stop that rounds onto price is not used", l.stop_price == 100.02 and l.stop_pct == 0.02)
l = se.tighten_short_levels(lv(), 0.0, 0.6); check("no live price: untouched", l.stop_price == 101.0)
print("ALL", ok, "OK")
