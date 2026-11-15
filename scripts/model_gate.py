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

Drift floor (E589, round 564): the check above is champion vs challenger only. A challenger that is 0.03 worse is promoted and becomes the champion, so the next
night's challenger may be another 0.03 worse than THAT, and so on: a staircase down with no floor. With --anchor FILE the gate also keeps the best F1 and top-slice precision
any promoted model has reached (the anchor) and rejects a challenger that is more than --max-total-drop (default 0.05) below it, whatever the champion says. The anchor only
moves up on a promotion (best value per metric), is set to the challenger when the two models answer a different question, and starts from the champion when the file is
missing. Without --anchor nothing changes.

Exit codes: 0 promote, 3 keep the old model, 2 usage error. Pure standard library; reads the meta files (and the anchor file), writes the --json file and, only on a promotion, the --anchor file.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, Optional, Tuple

MAX_DROP = 0.02
MAX_TOTAL_DROP = 0.05
ANCHOR_KEYS = ("f1", "top_precision")


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
    out: Dict[str, Optional[float]] = {"f1": None, "top_precision": None, "lift": None, "top_lift": None, "top_slice": None}
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
            out["top_slice"] = round(_num(best.get("pred_pos_rate")) or 0.0, 2) or None     # round 568: the share of rows flagged, so two metas can be checked for the same slice
            r2, p2 = _num(best.get("recall")), _num(best.get("pred_pos_rate"))
            if r2 is not None and p2 and p2 > 0:
                out["top_lift"] = round(r2 / p2, 3)
    except Exception:  # noqa: BLE001
        pass
    return out


def same_slice(a: Optional[float], b: Optional[float]) -> bool:
    """Round 568: top_precision of a 10 % slice and of a 20 % slice are different numbers (the smaller slice usually reads higher). They are compared only when both
    sides name the same slice (rounded share of rows flagged); a side that names none is trusted as before."""
    return a is None or b is None or abs(a - b) < 0.015


def comparable(old: Dict[str, Any], new: Dict[str, Any]) -> Tuple[bool, str]:
    for k in ("target_gain_pct", "lookahead_days"):
        a, b = _num(old.get(k)), _num(new.get(k))
        if a is not None and b is not None and abs(a - b) > 1e-9:
            return False, f"{k} changed ({a} -> {b}): the two models answer different questions"
    return True, ""


def read_anchor(path: str) -> Optional[Dict[str, Any]]:
    """The saved anchor ({f1, top_precision, target_gain_pct, lookahead_days}) or None when missing / unreadable / not an object."""
    if not path:
        return None
    return read_meta(path)


def _anchor_from(m: Dict[str, Optional[float]], meta: Dict[str, Any]) -> Dict[str, Any]:
    return {"f1": m.get("f1"), "top_precision": m.get("top_precision"), "top_slice": m.get("top_slice"),
            "target_gain_pct": _num(meta.get("target_gain_pct")), "lookahead_days": _num(meta.get("lookahead_days"))}


def _anchor_same_question(anchor: Dict[str, Any], new: Dict[str, Any]) -> bool:
    for k in ("target_gain_pct", "lookahead_days"):
        a, b = _num(anchor.get(k)), _num(new.get(k))
        if a is not None and b is not None and abs(a - b) > 1e-9:
            return False
    return True


def next_anchor(anchor: Optional[Dict[str, Any]], mn: Dict[str, Optional[float]], new: Dict[str, Any]) -> Dict[str, Any]:
    """The anchor after promoting `new`: best value per metric, or the challenger's own numbers when there is no anchor or it answers another question."""
    fresh = _anchor_from(mn, new)
    if not anchor or not _anchor_same_question(anchor, new):
        return fresh
    out = dict(fresh)
    for k in ANCHOR_KEYS:
        a, b = _num(anchor.get(k)), mn.get(k)
        if k == "top_precision" and not same_slice(_num(anchor.get("top_slice")), mn.get("top_slice")):
            continue          # round 568: another slice: the challenger's own number and slice stand (already in `out`)
        out[k] = a if b is None else (b if a is None else max(a, b))
        if k == "top_precision" and b is None:
            out["top_slice"] = _num(anchor.get("top_slice"))
    return out


