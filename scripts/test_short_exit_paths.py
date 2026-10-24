"""Short selling P1 item 10 (E102): behaviour tests of the REAL exit code with a short position. Runnable anywhere: no SQLAlchemy, no network, no pytest.

It loads the real orders/eod_squareoff.py, reconcile.py, exit_retry.py, close_all.py, stop_watchdog.py, overnight_stop.py, side.py and statuses.py, and
replaces only the edges with fakes in sys.modules: the database (a tiny in-memory query that really applies the filter), the ledger, the broker client
(its error classifiers are copied from the real dhan_client source, not rewritten), the notifier and the symbol lock. So it MUST run in its own interpreter
(tests/test_short_exit_paths.py does that with subprocess). By hand:  cd services/position-stocks-service && python3 -I ../../scripts/test_short_exit_paths.py
Prints one PASS/FAIL line per case and "ALL n OK"; exit 1 on any failure.
NOT covered (needs the VM and the broker): the real SQLAlchemy models and Dhan's real answers - that is T6 / T7.
"""
import ast, os, sys, types
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, ".")
os.environ.setdefault("CONTROL_DIR", os.path.join(os.environ.get("TMPDIR", "/tmp"), "short_exit_ctl"))
for n in ("sqlalchemy", "sqlalchemy.orm", "sqlalchemy.exc", "sqlalchemy.pool"):
    sys.modules[n] = mock.MagicMock()


# ---------- fake models and a database that applies the filters ----------
class Cond:
    def __init__(self, fn): self.fn = fn
class Col:
    def __init__(self, name): self.name = name
    def in_(self, vals): return Cond(lambda p: getattr(p, self.name, None) in vals)
    def isnot(self, v): return Cond(lambda p: getattr(p, self.name, None) is not v)
    def like(self, pat):
        needle = pat.strip("%")
        return Cond(lambda p: needle in (getattr(p, self.name, None) or ""))
    def __eq__(self, v): return Cond(lambda p: getattr(p, self.name, None) == v)
    def __ne__(self, v): return Cond(lambda p: getattr(p, self.name, None) != v)
    __hash__ = object.__hash__

POS_FIELDS = ("id", "symbol", "status", "side", "dhan_super_order_id", "dhan_entry_order_id", "dhan_exit_order_id", "dhan_security_id", "error_message", "quantity",
              "entry_price", "exit_price", "stop_price", "target_price", "capital_risked", "realized_pnl", "realized_pnl_pct", "closed_at", "overnight_converted_to_cnc",
              "overnight_stop_order_id", "overnight_stop_filled_qty_so_far", "overnight_stop_prior_qty", "consecutive_exit_failures", "last_exit_failure_at",
              "adaptive_stop_pct", "adaptive_target_pct", "stop_moved_to_breakeven", "opened_at", "created_at")
def mkmodel(name, fields):
    return type(name, (), {f: Col(f) for f in fields})
class _Pos(mkmodel("ScalpPosition", POS_FIELDS)):
    def __init__(self, **kw):
        self.id = kw.pop("id"); self.symbol = "TESTCO"; self.status = "OPEN"; self.side = "SHORT"; self.dhan_super_order_id = "S1"; self.dhan_entry_order_id = "S1"
        self.dhan_exit_order_id = None; self.dhan_security_id = "1234"; self.quantity = 10; self.entry_price = 100.0; self.exit_price = None
        self.stop_price = 101.0; self.target_price = 98.0; self.capital_risked = 1000.0; self.realized_pnl = None; self.realized_pnl_pct = None
        self.error_message = None; self.closed_at = None; self.overnight_converted_to_cnc = False; self.overnight_stop_order_id = None
        self.overnight_stop_filled_qty_so_far = 0; self.overnight_stop_prior_qty = 0; self.consecutive_exit_failures = 0; self.last_exit_failure_at = None
        self.adaptive_stop_pct = 1.0; self.adaptive_target_pct = 2.0; self.stop_moved_to_breakeven = False
        self.__dict__.update(kw)
class _Gate(mkmodel("ScalpGateState", ("mode",))):
    def __init__(self, **kw):
        self.eod_squareoff_fired_date = None; self.daily_loss_kill_switch_tripped = False; self.daily_loss_kill_switch_tripped_date = None; self.__dict__.update(kw)
models = types.ModuleType("models"); models.ScalpPosition = _Pos; models.ScalpGateState = _Gate; models.ScalpCandidateLog = mkmodel("ScalpCandidateLog", ("symbol",))
sys.modules["models"] = models

class DB:
    def __init__(self, positions=(), gate=None):
        self.positions, self.gate, self.commits, self.rollbacks = list(positions), gate or _Gate(), 0, 0
    def query(self, *cols):
        outer = self
        class Q:
            def __init__(s): s.conds = []
            def filter(s, *c): s.conds += [x for x in c if isinstance(x, Cond)]; return s
            def filter_by(s, **k): return s
            def _rows(s):
                if cols and cols[0] is _Gate: return [outer.gate]
                if cols and isinstance(cols[0], Col): return [(p.dhan_exit_order_id,) for p in outer.positions if p.dhan_exit_order_id]
                return [p for p in outer.positions if all(c.fn(p) for c in s.conds)]
            def all(s): return s._rows()
            def first(s): r = s._rows(); return r[0] if r else None
        return Q()
    def commit(self): self.commits += 1
    def rollback(self): self.rollbacks += 1
    def add(self, o): pass
    def refresh(self, o): pass
    def expire_all(self): pass

