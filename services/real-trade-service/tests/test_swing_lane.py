"""E132 (swing plan S3-S5): the pure lane rules, the evidence engine on an in-memory ledger, the exit / EOD wiring, and the S2 replay parity.
Plain asserts: pytest, or `python3 tests/test_swing_lane.py` from the service folder. No database, no network."""
from __future__ import annotations

import os
import random
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
SVC = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(SVC))
sys.path.insert(0, SVC)
sys.path.insert(0, os.path.join(REPO, "scripts"))
sys.path.insert(0, os.path.join(REPO, "shared"))

import config  # noqa: E402
from swing import engine, lane, stats  # noqa: E402
from swing.repo import MemRepo  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


def is_td(d: date) -> bool:
    return d.weekday() < 5


def next_td(d: date) -> date:
    n = d + timedelta(days=1)
    while not is_td(n):
        n += timedelta(days=1)
    return n


CAL = lambda: (is_td, next_td)  # noqa: E731
NOVETO = lambda s, d: []  # noqa: E731


def epoch(day: str) -> float:
    d = date.fromisoformat(day)
    return datetime(d.year, d.month, d.day, 15, 30, tzinfo=IST).timestamp()


def row(sym, day, o, lo, c, rank=1, screen=True, atr=2.0, pct=95.0):
    return {"symbol": sym, "status": "ok", "rank": rank, "pct_rank": pct, "screen": screen, "last_close": c, "last_open": o, "last_high": max(o, c) * 1.01,
            "last_low": lo, "last_t": epoch(day), "atr14_pct": atr, "sma10": c * 0.98, "above_sma200": True}


def snap(day, rows):
    return {"asof_bar_date": day, "rows": rows, "n_requested": len(rows), "failed": [], "notes": []}


def filler(day, n=6):
    return [row(f"F{i}", day, 100, 99, 100, rank=10 + i, screen=False) for i in range(n)]


FREE_COST = lambda e, x, q: 0.0  # noqa: E731


# ───────────── pure rules ─────────────

def test_stop_pct_matches_the_replay_definition():
    import entry_rules_backtest as er
    for atr in (None, 0, -1, 0.5, 1.0, 1.7, 2.9, 4.0, 9.0):
        assert abs(lane.stop_pct_from_atr(atr, 1.5, 2.0, 6.0) - er.stop_target(atr, 1.5, 1.0)[0]) < 1e-12, atr


def test_atr_pct_matches_the_replay_definition():
    import entry_rules_backtest as er
    from swing import momentum
    rng = random.Random(3)
    c, bars = 100.0, []
    for i in range(60):
        o = c
        c = o * (1 + rng.uniform(-0.03, 0.03))
        bars.append((1e9 + i * 86400, o, max(o, c) * 1.01, min(o, c) * 0.99, c, 1000.0))
    for i in (20, 40, 59):
        assert abs(momentum.atr_pct_simple(bars[: i + 1]) - er.atr_pct(bars, i)) < 1e-9


def test_advance_equals_the_replayed_trail_walk():
    import swing_backtest as sb
    from market_structure.ms_types import C, L, O
    rng = random.Random(11)
    for trial in range(300):
        c, bars = 100.0, []
        for i in range(8):
            o = c * (1 + rng.uniform(-0.04, 0.04))
            c = o * (1 + rng.uniform(-0.05, 0.05))
            bars.append((1e9 + i * 86400, o, max(o, c) * 1.01, min(o, c) * 0.99, c, 1000.0))
        sp = rng.choice([2.0, 3.3, 6.0])
        entry = bars[0][O] * 1.001
        gross, how = sb.walk_trail(bars, 0, entry, sp, 5)
        st = {"entry": entry, "stop_pct": sp, "stop_px": entry * (1 - sp / 100), "sessions": 0}
        for k in range(5):
            st = lane.advance(st, {"o": bars[k][O], "l": bars[k][L], "c": bars[k][C], "day": str(k)}, 5)
            if st["closed"]:
                break
        assert st["closed"], trial
        g2 = (st["exit_price"] / entry - 1) * 100
        assert abs(g2 - gross) < 1e-9 and (st["exit_reason"] == "stop") == (how == "stop"), (trial, g2, gross, how, st["exit_reason"])


