"""group 266: a CNC buy sold the same day is priced at INTRADAY rates (Dhan contract note, 07-Oct-2026)."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import charges_ledger as cl

T0 = datetime(2026, 10, 7, 4, 0, tzinfo=timezone.utc)


def _o(i, sym, side, qty, value, minutes=0, day="2026-10-07", product=None):
    return {"id": i, "symbol": sym, "side": side, "qty": qty, "value": value, "product_type": product,
            "created_at": T0 + timedelta(minutes=minutes), "day": day}


def _tot(rows, key):
    return sum(r["charges"][key] for r in rows)


def test_contract_note_07_oct_real_auto_trade_part():
    """The Real Auto Trade orders of 07-Oct (from the stored ledger) reproduce the note's brokerage / STT split."""
    orders = [
        _o(1, "ADANIGREEN", "SELL", 1, 1340.0, 1), _o(2, "CIEINDIA", "SELL", 4, 1512.0, 2),
        _o(3, "COHANCE", "SELL", 2, 911.0, 3), _o(4, "GODREJCP", "SELL", 1, 868.0, 4),
        _o(5, "IOC", "SELL", 1, 131.0, 5), _o(6, "SCI", "SELL", 1, 286.0, 6),
        _o(7, "DOMS", "BUY", 1, 2073.0, 10), _o(8, "DOMS", "SELL", 1, 2054.0, 20),
        _o(9, "ICIL", "BUY", 6, 2820.0, 11), _o(10, "ICIL", "SELL", 6, 2824.0, 21),
        _o(11, "INOXINDIA", "BUY", 1, 2091.0, 12), _o(12, "INOXINDIA", "SELL", 1, 2083.0, 22),
        _o(13, "JSWCEMENT", "BUY", 1, 450.0, 30), _o(14, "MPHASIS", "BUY", 1, 2285.0, 31),
        _o(15, "UNITDSPR", "BUY", 1, 1344.0, 32),
    ]
    rows = cl.build_rows(orders)
    # same-day pairs (DOMS, ICIL, INOXINDIA): 0.03 % on both legs = Rs 4.18 (note: 19.93 total - scalp share)
    assert _tot(rows, "brokerage") == pytest.approx((2073 + 2054 + 2820 + 2824 + 2091 + 2083) * 0.0003, abs=0.01)
    assert _tot(rows, "brokerage") == pytest.approx(4.18, abs=0.02)
    # STT = intraday 0.025 % on the 3 same-day sells + delivery 0.1 % on the carried sells and the 3 still-open buys
    expect = (2054 + 2824 + 2083) * 0.00025 + (1340 + 1512 + 911 + 868 + 131 + 286 + 450 + 2285 + 1344) * 0.001
    assert _tot(rows, "stt") == pytest.approx(expect, abs=0.01)
    assert _tot(rows, "dp") == pytest.approx(6 * 14.75, abs=0.01)         # only the 6 carried sells pay DP


def test_partial_same_day_sale_is_split_between_intraday_and_delivery():
    rows = {r["id"]: r for r in cl.build_rows([
        _o(1, "AAA", "BUY", 10, 1000.0, 0), _o(2, "AAA", "SELL", 4, 440.0, 5)])}
    assert rows[1]["same_day_frac"] == pytest.approx(0.4)                  # 4 of the 10 bought shares were closed today
    assert rows[1]["charges"]["brokerage"] == pytest.approx(1000 * 0.0003 * 0.4, abs=1e-6)
    assert rows[2]["same_day_frac"] == 1.0 and rows[2]["charges"]["dp"] == 0.0


def test_sale_of_an_earlier_days_buy_stays_delivery():
    rows = cl.build_rows([_o(1, "AAA", "BUY", 10, 1000.0, 0, day="2026-10-06"),
                          _o(2, "AAA", "SELL", 10, 1100.0, 1440, day="2026-10-07")])
    sell = rows[1]
    assert sell["same_day_frac"] == 0.0 and sell["charges"]["brokerage"] == 0.0 and sell["charges"]["dp"] > 0


def test_sell_before_rebuy_is_not_matched():
    rows = cl.build_rows([_o(1, "AAA", "BUY", 5, 500.0, 0, day="2026-10-06"),
                          _o(2, "AAA", "SELL", 5, 520.0, 1440), _o(3, "AAA", "BUY", 5, 510.0, 1450)])
    by = {r["id"]: r for r in rows}
    assert by[2]["same_day_frac"] == 0.0 and by[3]["same_day_frac"] == 0.0 and by[2]["charges"]["dp"] > 0


def test_explicit_intraday_orders_and_missing_qty_are_unchanged():
    rows = {r["id"]: r for r in cl.build_rows([
        _o(1, "MIS", "BUY", 10, 1000.0, 0, product="INTRADAY"), _o(2, "MIS", "SELL", 10, 1010.0, 1, product="INTRADAY"),
        _o(3, "NQ", "BUY", 0, 1000.0, 2), _o(4, "NQ", "SELL", 0, 1010.0, 3)])}
    assert rows[1]["charges"]["brokerage"] > 0 and rows[2]["charges"]["stt"] > 0
    assert rows[3]["same_day_frac"] == 0.0                                  # no qty -> priced as plain delivery as before
