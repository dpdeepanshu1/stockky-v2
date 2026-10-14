"""stockky_ms.observation - track what the structure gate saw and what the stock did next (WS-6). Pure; the services own the tables.

An observation is one gate verdict on one symbol on one day: the price then, the structural stop / target, the verdict
(rejected_enforced | rejected_shadow | passed | no_opinion) and, later, the forward outcome (T+1 / 5 / 10 / 20 trading days, MFE, MAE and the
first touch of target vs stop). Rejected stocks are tracked exactly like the ones that passed, so the only fair question can be asked:
"did what the gate turned away really do worse than what it let through?"

  classify(...)           the verdict string
  new_observation(...)    the row to insert (no outcome yet)
  due(row, now_ts)        whether (and how much history) the row still needs - a row is done once T+20 exists
  evaluate_row(row, bars_after)   outcome fields from the daily bars AFTER the observation day
  aggregate(rows)         per-verdict statistics + the gate edge, for the status endpoints and the dashboard
"""
from __future__ import annotations

import json
from statistics import mean, median
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .backtest import HORIZONS, forward_outcome
from .candles import day_key, ist_date
from .ms_types import C, H, L, T, finite

VERDICTS = ("rejected_enforced", "rejected_shadow", "passed", "no_opinion")
DAY_S = 86400
HORIZON_MAX = max(HORIZONS)
OUTCOME_FIELDS = tuple(f"ret_{h}d" for h in HORIZONS) + ("mfe_pct", "mae_pct", "first_touch", "bars_seen", "complete")


def classify(opinion: Any, would_reject: Any, enforced: Any) -> str:
    if opinion != "ok":
        return "no_opinion"
    if would_reject:
        return "rejected_enforced" if enforced else "rejected_shadow"
    return "passed"


def _num(v: Any) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if finite(x) else None


