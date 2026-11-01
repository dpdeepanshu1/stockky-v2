"""E285 round 104: Real Trade shows WHY each trade was taken (the setup stamp in the ENTER decision's flow_json) on the Positions cards and the History rows.

Backend: setup_stamp.public (screen shape), the trade record's `setup`, and the entry stamp joined onto the open-positions and closed-history routes through
_tape_fields_for_positions. Pure tests with stand-in rows; no database."""
from __future__ import annotations

import ast
import json
import logging
import os
import pathlib
import sys
import types
from datetime import datetime, timedelta
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest  # noqa: F401  (conftest needs it)

from entry_engine import setup_stamp as ss

MAIN = pathlib.Path(__file__).resolve().parents[1] / "main.py"
T0 = datetime(2026, 11, 2, 4, 0, 0)


def _flow(setup=None, **extra):
    d = dict(extra)
    if setup is not None:
        d["setup"] = setup
    return json.dumps(d)


STAMP = ss.build(trigger="BUY NOW", confirmations=["above_vwap", "book_bid_heavy"], penalties={"vix": 0.8}, size_mult=0.8, now=datetime(2026, 11, 2, 9, 35), side="LONG")


# -- setup_stamp.public ---------------------------------------------------------------------------------------------------------------------------------

def test_public_returns_the_screen_shape():
    out = ss.public(_flow(STAMP))
    assert set(out) == {"id", "trigger", "confirmations", "penalties", "size_mult", "slot", "side"}
    assert out["id"] == ss.LEGACY_ID and out["trigger"] == "BUY NOW" and out["slot"] == "0930" and out["side"] == "LONG"
    assert out["confirmations"] == ["above_vwap", "book_bid_heavy"] and out["penalties"] == {"vix": 0.8} and out["size_mult"] == 0.8


def test_public_is_none_for_no_stamp_or_junk():
    for bad in (None, "", "not json", "[]", "{}", _flow(None, tape={"against": 1}), _flow({"trigger": "x"}), 12, object()):
        assert ss.public(bad) is None


def test_public_fills_blanks_and_caps_confirmations():
    out = ss.public(_flow({"id": "S2_RS_BREAKOUT", "confirmations": [f"c{i}" for i in range(15)], "penalties": "bad"}))
    assert out["id"] == "S2_RS_BREAKOUT" and len(out["confirmations"]) == 10 and out["penalties"] == {}
    assert out["trigger"] == "" and out["slot"] == "later" and out["side"] == "LONG" and out["size_mult"] is None


def test_public_accepts_a_dict_flow():
    assert ss.public({"setup": STAMP})["id"] == ss.LEGACY_ID


# -- the trade record -----------------------------------------------------------------------------------------------------------------------------------

def _tr():
    try:
        import trade_records as tr
        return tr
    except Exception:  # sqlalchemy missing in a bare sandbox: CI has it
        return None


def _rec(flow_json):
    tr = _tr()
    if tr is None:
        return None
    from datetime import timezone
    u = lambda h, m=0: datetime(2026, 10, 9, h, m, tzinfo=timezone.utc)   # noqa: E731
    p = NS(id=1, mode="REAL", symbol="AAA", status="CLOSED", avg_entry_price=100.0, realized_pnl=50.0, opened_at=u(4), closed_at=u(5), broker_imported=False,
           watchlist_entry_id=None, source_tab="watchlist", entry_decision_label="BUY NOW", entry_conviction_score=70.0, is_regime_override=False,
           net_realized_pnl=None, realized_cost_estimate=None)
    o_buy = NS(id=1, side="BUY", symbol="AAA", mode="REAL", status="FILLED", exit_reason=None, decision_id=7, watchlist_entry_id=None, execution_source="AUTO",
               created_at=u(4), updated_at=u(4))
    o_sell = NS(id=2, side="SELL", symbol="AAA", mode="REAL", status="FILLED", exit_reason="STOP_LOSS", decision_id=None, watchlist_entry_id=None,
                execution_source="AUTO", created_at=u(5), updated_at=u(5))
    fills = [NS(order_id=1, qty=2, price=100.0, filled_at=u(4)), NS(order_id=2, qty=2, price=101.0, filled_at=u(5))]
    dec = NS(id=7, flow_json=flow_json, proposed_price=100.0, proposed_stop=99.0, initial_stop=None, stop_basis=None, symbol="AAA", mode="REAL")
    return tr.build_records([p], [o_buy, o_sell], fills, [dec], [], [], [])


