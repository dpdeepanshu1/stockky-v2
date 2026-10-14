#!/usr/bin/env python3
"""gen_ms_golden.py - freeze the LEGACY Market-Board zone maths (services/position-stocks-service/screening/market_structure.py) as a golden file.

  python3 scripts/gen_ms_golden.py            # check: regenerate in memory and compare with tests/golden/market_structure_v0.json (exit 1 on drift)
  python3 scripts/gen_ms_golden.py --update   # rewrite the golden file (do this ONLY when the legacy maths is changed on purpose)

41 seeded synthetic series (trends, ranges, plateaus, gaps, short, flat) -> swings, find_zones (default + full_range), zone_quality, pick_zone,
chase_atr, levels, risk_reward, zone_score, zone_risk, atr. The legacy module is the fallback when BOARD_USE_MS_ENGINE=0 or the engine errors,
so its behaviour must stay byte-identical to what the board served before the engine existed (WS-0 / WS-2 of the structure plan).
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PS = ROOT / "services" / "position-stocks-service"
GOLDEN = PS / "tests" / "golden" / "market_structure_v0.json"
sys.path.insert(0, str(PS))

from screening import market_structure as ms  # noqa: E402

T0 = 1_790_000_000


def _walk(seed: int, n: int, drift: float = 0.0, vol: float = 0.6, jump: float = 0.06, floor: float = 5.0):
    r = random.Random(seed)
    px, out = 100.0, []
    for i in range(n):
        o = px
        c = max(floor, o + drift + r.gauss(0, vol) + ((r.choice((-1, 1)) * r.uniform(1.5, 3.0)) if r.random() < jump else 0.0))
        h = max(o, c) + abs(r.gauss(0, vol / 3))
        l = min(o, c) - abs(r.gauss(0, vol / 3))
        out.append((T0 + i * 300, round(o, 4), round(h, 4), round(l, 4), round(c, 4), round(1000.0 + r.random() * 500, 2)))
        px = c
    return out


def series():
    """name -> bars (t, o, h, l, c, v)"""
    out = {}
    for s in range(12):
        out[f"walk{s:02d}"] = _walk(s, 300)
    for s in range(6):
        out[f"up{s}"] = _walk(100 + s, 260, drift=0.08)
        out[f"down{s}"] = _walk(200 + s, 260, drift=-0.08)
    for s in range(4):
        out[f"range{s}"] = _walk(300 + s, 240, vol=0.25, jump=0.0)
    for s in range(3):                                          # plateaus: repeated equal highs
        b = _walk(400 + s, 200, vol=0.2, jump=0.0)
        hi = max(x[2] for x in b[40:60])
        out[f"plateau{s}"] = [(t, o, (hi if 40 <= i < 60 and i % 3 == 0 else h), l, c, v) for i, (t, o, h, l, c, v) in enumerate(b)]
    for s in range(3):                                          # gaps
        b = _walk(500 + s, 200)
        out[f"gap{s}"] = [(t, o * 1.08 if i == 100 else o, h * 1.08 if i >= 100 else h, l * 1.08 if i >= 100 else l, c * 1.08 if i >= 100 else c, v)
                          for i, (t, o, h, l, c, v) in enumerate(b)]
    for n in (0, 1, 4, 5, 9):
        out[f"short{n}"] = _walk(600 + n, n)
    out["flat"] = [(T0 + i * 300, 100.0, 100.0, 100.0, 100.0, 1000.0) for i in range(120)]
    out["zero_volume"] = [(t, o, h, l, c, 0.0) for (t, o, h, l, c, v) in _walk(700, 150)]
    return out


def _r(v):
    """JSON-stable: floats rounded, tuples -> lists, dict keys sorted by json.dumps."""
    if isinstance(v, float):
        return round(v, 6)
    if isinstance(v, (list, tuple)):
        return [_r(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _r(x) for k, x in v.items()}
    return v


def snapshot():
    snap = {}
    for name, bars in series().items():
        ltp = bars[-1][4] if bars else 100.0
        sw = ms.swings(bars)
        z_def = ms.find_zones(bars)
        z_full = ms.find_zones(bars, full_range=True)
        ent = {"atr": ms.atr(bars), "swings": sw, "zones": z_def, "zones_full": z_full}
        for tag, zs in (("default", z_def), ("full", z_full)):
            per = {}
            for side in ("bull", "bear"):
                z = ms.pick_zone(zs, side, ltp)
                lv = ms.levels(ltp, sw, bars)
                rr = ms.risk_reward(side, ltp, z, lv)
                ch = ms.chase_atr(side, ltp, z)
                per[side] = {"picked": z, "quality": ms.zone_quality(z), "chase": ch, "levels": lv, "rr": rr,
                             "score": ms.zone_score(z, rr, ch), "risk": ms.zone_risk(side, ltp, z)}
            ent[f"pick_{tag}"] = per
        snap[name] = _r(ent)
    return snap


def main() -> int:
    cur = json.dumps({"schema": 1, "n_series": len(series()), "series": snapshot()}, sort_keys=True, indent=0)
    if "--update" in sys.argv:
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(cur + "\n", encoding="utf-8")
        print(f"wrote {GOLDEN} ({len(cur)} bytes, {len(series())} series)")
        return 0
    if not GOLDEN.exists():
        print("golden file missing - run with --update")
        return 1
    if GOLDEN.read_text(encoding="utf-8").strip() != cur.strip():
        print("DRIFT: the legacy board maths no longer matches tests/golden/market_structure_v0.json")
        return 1
    print(f"ok: legacy board maths matches the golden ({len(series())} series)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
