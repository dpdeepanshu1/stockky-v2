"""Round 557 (E582): the strategy_version stamp - pure helper, record field, and (by reading the source) the wiring in models / db / entry / portfolio. No DB, no network.
Runs with or without conftest (python -m pytest --noconftest tests/test_round557_strategy_version.py)."""
from __future__ import annotations

import ast
import os
import re
import sys
import types
from datetime import datetime, timezone
from types import SimpleNamespace as NS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import strategy_version as sv  # noqa: E402
import trade_records as tr  # noqa: E402

BASE = {"KRONOS_GATE_MODE": "off", "TIMESFM_GATE_MODE": "off", "STRUCTURE_STOP_MODE": "off", "STRUCTURE_EXIT_MODE": "off", "STRUCTURE_GATE_MODE": "enforce",
        "STRUCTURE_MAX_CHASE_ATR": 1.5, "STRUCTURE_MAX_STRETCH_ATR": 3.0, "STRUCTURE_MIN_ROOM_ATR": 0.5, "STRUCTURE_MIN_RR": 0.0, "OVERNIGHT_HOLD_ENABLED": False}


def rd(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def test_same_values_same_stamp_and_its_shape():
    a, b = sv.compute(BASE), sv.compute(dict(BASE))
    assert a == b and re.fullmatch(r"sv-[0-9a-f]{8}", a) and len(a) <= 24


def test_every_calibrated_switch_changes_the_stamp():
    base = sv.compute(BASE)
    changes = {"KRONOS_GATE_MODE": "log", "TIMESFM_GATE_MODE": "log", "STRUCTURE_STOP_MODE": "on", "STRUCTURE_EXIT_MODE": "on", "STRUCTURE_GATE_MODE": "off",
               "STRUCTURE_MAX_CHASE_ATR": 1.2, "STRUCTURE_MAX_STRETCH_ATR": 2.5, "STRUCTURE_MIN_ROOM_ATR": 0.7, "STRUCTURE_MIN_RR": 1.2, "OVERNIGHT_HOLD_ENABLED": True}
    assert set(changes) == set(sv.KEYS)
    seen = {base}
    for k, v in changes.items():
        s = sv.compute({**BASE, k: v})
        assert s != base and s not in seen, k
        seen.add(s)


def test_noise_is_not_a_change_and_other_keys_are_ignored():
    base = sv.compute(BASE)
    assert sv.compute({**BASE, "STRUCTURE_MAX_CHASE_ATR": 1.50}) == base and sv.compute({**BASE, "STRUCTURE_GATE_MODE": " ENFORCE "}) == base
    assert sv.compute({**BASE, "STRUCTURE_MAX_CHASE_ATR": 1.5000000001}) == base, "float noise"
    assert sv.compute({**BASE, "RISK_MAX_ENTRIES_PER_DAY": 7, "MARKET_STATE_RISK_MULT": 0.5, "SOMETHING_ELSE": "x"}) == base, "overlay dials and unknown keys never count"
    assert sv.compute({}) is None and sv.compute({"OTHER": 1}) is None and sv.compute({k: None for k in sv.KEYS}) is None


def test_current_reads_the_running_config_and_never_raises(monkeypatch=None):
    saved = sys.modules.get("config")
    try:
        cfg = types.ModuleType("config")
        for k, v in BASE.items():
            setattr(cfg, k, v)
        sys.modules["config"] = cfg
        assert sv.current() == sv.compute(BASE)
        cfg.STRUCTURE_MAX_CHASE_ATR = 1.2
        assert sv.current() == sv.compute({**BASE, "STRUCTURE_MAX_CHASE_ATR": 1.2}), "a live change shows at once"
        sys.modules["config"] = types.ModuleType("config")          # a config with none of the switches
        assert sv.current() is None
        sys.modules["config"] = None                                # an import that fails
        assert sv.current() is None
    finally:
        if saved is not None:
            sys.modules["config"] = saved
        else:
            sys.modules.pop("config", None)


def _pos(**kw):
    d = dict(id=1, mode="REAL", symbol="AAA", status="CLOSED", avg_entry_price=100.0, realized_pnl=-20.0, opened_at=datetime(2026, 10, 9, 4, tzinfo=timezone.utc),
             closed_at=datetime(2026, 10, 9, 5, tzinfo=timezone.utc), broker_imported=False, watchlist_entry_id=None, source_tab="watchlist", entry_decision_label="BUY NOW",
             entry_conviction_score=70.0, is_regime_override=False, net_realized_pnl=None, realized_cost_estimate=None)
    d.update(kw)
    return NS(**d)


def _order(id, side, **kw):
    d = dict(id=id, side=side, symbol="AAA", mode="REAL", status="FILLED", exit_reason=None, decision_id=None, watchlist_entry_id=None, execution_source="AUTO",
             created_at=datetime(2026, 10, 9, 4, tzinfo=timezone.utc), updated_at=datetime(2026, 10, 9, 4, tzinfo=timezone.utc))
    d.update(kw)
    return NS(**d)


def _rec(p, buy_kw=None):
    return tr.build_records([p], [_order(1, "BUY", **(buy_kw or {})), _order(2, "SELL")], [], [], [], [], [])[0]


def test_record_carries_the_stamp_position_first_then_the_entry_order_and_none_stays_none():
    assert _rec(_pos(strategy_version="sv-aaaa1111"))["strategy_version"] == "sv-aaaa1111"
    assert _rec(_pos(strategy_version=None), {"strategy_version": "sv-bbbb2222"})["strategy_version"] == "sv-bbbb2222"
    assert _rec(_pos(strategy_version="sv-aaaa1111"), {"strategy_version": "sv-bbbb2222"})["strategy_version"] == "sv-aaaa1111"
    assert _rec(_pos())["strategy_version"] is None and _rec(_pos(strategy_version="  "))["strategy_version"] is None
    r = _rec(_pos(market_state="stressed", profile_version="v7", strategy_version="sv-aaaa1111"))
    assert r["market_state"] == "stressed" and r["profile_version"] == "v7", "the Market Weather stamp is untouched"


def test_models_have_the_nullable_column_on_orders_and_positions():
    tree = ast.parse(rd("models.py"))
    found = {}
    for cls in [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name in ("TradeOrder", "TradePosition")]:
        for st in cls.body:
            if isinstance(st, ast.Assign) and isinstance(st.targets[0], ast.Name) and st.targets[0].id == "strategy_version":
                found[cls.name] = ast.unparse(st.value)
    assert found == {"TradeOrder": "Column(String(24), nullable=True)", "TradePosition": "Column(String(24), nullable=True)"}


def test_db_adds_both_columns_additively():
    src = rd("db.py")
    assert '("trade_orders",    "strategy_version", 24)' in src and '("trade_positions", "strategy_version", 24)' in src
    body = src[src.index("def _ensure_market_state_columns"):src.index("def _ensure_source_tab_columns")]
    assert "ADD COLUMN" in body and "ADD (" in body and "NOT NULL" not in body and "DEFAULT" not in body, "nullable, no default, no backfill"


def test_the_entry_order_is_stamped_and_the_fill_copies_it_both_ways():
    entry = rd("entry_engine/entry.py")
    assert "from strategy_version import current as _sv_current" in entry and entry.count("strategy_version=_sv_current()") == 1
    pf = rd("portfolio/portfolio.py")
    assert pf.count('strategy_version=getattr(order, "strategy_version", None)') == 2, "both position-creating fill paths copy it"
    assert pf.count('profile_version=getattr(order, "profile_version", None)') == 2


def test_nothing_reads_the_stamp_to_decide_anything():
    """Descriptive only: outside the stamp's own helper, its db column, the model, the order / fill copy and the records export, no module mentions it."""
    allowed = {"strategy_version.py", "models.py", "db.py", os.path.join("entry_engine", "entry.py"), os.path.join("portfolio", "portfolio.py"), "trade_records.py"}
    hits = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in ("tests", "__pycache__", "scripts")]
        for f in files:
            if f.endswith(".py"):
                rel = os.path.relpath(os.path.join(base, f), ROOT)
                if "strategy_version" in open(os.path.join(base, f), encoding="utf-8").read() and rel not in allowed:
                    hits.append(rel)
    assert hits == [], hits


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(n, "passed")
