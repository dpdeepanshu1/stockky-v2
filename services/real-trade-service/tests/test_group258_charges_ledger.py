"""group 258: real-trade-service cumulative brokerage report (charges_ledger.py). Pure helpers only."""
from datetime import datetime, timedelta, timezone

import pytest

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
    # group 262: delivery STT is 0.1 % on BOTH legs; DP is Rs 12.50 + 18 % GST on the sell only.
    assert buy["brokerage"] == 0 and abs(buy["charges"]["stt"] - 10.0) < 1e-9 and buy["charges"]["stamp"] > 0 and buy["charges"]["dp"] == 0
    assert sell["brokerage"] == 0 and abs(sell["charges"]["stt"] - 10.0) < 1e-9 and sell["charges"]["stamp"] == 0
    assert sell["charges"]["dp"] == pytest.approx(14.75) and cl.dp_charge_rs() == pytest.approx(14.75)


def test_dp_is_billed_once_per_scrip_per_day():
    rows = cl.build_rows([
        _row(1, "SELL", "AAA", 5_000, "CNC", "2026-10-07", 0),
        _row(2, "SELL", "AAA", 5_000, "CNC", "2026-10-07", 5),
        _row(3, "SELL", "AAA", 5_000, "CNC", "2026-10-08", 600),
    ])
    assert [r["charges"]["dp"] for r in rows] == [pytest.approx(14.75), 0.0, pytest.approx(14.75)]


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
    assert tot["components"]["dp"] == pytest.approx(29.5) and tot["all_charges"] == s["all_charges_total"] > 0


def test_history_none_when_only_today_and_empty_ok():
    s = cl.summarize(cl.build_rows([_row(1, "BUY", "AAA", 10_000, "CNC", "2026-10-08")]), today="2026-10-08")
    assert s["history"] is None and s["today"]["orders"] == 1
    e = cl.summarize([], today="2026-10-08")
    assert e["history"] is None and e["total"]["all_charges"] == 0


# -- group 262: corrected rate card, DP rule, unpriced sells --
def test_intraday_stt_is_sell_side_only():
    buy = cl.order_charges(10_000, "BUY", "INTRADAY")
    sell = cl.order_charges(10_000, "SELL", "INTRADAY")
    assert buy["stt"] == 0.0 and sell["stt"] == pytest.approx(2.5)
    assert buy["stamp"] == pytest.approx(0.3) and sell["stamp"] == 0.0


def test_delivery_buy_pays_stt_and_stamp_but_no_dp():
    c = cl.order_charges(2_613.15, "BUY", "CNC")          # the AEGISVOPAK buy on the Charges screen showed STT 0
    assert c["stt"] == pytest.approx(2.61315) and c["stamp"] == pytest.approx(0.39197, abs=1e-4)


def test_gst_covers_brokerage_exchange_and_sebi_but_not_stt_or_stamp():
    c = cl.order_charges(10_000, "SELL", "INTRADAY")
    assert c["gst"] == pytest.approx((c["brokerage"] + c["exchange"] + c["sebi"]) * 0.18)
    assert c["exchange"] == pytest.approx(10_000 * (config.EXCHANGE_TXN_PCT + config.IPFT_PCT) / 100.0)


def test_rate_card_matches_dhan_published_rates():
    assert config.EXCHANGE_TXN_PCT == 0.00297 and config.IPFT_PCT == 0.0001 and config.DP_CHARGE_FLAT == 12.5
    assert config.STT_DELIVERY_PCT_PER_LEG == 0.10 and config.STT_INTRADAY_SELL_PCT == 0.025


def _q(i, side, symbol, value, qty, minutes, product="CNC", day="2026-10-07"):
    return {**_o(i, side, symbol, value, product, minutes), "qty": qty, "day": day}


def test_dp_is_not_charged_when_the_sell_only_closes_shares_bought_the_same_day():
    rows = cl.build_rows([_q(1, "BUY", "AAA", 1_000, 10, 0), _q(2, "SELL", "AAA", 1_050, 10, 30)])
    assert rows[1]["charges"]["dp"] == 0.0


def test_dp_is_charged_when_the_sell_closes_older_shares():
    rows = cl.build_rows([_q(1, "BUY", "AAA", 1_000, 10, 0, day="2026-10-06"), _q(2, "SELL", "AAA", 1_050, 10, 30)])
    assert rows[1]["charges"]["dp"] == pytest.approx(14.75)


def test_dp_is_charged_on_the_part_sold_from_demat_when_a_sell_is_larger_than_todays_buy():
    rows = cl.build_rows([_q(1, "BUY", "AAA", 400, 4, 0), _q(2, "SELL", "AAA", 1_000, 10, 30)])
    assert rows[1]["charges"]["dp"] == pytest.approx(14.75)


def test_a_rebuy_after_a_demat_sell_does_not_cancel_that_sells_dp():
    rows = cl.build_rows([_q(1, "SELL", "AAA", 2_394, 4, 0), _q(2, "BUY", "AAA", 2_464, 4, 5)])
    assert rows[0]["charges"]["dp"] == pytest.approx(14.75) and rows[1]["charges"]["dp"] == 0.0


def test_unpriced_sell_is_estimated_from_the_last_buy_not_dropped():
    rows = cl.build_rows([_q(1, "BUY", "AAA", 1_000, 10, 0, day="2026-10-06"), _q(2, "SELL", "AAA", 0, 10, 30)])
    sell = [r for r in rows if r["side"] == "SELL"][0]
    assert sell["estimated"] is True and sell["value"] == pytest.approx(1_000)
    assert sell["charges"]["dp"] == pytest.approx(14.75) and sell["charges"]["stt"] == pytest.approx(1.0)
    assert cl.summarize(rows, today="2026-10-07")["orders_estimated"] == 1


def test_unpriced_sell_with_no_known_buy_is_still_skipped():
    assert cl.build_rows([_q(2, "SELL", "AAA", 0, 10, 0)]) == []


def test_todays_screen_totals_reproduce_with_the_corrected_card():
    """The 8 delivery trades + 6 sells on the group-261 screenshot, priced with the corrected card."""
    sells = [(4, 112.94), (1, 2315.0), (1, 1310.5), (1, 287.7), (4, 598.6), (1, 38.3)]
    buys = [(9, 290.35), (6, 290.60), (4, 615.95), (1, 299.25), (1, 40.23)]
    stt = sum(q * p for q, p in sells + buys) * 0.001
    assert stt == pytest.approx(sum(cl.order_charges(q * p, "SELL", "CNC")["stt"] for q, p in sells)
                                + sum(cl.order_charges(q * p, "BUY", "CNC")["stt"] for q, p in buys))
    assert stt > 6.8                                      # the old card showed 6.80 (sell-side STT only)
