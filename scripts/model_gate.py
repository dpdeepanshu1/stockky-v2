#!/usr/bin/env python3
"""scripts/model_gate.py - champion / challenger gate for the nightly prediction model (E575, round 550).

The nightly GitHub Action (.github/workflows/retrain-model.yml) trains a new model every night. Before this gate it copied the new model.pkl over the
old one without looking at how good it was, so one unlucky holdout split could silently replace a better model. This script compares the challenger's
`model_meta.json` (written by pred_train.py next to model.pkl) with the champion's saved meta and says whether to promote it.

  python3 scripts/model_gate.py --old data/prediction-model/model_meta.json --new services/decision-prediction-service/prediction/model_meta.json --json gate.json

Compared (same target only): the F1 at the chosen threshold, and the precision of the TOP slice of scores (the smallest quantile in the meta, about the
top 10 %), the picks that matter. The challenger is rejected when either number drops by more than --max-drop (default 0.02) against the champion.
Also reported (information only): the lift = recall / share of rows flagged, i.e. how many times better than flagging at random.

Not comparable, so PROMOTE with a note: different target_gain_pct or lookahead_days, a missing champion file, or a metric missing on either side.
A challenger whose meta cannot be read is NOT promoted (we cannot judge it).

Exit codes: 0 promote, 3 keep the old model, 2 usage error. Pure standard library; reads two files, writes only the --json file.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, Optional, Tuple

MAX_DROP = 0.02


def _num(x: Any) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) != float("inf") else None


def read_meta(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) else None
    except (OSError, ValueError):
        return None


def metrics(meta: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """f1 at the chosen threshold, precision of the top score slice, and the lift of both. None where the meta lacks them. Never raises."""
    out: Dict[str, Optional[float]] = {"f1": None, "top_precision": None, "lift": None, "top_lift": None}
    try:
        ts = meta.get("threshold_stats") or {}
        ch = ts.get("chosen") or {}
        out["f1"] = _num(ch.get("f1"))
        rec, rate = _num(ch.get("recall")), _num(ch.get("pred_pos_rate"))
        if rec is not None and rate and rate > 0:
            out["lift"] = round(rec / rate, 3)
        tq = ts.get("top_quantile") or {}
        best = None
        for v in tq.values():
            if isinstance(v, dict) and _num(v.get("pred_pos_rate")) and _num(v.get("precision")) is not None:
                if best is None or _num(v["pred_pos_rate"]) < _num(best["pred_pos_rate"]):
                    best = v
        if best:
            out["top_precision"] = _num(best.get("precision"))
            r2, p2 = _num(best.get("recall")), _num(best.get("pred_pos_rate"))
            if r2 is not None and p2 and p2 > 0:
                out["top_lift"] = round(r2 / p2, 3)
    except Exception:  # noqa: BLE001
        pass
    return out


def comparable(old: Dict[str, Any], new: Dict[str, Any]) -> Tuple[bool, str]:
    for k in ("target_gain_pct", "lookahead_days"):
        a, b = _num(old.get(k)), _num(new.get(k))
        if a is not None and b is not None and abs(a - b) > 1e-9:
            return False, f"{k} changed ({a} -> {b}): the two models answer different questions"
    return True, ""


def decide(old: Optional[Dict[str, Any]], new: Optional[Dict[str, Any]], max_drop: float = MAX_DROP) -> Dict[str, Any]:
    """{decision: promote|keep_old, reason, old: {...}, new: {...}}. Pure."""
    if new is None:
        return {"decision": "keep_old", "reason": "challenger meta unreadable: cannot judge it, keeping the champion", "old": None, "new": None}
    mn = metrics(new)
    if old is None:
        return {"decision": "promote", "reason": "no champion meta to compare with: promoting", "old": None, "new": mn}
    mo = metrics(old)
    same, why = comparable(old, new)
    if not same:
        return {"decision": "promote", "reason": f"not comparable: {why}", "old": mo, "new": mn}
    drops = []
    for k in ("f1", "top_precision"):
        a, b = mo.get(k), mn.get(k)
        if a is None or b is None:
            continue
        if b < a - max_drop:
            drops.append(f"{k} {b:.3f} < champion {a:.3f} - {max_drop}")
    if drops:
        return {"decision": "keep_old", "reason": "challenger is worse: " + "; ".join(drops), "old": mo, "new": mn}
    if all(mo.get(k) is None or mn.get(k) is None for k in ("f1", "top_precision")):
        return {"decision": "promote", "reason": "no metric present on both sides: promoting", "old": mo, "new": mn}
    return {"decision": "promote", "reason": "challenger is not worse than the champion beyond the allowed drop", "old": mo, "new": mn}


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--old", required=True, help="champion model_meta.json (may be missing: then the challenger is promoted)")
    ap.add_argument("--new", required=True, help="challenger model_meta.json")
    ap.add_argument("--max-drop", type=float, default=MAX_DROP)
    ap.add_argument("--json", default="", help="write the decision here")
    a = ap.parse_args(argv)
    res = decide(read_meta(a.old), read_meta(a.new), a.max_drop)
    print(f"MODEL GATE: {res['decision'].upper()} - {res['reason']}")
    for tag in ("old", "new"):
        if res.get(tag):
            print(f"  {tag}: " + ", ".join(f"{k}={v}" for k, v in res[tag].items()))
    if a.json:
        os.makedirs(os.path.dirname(os.path.abspath(a.json)), exist_ok=True)
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(res, fh, indent=1, sort_keys=True)
    return 0 if res["decision"] == "promote" else 3


if __name__ == "__main__":
    sys.exit(main())
