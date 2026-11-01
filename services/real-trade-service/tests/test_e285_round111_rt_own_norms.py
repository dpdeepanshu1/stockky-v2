"""E285 round 111: Real Trade reads the stock's own 1-minute range and volume pace (entry_engine/own_norms, from the minute bars Position Stocks records) and uses them in its VWAP and liquidity gates.
Plain-function runner: python3 tests/test_e285_round111_rt_own_norms.py (also collected by pytest)."""
from __future__ import annotations

import calendar
import json
import os
from urllib.parse import quote
import shutil
import sys
import tempfile
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from entry_engine import liquidity_gate, own_norms, soft_gates, vwap_gate
from tz_utils import IST

DAYS = ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26", "2026-10-27", "2026-10-28"]


def _t0(date: str) -> float:
    y, m, d = (int(x) for x in date.split("-"))
    return calendar.timegm((y, m, d, 0, 0, 0)) - 19800 + 9 * 3600 + 15 * 60


def _bars(date: str, range_pct: float, vol: float, n: int = 375, price: float = 100.0):
    out = []
    for k in range(n):
        half = price * range_pct / 100.0 / 2.0
        out.append([_t0(date) + 60 * k, price, price + half, price - half, price, vol])
    return out


def _write(root: str, date: str, symbol: str, bars) -> None:
    p = os.path.join(root, date, quote(symbol, safe="") + ".json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        json.dump({"v": 1, "symbol": symbol, "date": date, "tf": 1, "bars": bars}, f)


def _root(symbol: str, range_pct: float, vol: float, days=DAYS) -> str:
    root = tempfile.mkdtemp(prefix="own_norms_")
    for d in days:
        _write(root, d, symbol, _bars(d, range_pct, vol))
    return root


def _on(root: str):
    config.ENTRY_OWN_NORMS_ENABLED = True
    config.ADAPTIVE_ENTRY_PARAMS = True
    config.ENTRY_OWN_NORMS_BARS_DIR = root
    own_norms.reset()


def _off():
    config.ENTRY_OWN_NORMS_ENABLED = False
    config.ENTRY_OWN_NORMS_BARS_DIR = ""
    own_norms.reset()


def _wait(symbol: str, secs: float = 3.0):
    end = time.time() + secs
    while time.time() < end:
        nm = own_norms._norms(symbol)
        if nm is not None:
            return nm
        time.sleep(0.02)
    return None


def test_day_norms_complete_day():
    dn = own_norms.day_norms(_bars(DAYS[0], 0.30, 1000.0), DAYS[0])
    assert dn is not None and abs(dn["range"] - 0.30) < 1e-6, dn
    assert len(dn["cumx"]) == 376 and dn["cumx"][-1] == 375000.0


def test_day_norms_rejects_partial_day_and_no_volume():
    assert own_norms.day_norms(_bars(DAYS[0], 0.3, 1000.0, n=200), DAYS[0]) is None       # recorder started late / stopped early
    assert own_norms.day_norms(_bars(DAYS[0], 0.3, 0.0), DAYS[0]) is None                  # a backfill without minute volumes
    assert own_norms.day_norms([], DAYS[0]) is None


def test_build_reads_each_complete_day():
    root = _root("AAA", 0.30, 1000.0)
    try:
        nm = own_norms.build("AAA", today="2026-11-02", root=root)
        assert nm and nm["days"] == 8 and abs(nm["range_pct"] - 0.30) < 1e-6, nm
        assert own_norms.build("MISSING", today="2026-11-02", root=root) is None
        assert own_norms.build("AAA", today="2026-10-20", root=root)["days"] == 1            # only days BEFORE today are read
    finally:
        shutil.rmtree(root)


def test_vwap_tolerance_constant_with_too_few_days():
    root = _root("AAA", 0.30, 1000.0, days=DAYS[:4])
    try:
        _on(root)
        nm = _wait("AAA")
        assert nm and nm["days"] == 4
        out = own_norms.vwap_tolerance_pct("AAA", 0.15)
        assert out["source"] == "constant" and out["value"] == 0.15, out
        assert vwap_gate.own_tolerance_pct("AAA") is None                                   # the gate then uses ENTRY_VWAP_TOLERANCE_PCT
    finally:
        _off(); shutil.rmtree(root)


def test_vwap_tolerance_lively_stock_widens_within_band():
    root = _root("LIVELY", 0.30, 1000.0)
    try:
        _on(root)
        _wait("LIVELY")
        out = own_norms.vwap_tolerance_pct("LIVELY", 0.15)
        w = 8 / 18.0
        assert out["source"] == "learned" and abs(out["value"] - (w * 0.30 + (1 - w) * 0.15)) < 1e-6, out
        assert 0.15 < out["value"] <= 0.225
        assert abs(vwap_gate.own_tolerance_pct("LIVELY") - out["value"]) < 1e-9
    finally:
        _off(); shutil.rmtree(root)


def test_vwap_tolerance_hard_band():
    root = _root("WILD", 2.0, 1000.0)
    many = ["2026-09-%02d" % d for d in range(1, 21)]                                       # 20 days: the learned weight is 20 / 30, so a very quiet stock reaches the floor
    root2 = _root("SLEEPY", 0.01, 1000.0, days=many)
    try:
        _on(root)
        _wait("WILD")
        out = own_norms.vwap_tolerance_pct("WILD", 0.15)
        assert out["source"] == "ceiling" and abs(out["value"] - 0.225) < 1e-9, out
        _off()
        _on(root2)
        _wait("SLEEPY")
        out = own_norms.vwap_tolerance_pct("SLEEPY", 0.15)
        assert out["source"] == "floor" and abs(out["value"] - 0.075) < 1e-9, out
    finally:
        _off(); shutil.rmtree(root); shutil.rmtree(root2)


def test_expected_volume_own_pace():
    root = _root("VOLY", 0.2, 1000.0)
    try:
        _on(root)
        _wait("VOLY")
        # at 30 minutes after 09:15 the stock normally has 30,000 traded; the old curve says 33,000 -> the learned base sits between, inside +-50 % of the old one
        out = own_norms.expected_volume("VOLY", 33000.0, 30.0)
        w = 8 / 18.0
        assert out["source"] == "learned" and abs(out["value"] - (w * 30000.0 + (1 - w) * 33000.0)) < 1e-6, out
        mid = own_norms.expected_volume("VOLY", 33000.0, 30.5)                              # linear inside a minute
        assert mid and mid["value"] > out["value"] - 1e-9
        assert own_norms.own_cum_volume({"cumx": [[0.0, 10.0, 30.0]]}, 0.5) == 5.0
        assert own_norms.own_cum_volume({"cumx": [[0.0, 10.0, 30.0]]}, 0.0) is None
    finally:
        _off(); shutil.rmtree(root)


def test_expected_volume_band_binds_with_many_days():
    many = ["2026-09-%02d" % d for d in range(1, 21)]                                       # 20 days: weight 20 / 30, so the move can pass the 50 % band
    root = _root("VOLY", 0.2, 1000.0, days=many)
    try:
        _on(root)
        _wait("VOLY")
        far = own_norms.expected_volume("VOLY", 1_000_000.0, 30.0)                          # an old curve far above its own pace: the band stops the move at -50 %
        assert far["source"] == "floor" and abs(far["value"] - 500_000.0) < 1e-6, far
        assert far["days"] == 20
    finally:
        _off(); shutil.rmtree(root)


def test_switch_off_and_unknown_symbols_give_no_opinion():
    root = _root("AAA", 0.30, 1000.0)
    try:
        _on(root)
        config.ENTRY_OWN_NORMS_ENABLED = False
        assert own_norms.vwap_tolerance_pct("AAA", 0.15) is None and own_norms.expected_volume("AAA", 1000.0, 30.0) is None
        config.ENTRY_OWN_NORMS_ENABLED = True
        assert own_norms.vwap_tolerance_pct("^NSEI", 0.15) is None                          # an index is never loaded
        assert own_norms.vwap_tolerance_pct("AAA", 0.0) is None                             # no constant, nothing to shrink toward
        own_norms.reset()
        assert own_norms.vwap_tolerance_pct("AAA", 0.15) is None                            # first ask answers "unknown" and queues the load, never waits
    finally:
        _off(); shutil.rmtree(root)


def test_missing_history_is_cached_as_unknown():
    root = tempfile.mkdtemp(prefix="own_norms_empty_")
    try:
        _on(root)
        own_norms._norms("NOPE")
        end = time.time() + 3
        while time.time() < end and own_norms._running:
            time.sleep(0.02)
        assert "NOPE" in own_norms._cache and own_norms._cache["NOPE"][0] is None
        assert own_norms.vwap_tolerance_pct("NOPE", 0.15) is None
    finally:
        _off(); shutil.rmtree(root)


def test_vwap_reject_reason_uses_the_given_tolerance():
    saved = (config.ENTRY_VWAP_GATE_ENABLED, config.ENTRY_VWAP_TOLERANCE_PCT)
    config.ENTRY_VWAP_GATE_ENABLED, config.ENTRY_VWAP_TOLERANCE_PCT = True, 0.15
    try:
        price, vwap = 99.80, 100.0                                                          # 0.20 % under the VWAP
        assert vwap_gate.reject_reason(price, vwap) is not None                             # fixed 0.15 % -> refused
        assert vwap_gate.reject_reason(price, vwap, 0.225) is None                          # a lively stock's own 0.225 % -> fine
        assert vwap_gate.reject_reason(99.90, vwap, 0.075) is not None                      # a quiet stock's 0.075 % refuses a 0.10 % drop
        assert vwap_gate.reject_reason(price, vwap, None) == vwap_gate.reject_reason(price, vwap)     # no tolerance argument = the old behaviour
    finally:
        config.ENTRY_VWAP_GATE_ENABLED, config.ENTRY_VWAP_TOLERANCE_PCT = saved


def _at(h, m):
    return datetime(2026, 10, 13, h, m, tzinfo=IST)


def test_liquidity_decide_uses_own_expected_volume():
    saved = {k: getattr(config, k, None) for k in ("ENTRY_MIN_RVOL", "ENTRY_MIN_AVG_TURNOVER_CR", "ENTRY_MIN_DAY_TURNOVER_CR", "ENTRY_RVOL_MIN_TIME_IST")}
    config.ENTRY_MIN_RVOL, config.ENTRY_MIN_AVG_TURNOVER_CR, config.ENTRY_MIN_DAY_TURNOVER_CR = 0.35, 0.0, 0.0
    config.ENTRY_RVOL_MIN_TIME_IST = "09:45"
    try:
        now = _at(11, 0)
        frac = liquidity_gate.elapsed_fraction(now)
        av = 1_000_000.0
        old_base = av * frac
        dv = 0.3 * old_base                                                                 # 0.30 x the old pace: under the 0.35 floor
        assert str(liquidity_gate.decide(100.0, dv, av, 1e9, now)).startswith("LIQUIDITY_RVOL")
        assert liquidity_gate.decide(100.0, dv, av, 1e9, now, own_expected=0.5 * old_base) is None      # this stock normally trades less by now: 0.6x of its own pace
        assert str(liquidity_gate.decide(100.0, dv, av, 1e9, now, own_expected=2.0 * old_base)).startswith("LIQUIDITY_RVOL")
        assert str(liquidity_gate.decide(100.0, dv, av, 1e9, now, own_expected=-5.0)).startswith("LIQUIDITY_RVOL")     # a nonsense own value falls back to the old base
    finally:
        for k, v in saved.items():
            if v is not None:
                setattr(config, k, v)


def test_liquidity_own_expected_volume_reads_the_norms():
    root = _root("VOLY", 0.2, 1000.0)
    try:
        _on(root)
        _wait("VOLY")
        now = _at(11, 0)                                                                    # 105 minutes after 09:15
        frac = liquidity_gate.elapsed_fraction(now)
        av = 400_000.0
        got = liquidity_gate.own_expected_volume("VOLY", av, now)
        assert got is not None
        own = 105 * 1000.0
        w = 8 / 18.0
        assert abs(got - (w * own + (1 - w) * av * frac)) < 1e-3, (got, own, av * frac)
        assert liquidity_gate.own_expected_volume("VOLY", None, now) is None
        assert liquidity_gate.own_expected_volume("UNSEEN", av, now) is None
        config.ENTRY_OWN_NORMS_ENABLED = False
        assert liquidity_gate.own_expected_volume("VOLY", av, now) is None
    finally:
        _off(); shutil.rmtree(root)


def test_warm_queues_symbols_and_never_raises():
    root = _root("AAA", 0.30, 1000.0)
    try:
        _on(root)
        assert own_norms.warm(["AAA", "BBB"]) == 2
        assert own_norms.warm(None) == 0
        assert _wait("AAA") is not None
    finally:
        _off(); shutil.rmtree(root)


def test_master_switch_turns_the_own_norms_off():
    root = _root("LIVELY", 0.30, 1000.0)
    try:
        _on(root)
        _wait("LIVELY")
        assert vwap_gate.own_tolerance_pct("LIVELY") is not None
        config.ADAPTIVE_ENTRY_PARAMS = False                                                # E285 round 111: the master switch wins over ENTRY_OWN_NORMS_ENABLED
        assert vwap_gate.own_tolerance_pct("LIVELY") is None
        assert own_norms.expected_volume("LIVELY", 33000.0, 30.0) is None
        assert liquidity_gate.own_expected_volume("LIVELY", 400_000.0, _at(11, 0)) is None
        config.ADAPTIVE_ENTRY_PARAMS = True
        assert vwap_gate.own_tolerance_pct("LIVELY") is not None
    finally:
        config.ADAPTIVE_ENTRY_PARAMS = True
        _off(); shutil.rmtree(root)


def test_bars_root_defaults_to_the_parent_of_the_bar_records_dir():
    saved = (getattr(config, "ENTRY_OWN_NORMS_BARS_DIR", ""), config.BAR_RECORDS_DIR)
    try:
        config.ENTRY_OWN_NORMS_BARS_DIR = ""
        config.BAR_RECORDS_DIR = "/data/bars/rt"
        assert own_norms.bars_root() == "/data/bars"
        config.BAR_RECORDS_DIR = "/x/y/rt/"
        assert own_norms.bars_root() == "/x/y"
        config.ENTRY_OWN_NORMS_BARS_DIR = "/custom"
        assert own_norms.bars_root() == "/custom"
    finally:
        config.ENTRY_OWN_NORMS_BARS_DIR, config.BAR_RECORDS_DIR = saved


def test_the_rt_subfolder_and_other_non_date_folders_are_not_read_as_days():
    root = _root("AAA", 0.30, 1000.0)
    try:
        os.makedirs(os.path.join(root, "rt", "wanted"), exist_ok=True)
        os.makedirs(os.path.join(root, "not-a-date"), exist_ok=True)
        nm = own_norms.build("AAA", today="2026-11-02", root=root)
        assert nm and nm["days"] == 8, nm
        assert own_norms.build("AAA", today="2026-11-02", root=os.path.join(root, "missing")) is None      # no recorder volume at all: unknown, no raise
    finally:
        shutil.rmtree(root)


class _Tick:
    def __init__(self, symbol, price, vwap):
        self.symbol, self.price, self.vwap = symbol, price, vwap


def test_tick_gate_and_soft_gate_use_the_stocks_own_tolerance():
    root = _root("LIVELY.NS".replace(".NS", ""), 0.30, 1000.0)
    saved = (config.ENTRY_VWAP_GATE_ENABLED, config.ENTRY_VWAP_TOLERANCE_PCT, getattr(config, "SOFT_GATES_MODE", "penalty"))
    try:
        config.ENTRY_VWAP_GATE_ENABLED, config.ENTRY_VWAP_TOLERANCE_PCT = True, 0.15
        _on(root)
        _wait("LIVELY")
        tol = vwap_gate.own_tolerance_pct("LIVELY")
        assert 0.15 < tol <= 0.225
        t = _Tick("LIVELY.NS", 99.80, 100.0)                                                # 0.20 % under the VWAP
        assert vwap_gate.tolerance_for_tick(t) == tol
        under_tol_wider = tol > 0.20
        got = vwap_gate.reject_reason_tick(t)
        assert (got is None) == under_tol_wider, (got, tol)                                 # refused by the fixed 0.15 %, passed once the own tolerance is above 0.20 %
        other = _Tick("UNSEEN.NS", 99.80, 100.0)                                            # no history: the fixed 0.15 %
        assert vwap_gate.tolerance_for_tick(other) == 0.15
        assert vwap_gate.reject_reason_tick(other) is not None
        why, mult = soft_gates.vwap(other)
        assert why is None and 0.0 < mult < 1.0, (why, mult)                                # the old soft penalty is unchanged for a stock with no own norms
    finally:
        config.ENTRY_VWAP_GATE_ENABLED, config.ENTRY_VWAP_TOLERANCE_PCT = saved[0], saved[1]
        _off(); shutil.rmtree(root)


def _run_all() -> int:
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as e:  # noqa: BLE001
                fails += 1
                import traceback
                print("FAIL", name, repr(e))
                traceback.print_exc()
    print("failed:", fails)
    return fails


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