def test_record_carries_the_setup_of_its_entry_decision():
    recs = _rec(_flow(STAMP))
    if recs is None:
        return
    assert len(recs) == 1 and recs[0]["setup"]["id"] == ss.LEGACY_ID and recs[0]["setup"]["slot"] == "0930"


def test_record_setup_is_none_without_a_stamp():
    recs = _rec(_flow(None, tape={"against": 0}))
    if recs is None:
        return
    assert len(recs) == 1 and recs[0]["setup"] is None


# -- the routes: the stamp joined from the ENTER decision -----------------------------------------------------------------------------------------------

class _Col:
    def __getattr__(self, n): return self
    def __call__(self, *a, **k): return self
    def __eq__(self, o): return self
    def __ge__(self, o): return self
    def __le__(self, o): return self
    def __hash__(self): return 1


class _Q:
    def __init__(self, results): self._r = results
    def __getattr__(self, n): return lambda *a, **k: self
    def all(self): return self._r


class _DB:
    def __init__(self, *result_sets): self._qs = list(result_sets); self.queries = 0
    def query(self, *a):
        self.queries += 1
        return _Q(self._qs.pop(0))


class _M:
    TradePositionEvent = _Col()
    TradeDecision = _Col()


class _P:
    def __init__(self, id, symbol, opened_at, mode="REAL"): self.id, self.symbol, self.opened_at, self.mode = id, symbol, opened_at, mode


class _D:
    def __init__(self, symbol, created_at, flow_json, mode="REAL"): self.symbol, self.created_at, self.flow_json, self.mode = symbol, created_at, flow_json, mode


def _fn():
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    f = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_tape_fields_for_positions")
    fake_sa = types.ModuleType("sqlalchemy")
    fake_sa.func = types.SimpleNamespace(count=lambda *a: _Col())
    ns = {"models": _M, "timedelta": timedelta, "logger": logging.getLogger("t")}
    exec(compile(ast.Module([f], []), "main.py", "exec"), ns)
    fn = ns["_tape_fields_for_positions"]

    def run(db, rows, **kw):
        old = sys.modules.get("sqlalchemy", None)      # the function imports sqlalchemy.func at call time: swap it only for the call
        sys.modules["sqlalchemy"] = fake_sa
        try:
            return fn(db, rows, **kw)
        finally:
            if old is None:
                sys.modules.pop("sqlalchemy", None)
            else:
                sys.modules["sqlalchemy"] = old
    return run


def test_open_positions_get_their_entry_stamp_with_one_query():
    f = _fn()
    rows = [_P(1, "AAA", T0), _P(2, "BBB", T0), _P(3, "CCC", T0)]
    decs = [_D("AAA", T0 - timedelta(seconds=20), _flow(STAMP, tape={"against": 1})),
            _D("BBB", T0 - timedelta(hours=3), _flow(STAMP)),                    # too old to be this position's entry
            _D("CCC", T0 - timedelta(seconds=5), _flow(None, tape={"against": 0}))]   # an entry with no stamp
    db = _DB(decs)
    out = f(db, rows, with_tightens=False)
    assert db.queries == 1                                    # the open-positions poll skips the TAPE_TIGHTEN count
    assert out[1]["setup"]["id"] == ss.LEGACY_ID and out[1]["tape"] == {"against": 1} and "tightens" not in out[1]
    assert "setup" not in out.get(2, {}) and "setup" not in out.get(3, {})


