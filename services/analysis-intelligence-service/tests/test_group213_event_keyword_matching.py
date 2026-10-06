"""
tests/test_group213_event_keyword_matching.py

group213 (2026-10-06): the event service's site-wide feed matchers (Moneycontrol / Economic Times / CNBC TV18)
used a plain substring test over keywords that included every word of the company name, so a stock was given
other companies' headlines: the ticker "BEL" matched "label", "ITC" matched "pitch", and a bare "limited" /
"india" / "bank" matched almost everything.

Now: keywords are matched as whole words / phrases, and generic company-name words are not used on their own.
The full company name, the ticker and the aliases are still searched.

Run from services/analysis-intelligence-service:
    python -m pytest tests/test_group213_event_keyword_matching.py -v
"""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from event import main as em


@pytest.fixture(autouse=True)
def _clean():
    em._KW_PATTERN_CACHE.clear()
    yield
    em._KW_PATTERN_CACHE.clear()


# ── whole-word matching ───────────────────────────────────────────────────────
@pytest.mark.parametrize("kw,text,expected", [
    (["bel"], "the label was changed", False),
    (["bel"], "bel bags a defence order", True),
    (["itc"], "a perfect pitch to investors", False),
    (["itc"], "itc hotels demerger update", True),
    (["lt"], "the project was built on time", False),
    (["lt"], "l&t and lt group order inflow", True),
    (["abb"], "abbott india q2 results", False),
    (["abb"], "abb india wins order", True),
    (["m&m"], "m&m sales rise 8%", True),
    (["m&m"], "ram&mohan", False),
    (["tata consultancy services"], "tata consultancy services q2 profit", True),
    (["tata consultancy services"], "tata motors q2 profit", False),
    (["infosys"], "infosys's guidance cut", True),          # possessive: the apostrophe ends the word
    (["infosys"], "infosysbpm unit", False),
])
def test_whole_word_matching(kw, text, expected):
    assert em._text_matches_keywords(text, kw) is expected


def test_edges_none_empty_blank_and_one_char_keywords():
    assert em._text_matches_keywords("anything", []) is False
    assert em._text_matches_keywords("anything", ["", None, " ", "a"]) is False   # < 2 chars ignored
    assert em._text_matches_keywords("", ["abc"]) is False
    assert em._text_matches_keywords(None, ["abc"]) is False


def test_keyword_case_and_padding_are_normalised():
    assert em._text_matches_keywords("abb india wins order", ["  ABB "]) is True


def test_pattern_cache_is_reused_and_bounded(monkeypatch):
    em._text_matches_keywords("x", ["abc", "def"])
    em._text_matches_keywords("y", ["def", "abc"])           # same set, other order
    assert len(em._KW_PATTERN_CACHE) == 1
    for i in range(2100):
        em._KW_PATTERN_CACHE[("k%d" % i,)] = object()
    em._text_matches_keywords("x", ["zzz"])
    assert len(em._KW_PATTERN_CACHE) == 1                    # cleared past the bound, then this one stored


# ── keyword list ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name,dropped,kept", [
    ("Tata Motors Limited", {"limited", "motors"}, {"tata"}),
    ("Bank of India", {"bank", "india", "of"}, set()),
    ("Larsen & Toubro Ltd.", {"ltd", "ltd."}, {"larsen", "toubro"}),
    ("Reliance Industries Ltd", {"industries", "ltd"}, {"reliance"}),
    ("Kanohar Electricals Pvt (India)", {"pvt", "india", "(india)"}, {"kanohar", "electricals"}),
])
def test_generic_words_are_not_keywords_on_their_own(monkeypatch, name, dropped, kept):
    monkeypatch.setattr(em, "_get_company_name", lambda s: name)
    keys = set(em._get_keywords("XYZ.NS"))
    assert not (dropped & keys), dropped & keys
    assert kept <= keys
    assert name in keys and name.lower() in keys and "XYZ" in keys and "xyz" in keys   # full name + ticker stay


def test_aliases_still_searched_in_full(monkeypatch):
    monkeypatch.setattr(em, "_get_company_name", lambda s: "State Bank of India")
    keys = set(em._get_keywords("SBIN.NS"))
    assert {"State Bank of India", "state bank of india", "SBI", "sbi"} <= keys
    assert "bank" not in keys and "india" not in keys


def test_bare_ticker_fallback_name_still_works(monkeypatch):
    monkeypatch.setattr(em, "_get_company_name", lambda s: "ABB")
    assert {"ABB", "abb"} <= set(em._get_keywords("ABB.NS"))


# ── the three site-feed matchers ──────────────────────────────────────────────
def _entry(title, desc=""):
    return types.SimpleNamespace(title=title, description=desc, link="http://x/" + title[:5],
                                 published_parsed=None)


FEED = [
    _entry("Label maker Zed Ltd rallies"),                     # 'label' contains 'bel'
    _entry("Bharat Electronics: BEL bags Rs 500 cr order"),
    _entry("Perfect pitch at the ITC AGM"),                     # 'pitch' contains 'itc'
    _entry("Largest private bank India posts profit"),          # generic words only
    _entry("Tata Motors Limited Q2 profit"),
]


@pytest.mark.parametrize("fn,publisher", [
    ("_fetch_moneycontrol_news", "Moneycontrol"),
    ("_fetch_economic_times", "Economic Times"),
    ("_fetch_cnbc_tv18", "CNBC TV18"),
])
def test_site_feeds_only_return_headlines_about_the_symbol(monkeypatch, fn, publisher):
    monkeypatch.setattr(em, "_parse_site_feed", lambda url: types.SimpleNamespace(entries=FEED))
    monkeypatch.setattr(em, "_get_company_name", lambda s: "Bharat Electronics Limited")
    out = getattr(em, fn)("BEL.NS")
    assert [i["title"] for i in out] == ["Bharat Electronics: BEL bags Rs 500 cr order"]
    assert out[0]["publisher"] == publisher


@pytest.mark.parametrize("fn", ["_fetch_moneycontrol_news", "_fetch_economic_times", "_fetch_cnbc_tv18"])
def test_a_company_called_limited_india_bank_no_longer_matches_everything(monkeypatch, fn):
    monkeypatch.setattr(em, "_parse_site_feed", lambda url: types.SimpleNamespace(entries=FEED))
    monkeypatch.setattr(em, "_get_company_name", lambda s: "Acme India Bank Limited")
    out = getattr(em, fn)("ACME.NS")
    assert out == []                                           # nothing in FEED is about Acme
