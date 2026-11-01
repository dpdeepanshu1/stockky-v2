"""task 4: execution/lock_sweep.py (plain-function tests with fakes; no database needed).
Run from services/real-trade-service:  python3 tests/test_task4_lock_sweep.py   or   python3 -m pytest tests/test_task4_lock_sweep.py -q"""
from __future__ import annotations

import os
import sys
import types
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

NOW = datetime(2026, 10, 31, 10, 0, tzinfo=timezone.utc)

# E277: _load() below swaps config / models / execution.* for stand-ins. Put the real ones back after EVERY test, otherwise the next test's autouse fixtures
# (conftest.py: monkeypatch.setattr on config) run against the stand-in and error at setup, and so does every test file after this one.
_TOUCHED = ("config", "models", "notifier", "execution.shared_symbol_lock", "execution.lock_sweep", "execution.dhan_client")
_GONE = object()
try:
    import pytest
except ImportError:                      # plain runner: python3 tests/test_task4_lock_sweep.py
    pytest = None

if pytest is not None:
    @pytest.fixture(autouse=True)
    def _restore_modules():
        saved = {k: sys.modules.get(k, _GONE) for k in _TOUCHED}
        pkg = sys.modules.get("execution")
        attrs = {n: (pkg.__dict__.get(n, _GONE) if pkg is not None else _GONE) for n in ("shared_symbol_lock", "lock_sweep", "dhan_client")}
        yield
        for k, v in saved.items():
            if v is _GONE:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        pkg = sys.modules.get("execution")
        if pkg is not None:
            for n, v in attrs.items():
                if v is _GONE:
                    pkg.__dict__.pop(n, None)
                else:
                    setattr(pkg, n, v)


def _load(sweep_on=True):
    for m in ("config", "models", "execution.shared_symbol_lock", "notifier"):
        sys.modules.pop(m, None)
    sys.modules["config"] = types.SimpleNamespace(LOCK_SWEEP=sweep_on, LOCK_SWEEP_MIN_AGE_MIN=15, LOCK_SWEEP_INTERVAL_MIN=10)

    class _Col:
        def __getattr__(self, n):
            return self
        def in_(self, *a):
            return self
        def __eq__(self, o):
            return self
        def __gt__(self, o):
            return self
        def __or__(self, o):
            return self
    mk = lambda: type("T", (), {"status": _Col(), "qty_open": _Col(), "kind": _Col(), "side": _Col()})  # noqa: E731
    sys.modules["models"] = types.SimpleNamespace(TradePosition=mk(), SwingTrade=mk(), TradeOrder=mk(), SharedSymbolLock=object)
    released = []
    sys.modules["execution.shared_symbol_lock"] = types.SimpleNamespace(release=lambda db, s: released.append(s))
    import importlib
    import execution
    sys.modules.pop("execution.lock_sweep", None)
    ls = importlib.import_module("execution.lock_sweep")
    ls.reset()
    execution.shared_symbol_lock = sys.modules["execution.shared_symbol_lock"]
    return ls, released


class _Lock:
    def __init__(self, symbol, age_min=60, service="real-trade-service", mode="REAL"):
        self.symbol, self.held_by_service, self.held_by_mode = symbol, service, mode
        self.claimed_at = NOW - timedelta(minutes=age_min)


class _Q:
    def __init__(self, rows):
        self.rows = rows
    def filter_by(self, **kw):
        return _Q([r for r in self.rows if all(getattr(r, k) == v for k, v in kw.items())])
    def all(self):
        return self.rows


class _DB:
    def __init__(self, locks):
        self.locks = locks
    def query(self, m):
        return _Q(self.locks)
    def rollback(self):
        pass


def test_releases_only_unbacked_old_real_locks():
    ls, rel = _load()
    ls._backed_in_db = lambda db: {"BACKED"}
    db = _DB([_Lock("STALE"), _Lock("BACKED"), _Lock("NEW", age_min=5), _Lock("PSLOCK", service="position-stocks-service"), _Lock("HELD")])
    out = ls.sweep(db, [{"tradingSymbol": "HELD-EQ", "totalQty": 3}], [], now=NOW)
    assert out == ["STALE"] and rel == ["STALE"], (out, rel)


def test_zero_quantity_rows_do_not_back_a_lock():
    ls, rel = _load()
    ls._backed_in_db = lambda db: set()
    out = ls.sweep(_DB([_Lock("ZERO")]), [{"tradingSymbol": "ZERO", "totalQty": 0}], [{"tradingSymbol": "ZERO", "netQty": 0}], now=NOW)
    assert out == ["ZERO"]


def test_unreadable_input_releases_nothing():
    ls, rel = _load()
    ls._backed_in_db = lambda db: set()
    out = ls.sweep(_DB([_Lock("X")]), [{"tradingSymbol": "A", "totalQty": "junk"}], [], now=NOW)
    assert out == [] and rel == []
    def boom(db):
        raise RuntimeError("db down")
    ls._backed_in_db = boom
    assert ls.sweep(_DB([_Lock("X")]), [], [], now=NOW) == [] and rel == []


def test_switch_off_and_throttle_and_demo():
    ls, rel = _load(sweep_on=False)
    assert ls.maybe_sweep(_DB([]), "REAL") == []
    ls, rel = _load()
    assert ls.maybe_sweep(_DB([]), "DEMO") == []
    calls = []
    sys.modules["execution.dhan_client"] = types.SimpleNamespace(get_holdings=lambda db: calls.append("h") or [], get_positions=lambda db: calls.append("p") or [])
    import execution
    execution.dhan_client = sys.modules["execution.dhan_client"]
    ls._backed_in_db = lambda db: set()
    ls.maybe_sweep(_DB([]), "REAL", now_mono=1000.0)
    ls.maybe_sweep(_DB([]), "REAL", now_mono=1100.0)     # inside 10 min: skipped
    assert calls == ["h", "p"]
    ls.maybe_sweep(_DB([]), "REAL", now_mono=1700.0)
    assert calls == ["h", "p", "h", "p"]


def test_broker_read_failure_releases_nothing():
    ls, rel = _load()
    def bad(db):
        raise RuntimeError("dhan 500")
    sys.modules["execution.dhan_client"] = types.SimpleNamespace(get_holdings=bad, get_positions=lambda db: [])
    import execution
    execution.dhan_client = sys.modules["execution.dhan_client"]
    ls._backed_in_db = lambda db: set()
    assert ls.maybe_sweep(_DB([_Lock("X")]), "REAL", now_mono=5.0) == [] and rel == []


def test_wired_into_cycle_and_catalog():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert "maybe_sweep(db, mode)" in open(os.path.join(root, "cycle_runner.py"), encoding="utf-8").read()
    assert 'Flag("LOCK_SWEEP"' in open(os.path.join(root, "control", "catalog.py"), encoding="utf-8").read()


if __name__ == "__main__":
    n = 0
    for k, f in sorted(globals().items()):
        if k.startswith("test_") and callable(f):
            f(); n += 1; print("ok", k)
    print(n, "passed")
