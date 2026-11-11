"""E433 (round 416): the price cap runs inside the cut to SCAN_UNIVERSE_TARGET, so over-cap names no longer take slots."""
from __future__ import annotations

import pytest

from test_main_scoring import gw  # noqa: F401


def _names(n, prefix="S"):
    return [f"{prefix}{i:03d}" for i in range(n)]


def _wire(monkeypatch, over):
    calls = []

    def flt(symbols):
        calls.append(list(symbols))
        return [s for s in symbols if s not in over]

    monkeypatch.setattr(gw, "_filter_symbols_under_max_price", flt)
    for k in ("SCAN_UNIVERSE_PRICE_CUT_FIRST", "SCAN_UNIVERSE_PRICE_HEADROOM_PCT"):
        monkeypatch.delenv(k, raising=False)
    return calls


def test_nothing_over_cap_is_one_read_and_the_same_first_names(monkeypatch):
    calls = _wire(monkeypatch, set())
    out = gw._cut_universe_under_price_cap(_names(300), 100)
    assert out == _names(300)[:100]
    assert len(calls) == 1 and len(calls[0]) == 120          # target + 20 % margin


def test_over_cap_names_inside_the_margin_do_not_leave_the_universe_short(monkeypatch):
    names = _names(300)
    over = set(names[5:15])                                    # 10 over-cap names inside the first 100
    calls = _wire(monkeypatch, over)
    out = gw._cut_universe_under_price_cap(names, 100)
    assert len(out) == 100 and not (set(out) & over)
    assert out == [n for n in names if n not in over][:100]    # priority order kept
    assert len(calls) == 1


def test_more_over_cap_than_the_margin_reads_the_next_window(monkeypatch):
    names = _names(400)
    over = set(names[:60])                                     # 60 over cap, margin only 20
    calls = _wire(monkeypatch, over)
    out = gw._cut_universe_under_price_cap(names, 100)
    assert out == names[60:160] and len(calls) == 2
    assert calls[1][0] == names[120]                           # the second read starts where the first stopped


def test_candidates_run_out_gives_a_shorter_universe(monkeypatch):
    names = _names(150)
    over = set(names[:100])
    _wire(monkeypatch, over)
    assert gw._cut_universe_under_price_cap(names, 100) == names[100:]


def test_a_name_repeated_across_windows_is_kept_once(monkeypatch):
    names = _names(50) + _names(50)                            # same 50 twice
    _wire(monkeypatch, set())
    out = gw._cut_universe_under_price_cap(names, 80)
    assert out == _names(50)


def test_rollback_switch_cuts_first_then_filters_once(monkeypatch):
    names = _names(300)
    over = set(names[:10])
    calls = _wire(monkeypatch, over)
    monkeypatch.setenv("SCAN_UNIVERSE_PRICE_CUT_FIRST", "0")
    out = gw._cut_universe_under_price_cap(names, 100)
    assert out == names[10:100] and len(out) == 90 and calls == [names[:100]]


@pytest.mark.parametrize("raw,want", [("", 20.0), ("0", 0.0), ("35", 35.0), ("junk", 20.0), ("-4", 20.0)])
def test_headroom_parsing(monkeypatch, raw, want):
    monkeypatch.setenv("SCAN_UNIVERSE_PRICE_HEADROOM_PCT", raw)
    assert gw._price_headroom_pct() == want


def test_empty_and_zero_target(monkeypatch):
    _wire(monkeypatch, set())
    assert gw._cut_universe_under_price_cap([], 100) == []
    assert gw._cut_universe_under_price_cap(_names(5), 0) == []

