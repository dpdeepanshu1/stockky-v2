#!/usr/bin/env python3
"""ms_g1_check.py - gate G1 (correctness) of the structure plan, run on REAL daily history instead of synthetic bars.

  python3 scripts/ms_g1_check.py HIST_DIR [--out DIR] [--cuts 6] [--min-ca 10] [--min-symbols 20] [--budget-ms 15] [--sample 200 --seed 7] [--strict]

HIST_DIR: one JSON per symbol (market-data /history reply or a bare candle list; see scripts/fetch_history_for_backtest.py), the same folder the A/B report uses.

Three checks (plan section 9, G1):
  1. truncation invariance - for several cut points k per symbol, every swing / BOS / CHOCH that the engine finds on bars[:k] must be found
     again, identical in time, side and level, when it is run on the whole history and only events confirmed by bar k are looked at. A mismatch
     means the engine used a bar from the future. Events are matched by bar TIMESTAMP (not index) and only from the later of the two
     corporate-action segment starts on, because a split AFTER k legitimately moves the segment.
  2. corporate actions - symbols whose series has a >corp_action_jump_pct close-to-close jump (unadjusted split / bonus, or a data splice) are
     found; for each, the engine must report `ca_discontinuity`, and no structural swing it returns may lie before the jump. G1 asks for >= 10
     such real cases; with fewer the verdict is NOT_ENOUGH_DATA (download more symbols), never PASS.
  3. latency - wall time of analyze() on the last 250 bars (p50 / p95 over all symbols). <= budget PASS, <= 3x budget WARN, else FAIL. The
     "/analyze p95 within 15% of before" half of G1 needs the live service: compare `curl -w %{time_total}` for /analyze/SYM before and after
     (that part cannot be computed offline and is listed under `manual`).

Verdict: PASS | FAIL | NOT_ENOUGH_DATA. FAIL on any invariance violation, any broken corporate-action case, or a FAIL latency. Stdlib only,
deterministic apart from the timings. Exit 0 unless FAIL (or NOT_ENOUGH_DATA with --strict).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))

from market_structure.atr import atr_series  # noqa: E402
from market_structure.candles import candles_to_bars  # noqa: E402
from market_structure.engine import analyze  # noqa: E402
from market_structure.params import MSParams  # noqa: E402
from market_structure.prep import prep  # noqa: E402
from market_structure.structure import scan_structure  # noqa: E402
from market_structure.swings import raw_swings  # noqa: E402

PASS, FAIL, NED, WARN = "PASS", "FAIL", "NOT_ENOUGH_DATA", "WARN"
BENCH = ("^NSEI", "NIFTY", "NSEI", "NIFTY50")


def load(dir_: Path, sample: int = 0, seed: int = 7):
    files = [f for f in sorted(dir_.glob("*.json")) if f.stem.upper() not in BENCH]
    if sample and sample < len(files):
        files = sorted(random.Random(seed).sample(files, sample))
    for f in files:
        try:
            doc = json.loads(f.read_text())
            bars = candles_to_bars(doc.get("candles") if isinstance(doc, dict) else doc)
            if len(bars) >= 100:
                yield f.stem.upper(), bars
        except Exception as e:  # noqa: BLE001
            print(f"skip {f.name}: {e}", file=sys.stderr)


def events(bars: Sequence[Sequence[float]], p: MSParams) -> Dict[str, Any]:
    """Normalised, timestamp-keyed events of one run: {start_ts, last_ts, swings: {key: confirmed_ts}, bos: set, choch: set}."""
    pr = prep(bars, p)
    b = pr["bars"]
    out: Dict[str, Any] = {"start_ts": b[0][0] if b else None, "last_ts": b[-1][0] if b else None, "swings": {}, "bos": set(), "choch": set(),
                           "quality": pr["quality"]}
    if pr["quality"] == "insufficient" or not b:
        return out
    atr = atr_series(b, p.atr_period, p.atr_min_bars, p.corp_action_jump_pct)
    raw = raw_swings(b, atr, p, "v1", circuit=pr["circuit"])
    st = scan_structure(b, atr, raw, pr["gap_event"], pr["circuit"], p)
    for s in raw:
        if s["confirmed_at"] < len(b):
            out["swings"][(b[s["idx"]][0], s["kind"], round(float(s["price"]), 4))] = b[s["confirmed_at"]][0]
    for name in ("bos", "choch"):
        for e in st.get(name) or []:
            out[name].add((b[e["break_idx"]][0], e.get("direction") or e.get("dir"), round(float(e["level"]), 4)))
    return out


def check_invariance(bars: Sequence[Sequence[float]], p: MSParams, cuts: int = 6, events_fn=events) -> List[Dict[str, Any]]:
    """Violations (list of dicts) of truncation invariance on one symbol. Empty = invariant at every cut."""
    n = len(bars)
    lo, hi = p.min_bars + 5, n - 10
    if hi <= lo:
        return []
    ks = sorted({lo + (hi - lo) * i // max(1, cuts - 1) for i in range(max(1, cuts))})
    full = events_fn(bars, p)
    bad: List[Dict[str, Any]] = []
    for k in ks:
        pre = events_fn(bars[:k], p)
        if pre["start_ts"] is None or full["start_ts"] is None:
            continue
        if pre.get("quality") == "insufficient":
            continue                      # fewer than min_bars after the last corporate action: the engine makes no claim there, so there is nothing to compare
        floor, last = max(pre["start_ts"], full["start_ts"]), pre["last_ts"]
        want_sw = {key for key in pre["swings"] if key[0] >= floor}
        got_sw = {key for key, conf in full["swings"].items() if key[0] >= floor and conf <= last}
        diffs = {"swings": (want_sw, got_sw), "bos": ({e for e in pre["bos"] if e[0] >= floor}, {e for e in full["bos"] if floor <= e[0] <= last}),
                 "choch": ({e for e in pre["choch"] if e[0] >= floor}, {e for e in full["choch"] if floor <= e[0] <= last})}
        for kind, (a, b) in diffs.items():
            if a != b:
                bad.append({"cut": k, "kind": kind, "only_prefix": sorted(map(str, a - b))[:3], "only_full": sorted(map(str, b - a))[:3]})
    return bad


def check_corporate_action(bars: Sequence[Sequence[float]], p: MSParams) -> Optional[Dict[str, Any]]:
    """None when the series has no discontinuity; else {ok, jump_pct, start_ts, usable, problems[]} for the case."""
    pr = prep(bars, p)
    if not pr["dq"]["ca_discontinuity"]:
        return None
    b = pr["bars"]
    start_ts = b[0][0]
    clean = sorted({x[0]: x for x in bars if len(x) >= 5 and x[4] and x[4] > 0}.values(), key=lambda x: x[0])
    jump = next(((clean[i][4] / clean[i - 1][4] - 1.0) * 100.0 for i in range(len(clean) - 1, 0, -1) if clean[i][0] == start_ts), None)
    problems: List[str] = []
    if pr["quality"] != "insufficient":
        blk = analyze(bars, params=p)
        if not (blk.get("data_quality") or {}).get("ca_discontinuity"):
            problems.append("engine did not report ca_discontinuity")
        early = [s for s in (blk.get("swings") or []) if s.get("ts") is not None and s["ts"] < start_ts]
        if early:
            problems.append(f"{len(early)} swing(s) before the jump")
    return {"ok": not problems, "jump_pct": round(jump, 1) if jump is not None else None, "start_ts": start_ts, "usable": pr["quality"] != "insufficient",
            "problems": problems}


def percentile(vals: Sequence[float], q: float) -> Optional[float]:
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def check_latency(bars: Sequence[Sequence[float]], p: MSParams, reps: int = 3) -> List[float]:
    window = list(bars)[-250:]
    out = []
    for _ in range(max(1, reps)):
        t0 = time.perf_counter()
        analyze(window, params=p)
        out.append((time.perf_counter() - t0) * 1000.0)
    return out


def run(dir_: Path, cuts: int = 6, min_ca: int = 10, min_symbols: int = 20, budget_ms: float = 15.0, sample: int = 0, seed: int = 7) -> Dict[str, Any]:
    p = MSParams()
    n_sym, viol, ca_cases, lat = 0, [], [], []
    for sym, bars in load(dir_, sample, seed):
        n_sym += 1
        for v in check_invariance(bars, p, cuts):
            viol.append(dict(v, symbol=sym))
        ca = check_corporate_action(bars, p)
        if ca is not None:
            ca_cases.append(dict(ca, symbol=sym))
        lat.extend(check_latency(bars, p))
    p50, p95 = percentile(lat, 0.5), percentile(lat, 0.95)
    lat_v = None if p95 is None else (PASS if p95 <= budget_ms else WARN if p95 <= 3 * budget_ms else FAIL)
    ca_ok = [c for c in ca_cases if c["ok"]]
    ca_bad = [c for c in ca_cases if not c["ok"]]
    checks = {
        "truncation_invariance": {"verdict": (NED if n_sym < min_symbols else FAIL if viol else PASS), "symbols": n_sym, "violations": viol[:20],
                                  "violation_count": len(viol), "needs_symbols": min_symbols},
        "corporate_actions": {"verdict": (FAIL if ca_bad else PASS if len(ca_ok) >= min_ca else NED), "cases": len(ca_cases), "verified": len(ca_ok),
                              "broken": ca_bad[:20], "needs_cases": min_ca, "examples": ca_ok[:10]},
        "latency": {"verdict": lat_v or NED, "p50_ms": None if p50 is None else round(p50, 2), "p95_ms": None if p95 is None else round(p95, 2),
                    "budget_ms": budget_ms, "calls": len(lat)},
    }
    vs = [c["verdict"] for c in checks.values()]
    overall = FAIL if FAIL in vs else NED if NED in vs else PASS
    return {"overall": overall, "symbols": n_sym, "checks": checks,
            "manual": ["/analyze p95 latency on the live service within 15% of the pre-structure value (compare curl -w '%{time_total}' before/after)"]}


def to_md(r: Dict[str, Any]) -> str:
    c = r["checks"]
    lines = [f"# G1 correctness check - {r['overall']}", "", f"Symbols replayed: {r['symbols']}", "", "| Check | Verdict | Detail |", "|---|---|---|",
             f"| Truncation invariance | {c['truncation_invariance']['verdict']} | {c['truncation_invariance']['violation_count']} violation(s), needs >= {c['truncation_invariance']['needs_symbols']} symbols |",
             f"| Corporate actions | {c['corporate_actions']['verdict']} | {c['corporate_actions']['verified']} verified of {c['corporate_actions']['cases']} found, "
             f"{len(c['corporate_actions']['broken'])} broken, needs >= {c['corporate_actions']['needs_cases']} |",
             f"| Latency | {c['latency']['verdict']} | p50 {c['latency']['p50_ms']} ms, p95 {c['latency']['p95_ms']} ms, budget {c['latency']['budget_ms']} ms |", ""]
    for v in c["truncation_invariance"].get("violations", [])[:10]:
        lines.append(f"- violations: {json.dumps(v, default=str)}")
    for v in c["corporate_actions"].get("broken", [])[:10]:
        lines.append(f"- broken: {json.dumps(v, default=str)}")
    # group470: the old footer said "future data / split" for ANY fail, including a latency-only one (19-20 Oct: truncation PASS, 8 splits OK, p95 57 ms)
    correctness_fail = FAIL in (c["truncation_invariance"]["verdict"], c["corporate_actions"]["verdict"])
    lines += ["", "Still manual: " + "; ".join(r["manual"]), ""]
    if correctness_fail:
        lines.append("A FAIL on truncation invariance or corporate actions means the engine used future data or mishandled a split: keep STRUCTURE_POLICY_MODE=off until fixed.")
    if c["latency"]["verdict"] in (WARN, FAIL):
        lines.append(f"Latency {c['latency']['verdict']} is a SPEED finding, not a correctness one: p95 {c['latency']['p95_ms']} ms against a {c['latency']['budget_ms']} ms budget "
                     f"(PASS <= budget, WARN <= 3x, FAIL beyond). Judge it against the live /analyze latency check; the budget is a judgement call (--budget-ms / G1_BUDGET_MS).")
    lines.append("NOT_ENOUGH_DATA means the history set is too small to certify the gate - download more symbols, do not read it as PASS.")
    return "\n".join(lines) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("dir")
    ap.add_argument("--out", default=None)
    ap.add_argument("--cuts", type=int, default=6)
    ap.add_argument("--min-ca", type=int, default=10)
    ap.add_argument("--min-symbols", type=int, default=20)
    ap.add_argument("--budget-ms", type=float, default=float(os.environ.get("G1_BUDGET_MS", "15") or 15))
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--strict", action="store_true")
    a = ap.parse_args(argv)
    d = Path(a.dir)
    if not d.is_dir():
        print(f"not a folder: {d}", file=sys.stderr)
        return 2
    r = run(d, a.cuts, a.min_ca, a.min_symbols, a.budget_ms, a.sample, a.seed)
    out = Path(a.out) if a.out else d / "g1_out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "g1_report.json").write_text(json.dumps(r, indent=1, default=str))
    (out / "g1_report.md").write_text(to_md(r))
    print(to_md(r))
    return 1 if r["overall"] == FAIL or (a.strict and r["overall"] != PASS) else 0


if __name__ == "__main__":
    sys.exit(main())
