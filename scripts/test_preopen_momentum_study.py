"""Tests for scripts/preopen_momentum_study.py (E248). Plain asserts: python3 scripts/test_preopen_momentum_study.py  or  pytest."""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _intraday_data as idata  # noqa: E402
import preopen_momentum_study as pms  # noqa: E402


def _dates(k, start=datetime(2026, 9, 1)):
    out, d = [], start
    while len(out) < k:
        if d.weekday() < 5:
            out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return out


def _write(root, date, sym, bars):
    d = Path(root) / date
    d.mkdir(parents=True, exist_ok=True)
    (d / (quote(sym, safe="") + ".json")).write_text(json.dumps({"v": 1, "symbol": sym, "date": date, "tf": 1, "first_ts": bars[0][0],
                                                                  "last_ts": bars[-1][0], "n": len(bars), "backfilled": False, "bars": bars}))


def _session(date, open_px, step_pct, minutes=375):
    """A straight-line session: every minute moves step_pct %. Returns bars [t, o, h, l, c, v]."""
    t0 = idata.open_ts(date)
    out, px = [], open_px
    for m in range(minutes):
        c = px * (1 + step_pct / 100.0)
        out.append([t0 + 60 * m, px, max(px, c) * 1.0001, min(px, c) * 0.9999, c, 1000.0])
        px = c
    return out


def _chain(root, sym, n_days, gap_pct, step_fn):
    """n_days chained sessions: each opens gap_pct away from the previous close and then drifts by step_fn(day_index) % a minute."""
    prev = 100.0
    for i, date in enumerate(_dates(n_days)):
        bars = _session(date, prev * (1 + gap_pct / 100.0), step_fn(i))
        _write(root, date, sym, bars)
        prev = bars[-1][4]


def _find(rep, kind, bucket, horizon):
    return next(r for r in rep["rows"] if r["kind"] == kind and r["bucket"] == bucket and r["horizon"] == horizon)


def test_gap_bucket_edges():
    assert pms.gap_bucket(-4.0) == "gap < -3%"
    assert pms.gap_bucket(-1.0) == "-1% to -0.3%"
    assert pms.gap_bucket(0.0) == "flat (+-0.3%)"
    assert pms.gap_bucket(0.3) == "+0.3% to +1%"
    assert pms.gap_bucket(2.0) == "+1% to +3%"
    assert pms.gap_bucket(3.0) == "gap > +3%"


def test_gap_up_that_keeps_rising_is_an_edge_for_follow_and_a_loss_for_fade():
    with tempfile.TemporaryDirectory() as root:
        for s in ("AAA", "BBB", "CCC"):
            _chain(root, s, 12, 2.0, lambda i: 0.01 * (1 + i % 3))
        data = idata.load_dir(root)
        rep = pms.report(pms.observations(data, 0.3, 0.15), 30, 10)
        f = _find(rep, "A_follow", "ALL", "10:00")
        assert f["n"] == 33 and f["days"] == 11
        assert f["mean"] > 0.0 and f["verdict"] == "edge"
        fade = _find(rep, "A_fade", "ALL", "10:00")
        assert fade["mean"] < 0.0 and fade["verdict"] == "no_edge"
        assert _find(rep, "A_follow", "+1% to +3%", "15:00")["n"] == 33


def test_few_days_never_gets_a_verdict():
    with tempfile.TemporaryDirectory() as root:
        for s in ("AAA", "BBB", "CCC"):
            _chain(root, s, 5, 2.0, lambda i: 0.002)
        rep = pms.report(pms.observations(idata.load_dir(root), 0.3, 0.15), 5, 10)
        assert _find(rep, "A_follow", "ALL", "10:00")["verdict"] == "insufficient_sample"


def test_a_flat_gap_has_no_follow_side_and_a_partial_day_is_ignored():
    with tempfile.TemporaryDirectory() as root:
        _chain(root, "AAA", 4, 0.0, lambda i: 0.001)                 # no gap -> no A_follow row
        dates = _dates(5)
        _write(root, dates[4], "AAA", _session(dates[4], 105.0, 0.001, minutes=100))   # 100 of 375 minutes: not complete
        obs = pms.observations(idata.load_dir(root), 0.3, 0.15)
        assert not [o for o in obs if o["kind"] == "A_follow"]
        assert not [o for o in obs if o["date"] == dates[4]]
        assert [o for o in obs if o["kind"] == "B_5m"]               # the 5-minute direction still gives a side


def test_corporate_action_gap_is_dropped():
    with tempfile.TemporaryDirectory() as root:
        _chain(root, "AAA", 3, 40.0, lambda i: 0.001)
        assert pms.observations(idata.load_dir(root), 0.3, 0.15) == []


