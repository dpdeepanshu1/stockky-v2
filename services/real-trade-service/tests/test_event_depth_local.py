"""
tests/test_event_depth_local.py — direct tests for event_depth_local.py.

Was 40% (missing lines 46-51, the whole body of ``classify_text``): both
callers (watchlist_engine/sources.py Tier 2 and afterhours_scan.py) are
tested with ``classify_text`` monkeypatched away, so the real keyword matcher
had no coverage at all.

Also guards the module's one stated invariant: it is a *verbatim copy* of
analysis-intelligence-service/event/event_depth.py (kept separate on purpose
so real-trade-service never imports across a service boundary). Nothing
enforced "update this copy if the source changes" — the drift test below does.

Run from services/real-trade-service:
    python -m pytest tests/test_event_depth_local.py -v
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import event_depth_local as edl

_SERVICE_ROOT = Path(__file__).resolve().parents[1]
_SOURCE = _SERVICE_ROOT.parent / "analysis-intelligence-service" / "event" / "event_depth.py"


class TestClassifyText:
    @pytest.mark.parametrize("text,expected", [
        ("Acme Q2 results beat estimates", ["results"]),
        ("Big bulk deal seen in Acme", ["bulk_block"]),
        ("Promoter buying at Acme", ["insider"]),
        ("Board meeting on Friday to consider dividend", ["board"]),
    ])
    def test_single_category_matches(self, text, expected):
        assert edl.classify_text(text) == expected

    def test_multiple_categories_returned_in_keyword_table_order(self):
        tags = edl.classify_text("Board approves buyback after strong quarterly results and a block deal")
        assert tags == ["results", "bulk_block", "board"]

    def test_all_four_categories(self):
        tags = edl.classify_text("results; bulk deal; insider; dividend")
        assert tags == ["results", "bulk_block", "insider", "board"]

    def test_match_is_case_insensitive(self):
        assert edl.classify_text("NET PROFIT JUMPS") == ["results"]
        assert edl.classify_text("BuyBack Announced") == ["board"]

    def test_no_match_returns_empty_list(self):
        assert edl.classify_text("Acme launches a new product line") == []

    def test_none_and_empty_are_safe(self):
        assert edl.classify_text(None) == []
        assert edl.classify_text("") == []

    def test_each_category_matched_once_even_with_many_keywords(self):
        # 'results', 'earnings' and 'net profit' are all 'results' keywords —
        # the tag must appear once, not once per keyword.
        assert edl.classify_text("earnings results net profit") == ["results"]

    def test_returns_a_fresh_list_each_call(self):
        a = edl.classify_text("results")
        a.append("mutated")
        assert edl.classify_text("results") == ["results"]

    def test_every_keyword_in_the_table_is_reachable(self):
        for tag, words in edl.EVENT_KEYWORDS.items():
            for w in words:
                assert tag in edl.classify_text(f"headline about {w} today"), (tag, w)

    def test_substring_matching_is_by_design(self):
        # The matcher is a plain substring test (copied verbatim from the
        # source service), so short keywords match inside longer words. Pinned
        # so a future switch to word-boundary matching is a conscious change.
        assert edl.classify_text("Company adopts a bonus scheme") == ["board"]
        assert "results" in edl.classify_text("patient recovery")   # 'pat' in 'patient'


class TestKeywordTable:
    def test_expected_categories(self):
        assert list(edl.EVENT_KEYWORDS) == ["results", "bulk_block", "insider", "board"]

    def test_all_keywords_are_lowercase_non_empty_strings(self):
        for tag, words in edl.EVENT_KEYWORDS.items():
            assert words, tag
            for w in words:
                assert isinstance(w, str) and w and w == w.lower(), (tag, w)


@pytest.mark.skipif(not _SOURCE.exists(), reason="analysis-intelligence-service not present in this checkout")
class TestNoDriftFromSourceService:
    """The local copy must stay verbatim with event/event_depth.py."""

    @staticmethod
    def _source_keywords():
        tree = ast.parse(_SOURCE.read_text(encoding="utf-8"))
        for node in tree.body:
            target = None
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "EVENT_KEYWORDS" for t in node.targets
            ):
                target = node.value
            elif isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "EVENT_KEYWORDS":
                target = node.value
            if target is not None:
                return ast.literal_eval(target)
        raise AssertionError("EVENT_KEYWORDS not found in source service's event_depth.py")

    def test_keyword_table_identical_to_source(self):
        assert edl.EVENT_KEYWORDS == self._source_keywords()

    def test_keyword_order_identical_to_source(self):
        # order decides the order of returned tags, so it is part of the contract
        src = self._source_keywords()
        assert list(edl.EVENT_KEYWORDS) == list(src)
        for tag in src:
            assert edl.EVENT_KEYWORDS[tag] == src[tag]
