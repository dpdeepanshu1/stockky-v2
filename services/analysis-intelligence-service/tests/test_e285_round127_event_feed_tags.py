"""E285 round 127: the typed event feed tags a headline with a symbol. On 2 Nov the VM feed was ok:true with 0 events all morning because subscriptions are stored
as 'X.NS' (the headline token 'X' never equalled 'X.NS') and headlines name the company, not the ticker."""
import importlib.util
import os
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("event_feed_r127", os.path.join(_HERE, "..", "event", "event_feed.py"))
ef = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ef)

NOW = datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)


def _entry(title, minutes_ago=10):
    return {"title": title, "published": (NOW - timedelta(minutes=minutes_ago)).strftime("%a, %d %b %Y %H:%M:%S +0000")}


def _col(titles, symbols, names=None):
    return ef.FeedCollector(lambda: list(symbols), lambda url: ([_entry(t) for t in titles], 200), feeds=[("A", "http://a")], now_fn=lambda: NOW,
                            names_fn=(lambda: names) if names is not None else None)


def test_bare_symbol():
    assert ef.bare_symbol("bankindia.ns") == "BANKINDIA" and ef.bare_symbol("TCS.BO") == "TCS" and ef.bare_symbol("TCS") == "TCS" and ef.bare_symbol(None) == ""


def test_ns_subscriptions_now_match_a_ticker_in_the_headline():
    col = _col(["BANKINDIA wins order worth Rs 400 crore"], ["BANKINDIA.NS"])
    col.poll_once()
    r = col.response()
    assert [(e["symbol"], e["type"], e["polarity"]) for e in r["events"]] == [("BANKINDIA", "order", 1)] and r["ok"]


def test_company_name_in_the_headline_tags_the_symbol():
    names = {"KPRMILL.NS": "K.P.R. Mill Limited", "TATASTEEL.NS": "Tata Steel Limited", "BANKINDIA.NS": "Bank of India"}
    col = _col(["Tata Steel Q2 profit jumps 40%", "Tata Steel Ltd shares surge after upgrade"], ["TATASTEEL.NS", "KPRMILL.NS", "BANKINDIA.NS"], names)
    col.poll_once()
    assert {e["symbol"] for e in col.response()["events"]} == {"TATASTEEL"}


def test_longer_company_name_is_not_mistaken_for_a_shorter_one():
    names = {"BANKINDIA.NS": "Bank of India"}
    col = _col(["State Bank of India profit rises 12%", "Bank of India raises lending rate"], ["BANKINDIA.NS"], names)
    col.poll_once()
    evs = col.response()["events"]
    assert len(evs) == 1 and evs[0]["symbol"] == "BANKINDIA" and "State" not in evs[0]["headline"]


def test_unsubscribed_company_and_generic_names_never_tag():
    aliases = ef.name_aliases({"X.NS": "India Limited", "Y.NS": "Bank Ltd", "Z.NS": "ABC"})
    assert aliases == []
    assert ef.extract_symbol_by_name("Reliance Industries wins order", ef.name_aliases({"TATASTEEL.NS": "Tata Steel Limited"})) is None


def test_no_names_function_behaves_as_before():
    # round 179: with the bundled name list switched off, no names function still means no name tagging
    import os
    os.environ["EVENT_FEED_STATIC_NAMES"] = "0"
    try:
        col = _col(["Tata Steel Q2 profit jumps 40%"], ["TATASTEEL.NS"])
        col.poll_once()
        assert col.response()["events"] == []
    finally:
        os.environ.pop("EVENT_FEED_STATIC_NAMES", None)


def test_bundled_names_tag_a_subscribed_company_without_a_names_function():
    col = _col(["Tata Steel Q2 profit jumps 40%"], ["TATASTEEL.NS"])
    col.poll_once()
    assert [e["symbol"] for e in col.response()["events"]] == ["TATASTEEL"]


def test_bundled_names_never_tag_an_unsubscribed_symbol():
    col = _col(["Tata Steel Q2 profit jumps 40%"], ["SBIN.NS"])
    col.poll_once()
    assert col.response()["events"] == []


def test_bad_names_function_never_breaks_a_poll():
    def boom():
        raise RuntimeError("x")
    col = ef.FeedCollector(lambda: ["ABC.NS"], lambda url: ([_entry("ABC wins order")], 200), feeds=[("A", "http://a")], now_fn=lambda: NOW, names_fn=boom)
    col.poll_once()
    assert len(col.response()["events"]) == 1


def test_main_passes_the_cached_names_to_the_collector():
    src = open(os.path.join(_HERE, "..", "event", "main.py")).read()
    assert "names_fn=lambda: dict(_company_name_cache)" in src


def test_publisher_tail_does_not_make_a_sector_headline_a_results_event():
    import event_lite as el
    r = el.classify("Steel Prices Rise: Tata Steel, JSW Steel, SAIL Raise Rates Across Key Products - NDTV Profit")
    assert not (r["type"] == "results" and r["pol"] != 0)


def test_publisher_tail_is_stripped_but_a_real_results_headline_still_counts():
    import event_lite as el
    assert el.classify("Tata Steel Q2 profit rises 30% - NDTV Profit")["type"] == "results"


def test_multi_company_headline_keeps_no_sign():
    col = _col(["Steel Prices Rise: Tata Steel, JSW Steel, SAIL Raise Rates Across Key Products - NDTV Profit"], ["TATASTEEL.NS", "SAIL.NS", "JSWSTEEL.NS"])
    col.poll_once()
    evs = col.response()["events"]
    assert evs and all(e["polarity"] == 0 for e in evs)


def test_single_company_results_headline_keeps_its_sign():
    col = _col(["Tata Steel Q2 profit jumps 40%"], ["TATASTEEL.NS", "SAIL.NS"])
    col.poll_once()
    evs = col.response()["events"]
    assert evs and evs[0]["symbol"] == "TATASTEEL" and evs[0]["polarity"] == 1


def test_ipo_story_has_no_sign_and_is_not_an_order():
    import event_lite as el
    for h in ["Coal India's Mahanadi Coalfields IPO may launch in November, raise up to $1 billion - Moneycontrol",
              "Coal India arm plans IPO to raise Rs 5,000 crore"]:
        r = el.classify(h)
        assert r["pol"] == 0, (h, r)


def test_a_real_order_win_is_still_positive():
    import event_lite as el
    r = el.classify("Coal India wins order worth Rs 500 crore")
    assert r["pol"] == 1
