"""group 258: real-trade-service cumulative brokerage report (charges_ledger.py). Pure helpers only."""
from datetime import datetime, timedelta, timezone

import config
import charges_ledger as cl

_T0 = datetime(2026, 10, 7, 4, 0, tzinfo=timezone.utc)


def _o(i, side, symbol, value, product=None, minutes=0):
    t = _T0 + timedelta(minutes=minutes)
    return {"id": i, "symbol": symbol, "side": side, "product_type": product, "value": value,
            "created_at": t, "day": "2026-10-07"}


def test_delivery_pays_no_brokerage_intraday_pays_lower_of_cap_and_pct():
    assert cl.order_brokerage(50_000, "CNC") == config.CHARGES_DELIVERY_BROKERAGE_RS == 0.0
    assert abs(cl.order_brokerage(10_000, "INTRADAY") - 3.0) < 1e-9
    assert cl.order_brokerage(5_000_000, "MIS") == config.CHARGES_BROKERAGE_CAP_RS
    assert cl.order_brokerage(0, "INTRADAY") == 0.0


def test_unknown_product_is_treated_as_cnc():
    assert cl.order_brokerage(20_000, None) == 0.0


def test_sell_takes_the_product_of_the_latest_earlier_buy_of_that_symbol():
    rows = cl.build_rows([
        _o(2, "SELL", "AAA", 10_100, minutes=30),
        _o(1, "BUY", "AAA", 10_000, product="INTRADAY", minutes=0),
        _o(3, "BUY", "BBB", 10_000, product="CNC", minutes=1),
        _o(4, "SELL", "BBB", 10_100, minutes=40),
    ])
    by_id = {r["id"]: r for r in rows}
    assert by_id[2]["product"] == "INTRADAY" and by_id[2]["brokerage"] > 0
    assert by_id[4]["product"] == "CNC" and by_id[4]["brokerage"] == 0.0


def test_sell_with_no_known_buy_defaults_to_cnc():
    rows = cl.build_rows([_o(9, "SELL", "ZZZ", 12_000)])
    assert rows[0]["product"] == "CNC" and rows[0]["brokerage"] == 0.0


def test_summary_totals_cap_count_and_top_symbols():
    rows = cl.build_rows([
        _o(1, "BUY", "AAA", 10_000, "INTRADAY", 0),
        _o(2, "SELL", "AAA", 10_100, None, 10),
        _o(3, "BUY", "BIG", 5_000_000, "INTRADAY", 20),
        _o(4, "BUY", "DEL", 80_000, "CNC", 30),
    ])
    s = cl.summarize(rows)
    assert s["orders"] == 4 and s["orders_paying_brokerage"] == 3 and s["orders_at_cap"] == 1
    assert abs(s["brokerage_total"] - 26.03) < 0.005
    assert abs(s["brokerage_incl_gst"] - round(26.03 * 1.18, 2)) < 0.01
    assert s["top_symbols"][0] == {"symbol": "BIG", "brokerage": 20.0}
    assert s["by_product"]["CNC"]["brokerage"] == 0.0 and s["by_product"]["INTRADAY"]["orders"] == 3
    assert s["since"] == "2026-10-07" and s["trading_days"] == 1


def test_summary_empty():
    s = cl.summarize([])
    assert s["orders"] == 0 and s["brokerage_total"] == 0 and s["since"] is None
    assert s["avg_brokerage_per_paying_order"] is None


def test_recent_days_newest_first_and_limited():
    rows = []
    for d in range(5):
        r = cl.build_rows([_o(d, "BUY", "AAA", 10_000, "INTRADAY", d)])[0]
        r["day"] = f"2026-10-0{d + 1}"
        rows.append(r)
    s = cl.summarize(rows, recent_days=3)
    assert [x["day"] for x in s["recent_days"]] == ["2026-10-05", "2026-10-04", "2026-10-03"]


# -- group 259: all charges + range / today / total layout --
def _row(i, side, symbol, value, product, day, minutes=0):
    o = _o(i, side, symbol, value, product, minutes)
    o["day"] = day
    return o


def test_delivery_sell_pays_stt_and_dp_but_no_brokerage():
    buy, sell = cl.build_rows([
        _row(1, "BUY", "AAA", 10_000, "CNC", "2026-10-07", 0),
        _row(2, "SELL", "AAA", 10_000, None, "2026-10-07", 30),
    ])
    assert buy["brokerage"] == 0 and buy["charges"]["stt"] == 0 and buy["charges"]["stamp"] > 0 and buy["charges"]["dp"] == 0
    assert sell["brokerage"] == 0 and abs(sell["charges"]["stt"] - 10.0) < 1e-9 and sell["charges"]["dp"] == 13.5


def test_dp_is_billed_once_per_scrip_per_day():
    rows = cl.build_rows([
        _row(1, "SELL", "AAA", 5_000, "CNC", "2026-10-07", 0),
        _row(2, "SELL", "AAA", 5_000, "CNC", "2026-10-07", 5),
        _row(3, "SELL", "AAA", 5_000, "CNC", "2026-10-08", 600),
    ])
    assert [r["charges"]["dp"] for r in rows] == [13.5, 0.0, 13.5]


def test_summary_has_range_today_and_total_rows():
    rows = cl.build_rows([
        _row(1, "BUY", "AAA", 10_000, "CNC", "2026-10-01", 0),
        _row(2, "SELL", "AAA", 10_000, None, "2026-10-05", 30),
        _row(3, "SELL", "BBB", 20_000, "CNC", "2026-10-08", 60),
    ])
    s = cl.summarize(rows, today="2026-10-08")
    h, t, tot = s["history"], s["today"], s["total"]
    assert (h["from"], h["to"], h["orders"]) == ("2026-10-01", "2026-10-07", 2)
    assert (t["from"], t["to"], t["orders"]) == ("2026-10-08", "2026-10-08", 1)
    assert tot["orders"] == 3 and (tot["from"], tot["to"]) == ("2026-10-01", "2026-10-08")
    assert abs(h["all_charges"] + t["all_charges"] - tot["all_charges"]) < 0.011
    assert tot["components"]["dp"] == 27.0 and tot["all_charges"] == s["all_charges_total"] > 0


def test_history_none_when_only_today_and_empty_ok():
    s = cl.summarize(cl.build_rows([_row(1, "BUY", "AAA", 10_000, "CNC", "2026-10-08")]), today="2026-10-08")
    assert s["history"] is None and s["today"]["orders"] == 1
    e = cl.summarize([], today="2026-10-08")
    assert e["history"] is None and e["total"]["all_charges"] == 0
