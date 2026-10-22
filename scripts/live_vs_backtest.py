#!/usr/bin/env python3
"""live_vs_backtest.py - B17: does each structure rule do live what it did in the backtest? Offline readout, changes nothing.

  python3 scripts/live_vs_backtest.py --live live.json --ab ab_out/ab_report.json [--out DIR] [--min-n 30] [--boot 500]

  --live  JSON saved from real-trade `GET /structure/observations/REAL?days=90&recent=100000` (the report with its `recent` rows; a bare list of rows or
          {"observations": [...]} also works). `recent` defaults to 40 on the server, so ASK FOR A BIG NUMBER or only 40 rows are read.
  --ab    ab_report.json from scripts/ms_ab_report.py (the backtest it is compared with; its horizon is used for the live returns too).

For the gate as a whole and for each rule: gap = mean return of entries the rule let through MINUS mean return of entries it turned away (positive = the rule
rejected the weaker ones). The backtest gap comes from the A/B report (candidate mean net minus the mean net of that rule's rejected days); the live gap from the
observation rows (verdict `passed` with no reasons vs rows whose reasons name the rule). A gap is cost-neutral (a cost taken from both sides cancels), so a
gross live return and a net backtest return can be compared. The live gap gets a 90 % bootstrap interval resampling whole observation DAYS (stocks on one day move together).

Status per rule:
  insufficient_live    fewer than max(--min-n, 15) rows on either side live, or the backtest had fewer than 15 rejected days (the B15 floor): nothing is concluded
  no_backtest_edge     the backtest gap was not positive, so there is nothing to confirm (a live gap is shown anyway)
  confirmed            live gap > 0 with the whole interval above 0 and not significantly below the backtest gap
  weaker_than_backtest live interval sits entirely below the backtest gap (but not below 0): the rule works less live than the backtest promised
  reversed             live interval entirely below 0: the rule turned away stocks that did BETTER; consider log mode for it
  inconclusive         anything else
Live rows are different from backtest days (every /decide candidate vs the technical-only baseline), so this is a sanity check on direction and size,
not an exact reproduction. Reason text is mapped to rule ids by wording; rows whose text matches no rule are counted as `unmapped`.
Output: live_vs_backtest.json + live_vs_backtest.md in --out (default: next to the --live file). Stdlib only, deterministic.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path
from statistics import mean
from typing import Any, Dict, List, Optional, Sequence

HARD_MIN_N = 15
RULES = ("chase", "stretch", "room", "rr", "stop_wide", "bearish")
PATTERNS = (("chase", re.compile(r"\bchase\b", re.I)), ("stretch", re.compile(r"stretched", re.I)), ("room", re.compile(r"ATR of room", re.I)),
            ("rr", re.compile(r"R:R", re.I)), ("stop_wide", re.compile(r"stop is too wide", re.I)), ("bearish", re.compile(r"bearish", re.I)))
PASSED = ("passed",)


def rules_of(reasons: Any) -> List[str]:
    """Rule ids named by a row's reason texts (a row can name several). Unknown text -> []."""
    out: List[str] = []
    for t in reasons if isinstance(reasons, list) else []:
        for rid, rx in PATTERNS:
            if isinstance(t, str) and rx.search(t) and rid not in out:
                out.append(rid)
    return out


def load_live(doc: Any) -> List[Dict[str, Any]]:
    if isinstance(doc, dict):
        doc = doc.get("recent") if isinstance(doc.get("recent"), list) else doc.get("observations") if isinstance(doc.get("observations"), list) else []
    return [r for r in doc if isinstance(r, dict)] if isinstance(doc, list) else []


def _ret(r: Dict[str, Any], h: int) -> Optional[float]:
    v = r.get(f"ret_{h}d")
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) < 1e6 else None


def gap_stats(passed: Sequence[Dict[str, Any]], tripped: Sequence[Dict[str, Any]], h: int, n_boot: int, seed: int = 7) -> Dict[str, Any]:
    """passed mean minus tripped mean (live, return at horizon h), with a day-cluster 90 % bootstrap interval."""
    cl: Dict[str, List[float]] = {}
    np_ = nt = 0
    for k, rows in ((0, passed), (1, tripped)):
        for r in rows:
            v = _ret(r, h)
            if v is None:
                continue
            c = cl.setdefault(str(r.get("obs_day")), [0, 0.0, 0, 0.0])
            c[2 * k] += 1
            c[2 * k + 1] += v
            if k == 0:
                np_ += 1
            else:
                nt += 1
    out: Dict[str, Any] = {"passed_n": np_, "tripped_n": nt, "passed_mean": None, "tripped_mean": None, "gap": None, "ci90": None, "day_clusters": len(cl)}
    if not np_ or not nt:
        return out
    arr = list(cl.values())
    sp, st = sum(c[1] for c in arr), sum(c[3] for c in arr)
    out.update(passed_mean=round(sp / np_, 3), tripped_mean=round(st / nt, 3), gap=round(sp / np_ - st / nt, 3))
    if len(arr) >= 10 and n_boot >= 50:
        rng = random.Random(seed)
        m = len(arr)
        gaps: List[float] = []
        for _ in range(n_boot):
            a = b = c_ = d = 0
            for k in rng.choices(range(m), k=m):
                x = arr[k]
                a, b, c_, d = a + x[0], b + x[1], c_ + x[2], d + x[3]
            if a and c_:
                gaps.append(b / a - d / c_)
        if len(gaps) >= n_boot // 2:
            gaps.sort()
            out["ci90"] = [round(gaps[int(0.05 * (len(gaps) - 1))], 3), round(gaps[int(0.95 * (len(gaps) - 1))], 3)]
    return out


