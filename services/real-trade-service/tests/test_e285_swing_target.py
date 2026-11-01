"""E285 round 91: the swing take-profit level learned from the ledger (swing/learned_target.py), the peak the ledger now records (lane.peak_pct_after, engine.settle_shadow)
and the wiring in swing/live.py. Plain asserts: pytest, or `python3 tests/test_e285_swing_target.py` from the service folder. No database, no network."""
from __future__ import annotations

import os
import re
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
SVC = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(SVC))
sys.path.insert(0, SVC)
sys.path.insert(0, os.path.join(REPO, "scripts"))
sys.path.insert(0, os.path.join(REPO, "shared"))

import config  # noqa: E402
from swing import engine, lane, learned_target as lt  # noqa: E402
from swing.repo import MemRepo, TRADE_FIELDS  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


def closed(peak, net):
    return {"peak_pct": peak, "net_pct": net, "status": "CLOSED"}


def ledger(winner_peaks, n_losers, loser_peak=0.5):
    return [closed(p, 1.0) for p in winner_peaks] + [closed(loser_peak, -1.0) for _ in range(n_losers)]


# ───────────── pure helpers ─────────────

def test_peak_never_lowers_and_counts_the_high_and_the_close():
    assert lane.peak_pct_after(None, 100, 104, 102) == 4.0
    assert lane.peak_pct_after(4.0, 100, 101, 100.5) == 4.0                 # a lower bar never lowers the peak
    assert lane.peak_pct_after(4.0, 100, 107, 103) == 7.0
    assert lane.peak_pct_after(None, 100, None, 103) == 3.0                 # no usable high: the close counts
    assert lane.peak_pct_after(None, 100, "x", None) is None                # nothing readable
    assert lane.peak_pct_after(2.5, None, 110, 105) == 2.5                  # no entry: the previous peak is kept
    assert lane.peak_pct_after(None, 0, 110, 105) is None
    assert lane.peak_pct_after(None, 100, 98, 97) == -2.0                   # a trade that never went above the entry keeps its honest (negative) peak


def test_target_sale_only_when_the_price_reaches_the_level():
    assert "profit taken" in lane.target_sale(106.0, 105.0) and "105.00" in lane.target_sale(106.0, 105.0)
    assert lane.target_sale(105.0, 105.0) is not None                       # at the level counts
    assert lane.target_sale(104.99, 105.0) is None
    assert lane.target_sale(None, 105.0) is None and lane.target_sale(110.0, None) is None and lane.target_sale(110.0, 0) is None
    assert lane.target_sale("junk", 105.0) is None


def test_nothing_is_learned_under_thirty_trades_or_ten_winners():
    assert lt.learned_pct(ledger([6.0] * 9, 25)) is None                    # 34 trades, 9 winners
    assert lt.learned_pct(ledger([6.0] * 15, 14)) is None                   # 29 trades
    assert lt.learned_pct([]) is None


def test_learned_value_is_the_median_of_the_winners_peaks_only():
    r = lt.learned_pct(ledger([4.0, 6.0, 8.0, 10.0] * 5, 20, loser_peak=30.0))      # 40 trades, 20 winners; the losers' big peaks must not count
    assert r == {"pct": 7.0, "n": 40, "winners": 20}


def test_rows_without_a_readable_peak_or_result_are_skipped():
    bad = [{"peak_pct": None, "net_pct": 1.0}, {"peak_pct": "x", "net_pct": 1.0}, {"peak_pct": 5.0, "net_pct": None}, {}]
    assert lt.learned_pct(bad + ledger([6.0] * 20, 20))["n"] == 40


def test_a_median_that_is_not_positive_learns_nothing():
    assert lt.learned_pct(ledger([0.0] * 12, 20)) is None


def test_floors_the_target_is_never_nearer_than_min_r_stops_or_min_pct():
    assert lt.target_pct_for(7.0, 3.0, 1.5, 3.0) == 7.0                     # learned above both floors
    assert lt.target_pct_for(3.5, 3.0, 1.5, 3.0) == 4.5                     # 1.5 x the 3 % stop
    assert lt.target_pct_for(2.0, 1.0, 1.5, 3.0) == 3.0                     # the absolute floor
    assert lt.target_pct_for(None, 3.0, 1.5, 3.0) is None and lt.target_pct_for(0, 3.0, 1.5, 3.0) is None
    assert lt.target_pct_for(5.0, None, 1.5, 3.0) == 5.0                    # no stop: only the absolute floor


# ───────────── learned() / target_px_for() with a fake ledger ─────────────

class _FakeRepo:
    calls = 0
    rows = []

    def __init__(self, db):
        pass

    def trades(self, **kw):
        _FakeRepo.calls += 1
        return list(_FakeRepo.rows)


