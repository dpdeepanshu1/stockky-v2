"""Round 202: ANTALA (BSE-SME-only IPO, lists 5 Nov 2026) is not an NSE symbol; KNOWN_NOT_ON_NSE_EXTRA adds more without a code change."""
import importlib

import symbol_aliases as sa


def test_antala_is_dropped_from_the_nse_universe():
    assert "ANTALA" in sa.KNOWN_NOT_ON_NSE
    assert sa.resolve_base_symbol("ANTALA") is None
    assert sa.resolve_base_symbol("antala.ns") is None


def test_real_nse_names_next_to_it_are_untouched():
    for s in ("HEG", "BIRET", "RELIANCE"):
        assert s not in sa.KNOWN_NOT_ON_NSE
        assert sa.resolve_base_symbol(s) == s


def test_env_extra_adds_symbols_uppercased(monkeypatch):
    monkeypatch.setenv("KNOWN_NOT_ON_NSE_EXTRA", " foo , Bar,,")
    try:
        mod = importlib.reload(sa)
        assert {"FOO", "BAR", "ANTALA"} <= mod.KNOWN_NOT_ON_NSE
    finally:
        monkeypatch.delenv("KNOWN_NOT_ON_NSE_EXTRA", raising=False)
        importlib.reload(sa)
