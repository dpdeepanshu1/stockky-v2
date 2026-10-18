#!/usr/bin/env python3
"""Promotion-gate readout for the daily market-structure rules (group 381).

The execution plan (section 9) says the structure rules may be trusted in REAL mode only after gate G2 (benefit, out of sample) and gate G4
(live observations) pass. The rules are live now, so this script answers one question in plain words: does the evidence so far support that?

  python3 scripts/structure_gate_check.py --ab ab_out/ab_report.json [--ab seed2/ab_report.json ...] [--live report.json]

  --ab    one `ab_report.json` per random sample / seed (scripts/ms_ab_report.py). G2 needs EVERY one of them to pass.
  --live  the JSON of GET /api/structure/report (decision-prediction training app) for G4.
  --min-live-days / --min-live-n  G4 thresholds (defaults 20 observed sessions, 30 fully scored rows per group).
  --strict  exit 1 unless every gate that has input says PASS.

Verdicts: PASS | FAIL | NOT_ENOUGH_DATA | NO_INPUT. A FAIL or NOT_ENOUGH_DATA is a reason to keep the kill switches ready
(STRUCTURE_POLICY_MODE=off, STRUCTURE_GATE_MODE=off, STRUCTURE_STOP_MODE=off, STRUCTURE_PREPARE_UPGRADE=0), never a tuning instruction.
Stdlib only, reads files, changes nothing.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

PASS, FAIL, NED, NOINPUT = "PASS", "FAIL", "NOT_ENOUGH_DATA", "NO_INPUT"
MIN_KEPT_PCT = 30.0   # plan G2(e): the candidate must keep at least 30% of the baseline signals


def _num(v: Any) -> Optional[float]:
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def check_ab(rep: Dict[str, Any], name: str = "report") -> Dict[str, Any]:
    """G2 for one A/B report: verdict `pass`, enough signals kept, no regime with a negative edge."""
    reasons: List[str] = []
    verdict = str(rep.get("verdict") or "no_data")
    kept = _num(rep.get("kept_pct"))
    edge = _num(rep.get("edge_pp"))
    ci = (rep.get("bootstrap") or {}).get("ci90")
    if verdict in ("no_data", "insufficient_sample"):
        return {"name": name, "status": NED, "reasons": [f"A/B verdict is {verdict}"], "edge_pp": edge, "kept_pct": kept}
    status = PASS
    if verdict != "pass":
        status = FAIL
        reasons.append(f"A/B verdict is {verdict} (edge {edge} pp, 90% CI {ci}); the plan needs `pass`")
    if kept is None or kept < MIN_KEPT_PCT:
        status = FAIL
        reasons.append(f"the policy keeps only {kept}% of baseline signals (needs >= {MIN_KEPT_PCT:g}%)")
    bad = [k for k, v in (rep.get("by_regime") or {}).items() if _num((v or {}).get("edge_pp")) is not None and _num(v["edge_pp"]) < 0
           and (_num(v.get("candidate_n")) or 0) >= 30]
    if bad:
        status = FAIL
        reasons.append("negative edge in regime(s): " + ", ".join(sorted(bad)))
    rules = rep.get("by_rule") or {}
    # drop_one_edge_pp = candidate minus the gate-without-this-rule (scripts/ms_ab_report.py): positive = the rule earns its place, NEGATIVE =
    # the gate does better without it. (group424: this check used to be the other way round and named the helpful rule, not the harmful one.)
    hurting = [k for k, v in rules.items() if _num((v or {}).get("drop_one_edge_pp")) is not None and _num(v["drop_one_edge_pp"]) < -0.05
               and (_num(v.get("drop_one_n")) or 0) >= 30]
    if hurting:
        reasons.append("note: the gate does better WITHOUT rule(s) " + ", ".join(sorted(hurting)) + " (drop-one edge is negative)")
    return {"name": name, "status": status, "reasons": reasons, "edge_pp": edge, "kept_pct": kept}


def check_g2(reports: List[Dict[str, Any]], names: Optional[List[str]] = None) -> Dict[str, Any]:
    if not reports:
        return {"gate": "G2", "status": NOINPUT, "reasons": ["no A/B report given (--ab)"], "items": []}
    items = [check_ab(r, (names or [])[i] if names and i < len(names) else f"report{i + 1}") for i, r in enumerate(reports)]
    sts = {i["status"] for i in items}
    status = FAIL if FAIL in sts else NED if NED in sts else PASS
    reasons = [f"{i['name']}: {r}" for i in items for r in i["reasons"]]
    if status == PASS and len(items) < 2:
        status, reasons = NED, reasons + ["only one sample: run 2-3 seeds before reading a pass (plan section 9)"]
    return {"gate": "G2", "status": status, "reasons": reasons, "items": items}


def check_g4(live: Optional[Dict[str, Any]], min_days: int = 20, min_n: int = 30) -> Dict[str, Any]:
    """G4 on the live /api/structure/report: enough sessions, enough scored rows, passed BUY NOWs not worse than the demoted ones."""
    if not live:
        return {"gate": "G4", "status": NOINPUT, "reasons": ["no live report given (--live)"]}
    reasons: List[str] = []
    days = int(_num(live.get("days_observed")) or 0)
    p, d = live.get("passed_buy_now") or {}, live.get("policy_demoted") or {}
    np_, nd = int(_num(p.get("n_done_20d")) or 0), int(_num(d.get("n_done_20d")) or 0)
    if days < min_days:
        reasons.append(f"only {days} observed session(s), needs {min_days}")
    if np_ < min_n or nd < min_n:
        reasons.append(f"fully scored rows: passed {np_}, demoted {nd}, each needs {min_n}")
    if reasons:
        return {"gate": "G4", "status": NED, "reasons": reasons}
    hp, hd = _num(p.get("hit_5pct_within_20d_pct")), _num(d.get("hit_5pct_within_20d_pct"))
    rp, rd = _num(p.get("avg_ret_20")), _num(d.get("avg_ret_20"))
    status = PASS
    if hp is None or hd is None or hp < hd:
        status = FAIL
        reasons.append(f"BUY NOWs that passed hit +5% in 20d {hp}% vs demoted {hd}%: the policy did not keep the better setups")
    if rp is not None and rd is not None and rp < rd:
        status = FAIL
        reasons.append(f"avg 20d return passed {rp} vs demoted {rd}")
    pu = ((live.get("prepare_upgrade") or {}).get("edge")) or {}
    dh = _num(pu.get("hit_5pct_upgraded_minus_held_pts"))
    if dh is not None and dh < 0:
        reasons.append(f"note: P9 upgraded PREPAREs did {dh} pts worse than the ones held at PREPARE (STRUCTURE_PREPARE_UPGRADE=0 turns the upgrade off)")
    if status == PASS:
        reasons.append("note: no confidence interval here; treat PASS as 'not contradicted', not as proof")
    return {"gate": "G4", "status": status, "reasons": reasons}


def render(results: List[Dict[str, Any]]) -> str:
    out = ["Market-structure promotion gates", ""]
    for r in results:
        out.append(f"{r['gate']}: {r['status']}")
        out += [f"   - {x}" for x in r["reasons"]]
    out += ["", "Kill switches: STRUCTURE_POLICY_MODE=off, STRUCTURE_GATE_MODE=off, STRUCTURE_STOP_MODE=off, STRUCTURE_EXIT_MODE=off, STRUCTURE_PREPARE_UPGRADE=0"]
    return "\n".join(out)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ab", action="append", default=[], help="ab_report.json (repeat for several seeds)")
    ap.add_argument("--live", default="", help="JSON of GET /api/structure/report")
    ap.add_argument("--min-live-days", type=int, default=20)
    ap.add_argument("--min-live-n", type=int, default=30)
    ap.add_argument("--strict", action="store_true")
    a = ap.parse_args(argv)
    reports = [json.loads(Path(p).read_text()) for p in a.ab]
    live = json.loads(Path(a.live).read_text()) if a.live else None
    if isinstance(live, dict) and isinstance(live.get("report"), dict):
        live = live["report"]
    res = [check_g2(reports, a.ab), check_g4(live, a.min_live_days, a.min_live_n)]
    print(render(res))
    if a.strict and any(r["status"] != PASS for r in res if r["status"] != NOINPUT):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
