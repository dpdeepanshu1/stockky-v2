"""E272 round 11: a fresh typed event against an open trade pulls its stop in. Plain runner: python3 tests/test_e272_event_hook.py (pytest also collects it)."""
from __future__ import annotations

import os
import sys
import types
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _m in ("httpx", "sqlalchemy", "sqlalchemy.orm"):
    if _m not in sys.modules:
        try:
            __import__(_m)
        except Exception:  # noqa: BLE001
            sys.modules[_m] = types.ModuleType(_m)
if not hasattr(sys.modules["sqlalchemy.orm"], "Session"):
    sys.modules["sqlalchemy.orm"].Session = object

from orders import event_hook as eh

OPEN = datetime(2026, 11, 2, 4, 0, tzinfo=timezone.utc)
LATER = (OPEN + timedelta(minutes=20)).isoformat()
EARLIER = (OPEN - timedelta(minutes=20)).isoformat()


def ev(pol=-1, st=0.7, at=LATER, typ="probe"):
    return {"type": typ, "polarity": pol, "strength": st, "announced_at": at}


def dec(side="LONG", stop=98.0, ltp=100.0, event=None, **kw):
    return eh.decide(side=side, entry_ts=OPEN, stop=stop, ltp=ltp, tick=0.05, event=event, **kw)


def test_long_negative_event_halves_the_distance():
    d = dec(event=ev())
    assert d["action"] == "TIGHTEN" and d["new_stop"] == 99.0


def test_short_positive_event_halves_the_distance():
    d = dec(side="SHORT", stop=102.0, event=ev(pol=1))
    assert d["action"] == "TIGHTEN" and d["new_stop"] == 101.0


def test_wrong_polarity_weak_unknown_and_old_events_do_nothing():
    assert dec(event=ev(pol=1))["action"] == "HOLD"                 # good news on a long
    assert dec(side="SHORT", stop=102.0, event=ev(pol=-1))["action"] == "HOLD"
    assert dec(event=ev(st=0.2))["action"] == "HOLD"                # below the strength floor
    assert dec(event=ev(at=EARLIER))["action"] == "HOLD"            # known at entry
    assert dec(event=ev(at=None))["action"] == "HOLD"               # cannot prove it is news after entry
    assert dec(event=None)["action"] == "HOLD"


def test_never_loosens_and_never_crosses_the_price():
    assert dec(stop=99.98, event=ev())["action"] == "HOLD"          # already inside the target distance
    assert dec(stop=101.0, event=ev())["action"] == "HOLD"          # wrong side of price: watchdog case
    assert dec(side="SHORT", stop=99.0, event=ev(pol=1))["action"] == "HOLD"


def test_each_event_tightens_once():
    assert dec(event=ev(), already_seen=LATER)["action"] == "HOLD"
    assert dec(event=ev(), already_seen="other")["action"] == "TIGHTEN"


def test_strongest_event_parsing():
    b = {"ok": True, "events": [{"polarity": 0, "strength": 0.9}, {"polarity": 1, "strength": 0.3, "type": "order_win"},
                                {"polarity": -1, "strength": 0.6, "type": "probe", "published": LATER}]}
    e = eh.strongest_event(b)
    assert e["type"] == "probe" and e["polarity"] == -1 and e["announced_at"] == LATER
    assert eh.strongest_event({"ok": False, "events": b["events"]}) is None
    assert eh.strongest_event({"ok": True, "events": [{"polarity": 0, "strength": 1}]}) is None
    assert eh.strongest_event("junk") is None


# ---- whole pass with stand-ins ----
class Pos:
    def __init__(self, **kw):
        self.id, self.symbol, self.status, self.side = 1, "ABC", "OPEN", "LONG"
        self.stop_price, self.opened_at, self.flow_json = 98.0, OPEN, None
        self.dhan_super_order_id, self.plain_stop_order_id, self.overnight_converted_to_cnc = "SO1", None, False
        self.stop_moved_to_breakeven = False
        self.__dict__.update(kw)


