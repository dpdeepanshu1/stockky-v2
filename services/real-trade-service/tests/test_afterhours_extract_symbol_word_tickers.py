"""group151: tickers that are also everyday words must not be extracted from plain headline text.

VM scan 2026-10-05 kept ['LENSKART','BPCL','CLEANMAX','WIPRO','OIL','TCS','IT','ACE']: IT (the sector),
OIL ("oil prices") and ACE are words. On the known-universe path any token in the symbol master matched,
_STOPWORDS was ignored, and the FIRST such token won.
"""
from watchlist_engine.afterhours_scan import (
    _GENERIC_WORD_TICKERS,
    _NAME_ALIASES,
    _NAME_ONLY_TICKERS,
    _extract_symbol,
)

UNIVERSE = {"IT", "OIL", "ACE", "TCS", "INFY", "WIPRO", "RELIANCE", "ENERGY", "GOLD", "BANK"}


class TestGenericWordsAreSkipped:
    def test_sector_word_it_alone_gives_no_symbol(self):
        assert _extract_symbol("IT stocks rally as rupee weakens", UNIVERSE) is None

    def test_it_does_not_beat_the_real_company_later_in_the_headline(self):
        assert _extract_symbol("IT stocks: TCS jumps 3% on deal win", UNIVERSE) == "TCS"

    def test_title_case_it_pronoun_is_not_a_symbol(self):
        assert _extract_symbol("Wipro says it expects a strong quarter", UNIVERSE) == "WIPRO"

    def test_other_generic_words_skipped(self):
        for word in ("Energy", "Gold", "Bank"):
            assert _extract_symbol(f"{word} shares move higher", UNIVERSE) is None

    def test_generic_set_covers_it(self):
        assert "IT" in _GENERIC_WORD_TICKERS


class TestNameOnlyTickers:
    def test_oil_prices_is_not_oil_india(self):
        assert _extract_symbol("Oil prices fall as demand worries grow", UNIVERSE) is None

    def test_oil_india_by_name_still_resolves(self):
        assert _extract_symbol("Oil India Q2 profit rises 12%", UNIVERSE) == "OIL"

    def test_ace_word_is_not_a_symbol(self):
        assert _extract_symbol("Ace investor raises stake in mid-cap pharma firm", UNIVERSE) is None

    def test_action_construction_by_name_still_resolves(self):
        assert _extract_symbol("Action Construction Equipment bags new order", UNIVERSE) == "ACE"

    def test_name_only_tickers_each_have_an_alias(self):
        for sym in _NAME_ONLY_TICKERS:
            assert sym in _NAME_ALIASES

    def test_alias_still_gated_by_universe(self):
        assert _extract_symbol("Oil India Q2 profit rises 12%", {"TCS"}) is None


class TestNothingElseChanged:
    def test_ordinary_ticker_word_still_matches(self):
        assert _extract_symbol("Reliance shares surge on results", UNIVERSE) == "RELIANCE"

    def test_degraded_path_without_master_unchanged(self):
        assert _extract_symbol("Oil India Q2 profit rises 12%", set()) == "OIL"
