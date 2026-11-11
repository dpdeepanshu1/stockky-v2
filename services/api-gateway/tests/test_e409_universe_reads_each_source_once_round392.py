"""E409: _build_scan_universe reads each dynamic source (movers, bulk deals, 52w, news, IPOs, events) once per build, and the priority order is unchanged."""
from __future__ import annotations

from test_main_universe import S, gw, log, ub  # noqa: F401  (ub / log are fixtures)

DYN = ("_get_momentum_movers", "_get_bulk_deal_symbols", "_get_52w_extreme_symbols", "_get_news_mentioned_symbols", "_get_recent_ipos", "_get_event_symbols")


def test_every_dynamic_source_is_called_exactly_once(ub):
    ub.securities = S(300, "SEC")
    ub.movers, ub.bulk, ub.w52, ub.news, ub.ipos, ub.events = ["MOV1"], ["BULK1"], ["W521"], ["NEWS1"], ["IPO1"], ["EVT1"]
    out = gw._build_scan_universe()
    for name in DYN:
        assert ub.calls.count(name) == 1, name
    assert {"MOV1", "BULK1", "W521", "NEWS1", "IPO1", "EVT1"} <= set(out)


def test_priority_order_is_bulk_52w_movers_news_events_ipos(ub):
    ub.securities = S(300, "SEC")
    ub.movers, ub.bulk, ub.w52, ub.news, ub.ipos, ub.events = ["MOV1"], ["BULK1"], ["W521"], ["NEWS1"], ["IPO1"], ["EVT1"]
    out = gw._build_scan_universe()
    assert out[:6] == ["BULK1", "W521", "MOV1", "NEWS1", "EVT1", "IPO1"]


def test_a_symbol_in_two_sources_is_listed_once_at_its_first_place(ub):
    ub.securities = S(300, "SEC")
    ub.bulk, ub.movers, ub.news = ["AAA1"], ["AAA1", "MOV1"], ["MOV1"]
    out = gw._build_scan_universe()
    assert out[:2] == ["AAA1", "MOV1"] and out.count("AAA1") == 1 and out.count("MOV1") == 1


def test_a_source_that_raises_is_skipped_and_the_rest_still_lead(ub):
    ub.securities = S(300, "SEC")
    ub.bulk, ub.news, ub.events = ["BULK1"], ["NEWS1"], ["EVT1"]
    ub.raises["_get_news_mentioned_symbols"] = RuntimeError("rss down")
    out = gw._build_scan_universe()
    assert out[:2] == ["BULK1", "EVT1"] and "NEWS1" not in out
    assert ub.calls.count("_get_news_mentioned_symbols") == 1
