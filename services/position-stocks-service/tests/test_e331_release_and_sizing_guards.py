"""
E331 - position-stocks: capital / symbol-lock release after a plain short closes, finite-number guards, long sizing.

(a) _book_stop_fill / _adopt_cover closed the position row (commit) and THEN released the capital and the symbol lock in two unguarded calls. A
    failure there escaped to the per-position handler; the row is no longer OPEN, so nothing ever looked at it again: capital stayed reserved
    and the symbol stayed locked for the session. Now each release is guarded, queued on failure and retried every guard pass.
(b) plain_short._num let +inf through (`inf > 0`); int(inf) then raised OverflowError in _raw_filled, after the SELL was live.
(c) sizing.compute_quantity (LONG) turned a NaN / infinite price or budget into ONE share through the except fallbacks; it now skips (None), like the
    short path always did. Junk strings and zero keep their documented fallback.

Run from services/position-stocks-service:  python -m pytest tests/test_e331_release_and_sizing_guards.py -q
"""
from __future__ import annotations

import os
import sys
import types
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from orders import sizing  # noqa: E402


@pytest.fixture()
def ps(monkeypatch):
    """orders.plain_short with the ledger / lock / notifier / reconcile / side modules faked (restored after the test)."""
    calls = {"cap": [], "lock": [], "notes": [], "cap_fail": 0, "lock_fail": 0}

    def release_capital(db, position_value, realized_pnl):
        if calls["cap_fail"] > 0:
            calls["cap_fail"] -= 1
            raise RuntimeError("ledger busy")
        calls["cap"].append((position_value, realized_pnl))

    def release(db, sym):
        if calls["lock_fail"] > 0:
            calls["lock_fail"] -= 1
            raise RuntimeError("lock table busy")
        calls["lock"].append(sym)

    cap = types.ModuleType("capital")
    cap.__path__ = []
    led = types.ModuleType("capital.ledger")
    led.release_capital = release_capital
    ssl = types.ModuleType("capital.shared_symbol_lock")
    ssl.release = release
    cap.ledger, cap.shared_symbol_lock = led, ssl
    monkeypatch.setitem(sys.modules, "capital", cap)
    monkeypatch.setitem(sys.modules, "capital.ledger", led)
    monkeypatch.setitem(sys.modules, "capital.shared_symbol_lock", ssl)

    from orders import plain_short
    monkeypatch.setattr(plain_short, "_alert", lambda t: calls["notes"].append(t))
    plain_short.reset_state()
    plain_short._calls = calls
    yield plain_short
    plain_short.reset_state()


class _Db:
    def __init__(self):
        self.rollbacks = 0
        self.commits = 0

    def rollback(self):
        self.rollbacks += 1

    def commit(self):
        self.commits += 1


# ---------------------------- (a) releases ----------------------------

def test_both_releases_succeed(ps):
    assert ps.release_claims(_Db(), "ABC", 1000.0, -12.5) is True
    assert ps._calls["cap"] == [(1000.0, -12.5)] and ps._calls["lock"] == ["ABC"]
    assert ps.stats()["releases_pending"] == 0


def test_failed_capital_release_is_queued_and_the_lock_still_goes(ps):
    ps._calls["cap_fail"] = 1
    db = _Db()
    assert ps.release_claims(db, "ABC", 1000.0, 5.0) is False
    assert ps._calls["lock"] == ["ABC"], "the lock release must not depend on the capital release"
    assert ps.stats()["releases_pending"] == 1 and db.rollbacks == 1
    assert any("NOT released" in n for n in ps._calls["notes"])
    assert ps.retry_pending_releases(db) == 1
    assert ps._calls["cap"] == [(1000.0, 5.0)] and ps.stats()["releases_pending"] == 0
    assert ps._calls["lock"] == ["ABC"], "the lock is not released twice"


def test_failed_lock_release_is_retried_without_a_second_capital_release(ps):
    ps._calls["lock_fail"] = 2
    db = _Db()
    ps.release_claims(db, "XYZ", 500.0, 0.0)
    assert ps._calls["cap"] == [(500.0, 0.0)]
    assert ps.retry_pending_releases(db) == 0      # still failing: second failure
    assert ps.stats()["releases_pending"] == 1
    assert ps.retry_pending_releases(db) == 1
    assert ps._calls["lock"] == ["XYZ"] and ps._calls["cap"] == [(500.0, 0.0)]