def test_orb_stop_fills_at_the_stop_or_a_worse_open():
    t0 = 0.0
    mb = {i: (t0 + 60 * i, 100.0, 101.0, 99.0, 100.5, 1.0) for i in range(5)}      # range 99 .. 101
    mb[5] = (t0 + 300, 100.5, 101.6, 100.4, 101.5, 1.0)                              # closes above the range -> long at 101.5
    mb[6] = (t0 + 360, 101.5, 101.6, 98.0, 98.2, 1.0)                                # trades through the stop (99)
    side, entry, exit_px, stopped = pms._orb(mb, 101.0, 99.0, 45)
    assert side == 1 and entry == 101.5 and stopped is True and exit_px == 99.0
    mb[6] = (t0 + 360, 98.5, 98.6, 98.0, 98.2, 1.0)                                  # opens below the stop: the open is the fill
    assert pms._orb(mb, 101.0, 99.0, 45)[2] == 98.5


def test_orb_short_and_no_breakout():
    mb = {i: (60.0 * i, 100.0, 101.0, 99.0, 100.0, 1.0) for i in range(60)}
    assert pms._orb(mb, 101.0, 99.0, 45) is None
    mb[7] = (420.0, 99.5, 99.6, 98.0, 98.5, 1.0)                                     # closes below the low -> short
    r = pms._orb(mb, 101.0, 99.0, 45)
    assert r is not None and r[0] == -1 and r[1] == 98.5


def test_px_at_uses_the_open_then_the_previous_close():
    mb = {10: (0.0, 50.0, 51.0, 49.0, 50.5, 1.0), 12: (0.0, 52.0, 53.0, 51.0, 52.5, 1.0)}
    assert pms._px_at(mb, 12) == 52.0
    assert pms._px_at(mb, 13) == 52.5          # minute 13 missing: close of minute 12
    assert pms._px_at(mb, 30) is None


def test_summarize_uses_days_as_the_unit():
    # 60 observations on 2 days: n is large but there are only 2 independent days
    pairs = [("d1", 1.0)] * 30 + [("d2", 1.2)] * 30
    assert pms.summarize(pairs, 30, 10)["verdict"] == "insufficient_sample"
    pairs = [(f"d{i}", 0.5 + 0.01 * i) for i in range(12) for _ in range(3)]
    s = pms.summarize(pairs, 30, 10)
    assert s["verdict"] == "edge" and s["days"] == 12 and s["n"] == 36
    pairs = [(f"d{i}", (-1.0) ** i * 1.0) for i in range(12) for _ in range(3)]
    assert pms.summarize(pairs, 30, 10)["verdict"] == "unclear"
    assert pms.summarize([], 30, 10)["n"] == 0


def test_funnel_counts_only_the_opening_window_in_ist():
    # 03:50 UTC = 09:20 IST (in the window); 04:40 UTC = 10:10 IST (out); 03:30 UTC = 09:00 IST (out)
    rows = [{"created_at": "2026-10-27T03:50:00Z", "decision": "SKIPPED", "reason": "OPENING_GATE:EXTENDED_ABOVE_OR"},
            {"created_at": "2026-10-27T03:51:00Z", "decision": "SKIPPED", "reason": "MIN_QTY:qty=2<3:ltp=1500.00"},
            {"created_at": "2026-10-27T03:52:00Z", "decision": "SKIPPED", "reason": "MIN_QTY:qty=1<3:ltp=2500.00"},
            {"created_at": "2026-10-27T04:00:00Z", "decision": "ENTERED", "reason": None},
            {"created_at": "2026-10-27T04:40:00Z", "decision": "ENTERED", "reason": None},
            {"created_at": "2026-10-27T03:30:00Z", "decision": "SKIPPED", "reason": "BEFORE"},
            {"created_at": "not a date", "decision": "SKIPPED", "reason": "X"}]
    f = pms.funnel(rows)
    assert f["rows_09_15_to_10_00"] == 4
    assert f["decisions"] == {"SKIPPED": 3, "ENTERED": 1}
    assert f["skipped_by_gate"] == {"MIN_QTY": 2, "OPENING_GATE": 1}
    assert f["entered_by_half_hour"] == {"09:30": 1, "10:00": 1}


def test_cli_writes_outputs_and_reports_nothing_to_do():
    with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as out:
        for s in ("AAA", "BBB", "CCC"):
            _chain(root, s, 12, 2.0, lambda i: 0.002)
        cand = Path(out, "cand.json")
        cand.write_text(json.dumps([{"created_at": "2026-10-27T03:50:00Z", "decision": "SKIPPED", "reason": "RANGE_GATE:X"}]))
        assert pms.main([root, "--out", out, "--candidate-log", str(cand)]) == 0
        md = Path(out, "preopen_momentum_study.md").read_text()
        assert "A_follow" in md and "Opening funnel" in md and "RANGE_GATE" in md
        doc = json.loads(Path(out, "preopen_momentum_study.json").read_text())
        assert doc["study"]["days"] >= 11 and doc["funnel"]["rows_09_15_to_10_00"] == 1
    with tempfile.TemporaryDirectory() as empty:
        assert pms.main([str(Path(empty, "nope"))]) == 2


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
    print("ok")
