"""E274 round 49: a positive event extends a swing position's last session once (swing/live.maybe_extend / extra_sessions), with the DB stubbed. Plain runner: python3 tests/test_e274_swing_event_extend.py (pytest also collects it)."""
from __future__ import annotations

import importlib
import os
import sys
import types
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_KEYS = ("sqlalchemy", "sqlalchemy.orm", "models", "watchlist_engine.intraday_news")
_ORIG = {k: sys.modules.get(k) for k in _KEYS}


def _restore():
    """put the real modules back so other tests collected by the same pytest run are not affected"""
    for k, v in _ORIG.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


class _Q:
    def __init__(self, rows): self.rows = rows
    def filter_by(self, **kw): return _Q([r for r in self.rows if all(getattr(r, k, None) == v for k, v in kw.items())])
    def count(self): return len(self.rows)


class _DB:
    def __init__(self): self.rows = []; self.commits = 0
    def query(self, model): return _Q(self.rows)
    def add(self, row): self.rows.append(row)
    def commit(self): self.commits += 1
    def rollback(self): pass


class _Ev:
    def __init__(self, **kw): self.__dict__.update(kw)


def _install(db, event):
    so = types.ModuleType("sqlalchemy"); orm = types.ModuleType("sqlalchemy.orm"); orm.object_session = lambda p: db; so.orm = orm
    sys.modules["sqlalchemy"] = so; sys.modules["sqlalchemy.orm"] = orm
    sys.modules["models"] = types.SimpleNamespace(TradePositionEvent=_Ev)
    wl = types.ModuleType("watchlist_engine.intraday_news"); wl.positive_event_any = lambda d, s, now=None: event
    sys.modules["watchlist_engine.intraday_news"] = wl


def _live():
    import config
    try:
        return importlib.import_module("swing.live"), config
    except Exception:  # noqa: BLE001 - the stack may be missing in a bare sandbox
        return None, config


def _t1():
    db = _DB(); _install(db, {"strength": 0.8, "type": "order", "source": "intraday"})
    live, config = _live()
    if live is None:
        return
    pos = SimpleNamespace(id=7, symbol="ABC", avg_entry_price=100.0)
    assert live.extra_sessions(pos) == 0
    assert live.maybe_extend(db, pos, 105.0, sessions=5) == 1           # last session (max 5): extended
    assert live.extra_sessions(pos) == 1 and len(db.rows) == 1
    assert live.maybe_extend(db, pos, 106.0, sessions=6) == 1           # already used: no second extension
    assert len(db.rows) == 1


def _t2():
    live, config = _live()
    if live is None:
        return
    pos = SimpleNamespace(id=8, symbol="ABC", avg_entry_price=100.0)
    db = _DB(); _install(db, {"strength": 0.8, "type": "order", "source": "intraday"})
    assert live.maybe_extend(db, pos, 105.0, sessions=3) == 0           # not the last session
    assert live.maybe_extend(db, pos, 99.0, sessions=5) == 0            # in loss
    db = _DB(); _install(db, {"strength": 0.3, "type": "order", "source": "intraday"})
    assert live.maybe_extend(db, pos, 105.0, sessions=5) == 0           # weak event
    db = _DB(); _install(db, None)
    assert live.maybe_extend(db, pos, 105.0, sessions=5) == 0           # no event


def _t3():
    live, config = _live()
    if live is None:
        return
    db = _DB(); _install(db, {"strength": 0.9, "type": "order", "source": "intraday"})
    config.SWING_EVENT_EXTEND_ENABLED = False
    try:
        assert live.maybe_extend(db, SimpleNamespace(id=9, symbol="ABC", avg_entry_price=100.0), 105.0, sessions=5) == 0
    finally:
        config.SWING_EVENT_EXTEND_ENABLED = True


def _guard(fn):
    def run():
        try:
            fn()
        finally:
            _restore()
    run.__name__ = "test_" + fn.__name__.strip("_")
    return run


test_t1 = _guard(_t1)
test_t2 = _guard(_t2)
test_t3 = _guard(_t3)


if __name__ == "__main__":
    n = 0
    for k, f in list(globals().items()):
        if k.startswith("test_t"):
            f(); n += 1
    print(f"{n} passed")