# ---------- fake capital, broker, notifier ----------
LOG = {"released": [], "reclaimed": [], "lock_released": [], "lock_claimed": [], "late": [], "orders": [], "cancels": [], "budget": 0, "restricted": []}
NOTES, CRIT = [], []
cap = types.ModuleType("capital"); cap.__path__ = []
ledger = types.ModuleType("capital.ledger"); sso = types.ModuleType("capital.shared_order_budget"); ssl = types.ModuleType("capital.shared_symbol_lock")
ledger.release_capital = lambda db, position_value, realized_pnl: LOG["released"].append((position_value, realized_pnl))
ledger.reclaim_premature_release = lambda db, capital_risked: LOG["reclaimed"].append(capital_risked)
ledger.book_late_realized_pnl = lambda db, pnl: LOG["late"].append(pnl)
ledger.reconcile_position_cost = lambda db, delta: None
ledger.get_state = lambda db: {"total_allocated_capital": 100000.0}
sso.record_order_unconditional = lambda db: LOG.__setitem__("budget", LOG["budget"] + 1)
ssl.release = lambda db, s: LOG["lock_released"].append(s); ssl.try_claim = lambda db, s: (LOG["lock_claimed"].append(s) or True)
cap.ledger, cap.shared_order_budget, cap.shared_symbol_lock = ledger, sso, ssl
sys.modules.update({"capital": cap, "capital.ledger": ledger, "capital.shared_order_budget": sso, "capital.shared_symbol_lock": ssl})
nt = types.ModuleType("notifier"); nt.notify_fire_and_forget = lambda t: NOTES.append(t); nt.notify_critical = lambda t: CRIT.append(t); sys.modules["notifier"] = nt
ie = types.ModuleType("screening.intraday_eligibility"); ie.record_restriction = lambda db, sym, detail=None: LOG["restricted"].append(sym)
scr = types.ModuleType("screening"); scr.__path__ = []; scr.intraday_eligibility = ie; sys.modules.update({"screening": scr, "screening.intraday_eligibility": ie})

# the broker client: the error classifiers and tick sizes are the REAL ones (parsed out of execution/dhan_client.py), everything else is a script
ex = types.ModuleType("execution"); ex.__path__ = []; dc = types.ModuleType("execution.dhan_client")
_src = open("execution/dhan_client.py", encoding="utf-8").read()
_keep = {"TICK_SIZE", "_TICK_SIZE_BANDS", "_INTRADAY_CUTOFF_MARKERS", "_SECURITY_INTRADAY_RESTRICTED_MARKERS", "_INSUFFICIENT_FUNDS_MARKERS", "_CIRCUIT_LIMIT_MARKERS"}
_funcs = {"tick_size_for_price", "is_intraday_cutoff_error", "is_security_intraday_restricted_error", "is_insufficient_funds_error", "is_circuit_limit_error", "round_to_tick"}
_nodes = []
for node in ast.parse(_src).body:
    if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in _keep for t in node.targets): _nodes.append(node)
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id in _keep: _nodes.append(node)
    elif isinstance(node, ast.FunctionDef) and node.name in _funcs: _nodes.append(node)
import typing
for _n in ("Optional", "Any", "Dict", "List", "Tuple", "Union"): setattr(dc, _n, getattr(typing, _n))
import decimal
for _n in ("Decimal", "ROUND_HALF_UP", "ROUND_HALF_DOWN", "ROUND_UP", "ROUND_DOWN", "InvalidOperation"): setattr(dc, _n, getattr(decimal, _n))
exec(compile(ast.Module(body=_nodes, type_ignores=[]), "dhan_client(real parts)", "exec"), dc.__dict__)
assert all(hasattr(dc, f) for f in ("is_intraday_cutoff_error", "is_circuit_limit_error", "tick_size_for_price")), "could not lift the real classifiers"
sys.modules.update({"execution": ex, "execution.dhan_client": dc}); ex.dhan_client = dc
BOOK = {"orders": [], "supers": [], "orders_err": None, "supers_err": None, "place": None}
def _place(db, **k):
    LOG["orders"].append(k)
    if BOOK["place"]: return BOOK["place"](k)
    return {"orderId": "C%d" % len(LOG["orders"])}
dc.place_order = _place
def _orders(db, release_db=False):
    if BOOK["orders_err"]: raise BOOK["orders_err"]
    return BOOK["orders"]
dc.get_order_list = _orders
def _supers(db, release_db=False):
    if BOOK["supers_err"]: raise BOOK["supers_err"]
    return BOOK["supers"]
dc.get_super_order_list = _supers
dc.cancel_super_order = lambda db, order_id, order_leg: LOG["cancels"].append((order_id, order_leg)) or {}
dc.cancel_cnc_stop_loss_order = lambda *a, **k: {}
MODS = []
dc.modify_super_order = lambda db, **k: MODS.append(k) or {}
dc.verify_stop_leg = lambda *a, **k: True

import config
from orders import side, statuses, exit_retry, eod_squareoff as eod, reconcile, close_all, stop_watchdog   # the REAL modules
import orders.overnight_stop as overnight_stop
IST = __import__("tz_utils").IST

