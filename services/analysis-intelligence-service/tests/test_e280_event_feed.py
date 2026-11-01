"""tests/test_e280_event_feed.py - event/event_feed.py (pure module; plain-function style).

    python3 -m pytest tests/test_e280_event_feed.py -q       or       python3 tests/test_e280_event_feed.py
"""
import ast
import importlib.util
import os
import time
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("event_feed_under_test", os.path.join(_HERE, "..", "event", "event_feed.py"))
ef = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ef)

NOW = datetime(2026, 10, 31, 10, 0, tzinfo=timezone.utc)


def _rfc(dt):
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")


def _entry(title, minutes_ago=10, published=True):
    d = {"title": title}
    if published:
        d["published"] = _rfc(NOW - timedelta(minutes=minutes_ago))
    return d


def test_classify_order_win_is_positive():
    c = ef.classify("Tata Motors wins order worth Rs 500 crore")
    assert (c["type"], c["polarity"]) == ("order", 1)


def test_regulatory_negative_beats_win_words():
    c = ef.classify("ABC wins order but faces SEBI probe")
    assert (c["type"], c["polarity"]) == ("regulatory", -1)


def test_dismissed_probe_is_not_negative():
    c = ef.classify("SEBI probe dismissed against ABC")
    assert c["type"] == "regulatory" and c["polarity"] != -1


def test_cost_context_not_negative():
    assert ef.classify("Paint maker profit rises as fall in input costs helps margins")["polarity"] == 1
    assert ef.classify("ABC profit falls 20% in Q3")["polarity"] == -1


def test_loss_narrows_positive():
    c = ef.classify("XYZ loss narrows in Q2")
    assert (c["type"], c["polarity"]) == ("results", 1)
    assert ef.classify("XYZ loss widens in Q2")["polarity"] == -1


def test_extract_symbol_only_known():
    assert ef.extract_symbol("RELIANCE wins big order", ["RELIANCE", "TCS"]) == "RELIANCE"
    assert ef.extract_symbol("UNKNOWN wins big order", ["RELIANCE"]) is None
    assert ef.extract_symbol("anything", []) is None


def test_parse_published_variants():
    assert ef.parse_published({"published": _rfc(NOW)}) == NOW
    assert ef.parse_published({"published_parsed": time.struct_time((2026, 10, 31, 10, 0, 0, 0, 0, 0))}) == NOW
    assert ef.parse_published({"published": "2026-10-31T10:00:00Z"}) == NOW
    assert ef.parse_published({"title": "x"}) is None and ef.parse_published({"published": "junk"}) is None


def test_strength_decays_and_neutral_is_zero():
    fresh = ef.strength("results", 1, 0)
    old = ef.strength("results", 1, 7200)
    assert fresh > old > 0 and ef.strength("results", 0, 0) == 0.0
    assert ef.strength("regulatory", -1, 0, source_weight=5) <= 1.0
    assert ef.strength("results", 1, "not a number") == fresh          # junk age never raises


def test_dedupe_within_6h_only():
    a = ef.make_event("ABC", "ABC wins order", NOW - timedelta(hours=9), "s", NOW)
    b = ef.make_event("ABC", "ABC wins order", NOW - timedelta(hours=2), "s", NOW)
    c = ef.make_event("ABC", "ABC wins order", NOW - timedelta(hours=1), "s", NOW)
    out = ef.dedupe([c, b, a])
    assert len(out) == 2 and out[0]["published_ts"] == a["published_ts"] and out[1]["published_ts"] == b["published_ts"]


def test_float_guard_never_raises_on_objects():
    assert ef._f({"a": 1}) == 0.0 and ef._f(object(), 7.0) == 7.0 and ef._f(float("nan"), 2.0) == 2.0 and ef._f("3.5") == 3.5


def test_build_response_failsafe():
    r = ef.build_response([{"symbol": "ABC", "published_ts": {"bad": 1}, "type": object(), "polarity": {}}], {"s": {"ok": True, "last_delay_s": {"x": 1}}}, now=NOW)
    assert isinstance(r, dict) and "ok" in r and "events" in r
    r = ef.build_response(None, None, now=NOW)
    assert r["ok"] is False and r["events"] == []