def _with_fake_ledger(fn):
    import swing.repo as repo_mod
    real = repo_mod.SqlRepo
    keep = (getattr(config, "SWING_LEARNED_TARGET_ENABLED", True), getattr(config, "SWING_TARGET_MIN_R", 1.5), getattr(config, "SWING_TARGET_MIN_PCT", 3.0))
    repo_mod.SqlRepo = _FakeRepo
    config.SWING_LEARNED_TARGET_ENABLED, config.SWING_TARGET_MIN_R, config.SWING_TARGET_MIN_PCT = True, 1.5, 3.0
    lt.reset()
    _FakeRepo.calls, _FakeRepo.rows = 0, []
    try:
        fn()
    finally:
        repo_mod.SqlRepo = real
        config.SWING_LEARNED_TARGET_ENABLED, config.SWING_TARGET_MIN_R, config.SWING_TARGET_MIN_PCT = keep
        lt.reset()


def test_target_px_for_is_none_until_the_ledger_has_the_trades_then_floors_apply():
    def body():
        db = object()
        assert lt.target_px_for(db, 100.0, 3.0) is None                                     # empty ledger: no target
        lt.reset()
        _FakeRepo.rows = ledger([4.0, 6.0, 8.0, 10.0] * 5, 20)                              # median winner peak 7 %
        assert lt.target_px_for(db, 100.0, 3.0) == 107.0
        assert lt.target_px_for(db, 200.0, 6.0) == 218.0                                    # 1.5 x the 6 % stop = 9 % beats the learned 7 %
        assert lt.target_px_for(db, 0, 3.0) is None and lt.target_px_for(db, None, 3.0) is None
    _with_fake_ledger(body)


def test_switch_off_or_no_database_means_no_target_and_no_read():
    def body():
        _FakeRepo.rows = ledger([4.0, 6.0, 8.0, 10.0] * 5, 20)
        config.SWING_LEARNED_TARGET_ENABLED = False
        assert lt.target_px_for(object(), 100.0, 3.0) is None and _FakeRepo.calls == 0
        config.SWING_LEARNED_TARGET_ENABLED = True
        assert lt.target_px_for(None, 100.0, 3.0) is None and _FakeRepo.calls == 0
    _with_fake_ledger(body)


def test_the_ledger_is_read_once_per_cache_window_and_a_read_error_means_no_target():
    def body():
        _FakeRepo.rows = ledger([4.0, 6.0, 8.0, 10.0] * 5, 20)
        db = object()
        assert lt.learned(db, now_ts=1000.0)["pct"] == 7.0
        assert lt.learned(db, now_ts=1100.0)["pct"] == 7.0 and _FakeRepo.calls == 1         # cached
        assert lt.learned(db, now_ts=1700.0)["pct"] == 7.0 and _FakeRepo.calls == 2         # window over: read again

        def boom(self, **kw):
            raise RuntimeError("db down")
        _FakeRepo.trades = boom
        lt.reset()
        try:
            assert lt.learned(db, now_ts=5000.0) is None and lt.target_px_for(db, 100.0, 3.0) is None
        finally:
            _FakeRepo.trades = _orig_trades
    _orig_trades = _FakeRepo.trades
    _with_fake_ledger(body)


# ───────────── the shadow lane records the peak and still plans with no target ─────────────

def _is_td(d: date) -> bool:
    return d.weekday() < 5


def _next_td(d: date) -> date:
    n = d + timedelta(days=1)
    while not _is_td(n):
        n += timedelta(days=1)
    return n


def _epoch(day: str) -> float:
    d = date.fromisoformat(day)
    return datetime(d.year, d.month, d.day, 15, 30, tzinfo=IST).timestamp()


def _row(sym, day, o, hi, lo, c, rank=1, screen=True):
    return {"symbol": sym, "status": "ok", "rank": rank, "pct_rank": 95.0, "screen": screen, "last_close": c, "last_open": o, "last_high": hi, "last_low": lo,
            "last_t": _epoch(day), "atr14_pct": 2.0, "sma10": c * 0.98, "above_sma200": True}


def _scan(repo, day, rows):
    fillers = [_row(f"F{i}", day, 100, 100, 99, 100, rank=10 + i, screen=False) for i in range(6)]
    snap = {"asof_bar_date": day, "rows": rows + fillers, "n_requested": len(rows) + 6, "failed": [], "notes": []}
    return engine.run_scan(repo, snap, vetoes_fn=lambda s, d: [], calendar=lambda: (_is_td, _next_td), cost_fn=lambda e, x, q: 0.0)


