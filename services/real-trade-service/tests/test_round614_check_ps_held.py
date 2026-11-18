"""Round 614: the /check warm-up also sends the names Position Stocks holds (shared symbol-lock table), after Real Trade's own.

Plain runner: `python tests/test_round614_check_ps_held.py` (also collected by pytest)."""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import watchlist_engine.dynamic_universe as du  # noqa: E402


class _Col:
    def __init__(self, name):
        self.name = name

    def in_(self, v):
        return self

    def __eq__(self, other):          # models.SharedSymbolLock.held_by_service == "..."
        return self

    __hash__ = object.__hash__


class _Query:
    def __init__(self, rows, boom):
        self.rows, self.boom = rows, boom

    def filter(self, *a):
        return self

    def all(self):
        if self.boom:
            raise RuntimeError("table missing")
        return [(r,) for r in self.rows]


class _Db:
    def __init__(self, rt, ps, ps_boom=False):
        self.rt, self.ps, self.ps_boom, self.closed, self.rolled = rt, ps, ps_boom, False, False

    def query(self, col):
        return _Query(self.ps, self.ps_boom) if col.name == "lock_symbol" else _Query(self.rt, False)

    def rollback(self):
        self.rolled = True

    def close(self):
        self.closed = True


_ORIG = {}


def _restore():
    for k in ("models", "db"):
        if k in _ORIG:
            if _ORIG[k] is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = _ORIG[k]
    _ORIG.clear()


try:
    import pytest

    @pytest.fixture(autouse=True)
    def _auto_restore():
        yield
        _restore()
        for k in ("DYNAMIC_UNIVERSE_CHECK_PS_HELD", "DYNAMIC_UNIVERSE_CHECK_HELD_FIRST"):
            os.environ.pop(k, None)
except ImportError:
    pass


def _install(rt, ps, ps_boom=False):
    _restore()
    for k in ("models", "db"):
        _ORIG[k] = sys.modules.get(k)
    m = types.ModuleType("models")
    m.TradePosition = types.SimpleNamespace(symbol=_Col("tp_symbol"), status=_Col("tp_status"))
    m.SharedSymbolLock = types.SimpleNamespace(symbol=_Col("lock_symbol"), held_by_service=_Col("lock_service"))
    d = types.ModuleType("db")
    db = _Db(rt, ps, ps_boom)
    d.get_session_factory = lambda: (lambda: db)
    sys.modules["models"], sys.modules["db"] = m, d
    os.environ.pop("DYNAMIC_UNIVERSE_CHECK_PS_HELD", None)
    os.environ.pop("DYNAMIC_UNIVERSE_CHECK_HELD_FIRST", None)
    return db


def test_position_stocks_names_follow_real_trade_names_without_repeats():
    db = _install(["TCS", "INFY"], ["INFY", "RELIANCE", None, "SBIN"])
    assert du._held_symbols_csv() == "TCS,INFY,RELIANCE,SBIN"
    assert db.closed is True


def test_only_position_stocks_holds():
    _install([], ["SBIN"])
    assert du._held_symbols_csv() == "SBIN"


def test_failed_position_stocks_read_still_sends_real_trade_names():
    db = _install(["TCS"], [], ps_boom=True)
    assert du._held_symbols_csv() == "TCS"
    assert db.rolled is True and db.closed is True


def test_position_stocks_switch_off():
    _install(["TCS"], ["SBIN"])
    os.environ["DYNAMIC_UNIVERSE_CHECK_PS_HELD"] = "0"
    assert du._held_symbols_csv() == "TCS"


def test_main_switch_still_turns_everything_off():
    _install(["TCS"], ["SBIN"])
    os.environ["DYNAMIC_UNIVERSE_CHECK_HELD_FIRST"] = "0"
    assert du._held_symbols_csv() == ""


def test_list_is_capped_at_60():
    _install(["R%d" % i for i in range(50)], ["P%d" % i for i in range(30)])
    out = du._held_symbols_csv().split(",")
    assert len(out) == 60 and out[0] == "R0" and out[49] == "R49" and out[50] == "P0"


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            finally:
                _restore()
                os.environ.pop("DYNAMIC_UNIVERSE_CHECK_PS_HELD", None)
                os.environ.pop("DYNAMIC_UNIVERSE_CHECK_HELD_FIRST", None)
            n += 1
            print("ok", name)
    print(n, "passed")
