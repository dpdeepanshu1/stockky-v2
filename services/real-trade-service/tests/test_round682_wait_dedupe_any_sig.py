"""Round 682: WAIT_DEDUPE_MODE=any_sig writes each (symbol, signature) once per window; the default rule is unchanged."""
from types import SimpleNamespace as NS

import pytest

import wait_dedupe as wd


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("WAIT_DEDUPE_MINUTES", "15")
    monkeypatch.delenv("WAIT_DEDUPE_MODE", raising=False)
    wd.reset_for_tests()
    yield
    wd.reset_for_tests()


def row(symbol="AAA", tag="no_setup_window", action="WAIT", mode="REAL"):
    return NS(mode=mode, symbol=symbol, action=action, reasoning="blocked", gate_tag=tag, risk_verdict=None, risk_verdict_reason=None)


def _flip(n=20, step=180.0):
    kept = 0
    for i in range(n):
        if not wd.is_repeat(row(tag="structure" if i % 2 else "no_setup_window"), now=i * step):
            kept += 1
    return kept


def test_default_mode_is_last_and_a_flipping_name_writes_every_row():
    assert wd.mode() == "last"
    assert _flip() == 20


def test_any_sig_writes_each_signature_once_per_window(monkeypatch):
    monkeypatch.setenv("WAIT_DEDUPE_MODE", "any_sig")
    assert wd.mode() == "any_sig"
    assert _flip(20, 180.0) == 8          # 3600 s: two signatures, each written at 0 s and again after 900 s (x4 windows)


def test_any_sig_first_row_of_every_signature_is_kept_and_written_again_after_window(monkeypatch):
    monkeypatch.setenv("WAIT_DEDUPE_MODE", " Any_Sig ")
    assert wd.is_repeat(row(tag="a"), now=0.0) is False
    assert wd.is_repeat(row(tag="b"), now=10.0) is False
    assert wd.is_repeat(row(tag="a"), now=20.0) is True
    assert wd.is_repeat(row(tag="b"), now=30.0) is True
    assert wd.is_repeat(row(tag="a"), now=900.0) is False


def test_any_sig_enter_resets_and_other_symbols_are_independent(monkeypatch):
    monkeypatch.setenv("WAIT_DEDUPE_MODE", "any_sig")
    assert wd.is_repeat(row(tag="a"), now=0.0) is False
    assert wd.is_repeat(row(tag="a", action="ENTER"), now=5.0) is False
    assert wd.is_repeat(row(tag="a"), now=10.0) is False
    assert wd.is_repeat(row("BBB", tag="a"), now=11.0) is False


def test_any_sig_off_switch_unknown_mode_and_junk(monkeypatch):
    monkeypatch.setenv("WAIT_DEDUPE_MODE", "any_sig")
    monkeypatch.setenv("WAIT_DEDUPE_MINUTES", "0")
    assert wd.is_repeat(row(), now=0.0) is False and wd.is_repeat(row(), now=1.0) is False
    monkeypatch.setenv("WAIT_DEDUPE_MODE", "bogus")
    assert wd.mode() == "last"
    monkeypatch.setenv("WAIT_DEDUPE_MODE", "any_sig")
    assert wd.is_repeat(object(), now=0.0) is False and wd.is_repeat(None) is False


def test_any_sig_memory_is_bounded(monkeypatch):
    monkeypatch.setenv("WAIT_DEDUPE_MODE", "any_sig")
    monkeypatch.setattr(wd, "_MAX_KEYS", 50)
    monkeypatch.setattr(wd, "_MAX_SIGS", 5)
    for i in range(200):
        wd.is_repeat(row(f"S{i}"), now=float(i))
    assert wd.status()["tracked"] <= 50
    for i in range(30):
        wd.is_repeat(row("ONE", tag=f"t{i}"), now=1000.0 + i)
    assert len(wd._seen[("REAL", "ONE")]) <= 5
    assert wd.status()["mode"] == "any_sig"
