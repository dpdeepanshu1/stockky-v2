"""E285 round 103: the stock's own gap rank (gap_rank.py + adaptive_params.overnight_gaps / gap_pctile). Makes `gap_pctile` real, so the "gap above the 95th percentile of
its own gaps halves the size" rule of setup_engine can fire."""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from orders import adaptive_params as ap, gap_rank, setup_engine

DAY = 86400.0
TODAY_TS = datetime(2026, 11, 2, 4, 0, tzinfo=timezone.utc).timestamp()       # 2026-11-02 09:30 IST


@pytest.fixture(autouse=True)
def on(monkeypatch):
    monkeypatch.setattr(config, "GAP_RANK_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "GAP_RANK_SESSIONS", 60, raising=False)
    monkeypatch.setattr(config, "GAP_RANK_MIN_SESSIONS", 30, raising=False)
    gap_rank.reset()


def bars(n, last_is_today=False, step=0.5):
    """n daily bars (ts, o, h, l, c, v) ending yesterday (or today); each day opens `step` % above the previous close."""
    out, c = [], 100.0
    end = TODAY_TS if last_is_today else TODAY_TS - DAY
    for i in range(n):
        ts = end - (n - 1 - i) * DAY
        o = c * (1 + step / 100.0 * (1 if i % 2 else -1) * (1 + i % 3))
        cl = o * 1.001
        out.append((ts, o, max(o, cl), min(o, cl), cl, 1000.0))
        c = cl
    return out


class TestMaths:
    def test_overnight_gaps_are_signed_and_aligned(self):
        g = ap.overnight_gaps([100, 102, 99], [101, 100, 98])
        assert [round(x, 3) for x in g] == [round((102 - 101) / 101 * 100, 3), round((99 - 100) / 100 * 100, 3)]

    def test_bad_pairs_and_splits_are_skipped_and_the_cap_applies(self):
        assert ap.overnight_gaps([100, None, 100, 500], [100, 100, 0, 100]) == [0.0]       # None open and a zero previous close are skipped
        assert ap.overnight_gaps([100, 500], [100, 100]) == []                              # +400 % is a split / bad bar
        assert len(ap.overnight_gaps(list(range(1, 100)), list(range(1, 100)), 60)) == 60
        assert ap.overnight_gaps(None, None) == [] and ap.overnight_gaps([1], [1]) == []

    def test_pctile_ranks_inside_the_own_history_and_is_none_when_thin(self):
        hist = [i / 10.0 for i in range(-20, 20)]                                          # 40 gaps from -2.0 to +1.9
        assert ap.gap_pctile(5.0, hist) == 1.0
        assert ap.gap_pctile(-5.0, hist) == 0.0
        assert 0.4 < ap.gap_pctile(0.0, hist) < 0.6
        assert ap.gap_pctile(1.0, hist[:10]) is None and ap.gap_pctile("x", hist) is None


class TestCompute:
    def test_todays_bar_is_dropped_and_a_short_history_is_unknown(self):
        full = bars(70, last_is_today=True)
        g = gap_rank.compute("ABC", lambda s: full, now_ts=TODAY_TS)
        assert g is not None and len(g) == 60
        assert gap_rank.compute("ABC", lambda s: bars(20), now_ts=TODAY_TS) is None          # < GAP_RANK_MIN_SESSIONS gaps
        n_with = len(ap.overnight_gaps([b[1] for b in full], [b[4] for b in full], 100))
        assert n_with == 69 and len(gap_rank.compute("ABC", lambda s: full, now_ts=TODAY_TS)) == 60

    def test_a_failing_fetch_is_unknown_not_an_error(self):
        def boom(s):
            raise RuntimeError("down")
        assert gap_rank.compute("ABC", boom) is None


class TestCache:
    def test_load_then_pctile_reads_the_cache_without_a_fetch(self, monkeypatch):
        calls = []
        monkeypatch.setattr(gap_rank, "compute", lambda s, f=None, now_ts=None: calls.append(s) or [i / 10.0 for i in range(-20, 20)])
        gap_rank.load("ABC")
        assert gap_rank.pctile("ABC", 9.0) == 1.0 and gap_rank.pctile("abc.NS", -9.0) == 0.0 and calls == ["ABC"]

    def test_unknown_symbol_asks_for_a_background_warm_and_has_no_rank_yet(self, monkeypatch):
        asked = []
        monkeypatch.setattr(gap_rank, "warm", lambda syms, **kw: asked.append(list(syms)) or True)
        assert gap_rank.pctile("NEW", 3.0) is None and asked == [["NEW"]]
        assert gap_rank.pctile("NEW", None) is None

    def test_warm_sync_fills_the_cache_and_does_not_repeat_the_fetch(self):
        calls = []
        f = lambda s: calls.append(s) or bars(70)                        # noqa: E731
        assert gap_rank.warm(["AAA", "BBB", "AAA"], fetch=f, sync=True) is True
        assert sorted(calls) == ["AAA", "BBB"]
        assert gap_rank.warm(["AAA", "BBB"], fetch=f, sync=True) is False and len(calls) == 2
        assert gap_rank.status()["cached"] == 2

    def test_a_symbol_with_no_history_is_cached_as_unknown_and_not_retried(self):
        calls = []
        assert gap_rank.warm(["THIN"], fetch=lambda s: calls.append(s) or bars(10), sync=True)
        assert gap_rank.pctile("THIN", 9.0) is None
        assert gap_rank.warm(["THIN"], fetch=lambda s: calls.append(s) or bars(10), sync=True) is False and len(calls) == 1

    def test_warm_respects_the_limit(self):
        calls = []
        gap_rank.warm(["A1", "A2", "A3", "A4"], fetch=lambda s: calls.append(s) or bars(70), sync=True, limit=2)
        assert len(calls) == 2

    def test_switch_off_means_no_rank_and_no_fetch(self, monkeypatch):
        monkeypatch.setattr(config, "GAP_RANK_ENABLED", False)
        assert gap_rank.warm(["AAA"], fetch=lambda s: (_ for _ in ()).throw(AssertionError("no fetch")), sync=True) is False
        assert gap_rank.pctile("AAA", 9.0) is None


class TestSizeRule:
    def test_a_gap_above_the_95th_percentile_halves_the_setup_size(self):
        base = {"promoted": ()}
        full = setup_engine._size(setup_engine.S1, "0930", dict(base), 0.0)
        halved = setup_engine._size(setup_engine.S1, "0930", dict(base, gap_pctile=0.97), 0.0)
        same = setup_engine._size(setup_engine.S1, "0930", dict(base, gap_pctile=0.90), 0.0)
        assert halved == round(full * 0.5, 4) and same == full