def test_gap_through_the_stop_exits_at_the_open_and_is_flagged():
    st = {"entry": 100.0, "stop_pct": 3.0, "stop_px": 97.0, "sessions": 0}
    r = lane.advance(st, {"o": 94.0, "l": 93.0, "c": 95.0, "day": "d"}, 5)
    assert r["closed"] and r["exit_price"] == 94.0 and r["gap_through"] is True
    r = lane.advance(st, {"o": 99.0, "l": 96.0, "c": 98.0, "day": "d"}, 5)
    assert r["closed"] and r["exit_price"] == 97.0 and r["gap_through"] is False


def test_hold_verdict_rules_and_fail_closed():
    base = dict(sessions=2, max_hold=5, ltp=105.0, stop=98.0, sma10=100.0, pct_rank=90.0, keep_min_pct_rank=50.0, require_above_sma10=True, vetoes=())
    assert lane.hold_verdict(**base)[0] is True
    assert lane.hold_verdict(**{**base, "sessions": 5})[0] is False
    assert lane.hold_verdict(**{**base, "ltp": None})[0] is False
    assert lane.hold_verdict(**{**base, "ltp": 97.0})[0] is False
    assert lane.hold_verdict(**{**base, "vetoes": ("results tomorrow",)})[0] is False
    assert lane.hold_verdict(**{**base, "sma10": None})[0] is False
    assert lane.hold_verdict(**{**base, "ltp": 99.0, "stop": 90.0})[0] is False           # under its 10-day average
    assert lane.hold_verdict(**{**base, "pct_rank": 40.0})[0] is False
    assert lane.hold_verdict(**{**base, "pct_rank": None})[0] is False
    assert lane.hold_verdict(**{**base, "keep_min_pct_rank": 0, "pct_rank": None})[0] is True   # rule off


def test_sessions_count_the_entry_session_and_skip_weekends():
    fri = date(2026, 10, 23)
    assert lane.count_sessions(fri, fri, is_td) == 1
    assert lane.count_sessions(fri, date(2026, 10, 27), is_td) == 3          # Fri, Mon, Tue
    assert lane.count_sessions(date(2026, 10, 27), fri, is_td) == 0


def test_pause_rules():
    L = lambda net, gross=None, sp=3.0, at="2026-10-20T10:00:00": {"closed_at": at, "net_pct": net, "gross_pct": net if gross is None else gross, "stop_pct": sp}  # noqa: E731
    assert lane.pause_state([L(-1), L(-2), L(-0.5)], 3, True, 1.3)[0] is True
    assert lane.pause_state([L(-1), L(2), L(-0.5)], 3, True, 1.3)[0] is False
    assert lane.pause_state([L(-1), L(-2)], 3, True, 1.3)[0] is False
    assert lane.pause_state([L(-5.0, sp=3.0)], 3, True, 1.3)[0] is True                  # lost 5% against a 3% stop: a gap
    assert lane.pause_state([L(-5.0, sp=3.0)], 3, False, 1.3)[0] is False
    assert lane.pause_state([L(-1)] * 3, 0, True, 1.3)[0] is False                         # rule off
    assert lane.pause_state([L(-1)] * 3, 3, True, 1.3, resumed_at="2026-10-21T00:00:00")[0] is False   # resumed after them


def test_sizing_and_slots():
    assert lane.entry_qty(100.0, 20000, 100000, 40, 0) == 200
    assert lane.entry_qty(100.0, 20000, 100000, 40, 30000) == 100                         # 40% of 100k = 40k, 30k used
    assert lane.entry_qty(100.0, 20000, 100000, 40, 40000) == 0
    assert lane.entry_qty(100.0, 20000, 0, 40, 0) == 0                                     # no equity: no entry
    assert lane.entry_qty(0, 20000, 100000, 40, 0) == 0
    assert lane.entry_slots(2, 1, 2) == 1 and lane.entry_slots(2, 2, 2) == 0 and lane.entry_slots(0, 0, 2) == 0


def test_live_is_demo_only_until_real_is_enabled():
    assert lane.live_allowed("live", "DEMO", False) and not lane.live_allowed("live", "REAL", False) and lane.live_allowed("live", "REAL", True)
    assert not lane.live_allowed("shadow", "DEMO", True) and not lane.live_allowed("off", "DEMO", True) and not lane.live_allowed("bogus", "DEMO", True)
    assert lane.norm_mode("LIVE ") == "live" and lane.norm_mode("x") == "shadow"