ok = 0
def check(name, cond, detail=""):
    global ok
    if cond: ok += 1; print("PASS", name)
    else: print("FAIL", name, detail); sys.exit(1)

def reset():
    for k in ("released", "reclaimed", "lock_released", "lock_claimed", "late", "orders", "cancels", "restricted"): LOG[k].clear()
    LOG["budget"] = 0; NOTES.clear(); CRIT.clear()
    BOOK.update(orders=[], supers=[], orders_err=None, supers_err=None, place=None)
    config.USE_SUPER_ORDER = True; config.MANUAL_EXIT_CANCEL_WAIT_S = 0; config.EOD_SELL_RETRY_ATTEMPTS = 3; config.EOD_SELL_RETRY_DELAY_SECONDS = 0
    config.EXIT_RETRY_BASE_COOLDOWN_SECONDS = 60; config.EXIT_RETRY_MAX_COOLDOWN_SECONDS = 600; config.EXIT_RETRY_ALERT_THRESHOLD = 3
    config.OVERNIGHT_HOLD_ENABLED = False; config.SCALP_PRODUCT_TYPE = "INTRADAY"; config.SCALP_EXCHANGE_SEGMENT = "NSE_EQ"; config.SHORT_SQUAREOFF_TIME_IST = "14:55"
    config.RECONCILE_USE_ORDER_EVENTS = False; config.ENTRY_FILL_SLIPPAGE_ALERT_PCT = 0.0
    config.STOP_WATCHDOG_ENABLED = True; config.STOP_WATCHDOG_MIN_BREACH_TICKS = 1; config.STOP_WATCHDOG_GRACE_S = 0; config.STOP_WATCHDOG_MAX_TICK_AGE_S = 15; config.STOP_WATCHDOG_RETRY_S = 30
    reconcile._pending_kept_logged.clear(); reconcile._rearm_state.clear(); eod._short_cover_logged.clear(); stop_watchdog.reset_state()
    for a in ("_exit_fill_logged", "_exit_fill_confirmed"): getattr(reconcile, a).clear()
eod.time.sleep = lambda s: None

def short(id=1, **kw): return _Pos(id=id, side="SHORT", **kw)
def long_(id=2, **kw):
    d = dict(stop_price=99.0, target_price=102.0); d.update(kw); return _Pos(id=id, side="LONG", **d)
def super_row(entry=100.0, target=None, stop=None, oid="S1"):
    """A Dhan super order row. target/stop: None = leg still live, else (status, price)."""
    legs = []
    if target is not None: legs.append({"legName": "TARGET_LEG", "orderStatus": target[0], "averageTradedPrice": target[1], "orderId": "T1"})
    else: legs.append({"legName": "TARGET_LEG", "orderStatus": "PENDING"})
    if stop is not None: legs.append({"legName": "STOP_LOSS_LEG", "orderStatus": stop[0], "averageTradedPrice": stop[1], "orderId": "L1"})
    else: legs.append({"legName": "STOP_LOSS_LEG", "orderStatus": "PENDING"})
    return {"orderId": oid, "orderStatus": "TRADED", "averageTradedPrice": entry, "legName": "ENTRY_LEG", "legDetails": legs}
def plain(oid, tx, status, qty=10, px=0.0, sid="1234", tag="EOD_SQUAREOFF"):
    return {"orderId": oid, "transactionType": tx, "orderStatus": status, "quantity": qty, "filledQty": qty if status == "TRADED" else 0,
            "averageTradedPrice": px, "securityId": sid, "tag": tag, "orderType": "MARKET"}
def ist(h, m): return datetime(2026, 10, 26, h, m, tzinfo=IST)

# ============ 1. stop hit (price ABOVE entry) ============
reset(); p = short(); db = DB([p]); BOOK["supers"] = [super_row(stop=("TRADED", 101.0))]
n = reconcile.run_exit_reconciliation(db)
check("1 stop hit: booked as STOP_HIT, exit 101", n == 1 and p.status == "STOP_HIT" and p.exit_price == 101.0)
check("1 stop hit: a short LOSES when price rises: pnl -10 (10 sh x 1.0), -1.00%", abs(p.realized_pnl + 10.0) < 1e-9 and abs(p.realized_pnl_pct + 1.0) < 1e-9)
check("1 stop hit: capital returned in full with the (negative) pnl, symbol lock released", LOG["released"] == [(1000.0, -10.0)] and LOG["lock_released"] == ["TESTCO"])
check("1 stop hit: red alert sent, closed_at set, error text cleared", any("STOP_HIT" in t and "🔴" in t for t in NOTES) and p.closed_at and p.error_message is None)

# ============ 2. target hit (price BELOW entry) ============
reset(); p = short(); db = DB([p]); BOOK["supers"] = [super_row(target=("TRADED", 98.0))]
reconcile.run_exit_reconciliation(db)
check("2 target hit: TARGET_HIT, a short WINS when price falls: pnl +20, +2.00%", p.status == "TARGET_HIT" and abs(p.realized_pnl - 20.0) < 1e-9 and abs(p.realized_pnl_pct - 2.0) < 1e-9)
check("2 target hit: green alert, capital back with +20", LOG["released"] == [(1000.0, 20.0)] and any("🟢" in t for t in NOTES))
# the mirror image on a long, so a sign slip in either direction is caught
reset(); q = long_(); BOOK["supers"] = [super_row(target=("TRADED", 102.0), oid="S1")]; reconcile.run_exit_reconciliation(DB([q]))
check("2 control: a LONG target at 102 books +20 (not the short's sign)", q.status == "TARGET_HIT" and abs(q.realized_pnl - 20.0) < 1e-9)

