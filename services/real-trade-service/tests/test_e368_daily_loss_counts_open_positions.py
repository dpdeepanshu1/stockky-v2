"""E368: real-trade's daily loss check also counts the open positions' unrealized P&L (fresh quotes only); it only blocks a new BUY."""
from __future__ import annotations

import asyncio
import re
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 11, 11, 5, 0, tzinfo=timezone.utc)


def _load_open_pnl():
    ns = {}
    exec(compile((ROOT / "portfolio" / "open_pnl.py").read_text(encoding="utf-8"), "open_pnl.py", "exec"), ns)
    return ns


def _tick(price, age=1.0, source="live"):
    return types.SimpleNamespace(price=price, as_of=NOW - timedelta(seconds=age), source=source, age_known=True)


def _pos(sym, entry, qty):
    return types.SimpleNamespace(symbol=sym, avg_entry_price=entry, qty_open=qty)


def test_unrealized_sums_fresh_ticks_only():
    f = _load_open_pnl()["unrealized_from"]
    ps = [_pos("A", 100.0, 10), _pos("B", 50.0, 20), _pos("C", 10.0, 5), _pos("D", float("nan"), 5)]
    ticks = {"A": _tick(95.0), "B": _tick(60.0, age=99.0), "C": _tick(1.0, source="stale_last_good(x)"), "D": _tick(1.0)}
    assert f(ps, ticks, NOW, 30.0) == -50.0


def test_bad_prices_and_missing_ticks_count_zero():
    f = _load_open_pnl()["unrealized_from"]
    ps = [_pos("A", 100.0, 10), _pos("B", 100.0, 10), _pos("C", 100.0, 10)]
    assert f(ps, {"A": _tick(float("inf")), "B": _tick(-3.0)}, NOW, 30.0) == 0.0


def test_quote_failure_is_zero_not_an_error():
    ns = _load_open_pnl()

    async def boom(_syms, **_k):
        raise RuntimeError("feed down")

    class DB: ...
    import sys
    fake = types.ModuleType("portfolio.portfolio"); fake.held_exposure_positions = lambda db, mode: [_pos("A", 100.0, 1)]
    pkg = types.ModuleType("portfolio"); pkg.__path__ = []
    old = {k: sys.modules.get(k) for k in ("portfolio", "portfolio.portfolio")}
    sys.modules["portfolio"], sys.modules["portfolio.portfolio"] = pkg, fake
    try:
        assert asyncio.run(ns["open_unrealized_pnl"](DB(), "REAL", quotes_fn=boom)) == 0.0
    finally:
        for k, v in old.items():
            if v is None: sys.modules.pop(k, None)
            else: sys.modules[k] = v


def test_engine_check_is_wired_flagged_and_buy_only():
    src = (ROOT / "risk_engine" / "engine.py").read_text(encoding="utf-8")
    i = src.index("3-open. E368")
    seg = src[i:i + 1100]
    assert 'intent.side == "BUY"' in seg and "account.open_unrealized_pnl < 0" in seg and "_daily_loss_counts_unrealized()" in seg
    assert '"daily_loss_limit_open"' in seg and "BLOCKED_GLOBAL" in seg
    assert "open_unrealized_pnl: float = 0.0" in src


def test_entry_computes_it_once_per_cycle_and_passes_the_patchable_quote_fn():
    src = (ROOT / "entry_engine" / "entry.py").read_text(encoding="utf-8")
    assert src.count("open_unrealized_pnl(db, mode, quotes_fn=get_quotes)") == 1
    assert re.search(r"_account_state\(db, mode, gate_armed, reserved_cash, open_pnl=_open_pnl_box\[\"v\"\]\)", src)
    assert "open_unrealized_pnl=float(open_pnl or 0.0)" in src
