"""E272 round 6: the after-hours scan as a reader over the typed event feed, the veto row, and the new cadence.
Plain-function runner: python3 tests/test_e272_afterhours_event_reader.py (also collected by pytest). No sqlalchemy / httpx needed: event_reader is loaded by path,
`_afterhours_next_sleep_seconds` is cut out of auto_pilot.py and run with a stand-in config, the wiring is checked by source text."""
from __future__ import annotations

import ast
import importlib.util
import os
import sys
import types
from datetime import datetime, time
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
IST = ZoneInfo("Asia/Kolkata")


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


er = _load("e272_event_reader", "watchlist_engine/event_reader.py")
_AP = open(os.path.join(ROOT, "execution", "auto_pilot.py"), encoding="utf-8").read()
_AS = open(os.path.join(ROOT, "watchlist_engine", "afterhours_scan.py"), encoding="utf-8").read()


def ev(sym, typ, pol, src="Moneycontrol", ts=1.0, head="h"):
    return {"symbol": sym, "type": typ, "polarity": pol, "source": src, "published_ts": ts, "headline": head}


# ---- tag round trip -------------------------------------------------------------------------------------------------
def test_tag_round_trip_and_length():
    t = er.encode_tag("Moneycontrol-google", "results", 1, 0.85)
    assert t == "Moneycontrol-google|ev=results|p=+1|s=0.85"
    d = er.decode_tag(t)
    assert d == {"source": "Moneycontrol-google", "type": "results", "polarity": 1, "strength": 0.85}
    long = er.encode_tag("X" * 200, "regulatory", -1, 0.9)
    assert len(long) <= 64 and er.decode_tag(long)["polarity"] == -1 and er.decode_tag(long)["type"] == "regulatory"


def test_legacy_and_garbage_tags_decode_to_none():
    assert er.decode_tag("Moneycontrol") == {"source": "Moneycontrol", "type": None, "polarity": None, "strength": None}
    assert er.decode_tag(None)["polarity"] is None
    assert er.is_veto("LiveMint|ev=order|p=-1|s=0.65") and not er.is_veto("LiveMint|ev=order|p=+1|s=0.65") and not er.is_veto("NSE-bulk-deals")
    assert er.decode_tag("a|p=banana|s=nan")["polarity"] is None


# ---- hits ---------------------------------------------------------------------------------------------------------------
def test_unknown_polarity_never_becomes_a_hit():
    assert er.hits_from_events([ev("ABC", "results", 0)]) == {}


def test_positive_score_is_strength_at_announce_time_not_read_time():
    h = er.hits_from_events([dict(ev("ABC", "results", 1), strength=0.0001)])["ABC"]     # the feed's read-time strength decayed to ~0 overnight
    assert h["score"] == 70.0 and h["polarity"] == 1 and h["catalyst_type"] == "results"   # single source: capped at 0.70, not 0.85
    assert h["source"].startswith("Moneycontrol|ev=results|p=+1|s=0.7")


def test_two_independent_sources_lift_strength_one_publisher_does_not():
    two = er.hits_from_events([ev("ABC", "results", 1, "Moneycontrol"), ev("ABC", "results", 1, "LiveMint", head="h2")])["ABC"]
    assert two["strength"] == 0.977 and two["score"] == 97.7
    same = er.hits_from_events([ev("ABC", "results", 1, "Moneycontrol"), ev("ABC", "results", 1, "Moneycontrol-google", head="h2")])["ABC"]
    assert same["strength"] == 0.7                       # one publisher counted twice is still one source


def test_a_single_negative_source_keeps_full_weight_and_scores_zero():
    h = er.hits_from_events([ev("XYZ", "regulatory", -1, "NDTVProfit")])["XYZ"]
    assert h["score"] == 0.0 and h["polarity"] == -1 and h["strength"] == 0.9 and er.is_veto(h["source"])


def test_veto_beats_positive_for_the_same_symbol():
    h = er.hits_from_events([ev("XYZ", "order", 1, "LiveMint"), ev("XYZ", "regulatory", -1, "NDTVProfit")])["XYZ"]
    assert h["polarity"] == -1