# ============ 3. leg fill with no price on the leg: the cover price comes from the BUY, never from the entry SELL ============
reset(); p = short(); bare = super_row(stop=("TRADED", None)); bare["legDetails"][1].pop("averageTradedPrice")
BOOK["supers"] = [bare]; BOOK["orders"] = [plain("E1", "SELL", "TRADED", px=100.0, tag=""), plain("B1", "BUY", "TRADED", px=100.6, tag="")]
reconcile.run_exit_reconciliation(DB([p]))
check("3 leg without a price: exit read from the BUY fill (100.60), not the entry SELL (100.00)", p.status == "STOP_HIT" and abs(p.exit_price - 100.6) < 1e-9 and abs(p.realized_pnl + 6.0) < 1e-9)
reset(); p = short(); BOOK["supers"] = [bare]; BOOK["orders"] = [plain("E1", "SELL", "TRADED", px=97.0, tag="")]   # only the entry SELL is in the book (a made-up 97 so it cannot be confused)
reconcile.run_exit_reconciliation(DB([p]))
check("3 leg without a price and no BUY in the book: the SELL row (97) is ignored; the existing fallback chain applies (parent row / trigger), same as for longs", p.status == "STOP_HIT" and p.exit_price != 97.0)

# ============ 4. partial fills ============
reset(); p = short(); BOOK["supers"] = [super_row(target=("PART_TRADED", 98.0))]; n = reconcile.run_exit_reconciliation(DB([p]))
check("4 half-filled target leg: position NOT closed, nothing booked, capital not released", n == 0 and p.status == "OPEN" and not LOG["released"] and p.realized_pnl is None)
reset(); p = short(status="EOD_SQUAREOFF", dhan_exit_order_id="C9", exit_price=100.0, realized_pnl=0.0, realized_pnl_pct=0.0, closed_at=datetime.now(timezone.utc),
                   error_message="EOD_SQUAREOFF_PENDING_RECONCILE: placeholder")
BOOK["orders"] = [plain("C9", "BUY", "PART_TRADED", px=99.5)]; BOOK["supers"] = [super_row()]
reconcile.run_exit_reconciliation(DB([p]))
check("4 half-filled cover BUY: the row stays pending (placeholder kept), no P&L booked yet", p.status == "EOD_SQUAREOFF" and "PENDING_RECONCILE" in (p.error_message or "") and not LOG["released"])
BOOK["orders"] = [plain("C9", "BUY", "TRADED", px=99.5)]
reconcile.run_exit_reconciliation(DB([p]))
check("4 the cover then fills: real price booked, short pnl = (100-99.5) x 10 = +5, only the pnl is released (capital went back at placement)",
      abs(p.exit_price - 99.5) < 1e-9 and abs(p.realized_pnl - 5.0) < 1e-9 and LOG["released"] == [(0.0, 5.0)] and p.error_message is None)

# ============ 5. rejected cover and the retry ============
reset(); p = short(); calls = []
def flaky(k):
    calls.append(k)
    if len(calls) < 3: raise TimeoutError("read timed out")
    return {"orderId": "OK3"}
BOOK["place"] = flaky
r = eod._fire_flat_sell(DB([p]), p)
check("5 two timeouts then success: 3 attempts, ALL are BUY / INTRADAY / MARKET / qty 10 (never a SELL, which would double the short)",
      r["orderId"] == "OK3" and len(calls) == 3 and all(c["transaction_type"] == "BUY" and c["product_type"] == "INTRADAY" and c["order_type"] == "MARKET" and c["quantity"] == 10 for c in calls))
check("5 a successful placement clears the failure streak", p.consecutive_exit_failures == 0 and p.last_exit_failure_at is None)

reset(); p = short(); calls = []
def timeout_but_accepted(k):
    calls.append(k); raise TimeoutError("timeout after send")
BOOK["place"] = timeout_but_accepted; BOOK["orders"] = [plain("SEEN1", "BUY", "PENDING", px=0.0)]    # the first attempt reached Dhan after all
r = eod._fire_flat_sell(DB([p]), p)
check("5 the timed-out BUY was in fact accepted: attempt 2 adopts it, no duplicate BUY is sent", r["orderId"] == "SEEN1" and len(calls) == 1)
reset(); p = short(); calls = []; BOOK["place"] = timeout_but_accepted; BOOK["orders"] = [plain("ENTRY1", "SELL", "TRADED", px=100.0, tag="")]   # only the short's own entry SELL is in the book
try: eod._fire_flat_sell(DB([p]), p); raised = False
except TimeoutError: raised = True
check("5 the short's own entry SELL is never mistaken for the cover: all 3 attempts go out as BUY, then the error is raised", raised and len(calls) == 3 and all(c["transaction_type"] == "BUY" for c in calls))
check("5 exhausted attempts record one placement failure", p.consecutive_exit_failures == 1 and p.last_exit_failure_at is not None)

