"""E413: a decision row whose `reasons` is an explicit None (or not a dict) must not turn the symbol into an ERROR row when the event / news merge adds
a reason, and a failing summary fallback must not either."""
from __future__ import annotations

import pytest

from test_main_analyze_symbol import ALL_SUPPLIED, DResp, analyze, env, gw, log  # noqa: F401

_SOON = (__import__("datetime").datetime.now(__import__("datetime").timezone(__import__("datetime").timedelta(hours=5, minutes=30))).date() + __import__("datetime").timedelta(days=1)).isoformat()   # E427: a date inside the event-risk window, so these tests do not rot


@pytest.mark.parametrize("bad", [None, "text", ["a"], 5])
def test_event_reason_is_added_when_reasons_is_not_a_dict(env, bad):
    env.get_result = DResp(dict(ALL_SUPPLIED, event_data=None, reasons=bad))
    env.event = {"next_earnings_date": _SOON}
    out = analyze("TCS")
    assert out["decision"] != "ERROR" and out["reasons"]["event"] == [f"Earnings due: {_SOON}"]


@pytest.mark.parametrize("bad", [None, "text"])
def test_news_reason_is_added_when_reasons_is_not_a_dict(env, bad):
    env.get_result = DResp(dict(ALL_SUPPLIED, news_score=None, reasons=bad))
    env.news = {"news_score": 61, "reasons": ["good headline"]}
    out = analyze("TCS")
    assert out["decision"] != "ERROR" and out["news_score"] == 61 and out["reasons"]["news"] == ["good headline"]


def test_a_dict_reasons_keeps_its_other_keys(env):
    env.get_result = DResp(dict(ALL_SUPPLIED, event_data=None, reasons={"technical": ["t"]}))
    env.event = {"next_earnings_date": _SOON}
    out = analyze("TCS")
    assert out["reasons"]["technical"] == ["t"] and out["reasons"]["event"]


def test_a_summary_that_raises_does_not_become_an_error_row(env, monkeypatch):
    async def boom(data, client):
        raise RuntimeError("template broke")
    monkeypatch.setattr(gw, "_generate_ai_summary", boom)
    env.get_result = DResp(dict(ALL_SUPPLIED))
    out = analyze("TCS")
    assert out["decision"] == "HOLD" and out["natural_language_summary"]


def test_reasons_dict_helper():
    row = {"reasons": None}
    d = gw._reasons_dict(row)
    d["k"] = 1
    assert row["reasons"] == {"k": 1}
    same = {"reasons": {"a": 1}}
    assert gw._reasons_dict(same) is same["reasons"]
