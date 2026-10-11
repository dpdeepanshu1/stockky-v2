"""screening/board_report.py - group309: the shadow report deciding whether a Market Board signal may be promoted.
Pure maths over row dicts. Forward returns are signed (positive = the signal's side was right); costs are a flat round-trip estimate
(BOARD_COST_PCT, NOT the exact charge model); every signal is compared with a control from the same sessions. Snapshot rows repeat,
so they are de-duplicated to one observation per (symbol, day, side).
Promotion (primary horizon 30m): n >= min_obs AND sessions >= min_sessions AND mean net > 0 AND the bootstrap interval of
(signal - control) excludes zero. The chase guard is inverted (promote when what it blocks does clearly WORSE). Research gate, not advice."""
from __future__ import annotations

import random
from typing import Dict, List, Optional, Sequence

HORIZON_COLS = {"15m": "fwd15_pct", "30m": "fwd30_pct", "60m": "fwd60_pct", "close": "fwd_close_pct"}
PRIMARY = "30m"

RULES: Dict[str, dict] = {
    "chase_guard": {
        "kind": "entry",
        "signal": lambda r: str(r.get("note") or "").startswith(("would_block", "enforced")),
        "control": lambda r: r.get("note") == "pass",
        "invert": True,
        "switch": "BOARD_GATE_MODE=enforce",
        "about": "entries refused as extended (chase) vs entries let through",
    },
    "top5_rank": {
        "kind": "snapshot",
        "signal": lambda r: (r.get("rank") or 99) <= 5,
        "control": lambda r: 5 < (r.get("rank") or 0) <= 20,
        "switch": "ranking factor (not built yet)",
        "about": "top 5 per side vs ranks 6-20",
    },
    "high_conviction": {
        "kind": "snapshot",
        "signal": lambda r: (r.get("conviction") or 0) >= 70,
        "control": lambda r: r.get("conviction") is not None and r["conviction"] < 70,
        "switch": "ranking factor (not built yet)",
        "about": "conviction >= 70 vs below",
    },
    "valid_fresh_zone": {
        "kind": "snapshot",
        "signal": lambda r: r.get("zone_status") == "fresh" and not r.get("note"),
        "control": lambda r: r.get("note") == "extended",
        "switch": "entry zone requirement (not built yet)",
        "about": "fresh zone, not extended vs extended",
    },
}


def dedupe(rows: Sequence[dict]) -> List[dict]:
    seen, out = set(), []
    for r in sorted(rows, key=lambda x: (str(x.get("day")), str(x.get("ts")))):
        k = (r.get("symbol"), r.get("day"), r.get("side"))
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def _net(r: dict, col: str, cost_pct: float) -> Optional[float]:
    v = r.get(col)
    return None if v is None else float(v) - cost_pct


def _stats(vals: List[float], sessions: int) -> dict:
    n = len(vals)
    if n == 0:
        return {"n": 0, "sessions": sessions, "mean_net": None, "win_rate": None}
    return {"n": n, "sessions": sessions, "mean_net": round(sum(vals) / n, 4), "win_rate": round(sum(1 for v in vals if v > 0) / n, 3)}


def bootstrap_diff(a: Sequence[float], b: Sequence[float], iters: int = 2000, alpha: float = 0.05, seed: int = 7) -> Optional[tuple]:
    if not a or not b:
        return None
    rng = random.Random(seed)
    diffs = []
    na, nb = len(a), len(b)
    for _ in range(iters):
        ma = sum(a[rng.randrange(na)] for _ in range(na)) / na
        mb = sum(b[rng.randrange(nb)] for _ in range(nb)) / nb
        diffs.append(ma - mb)
    diffs.sort()
    return (round(diffs[int(iters * alpha / 2)], 4), round(diffs[int(iters * (1 - alpha / 2)) - 1], 4))


def verdict(sig: dict, ctl: dict, ci: Optional[tuple], min_obs: int, min_sessions: int, invert: bool = False) -> str:
    if sig["n"] < min_obs or sig["sessions"] < min_sessions:
        return f"needs more data ({sig['n']}/{min_obs} obs, {sig['sessions']}/{min_sessions} sessions)"
    if ci is None or sig["mean_net"] is None:
        return "needs more data (no control group)"
    if invert:
        return "promote" if ci[1] < 0 else "do not promote"
    return "promote" if (sig["mean_net"] > 0 and ci[0] > 0) else "do not promote"


def report(rows: Sequence[dict], cost_pct: float, min_obs: int, min_sessions: int, params_version: Optional[str] = None) -> dict:
    out_rules = {}
    for name, rule in RULES.items():
        kind_rows = [r for r in rows if r.get("kind") == rule["kind"]]
        if params_version:
            kind_rows = [r for r in kind_rows if r.get("params_version") in (None, params_version)]
        sig_rows = [r for r in kind_rows if rule["signal"](r)]
        ctl_rows = [r for r in kind_rows if rule["control"](r) and not rule["signal"](r)]
        if rule["kind"] == "snapshot":
            sig_rows, ctl_rows = dedupe(sig_rows), dedupe(ctl_rows)
        horizons = {}
        for hname, col in HORIZON_COLS.items():
            sv = [v for v in (_net(r, col, cost_pct) for r in sig_rows) if v is not None]
            cv = [v for v in (_net(r, col, cost_pct) for r in ctl_rows) if v is not None]
            s_sessions = len({r.get("day") for r in sig_rows if r.get(col) is not None})
            c_sessions = len({r.get("day") for r in ctl_rows if r.get(col) is not None})
            horizons[hname] = {"signal": _stats(sv, s_sessions), "control": _stats(cv, c_sessions), "diff_ci95": bootstrap_diff(sv, cv)}
        p = horizons[PRIMARY]
        out_rules[name] = {"about": rule["about"], "would_switch_on": rule["switch"], "primary_horizon": PRIMARY, "horizons": horizons,
                           "verdict": verdict(p["signal"], p["control"], p["diff_ci95"], min_obs, min_sessions, rule.get("invert", False))}
    return {"cost_pct_assumed": cost_pct, "min_obs": min_obs, "min_sessions": min_sessions, "rows": len(rows),
            "params_version": params_version, "rules": out_rules,
            "notes": ["Forward returns are before costs; the flat cost estimate is subtracted here (not the exact charge model).",
                      "Snapshot rows are de-duplicated to one observation per symbol, day and side.",
                      "Instrumentation, not advice: a 'promote' verdict only means the written rule was met."]}