reset(); p = short(); BOOK["place"] = lambda k: (_ for _ in ()).throw(RuntimeError("RMS: Rate Not Within Ckt Limit 90 To 110"))
try: eod._fire_flat_sell(DB([p]), p)
except RuntimeError: pass
check("5 a circuit-limit rejection is not retried (1 call) and starts the cooldown", len(LOG["orders"]) == 1 and p.consecutive_exit_failures == 1)
try: eod._fire_flat_sell(DB([p]), p); cool = False
except exit_retry.ExitInCooldown: cool = True
check("5 the very next cover attempt is held by the cooldown without calling the broker", cool and len(LOG["orders"]) == 1)
try: eod.close_position_now(DB([p]), p); rej = ""
except eod.ManualCloseRejected as e: rej = str(e)
check("5 a manual close during the cooldown is refused with the remaining wait", "backoff" in rej and len(LOG["orders"]) == 1)
p.consecutive_exit_failures = 3; exit_retry.record_failure(DB([p]), p, "boom")
check("5 the 4th straight failure (not a multiple of the alert threshold 3) sends no alert", len(CRIT) == 0)
p.consecutive_exit_failures = 5; exit_retry.record_failure(DB([p]), p, "boom")
check("5 the 6th failure (a multiple of 3) fires the critical alert", len(CRIT) == 1 and "TESTCO" in CRIT[0])

reset(); p = short(); BOOK["place"] = lambda k: (_ for _ in ()).throw(RuntimeError("Insufficient funds / margin shortfall"))
try: eod.close_position_now(DB([p]), p); msg = ""
except eod.ManualCloseRejected as e: msg = str(e)
check("5 insufficient margin on the cover: refused with the broker's reason, position still OPEN, capital NOT released", "insufficient margin" in msg and p.status == "OPEN" and not LOG["released"] and not LOG["lock_released"])

# ---- a dead cover (REJECTED, zero fill): the short is still open at the broker ----
reset(); p = short(status="EOD_SQUAREOFF", dhan_exit_order_id="C7", exit_price=100.0, realized_pnl=0.0, error_message="EOD_SQUAREOFF_PENDING_RECONCILE: placeholder")
BOOK["orders"] = [plain("C7", "BUY", "REJECTED"), plain("E1", "SELL", "TRADED", px=100.0, tag="")]; BOOK["supers"] = [super_row()]
reconcile.run_exit_reconciliation(DB([p]))
check("5 a rejected cover with no other exit: row goes to ERROR (the short may still be open), capital and symbol lock are taken back", p.status == "ERROR" and "SELL_DEAD" in p.error_message and LOG["reclaimed"] == [1000.0] and LOG["lock_claimed"] == ["TESTCO"])
check("5 ...and a critical alert goes out", len(CRIT) >= 1)
reset(); p = short(status="EOD_SQUAREOFF", dhan_exit_order_id="C7", exit_price=100.0, realized_pnl=0.0, error_message="EOD_SQUAREOFF_PENDING_RECONCILE: placeholder")
BOOK["orders"] = [plain("C7", "BUY", "REJECTED"), plain("C8", "BUY", "TRADED", px=99.0)]; BOOK["supers"] = [super_row()]
reconcile.run_exit_reconciliation(DB([p]))
check("5 a rejected duplicate cover while an earlier BUY already filled: books that fill (+10), NOT an ERROR", p.status == "EOD_SQUAREOFF" and p.exit_price == 99.0 and abs(p.realized_pnl - 10.0) < 1e-9 and p.dhan_exit_order_id == "C8")
reset(); p = short(status="EOD_SQUAREOFF", dhan_exit_order_id="C7", exit_price=100.0, realized_pnl=0.0, error_message="EOD_SQUAREOFF_PENDING_RECONCILE: placeholder")
BOOK["orders"] = [plain("C7", "BUY", "REJECTED"), plain("E1", "SELL", "TRADED", px=97.0, tag="")]; BOOK["supers"] = [super_row()]
reconcile.run_exit_reconciliation(DB([p]))
check("5 a filled SELL (the entry!) is never taken as the exit of a short: still ERROR", p.status == "ERROR")

# ============ 6. forced cover: 14:55 for shorts, 15:00 for the rest ============
reset(); s_, l_ = short(1), long_(2, symbol="LONGCO", dhan_super_order_id="S2", dhan_security_id="9999"); db = DB([s_, l_])
check("6 before 14:55 nothing happens", eod.run_short_squareoff(db, ist(14, 54)) == 0 and not LOG["orders"] and s_.status == "OPEN")
n = eod.run_short_squareoff(db, ist(14, 55))
check("6 at 14:55 the short is covered with ONE plain MARKET BUY, INTRADAY, qty 10, security 1234", n == 1 and len(LOG["orders"]) == 1 and LOG["orders"][0]["transaction_type"] == "BUY"
      and LOG["orders"][0]["product_type"] == "INTRADAY" and LOG["orders"][0]["security_id"] == "1234" and LOG["orders"][0]["quantity"] == 10)
