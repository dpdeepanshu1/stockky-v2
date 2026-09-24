"""
tests/test_afterhours_extract_symbol.py

Covers watchlist_engine.afterhours_scan._extract_symbol: the direct
ALL-CAPS-token match against the live symbol universe, the degraded
whitelist+heuristic fallback when that universe is unavailable, and the
2026-09-24 name-alias fix for tickers (MANINDS, EKC, OLAELEC,
RAYMONDREL, UTLSOLAR) that never appear as a literal word inside their
own company's headlines.

Run from services/real-trade-service:
    python -m pytest tests/test_afterhours_extract_symbol.py -q
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from watchlist_engine.afterhours_scan import _extract_symbol, _NAME_ALIASES


class TestDirectTokenMatch:
    def test_matches_ticker_that_equals_company_name(self):
        assert _extract_symbol("Reliance shares surge 4% on strong results", {"RELIANCE"}) == "RELIANCE"

    def test_no_match_returns_none(self):
        assert _extract_symbol("Nifty ends flat in a quiet session", {"RELIANCE"}) is None

    def test_prefers_direct_token_over_alias_when_both_present(self):
        # If a headline happens to contain the literal ticker token, that
        # match wins — the alias pass only runs after tokens are exhausted.
        headline = "MANINDS rockets 16% intraday"
        assert _extract_symbol(headline, {"MANINDS"}) == "MANINDS"


class TestNameAliasFallback:
    @pytest.mark.parametrize("sym", sorted(_NAME_ALIASES))
    def test_every_alias_entry_is_reachable(self, sym):
        """Each seeded alias must actually resolve its symbol given a
        realistic prose headline using the company's display name."""
        headline = f"{_NAME_ALIASES[sym][0].title()} shares jump on strong order book"
        assert _extract_symbol(headline, {sym}) == sym

    def test_man_industries_headline_resolves_to_maninds(self):
        headline = "Man Industries shares surge 16% after record order win"
        assert _extract_symbol(headline, {"MANINDS"}) == "MANINDS"

    def test_everest_kanto_cylinder_headline_resolves_to_ekc(self):
        headline = "Everest Kanto Cylinder gains 12% on strong volumes"
        assert _extract_symbol(headline, {"EKC"}) == "EKC"

    def test_ola_electric_headline_resolves_to_olaelec(self):
        headline = "OLA Electric jumps 10% as delivery numbers beat estimates"
        assert _extract_symbol(headline, {"OLAELEC"}) == "OLAELEC"

    def test_raymond_realty_headline_resolves_to_raymondrel(self):
        headline = "Raymond Realty rallies on strong project launch"
        assert _extract_symbol(headline, {"RAYMONDREL"}) == "RAYMONDREL"

    def test_fujiyama_headline_resolves_to_utlsolar_via_rename_alias(self):
        headline = "Fujiyama Power Systems announces record order win"
        assert _extract_symbol(headline, {"UTLSOLAR"}) == "UTLSOLAR"

    def test_alias_never_fires_when_symbol_missing_from_known_universe(self):
        """The alias table is a name→symbol hint, not an independent
        source of truth — it must still respect known_symbols, same as
        the direct-token path, so a stale/wrong alias can never invent a
        symbol that isn't real."""
        headline = "Man Industries shares surge 16% after record order win"
        assert _extract_symbol(headline, {"RELIANCE"}) is None  # MANINDS not in universe

    def test_alias_fallback_also_applies_when_symbol_master_unavailable(self):
        headline = "Man Industries shares surge 16% after record order win"
        assert _extract_symbol(headline, set()) == "MANINDS"


class TestDegradedHeuristicUnaffectedByAliasFix(object):
    def test_unknown_long_token_still_falls_through_to_heuristic(self):
        # No known_symbols (degraded path) and no alias match — should
        # still fall back to the old length-heuristic behavior.
        headline = "SOMENEWCORP shares list at a premium on debut"
        assert _extract_symbol(headline, set()) == "SOMENEWCORP"


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(pytest.main([__file__, "-v"]))
