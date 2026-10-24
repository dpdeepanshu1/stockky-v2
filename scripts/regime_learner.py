#!/usr/bin/env python3
"""regime_learner.py - the slow weekly learner (Part 1 step 7). PROPOSES at most ONE dial change; writes no setting, sends no dial.

Groups your closed real-trade records by (market state in force at entry) x (time-of-day bucket), using the shadow log for the state (same lookup as
regime_shadow_report). For each group:
  * old trades fade: every trade is weighted 0.5 ** (age_days / HALF_LIFE_DAYS)
  * shrinkage: the group's weighted mean P&L is pulled toward the all-trade mean, with SHRINK_K trades' worth of weight
  * a group needs MIN_GROUP_TRADES raw trades before it can say anything
  * a trial counter: every group that reaches the minimum counts as one trial; the confidence bar rises with the number of trials
    (z = the normal quantile of 1 - 0.05 / trials, a Bonferroni-style deflation; the idea behind the Deflated Sharpe paper)
Then, at most one proposal per run, strongest evidence first:
  * TIGHTEN (safe direction): a restricted-bucket group whose upper bound is below zero while the profile does NOT already cover that bucket
  * LOOSEN (needs evidence): a bucket the profile currently blocks whose group has a lower bound above zero -> relax that ONE dial by one step,
    never beyond the all-states default (a capped relaxation, not a removal)
A loosening proposal is only CONFIRMED when the previous run (a different day, kept in learner_history.jsonl) proposed the identical change: the same
two-run rule autocalib uses. The runner never applies anything from here. A confirmed proposal is for YOU to read; applying it is a separate step.

Verdict (exit code): PROPOSE_TIGHTEN (0) | PROPOSE_LOOSEN (0) | NO_CHANGE (0) | NOT_ENOUGH_DATA (1)
Usage: regime_learner.py --log weather/log.jsonl rt_latest.json [more records files...] [--out DIR] [--history FILE] [--today YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import NormalDist
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import regime_shadow_report as S  # noqa: E402  (same log / record loaders and state lookup, so the two reports can never disagree)
from regime import STATES  # noqa: E402
from regime import profiles  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
HALF_LIFE_DAYS = 45.0
SHRINK_K = 15.0
MIN_GROUP_TRADES = 30
MIN_TOTAL_TRADES = 90
MIN_DAYS = 20
LOOSEN_STEP_MIN = 30           # a time dial moves 30 minutes
BUCKETS = (("open", 0, 9 * 60 + 45), ("mid", 9 * 60 + 45, 13 * 60), ("late", 13 * 60, 24 * 60))   # minutes after midnight, [lo, hi)
# bucket -> the dial that governs it and the all-states default it may be relaxed toward (never beyond)
BUCKET_DIAL = {"open": ("OPENING_ENTRY_NOT_BEFORE_IST", "09:20"), "late": ("ENTRY_LAST_TIME_IST", "14:30")}


def bucket_of(minute: int) -> str:
    for name, lo, hi in BUCKETS:
        if lo <= minute < hi:
            return name
    return "late"


def _mmss(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def weighted_stats(rows: List[Dict[str, float]]) -> Dict[str, float]:
    w = sum(r["w"] for r in rows)
    if w <= 0:
        return {"n": len(rows), "n_eff": 0.0, "mean": 0.0, "se": float("inf")}
    mean = sum(r["w"] * r["pnl"] for r in rows) / w
    var = sum(r["w"] * (r["pnl"] - mean) ** 2 for r in rows) / w
    n_eff = (w * w) / sum(r["w"] ** 2 for r in rows)           # Kish effective sample size
    return {"n": len(rows), "n_eff": n_eff, "mean": mean, "se": math.sqrt(var / max(n_eff, 1.0))}


def restricted(state: str, bucket: str) -> bool:
    """Does the CURRENT profile for this state hold this bucket's dial tighter than the all-states default?"""
    dial, floor = BUCKET_DIAL[bucket]
    cur = S._hm(profiles.PROFILE[state].get(dial))
    if cur is None:
        return False
    return cur > S._hm(floor) if bucket == "open" else cur < S._hm(floor)