def new_observation(symbol: str, source: str, ts: float, price: Any, verdict: str, stop: Any = None, target: Any = None, atr: Any = None,
                    mode: Optional[str] = None, gate_mode: Optional[str] = None, reasons: Optional[Sequence[str]] = None,
                    summary: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """The row to insert. None when there is nothing to track (no symbol / no usable price). `summary` is trimmed to a few ms_* keys."""
    sym = str(symbol or "").strip().upper()
    px = _num(price)
    if not sym or px is None or px <= 0 or verdict not in VERDICTS:
        return None
    keep = ("ms_quality", "ms_trend", "ms_tqs", "ms_loc_score", "ms_rr", "ms_chase_atr", "ms_stretch_atr", "ms_room_atr", "ms_bos_dir",
            "ms_bos_bars", "ms_rr_flags")
    sm = {k: summary.get(k) for k in keep if isinstance(summary, dict) and summary.get(k) is not None}
    row: Dict[str, Any] = {
        "symbol": sym, "source": source, "mode": (str(mode).upper() if mode else None), "obs_day": ist_date(ts), "obs_ts": float(ts),
        "price": round(px, 4), "stop": _num(stop), "target": _num(target), "atr": _num(atr), "verdict": verdict, "gate_mode": gate_mode,
        "reasons_json": json.dumps(list(reasons or [])[:8], separators=(",", ":")),
        "summary_json": json.dumps(sm, separators=(",", ":"), default=str)[:1500],
        "evaluated_at": None,
    }
    row.update({k: None for k in OUTCOME_FIELDS})
    row["bars_seen"], row["complete"] = 0, False
    return row


def due(row: Dict[str, Any], now_ts: float) -> Dict[str, Any]:
    """{due, reason, lookback_days}. Done once T+20 exists; nothing to fetch until at least one session after the observation day has
    closed (the evaluator runs after the close). lookback_days = calendar days of daily history that cover the observation + 20 sessions."""
    if row.get("complete"):
        return {"due": False, "reason": "complete", "lookback_days": 0}
    obs_k = day_key(float(row.get("obs_ts") or 0))
    now_k = day_key(float(now_ts))
    if now_k <= obs_k:
        return {"due": False, "reason": "same_day", "lookback_days": 0}
    age = now_k - obs_k
    return {"due": True, "reason": "pending", "lookback_days": min(120, age + 10)}


def bars_after(bars: Sequence[Sequence[float]], obs_ts: float) -> List[Sequence[float]]:
    """Daily bars strictly after the observation's IST day, ascending."""
    k = day_key(obs_ts)
    return [b for b in sorted(bars, key=lambda x: x[T]) if day_key(b[T]) > k]


def evaluate_row(row: Dict[str, Any], bars_after_obs: Sequence[Sequence[float]]) -> Dict[str, Any]:
    """Outcome fields (OUTCOME_FIELDS) for the row from the bars after its day. Nothing but those keys is returned, so the caller can
    `setattr` them straight onto the table row. `ambiguous` (stop and target in one bar) is folded into first_touch = 'stop'."""
    o = forward_outcome(row.get("price"), row.get("stop"), row.get("target"), bars_after_obs)
    return {k: o.get(k) for k in OUTCOME_FIELDS}


def _stats(rows: List[Dict[str, Any]], h: int) -> Dict[str, Any]:
    rets = [float(r[f"ret_{h}d"]) for r in rows if finite(r.get(f"ret_{h}d"))]
    if not rets:
        return {"n": 0, "mean": None, "median": None, "win_pct": None}
    return {"n": len(rets), "mean": round(mean(rets), 3), "median": round(median(rets), 3),
            "win_pct": round(100.0 * sum(1 for x in rets if x > 0) / len(rets), 1)}


def aggregate(rows: Iterable[Dict[str, Any]], min_n: int = 20, edge_pct: float = 0.5) -> Dict[str, Any]:
    """Per-verdict counts and forward stats at every horizon, MFE / MAE means, target-vs-stop first-touch rates, and the gate edge:
    edge[h] = mean return of PASSED minus mean return of REJECTED (enforced + shadow). Positive = the gate turned away the weaker ones.
    conclusion: insufficient_sample | gate_helps | gate_hurts | inconclusive, judged on the longest horizon with >= min_n on both sides."""
    rs = [r for r in rows if isinstance(r, dict)]
    out: Dict[str, Any] = {"total": len(rs), "evaluated": sum(1 for r in rs if (r.get("bars_seen") or 0) > 0), "complete": sum(1 for r in rs if r.get("complete")),
                           "by_verdict": {}, "edge": {}, "conclusion": "insufficient_sample", "conclusion_horizon": None}
    groups = {v: [r for r in rs if r.get("verdict") == v] for v in VERDICTS}
    groups["rejected"] = groups["rejected_enforced"] + groups["rejected_shadow"]
    for v, g in groups.items():
        ev = [r for r in g if (r.get("bars_seen") or 0) > 0]
        touch = [r.get("first_touch") for r in ev if r.get("first_touch")]
        mf = [float(r["mfe_pct"]) for r in ev if finite(r.get("mfe_pct"))]
        ma = [float(r["mae_pct"]) for r in ev if finite(r.get("mae_pct"))]
        out["by_verdict"][v] = {"n": len(g), "evaluated": len(ev), "horizons": {f"{h}d": _stats(g, h) for h in HORIZONS},
                                "mfe_mean": round(mean(mf), 3) if mf else None, "mae_mean": round(mean(ma), 3) if ma else None,
                                "target_first": sum(1 for t in touch if t == "target"), "stop_first": sum(1 for t in touch if t == "stop"),
                                "untouched": len(ev) - len(touch)}
    best = None
    for h in HORIZONS:
        a, b = out["by_verdict"]["rejected"]["horizons"][f"{h}d"], out["by_verdict"]["passed"]["horizons"][f"{h}d"]
        if a["mean"] is not None and b["mean"] is not None:
            out["edge"][f"{h}d"] = {"edge": round(b["mean"] - a["mean"], 3), "rejected_n": a["n"], "passed_n": b["n"]}
            if a["n"] >= min_n and b["n"] >= min_n:
                best = (h, b["mean"] - a["mean"])
    if best is not None:
        out["conclusion_horizon"] = f"{best[0]}d"
        out["conclusion"] = "gate_helps" if best[1] > edge_pct else "gate_hurts" if best[1] < -edge_pct else "inconclusive"
    return out