def test_ratchet_never_lowers_and_pick_needs_screen_rank_atr():
    assert lane.ratcheted_stop(97.0, 110.0, 3.0) == 106.7 and lane.ratcheted_stop(120.0, 110.0, 3.0) == 120.0 and lane.ratcheted_stop(None, 100, 0) is None
    rows = [row("A", "2026-10-26", 100, 99, 100, rank=2), row("B", "2026-10-26", 100, 99, 100, rank=1), row("C", "2026-10-26", 100, 99, 100, rank=3, screen=False),
            row("D", "2026-10-26", 100, 99, 100, rank=4, atr=0), row("E", "2026-10-26", 100, 99, 100, rank=5)]
    assert [r["symbol"] for r in lane.pick_candidates(rows, {"E"}, 5)] == ["B", "A"]
    assert lane.earnings_block(date(2026, 10, 30), date(2026, 10, 26), 7) and not lane.earnings_block(date(2026, 11, 20), date(2026, 10, 26), 7)
    assert lane.earnings_block(None, date(2026, 10, 26), 7) is None and lane.earnings_block(date(2026, 10, 30), date(2026, 10, 26), 0) is None


# ───────────── the evidence engine on a memory ledger ─────────────

def _scan(repo, day, rows, veto=NOVETO):
    return engine.run_scan(repo, snap(day, rows + filler(day)), vetoes_fn=veto, calendar=CAL, cost_fn=FREE_COST)


def test_shadow_trade_lifecycle_plan_enter_stop_ratchet_time_exit():
    config.SWING_MAX_HOLD_DAYS = 3
    repo = MemRepo()
    r = _scan(repo, "2026-10-26", [row("AAA", "2026-10-26", 100, 99, 100, rank=1)])          # Mon: signal
    assert r["status"] == "scanned" and r["planned"]["planned"] == 1
    t = repo.trades(kind="SHADOW")[0]
    assert t["status"] == "PLANNED" and t["stop_pct"] == 3.0 and t["signal_close"] == 100
    _scan(repo, "2026-10-27", [row("AAA", "2026-10-27", 101, 100, 102, rank=1)])             # Tue: entry at the open + 0.10 %, session 1
    t = repo.trades(kind="SHADOW", symbol="AAA")[0]
    assert t["status"] == "OPEN" and abs(t["entry_price"] - 101.101) < 1e-6 and t["sessions"] == 1
    assert abs(t["stop_px"] - 102 * 0.97) < 1e-3                                                # ratcheted to close - 3 %
    _scan(repo, "2026-10-28", [row("AAA", "2026-10-28", 103, 101, 104, rank=1)])             # Wed: session 2
    _scan(repo, "2026-10-29", [row("AAA", "2026-10-29", 105, 103, 106, rank=1)])             # Thu: session 3 = last: exits at the close
    t = repo.trades(kind="SHADOW", symbol="AAA")[0]
    assert t["status"] == "CLOSED" and t["exit_reason"] == "time" and t["exit_price"] == 106 and t["sessions"] == 3
    assert abs(t["gross_pct"] - (106 / 101.101 - 1) * 100) < 1e-3 and t["net_pct"] == t["gross_pct"]
    config.SWING_MAX_HOLD_DAYS = 5


def test_stop_exit_gap_and_costs_are_charged():
    config.SWING_MAX_HOLD_DAYS = 5
    repo = MemRepo()
    engine.run_scan(repo, snap("2026-10-26", [row("BBB", "2026-10-26", 100, 99, 100, rank=1)] + filler("2026-10-26")), vetoes_fn=NOVETO, calendar=CAL, cost_fn=lambda e, x, q: 0.5)
    engine.run_scan(repo, snap("2026-10-27", [row("BBB", "2026-10-27", 100, 99.5, 100.5, rank=1)] + filler("2026-10-27")), vetoes_fn=NOVETO, calendar=CAL, cost_fn=lambda e, x, q: 0.5)
    engine.run_scan(repo, snap("2026-10-28", [row("BBB", "2026-10-28", 90, 89, 91, rank=1, screen=False)] + filler("2026-10-28")), vetoes_fn=NOVETO, calendar=CAL, cost_fn=lambda e, x, q: 0.5)
    t = repo.trades(kind="SHADOW", symbol="BBB")[0]
    assert t["status"] == "CLOSED" and t["exit_reason"] == "stop" and t["gap_through"] is True and t["exit_price"] == 90      # gapped under the ratcheted stop
    assert abs(t["net_pct"] - (t["gross_pct"] - 0.5)) < 1e-3 and t["net_pct"] < -9


