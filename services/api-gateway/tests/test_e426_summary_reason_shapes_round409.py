"""E426: the Gemini prompt and the template summary read `reasons` through `_reason_list`, so any shape the engine sends gives
text lines or nothing, never an exception or the first letter of a string."""
from __future__ import annotations

import pytest

from test_main_scoring import gw  # noqa: F401


@pytest.mark.parametrize("reasons, key, expected", [
    ({"technical": ["a", "b"]}, "technical", ["a", "b"]),
    ({"technical": ("a",)}, "technical", ["a"]),
    ({"technical": "RSI ok"}, "technical", ["RSI ok"]),
    ({"technical": ["a", None, "", "  ", 5]}, "technical", ["a", "5"]),
    ({"technical": None}, "technical", []),
    ({"technical": {"x": 1}}, "technical", []),
    ({"technical": 7}, "technical", []),
    ({}, "technical", []),
    ({"fundamental": ["f"]}, "technical", []),
    (None, "technical", []),
    (["a"], "technical", []),
    ("text", "technical", []),
])
def test_reason_list_shapes(reasons, key, expected):
    assert gw._reason_list(reasons, key) == expected


@pytest.mark.parametrize("reasons", [None, ["x"], "text", 5, {"technical": None, "fundamental": {"k": 1}}])
def test_prompt_and_template_do_not_raise_on_odd_reasons(reasons):
    d = {"decision": "BUY NOW", "symbol": "TCS", "reasons": reasons}
    p = gw._build_gemini_prompt(d)
    assert "Technical reasons: none listed." in p and "Fundamental reasons: none listed." in p
    s = gw._generate_summary(d)
    assert "तकनीकी" not in s and "फंडामेंटल" not in s
    s2 = gw._generate_summary({"decision": "DO NOT BUY", "symbol": "TCS", "reasons": reasons})
    assert "तकनीकी" not in s2 and s2.endswith("कुछ दिन और देखें.")


def test_a_text_reason_is_one_line_not_its_first_letter():
    d = {"decision": "BUY NOW", "symbol": "TCS", "reasons": {"technical": "RSI ok", "fundamental": ["ROE ok", None]}}
    s = gw._generate_summary(d)
    assert "तकनीकी: RSI ok." in s and "फंडामेंटल: ROE ok." in s
    assert "Technical reasons: RSI ok." in gw._build_gemini_prompt(d)


def test_a_non_text_item_no_longer_breaks_the_prompt_join():
    p = gw._build_gemini_prompt({"decision": "HOLD", "reasons": {"technical": [1, "b", "c"]}})
    assert "Technical reasons: 1; b." in p
