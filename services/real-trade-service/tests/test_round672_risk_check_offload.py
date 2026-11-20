"""round 672 (D7h): POST /risk-engine/check (an admin dry run) reads the DB in two worker-thread steps around the live-price await,
instead of ~20 blocking queries on the event loop. Source-level checks, so they run without the app's packages; the behaviour of the
route is covered by the /risk-engine/check cases in test_main_routes_core.py."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _route() -> str:
    s = (ROOT / "main.py").read_text(encoding="utf-8")
    a = s.index("async def risk_engine_check")
    return s[a:s.index("class ManualOrderRequest", a)]


def test_two_off_loop_steps_with_the_price_await_between_them():
    r = _route()
    i1 = r.index("await _db_off_loop(_prepare)")
    i2 = r.index("await _open_pnl_mod.open_unrealized_pnl(")
    i3 = r.index("await _db_off_loop(_decide)")
    i4 = r.index("await _commit_off_loop(db)")
    assert i1 < i2 < i3 < i4


def test_blocking_reads_sit_inside_the_steps_not_on_the_loop():
    r = _route()
    prep = r[r.index("def _prepare():"):r.index("await _db_off_loop(_prepare)")]
    dec = r[r.index("def _decide():"):r.index("await _db_off_loop(_decide)")]
    for call in ("_check_and_expire_gates(db, mode)", "db.query(models.TradeAccount)", "_pf_held_exposure_positions(db, mode)",
                 "_pf_today_loss_streak(db, mode)", "shared_exposure.get_other_service_exposure_age(db)"):
        assert call in prep
    for call in ("shared_exposure.get_in_flight_buy_value(db, mode)", "heat_state.own_heat(db, mode, open_positions)",
                 "risk_evaluate(intent, account_state)", "db.add(models.TradeRiskEvent("):
        assert call in dec
    outside = r.replace(prep, "").replace(dec, "")
    assert "db.query" not in outside and "shared_exposure." not in outside and "heat_state." not in outside


def test_admin_check_and_404_and_buy_only_open_loss_are_unchanged():
    r = _route()
    assert r.index("require_admin_if_real(mode, authorization)") < r.index("def _prepare():")   # auth first, before any DB work
    assert 'status_code=404, detail=f"No account/risk-config for mode={mode}"' in r
    assert 'if body.side.upper() == "BUY":   # E370' in r