def decide(old: Optional[Dict[str, Any]], new: Optional[Dict[str, Any]], max_drop: float = MAX_DROP,
           anchor: Optional[Dict[str, Any]] = None, max_total_drop: Optional[float] = None) -> Dict[str, Any]:
    """{decision: promote|keep_old, reason, old: {...}, new: {...}, anchor_after: {...} on promote when max_total_drop is given}. Pure.
    max_total_drop None = no drift floor (the round 550 behaviour). With it, `anchor` is the saved best-so-far; None means start from the champion."""
    def done(res: Dict[str, Any], mo: Optional[Dict[str, Any]], mn: Optional[Dict[str, Any]], base: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if max_total_drop is not None and res["decision"] == "promote" and new is not None and mn is not None:
            res["anchor_after"] = next_anchor(base, mn, new)
        return res

    if new is None:
        return {"decision": "keep_old", "reason": "challenger meta unreadable: cannot judge it, keeping the champion", "old": None, "new": None}
    mn = metrics(new)
    if old is None:
        return done({"decision": "promote", "reason": "no champion meta to compare with: promoting", "old": None, "new": mn}, None, mn, anchor)
    mo = metrics(old)
    same, why = comparable(old, new)
    if not same:
        return done({"decision": "promote", "reason": f"not comparable: {why}", "old": mo, "new": mn}, mo, mn, None)   # new question: the anchor starts again
    drops = []
    for k in ("f1", "top_precision"):
        a, b = mo.get(k), mn.get(k)
        if a is None or b is None:
            continue
        if k == "top_precision" and not same_slice(mo.get("top_slice"), mn.get("top_slice")):
            continue          # round 568: not the same slice, so not the same question: skip this metric
        if b < a - max_drop:
            drops.append(f"{k} {b:.3f} < champion {a:.3f} - {max_drop}")
    if drops:
        return {"decision": "keep_old", "reason": "challenger is worse: " + "; ".join(drops), "old": mo, "new": mn}
    # the anchor: saved one if it answers the same question, else the champion's own numbers
    base = anchor if (anchor and _anchor_same_question(anchor, new)) else _anchor_from(mo, old)
    if max_total_drop is not None:
        floor = []
        for k in ANCHOR_KEYS:
            a, b = _num(base.get(k)), mn.get(k)
            if a is None or b is None:
                continue
            if k == "top_precision" and not same_slice(_num(base.get("top_slice")), mn.get("top_slice")):
                continue
            if b < a - max_total_drop:
                floor.append(f"{k} {b:.3f} < best accepted {a:.3f} - {max_total_drop}")
        if floor:
            return {"decision": "keep_old", "reason": "challenger is within the allowed drop of the champion but has drifted too far below the best accepted model: " + "; ".join(floor),
                    "old": mo, "new": mn}
    if all(mo.get(k) is None or mn.get(k) is None for k in ("f1", "top_precision")):
        return done({"decision": "promote", "reason": "no metric present on both sides: promoting", "old": mo, "new": mn}, mo, mn, base)
    return done({"decision": "promote", "reason": "challenger is not worse than the champion beyond the allowed drop", "old": mo, "new": mn}, mo, mn, base)


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--old", required=True, help="champion model_meta.json (may be missing: then the challenger is promoted)")
    ap.add_argument("--new", required=True, help="challenger model_meta.json")
    ap.add_argument("--max-drop", type=float, default=MAX_DROP)
    ap.add_argument("--json", default="", help="write the decision here")
    ap.add_argument("--anchor", default="", help="drift floor file (best accepted F1 / top precision); read, and rewritten on a promotion. Empty = no drift floor")
    ap.add_argument("--max-total-drop", type=float, default=MAX_TOTAL_DROP, help="largest allowed gap below the anchor (only with --anchor)")
    a = ap.parse_args(argv)
    anchor = read_anchor(a.anchor)
    res = decide(read_meta(a.old), read_meta(a.new), a.max_drop, anchor=anchor, max_total_drop=a.max_total_drop if a.anchor else None)
    print(f"MODEL GATE: {res['decision'].upper()} - {res['reason']}")
    for tag in ("old", "new"):
        if res.get(tag):
            print(f"  {tag}: " + ", ".join(f"{k}={v}" for k, v in res[tag].items()))
    if a.anchor and res["decision"] == "promote" and res.get("anchor_after"):
        os.makedirs(os.path.dirname(os.path.abspath(a.anchor)), exist_ok=True)
        with open(a.anchor, "w", encoding="utf-8") as fh:
            json.dump(res["anchor_after"], fh, indent=1, sort_keys=True)
        print("  anchor: " + ", ".join(f"{k}={v}" for k, v in sorted(res["anchor_after"].items())))
    if a.json:
        os.makedirs(os.path.dirname(os.path.abspath(a.json)), exist_ok=True)
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(res, fh, indent=1, sort_keys=True)
    return 0 if res["decision"] == "promote" else 3


if __name__ == "__main__":
    sys.exit(main())
