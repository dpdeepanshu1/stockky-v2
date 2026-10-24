"""Short selling step 4: mirrored levels (adaptive, rr_floor), SELL Super Order clamps, short sizing and ledger, trailing and breakeven flips.
Longs must be unchanged; nothing here opens a short."""
import types
import pytest

import config
from execution import dhan_client
from orders import adaptive, rr_floor, sizing, trailing


# ---------------- adaptive ----------------
def test_long_default_levels_unchanged():
    a = adaptive.compute(1.0, 100.0)
    b = adaptive.compute(1.0, 100.0, None, side="LONG")
    assert a == b and a.side == "LONG"
    assert a.stop_price < 100.0 < a.target_price


def test_short_levels_are_mirrored():
    lo = adaptive.compute(1.0, 100.0)
    sh = adaptive.compute(1.0, 100.0, None, side="SHORT")
    assert sh.side == "SHORT"
    assert sh.target_price < 100.0 < sh.stop_price
    assert sh.stop_pct == lo.stop_pct and sh.target_pct == lo.target_pct
    assert rr_floor.geometry_reason(sh, 100.0) is None and rr_floor.geometry_reason(lo, 100.0) is None


def test_short_accepts_negative_pct_change():
    assert adaptive.compute(-1.2, 100.0, None, side="SHORT").target_price < 100.0


def test_unknown_side_raises():
    with pytest.raises(ValueError):
        adaptive.compute(1.0, 100.0, None, side="SIDEWAYS")


@pytest.mark.parametrize("ltp", [5.0, 10.02, 50.05, 99.95])
def test_cheap_stock_clamps_keep_legs_on_the_right_side(ltp):
    for side in ("LONG", "SHORT"):
        lv = adaptive.compute(0.1, ltp, None, side=side)
        assert rr_floor.geometry_reason(lv, ltp) is None, (side, ltp, lv)


def test_short_room_is_mirrored(monkeypatch):
    monkeypatch.setattr(adaptive, "_intraday_range_position", lambda s, p: 0.05)   # price near the day LOW
    monkeypatch.setattr(adaptive, "_atr_proxy_pct", lambda s, p: 1.0)
    monkeypatch.setattr(config, "ADAPTIVE_BAR_ATR_ENABLED", False)
    # _compute_levels, not compute(): compute() then applies the symmetric SCALP_TARGET_CAP_PCT, which flattens both targets to the cap
    sh = adaptive._compute_levels(1.0, 100.0, "X", side="SHORT")    # little room down
    lo = adaptive._compute_levels(1.0, 100.0, "X", side="LONG")     # lots of room up
    assert sh.range_regime == "near_low" and lo.range_regime == "near_low"
    assert sh.target_pct < lo.target_pct


def test_target_cap_keeps_side():
    lv = adaptive.compute(3.0, 100.0, None, side="SHORT")
    capped = adaptive.apply_target_cap(lv, 100.0)
    assert capped.side == "SHORT" and capped.target_price < 100.0 < capped.stop_price


# ---------------- rr_floor geometry ----------------
def test_geometry_rejects_wrong_way_levels():
    bad_long = types.SimpleNamespace(side="LONG", target_price=99.0, stop_price=101.0)
    bad_short = types.SimpleNamespace(side="SHORT", target_price=101.0, stop_price=99.0)
    assert "LEVEL_GEOMETRY" in rr_floor.geometry_reason(bad_long, 100.0)
    assert "LEVEL_GEOMETRY" in rr_floor.geometry_reason(bad_short, 100.0)
    assert rr_floor.geometry_reason(types.SimpleNamespace(side="X", target_price=1, stop_price=2), 100.0)
    assert rr_floor.geometry_reason(types.SimpleNamespace(), 0) is None


# ---------------- sizing ----------------
def test_long_sizing_unchanged():
    # the long path is untouched by the side argument: default, "LONG" and a blank/legacy side all give the same answer, and it is the
    # long rounding (floor 10, may round UP within SIZING_MAX_OVERSHOOT_PCT), so never below the floor
    q = sizing.compute_quantity(1000.0, 100.0)
    assert q == sizing.compute_quantity(1000.0, 100.0, "LONG") == sizing.compute_quantity(1000.0, 100.0, "") and q >= 10


def test_short_sizing_never_rounds_up_or_forces_a_share():
    assert sizing.compute_quantity(1000.0, 99.0, "SHORT") == 10        # floor only
    assert sizing.compute_quantity(1000.0, 1001.0, "SHORT") is None    # one share does not fit: skip (a long may be forced to 1)
    assert sizing.compute_quantity(0.0, 10.0, "SHORT") is None
    with pytest.raises(ValueError):
        sizing.compute_quantity(1000.0, 10.0, "UP")


# ---------------- dhan_client ----------------
def test_clamp_buy_unchanged_and_sell_mirrored():
    assert dhan_client.clamp_super_levels("BUY", 100.0, 102.0, 98.0)[:2] == (102.0, 98.0)
    t, s, n = dhan_client.clamp_super_levels("BUY", 100.0, 100.0, 100.0)
    assert t > 100.0 > s and len(n) == 2
    assert dhan_client.clamp_super_levels("SELL", 100.0, 98.0, 102.0)[:2] == (98.0, 102.0)
    t, s, n = dhan_client.clamp_super_levels("SELL", 100.0, 100.0, 100.0)
    assert t < 100.0 < s and len(n) == 2
    assert dhan_client.clamp_super_levels("SELL", 0, 5, 6) == (5.0, 6.0, [])
    with pytest.raises(ValueError):
        dhan_client.clamp_super_levels("HOLD", 100, 1, 2)