def status(bt_gap: Optional[float], bt_n: int, live: Dict[str, Any], min_n: int) -> str:
    floor = max(min_n, HARD_MIN_N)
    if bt_gap is None or bt_n < HARD_MIN_N or live["passed_n"] < floor or live["tripped_n"] < floor or live["gap"] is None:
        return "insufficient_live"
    if bt_gap <= 0:
        return "no_backtest_edge"
    ci = live["ci90"]
    if ci is None:
        return "inconclusive"
    if ci[1] < 0:
        return "reversed"
    if ci[1] < bt_gap:
        return "weaker_than_backtest"
    if ci[0] > 0:
        return "confirmed"
    return "inconclusive"


def _bt_gap(rep: Dict[str, Any], rule: Optional[str]) -> Dict[str, Any]:
    cand = (rep.get("candidate") or {}).get("mean_net")
    rej = (rep.get("rejected") if rule is None else ((rep.get("by_rule") or {}).get(rule) or {}).get("rejected")) or {}
    m, n = rej.get("mean_net"), int(rej.get("n") or 0)
    return {"gap": round(cand - m, 3) if cand is not None and m is not None else None, "rejected_n": n, "rejected_mean_net": m}


def build(live_rows: List[Dict[str, Any]], ab_rep: Dict[str, Any], min_n: int = 30, n_boot: int = 500) -> Dict[str, Any]:
    h = int(ab_rep.get("horizon") or 10)
    passed = [r for r in live_rows if r.get("verdict") in PASSED and not (r.get("reasons") or [])]
    rejected = [r for r in live_rows if r.get("verdict") not in PASSED or (r.get("reasons") or [])]
    unmapped = sum(1 for r in rejected if (r.get("reasons") or []) and not rules_of(r.get("reasons")))
    rep: Dict[str, Any] = {"horizon": h, "live_rows": len(live_rows), "live_passed": len(passed), "live_rejected": len(rejected), "unmapped": unmapped,
                           "min_n": max(min_n, HARD_MIN_N), "gate": {}, "rules": {}}
    items = [("gate", None, rejected)] + [(rule, rule, [r for r in rejected if rule in rules_of(r.get("reasons"))]) for rule in RULES]
    for name, rule, trip in items:
        bt = _bt_gap(ab_rep, rule)
        lv = gap_stats(passed, trip, h, n_boot)
        entry = {"backtest": bt, "live": lv, "status": status(bt["gap"], bt["rejected_n"], lv, min_n)}
        if name == "gate":
            rep["gate"] = entry
        else:
            rep["rules"][name] = entry
    rep["summary"] = {k: sum(1 for v in rep["rules"].values() if v["status"] == k)
                      for k in ("confirmed", "weaker_than_backtest", "reversed", "inconclusive", "no_backtest_edge", "insufficient_live")}
    return rep


def to_markdown(rep: Dict[str, Any]) -> str:
    f = lambda v: "-" if v is None else str(v)  # noqa: E731
    L_ = [f"# Live vs backtest, per rule (T+{rep['horizon']})", "",
          f"- live rows read: {rep['live_rows']} (passed {rep['live_passed']}, rejected {rep['live_rejected']}, reason text matching no rule: {rep['unmapped']}); needs {rep['min_n']}+ rows on each side per rule",
          "- gap = mean return of entries the rule let through minus those it turned away (positive = the rule rejected the weaker ones). Live returns are gross, backtest net; the gap cancels costs.", "",
          "| rule | status | backtest gap pp | backtest rejected n | live gap pp | live 90% CI | live passed n | live tripped n |", "|---|---|---|---|---|---|---|---|"]
    for name, e in (("GATE (all rules)", rep["gate"]), *rep["rules"].items()):
        b, lv = e["backtest"], e["live"]
        L_.append(f"| {name} | `{e['status']}` | {f(b['gap'])} | {b['rejected_n']} | {f(lv['gap'])} | {f(lv['ci90'])} | {lv['passed_n']} | {lv['tripped_n']} |")
    L_ += ["", "`reversed` / `weaker_than_backtest` are the rows to act on (log mode for that rule); `insufficient_live` means keep collecting, not a pass. "
               "Live rows are every /decide candidate, the backtest a technical-only baseline: read direction and size, not exact equality."]
    return "\n".join(L_) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--live", required=True)
    ap.add_argument("--ab", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--min-n", type=int, default=30)
    ap.add_argument("--boot", type=int, default=500)
    a = ap.parse_args(argv)
    try:
        live = load_live(json.loads(Path(a.live).read_text()))
        ab_rep = json.loads(Path(a.ab).read_text())
    except (OSError, ValueError) as e:
        print(f"cannot read input: {e}", file=sys.stderr)
        return 2
    if not isinstance(ab_rep, dict):
        print("--ab is not an ab_report.json", file=sys.stderr)
        return 2
    if len(live) == 40:
        print("warning: exactly 40 live rows - the server default for `recent`; re-export with &recent=100000", file=sys.stderr)
    rep = build(live, ab_rep, a.min_n, a.boot)
    out = Path(a.out) if a.out else Path(a.live).resolve().parent / "lvb_out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "live_vs_backtest.json").write_text(json.dumps(rep, indent=1))
    md = to_markdown(rep)
    (out / "live_vs_backtest.md").write_text(md)
    print(md)
    return 0 if live else 1


if __name__ == "__main__":
    sys.exit(main())
