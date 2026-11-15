"""Round 559 (E584, Tredye plan P8): screening/liquidity_badge - turnover rank and the recorded spread for the stock popup. Display only.
Plain-function runner: python3 tests/test_round559_liquidity_badge.py (also collected by pytest)."""
from __future__ import annotations

import math
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from feed import bar_store, spread_recorder as sr
from screening import liquidity_badge as lb

HERE = os.path.dirname(os.path.abspath(__file__))
IST = timezone(timedelta(hours=5, minutes=30))
SESSION = "2026-11-13"


def _ts(h: int, m: int, day: str = SESSION) -> float:
    y, mo, d = (int(x) for x in day.split("-"))
    return datetime(y, mo, d, h, m, tzinfo=IST).timestamp()


class _env:
    """Sets BAR_RECORD_DIR to a fresh folder and puts bar_store, the recorder and the config back afterwards (nothing leaks into other tests)."""
    def __enter__(self) -> str:
        self.old = getattr(config, "BAR_RECORD_DIR", None)
        self.root = tempfile.mkdtemp(prefix="liq_badge_")
        config.BAR_RECORD_DIR = self.root
        return self.root

    def __exit__(self, *a) -> None:
        config.BAR_RECORD_DIR = self.old
        bar_store.reset()
        sr.reset()


def _fill(n: int = 30) -> None:
    bar_store.reset()
    for i in range(n):
        sym = f"S{i:02d}"
        bar_store.on_tick(sym, 100.0, 300000.0 + i * 100000.0, _ts(10, 0))
        bar_store.on_tick(sym, 100.0, 300000.0 + i * 100000.0 + 10.0, _ts(10, 1))


def test_pct_rank_counts_smaller_turnovers_only_and_needs_enough_stocks():
    pool = [float(i * 1000) for i in range(1, 41)]
    assert lb.pct_rank(30000.0, pool) == 72.5
    assert lb.pct_rank(1000.0, pool) == 0.0
    assert lb.pct_rank(40000.0, pool) == 97.5
    assert lb.pct_rank(30000.0, pool[:19]) is None
    assert lb.pct_rank(30000.0, pool[:19], min_tracked=10) is not None


def test_pct_rank_ignores_unknown_numbers_and_never_returns_nan():
    pool = [float(i * 1000) for i in range(1, 31)] + [0, 0.0, None, "x", float("nan"), float("inf"), -5]
    assert lb.pct_rank(15000.0, pool) == 46.7
    for bad in (0, -1, None, "x", float("nan"), float("inf")):
        assert lb.pct_rank(bad, pool) is None
    assert lb.pct_rank(5.0, None) is None
    assert not math.isnan(lb.pct_rank(500.0, pool))


def test_spread_stats_uses_only_rows_of_the_session_day():
    rows = [[_ts(9, 20), 0.20, 0.30, 5], [_ts(10, 0), 0.10, 0.40, 6], [_ts(11, 0), 0.30, 0.35, 4], [_ts(15, 0, "2026-11-12"), 9.0, 9.5, 3]]
    s = lb.spread_stats(rows, SESSION)
    assert s == {"last_pct": 0.3, "last_at": _ts(11, 0), "median_pct": 0.2, "max_pct": 0.4, "minutes": 3}
    assert lb.spread_stats(rows, "2026-11-14") is None
    assert lb.spread_stats(rows[3:], SESSION) is None


def test_spread_stats_skips_bad_rows_and_survives_garbage():
    rows = [[_ts(10, 0), 0.2, 0.3, 1], [None, 0.2, 0.3], [_ts(10, 1), "x", 0.3], [_ts(10, 2), float("nan"), 1], [_ts(10, 3), -1, 1], [_ts(10, 4)], "zz", 5]
    s = lb.spread_stats(rows, SESSION)
    assert s is not None and s["minutes"] == 1 and s["last_pct"] == 0.2
    for bad in (None, 5, "x", {}, []):
        assert lb.spread_stats(bad, SESSION) is None


def test_session_date_from_the_bar_store_day_number():
    n = int((_ts(10, 0) + 330 * 60) // 86400)
    assert lb.session_date(n) == SESSION
    for bad in (None, "x", float("nan"), float("inf")):
        assert lb.session_date(bad) is None


def test_build_reads_the_real_bar_store_and_the_recorded_spread():
    with _env() as root:
        _fill(30)
        sr.write_symbol(root, SESSION, "S10", [[_ts(10, 0), 0.12, 0.2, 4], [_ts(10, 1), 0.18, 0.25, 4]])
        sr.write_symbol(root, "2026-11-12", "S11", [[_ts(10, 0, "2026-11-12"), 0.5, 0.6, 4]])
        b = lb.build("S10")
        assert b["session"] == SESSION
        t = b["turnover"]
        assert t["of"] == 30 and t["pct_rank"] == round(100.0 * 10 / 30, 1) and t["as_of"] == _ts(10, 1) // 60 * 60
        assert t["value"] == round((300000.0 + 10 * 100000.0 + 10.0) * 100.0, 0)
        assert b["spread"] == {"last_pct": 0.18, "last_at": _ts(10, 1), "median_pct": 0.15, "max_pct": 0.25, "minutes": 2}
        assert lb.build("S11")["spread"] is None, "an older session's spread is not shown for today"
        assert lb.build("S12")["spread"] is None, "no recorded reading = nothing, not a guess"


def test_build_for_an_untracked_symbol_or_a_thin_board_gives_dashes_not_zeros():
    with _env():
        _fill(5)
        b = lb.build("S01")
        assert b["turnover"]["pct_rank"] is None and b["turnover"]["of"] == 5 and b["turnover"]["value"] > 0
        nb = lb.build("NOSUCH")
        assert nb["turnover"] is None and nb["spread"] is None
        bar_store.reset()
        assert lb.build("S01") == {"session": None, "turnover": None, "spread": None}


def test_build_never_raises():
    orig = bar_store.get_bars
    try:
        bar_store.get_bars = lambda s: 1 / 0
        assert lb.build("S01")["turnover"] is None
    finally:
        bar_store.get_bars = orig


def test_it_is_display_only_and_wired_into_the_popup_route_alone():
    root = os.path.dirname(HERE)
    users = []
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in ("tests", "__pycache__", ".git")]
        for f in fn:
            if f.endswith(".py") and f != "liquidity_badge.py":
                if re.search(r"liquidity_badge", open(os.path.join(dp, f), encoding="utf-8", errors="ignore").read()):
                    users.append(os.path.relpath(os.path.join(dp, f), root).replace(os.sep, "/"))
    assert users == ["main.py"], users
    main_src = open(os.path.join(root, "main.py"), encoding="utf-8").read()
    i = main_src.index('@app.get("/board/{symbol}")')
    block = main_src[i:main_src.index('@app.get("/dhan/account")', i)]
    assert 'view["liquidity"] = liquidity_badge.build(sym)' in block and "except Exception" in block


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok -", name)
            except Exception as e:  # noqa: BLE001
                fails += 1
                print("FAIL -", name, repr(e))
    sys.exit(1 if fails else 0)
