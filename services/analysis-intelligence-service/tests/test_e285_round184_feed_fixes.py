"""Round 184 (follow-up to round 183): three fixes found on the 4 Nov live headlines.
1. a trailing publisher name (" - NDTV Profit") is not part of the news ("Profit" made 4 stock-picks lines 'results' events);
2. IndiGo / InterGlobe Aviation are known names (a two-company headline naming IndiGo must lose its sign), Indigo Paints stays a different company;
3. 'X rise ... as crude oil falls' is never -1 (a falling cost is good news)."""
import importlib.util
import os
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("event_feed_r184", os.path.join(_HERE, "..", "event", "event_feed.py"))
ef = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ef)

NOW = datetime(2026, 11, 4, 5, 0, tzinfo=timezone.utc)


def _entry(title, minutes_ago=10):
    return {"title": title, "published": (NOW - timedelta(minutes=minutes_ago)).strftime("%a, %d %b %Y %H:%M:%S +0000")}


def _col(titles, symbols):
    return ef.FeedCollector(lambda: list(symbols), lambda url: ([_entry(t) for t in titles], 200), feeds=[("A", "http://a")], now_fn=lambda: NOW)


# ── 1. publisher suffix ──────────────────────────────────────────────────────────────────────────────────────────
def test_strip_source_known_publishers():
    assert ef.strip_source("Reliance shares rise 2% - NDTV Profit") == "Reliance shares rise 2%"
    assert ef.strip_source("Tata Steel Q2 results | Moneycontrol") == "Tata Steel Q2 results"
    assert ef.strip_source("Infosys wins order - livemint.com") == "Infosys wins order"
    assert ef.strip_source("SBI profit rises – The Economic Times") == "SBI profit rises"


def test_strip_source_leaves_real_text_alone():
    assert ef.strip_source("Tata Steel - Q2 profit jumps 40%") == "Tata Steel - Q2 profit jumps 40%"
    assert ef.strip_source("NDTV Profit") == "NDTV Profit"          # a title that is only a publisher is kept
    assert ef.strip_source(None) == "" and ef.strip_source(123) == "123"


def test_publisher_word_profit_is_no_longer_a_results_event():
    for h in ("Reliance shares rise 2% - NDTV Profit", "Stock Picks Today: SBI, BEL, NSE - NDTV Profit"):
        r = ef.classify(h)
        assert r["type"] == "other" and r["polarity"] == 0, (h, r)


def test_real_results_headline_with_publisher_still_results():
    r = ef.classify("Tata Steel Q2 profit jumps 40% - NDTV Profit")
    assert r["type"] == "results" and r["polarity"] == 1


def test_collector_keeps_the_original_headline_text():
    col = _col(["BEL shares rise 3% - NDTV Profit"], ["BEL.NS"])
    col.poll_once()
    ev = col.response()["events"][0]
    assert ev["headline"].endswith("NDTV Profit") and ev["type"] == "other"


# ── 2. IndiGo ─────────────────────────────────────────────────────────────────────────────────────────────────────
def test_indigo_names_in_company_file():
    al = dict(ef.static_aliases())
    assert al.get("interglobe aviation") == "INDIGO" and al.get("indigo paints") == "INDIGOPNTS"
    assert "indigo" not in al                                       # bare word would also match Indigo Paints


def test_two_company_headline_with_indigo_counts_two_and_loses_its_sign():
    al = ef.static_aliases()
    known = ["ASIANPAINT.NS", "INDIGO.NS", "INDIGOPNTS.NS"]
    h = "Asian Paints, IndiGo rise up to 4% as crude oil falls"
    assert ef.count_named_symbols(h, known, al) == 2
    assert ef.classify(h)["polarity"] >= 0
    h2 = "Asian Paints, IndiGo surges as crude oil falls"       # a signed two-company line: the collector must drop the sign
    assert ef.classify(h2)["polarity"] == 1
    col = _col([h2], known)
    col.poll_once()
    ev = col.response()["events"][0]
    assert ev["polarity"] == 0 and ev.get("multi") is True


def test_indigo_paints_is_not_indigo_airline():
    al = ef.static_aliases()
    assert ef.extract_symbol_by_name("Indigo Paints Q2 profit jumps 20%", al) == "INDIGOPNTS"
    assert ef.extract_symbol_by_name("IndiGo shares jump on record profit", al) == "INDIGO"
    assert ef.extract_symbol_by_name("IndiGo Paints expands", al) != "INDIGO"
    assert ef.count_named_symbols("Indigo Paints Q2 profit jumps 20%", ["INDIGO", "INDIGOPNTS"], al) == 1


def test_nested_name_counts_once():
    al = ef.name_aliases({"SBIN": "State Bank of India", "BANKINDIA": "Bank of India"})
    assert ef.count_named_symbols("State Bank of India profit rises", ["SBIN", "BANKINDIA"], al) == 1
    assert ef.count_named_symbols("State Bank of India and Bank of India profit rise", ["SBIN", "BANKINDIA"], al) == 2


# ── 3. falling cost is not bad news ──────────────────────────────────────────────────────────────────────────────
def test_cost_subject_falling_verb_is_not_negative():
    for h in ("Asian Paints, IndiGo rise up to 4% as crude oil falls", "Asian Paints rises as crude oil drops", "Paint stocks gain as raw material prices slump",
              "Airline shares rise as crude falls"):
        assert ef.classify(h)["polarity"] >= 0, h


def test_other_falls_still_negative():
    assert ef.classify("Stock falls 5% after probe")["polarity"] == -1
    assert ef.classify("Profit falls 10% in Q2")["polarity"] == -1
    assert ef.classify("Shares plunge on fraud allegations")["polarity"] == -1


def test_old_cost_after_verb_rule_unchanged():
    assert ef.classify("Paint makers gain on fall in input costs")["polarity"] >= 0