def test_scan_is_idempotent_and_vetoes_and_gaps_are_recorded():
    repo = MemRepo()
    veto = lambda s, d: ["results due 2026-10-29 (3 day(s) away, inside the 7-day hold window)"] if s == "VET" else []  # noqa: E731
    rows = [row("VET", "2026-10-26", 100, 99, 100, rank=1), row("OK1", "2026-10-26", 100, 99, 100, rank=2), row("GAP", "2026-10-26", 100, 99, 100, rank=3)]
    r1 = _scan(repo, "2026-10-26", rows, veto)
    assert r1["planned"]["planned"] == 2 and r1["planned"]["vetoed"] == 1
    assert _scan(repo, "2026-10-26", rows, veto)["status"] == "already_done"
    assert len(repo.trades(kind="SHADOW")) == 3
    assert [t["status"] for t in repo.trades(kind="SHADOW", symbol="VET")] == ["SKIPPED"]
    _scan(repo, "2026-10-27", [row("OK1", "2026-10-27", 100, 99, 100, rank=2), row("GAP", "2026-10-27", 108, 107, 109, rank=3)])     # GAP opens +8 %
    assert repo.trades(kind="SHADOW", symbol="GAP")[0]["status"] == "SKIPPED" and "above the signal close" in repo.trades(kind="SHADOW", symbol="GAP")[0]["skip_reason"]
    assert repo.trades(kind="SHADOW", symbol="OK1")[0]["status"] == "OPEN"


def test_a_missed_session_voids_instead_of_inventing_a_path():
    repo = MemRepo()
    _scan(repo, "2026-10-26", [row("AAA", "2026-10-26", 100, 99, 100, rank=1)])
    _scan(repo, "2026-10-27", [row("AAA", "2026-10-27", 100, 99, 101, rank=1)])
    _scan(repo, "2026-10-29", [row("AAA", "2026-10-29", 100, 99, 101, rank=1)])               # Wed 28th was never scanned
    assert repo.trades(kind="SHADOW", symbol="AAA")[0]["status"] == "VOID"
    repo2 = MemRepo()
    _scan(repo2, "2026-10-26", [row("ZZZ", "2026-10-26", 100, 99, 100, rank=1)])
    _scan(repo2, "2026-10-28", [row("ZZZ", "2026-10-28", 100, 99, 101, rank=1)])               # the entry session was missed
    assert repo2.trades(kind="SHADOW", symbol="ZZZ")[0]["status"] == "SKIPPED"


def test_bad_snapshot_is_refused_without_writing():
    repo = MemRepo()
    assert engine.run_scan(repo, {"asof_bar_date": None, "rows": []}, vetoes_fn=NOVETO, calendar=CAL)["ok"] is False
    assert engine.run_scan(repo, snap("2026-10-26", filler("2026-10-26", 3)), vetoes_fn=NOVETO, calendar=CAL)["status"] == "nothing_ranked"
    assert repo.trades() == [] and repo.latest_run("scan") is None


def test_latest_scan_round_trips_the_compact_rows():
    repo = MemRepo()
    _scan(repo, "2026-10-26", [row("AAA", "2026-10-26", 100, 99, 100, rank=1)])
    s = engine.latest_scan(repo)
    assert s["asof"] == "2026-10-26" and s["rows"]["AAA"]["sma10"] == 98.0 and s["rows"]["AAA"]["last_bar_day"] == "2026-10-26" and s["n_screen"] == 1