def tighter_target(bucket: str) -> int:
    """The value that closes the whole bucket: the open bucket ends 09:45, the late bucket starts 13:00."""
    return BUCKETS[0][2] if bucket == "open" else BUCKETS[2][1]


def learn(log: List[Dict[str, Any]], trades: List[Dict[str, Any]], today: datetime) -> Dict[str, Any]:
    joined, no_state = [], 0
    for t in trades:
        st = S.state_at(log, t["at"])
        if st is None:
            no_state += 1
            continue
        age = max(0.0, (today - t["at"]).total_seconds() / 86400.0)
        joined.append({"state": st, "bucket": bucket_of(t["min"]), "pnl": t["pnl"], "w": 0.5 ** (age / HALF_LIFE_DAYS)})
    days = {r["day"] for r in log if r["stage"] in ("lock", "tick")}
    out: Dict[str, Any] = {"days_logged": len(days), "trades_joined": len(joined), "trades_no_state": no_state, "half_life_days": HALF_LIFE_DAYS,
                           "shrink_k": SHRINK_K, "groups": [], "trials": 0, "proposal": None}
    if len(days) < MIN_DAYS or len(joined) < MIN_TOTAL_TRADES:
        out["verdict"] = "NOT_ENOUGH_DATA"
        return out
    all_stats = weighted_stats(joined)
    grand = all_stats["mean"]
    groups: Dict[tuple, List[Dict[str, float]]] = {}
    for r in joined:
        groups.setdefault((r["state"], r["bucket"]), []).append(r)
    eligible = {k: v for k, v in groups.items() if len(v) >= MIN_GROUP_TRADES}
    trials = max(1, len(eligible))
    z = NormalDist().inv_cdf(1.0 - 0.05 / trials)
    out["trials"], out["z"] = trials, round(z, 3)
    cands: List[Dict[str, Any]] = []
    for (st, bk), rows in sorted(groups.items()):
        s = weighted_stats(rows)
        shrunk = (s["n_eff"] * s["mean"] + SHRINK_K * grand) / (s["n_eff"] + SHRINK_K)
        g = {"state": st, "bucket": bk, "trades": s["n"], "n_eff": round(s["n_eff"], 1), "mean": round(s["mean"], 2), "shrunk_mean": round(shrunk, 2),
             "eligible": (st, bk) in eligible}
        if g["eligible"]:
            lb, ub = shrunk - z * s["se"], shrunk + z * s["se"]
            g["lower"], g["upper"] = round(lb, 2), round(ub, 2)
            if bk in BUCKET_DIAL:
                dial, floor = BUCKET_DIAL[bk]
                cur = profiles.PROFILE[st].get(dial)
                cur_m = S._hm(cur) if cur is not None else S._hm(floor)
                if restricted(st, bk) and lb > 0:
                    m = cur_m + (-LOOSEN_STEP_MIN if bk == "open" else LOOSEN_STEP_MIN)
                    m = max(m, S._hm(floor)) if bk == "open" else min(m, S._hm(floor))        # capped: never beyond the default
                    if m != cur_m:
                        cands.append({"action": "LOOSEN", "state": st, "dial": dial, "from": _mmss(cur_m), "to": _mmss(m), "evidence": lb, "group": f"{st}/{bk}"})
                tgt = tighter_target(bk)
                if ub < 0 and ((bk == "open" and cur_m < tgt) or (bk == "late" and cur_m > tgt)):
                    cands.append({"action": "TIGHTEN", "state": st, "dial": dial, "from": _mmss(cur_m), "to": _mmss(tgt), "evidence": -ub, "group": f"{st}/{bk}"})
        out["groups"].append(g)
    if cands:
        cands.sort(key=lambda c: -c["evidence"])
        best = cands[0]
        best["evidence"] = round(best["evidence"], 2)
        out["proposal"] = best                                    # ONE dial per run, the rest are only counted
        out["other_candidates"] = len(cands) - 1
        out["verdict"] = "PROPOSE_LOOSEN" if best["action"] == "LOOSEN" else "PROPOSE_TIGHTEN"
    else:
        out["verdict"] = "NO_CHANGE"
    return out


