"""group348: Dhan-based day P&L - the 13 Oct MANALIPETC double exit (2 buys, 2 sells) is visible, a short is flagged."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from orders.day_pnl import summarize  # noqa: E402


def o(side, sym, qty, px, status="TRADED"):
    return {"transactionType": side, "tradingSymbol": sym, "filledQty": qty, "averageTradedPrice": px, "orderStatus": status}


def test_manalipetc_13_oct_matches_dhan():
    r = summarize([o("BUY", "MANALIPETC", 20, 84.19), o("SELL", "MANALIPETC", 20, 81.87),
                   o("SELL", "MANALIPETC", 20, 82.10), o("BUY", "MANALIPETC", 20, 82.75)])
    row = r["rows"][0]
    assert row["net_qty"] == 0 and row["avg_buy"] == 83.47 and row["avg_sell"] == 81.985
    assert r["gross_pnl"] == -59.4                      # exact fills; the Dhan app line shows about -59.20 from rounded averages


def test_double_sell_without_cover_is_a_flagged_short():
    r = summarize([o("BUY", "X", 10, 100), o("SELL", "X", 10, 99), o("SELL", "X", 10, 99.5)])
    assert r["not_flat"] == ["X"] and r["rows"][0]["flag"] == "SHORT" and r["rows"][0]["net_qty"] == -10


def test_open_holding_is_flagged_held():
    r = summarize([o("BUY", "Y", 5, 50)])
    assert r["rows"][0]["flag"] == "HELD" and r["gross_pnl"] == 0.0


def test_cancelled_rejected_and_bad_rows_are_ignored():
    r = summarize([o("BUY", "Z", 5, 10, "CANCELLED"), o("BUY", "Z", 5, 10, "REJECTED"), {"junk": 1}, o("SELL", "Z", 0, 10)])
    assert r["symbols"] == 0


def test_profit_and_multiple_symbols_total():
    r = summarize([o("BUY", "A", 10, 100), o("SELL", "A", 10, 101), o("BUY", "B", 2, 50), o("SELL", "B", 2, 49)])
    assert r["gross_pnl"] == 8.0 and r["symbols"] == 2


def test_empty_and_none():
    assert summarize(None)["gross_pnl"] == 0.0 and summarize([])["rows"] == []