check("6 the Super Order legs are cancelled first (entry, target, stop)", [c[1] for c in LOG["cancels"]] == ["ENTRY_LEG", "TARGET_LEG", "STOP_LOSS_LEG"] and all(c[0] == "S1" for c in LOG["cancels"]))
check("6 the short is now EOD_SQUAREOFF + pending reconcile, capital released with 0 pnl, lock released, order counted", s_.status == "EOD_SQUAREOFF" and "PENDING_RECONCILE" in s_.error_message
      and LOG["released"] == [(1000.0, 0.0)] and LOG["lock_released"] == ["TESTCO"] and LOG["budget"] == 1 and s_.dhan_exit_order_id == "C1")
check("6 the LONG is left alone by the 14:55 sweep", l_.status == "OPEN" and all(o["security_id"] != "9999" for o in LOG["orders"]))
check("6 a second tick does not cover twice", eod.run_short_squareoff(db, ist(14, 56)) == 0 and len(LOG["orders"]) == 1)
n = eod.run_eod_squareoff(db)
check("6 the 15:00 sweep then closes the long with a SELL and finds no short left to touch", n == 1 and l_.status == "EOD_SQUAREOFF" and len(LOG["orders"]) == 2
      and LOG["orders"][1]["transaction_type"] == "SELL" and LOG["orders"][1]["security_id"] == "9999")
check("6 SHORT_SQUAREOFF_TIME_IST is read at call time (a Switches-page change applies at once)", (setattr(config, "SHORT_SQUAREOFF_TIME_IST", "14:30"), eod.run_short_squareoff(DB([short(5)]), ist(14, 40)))[1] == 1)
reset(); s_ = short(1); BOOK["place"] = lambda k: (_ for _ in ()).throw(RuntimeError("Rate Not Within Ckt Limit")); db = DB([s_])
check("6 a refused 14:55 cover leaves the short OPEN, never raises, and the 15:00 sweep still tries it", eod.run_short_squareoff(db, ist(14, 56)) == 0 and s_.status == "OPEN")
BOOK["place"] = None; s_.consecutive_exit_failures = 0; s_.last_exit_failure_at = None
check("6 the 15:00 sweep covers it with a BUY once the broker accepts", eod.run_eod_squareoff(db) == 1 and s_.status == "EOD_SQUAREOFF" and LOG["orders"][-1]["transaction_type"] == "BUY")
reset(); s_ = short(1, status="EXIT_LEGS_REJECTED"); check("6 a short whose exit legs were rejected is covered too", eod.run_short_squareoff(DB([s_]), ist(14, 57)) == 1 and LOG["orders"][0]["transaction_type"] == "BUY")
reset(); s_ = _Pos(id=1, side="SIDEWAYS"); check("6 a position with an unknown side is not touched by the short sweep (no wrong-way order)", eod.run_short_squareoff(DB([s_]), ist(14, 57)) == 0 and not LOG["orders"])
reset(); s_ = short(1); BOOK["supers"] = [super_row(stop=("TRADED", 101.0))]
check("6 the stop leg filled just before the cover: no BUY is sent (it would open a long), the row waits for reconcile", eod.run_short_squareoff(DB([s_]), ist(14, 56)) == 0 and not LOG["orders"] and s_.status == "OPEN")
check("6 ...and reconcile then books the stop hit", reconcile.run_exit_reconciliation(DB([s_])) == 1 and s_.status == "STOP_HIT")
reset(); s_ = short(1, overnight_converted_to_cnc=True)
check("6 a short flagged CNC is refused, not bought on top of itself", eod.run_short_squareoff(DB([s_]), ist(14, 56)) == 0 and not LOG["orders"])
reset(); s_ = short(1); db = DB([s_]); db.query = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down"))
check("6 a database error in the short sweep is swallowed (the trading loop must not stop)", eod.run_short_squareoff(db, ist(15, 0)) == 0)

# ============ 7. restart in the middle of a trade ============
# A process restart loses every in-memory dict; the database row and the broker book are all that is left.
reset(); p = short(); BOOK["supers"] = [super_row()]
for d in (reconcile._rearm_state, reconcile._pending_kept_logged, eod._short_cover_logged): d.clear(); stop_watchdog.reset_state()
check("7 restart with a live short and live legs: nothing is closed, nothing is sent", reconcile.run_exit_reconciliation(DB([p])) == 0 and p.status == "OPEN" and not LOG["orders"])
reset(); p = short(); BOOK["supers"] = [super_row(target=("TRADED", 98.0))]
check("7 restart after the target filled while we were down: booked from the broker's row (+20)", reconcile.run_exit_reconciliation(DB([p])) == 1 and abs(p.realized_pnl - 20.0) < 1e-9)
reset(); p = short(status="EOD_SQUAREOFF", dhan_exit_order_id="C5", exit_price=100.0, realized_pnl=0.0, closed_at=datetime.now(timezone.utc), error_message="EOD_SQUAREOFF_PENDING_RECONCILE: placeholder")
BOOK["orders"] = [plain("C5", "BUY", "TRADED", px=98.4)]; BOOK["supers"] = [super_row()]
reconcile.run_exit_reconciliation(DB([p]))
check("7 restart after the cover was sent but before its fill was booked: the BUY fill is booked (+16)", abs(p.realized_pnl - 16.0) < 1e-9 and p.error_message is None)
try: eod.close_position_now(DB([p]), p); again = False
except eod.ManualCloseRejected: again = True
check("7 a second close click on a short that is already covered is refused (no double BUY)", again and not LOG["orders"])
reset(); gate = _Gate(eod_squareoff_fired_date=eod.ist_today_str()); s_ = short(1)
check("7 the once-a-day guard survives a restart: the 15:00 sweep does not run twice", eod.run_eod_squareoff(DB([s_], gate)) == 0 and not LOG["orders"])
check("7 ...but the 14:55 short sweep has no once-a-day lock, so a short opened after a restart is still covered", eod.run_short_squareoff(DB([s_], gate), ist(14, 58)) == 1)
reset(); p = short(); BOOK["supers"] = [super_row(stop=("REJECTED", None), target=("REJECTED", None))]
reconcile.run_exit_reconciliation(DB([p]))
check("7 restart with both exit legs dead at the broker: marked EXIT_LEGS_REJECTED so the cover sweep picks it up", p.status == "EXIT_LEGS_REJECTED")

