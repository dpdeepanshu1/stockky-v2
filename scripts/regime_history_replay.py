#!/usr/bin/env python3
"""regime_history_replay.py - does the Market Weather classifier see trouble BEFORE it happens? (Part 1 step 3 check; changes nothing)

For every past trading day D the state is computed from data that ended on D-1 (plus, for the "lock" column, D's own opening gap), then compared
with what the index actually did on D: its range, and how far it fell below the open. A useful classifier gives wider days in the worse states
and beats a plain "yesterday's ATR" predictor. This is a PROXY about the index, not a P&L test of your trades: it says whether the state carries
information, not whether the dials move money the right way. That is judged later, from the stamped trades (shadow report).

Verdicts (exit code): USEFUL (0) | USEFUL_NOT_BEYOND_ATR (1) | NO_EDGE (1) | NOT_ENOUGH_DATA (1)
Input: --nifty FILE [--vix FILE] (JSON from market-data /history) or --base URL to download ^NSEI and ^INDIAVIX.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

HERE = Path(__file__).resolve().parent
for _p in (HERE.parent, HERE.parent / "services" / "calibration-runner-service"):     # /app in the runner image; the repo layout otherwise
    if (_p / "regime" / "__init__.py").exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from regime import STATES  # noqa: E402
from regime import classifier, data, features  # noqa: E402

WARMUP = 80                  # bars needed before the first day is judged (ATR, 20-day, VIX percentile history)
MIN_DAYS = 250
MIN_TIGHT_DAYS = 30          # days in choppy + stressed + panic together
MIN_STATE_N = 15             # a state needs this many days to be part of the order check
MIN_RANK_CORR = 0.15
MIN_TIGHT_RATIO = 1.25       # mean range of tight days / mean range of calm+normal days
MONOTONE_Z = 2.0             # a worse state counts as out of order only if its mean range is LOWER by more than this many standard errors


def ranks(xs: Sequence[float]) -> List[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    out = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return out


def spearman(a: Sequence[float], b: Sequence[float]) -> Optional[float]:
    if len(a) != len(b) or len(a) < 3:
        return None
    ra, rb = ranks(a), ranks(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va, vb = sum((x - ma) ** 2 for x in ra), sum((y - mb) ** 2 for y in rb)
    return None if va <= 0 or vb <= 0 else cov / (va * vb) ** 0.5


def mean(xs: Sequence[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def replay(nifty: List[Dict[str, Any]], vix: Optional[List[Dict[str, Any]]] = None, warmup: int = WARMUP) -> List[Dict[str, Any]]:
    """One row per judged day. Features for day i use nifty[:i] and VIX bars dated before that day only."""
    rows: List[Dict[str, Any]] = []
    for i in range(max(warmup, 21), len(nifty)):
        day = nifty[i]
        hist = nifty[:i]
        vh = data.upto(vix, day["d"]) if vix else None
        f = features.daily_features(hist, vh)
        prev_c = hist[-1]["c"]
        morning = classifier.classify(f)
        lock_f = dict(f)
        lock_f.update(features.opening_features(prev_c, day["o"], f.get("atr14_pct")))
        lock = classifier.classify(lock_f)
        rows.append({
            "d": day["d"], "state": morning["state"], "score": morning["score"], "data_ok": morning["data_ok"],
            "lock_state": lock["state"], "lock_score": lock["score"], "atr14_pct": f.get("atr14_pct"),
            "range_pct": (day["h"] - day["l"]) / prev_c * 100.0, "down_pct": max(0.0, (day["o"] - day["l"]) / day["o"] * 100.0),
            "abs_ret_pct": abs(day["c"] / prev_c - 1.0) * 100.0, "ret_pct": (day["c"] / prev_c - 1.0) * 100.0,
        })
    return rows


def summarise(rows: List[Dict[str, Any]], key: str = "state") -> Dict[str, Any]:
    per: Dict[str, Dict[str, Any]] = {}
    for s in STATES:
        rs = [r for r in rows if r[key] == s]
        rng = [r["range_pct"] for r in rs]
        m = mean(rng)
        se = (sum((x - m) ** 2 for x in rng) / (len(rng) - 1) / len(rng)) ** 0.5 if len(rng) > 1 else None
        per[s] = {"n": len(rs), "mean_range_pct": m, "se_range_pct": se, "mean_down_pct": mean([r["down_pct"] for r in rs]),
                  "mean_abs_ret_pct": mean([r["abs_ret_pct"] for r in rs]), "mean_ret_pct": mean([r["ret_pct"] for r in rs])}
    return per


def verdict(rows: List[Dict[str, Any]], per: Dict[str, Dict[str, Any]], vix_present: bool) -> Dict[str, Any]:
    notes: List[str] = []
    n = len(rows)
    tight = [r for r in rows if r["state"] in STATES[2:]]
    easy = [r for r in rows if r["state"] in STATES[:2]]
    sc = [(r["score"], r["range_pct"], r["atr14_pct"]) for r in rows if r["score"] is not None and r["atr14_pct"] is not None]
    rho = spearman([x[0] for x in sc], [x[1] for x in sc])
    rho_atr = spearman([x[2] for x in sc], [x[1] for x in sc])
    ratio = None
    if tight and easy:
        ratio = mean([r["range_pct"] for r in tight]) / mean([r["range_pct"] for r in easy])
    seq = [(per[s]["mean_range_pct"], per[s]["se_range_pct"] or 0.0) for s in STATES if per[s]["n"] >= MIN_STATE_N]
    in_order = all(a[0] - b[0] <= MONOTONE_Z * (a[1] ** 2 + b[1] ** 2) ** 0.5 for a, b in zip(seq, seq[1:])) if len(seq) >= 2 else None
    if not vix_present:
        notes.append("no VIX series: the VIX components were left out, so this is a weaker test than the live classifier")
    out = {"days": n, "tight_days": len(tight), "rank_corr_score_vs_range": rho, "rank_corr_atr_vs_range": rho_atr,
           "beyond_atr": None if rho is None or rho_atr is None else rho - rho_atr, "tight_vs_easy_range_ratio": ratio, "states_in_order": in_order, "notes": notes}
    if n < MIN_DAYS or len(tight) < MIN_TIGHT_DAYS or not easy or rho is None or ratio is None or in_order is None:
        notes.append(f"need >= {MIN_DAYS} judged days and >= {MIN_TIGHT_DAYS} days in choppy/stressed/panic (have {n} / {len(tight)})")
        out["verdict"] = "NOT_ENOUGH_DATA"
    elif rho >= MIN_RANK_CORR and ratio >= MIN_TIGHT_RATIO and in_order:
        out["verdict"] = "USEFUL" if rho >= rho_atr else "USEFUL_NOT_BEYOND_ATR"
        if out["verdict"] != "USEFUL":
            notes.append("the state predicts wide days, but no better than yesterday's ATR alone: the extra inputs add nothing yet")
    else:
        out["verdict"] = "NO_EDGE"
        if rho < MIN_RANK_CORR:
            notes.append(f"rank correlation {rho:.2f} is below {MIN_RANK_CORR}")
        if ratio < MIN_TIGHT_RATIO:
            notes.append(f"tight days are only {ratio:.2f}x as wide as easy days (need {MIN_TIGHT_RATIO}x)")
        if not in_order:
            notes.append("the states are not in order of day width")
    return out


def _fmt(x: Optional[float], nd: int = 2) -> str:
    return "-" if x is None else f"{x:.{nd}f}"


def render(per: Dict[str, Any], per_lock: Dict[str, Any], v: Dict[str, Any], span: str) -> str:
    L = [f"Market Weather history replay ({span}), classifier {classifier.CLASSIFIER_VERSION}", "",
         "State from data up to the day before (morning). Range = high-low as % of the previous close.",
         f"{'state':9} {'days':>5} {'range%':>7} {'down%':>6} {'|ret|%':>7}   | with the opening gap (lock): {'days':>5} {'range%':>7}"]
    for s in STATES:
        a, b = per[s], per_lock[s]
        L.append(f"{s:9} {a['n']:5d} {_fmt(a['mean_range_pct']):>7} {_fmt(a['mean_down_pct']):>6} {_fmt(a['mean_abs_ret_pct']):>7}   |{'':29}{b['n']:5d} {_fmt(b['mean_range_pct']):>7}")
    L += ["", f"rank correlation, state score vs next-day range: {_fmt(v['rank_corr_score_vs_range'])}  (yesterday's ATR alone: {_fmt(v['rank_corr_atr_vs_range'])}, difference {_fmt(v['beyond_atr'])})",
          f"tight days (choppy/stressed/panic) vs easy days (calm/normal): {_fmt(v['tight_vs_easy_range_ratio'])}x as wide;  states in order of width: {v['states_in_order']}"]
    L += [f"note: {n}" for n in v["notes"]]
    L += ["", f"VERDICT: {v['verdict']}", "", "This tests the index only. It does not say the dials make money: that comes from the stamped trades in the shadow report. Nothing was changed."]
    return "\n".join(L) + "\n"


def _load(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--nifty", default="", help="JSON file: market-data /history of ^NSEI")
    ap.add_argument("--vix", default="", help="JSON file: market-data /history of ^INDIAVIX (optional)")
    ap.add_argument("--base", default="", help="market-data URL: download both instead of reading files")
    ap.add_argument("--period", default="5y")
    ap.add_argument("--out", default="", help="directory for regime_replay.txt and regime_replay.json")
    ap.add_argument("--warmup", type=int, default=WARMUP)
    a = ap.parse_args(argv)
    try:
        if a.base:
            ndoc = data.fetch_history(a.base, data.NIFTY, a.period)
            try:
                vdoc = data.fetch_history(a.base, data.VIX, a.period)
            except Exception as e:  # noqa: BLE001
                print(f"WARN: VIX download failed ({e}); continuing without it", file=sys.stderr)
                vdoc = None
        elif a.nifty:
            ndoc, vdoc = _load(a.nifty), (_load(a.vix) if a.vix else None)
        else:
            print("give --nifty FILE or --base URL", file=sys.stderr)
            return 2
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: could not read the history: {e}", file=sys.stderr)
        return 2
    nifty = data.clean_daily(ndoc)
    vix = data.clean_daily(vdoc) if vdoc is not None else None
    vix = vix or None
    if len(nifty) < a.warmup + 5:
        print(f"NOT_ENOUGH_DATA: only {len(nifty)} clean index bars", file=sys.stderr)
        return 1
    rows = replay(nifty, vix, a.warmup)
    per, per_lock = summarise(rows, "state"), summarise(rows, "lock_state")
    v = verdict(rows, per, vix is not None)
    text = render(per, per_lock, v, f"{rows[0]['d']} .. {rows[-1]['d']}")
    print(text)
    if a.out:
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "regime_replay.txt").write_text(text, encoding="utf-8")
        (out / "regime_replay.json").write_text(json.dumps({"classifier": classifier.CLASSIFIER_VERSION, "per_state": per, "per_state_lock": per_lock, "summary": v}, indent=1), encoding="utf-8")
    return 0 if v["verdict"] == "USEFUL" else 1


if __name__ == "__main__":
    sys.exit(main())