def test_verdict_words():
    rows = [{"entry_day": f"2026-09-{(i % 25) + 1:02d}", "net_pct": 1.0 + (i % 3) * 0.2, "gross_pct": 1.3, "exit_reason": "time"} for i in range(60)]
    assert stats.verdict(rows[:10])["verdict"] == "NOT_ENOUGH_DATA"
    assert stats.verdict(rows, min_days=20)["verdict"] == "USEFUL"
    bad = [dict(r, net_pct=-0.5 + (i % 3) * 0.2, gross_pct=0.1) for i, r in enumerate(rows)]
    v = stats.verdict(bad, min_days=20)
    assert v["verdict"] == "NO_EDGE" and any("costs eat" in x for x in v["reasons"])


def test_report_script_needs_enough_days_and_computes_forward_returns():
    import swing_shadow_report as rpt
    snaps = []
    d = date(2026, 10, 5)
    for i in range(30):
        while not is_td(d):
            d += timedelta(days=1)
        px = 100 + i
        rows = {f"S{k}": {"symbol": f"S{k}", "last_close": px * (1 + 0.01 * k), "rank": k, "screen": k < 3} for k in range(10)}
        snaps.append(rpt._snap(d.isoformat(), list(rows.values())))
        d += timedelta(days=1)
    rep = rpt.build_report(snaps, 5, 20000, 0.1, 30, 20, 100)
    assert rep["verdict"] in ("USEFUL", "NO_EDGE", "NOT_ENOUGH_DATA") and rep["horizons"]["5"]["screened"]["n"] > 0
    assert rpt.build_report(snaps[:5], 5, 20000, 0.1, 30, 20, 100)["verdict"] == "NOT_ENOUGH_DATA"
    assert rpt.to_markdown(rep).startswith("VERDICT:")


# ───────────── wiring that cannot be exercised without the service (static) ─────────────

def _src(*p):
    return open(os.path.join(SVC, *p), encoding="utf-8").read()


def test_eod_squareoff_asks_the_swing_lane_and_keeps_swing_out_of_the_overnight_selector():
    s = _src("execution", "auto_pilot.py")
    assert "_swing_live.eod_holds" in s and 'upper() != "SWING_MOMENTUM"' in s and "await _swing_step(db, mode" in s


def test_exit_engine_owns_no_target_trail_for_swing_positions():
    s = _src("exit_engine", "exit.py")
    i, j = s.index("1b. E132 swing lane"), s.index("# ── 2. First target hit")
    assert s.index("# ── 1. Stop hit") < i < j and 'current_target = None' in s[i:j] and "continue" in s[i:j]


def test_manual_engine_default_behaviour_is_unchanged_without_swing():
    s = _src("manual_engine.py")
    assert 'execution_source=("AUTO" if swing else "MANUAL")' in s and "swing: Optional[dict] = None" in s and "(None if swing else target_price)" in s


def test_switches_ship_live_in_demo_and_real_by_default():
    assert config.SWING_MODE == "live" and config.SWING_LIVE_REAL_ENABLED is True and config.SWING_BROKER_STOP_ENABLED is False   # E233: REAL is on by default
    assert config.SWING_MAX_HOLD_DAYS == 5 and config.SWING_MAX_POSITIONS == 2 and config.SWING_PAUSE_AFTER_LOSSES == 3


def test_models_and_routes_exist():
    m = _src("models.py")
    assert "trade_swing_ledger" in m and "trade_swing_runs" in m
    r = _src("main.py")
    for route in ("/swing/status", "/swing/ledger", "/swing/report", "/swing/snapshots", "/swing/scan", "/swing/resume", "/swing/probe"):
        assert f'"{route}"' in r, route


def test_probe_judgement():
    from swing import probe
    assert probe.judge_check({"orderStatus": "PENDING"})["verdict"] == "PASS"
    assert probe.judge_check({"orderStatus": "TRIGGER_PENDING"})["verdict"] == "PASS"
    assert probe.judge_check({"orderStatus": "CANCELLED"})["verdict"] == "FAIL"
    assert probe.judge_check(None)["verdict"] == "FAIL"


