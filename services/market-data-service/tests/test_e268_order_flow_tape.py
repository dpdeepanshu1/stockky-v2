"""E268: intraday order-flow tape (order_flow_tape.py)."""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import order_flow_tape as t  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
T0 = datetime(2026, 10, 30, 10, 0, 0, tzinfo=IST)


def _row(price, vol, avg, bid=None, ask=None, bq=None, aq=None, sym="ABC"):
    r = {"symbol": sym, "price": price, "volume": vol, "avg_price": avg}
    if bid is not None:
        r.update(best_bid=bid, best_ask=ask)
    if bq is not None:
        r.update(bid_qty_5=bq, ask_qty_5=aq)
    return r


def _feed(n, step_s=2, start_px=100.0, drift=0.0, buy=True, dv=1000, sym="ABC", t0=T0, bq=None, aq=None):
    """n snapshots; every interval trades at the ask (buy) or the bid (sell); VWAP follows the trade price."""
    t._reset_for_tests()
    vol, avg_num, px = 100_000.0, 100_000.0 * start_px, start_px
    t.on_rows([_row(px, vol, start_px, px - .05, px + .05, bq, aq, sym)], now=t0)
    for i in range(1, n + 1):
        px = round(start_px + drift * i, 2)
        tp = px + .05 if buy else px - .05
        vol += dv
        avg_num += dv * tp
        t.on_rows([_row(px, vol, avg_num / vol, px - .05, px + .05, bq, aq, sym)], now=t0 + timedelta(seconds=step_s * i))
    return t0 + timedelta(seconds=step_s * n)


def setup_function(_f=None):
    t._reset_for_tests()


def test_interval_price_recovers_trade_price_from_vwap():
    # 1000 shares at 100.10 on top of 100000 @ 100.00
    avg1 = (100000 * 100.0 + 1000 * 100.10) / 101000
    assert abs(t.interval_price(100.0, 100000, avg1, 101000, 100.1) - 100.10) < 1e-6


def test_interval_price_falls_back_to_ltp():
    assert t.interval_price(None, 100, 100.0, 200, 100.2) == 100.2
    assert t.interval_price(100.0, 100, 100.0, 100, 100.2) == 100.2          # no new volume
    assert t.interval_price(100.0, 100, 150.0, 200, 100.2) == 100.2          # > 2 % away from the LTP


def test_classify_quote_then_tick_then_last_side():
    assert t.classify(100.05, 100.0, 100.05, 100.0, None) == "buy"
    assert t.classify(100.0, 100.0, 100.05, 100.02, None) == "sell"
    assert t.classify(100.03, 100.0, 100.05, 100.02, None) == "buy"          # tick rule
    assert t.classify(100.03, 100.0, 100.05, 100.04, None) == "sell"
    assert t.classify(100.03, 100.0, 100.05, 100.03, "buy") == "buy"
    assert t.classify(100.03, 100.0, 100.05, 100.03, None) == "neutral"


def test_buying_tape_has_positive_delta_and_buyers_aggressive():
    now = _feed(400, buy=True)
    s = t.snapshot("ABC", now)
    assert s["available"] and s["reliable"], s["reason"]
    assert s["signals"]["delta_day"] > 0 and s["signals"]["aggressor"] == "buyers"
    assert s["signals"]["buy_pct_10m"] > 90


def test_selling_tape_has_negative_delta():
    now = _feed(400, buy=False)
    s = t.snapshot("ABC", now)
    assert s["signals"]["delta_day"] < 0 and s["signals"]["aggressor"] == "sellers"


def test_unreliable_until_enough_tape():
    now = _feed(10)
    s = t.snapshot("ABC", now)
    assert s["available"] and not s["reliable"] and s["reason"] == "too_few_intervals"
    v = t.verdict("ABC", "LONG", 1, now)
    assert v["block"] is False


def test_stale_tape_is_unreliable():
    now = _feed(400)
    assert t.snapshot("ABC", now + timedelta(seconds=600))["reason"] == "stale"


def test_no_tape_for_unknown_symbol():
    s = t.snapshot("NOPE", T0)
    assert s["available"] is False and s["reliable"] is False


