"""E107 (P2 item 16, the two decisions): pre-open gap judged against the stock's own ATR, and VIX caution at its own 12-month percentile. Both can only ADD
caution (the lower of the fixed and the new threshold applies), both fail open on missing data, both are live protected switches. Stdlib only."""
import asyncio
import pathlib
import re
import sys
from types import SimpleNamespace as NS

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import config  # noqa: E402
from screening import preopen, prev_day, vix_gate  # noqa: E402

CAT = (ROOT / "control" / "catalog.py").read_text(encoding="utf-8")
AUTO = (ROOT.parent / "calibration-runner-service" / "autocalib.py").read_text(encoding="utf-8")


def _gap_env(mp, gap, atr, mult=1.5, up=6.0, dn=3.0, floor=1.5):
    mp.setattr(config, "PREOPEN_SNAPSHOT_ENABLED", True, raising=False)
    mp.setattr(config, "PREOPEN_GAP_UP_BLOCK_PCT", up, raising=False)
    mp.setattr(config, "PREOPEN_GAP_DOWN_BLOCK_PCT", dn, raising=False)
    mp.setattr(config, "PREOPEN_GAP_ATR_MULT", mult, raising=False)
    mp.setattr(config, "PREOPEN_GAP_ATR_FLOOR_PCT", floor, raising=False)
    mp.setattr(config, "PREOPEN_GAP_GATE_UNTIL_IST", "23:59", raising=False)
    mp.setattr(preopen, "ist_today_str", lambda now=None: "2026-10-26")
    mp.setattr(preopen, "ist_time_at_or_after", lambda t, now=None: False)
    mp.setitem(preopen._snap, "day", "2026-10-26")
    mp.setitem(preopen._snap, "gaps", {"AAA": gap})
    mp.setattr(prev_day, "get", lambda sym: (NS(atr_pct=atr) if atr is not None else None))


def test_calm_stock_gap_up_is_blocked_by_its_own_atr_below_the_fixed_percent(monkeypatch):
    _gap_env(monkeypatch, gap=2.5, atr=1.2)            # 1.5 x 1.2 = 1.8 -> floor 1.8 (above 1.5): 2.5% >= 1.8%
    r = preopen.gap_reason("AAA")
    assert r and r.startswith("PREOPEN_GAP_UP") and "daily ATR" in r


def test_wild_stock_same_gap_is_not_blocked_and_the_fixed_percent_still_backstops(monkeypatch):
    _gap_env(monkeypatch, gap=2.5, atr=3.0)            # 1.5 x 3 = 4.5 -> 2.5% passes; fixed 6% not reached
    assert preopen.gap_reason("AAA") is None
    _gap_env(monkeypatch, gap=6.5, atr=3.0)            # fixed 6% still blocks (ATR 4.5 < 6 -> ATR text, still a block)
    assert preopen.gap_reason("AAA", None).startswith("PREOPEN_GAP_UP")
    _gap_env(monkeypatch, gap=6.5, atr=9.0)            # ATR threshold 13.5 > fixed 6 -> the fixed % blocks
    assert "ATR" not in preopen.gap_reason("AAA")


def test_gap_down_uses_the_same_atr_test(monkeypatch):
    _gap_env(monkeypatch, gap=-2.0, atr=1.0)           # 1.5 x 1.0 -> floor 1.5: -2.0 <= -1.5
    assert preopen.gap_reason("AAA").startswith("PREOPEN_GAP_DOWN")


def test_missing_atr_or_switch_zero_is_the_old_behaviour(monkeypatch):
    _gap_env(monkeypatch, gap=2.5, atr=None)
    assert preopen.gap_reason("AAA") is None
    _gap_env(monkeypatch, gap=2.5, atr=1.2, mult=0.0)
    assert preopen.gap_reason("AAA") is None
    _gap_env(monkeypatch, gap=2.5, atr=1.2, mult=0.0, up=2.0)
    assert preopen.gap_reason("AAA").startswith("PREOPEN_GAP_UP")


def test_atr_test_only_blocks_more_never_less(monkeypatch):
    for gap in (0.5, 1.0, 1.4, 2.0, 3.5, 5.0, 7.0, -1.0, -2.5, -4.0):
        for atr in (None, 0.8, 1.5, 3.0, 6.0):
            _gap_env(monkeypatch, gap, atr, mult=0.0)
            old = preopen.gap_reason("AAA")
            _gap_env(monkeypatch, gap, atr, mult=1.5)
            new = preopen.gap_reason("AAA")
            assert old is None or new is not None, (gap, atr)