def test_shadow_trades_record_their_peak_and_never_get_a_target():
    keep = config.SWING_MAX_HOLD_DAYS
    config.SWING_MAX_HOLD_DAYS = 4
    try:
        repo = MemRepo()
        _scan(repo, "2026-10-26", [_row("AAA", "2026-10-26", 100, 101, 99, 100)])                    # Mon: signal
        _scan(repo, "2026-10-27", [_row("AAA", "2026-10-27", 101, 104, 100, 102)])                   # Tue: entry at 101.101, high 104
        t = repo.trades(kind="SHADOW", symbol="AAA")[0]
        assert abs(t["peak_pct"] - (104 / 101.101 - 1) * 100) < 1e-3 and t["target_px"] is None
        _scan(repo, "2026-10-28", [_row("AAA", "2026-10-28", 102, 103, 101, 102)])                   # Wed: a lower high must not lower the peak
        t = repo.trades(kind="SHADOW", symbol="AAA")[0]
        assert abs(t["peak_pct"] - (104 / 101.101 - 1) * 100) < 1e-3
        _scan(repo, "2026-10-29", [_row("AAA", "2026-10-29", 103, 109, 102, 108)])                   # Thu: a new high
        t = repo.trades(kind="SHADOW", symbol="AAA")[0]
        assert abs(t["peak_pct"] - (109 / 101.101 - 1) * 100) < 1e-3 and t["status"] == "OPEN" and t["target_px"] is None
    finally:
        config.SWING_MAX_HOLD_DAYS = keep


def test_the_ledger_has_the_two_new_fields_and_memrepo_accepts_them():
    assert "peak_pct" in TRADE_FIELDS and "target_px" in TRADE_FIELDS
    repo = MemRepo()
    tid = repo.add_trade(kind="LIVE", mode="DEMO", symbol="X", status="OPEN", signal_day="2026-10-26", target_px=107.0)
    repo.update_trade(tid, peak_pct=5.5)
    t = repo.trades()[0]
    assert t["target_px"] == 107.0 and t["peak_pct"] == 5.5
    assert MemRepo().add_trade(kind="SHADOW", symbol="Y", status="PLANNED", signal_day="2026-10-26") and MemRepo().trades() == []


# ───────────── wiring in the files that cannot be imported without a database ─────────────

def _src(rel):
    with open(os.path.join(SVC, rel), encoding="utf-8") as f:
        return f.read()


def test_live_entries_store_the_target_but_the_order_carries_none():
    s = _src("swing/live.py")
    assert "learned_target.target_px_for(db, limit, sp)" in s and "target_px=tgt" in s
    assert "target_price=None" in s                                                       # the broker order is unchanged: no resting target
    assert "asyncio.to_thread(learned_target" not in s                                    # the session is not thread-safe: the read runs inline


def test_eod_holds_sells_at_the_target_only_after_hold_verdict_said_keep_and_before_the_cap():
    s = _src("swing/live.py")
    body = s[s.index("async def eod_holds"):s.index("def after_scan")]
    i_verdict, i_target, i_cap = body.index("lane.hold_verdict("), body.index("lane.target_sale("), body.index("carry cap")
    assert i_verdict < i_target < i_cap
    seg = body[body.rindex("if ok and t", 0, i_target):i_target + 200]
    assert "SWING_LEARNED_TARGET_ENABLED" in seg and "ok, why = False," in body[i_target:i_cap]      # only ever turns a keep into a sale
    assert "ok, why = True" not in body                                                              # never turns a sale into a keep


def test_after_scan_updates_the_open_rows_peak_from_the_evening_bar():
    s = _src("swing/live.py")
    body = s[s.index("def after_scan"):]
    assert re.search(r'f\.update\(peak_pct=lane\.peak_pct_after\(t\.get\("peak_pct"\)', body)
    assert body.index("peak_pct=lane.peak_pct_after") > body.index("elif asof and")           # only in the branch where the scan row exists


def test_columns_migration_and_switches_are_in_place():
    m = _src("models.py")
    seg = m[m.index("class SwingTrade"):m.index("class SwingRun")]
    assert "peak_pct = Column(Float, nullable=True)" in seg and "target_px = Column(Float, nullable=True)" in seg
    d = _src("db.py")
    assert "_ensure_swing_ledger_columns(eng, dialect())" in d and 'for col_name in ("peak_pct", "target_px")' in d
    cat = _src("control/catalog.py")
    for k in ("SWING_LEARNED_TARGET_ENABLED", "SWING_TARGET_MIN_R", "SWING_TARGET_MIN_PCT"):
        assert f'"{k}"' in cat and k in _src("config.py")
    assert config.SWING_LEARNED_TARGET_ENABLED is True and config.SWING_TARGET_MIN_R == 1.5 and config.SWING_TARGET_MIN_PCT == 3.0


def test_the_shadow_lane_does_not_read_the_target_switch():
    e = _src("swing/engine.py")
    assert "learned_target" not in e and "SWING_LEARNED_TARGET_ENABLED" not in e and "target_px" not in e


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok", name)
            except Exception as e:  # noqa: BLE001
                fails += 1
                print("FAIL", name, type(e).__name__, e)
    sys.exit(1 if fails else 0)