def test_verdict_blocks_a_long_into_selling_and_not_a_short():
    now = _feed(400, buy=False, drift=-0.001)
    v = t.verdict("ABC", "LONG", 2, now)
    assert v["reliable"] and v["block"] and v["against"] >= 2 and v["reason"].startswith("ORDER_FLOW:")
    sv = t.verdict("ABC", "SHORT", 2, now)
    assert sv["block"] is False


def test_verdict_lets_a_long_through_a_buying_tape():
    now = _feed(400, buy=True, drift=0.001)
    assert t.verdict("ABC", "LONG", 2, now)["block"] is False


def test_book_imbalance_counts_for_the_imbalance_check():
    now = _feed(400, buy=True, bq=1000, aq=9000)
    s = t.snapshot("ABC", now)
    assert s["imbalance_5"] < -0.3 and s["checklist"]["long"]["checks"]["imbalance"] == "against"


def test_cumulative_volume_reset_reanchors_and_does_not_count():
    now = _feed(60)
    before = t.snapshot("ABC", now)["intervals"]
    t.on_rows([_row(100.0, 5_000, 100.0)], now=now + timedelta(seconds=2))       # volume went backwards
    assert t.status()["skipped_reset"] == 1
    assert t.snapshot("ABC", now + timedelta(seconds=2))["intervals"] == before


def test_feed_gap_is_skipped_not_lumped():
    now = _feed(60)
    before = t.snapshot("ABC", now)["intervals"]
    t.on_rows([_row(100.0, 10_000_000, 100.0)], now=now + timedelta(seconds=400))
    assert t.status()["skipped_stale"] == 1
    assert t.snapshot("ABC", now + timedelta(seconds=400))["intervals"] == before


def test_missing_volume_is_skipped():
    t.on_rows([{"symbol": "ABC", "price": 100.0}], now=T0)
    assert t.status()["skipped_no_volume"] == 1 and t.snapshot("ABC", T0)["available"] is False


def test_out_of_order_row_is_ignored():
    now = _feed(60)
    before = t.snapshot("ABC", now)["intervals"]
    # A slightly older row (volume slightly lower, within 2%) arrives late
    t.on_rows([_row(100.0, 159_000, 100.0)], now=now + timedelta(seconds=2))
    assert t.status()["skipped_reorder"] == 1
    assert t.snapshot("ABC", now + timedelta(seconds=2))["intervals"] == before


def test_new_day_starts_a_new_tape():
    _feed(60)
    tomorrow = T0 + timedelta(days=1)
    t.on_rows([_row(100.0, 50_000, 100.0)], now=tomorrow)
    assert t.snapshot("ABC", tomorrow)["intervals"] == 0


def test_profile_poc_value_area_and_location():
    w = 0.5
    bins = {int(p / w): v for p, v in {98.0: 100, 98.5: 300, 99.0: 800, 99.5: 1500, 100.0: 2500, 100.5: 1400, 101.0: 700, 101.5: 250, 102.0: 90}.items()}
    p = t.build_profile(bins, w, 103.0)
    assert p["poc"] == 100.0 and p["val"] <= 99.5 and p["vah"] >= 100.5
    assert p["location"] == "above_value"
    assert t.build_profile(bins, w, 97.0)["location"] == "below_value"
    assert t.build_profile(bins, w, 100.0)["location"] == "inside_value"
    assert t.build_profile({}, w) is None


def test_profile_finds_hvn_wall_above_and_lvn():
    w = 0.5
    prices = {99.0: 400, 99.5: 800, 100.0: 3000, 100.5: 800, 101.0: 120, 101.5: 100, 102.0: 900, 102.5: 3200, 103.0: 900, 103.5: 300}
    bins = {int(p / w): v for p, v in prices.items()}
    p = t.build_profile(bins, w, 101.0)
    assert 102.5 in p["hvn"] and 100.0 in p["hvn"]
    assert any(101.0 <= x <= 101.5 for x in p["lvn"])
    assert p["node"] == "lvn" and p["hvn_above"] == 102.5 and p["hvn_below"] == 100.0