def test_retry_gives_up_loudly(ps, monkeypatch):
    monkeypatch.setattr(ps, "RELEASE_RETRY_LIMIT", 3)
    ps._calls["cap_fail"] = 99
    db = _Db()
    ps.release_claims(db, "ABC", 1.0, 0.0)
    for _ in range(3):
        ps.retry_pending_releases(db)
    assert ps.stats()["releases_pending"] == 0
    assert any("still held" in n for n in ps._calls["notes"])


def test_book_stop_fill_releases_through_the_guard(ps, monkeypatch):
    rc = types.ModuleType("orders.reconcile")
    rc.note_stop_hit = lambda *a, **k: None
    sd = types.ModuleType("orders.side")
    sd.pnl = lambda pos, e, x, q: round((e - x) * q, 2)
    sd.pnl_pct = lambda pos, e, x: round((e - x) / e * 100, 4)
    monkeypatch.setitem(sys.modules, "orders.reconcile", rc)
    monkeypatch.setitem(sys.modules, "orders.side", sd)
    import orders
    monkeypatch.setattr(orders, "reconcile", rc, raising=False)
    monkeypatch.setattr(orders, "side", sd, raising=False)
    ps._calls["cap_fail"] = 1
    db = _Db()
    pos = SimpleNamespace(id=7, symbol="ABC", stop_price=102.0, entry_price=100.0, quantity=10, capital_risked=1000.0, plain_stop_order_id="S1",
                          status="OPEN", exit_price=None, realized_pnl=None, realized_pnl_pct=None, dhan_exit_order_id=None, error_message=None, closed_at=None)
    ps._book_stop_fill(db, pos, {"averageTradedPrice": 102.0})     # must not raise although the ledger failed
    assert pos.status == "STOP_HIT" and pos.realized_pnl == -20.0 and db.commits == 1
    assert ps._calls["lock"] == ["ABC"] and ps.stats()["releases_pending"] == 1
    ps.retry_pending_releases(db)
    assert ps._calls["cap"] == [(1000.0, -20.0)]


def test_guard_pass_retries_queued_releases(ps, monkeypatch):
    ps._calls["cap_fail"] = 1
    ps.release_claims(_Db(), "ABC", 10.0, 0.0)
    monkeypatch.setattr(ps, "run_orphan_check", lambda db, now: 0)
    monkeypatch.setattr(ps, "_plain_shorts", lambda db: [])
    assert ps.run_plain_short_guard(_Db(), now_mono=1.0) == 0
    assert ps._calls["cap"] == [(10.0, 0.0)] and ps.stats()["releases_pending"] == 0


# ---------------------------- (b) finite numbers ----------------------------

@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan"), 0, -3, None, "x"])
def test_num_refuses_non_finite_and_non_positive(ps, bad):
    assert ps._num(bad) is None


def test_num_accepts_a_normal_value_and_filled_qty_survives_inf(ps):
    assert ps._num("12.5") == 12.5
    row = {"filledQty": float("inf"), "orderStatus": "TRADED"}
    assert ps._raw_filled(row) == 0               # was OverflowError
    assert ps._filled_qty(row, 10) == 10          # the filled status still decides


# ---------------------------- (c) long sizing ----------------------------

@pytest.mark.parametrize("price,value", [(float("nan"), 1000.0), (float("inf"), 1000.0), (100.0, float("nan")), (100.0, float("inf")), (float("-inf"), 1000.0)])
def test_long_with_non_finite_inputs_is_skipped(price, value):
    assert sizing.compute_quantity(value, price) is None
    assert sizing.compute_quantity(value, price, "LONG") is None


def test_long_documented_fallbacks_are_unchanged():
    assert sizing.compute_quantity(1000.0, 0.0) == 1
    assert sizing.compute_quantity(0.0, 50.0) == 1
    assert sizing.compute_quantity("x", 50.0) == 1
    assert sizing.compute_quantity(1900.0, 1000.0) == 2 or not getattr(config, "SIZING_ROUNDING_ENABLED", True)


def test_short_path_still_skips_non_finite():
    assert sizing.compute_quantity(1000.0, float("nan"), "SHORT") is None
    assert sizing.compute_quantity(float("inf"), 100.0, "SHORT") is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