# ============ 8. close_all (the big red button) ============
reset(); s_, l_ = short(1), long_(2, symbol="LONGCO", dhan_super_order_id="S2", dhan_security_id="9999"); db = DB([s_, l_]); res = close_all.sweep(db)
tx = {o["security_id"]: o["transaction_type"] for o in LOG["orders"]}
check("8 close-all sends BUY for the short and SELL for the long, one order each", res["sent"] == 2 and tx == {"1234": "BUY", "9999": "SELL"})
check("8 both positions are CLOSE_ALL + pending reconcile (a status reconcile resolves)", s_.status == "CLOSE_ALL" and l_.status == "CLOSE_ALL" and "CLOSE_ALL" in statuses.FLAT_SELL_STATUSES)
BOOK["orders"] = [plain("C1", "BUY", "TRADED", px=99.0, tag="EOD_SQUAREOFF"), plain("C2", "SELL", "TRADED", px=101.0, sid="9999")]; BOOK["supers"] = [super_row(), super_row(oid="S2")]
reconcile.run_exit_reconciliation(db)
check("8 close-all fills resolve with the right sign: short +10 (100->99), long +10 (100->101)", abs(s_.realized_pnl - 10.0) < 1e-9 and abs(l_.realized_pnl - 10.0) < 1e-9)
reset(); s_ = short(1); BOOK["place"] = lambda k: (_ for _ in ()).throw(RuntimeError("Insufficient funds"))
res = close_all.sweep(DB([s_]))
check("8 close-all reports a failed cover loudly (so the person closes it by hand)", res["sent"] == 0 and res["failed"] and "FAILED: TESTCO" in close_all.summary(res))

# ============ 9. stop watchdog on a short ============
def tick(px, age=0.0):
    ws = types.ModuleType("feed.ws_client"); ws.get_tick_buffer = lambda sym: [(__import__("time").time() - age, px)]
    fd = types.ModuleType("feed"); fd.__path__ = []; fd.ws_client = ws; sys.modules.update({"feed": fd, "feed.ws_client": ws})
reset(); s_ = short(1, stop_price=101.0); tick(100.5)
check("9 watchdog: price still below a short's stop (in profit or flat): no action", stop_watchdog.run_stop_watchdog(DB([s_])) == 0 and not LOG["orders"])
tick(101.4)
n = stop_watchdog.run_stop_watchdog(DB([s_]))
check("9 watchdog: price ABOVE a short's stop and the position still open: covered at market with a BUY, status STOP_BREACH_EXIT", n == 1 and s_.status == "STOP_BREACH_EXIT" and LOG["orders"][0]["transaction_type"] == "BUY")
reset(); s_ = short(1, stop_price=101.0); tick(99.0)
check("9 watchdog: a LOWER price is never read as a breach for a short (a long's rule would have fired here)", stop_watchdog.run_stop_watchdog(DB([s_])) == 0 and not LOG["orders"])
reset(); l_ = long_(2, stop_price=99.0); tick(98.0)
check("9 watchdog control: the same low price IS a breach for a long", stop_watchdog.run_stop_watchdog(DB([l_])) == 1 and LOG["orders"][0]["transaction_type"] == "SELL")

# ============ 10. every closed row's P&L and the reconcile filters: no long-only leftovers ============
reset(); MODS.clear(); p = short(); BOOK["supers"] = [super_row(entry=100.4, stop=("TRADED", 101.0))]
reconcile.run_exit_reconciliation(DB([p]))
check("10 entry corrected to the real SELL fill (100.40): the stop-hit P&L uses it, short sign: (100.4-101)x10 = -6", abs(p.entry_price - 100.4) < 1e-9 and abs(p.realized_pnl + 6.0) < 1e-9)
reset(); MODS.clear(); p = short(); BOOK["supers"] = [super_row(entry=100.4)]; reconcile.run_exit_reconciliation(DB([p]))
tg = next((m["target_price"] for m in MODS if m.get("order_leg") == "TARGET_LEG"), None); sl = next((m["stop_loss_price"] for m in MODS if m.get("order_leg") == "STOP_LOSS_LEG"), None)
check("10 entry fill 100.40 while the position is still open: the legs are re-armed MIRRORED (target 98.40 below the entry, stop 101.40 above it)", tg is not None and sl is not None and abs(tg - 98.4) < 0.051 and abs(sl - 101.4) < 0.051 and tg < 100.4 < sl, (tg, sl))
check("10 charges: the sell leg of a short is the ENTRY (checked in the module, see test_short_exit_side)", "short=" in open("orders/charges_ledger.py").read())