def test_stacked_buy_imbalance_needs_three_adjacent_levels():
    b = t._Bar(600, 100.0)
    b.levels = {1000: [900.0, 100.0], 1001: [800.0, 100.0], 1002: [700.0, 50.0], 1010: [900.0, 10.0]}
    assert t._stacked([b]) == {"buy": 1, "sell": 0}
    b.levels = {1000: [900.0, 100.0], 1001: [800.0, 100.0], 1003: [700.0, 50.0]}
    assert t._stacked([b]) == {"buy": 0, "sell": 0}


def test_big_print_is_an_outsized_interval_with_enough_value():
    t._reset_for_tests()
    vol, num = 100_000.0, 100_000.0 * 100.0
    t.on_rows([_row(100.0, vol, 100.0, 99.95, 100.05)], now=T0)
    for i in range(1, 31):                                   # 30 normal intervals of 1000
        vol += 1000
        num += 1000 * 100.05
        t.on_rows([_row(100.0, vol, num / vol, 99.95, 100.05)], now=T0 + timedelta(seconds=2 * i))
    vol += 50_000                                            # one 50x block bought at the ask
    num += 50_000 * 100.05
    t.on_rows([_row(100.0, vol, num / vol, 99.95, 100.05)], now=T0 + timedelta(seconds=62))
    bars = t.bars("ABC", 5)
    assert sum(b["big_buy"] for b in bars) == 1 and sum(b["big_sell"] for b in bars) == 0


def test_absorption_flagged_when_buying_does_not_lift_price():
    t._reset_for_tests()
    vol, num = 100_000.0, 100_000.0 * 100.0
    t.on_rows([_row(100.0, vol, 100.0, 99.95, 100.05)], now=T0)
    for i in range(1, 400):
        heavy = i > 340                                      # last ~2 minutes: 4x volume, all buys at the ask, price flat
        dv = 4000 if heavy else 1000
        tp = 100.05 if (heavy or i % 2) else 99.95
        vol += dv
        num += dv * tp
        t.on_rows([_row(100.0, vol, num / vol, 99.95, 100.05)], now=T0 + timedelta(seconds=2 * i))
    s = t.snapshot("ABC", T0 + timedelta(seconds=800))
    assert s["signals"]["absorption"] == "buying_absorbed"
    assert s["checklist"]["long"]["checks"]["imbalance"] == "against"


def test_memory_is_bounded():
    t._reset_for_tests()
    os.environ["FLOW_TAPE_MAX_SYMBOLS"] = "50"
    try:
        for i in range(80):
            t.on_rows([_row(100.0, 1000, 100.0, sym=f"S{i}")], now=T0)
        assert t.status()["symbols"] <= 50 and t.status()["symbols_evicted"] == 30
    finally:
        del os.environ["FLOW_TAPE_MAX_SYMBOLS"]


def test_disabled_switch_stops_intake():
    os.environ["FLOW_TAPE_ENABLED"] = "0"
    try:
        assert t.on_rows([_row(100.0, 1000, 100.0)], now=T0) == 0
        assert t.status()["symbols"] == 0
    finally:
        del os.environ["FLOW_TAPE_ENABLED"]


def test_public_functions_never_raise_on_garbage():
    assert t.on_rows([None, {}, {"symbol": "X", "price": "bad", "volume": "x"}], now=T0) == 0
    assert t.snapshot(None)["available"] is False
    assert t.verdict(None, None)["block"] is False
    assert t.bars("NOPE") == []


def test_flow_routes_answer_with_and_without_a_tape():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    import main
    now = _feed(400, buy=False, drift=-0.001, t0=datetime.now(timezone.utc) - timedelta(seconds=800))
    c = TestClient(main.app)
    j = c.get("/flow/NOPE").json()
    assert j["available"] is False
    assert c.get("/flow/NOPE", params={"side": "LONG"}).json()["block"] is False
    assert c.get("/flow/ABC/bars", params={"limit": 5}).status_code == 200
    assert "enabled" in c.get("/internal/flow-status").json()