def _closes(n=250):
    return [12.0 + (i % 100) * 0.1 for i in range(n)]          # 12.0 .. 21.9, 75th percentile = about 19.4


def test_percentile_is_linear_and_needs_60_points():
    assert vix_gate.percentile([10.0 + i for i in range(101)], 75) == 85.0
    assert vix_gate.percentile([15.0] * 59, 75) is None
    assert vix_gate.percentile([], 75) is None


def _vix_env(mp, level, pctile, fixed=18.0, closes=None):
    vix_gate.reset()
    mp.setattr(config, "VIX_GATE_ENABLED", True, raising=False)
    mp.setattr(config, "VIX_CAUTION_ABOVE", fixed, raising=False)
    mp.setattr(config, "VIX_CAUTION_PCTILE", pctile, raising=False)
    mp.setattr(config, "VIX_CAUTION_SIZE_MULT", 0.7, raising=False)
    mp.setattr(config, "VIX_SPIKE_PCT", 0.0, raising=False)
    if closes is not None:
        vix_gate._hist.update(day="2026-10-26", closes=closes)
    return {"level": level, "change_pct": 0.0}


def test_percentile_adds_caution_below_the_fixed_level_when_the_year_was_calm(monkeypatch):
    calm = [11.0 + (i % 50) * 0.05 for i in range(250)]       # a quiet year: 75th percentile about 12.8
    snap = _vix_env(monkeypatch, 14.0, 75.0, closes=calm)
    assert vix_gate.size_mult(snap) == 0.7                    # 14 is under the fixed 18 but top quarter of its own year
    snap = _vix_env(monkeypatch, 14.0, 0.0, closes=calm)
    assert vix_gate.size_mult(snap) == 1.0                    # switch 0 = fixed level only


def test_fixed_level_still_applies_when_the_percentile_is_higher_or_history_is_missing(monkeypatch):
    wild = [25.0 + (i % 50) * 0.1 for i in range(250)]        # a wild year: percentile about 28 > 18
    snap = _vix_env(monkeypatch, 19.0, 75.0, closes=wild)
    assert vix_gate.size_mult(snap) == 0.7                    # lower of (18, ~28) = 18 applies
    snap = _vix_env(monkeypatch, 17.0, 75.0, closes=wild)
    assert vix_gate.size_mult(snap) == 1.0
    snap = _vix_env(monkeypatch, 17.0, 75.0, closes=None)
    assert vix_gate.size_mult(snap) == 1.0 and vix_gate.pctile_threshold() is None


def test_history_fetch_failure_is_swallowed_and_retried_later(monkeypatch):
    _vix_env(monkeypatch, 15.0, 75.0)

    async def boom():
        raise RuntimeError("down")
    monkeypatch.setattr(vix_gate, "_fetch_history", boom)
    assert asyncio.run(vix_gate.refresh_history()) is None and vix_gate._hist["closes"] is None
    async def ok():
        return _closes()
    monkeypatch.setattr(vix_gate, "_fetch_history", ok)
    vix_gate._hist["ts"] = -1e9                                # the retry window has passed
    assert asyncio.run(vix_gate.refresh_history()) and vix_gate.pctile_threshold() is not None


def test_switches_live_protected_and_shipped_equal_default():
    for k, shipped in (("PREOPEN_GAP_ATR_MULT", "1.5"), ("VIX_CAUTION_PCTILE", "75.0")):
        i = CAT.index(f'Flag("{k}"')
        f = CAT[i:CAT.index("\n    Flag(", i + 1)]
        assert "live=True" in f and f'shipped="{shipped}"' in f and "zero_off=True" in f and 'looser="higher"' in f, k
        assert re.search(r'"%s":\s*"' % k, AUTO), k
    # tests/conftest.py zeroes both on the shared module for every test, so read the SHIPPED values from a fresh copy of config.py
    import importlib.util
    spec = importlib.util.spec_from_file_location("_config_fresh_e107", ROOT / "config.py")
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    assert fresh.PREOPEN_GAP_ATR_MULT == 1.5 and fresh.VIX_CAUTION_PCTILE == 75.0