def same_change(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> bool:
    keys = ("action", "state", "dial", "from", "to")
    return bool(a and b and all(a.get(k) == b.get(k) for k in keys))


def two_run_check(result: Dict[str, Any], history_path: str, today: str) -> None:
    """A LOOSEN is confirmed only if the last run on a DIFFERENT day proposed the identical change. Appends this run to the history."""
    prev = None
    if history_path and Path(history_path).exists():
        for ln in Path(history_path).read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if r.get("day") != today:
                prev = r
    p = result.get("proposal")
    if p:
        p["confirmed"] = p["action"] == "TIGHTEN" or same_change(p, (prev or {}).get("proposal"))
        if not p["confirmed"]:
            p["note"] = "needs the same proposal from a second run on a different day before it is confirmed"
    if history_path:
        Path(history_path).parent.mkdir(parents=True, exist_ok=True)
        with open(history_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"day": today, "verdict": result["verdict"], "proposal": p}) + "\n")


def render(r: Dict[str, Any]) -> str:
    L = ["Market Weather learner (proposes at most one change; nothing was applied)", "",
         f"Logged trading days: {r['days_logged']} (need {MIN_DAYS})   Trades joined: {r['trades_joined']} (need {MIN_TOTAL_TRADES})   without a state: {r['trades_no_state']}"]
    if r["verdict"] != "NOT_ENOUGH_DATA":
        L += [f"Trials (groups with >= {MIN_GROUP_TRADES} trades): {r['trials']}   confidence bar z = {r.get('z')}   half-life {HALF_LIFE_DAYS:.0f} days   shrink {SHRINK_K:.0f}", "",
              f"{'group':16} {'trades':>6} {'n_eff':>6} {'mean':>8} {'shrunk':>8} {'lower':>8} {'upper':>8}"]
        for g in r["groups"]:
            lo = f"{g['lower']:>8.0f}" if "lower" in g else f"{'-':>8}"
            up = f"{g['upper']:>8.0f}" if "upper" in g else f"{'-':>8}"
            L.append(f"{g['state'] + '/' + g['bucket']:16} {g['trades']:>6} {g['n_eff']:>6.0f} {g['mean']:>8.0f} {g['shrunk_mean']:>8.0f} {lo} {up}")
        p = r.get("proposal")
        L.append("")
        if p:
            L.append(f"PROPOSAL ({p['action']}, {'CONFIRMED' if p.get('confirmed') else 'NOT yet confirmed'}): {p['state']} {p['dial']} {p['from']} -> {p['to']}  (from {p['group']})")
            if p.get("note"):
                L.append(p["note"])
            if r.get("other_candidates"):
                L.append(f"{r['other_candidates']} weaker candidate(s) not proposed: one dial per run.")
        else:
            L.append("No group beat the confidence bar. Nothing to change.")
    L += ["", "A direction from your own thin record, not a proof. This job applies nothing; a confirmed proposal still needs your reading and a separate change.",
          f"REGIME_LEARNER: {r['verdict']}"]
    return "\n".join(L)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("records", nargs="+")
    ap.add_argument("--log", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--history", default="", help="learner_history.jsonl (the two-run rule reads and appends it)")
    ap.add_argument("--today", default="", help="YYYY-MM-DD (tests)")
    a = ap.parse_args(argv)
    try:
        log, trades = S.load_log(a.log), S.load_trades(a.records)
    except (OSError, ValueError) as e:
        print(f"cannot read input: {e}")
        print("REGIME_LEARNER: NOT_ENOUGH_DATA")
        return 1
    today = datetime.fromisoformat(a.today + "T16:00:00+05:30") if a.today else datetime.now(IST)
    r = learn(log, trades, today)
    if r["verdict"] != "NOT_ENOUGH_DATA":
        two_run_check(r, a.history, today.strftime("%Y-%m-%d"))
    print(render(r))
    if a.out:
        Path(a.out).mkdir(parents=True, exist_ok=True)
        (Path(a.out) / "regime_learner.json").write_text(json.dumps(r, indent=1), encoding="utf-8")
    return 1 if r["verdict"] == "NOT_ENOUGH_DATA" else 0


if __name__ == "__main__":
    sys.exit(main())
