"""Entry-slippage review: is paying above the signal price a material cost on real trades? (group 468, item 9)

Reads the same rt_*.json record exports as records_report.py and answers one question: should anyone build the adaptive
limit-order walk? It does NOT change any order. Slippage here is entry_price vs signal_price (+ = paid above the signal),
in percent and in rupees (slippage_pct * entry * quantity). The verdict is 'material' when EITHER the mean slippage is at
least --mean-pct (default 0.15%) OR the rupees lost to slippage are at least --share-pct (default 10%) of the gross profit
(or, when gross is not positive, of the charges-plus-loss it adds to). The thresholds are judgement calls, printed with the
verdict; under --min-n trades (default 30) the verdict is 'too few trades'.
"""
from __future__ import annotations

import argparse
import glob
import json
import statistics as st
import sys
from collections import defaultdict
from typing import Any, Dict, List, Optional

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from records_report import filter_origin, load_records, num  # noqa: E402


def _rupees(r: Dict[str, Any]) -> Optional[float]:
    s, e, q = num(r.get("entry_slippage_pct")), num(r.get("entry_price")), num(r.get("quantity"))
    return None if s is None or not e or not q or q <= 0 else s / 100.0 * e * q / (1.0 + s / 100.0)


def _grp(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    pct = [v for v in (num(r.get("entry_slippage_pct")) for r in rows) if v is not None]
    rup = [v for v in (_rupees(r) for r in rows) if v is not None]
    return {"n": len(pct), "mean_pct": round(st.mean(pct), 3) if pct else None, "median_pct": round(st.median(pct), 3) if pct else None,
            "worst_pct": round(max(pct), 3) if pct else None, "rupees": round(sum(rup), 2) if rup else None}


def review(records: List[Dict[str, Any]], mean_pct: float = 0.15, share_pct: float = 10.0, min_n: int = 30) -> Dict[str, Any]:
    """Pure. {"overall", "by_hour", "by_source_tab", "gross", "share_of_gross_pct", "verdict", "reason"}."""
    rows = [r for r in records if num(r.get("entry_slippage_pct")) is not None]
    overall = _grp(rows)
    rep: Dict[str, Any] = {"trades": len(records), "with_slippage": len(rows), "overall": overall,
                           "thresholds": {"mean_pct": mean_pct, "share_pct": share_pct, "min_n": min_n}}
    for key, label in (("entry_hour_ist", "by_hour"), ("source_tab", "by_source_tab")):
        g: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for r in rows:
            g[str(r.get(key))].append(r)
        rep[label] = {k: _grp(v) for k, v in sorted(g.items(), key=lambda kv: (-len(kv[1]), kv[0]))}
    gross = sum(num(r.get("gross_pnl")) or 0.0 for r in records)
    rep["gross"] = round(gross, 2)
    cost = overall["rupees"]
    rep["share_of_gross_pct"] = round(100.0 * cost / gross, 1) if cost is not None and gross > 0 else None
    if overall["n"] < min_n:
        rep["verdict"], rep["reason"] = "too_few_trades", f"{overall['n']} trade(s) with a slippage number, need {min_n}"
    elif overall["mean_pct"] is not None and overall["mean_pct"] >= mean_pct:
        rep["verdict"], rep["reason"] = "material", f"mean slippage {overall['mean_pct']}% is at least {mean_pct}%"
    elif rep["share_of_gross_pct"] is not None and rep["share_of_gross_pct"] >= share_pct:
        rep["verdict"], rep["reason"] = "material", f"slippage cost is {rep['share_of_gross_pct']}% of the gross profit (limit {share_pct}%)"
    else:
        rep["verdict"], rep["reason"] = "not_material", "below both thresholds: a limit-order walk would save little"
    return rep


def to_markdown(rep: Dict[str, Any]) -> str:
    o = rep["overall"]
    f = lambda v: "-" if v is None else str(v)  # noqa: E731
    L = [f"# Entry slippage review - {rep['trades']} trades, {rep['with_slippage']} with a slippage number", "",
         f"Mean {f(o['mean_pct'])}%, median {f(o['median_pct'])}%, worst {f(o['worst_pct'])}%; paid above the signal in total: {f(o['rupees'])} rupees"
         + (f" ({rep['share_of_gross_pct']}% of the gross profit)" if rep.get("share_of_gross_pct") is not None else "") + ".",
         f"Verdict: **{rep['verdict']}** - {rep['reason']}.", ""]
    for title, key in (("entry hour IST", "by_hour"), ("source tab", "by_source_tab")):
        L += [f"## by {title}", "", "| group | n | mean % | median % | worst % | rupees |", "|---|---|---|---|---|---|"]
        for k, g in rep[key].items():
            L.append(f"| {k[:32]} | {g['n']} | {f(g['mean_pct'])} | {f(g['median_pct'])} | {f(g['worst_pct'])} | {f(g['rupees'])} |")
        L.append("")
    L.append("Build the adaptive limit-order walk only when the verdict is 'material' on a sample of this size. Thresholds are judgement calls.")
    return "\n".join(L)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("files", nargs="*", help="record export files (default rt_*.json)")
    ap.add_argument("--mode", default="REAL")
    ap.add_argument("--origin", default="system", choices=["all", "system", "imported"])
    ap.add_argument("--mean-pct", type=float, default=0.15)
    ap.add_argument("--share-pct", type=float, default=10.0)
    ap.add_argument("--min-n", type=int, default=30)
    ap.add_argument("--json", default="")
    a = ap.parse_args(argv)
    paths = [p for pat in (a.files or ["rt_*.json"]) for p in (sorted(glob.glob(pat)) or [pat])]
    recs = filter_origin(load_records(paths, a.mode or None), a.origin)
    if not recs:
        print(f"no records found in {len(paths)} file(s)", file=sys.stderr)
        return 1
    rep = review(recs, a.mean_pct, a.share_pct, a.min_n)
    print(to_markdown(rep))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
