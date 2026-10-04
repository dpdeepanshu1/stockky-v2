"""group126: the built-in boot-time liquid universe (surprise_premarket._FALLBACK_LIQUID_UNIVERSE)
must not contain a known-delisted symbol. Both WS feeds start on this list for ~20 s after boot
before the live /scan/universe refresh replaces it (group125 filters that refreshed list)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as m
import surprise_premarket as sp


def test_fallback_universe_has_no_known_delisted_symbol():
    bad = [s for s in sp._FALLBACK_LIQUID_UNIVERSE if m.is_known_delisted(s)]
    assert bad == [], f"delisted names in the built-in boot universe: {bad}"


def test_fallback_universe_has_no_duplicates_or_blanks():
    u = list(sp._FALLBACK_LIQUID_UNIVERSE)
    assert all(isinstance(s, str) and s.strip() for s in u)
    assert len(u) == len(set(u))
