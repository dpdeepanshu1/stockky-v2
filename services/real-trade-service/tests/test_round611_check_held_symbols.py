"""Round 611: the dynamic universe's /check warm-up sends the symbols Real Trade holds as `?first=`.

Plain runner: `python tests/test_round611_check_held_symbols.py` (also collected by pytest)."""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import watchlist_engine.dynamic_universe as du  # noqa: E402


class _Col:
    def in_(self, v):
        return self


class _Query:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *a):
        return self

    def all(self):
        return [(r,) for r in self.rows]


class _Db:
    def __init__(self, rows):
        self.rows, self.closed = rows, False

    def query(self, *a):
        return _Query(self.rows)

    def close(self):
        self.closed = True


_ORIG = {}


def _restore():
    """Put back the real `models` / `db` modules (if they were loaded) so other tests in the same pytest run are not affected."""
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
except ImportError:  # plain runner
    pass


def _install(rows, boom=False):
    _restore()
    for k in ("models", "db"):
        _ORIG[k] = sys.modules.get(k)
    m = types.ModuleType("models")
    m.TradePosition = types.SimpleNamespace(symbol=_Col(), status=_Col())
    d = types.ModuleType("db")
    db = _Db(rows)

    def factory():
        if boom:
            raise RuntimeError("no db")
        return lambda: db

    d.get_session_factory = factory
    sys.modules["models"], sys.modules["db"] = m, d
    return db


def test_held_symbols_are_listed_once_and_the_session_is_closed():
    os.environ.pop("DYNAMIC_UNIVERSE_CHECK_HELD_FIRST", None)
    db = _install(["TCS", "INFY", "TCS", None])
    assert du._held_symbols_csv() == "TCS,INFY"
    assert db.closed is True


def test_unreadable_database_gives_empty_text():
    os.environ.pop("DYNAMIC_UNIVERSE_CHECK_HELD_FIRST", None)
    _install([], boom=True)
    assert du._held_symbols_csv() == ""


def test_switch_off():
    _install(["TCS"])
    os.environ["DYNAMIC_UNIVERSE_CHECK_HELD_FIRST"] = "0"
    try:
        assert du._held_symbols_csv() == ""
    finally:
        os.environ.pop("DYNAMIC_UNIVERSE_CHECK_HELD_FIRST", None)


def test_worker_sends_first_param_only_when_there_are_held_names():
    seen = []

    class C:
        def __init__(self, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, params=None, **k):
            seen.append(params)
            return types.SimpleNamespace(raise_for_status=lambda: None)

    real_client, real_csv = du.httpx.Client, du._held_symbols_csv
    du.httpx.Client = C
    try:
        du._held_symbols_csv = lambda: "TCS,INFY"
        du._check_worker(5)
        du._held_symbols_csv = lambda: ""
        du._check_worker(5)
    finally:
        du.httpx.Client, du._held_symbols_csv = real_client, real_csv
    assert seen == [{"first": "TCS,INFY"}, None], seen


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            finally:
                _restore()
            n += 1
            print("ok", name)
    print(n, "passed")