def test_universe_falls_back_to_the_built_in_list_and_says_so():
    """E133: a fresh deploy (no SWING_UNIVERSE_SYMBOLS, no SWING_RANK_DEFAULT_SYMBOLS) ranks the built-in sector-map names instead of stopping at no_universe;
    an explicit list always wins; positions held are always added."""
    from swing import ranker
    old = (config.SWING_UNIVERSE_SYMBOLS, list(ranker.DEFAULT_SYMBOLS), config.SWING_UNIVERSE_MAX)
    try:
        config.SWING_UNIVERSE_SYMBOLS, ranker.DEFAULT_SYMBOLS[:] = "", []
        config.SWING_UNIVERSE_MAX = 450
        syms = engine.universe_symbols()
        assert engine.universe_source() == "builtin" and 50 <= len(syms) <= 450 and "TCS" in syms and len(set(syms)) == len(syms)
        assert "ZZHELD" in engine.universe_symbols(["zzheld"]) and "ZZHELD" not in syms
        ranker.DEFAULT_SYMBOLS[:] = ["AAA", "BBB"]
        assert engine.universe_source() == "rank_default" and engine.universe_symbols() == ["AAA", "BBB"]
        config.SWING_UNIVERSE_SYMBOLS = "ccc, ddd"
        assert engine.universe_source() == "env" and engine.universe_symbols() == ["CCC", "DDD"]
        config.SWING_UNIVERSE_SYMBOLS, ranker.DEFAULT_SYMBOLS[:] = "", []
        config.SWING_UNIVERSE_MAX = 20
        assert len(engine.universe_symbols()) == 20                     # the cap still applies to the fallback
    finally:
        config.SWING_UNIVERSE_SYMBOLS, config.SWING_UNIVERSE_MAX = old[0], old[2]
        ranker.DEFAULT_SYMBOLS[:] = old[1]


def test_status_route_and_scan_result_report_the_universe_source():
    assert "universe_source" in _src("main.py")
    e = _src("swing/engine.py")
    assert 'res.setdefault("universe"' in e and "no built-in list could be read" in e


def test_scan_records_which_universe_it_ranked():
    repo = MemRepo()
    u = {"source": "builtin", "size": 118}
    r = engine.run_scan(repo, snap("2026-10-26", [row("AAA", "2026-10-26", 100, 99, 100, rank=1)] + filler("2026-10-26")), vetoes_fn=NOVETO, calendar=CAL,
                        cost_fn=FREE_COST, universe=u)
    assert r["universe"] == u and engine.latest_scan(repo)["universe"] == u
    repo2 = MemRepo()
    engine.run_scan(repo2, snap("2026-10-26", [row("AAA", "2026-10-26", 100, 99, 100, rank=1)] + filler("2026-10-26")), vetoes_fn=NOVETO, calendar=CAL, cost_fn=FREE_COST)
    assert engine.latest_scan(repo2)["universe"] is None                 # old callers and old rows still work
    assert "scan_universe" in _src("main.py")


def test_report_since_counts_only_later_signals():
    repo = MemRepo()
    for i, (day, net) in enumerate([("2026-10-01", 1.0), ("2026-10-02", 2.0), ("2026-10-20", -1.0)]):
        repo.add_trade(kind="SHADOW", symbol=f"S{i}", status="CLOSED", signal_day=day, net_pct=net, gross_pct=net, cost_pct=0.0, entry_day=day, exit_day=day)
    assert len(engine.closed_for_stats(repo)) == 3
    assert [t["signal_day"] for t in engine.closed_for_stats(repo, since="2026-10-15")] == ["2026-10-20"]
    r = engine.report(repo, since="2026-10-15")
    assert r["since"] == "2026-10-15" and r["by_status"] == {"CLOSED": 1}
    assert engine.report(repo)["by_status"] == {"CLOSED": 3} and engine.report(repo)["since"] is None
    assert "error" in engine.report(repo, since="yesterday")
    assert "since=(since" in _src("main.py")


