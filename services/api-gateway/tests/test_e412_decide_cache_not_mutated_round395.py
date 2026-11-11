"""E412: the decide-cache entry is stored and served as a copy, so enrichment of the returned row (event data, reasons, summary, price) never writes into the cached object."""
from __future__ import annotations

import copy

from test_main_analyze_symbol import DResp, analyze, env, gw, log  # noqa: F401

_SOON = (__import__("datetime").datetime.now(__import__("datetime").timezone(__import__("datetime").timedelta(hours=5, minutes=30))).date() + __import__("datetime").timedelta(days=1)).isoformat()   # E427: a date inside the event-risk window, so these tests do not rot

KEY = gw.DECIDE_CACHE_PREFIX + "TCS"


def test_a_miss_stores_the_engine_answer_not_the_enriched_row(env):
    env.get_result = DResp({"decision": "HOLD", "combined_score": 55, "reasons": {"technical": ["t"]}})
    env.event = {"next_earnings_date": _SOON}
    out = analyze("TCS")
    assert out["event_data"] and out["reasons"]["event"] and out["natural_language_summary"]
    cached = env.kv[KEY]
    for k in ("event_data", "natural_language_summary", "holding_period_estimate"):
        assert k not in cached, k
    assert "event" not in cached["reasons"]


def test_a_hit_does_not_write_into_the_cached_row(env):
    env.kv[KEY] = {"decision": "HOLD", "combined_score": 55, "reasons": {"technical": ["t"]}}
    before = copy.deepcopy(env.kv[KEY])
    env.event = {"next_earnings_date": _SOON}
    out = analyze("TCS")
    assert out["from_decide_cache"] is True and out["reasons"]["event"]
    assert env.kv[KEY] == before


def test_the_copy_helper_is_independent_and_survives_odd_values():
    row = {"a": {"b": [1]}}
    c = gw._decide_cache_copy(row)
    c["a"]["b"].append(2)
    assert row == {"a": {"b": [1]}}
    class NoCopy:
        def __deepcopy__(self, memo):
            raise RuntimeError("x")
    assert gw._decide_cache_copy({"k": NoCopy()})["k"] is not None
    assert gw._decide_cache_copy(None) is None