def test_history_still_counts_tightens_and_adds_the_stamp():
    f = _fn()
    rows = [_P(1, "AAA", T0)]
    out = f(_DB([(1, 2)], [_D("AAA", T0 - timedelta(seconds=20), _flow(STAMP))]), rows)
    assert out[1]["tightens"] == 2 and out[1]["setup"]["trigger"] == "BUY NOW"


def test_the_stamp_lookup_is_fail_open():
    f = _fn()
    class Boom:
        def query(self, *a): raise RuntimeError("db down")
    assert f(Boom(), [_P(1, "AAA", T0)], with_tightens=False) == {}


def test_both_routes_send_setup_and_the_poll_skips_the_tighten_count():
    src = MAIN.read_text(encoding="utf-8")
    a = src.index('@app.get("/positions/{mode}")')
    b = src.index("# 2026-09-12 fix (audit finding", a)
    assert '"setup": _setup_by_pos.get(p.id, {}).get("setup")' in src[a:b] and "with_tightens=False" in src[a:b]
    c = src.index('@app.get("/positions/{mode}/history")')
    d = src.index('@app.get("/positions/{mode}/breakdown")', c)
    assert '"setup": _tape_by_pos.get(p.id, {}).get("setup")' in src[c:d]


# -- end to end through the real routes (in-memory SQLite) ----------------------------------------------------------------------------------------------

def _http_env():
    os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
    try:
        from fastapi.testclient import TestClient
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        import main
        import models
    except Exception:  # sqlalchemy / fastapi missing in a bare sandbox: CI has them
        return None
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(eng)
    return TestClient, main, models, sessionmaker(bind=eng)()


def _naive_now(minutes_ago):
    from datetime import timezone
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).replace(tzinfo=None)


def test_routes_return_the_entry_reason_end_to_end(monkeypatch):
    env = _http_env()
    if env is None:
        return
    TestClient, main, models, db = env
    opened = _naive_now(120)
    # a closed trade whose ENTER decision carries a stamp, an open one with a stamp, an open one without
    for sym, status, flow in (("AAA", "CLOSED", _flow(STAMP)), ("BBB", "OPEN", _flow(STAMP)), ("CCC", "OPEN", _flow(None, tape={"against": 0}))):
        db.add(models.TradeDecision(mode="DEMO", symbol=sym, decision_type="ENTRY", action="ENTER", flow_json=flow, created_at=opened - timedelta(seconds=30)))
        db.add(models.TradePosition(mode="DEMO", symbol=sym, status=status, qty_open=0 if status == "CLOSED" else 5, avg_entry_price=100.0, realized_pnl=10.0 if status == "CLOSED" else 0.0,
                                    opened_at=opened, closed_at=_naive_now(60) if status == "CLOSED" else None, source_tab="watchlist"))
    # an imported holding: no ENTER decision at all
    db.add(models.TradePosition(mode="DEMO", symbol="DDD", status="OPEN", qty_open=3, avg_entry_price=50.0, opened_at=opened, broker_imported=True))
    db.commit()

    async def _no_prices(symbols):
        return {}

    async def _no_heal(db_, mode):
        return None
    monkeypatch.setattr(main, "_live_prices", _no_prices)
    monkeypatch.setattr(main, "_self_heal_orders", _no_heal)
    main.app.dependency_overrides[main.get_db] = lambda: db
    try:
        c = TestClient(main.app)
        opened_rows = {r["symbol"]: r for r in c.get("/positions/DEMO").json()}
        assert opened_rows["BBB"]["setup"]["id"] == ss.LEGACY_ID and opened_rows["BBB"]["setup"]["trigger"] == "BUY NOW"
        assert opened_rows["CCC"]["setup"] is None and opened_rows["DDD"]["setup"] is None      # no stamp / an import: null, never invented
        hist = c.get("/positions/DEMO/history").json()["positions"]
        assert len(hist) == 1 and hist[0]["symbol"] == "AAA" and hist[0]["setup"]["slot"] == "0930"
    finally:
        main.app.dependency_overrides.clear()
