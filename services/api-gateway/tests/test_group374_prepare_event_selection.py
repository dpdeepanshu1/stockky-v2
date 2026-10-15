"""group 374: the PREPARE refresh is event-driven (LIVE). buy_sniper.select_prepare_event_symbols picks rows OUTSIDE the score band whose daily structure
shows a P9 event; data_feed.find_prepare_to_buy_candidates adds them. Plain asserts; runs with pytest or `python3 tests/test_group374_...py`."""
from __future__ import annotations

import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = os.path.dirname(_HERE)
_ENV = ("SNIPER_PREPARE_STRUCTURE_MODE", "SNIPER_PREPARE_EVENT_MARGIN", "SNIPER_PREPARE_EVENT_MAX", "STRUCTURE_MAX_AGE_DAYS", "STRUCTURE_MAX_PRICE_DEV_PCT")


def bs(**env):
    for k in _ENV:
        os.environ.pop(k, None)
    os.environ.update({k: str(v) for k, v in env.items()})
    spec = importlib.util.spec_from_file_location("buy_sniper_t374", os.path.join(_SERVICE, "buy_sniper.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _score(row):
    return float(row.get("conviction") or 0)


def _row(score=55, **kw):
    d = {"ms_quality": "ok", "ms_price": 100.0, "ms_atr": 2.0, "ms_trend": "bullish", "ms_bos_dir": "bullish", "ms_bos_bars": 1, "ms_bos_failed": False,
         "decision": "WAIT", "price": 100.0, "conviction": score}
    d.update(kw)
    return d


def test_event_row_below_the_band_is_selected_with_its_triggers():
    m = bs()
    out = m.select_prepare_event_symbols({"A": _row()}, set(), _score, 58.0)
    assert out == [("A", ["bos_near"])]


def test_default_mode_is_live_and_stale_log_means_live():
    assert bs()._prep_mode() == "live"
    assert bs(SNIPER_PREPARE_STRUCTURE_MODE="log")._prep_mode() == "live"
    assert bs(SNIPER_PREPARE_STRUCTURE_MODE="off")._prep_mode() == "off"


def test_off_selects_nothing():
    m = bs(SNIPER_PREPARE_STRUCTURE_MODE="off")
    assert m.select_prepare_event_symbols({"A": _row()}, set(), _score, 58.0) == []


def test_score_floor_uses_the_margin():
    m = bs()
    rows = {"LOW": _row(49), "OK": _row(50)}
    assert [s for s, _ in m.select_prepare_event_symbols(rows, set(), _score, 58.0)] == ["OK"]
    m = bs(SNIPER_PREPARE_EVENT_MARGIN=0)
    assert m.select_prepare_event_symbols(rows, set(), _score, 58.0) == []


def test_buy_now_sell_excluded_and_already_selected_skipped():
    m = bs()
    rows = {"BN": _row(decision="BUY NOW"), "SL": _row(decision="SELL"), "SEEN": _row(), "NEW": _row()}
    assert [s for s, _ in m.select_prepare_event_symbols(rows, {"SEEN"}, _score, 58.0)] == ["NEW"]


def test_no_event_or_unusable_structure_is_not_selected():
    m = bs()
    rows = {"QUIET": _row(ms_bos_dir=None), "BADQ": _row(ms_quality="stale"), "NOSUM": {"decision": "WAIT", "conviction": 60, "price": 100.0},
            "FAILED": _row(ms_bos_failed=True), "NOATR": _row(ms_atr=None)}
    assert m.select_prepare_event_symbols(rows, set(), _score, 58.0) == []


def test_cap_and_ordering_best_score_first():
    m = bs(SNIPER_PREPARE_EVENT_MAX=2)
    rows = {"A": _row(52), "B": _row(57), "C": _row(55)}
    assert [s for s, _ in m.select_prepare_event_symbols(rows, set(), _score, 58.0)] == ["B", "C"]
    assert bs(SNIPER_PREPARE_EVENT_MAX=0).select_prepare_event_symbols(rows, set(), _score, 58.0) == []


def test_garbage_never_raises():
    m = bs()
    assert m.select_prepare_event_symbols(None, set(), _score, 58.0) == []
    assert m.select_prepare_event_symbols({"A": "x", "B": None}, None, _score, 58.0) == []
    assert m.select_prepare_event_symbols({"A": _row()}, set(), lambda r: 1 / 0, 58.0) == []


def _data_feed(feeds, **env):
    for k in _ENV:
        os.environ.pop(k, None)
    os.environ.update({k: str(v) for k, v in env.items()})
    if _SERVICE not in sys.path:
        sys.path.insert(0, _SERVICE)
    sys.modules.pop("buy_sniper", None)        # the route imports buy_sniper lazily: make sure it sees THIS environment
    import data_feed as d

    class _Store:
        def list_symbols(self):
            return list(feeds)

    d.get_data_feed_store = lambda: _Store()
    d.get_all_stock_feeds = lambda syms: feeds
    return d


def test_find_prepare_candidates_adds_event_rows_but_not_quiet_or_buy_now_rows():
    base = {"ms_quality": "ok", "ms_price": 100.0, "ms_atr": 2.0, "ms_trend": "bullish", "price": 100.0}
    evt = {"ms_bos_dir": "bullish", "ms_bos_bars": 1, "ms_bos_failed": False}
    feeds = {"INBAND": {"conviction": 60, "decision": "WAIT", **base}, "EVT": {"conviction": 55, "decision": "WAIT", **evt, **base},
             "QUIET": {"conviction": 55, "decision": "WAIT", **base}, "BUYNOW": {"conviction": 90, "decision": "BUY NOW", **evt, **base}}
    assert _data_feed(feeds).find_prepare_to_buy_candidates() == ["INBAND", "EVT"]
    assert _data_feed(feeds, SNIPER_PREPARE_STRUCTURE_MODE="off").find_prepare_to_buy_candidates() == ["INBAND"]
    assert _data_feed(feeds, SNIPER_PREPARE_EVENT_MAX=0).find_prepare_to_buy_candidates() == ["INBAND"]


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
            n += 1
    print(f"{n} passed 0 failed")