# ============ 11. E105 (P2 item 15): the parent row's average is the ENTRY fill, never an exit; stop overshoot is measured ============
def zero_os():
    for k in reconcile._overshoot: reconcile._overshoot[k] = 0
def bare_leg(**kw):
    r = super_row(**kw)
    for leg in r["legDetails"]: leg.pop("averageTradedPrice", None)
    return r
zero_os(); reset(); p = short(); BOOK["supers"] = [bare_leg(stop=("TRADED", None))]; reconcile.run_exit_reconciliation(DB([p]))
check("11 short stop hit, no price on the leg, nothing in the order book: booked at its stop 101 (was the ENTRY fill 100 -> P&L 0)", p.status == "STOP_HIT" and p.exit_price == 101.0 and abs(p.realized_pnl + 10.0) < 1e-9)
check("11 ...counted as an ESTIMATE, not as a measured overshoot", reconcile._overshoot["stop_hits"] == 1 and reconcile._overshoot["estimated"] == 1 and reconcile._overshoot["confirmed"] == 0 and reconcile._overshoot["beyond"] == 0)
reset(); q = long_(); BOOK["supers"] = [bare_leg(target=("TRADED", None))]; reconcile.run_exit_reconciliation(DB([q]))
check("11 long target hit, no price anywhere: booked at its target 102 (was the entry fill 100 -> P&L 0)", q.status == "TARGET_HIT" and q.exit_price == 102.0 and abs(q.realized_pnl - 20.0) < 1e-9)
check("11 _extract_leg_price: parent average equal to the entry fill is skipped (trigger used)", reconcile._extract_leg_price({}, {"averageTradedPrice": 100.0}, 101.0, entry_price=100.0) == 101.0)
check("11 _extract_leg_price: a parent average that is NOT the entry fill is still used", reconcile._extract_leg_price({}, {"averageTradedPrice": 100.7}, 101.0, entry_price=100.0) == 100.7)
check("11 _extract_leg_price: old callers (no entry_price) keep the old order", reconcile._extract_leg_price({}, {"averageTradedPrice": 100.0}, 101.0) == 100.0)
check("11 _extract_leg_price: a real leg fill still wins over everything", reconcile._extract_leg_price({"averageTradedPrice": 101.4}, {"averageTradedPrice": 100.0}, 101.0, entry_price=100.0) == 101.4)
sp, lp = short(), long_()
check("11 stop_overshoot_pct: short exit above its stop 101 at 101.5 = 0.495; long exit below stop 99 at 98.5 = 0.505", abs(reconcile.stop_overshoot_pct(sp, 101.5) - 0.495) < 1e-9 and abs(reconcile.stop_overshoot_pct(lp, 98.5) - 0.505) < 1e-9)
check("11 stop_overshoot_pct: a better-than-stop fill is negative, on the stop is 0, junk is None", reconcile.stop_overshoot_pct(sp, 100.5) < 0 and reconcile.stop_overshoot_pct(sp, 101.0) == 0 and reconcile.stop_overshoot_pct(sp, None) is None and reconcile.stop_overshoot_pct(sp, 0) is None)
zero_os(); reset(); p = short(); BOOK["supers"] = [super_row(stop=("TRADED", 101.5))]; reconcile.run_exit_reconciliation(DB([p]))
check("11 short stop filled at 101.5 (stop 101): measured, 1 beyond, 0.495 %", p.exit_price == 101.5 and reconcile._overshoot["confirmed"] == 1 and reconcile._overshoot["beyond"] == 1 and abs(reconcile._overshoot["max_pct"] - 0.495) < 1e-9)
reset(); q = long_(); BOOK["supers"] = [super_row(stop=("TRADED", 98.5))]; reconcile.run_exit_reconciliation(DB([q]))
st = reconcile.overshoot_stats()
check("11 long stop filled at 98.5 (stop 99): second one beyond, mean of the two 0.5 %", st["beyond"] == 2 and st["stop_hits"] == 2 and abs(st["mean_pct_of_beyond"] - 0.5) < 1e-9 and abs(st["max_pct"] - 0.505) < 1e-9)
reset(); q = long_(); BOOK["supers"] = [super_row(stop=("TRADED", 99.0))]; reconcile.run_exit_reconciliation(DB([q]))
reset(); p = short(); BOOK["supers"] = [super_row(stop=("TRADED", 100.8))]; reconcile.run_exit_reconciliation(DB([p]))
st = reconcile.overshoot_stats()
check("11 a fill ON the stop and a fill better than it are measured but are not 'beyond'", st["confirmed"] == 4 and st["beyond"] == 2)
zero_os(); reset(); p = short(); BOOK["supers"] = [super_row(target=("TRADED", 98.0))]; reconcile.run_exit_reconciliation(DB([p]))
check("11 a TARGET hit is never counted as a stop hit", reconcile._overshoot["stop_hits"] == 0)
bad = types.SimpleNamespace(stop_price="x", symbol="T", id=1, side="LONG"); reconcile.note_stop_hit(bad, 98.0, True); reconcile.note_stop_hit(None, None, True)
check("11 note_stop_hit never raises on junk", True)

print("ALL", ok, "OK")
