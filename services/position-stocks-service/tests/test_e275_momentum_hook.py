"""E275: orders/momentum_hook.py with a fake DB, broker and eod_squareoff (no sqlalchemy / network needed).

The hook is loaded under a private module name with the heavy imports stubbed only while it loads, so a real `models` / `sqlalchemy` in sys.modules (the VM
test run) is never replaced for other tests."""
import importlib.util
import pathlib
import sys
import types
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
T_NOW = datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)


class FakeQuery:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *a, **k):
        return self

    def all(self):
        return list(self.rows)


class FakeDB:
    def __init__(self, rows):
        self.rows = rows
        self.commits = 0
        self.rollbacks = 0

    def query(self, *a, **k):
        return FakeQuery(self.rows)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class Col:
    def __eq__(self, other):
        return True


class Rejected(Exception):
    pass


@pytest.fixture()
def hook(monkeypatch):
    import config
    monkeypatch.setattr(config, "MOMENTUM_EXIT_MODE", "live", raising=False)
    monkeypatch.setattr(config, "MOMENTUM_EXIT_MIN_INTERVAL_S", 0.0, raising=False)
    calls = {"modify": [], "closed": [], "stamped": []}
    dhan = types.ModuleType("execution.dhan_client")
    dhan.tick_size_for_price = lambda p: 0.05
    dhan.modify_super_order = lambda db, **k: calls["modify"].append(k)
    dhan.verify_stop_leg = lambda db, **k: True
    execution = types.ModuleType("execution")
    execution.dhan_client = dhan
    models = types.ModuleType("models")
    models.ScalpPosition = SimpleNamespace(status=Col())
    sa = types.ModuleType("sqlalchemy")
    sa_orm = types.ModuleType("sqlalchemy.orm")
    sa_orm.Session = object
    sa.orm = sa_orm
    eod = types.ModuleType("orders.eod_squareoff")
    eod.ManualCloseRejected = Rejected
    eod._stamp_exit_rule = lambda pos, rule: calls["stamped"].append(rule)
    eod.close_position_now = lambda db, pos, exit_reason="MANUAL_EXIT": calls["closed"].append((pos.id, exit_reason))
    import orders
    monkeypatch.setattr(orders, "eod_squareoff", eod, raising=False)
    monkeypatch.setitem(sys.modules, "orders.eod_squareoff", eod)
    for name, mod in (("execution", execution), ("execution.dhan_client", dhan), ("models", models), ("sqlalchemy", sa), ("sqlalchemy.orm", sa_orm)):
        monkeypatch.setitem(sys.modules, name, mod)
    spec = importlib.util.spec_from_file_location("momentum_hook_under_test", ROOT / "orders" / "momentum_hook.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._last_run.clear()
    mod.calls = calls
    return mod


def make_pos(**k):
    base = dict(id=1, symbol="AAA", side="LONG", entry_price=100.0, stop_price=99.0, target_price=102.0, quantity=10, status="OPEN",
                opened_at=T_NOW - timedelta(minutes=9), dhan_super_order_id="SO1", plain_stop_order_id=None, overnight_converted_to_cnc=False,
                flow_json=None, stop_moved_to_breakeven=False)
    base.update(k)
    return SimpleNamespace(**base)


def bars_for(pos_open_ts, n=9, flip=False):
    out = []
    for i in range(n):
        p = 100.0 + 0.1 * i
        o, h, l, c = p, p + 0.08, p - 0.02, p + 0.06
        if flip:
            o, h, l, c = 200 - o, 200 - l, 200 - h, 200 - c
        out.append((pos_open_ts + 60.0 * (i - 1), o, h, l, c, 1000.0))
    return out


def wire(hook, monkeypatch, *, pos, ltp=100.8, tape=None, fading=True, flip=False):
    monkeypatch.setattr(hook, "_ltp", lambda s: ltp)
    monkeypatch.setattr(hook, "_trade_bars", lambda s, ts: bars_for(ts, flip=flip) + [(ts + 60.0 * 9, ltp, ltp, ltp, ltp, 5.0)])
    monkeypatch.setattr(hook, "_tape_verdict", lambda s, side: tape)
    monkeypatch.setattr(hook.exit_adaptive, "is_fading", lambda *a, **k: fading)
    monkeypatch.setattr(hook, "datetime", SimpleNamespace(now=lambda tz=None: T_NOW, fromtimestamp=datetime.fromtimestamp))
    return FakeDB([pos])


TAPE_TIGHTEN = {"available": True, "reliable": True, "checks": {"delta": "against"}, "hvn_above": 101.2}
TAPE_BOOK = {"available": True, "reliable": True, "checks": {"delta": "against"}, "hvn_above": 100.85}


def test_tighten_moves_both_legs_and_stamps(hook, monkeypatch):
    pos = make_pos()
    db = wire(hook, monkeypatch, pos=pos, tape=TAPE_TIGHTEN)
    assert hook.run_momentum_exit(db) == 1
    legs = [m["order_leg"] for m in hook.calls["modify"]]
    assert legs == ["STOP_LOSS_LEG", "TARGET_LEG"] and 99.0 < pos.stop_price < 100.8 and pos.target_price == 101.15
    assert '"momentum"' in pos.flow_json and "TIGHTEN" in pos.flow_json and db.commits == 1 and not hook.calls["closed"]


def test_book_closes_in_full_with_the_momentum_status(hook, monkeypatch):
    pos = make_pos()
    db = wire(hook, monkeypatch, pos=pos, tape=TAPE_BOOK)
    assert hook.run_momentum_exit(db) == 1
    assert hook.calls["closed"] == [(1, "MOMENTUM_EXIT")] and hook.calls["stamped"] == ["momentum"] and not hook.calls["modify"]
    assert '"BOOK"' in pos.flow_json and '"FULL"' in pos.flow_json


def test_old_log_value_acts_like_live(hook, monkeypatch):
    import config
    monkeypatch.setattr(config, "MOMENTUM_EXIT_MODE", "log", raising=False)      # E276 round 66: a stale "log" is live, never a recorder
    assert hook.mode() == "live"
    pos = make_pos()
    db = wire(hook, monkeypatch, pos=pos, tape=TAPE_BOOK)
    assert hook.run_momentum_exit(db) == 1 and hook.calls["closed"]


def test_off_mode_does_nothing(hook, monkeypatch):
    import config
    monkeypatch.setattr(config, "MOMENTUM_EXIT_MODE", "off", raising=False)
    db = wire(hook, monkeypatch, pos=make_pos(), tape=TAPE_BOOK)
    assert hook.run_momentum_exit(db) == 0 and not hook.calls["closed"]


def test_short_book_and_plain_short_never_moves_its_broker_stop(hook, monkeypatch):
    pos = make_pos(id=2, side="SHORT", stop_price=101.0, target_price=98.0, dhan_super_order_id=None, plain_stop_order_id="PS1")
    tape = {"available": True, "reliable": True, "checks": {"delta": "against"}, "hvn_below": 99.2}
    db = wire(hook, monkeypatch, pos=pos, ltp=99.2, tape=tape, flip=True)
    hook.run_momentum_exit(db)
    assert pos.stop_price == 101.0 and not hook.calls["modify"]          # E114: broker stop of a plain short is fixed by design
    pos2 = make_pos(id=3, side="SHORT", stop_price=101.0, target_price=98.0, dhan_super_order_id=None, plain_stop_order_id="PS1")
    tape2 = {"available": True, "reliable": True, "checks": {"delta": "against"}, "hvn_below": 98.8}
    db2 = wire(hook, monkeypatch, pos=pos2, ltp=99.2, tape=tape2, flip=True)
    hook.run_momentum_exit(db2)
    assert pos2.stop_price == 101.0 and pos2.target_price == 98.85       # only the software target moved


def test_skips_overnight_no_order_id_no_bars_and_no_price(hook, monkeypatch):
    for kw in ({"overnight_converted_to_cnc": True}, {"dhan_super_order_id": None}):
        db = wire(hook, monkeypatch, pos=make_pos(**kw), tape=TAPE_BOOK)
        assert hook.run_momentum_exit(db) == 0
    db = wire(hook, monkeypatch, pos=make_pos(), tape=TAPE_BOOK)
    monkeypatch.setattr(hook, "_trade_bars", lambda s, ts: [])
    assert hook.run_momentum_exit(db) == 0
    monkeypatch.setattr(hook, "_ltp", lambda s: None)
    assert hook.run_momentum_exit(db) == 0 and not hook.calls["closed"]


def test_a_rejected_close_is_logged_not_raised_and_throttle_holds(hook, monkeypatch):
    def boom(db, pos, exit_reason="X"):
        raise Rejected("already closed")
    monkeypatch.setattr(sys.modules["orders.eod_squareoff"], "close_position_now", boom)
    pos = make_pos()
    db = wire(hook, monkeypatch, pos=pos, tape=TAPE_BOOK)
    assert hook.run_momentum_exit(db) == 0 and db.rollbacks == 1
    import config
    monkeypatch.setattr(config, "MOMENTUM_EXIT_MIN_INTERVAL_S", 3600.0, raising=False)
    hook._last_run.clear()
    hook._mark(("ps-mom", 1))
    assert hook.run_momentum_exit(db) == 0 and db.rollbacks == 1          # throttled: not even decided again


def test_unexpected_error_in_one_position_does_not_stop_the_pass(hook, monkeypatch):
    bad = make_pos(id=7, side="WEIRD")
    good = make_pos(id=8)
    db = wire(hook, monkeypatch, pos=good, tape=TAPE_BOOK)
    db.rows = [bad, good]
    assert hook.run_momentum_exit(db) == 1 and hook.calls["closed"] == [(8, "MOMENTUM_EXIT")] and hook.stats()["errors"] == 1