def test_convert_position_refuses_short():
    with pytest.raises(ValueError):
        dhan_client.convert_position(None, is_armed=True, security_id="1", exchange_segment="NSE_EQ", position_type="SHORT", convert_qty=1)


def _stub_client(monkeypatch, client):
    monkeypatch.setattr(dhan_client, "_get_sdk_client", lambda db: client)


def test_limit_super_order_geometry_checked(monkeypatch):
    class Stub:
        def place_super_order(self, **k): return {"data": {"orderId": "1"}}
    _stub_client(monkeypatch, Stub())
    kw = dict(is_armed=True, security_id="1", exchange_segment="NSE_EQ", quantity=1, order_type="LIMIT", price=100)
    with pytest.raises(ValueError):
        dhan_client.place_super_order(None, transaction_type="SELL", target_price=101, stop_loss_price=99, **kw)
    with pytest.raises(ValueError):
        dhan_client.place_super_order(None, transaction_type="BUY", target_price=99, stop_loss_price=101, **kw)
    dhan_client.place_super_order(None, transaction_type="SELL", target_price=98, stop_loss_price=102, **kw)
    dhan_client.place_super_order(None, transaction_type="BUY", target_price=102, stop_loss_price=98, **kw)


def test_market_sell_payload_levels(monkeypatch):
    sent = {}
    class Http:
        def post(self, path, payload): sent.update(payload); return {"data": {"orderId": "9"}}
    class Stub: dhan_http = Http()
    _stub_client(monkeypatch, Stub())
    dhan_client.place_super_order(None, is_armed=True, security_id="1", exchange_segment="NSE_EQ", transaction_type="SELL", quantity=2,
                                  order_type="MARKET", price=100.0, target_price=100.0, stop_loss_price=100.0)
    assert sent["transactionType"] == "SELL" and sent["targetPrice"] < 100.0 < sent["stopLossPrice"]


def test_recent_order_matcher_is_side_aware():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    row = {"transactionType": "SELL", "securityId": "7", "orderStatus": "PENDING", "quantity": 3, "orderId": "a", "createTime": now.strftime("%Y-%m-%d %H:%M:%S")}
    assert dhan_client.match_recent_buy_order([row], "7", 3) is None
    assert dhan_client.match_recent_buy_order([row], "7", 3, transaction_type="SELL")["orderId"] == "a"


# ---------------- ledger ----------------
def test_short_risk_mult():
    from capital import ledger
    assert ledger._short_risk_mult(None) == 1.0 and ledger._short_risk_mult("LONG") == 1.0
    assert ledger._short_risk_mult("SHORT") == config.SHORT_RISK_MULT <= 1.0
    with pytest.raises(ValueError):
        ledger._short_risk_mult("UP")


def test_short_refused_unless_mode_live(monkeypatch):
    from capital import ledger
    for mode in ("off", "shadow"):
        monkeypatch.setattr(config, "SHORT_MODE", mode)
        assert ledger.reserve_capital(None, adaptive_stop_pct=2.0, side="SHORT") is None   # refused before any DB access


def test_short_risk_mult_is_clamped(monkeypatch):
    from capital import ledger
    for raw, want in ((5.0, 1.0), (0.0, 0.1), (float("nan"), 0.5)):
        monkeypatch.setattr(config, "SHORT_RISK_MULT", raw)
        assert ledger._short_risk_mult("SHORT") == want


# ---------------- trailing ----------------
def _tr(**kw):
    base = dict(entry=100.0, peak=100.0, ltp=100.0, current_stop=102.0, target=94.0, stop_pct=2.0, side="SHORT")
    base.update(kw); return trailing.compute_trailing_stop(**base)


def test_long_trailing_unchanged():
    a = trailing.compute_trailing_stop(entry=100.0, peak=103.0, ltp=102.5, current_stop=98.0, target=108.0, stop_pct=2.0, side="LONG")
    assert a == trailing.compute_trailing_stop(entry=100.0, peak=103.0, ltp=102.5, current_stop=98.0, target=108.0, stop_pct=2.0)


def test_short_trailing_follows_trough_down_only():
    new = _tr(peak=97.0, ltp=97.4)                          # trough 97 (3% down)
    assert new is not None and 97.4 < new < 100.0 and new < 102.0
    assert _tr(peak=99.9, ltp=99.9) is None                 # not activated
    assert _tr(peak=97.0, ltp=97.4, current_stop=new - 0.01) is None   # a stop never goes UP
    stop = _tr(peak=97.0, ltp=99.0)                         # bounced through the trail: one tick over the market
    assert stop is not None and stop > 99.0
    assert _tr(peak=90.0, ltp=90.5, target=95.0) is None    # target leg owns that exit


# ---------------- wiring ----------------
def test_no_short_skip_left_in_runners():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    for rel in ("orders/trailing.py", "orders/breakeven.py"):
        t = (root / rel).read_text(encoding="utf-8")
        assert "never trail a short" not in t and "never move a short" not in t, rel
        assert "side_mod.is_short" in t, rel
