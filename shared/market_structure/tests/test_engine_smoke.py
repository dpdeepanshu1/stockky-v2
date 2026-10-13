"""Smoke test for the recovered market-structure modules (runs with pytest or plain `python`). Synthetic bars only."""
import os, random, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from market_structure.atr import atr_series
from market_structure.fvg import find_fvgs
from market_structure.liquidity import find_sweeps
from market_structure.order_blocks import displacement, find_order_blocks
from market_structure.params import MSParams, from_dict, params_hash
from market_structure.prep import prep
from market_structure.structure import scan_structure
from market_structure.summary import SUMMARY_KEYS, flatten_summary
from market_structure.swings import raw_swings


def _bars(seed=7, n=220):
    random.seed(seed)
    px, out = 100.0, []
    for i in range(n):
        o, c = px, max(5.0, px + random.gauss(0.25 if (i // 30) % 2 == 0 else -0.25, 1.6))
        h = max(o, c) + abs(random.gauss(0, 0.8))
        l = min(o, c) - abs(random.gauss(0, 0.8))
        out.append((1_700_000_000 + i * 86400, o, h, l, c, 1000 + random.random() * 500))
        px = c
    return out


def _run(raw_bars):
    p = MSParams()
    pr = prep(raw_bars, p)
    b = pr["bars"]
    a = atr_series(b, p.atr_period, p.atr_min_bars, p.corp_action_jump_pct)
    raw = raw_swings(b, a, p)
    st = scan_structure(b, a, raw, pr["gap_event"], pr["circuit"], p)
    disp = [displacement(x, a[i], p) for i, x in enumerate(b)]
    return p, pr, b, a, raw, st, disp


def test_pipeline_runs_and_is_deterministic():
    r1, r2 = _run(_bars()), _run(_bars())
    assert r1[5]["state"] in ("bullish", "bearish", "neutral", "transition")
    assert r1[5]["state"] == r2[5]["state"] and len(r1[5]["bos"]) == len(r2[5]["bos"])


def test_detectors_return_expected_shapes():
    p, pr, b, a, raw, st, disp = _run(_bars())
    assert isinstance(find_order_blocks(b, a, p, raw, pr["gap_event"], pr["circuit"]), list)
    assert set(find_sweeps(b, a, raw, p, st["bos"], disp)) >= {"pools", "sweeps"}
    assert isinstance(find_fvgs(b, a, p, pr["gap_event"], st["bos"], pr["circuit"]), list)


def test_no_lookahead_swings_confirmed_before_use():
    _, _, _, _, raw, _, _ = _run(_bars())
    assert all(s["confirmed_at"] > s["idx"] for s in raw)


def test_short_history_is_insufficient():
    assert prep(_bars(n=30), MSParams())["quality"] == "insufficient"


def test_bad_bars_are_dropped_not_raised():
    bars = _bars() + [(1.0, float("nan"), 1, 1, 1, 1), (2.0, -1, 1, 1, 1, 1)]
    assert prep(bars, MSParams())["dq"]["bars_dropped"] >= 2


def test_params_hash_and_overrides():
    assert params_hash(MSParams()) == params_hash(from_dict({}))
    assert params_hash(from_dict({"rr_min": 2.0})) != params_hash(MSParams())
    assert from_dict({"nope": 1, "rr_min": "x"}) == MSParams()


def test_summary_tolerates_missing_block():
    s = flatten_summary(None)
    assert set(s) == set(SUMMARY_KEYS) and s["ms_quality"] == "unavailable"


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
            print("ok", k)