def test_probe_judgement_forever_and_day_kinds_e261():
    from swing import probe
    # forever order (GTT): lives in Dhan's forever list; PENDING / CONFIRM / TRANSIT mean "still waiting for its trigger"
    for st in ("PENDING", "CONFIRM", "TRANSIT"):
        assert probe.judge_check({"orderStatus": st}, "forever")["verdict"] == "PASS"
    for st in ("EXPIRED", "CANCELLED", "REJECTED", "TRADED", ""):
        assert probe.judge_check({"orderStatus": st}, "forever")["verdict"] == "FAIL"
    r = probe.judge_check(None, "forever")
    assert r["verdict"] == "FAIL" and "forever-order list" in r["reason"]
    # the old DAY stop: gone / EXPIRED is the expected result, and the reason now says why
    r = probe.judge_check(None, "day")
    assert r["verdict"] == "FAIL" and "DAY order is dropped at the close" in r["reason"]
    assert probe.judge_check({"orderStatus": "EXPIRED"}, "day")["verdict"] == "FAIL"
    assert probe.judge_check({"orderStatus": "CONFIRM"}, "day")["verdict"] == "FAIL"     # CONFIRM is a forever-order status only
    assert probe.judge_check({"orderStatus": "TRIGGER_PENDING"})["verdict"] == "PASS"    # default kind stays "day" (old callers)


def test_probe_uses_forever_orders_by_default_e261():
    src = _src("swing", "probe.py")
    assert 'kind: str = "forever"' in src and "place_forever_order(" in src and "get_forever_orders(db)" in src and "cancel_forever_order(" in src
    assert 'str(prev.get("kind") or "day")' in src                 # a probe saved before E261 has no kind: it placed a DAY stop
    cl = _src("execution", "dhan_client.py")
    assert "/forever/orders" in cl and "/forever/all" in cl and '"orderFlag": "SINGLE"' in cl and 'if not is_armed:' in cl.split("def place_forever_order")[1][:700]
    m = _src("main.py")
    assert 'kind: str = "forever"' in m and "probe.open_phase(db, repo, armed, symbol, kind)" in m


def test_probe_check_releases_the_lock_when_cleanup_fails_e261():
    src = _src("swing", "probe.py")
    tail = src.split("def check_phase")[1]
    exc = tail.split("except Exception as e:  # noqa: BLE001")[1]
    assert "shared_symbol_lock.release(db, sym)" in exc


def test_probe_failed_open_does_not_block_the_next_open_and_releases_the_lock():
    from swing import probe
    assert probe.waiting_for_check({"phase": "open", "stop_id": "9", "checked": False}) is True
    assert probe.waiting_for_check({"phase": "open", "stop_id": "9", "checked": True}) is False
    assert probe.waiting_for_check({"phase": "open", "symbol": "ITC", "verdict": "FAIL"}) is False      # a failed open has no stop to check
    assert probe.waiting_for_check(None) is False
    repo = MemRepo()
    repo.add_run("probe", "2026-10-26", "REAL", False, "open failed", '{"phase": "open", "symbol": "ITC", "verdict": "FAIL"}')
    assert probe.waiting_for_check(probe.last_probe(repo)) is False
    repo.add_run("probe", "2026-10-27", "REAL", True, "open: ITC stop PENDING", '{"phase": "open", "symbol": "ITC", "stop_id": "55", "checked": false}')
    assert probe.waiting_for_check(probe.last_probe(repo)) is True
    src = _src("swing", "probe.py")                      # the SQL / broker half cannot run without SQLAlchemy: check its shape
    exc = src.split("except Exception as e:  # noqa: BLE001\n        msg = str(e)")[1]
    assert "shared_symbol_lock.release(db, sym)" in exc and "held_share" in exc
    assert src.count("waiting_for_check(") >= 3


def test_a_stock_with_a_stale_last_bar_is_never_picked():
    day = "2026-10-26"
    stale = row("STALE", "2026-10-20", 100, 99, 100, rank=1)              # its own last bar is 6 days old: rank 1 on old prices
    good = row("GOOD", day, 100, 99, 100, rank=2)
    repo = MemRepo()
    r = engine.run_scan(repo, snap(day, [stale, good] + filler(day)), vetoes_fn=NOVETO, calendar=CAL, cost_fn=FREE_COST)
    assert r["planned"]["planned"] == 1
    assert [t["symbol"] for t in repo.trades(kind="SHADOW", status="PLANNED")] == ["GOOD"]
    compact = engine.compact_rows([stale, good])
    assert [c["symbol"] for c in lane.pick_candidates(compact, set(), 5, asof=day)] == ["GOOD"]
    assert [c["symbol"] for c in lane.pick_candidates(compact, set(), 5)] == ["STALE", "GOOD"]          # no asof = old behaviour


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
