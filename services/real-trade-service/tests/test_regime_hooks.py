"""Market Weather step 2 (real-trade-service): the hooks are 1.0 / 0 by default (behaviour identical), only ever TIGHTEN, and are read at call time.
Runs under pytest or plainly:  cd services/real-trade-service && python3 tests/test_regime_hooks.py"""
from __future__ import annotations

import os
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import config  # noqa: E402
from risk_engine import effective_risk, engine  # noqa: E402
from risk_engine.engine import AccountState, OrderIntent, RiskVerdict, evaluate  # noqa: E402

NOW = datetime(2026, 10, 9, 5, 0, 0, tzinfo=timezone.utc)   # 10:30 IST, market hours


@contextmanager
def _set(obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


@contextmanager
def _pinned():
    with _set(engine, "MIN_STOCK_PRICE", 20.0), _set(engine, "MAX_STOCK_PRICE", 3000.0), _set(engine, "MAX_STOCK_PRICE_EXPLICITLY_SET", False), \
            _set(engine, "RISK_MAX_STOCK_PRICE_ADAPTIVE", True), _set(engine, "CAPITAL_SHARE_PCT", 50.0), _set(engine, "LOSS_STREAK_MAX", 3), \
            _set(engine, "MAX_ENTRIES_PER_DAY", 6):
        yield


def _account(**over):
    base = dict(equity=100_000.0, risk_per_trade_pct=1.0, max_daily_loss_pct=3.0, max_concurrent_positions=3, max_portfolio_risk_pct=5.0,
                stale_data_seconds=30, max_tick_volatility_mult=2.0, allow_pyramiding=False, realized_pnl_today=0.0, open_position_count=0,
                open_position_symbols=set(), open_positions_total_risk=0.0, trading_globally_paused=False, market_is_open=True,
                cash_available=100_000.0, broker_cash_available=200_000.0, open_positions_market_value=0.0, other_service_open_positions_market_value=0.0)
    base.update(over)
    return AccountState(**base)


def _intent(**over):
    base = dict(mode="REAL", symbol="TESTCO", side="BUY", qty=250, entry_price=100.0, stop_price=90.0)    # Rs 10 risk a share: the 1% cap (Rs 1000) binds before the 25% concentration cap
    base.update(over)
    return OrderIntent(**base)


def _qty(**acct):
    r = evaluate(_intent(), _account(**acct), now=NOW)
    assert r.verdict == RiskVerdict.APPROVED and r.check_name == "sized_down", r
    return r


def test_defaults_change_nothing():
    assert config.MARKET_STATE_MODE == "shadow" and config.MARKET_STATE_RISK_MULT == 1.0 and config.MARKET_STATE_CONVICTION_ADD == 0.0
    assert effective_risk.market_state_risk_mult() == 1.0


def test_multiplier_shrinks_the_per_trade_cap_and_default_is_identical():
    with _pinned():
        base = _qty()
        with _set(config, "MARKET_STATE_RISK_MULT", 1.0):
            same = _qty()
        with _set(config, "MARKET_STATE_RISK_MULT", 0.5):
            half = _qty()
    assert (base.verdict, base.approved_qty) == (same.verdict, same.approved_qty)
    assert base.approved_qty and half.approved_qty and half.approved_qty < base.approved_qty
    assert (base.approved_qty, half.approved_qty) == (100, 50), (base.approved_qty, half.approved_qty)    # 1% cap -> 0.5% cap


def test_multiplier_also_tightens_a_conviction_upsize():
    with _pinned():
        up = evaluate(_intent(adj_risk_pct=2.0), _account(), now=NOW)
        with _set(config, "MARKET_STATE_RISK_MULT", 0.5):
            down = evaluate(_intent(adj_risk_pct=2.0), _account(), now=NOW)
    assert (up.approved_qty, down.approved_qty) == (200, 100), (up.approved_qty, down.approved_qty)    # a 2% upsize is halved too


def test_multiplier_is_clamped_so_it_can_only_reduce():
    for raw, want in ((5.0, 1.0), (0.0, 0.1), (-3.0, 0.1), (float("nan"), 1.0), ("junk", 1.0), (0.37, 0.37)):
        with _set(config, "MARKET_STATE_RISK_MULT", raw):
            assert effective_risk.market_state_risk_mult() == want, raw
            assert engine._market_state_risk_mult() == want, raw


def test_display_value_applies_the_multiplier_once_and_the_engine_feed_is_untouched():
    class Row:
        risk_per_trade_pct = 2.0
        updated_by = "admin"          # saved by a person: the row wins
    with _set(config, "MARKET_STATE_RISK_MULT", 0.5):
        assert effective_risk.effective_risk_pct(Row()) == 2.0                 # feeds AccountState: NOT multiplied (the engine does it, once)
        assert effective_risk.effective_risk_pct_in_force(Row()) == 1.0        # what the page shows
    assert effective_risk.effective_risk_pct_in_force(None) is None


def _load_min_conviction():
    """The real `_min_conviction` source, executed on its own: candidates.py itself needs httpx / sqlalchemy, which a bare checkout may not have."""
    import ast
    src = (ROOT / "candidate_engine" / "candidates.py").read_text(encoding="utf-8")
    fn = [n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "_min_conviction"]
    assert len(fn) == 1
    ns = {"config": config, "MIN_CONVICTION": 55.0}
    exec(compile(ast.Module(body=[fn[0]], type_ignores=[]), "candidates._min_conviction", "exec"), ns)
    return ns["_min_conviction"]


def test_conviction_floor_default_and_clamp():
    f = _load_min_conviction()
    assert f() == 55.0
    for raw, want in ((7.0, 62.0), (50.0, 75.0), (-4.0, 55.0), (float("nan"), 55.0), ("x", 55.0), (None, 55.0)):
        with _set(config, "MARKET_STATE_CONVICTION_ADD", raw):
            assert f() == want, raw


def test_conviction_hook_is_used_at_every_comparison_and_never_where_a_score_is_assigned():
    src = "\n".join(l for l in (ROOT / "candidate_engine" / "candidates.py").read_text(encoding="utf-8").splitlines() if not l.lstrip().startswith("#"))
    assert not re.search(r"score\s*<\s*MIN_CONVICTION", src), "a `score < MIN_CONVICTION` comparison still ignores the market-state add"
    assert len(re.findall(r"score\s*<\s*_min_conviction\(\)", src)) == 3
    assert len(re.findall(r"score = max\(MIN_CONVICTION, \d+\.0\)", src)) == 3          # assignments keep the base (a raised floor must not inflate a score)


def test_engine_applies_the_multiplier_once_before_the_daily_loss_cap():
    src = (ROOT / "risk_engine" / "engine.py").read_text(encoding="utf-8")
    assert src.count("effective_risk_pct *= _market_state_risk_mult()") == 1
    assert src.index("effective_risk_pct *= _market_state_risk_mult()") < src.index("_cap_trade_risk_at_daily_loss() and account.max_daily_loss_pct")
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    assert main.count("effective_risk_pct_in_force(") == 2 and "risk_per_trade_pct=effective_risk_pct(risk_row)" in main


def test_stamp_columns_exist_and_are_additive_and_nullable():
    import ast
    tree = ast.parse((ROOT / "models.py").read_text(encoding="utf-8"))
    for cls in [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name in ("TradeOrder", "TradePosition")]:
        found = {}
        for st in cls.body:
            if isinstance(st, ast.Assign) and isinstance(st.targets[0], ast.Name) and st.targets[0].id in ("market_state", "profile_version"):
                found[st.targets[0].id] = ast.unparse(st.value)
        assert found == {"market_state": "Column(String(16), nullable=True)", "profile_version": "Column(String(24), nullable=True)"}, (cls.name, found)
    db_src = (ROOT / "db.py").read_text(encoding="utf-8")
    assert "_ensure_market_state_columns(eng, dialect())" in db_src and "def _ensure_market_state_columns" in db_src
    entry = (ROOT / "entry_engine" / "entry.py").read_text(encoding="utf-8")
    assert "market_state=_ms_current_profile()" in entry
    pf = (ROOT / "portfolio" / "portfolio.py").read_text(encoding="utf-8")
    assert pf.count('market_state=getattr(order, "market_state", None)') == 2


def test_stamp_reads_the_kit_profile_and_is_none_without_one():
    from control import kit
    kit.ACTIVE.clear()
    assert kit.current_profile() == {"market_state": None, "profile_version": None}
    kit.ACTIVE.update({"state": "stressed", "version": "v1", "until_ts": datetime.now(timezone.utc).timestamp() + 600})
    try:
        assert kit.current_profile() == {"market_state": "stressed", "profile_version": "v1"}
    finally:
        kit.ACTIVE.clear()


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(f"{n} passed")