class Q:
    def __init__(self, rows): self.rows = rows
    def filter(self, *a): return self
    def all(self): return self.rows


class DB:
    def __init__(self, rows): self.rows, self.commits = rows, 0
    def query(self, *_): return Q(self.rows)
    def commit(self): self.commits += 1
    def rollback(self): pass


def _install(verify=True, raises=False):
    calls = []
    dc = types.ModuleType("execution.dhan_client")
    dc.tick_size_for_price = lambda p: 0.05
    def modify(db, **kw):
        calls.append(kw)
        if raises:
            raise RuntimeError("rejected")
    dc.modify_super_order = modify
    dc.verify_stop_leg = lambda db, **kw: verify
    ex = types.ModuleType("execution"); ex.dhan_client = dc
    sys.modules["execution"], sys.modules["execution.dhan_client"] = ex, dc
    m = types.ModuleType("models")
    class SP:  # column stand-in so `ScalpPosition.status == "OPEN"` evaluates
        status = None
    m.ScalpPosition = SP
    sys.modules["models"] = m
    sd = types.ModuleType("orders.side"); sd.is_short = lambda p: str(p.side).upper() == "SHORT"
    sys.modules["orders.side"] = sd
    import orders
    orders.side = sd
    return calls


def _run(pos, body, **inst):
    calls = _install(**inst)
    eh._reset_for_tests()
    db = DB([pos])
    n = eh.run_event_tighten(db, fetch=lambda s: body, ltp_fn=lambda s: 100.0)
    return n, calls, db


BODY = {"ok": True, "events": [{"polarity": -1, "strength": 0.7, "type": "probe", "published": LATER}]}


def test_pass_moves_the_super_order_stop_stamps_and_commits():
    p = Pos()
    n, calls, db = _run(p, BODY)
    assert n == 1 and p.stop_price == 99.0 and p.stop_moved_to_breakeven and db.commits == 1
    assert calls == [{"order_id": "SO1", "order_leg": "STOP_LOSS_LEG", "stop_loss_price": 99.0}]
    assert '"event_tighten"' in p.flow_json and eh._seen(p.flow_json) == LATER
    n2 = eh.run_event_tighten(DB([p]), fetch=lambda s: BODY, ltp_fn=lambda s: 100.0)   # same event again
    assert n2 == 0


def test_unconfirmed_or_rejected_modify_leaves_the_stored_stop():
    p = Pos(); n, _, db = _run(p, BODY, verify=False)
    assert n == 0 and p.stop_price == 98.0 and db.commits == 0
    p = Pos(); n, _, db = _run(p, BODY, raises=True)
    assert n == 0 and p.stop_price == 98.0


def test_plain_short_cnc_carry_and_feed_down_are_untouched():
    p = Pos(side="SHORT", stop_price=102.0, dhan_super_order_id=None, plain_stop_order_id="P1")
    n, calls, _ = _run(p, {"ok": True, "events": [{"polarity": 1, "strength": 0.9, "published": LATER}]})
    assert n == 0 and calls == [] and p.stop_price == 102.0
    p = Pos(overnight_converted_to_cnc=True); n, calls, _ = _run(p, BODY); assert n == 0 and calls == []
    p = Pos(); n, calls, _ = _run(p, None); assert n == 0 and calls == []


def test_switch_off_does_nothing():
    import config
    old = getattr(config, "EVENT_HELD_TIGHTEN_ENABLED", True)
    config.EVENT_HELD_TIGHTEN_ENABLED = False
    try:
        p = Pos(); n, calls, _ = _run(p, BODY)
        assert n == 0 and calls == []
    finally:
        config.EVENT_HELD_TIGHTEN_ENABLED = old


def test_wiring_by_source_text():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "main.py")).read()
    assert "event_hook.run_event_tighten" in src and src.index("tape_hook.run_tape_exit") < src.index("event_hook.run_event_tighten") < src.index("stop_watchdog.run_stop_watchdog")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        f(); print("ok", f.__name__)
    print(len(fns), "passed")