def test_known_symbol_filter_and_bad_input_never_raise():
    assert er.hits_from_events([ev("ZZZ", "order", 1)], known_symbols=["ABC"]) == {}
    assert er.hits_from_events(None) == {} and er.hits_from_events([None, 5, "x", {"symbol": None}]) == {}
    assert er.hits_from_events([{"symbol": "A", "polarity": "x"}]) == {}


def test_catalyst_type_keeps_the_legacy_names():
    g = lambda t: er.hits_from_events([ev("A", t, 1)])["A"]["catalyst_type"]
    assert g("results") == "results" and g("deal") == "bulk_block" and g("management") == "board" and g("order") == "news" and g("rating") == "news"


# ---- merge ----------------------------------------------------------------------------------------------------------
def test_merge_typed_over_keyword_keeps_the_higher_score_and_the_tag():
    best = {"ABC": {"score": 90.0, "headline": "kw", "catalyst_type": "news", "source": "Moneycontrol"}}
    er.merge_into(best, er.hits_from_events([ev("ABC", "order", 1, "LiveMint")]))
    assert best["ABC"]["score"] == 90.0 and er.decode_tag(best["ABC"]["source"])["type"] == "order"


def test_merge_veto_replaces_keywords_and_a_stored_veto_stands():
    best = {"ABC": {"score": 90.0, "headline": "kw", "catalyst_type": "news", "source": "Moneycontrol"}}
    veto = er.hits_from_events([ev("ABC", "regulatory", -1, "LiveMint")])
    er.merge_into(best, veto)
    assert best["ABC"]["score"] == 0.0 and best["ABC"]["polarity"] == -1
    er.merge_into(best, er.hits_from_events([ev("ABC", "results", 1, "LiveMint")]))
    assert best["ABC"]["polarity"] == -1


# ---- fetch fails safe -------------------------------------------------------------------------------------------------------
def test_fetch_active_returns_none_when_unreachable():
    assert er.fetch_active("http://127.0.0.1:9", 0.3) is None


# ---- type weights stay equal to the feed's own table ---------------------------------------------------------------------------
def test_type_weight_equals_the_event_feed_table():
    src = open(os.path.join(ROOT, "..", "analysis-intelligence-service", "event", "event_feed.py"), encoding="utf-8", errors="replace").read()
    line = next(l for l in src.splitlines() if l.startswith("TYPE_WEIGHT"))
    assert ast.literal_eval(line.split("=", 1)[1].strip()) == er.TYPE_WEIGHT


# ---- cadence --------------------------------------------------------------------------------------------------------------
def _cad(h, m):
    return er.cadence_seconds(time(h, m), time(15, 30), time(20, 0), time(8, 0), time(9, 0))


def test_cadence_table():
    assert _cad(15, 30) == 1800 and _cad(19, 59) == 1800
    assert _cad(20, 0) == 7200 and _cad(23, 0) == 7200 and _cad(2, 0) == 7200 and _cad(7, 59) == 7200
    assert _cad(8, 0) == 600 and _cad(8, 59) == 600
    assert _cad(9, 0) == 86400 and _cad(12, 0) == 86400 and _cad(15, 29) == 86400
    assert er.cadence_seconds(time(8, 30), time(15, 30), time(20, 0), time(8, 0), time(9, 0), ramp_s=5) == 30     # never below 30 s


def _sleep_fn(cfg):
    """_afterhours_next_sleep_seconds and its helper cut out of auto_pilot.py, run with a stand-in config and the real tz_utils / event_reader."""
    tree = ast.parse(_AP)
    names = {"_afterhours_next_sleep_seconds", "_afterhours_start_str"}
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    pkg = types.ModuleType("watchlist_engine")
    pkg.event_reader = er
    sys.modules["watchlist_engine"] = pkg
    sys.modules["watchlist_engine.event_reader"] = er
    import tz_utils
    ns = {"config": cfg, "parse_hhmm": tz_utils.parse_hhmm}
    exec(compile(ast.Module(body=fns, type_ignores=[]), "auto_pilot.py", "exec"), ns)
    return ns["_afterhours_next_sleep_seconds"], ns["_afterhours_start_str"]


