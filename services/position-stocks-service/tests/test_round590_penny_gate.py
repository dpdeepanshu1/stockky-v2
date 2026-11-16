"""round 590: cheap-stock tick friction and dust-position floor (16 Nov: AGL at about Rs 14, 4 shares, about Rs 57, STOP_HIT -2.53 %).
Plain runner: python3 tests/test_round590_penny_gate.py"""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from orders import penny_gate as pg


def _bands(price):   # the NSE tick bands the live client uses: Rs 0.05 below Rs 250 is all this test needs
    return 0.05


def setup():
    config.PENNY_TICK_GATE_ENABLED = True
    config.PENNY_TICK_MAX_STOP_FRAC = 0.12
    config.ENTRY_MIN_NOTIONAL_RS = 300.0


def r(price, stop_pct, qty, tick=0.05):
    return pg.reject_reason(price, stop_pct, qty, tick=tick)


def test_agl_case_is_refused_on_tick_friction():
    setup()
    assert r(14.2, 2.5, 4).startswith("PENNY_TICK")          # one tick = 14 % of a 2.5 % stop
    assert r(10.6, 2.5, 40).startswith("PENNY_TICK")


def test_a_wider_stop_lets_a_cheap_stock_trade():
    setup()
    assert r(14.2, 4.5, 40) is None                           # volatile cheap name: a tick is under 8 % of the risk
    assert r(30.0, 2.5, 20) is None                           # normal price, normal stop


def test_dust_notional_is_refused():
    setup()
    assert r(80.0, 2.5, 3).startswith("MIN_NOTIONAL")        # Rs 240
    assert r(80.0, 2.5, 4) is None                            # Rs 320


def test_switches_and_fail_open():
    setup()
    config.PENNY_TICK_GATE_ENABLED = False
    assert r(14.2, 2.5, 40) is None
    setup()
    config.PENNY_TICK_MAX_STOP_FRAC = 0.0
    assert r(14.2, 2.5, 40) is None                           # 0 = off
    setup()
    config.ENTRY_MIN_NOTIONAL_RS = 0.0
    assert r(80.0, 2.5, 1) is None
    setup()
    assert r(0, 2.5, 4) is None and r(14.2, 0, 40) is None and r(14.2, None, 40) is None and r(14.2, 2.5, 0) is None
    assert r("x", 2.5, 4) is None                             # garbage never raises


def test_tick_friction_value():
    assert abs(pg.tick_friction(14.2, 2.5, tick=0.05) - (0.05 / 14.2 * 100 / 2.5)) < 1e-9
    assert pg.tick_friction(0, 2.5, tick=0.05) is None


def test_live_tick_lookup_is_used_when_no_tick_given():
    setup()
    fake = types.SimpleNamespace(tick_size_for_price=_bands)
    sys.modules.setdefault("execution", types.ModuleType("execution"))
    old = sys.modules.get("execution.dhan_client")
    sys.modules["execution.dhan_client"] = fake
    sys.modules["execution"].dhan_client = fake
    try:
        assert pg.reject_reason(14.2, 2.5, 4).startswith("PENNY_TICK")
    finally:
        if old is None:
            sys.modules.pop("execution.dhan_client", None)
        else:
            sys.modules["execution.dhan_client"] = old


def test_switches_are_in_the_calibration_catalog_and_wired():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cat = open(os.path.join(here, "control", "catalog.py"), encoding="utf-8").read()
    ent = open(os.path.join(here, "orders", "entry.py"), encoding="utf-8").read()
    sht = open(os.path.join(here, "orders", "short_entry.py"), encoding="utf-8").read()
    for k in ("PENNY_TICK_GATE_ENABLED", "PENNY_TICK_MAX_STOP_FRAC", "ENTRY_MIN_NOTIONAL_RS"):
        assert f'Flag("{k}"' in cat, k
    assert "penny_gate.reject_reason" in ent and "penny_gate.reject_reason" in sht      # live on both sides, not log-only
    assert '"PENNY_TICK"' in ent and '"MIN_NOTIONAL"' in ent                            # per-symbol skip, ends no cycle


if __name__ == "__main__":
    n = 0
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
            n += 1
    print("ok", n)
