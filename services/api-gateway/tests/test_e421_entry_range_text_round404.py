"""E421: the template summary and the Gemini prompt no longer print "None-None" for a missing entry range.

`_entry_range_text(entry, missing)`: both ends -> "low-high"; one end -> that end alone; neither or a non-dict -> `missing`.
"""
from __future__ import annotations

import pytest

from test_main_scoring import gw  # noqa: F401


@pytest.mark.parametrize("entry, expected", [
    ({"low": 1, "high": 2}, "1-2"),
    ({"low": 4000.5, "high": 4050}, "4000.5-4050"),
    ({"low": 0, "high": 2}, "0-2"),           # a 0 is a value, not "missing"
    ({"low": 1, "high": None}, "1"),
    ({"low": None, "high": 2}, "2"),
    ({"low": None, "high": None}, "not set"),
    ({}, "not set"),
    (None, "not set"),
    ([1, 2], "not set"),
    ("1-2", "not set"),
])
def test_entry_range_text_shapes(entry, expected):
    assert gw._entry_range_text(entry) == expected


def test_entry_range_text_custom_missing_label():
    assert gw._entry_range_text(None, "तय नहीं") == "तय नहीं"


def test_gemini_prompt_never_prints_none_dash_none():
    for entry in (None, {}, {"low": None, "high": None}, [1], "x"):
        p = gw._build_gemini_prompt({"decision": "HOLD", "entry_range": entry})
        assert "None-None" not in p and "Entry range: not set, target:" in p
    p = gw._build_gemini_prompt({"decision": "BUY NOW", "entry_range": {"low": 4000, "high": 4050}})
    assert "Entry range: 4000-4050, target:" in p


def test_template_summary_never_prints_none_dash_none_and_a_non_dict_range_does_not_raise():
    for decision in ("BUY NOW", "PREPARE TO BUY"):
        for entry in (None, {}, {"low": None, "high": None}, [1], "x"):
            s = gw._generate_summary({"decision": decision, "symbol": "TCS", "entry_range": entry})
            assert "None-None" not in s and "एंट्री तय नहीं, टारगेट" in s
        s = gw._generate_summary({"decision": decision, "symbol": "TCS", "entry_range": {"low": 1, "high": 2}})
        assert "एंट्री 1-2, टारगेट" in s


def test_template_summary_one_sided_range():
    s = gw._generate_summary({"decision": "BUY NOW", "symbol": "TCS", "entry_range": {"low": 1, "high": None}})
    assert "एंट्री 1, टारगेट" in s and "1-None" not in s