def _cfg(v2):
    return types.SimpleNamespace(
        AFTERHOURS_CADENCE_V2=v2, AFTERHOURS_V2_START_IST="15:30", AFTERHOURS_V2_EVENING_END_IST="20:00", AFTERHOURS_V2_EVENING_INTERVAL_SECONDS=1800,
        AFTERHOURS_V2_NIGHT_INTERVAL_SECONDS=7200, AFTERHOURS_V2_RAMP_INTERVAL_SECONDS=600, AFTERHOURS_SCAN_START_IST="15:45",
        AFTERHOURS_SCAN_RAMP_START_IST="08:00", AFTERHOURS_SCAN_RAMP_END_IST="09:00", AFTERHOURS_FINALIZE_TIME_IST="08:45",
        AFTERHOURS_SCAN_RAMP_INTERVAL_SECONDS=1800, AFTERHOURS_SCAN_OFFHOURS_INTERVAL_SECONDS=21600)


def _at(h, m=0, s=0):
    return datetime(2026, 11, 2, h, m, s, tzinfo=IST)


def test_sleep_v2_evening_night_ramp_and_boundaries():
    sleep, start = _sleep_fn(_cfg(True))
    assert start() == "15:30"
    assert sleep(_at(15, 30)) == 1800                 # evening: every 30 min
    assert sleep(_at(19, 45)) == 15 * 60              # capped at 20:00 so the night interval starts on time
    assert sleep(_at(20, 0)) == 7200                  # night: every 2 h
    assert sleep(_at(2, 0)) == 7200
    assert sleep(_at(7, 30)) == 30 * 60               # capped at the 08:00 ramp start
    assert sleep(_at(8, 0)) == 600                    # ramp: every 10 min
    assert sleep(_at(8, 40)) == 5 * 60                # capped at the 08:45 finalize
    assert sleep(_at(9, 0)) == 6 * 3600 + 30 * 60     # session: sleeps to the 15:30 start
    assert sleep(_at(8, 59, 50)) == 30


def test_sleep_v2_off_is_the_old_cadence():
    sleep, start = _sleep_fn(_cfg(False))
    assert start() == "15:45"
    assert sleep(_at(8, 0)) == 30 * 60 and sleep(_at(15, 45)) == 6 * 3600 and sleep(_at(9, 0)) == 6 * 3600 + 45 * 60


# ---- wiring (source text) ------------------------------------------------------------------------------------------------
def test_scan_wiring():
    assert "AFTERHOURS_EVENT_FEED_MODE" in _AS and "event_reader.fetch_active" in _AS and "event_reader.merge_into(best, feed_hits)" in _AS
    assert _AS.index("event_reader.fetch_active") < _AS.index("for feed in ([] if (feed_mode == \"feed\" and feed_ok) else _RSS_FEEDS):")
    assert "elif _upsert_wins(hit, existing_row):" in _AS and "catalyst_source=hit[\"source\"]" in _AS
    # the old keyword scan is only skipped when the feed answered in "feed" mode
    assert "feed_mode == \"feed\" and feed_ok" in _AS


def test_pre_pick_veto_wiring():
    assert "def _decode_event_tag" in _AP               # defined once at module level; looked up when the pre-pick runs
    veto = _AP.index("vetoed by a negative after-hours event")
    low = _AP.index("if row.priority_score < config.AFTERHOURS_SCAN_MIN_INJECT_SCORE:\n                row.consumed = True")
    assert veto < low                                                      # the veto test runs before the score test
    assert "db.add(models.TradeCandidate(" in _AP[veto:]                    # injection comes after it


def test_config_defaults_in_source():
    c = open(os.path.join(ROOT, "config.py"), encoding="utf-8").read()
    assert 'or "both").strip().lower()' in c and '"AFTERHOURS_CADENCE_V2") or "true"' in c
    assert '"AFTERHOURS_V2_START_IST", "15:30"' in c and '"AFTERHOURS_V2_EVENING_END_IST", "20:00"' in c


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
            n += 1
    print(f"{n} passed")
