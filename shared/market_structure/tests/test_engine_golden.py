"""group349 WS-0: golden baseline of analyze() on fixed synthetic series.

Any change to a threshold or detector changes the output and fails here until the golden file is regenerated on purpose:
    python3 tests/test_engine_golden.py --update      (from shared/market_structure, with PYTHONPATH=..)
Review the diff of golden/analyze_golden.json in the PR; the params hash is stored with every case.
"""
from __future__ import annotations

import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))  # .../shared

try:
    from market_structure.engine import analyze          # shared/ layout
except Exception:  # noqa: BLE001
    from stockky_ms.engine import analyze                # vendored layout

GOLDEN = os.path.join(HERE, "golden", "analyze_golden.json")


def _series(kind: str, seed: int, n: int = 220):
    random.seed(seed)
    px, out = 100.0, []
    for i in range(n):
        if kind == "up":
            drift, vol = 0.35, 0.9
        elif kind == "down":
            drift, vol = -0.35, 0.9
        elif kind == "range":
            drift, vol = (100.0 - px) * 0.08, 1.0
        elif kind == "choppy":
            drift, vol = 0.0, 2.2
        else:  # "spiky"
            drift, vol = 0.1, 0.7
        o = px
        c = max(5.0, o + drift + random.gauss(0, vol) + (random.choice((-1, 1)) * random.uniform(3, 6) if kind == "spiky" and random.random() < 0.05 else 0.0))
        h = max(o, c) + abs(random.gauss(0, 0.4))
        l = min(o, c) - abs(random.gauss(0, 0.4))
        out.append((1_700_000_000 + i * 86400, round(o, 4), round(h, 4), round(l, 4), round(c, 4), 1000.0 + random.random() * 800))
        px = c
    return out


def _cases():
    cases = {}
    for kind in ("up", "down", "range", "choppy", "spiky"):
        for seed in (11, 12):
            cases[f"{kind}_{seed}"] = _series(kind, seed)
    return cases


def _snapshot():
    snap = {}
    for name, bars in _cases().items():
        blk = analyze(bars, ctx={}, stale=False)
        snap[name] = json.loads(json.dumps(blk, sort_keys=True, default=str))
    return snap


def test_golden_matches():
    assert os.path.exists(GOLDEN), "golden file missing: run with --update"
    want = json.load(open(GOLDEN, encoding="utf-8"))
    got = _snapshot()
    assert set(got) == set(want)
    bad = [k for k in want if got[k] != want[k]]
    assert not bad, f"analyze() output changed for {bad}; review and regenerate with --update if intended"


def test_golden_is_deterministic():
    assert _snapshot() == _snapshot()


def test_golden_cases_are_not_all_empty():
    snap = _snapshot()
    assert any(v.get("quality") not in (None, "unavailable", "insufficient") for v in snap.values())


if __name__ == "__main__":
    if "--update" in sys.argv:
        os.makedirs(os.path.dirname(GOLDEN), exist_ok=True)
        json.dump(_snapshot(), open(GOLDEN, "w", encoding="utf-8"), indent=1, sort_keys=True)
        print("wrote", GOLDEN)
    else:
        for n, f in list(globals().items()):
            if n.startswith("test_"):
                f(); print("ok", n)