def test_build_response_filters_symbol_age_and_sorts():
    evs = [ef.make_event("ABC", "ABC profit jumps in Q2", NOW - timedelta(minutes=5), "s", NOW),
           ef.make_event("XYZ", "XYZ wins order", NOW - timedelta(minutes=5), "s", NOW),
           ef.make_event("ABC", "ABC wins order", NOW - timedelta(hours=30), "s", NOW)]
    r = ef.build_response(evs, {"s": {"ok": True, "last_delay_s": 300}}, symbol="abc", now=NOW)
    assert r["ok"] and r["count"] == 1 and r["events"][0]["symbol"] == "ABC" and r["last_delay_s"] == 300
    assert "published_ts" not in r["events"][0]


def _collector(fetch, symbols=("ABC", "XYZ"), cooldown=1800):
    return ef.FeedCollector(lambda: list(symbols), fetch, feeds=[("A", "http://a"), ("B", "http://b")], now_fn=lambda: NOW, cooldown_s=cooldown)


def test_collector_collects_typed_events_and_drops_undated_and_unknown():
    entries = [_entry("ABC profit jumps in Q2"), _entry("ABC wins order", published=False), _entry("NOPE wins order"), _entry("XYZ SEBI probe launched", 20)]
    col = _collector(lambda url: (entries if url.endswith("a") else [], 200))
    col.poll_once()
    r = col.response()
    kinds = sorted((e["symbol"], e["type"], e["polarity"]) for e in r["events"])
    assert kinds == [("ABC", "results", 1), ("XYZ", "regulatory", -1)], kinds
    assert r["ok"] and r["healthy_sources"] == ["A"] and r["last_delay_s"] == 600


def test_collector_all_fail_gives_ok_false():
    def boom(url):
        raise RuntimeError("down")
    col = _collector(boom)
    col.poll_once()
    r = col.response()
    assert r["ok"] is False and r["events"] == [] and all(not s["ok"] for s in r["sources"].values())


def test_collector_block_cooldown_and_snapshot():
    calls = []
    snaps = []

    def fetch(url):
        calls.append(url)
        return ([], 403) if url.endswith("a") else ([_entry("ABC wins order")], 200)
    col = ef.FeedCollector(lambda: ["ABC"], fetch, feeds=[("A", "http://a"), ("B", "http://b")], now_fn=lambda: NOW, cooldown_s=1800, snapshot_fn=snaps.append)
    col.poll_once()
    col.poll_once()
    assert calls.count("http://a") == 1 and calls.count("http://b") == 2          # blocked source is left alone
    assert col.response()["sources"]["A"]["status"] == "blocked_cooldown" and snaps and snaps[-1]["sources"]["B"]["ok"]


def test_collector_dedupes_across_polls_and_uses_extra_symbols(monkeypatch=None):
    os.environ["EVENT_FEED_EXTRA_SYMBOLS"] = "EXTRA1, extra2"
    try:
        col = _collector(lambda url: ([_entry("EXTRA2 wins order")] if url.endswith("a") else [], 200), symbols=())
        col.poll_once()
        col.poll_once()
        assert [e["symbol"] for e in col.response()["events"]] == ["EXTRA2"]
    finally:
        os.environ.pop("EVENT_FEED_EXTRA_SYMBOLS", None)


def test_collector_cadence_loop_is_a_daemon_and_disabled_by_env():
    os.environ["EVENT_FEED_ENABLED"] = "0"
    try:
        assert ef.start_collector(lambda: []) is None
    finally:
        os.environ.pop("EVENT_FEED_ENABLED", None)


def test_main_routes_ordered_and_wired():
    src = open(os.path.join(_HERE, "..", "event", "main.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    order = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef):
            for d in n.decorator_list:
                if isinstance(d, ast.Call) and d.args and isinstance(d.args[0], ast.Constant):
                    order.append(d.args[0].value)
    assert order.index("/events/active") < order.index("/events/{symbol}")
    assert order.index("/events/{symbol}/active") < order.index("/events/{symbol}")
    assert order.index("/events/raw-feed") < order.index("/events/{symbol}")
    assert "event_feed.py" in src and "start_collector" in src and '"pytest" not in' in src


def test_feeds_have_unique_names_and_https_urls():
    names = [n for n, _ in ef.FEEDS]
    assert len(names) == len(set(names)) and all(u.startswith("https://") for _, u in ef.FEEDS)


if __name__ == "__main__":
    n = 0
    for k, f in sorted(globals().items()):
        if k.startswith("test_") and callable(f):
            f(); n += 1; print("ok", k)
    print(n, "passed")
