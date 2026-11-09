"""stockky_ms.observation - track what the structure gate saw and what the stock did next (WS-6). Pure; the services own the tables.

An observation is one gate verdict on one symbol on one day: the price then, the structural stop / target, the verdict
(rejected_enforced | passed | no_opinion; the gates always enforce, so every rejection is a real one) and, later, the forward outcome (T+1 / 5 / 10 / 20 trading days, MFE, MAE and the
first touch of target vs stop). Rejected stocks are tracked exactly like the ones that passed, so the only fair question can be asked:
"did what the gate turned away really do worse than what it let through?"

  classify(...)           the verdict string
  new_observation(...)    the row to insert (no outcome yet)
  due(row, now_ts)        whether (and how much history) the row still needs - a row is done once T+20 exists
  evaluate_row(row, bars_after)   outcome fields from the daily bars AFTER the observation day
  bench_outcome(obs_ts, bars)     group402: the market (Nifty) return over the same sessions, so a pick can be judged against the market
  aggregate(rows)         per-verdict statistics + the gate edge, for the status endpoints and the dashboard
  calibrate(rows)         group362: from the OBSERVED outcomes, does each gate rule earn its place, does the score rank outcomes, and would a
                          different threshold have held up out-of-sample? Advisory only - nothing is ever applied automatically.
"""
from __future__ import annotations

import json
from statistics import mean, median
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .backtest import DEFAULT_THRESHOLDS, HORIZONS, RULES, calibration, forward_outcome, rule_effects, walk_forward
from .candles import day_key, ist_date
from .ms_types import C, H, L, T, finite

VERDICTS = ("rejected_enforced", "passed", "no_opinion")
_LEGACY_REJECTED = "rejected_shadow"   # rows written before group370; counted as rejected_enforced when read
BENCH_FIELDS = tuple(f"bench_ret_{h}d" for h in HORIZONS)    # group402: market return over the same sessions as ret_{h}d
DAY_S = 86400
HORIZON_MAX = max(HORIZONS)
OUTCOME_FIELDS = tuple(f"ret_{h}d" for h in HORIZONS) + ("mfe_pct", "mae_pct", "first_touch", "bars_seen", "complete")


def classify(opinion: Any, would_reject: Any, enforced: Any) -> str:
    if opinion != "ok":
        return "no_opinion"
    if would_reject:
        return "rejected_enforced"        # `enforced` kept for call compatibility: there is no record-only mode any more
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


def bench_outcome(obs_ts: Any, bench_bars: Sequence[Sequence[float]]) -> Dict[str, Optional[float]]:
    """group402: BENCH_FIELDS for one observation from the benchmark's daily bars. Same basis as ret_{h}d (close of the h-th session after the
    observation day), measured from the benchmark's close ON the observation day (the stock's entry price is intraday and the benchmark's
    intraday level was not stored, so this is an approximation of a few tenths of a percent on a normal day). Every field is None when the
    benchmark has no bar on the observation day or no session after it: unknown is never 0. Never raises."""
    out: Dict[str, Optional[float]] = {k: None for k in BENCH_FIELDS}
    try:
        ts = _num(obs_ts)
        if ts is None or not bench_bars:
            return out
        k = day_key(ts)
        base = next((float(b[C]) for b in sorted(bench_bars, key=lambda x: x[T]) if day_key(b[T]) == k and finite(b[C]) and float(b[C]) > 0), None)
        if base is None:
            return out
        after = bars_after(bench_bars, ts)
        for h in HORIZONS:
            if len(after) >= h and finite(after[h - 1][C]):
                out[f"bench_ret_{h}d"] = round((float(after[h - 1][C]) / base - 1.0) * 100.0, 3)
        return out
    except Exception:  # noqa: BLE001
        return {k: None for k in BENCH_FIELDS}


def _excess(rows: List[Dict[str, Any]], h: int) -> Dict[str, Any]:
    """group402: return minus the market's return at horizon h, over rows that have both. Rows without a benchmark value are left out, never counted as 0."""
    ex = [float(r[f"ret_{h}d"]) - float(r[f"bench_ret_{h}d"]) for r in rows if finite(r.get(f"ret_{h}d")) and finite(r.get(f"bench_ret_{h}d"))]
    if not ex:
        return {"n": 0, "mean": None, "win_pct": None}
    return {"n": len(ex), "mean": round(mean(ex), 3), "win_pct": round(100.0 * sum(1 for x in ex if x > 0) / len(ex), 1)}


def _stats(rows: List[Dict[str, Any]], h: int) -> Dict[str, Any]:
    rets = [float(r[f"ret_{h}d"]) for r in rows if finite(r.get(f"ret_{h}d"))]
    if not rets:
        return {"n": 0, "mean": None, "median": None, "win_pct": None}
    return {"n": len(rets), "mean": round(mean(rets), 3), "median": round(median(rets), 3),
            "win_pct": round(100.0 * sum(1 for x in rets if x > 0) / len(rets), 1)}


def aggregate(rows: Iterable[Dict[str, Any]], min_n: int = 20, edge_pct: float = 0.5) -> Dict[str, Any]:
    """Per-verdict counts and forward stats at every horizon, MFE / MAE means, target-vs-stop first-touch rates, and the gate edge:
    edge[h] = mean return of PASSED minus mean return of REJECTED (enforced; legacy shadow rows included). Positive = the gate turned away the weaker ones.
    conclusion: insufficient_sample | gate_helps | gate_hurts | inconclusive, judged on the longest horizon with >= min_n on both sides."""
    rs = [r for r in rows if isinstance(r, dict)]
    out: Dict[str, Any] = {"total": len(rs), "evaluated": sum(1 for r in rs if (r.get("bars_seen") or 0) > 0), "complete": sum(1 for r in rs if r.get("complete")),
                           "by_verdict": {}, "edge": {}, "conclusion": "insufficient_sample", "conclusion_horizon": None}
    groups = {v: [r for r in rs if r.get("verdict") == v or (v == "rejected_enforced" and r.get("verdict") == _LEGACY_REJECTED)] for v in VERDICTS}
    groups["rejected"] = list(groups["rejected_enforced"])
    for v, g in groups.items():
        ev = [r for r in g if (r.get("bars_seen") or 0) > 0]
        touch = [r.get("first_touch") for r in ev if r.get("first_touch")]
        mf = [float(r["mfe_pct"]) for r in ev if finite(r.get("mfe_pct"))]
        ma = [float(r["mae_pct"]) for r in ev if finite(r.get("mae_pct"))]
        out["by_verdict"][v] = {"n": len(g), "evaluated": len(ev), "horizons": {f"{h}d": _stats(g, h) for h in HORIZONS},
                                "mfe_mean": round(mean(mf), 3) if mf else None, "mae_mean": round(mean(ma), 3) if ma else None,
                                "target_first": sum(1 for t in touch if t == "target"), "stop_first": sum(1 for t in touch if t == "stop"),
                                "untouched": len(ev) - len(touch),
                                "excess": {f"{h}d": _excess(g, h) for h in HORIZONS}}
    out["edge_excess"] = {}      # group402: the same edge after taking the market out (market up = both sides up)
    for h in HORIZONS:
        xa, xb = out["by_verdict"]["rejected"]["excess"][f"{h}d"], out["by_verdict"]["passed"]["excess"][f"{h}d"]
        if xa["mean"] is not None and xb["mean"] is not None:
            out["edge_excess"][f"{h}d"] = {"edge": round(xb["mean"] - xa["mean"], 3), "rejected_n": xa["n"], "passed_n": xb["n"]}
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


# ---- group362: calibration from observed outcomes -------------------------------------------------------------------------------------
# Grids are the thresholds worth trying around the shipped defaults (backtest.DEFAULT_THRESHOLDS). stop_wide / bearish are on-off rules with
# no threshold to tune, so they only get the helps / hurts verdict.
CAL_GRIDS: Dict[str, List[float]] = {"chase": [1.0, 1.5, 2.0, 2.5], "stretch": [2.0, 3.0, 4.0], "room": [0.25, 0.5, 1.0], "rr": [1.0, 1.2, 1.5, 2.0]}
_TH_KEY = {"chase": "chase_atr", "stretch": "stretch_atr", "room": "room_atr", "rr": "rr"}
_ADVICE = {"helps": "keep", "hurts": "review_loosen", "inconclusive": "no_evidence", "insufficient_sample": "collect_more"}


def _summary_of(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    sm = row.get("summary")
    if not isinstance(sm, dict):
        raw = row.get("summary_json")
        try:
            sm = json.loads(raw) if isinstance(raw, str) and raw else None
        except (TypeError, ValueError):
            sm = None
    return sm if isinstance(sm, dict) and sm else None


def samples_from_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Observation rows -> the sample shape backtest.* expects ({ts, summary, outcome}). Rows without a stored summary are dropped."""
    out: List[Dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        sm = _summary_of(r)
        ts = _num(r.get("obs_ts"))
        if sm is None or ts is None:
            continue
        out.append({"ts": ts, "summary": sm, "outcome": {f"ret_{h}d": r.get(f"ret_{h}d") for h in HORIZONS}})
    return out


def calibrate(rows: Iterable[Dict[str, Any]], thresholds: Optional[Dict[str, Any]] = None, horizon: Optional[int] = None, min_n: int = 20,
              folds: int = 4) -> Dict[str, Any]:
    """Advisory calibration from the observed outcomes. `thresholds` = the gate's own thresholds (defaults: backtest.DEFAULT_THRESHOLDS).
    horizon None = the longest horizon (T+5 / T+10 / T+20) that has at least `min_n` finished rows, else T+5 so the report shows how far off it is.
    Per rule: its edge (passed mean minus rejected mean) with `advice` keep | review_loosen | no_evidence | collect_more, and for the four
    threshold rules the out-of-sample walk-forward: `suggested` is set only when the folds are stable, the pooled out-of-sample edge clears
    0.25 and the best threshold differs from the shipped one. Plus the T+h bucket table of `ms_tqs` (does a higher score rank outcomes?).
    Observations are one row per symbol per day and neighbouring days overlap in time, so treat edges as indications, not proof."""
    samples = samples_from_rows(rows)
    th = dict(DEFAULT_THRESHOLDS)
    th.update(thresholds or {})
    have = {h: sum(1 for s in samples if finite(s["outcome"].get(f"ret_{h}d"))) for h in HORIZONS if h >= 5}
    h = int(horizon) if horizon in HORIZONS else next((x for x in sorted(have, reverse=True) if have[x] >= min_n), 5)
    out: Dict[str, Any] = {"advisory": True, "horizon": h, "min_n": min_n, "samples": len(samples), "finished": have.get(h, 0), "thresholds": th,
                           "rules": {}, "score": None, "suggestions": [], "note": None}
    if have.get(h, 0) < min_n:
        out["note"] = "insufficient_sample"
    eff = rule_effects(samples, h, th, min_n=min_n)
    out["all"] = eff["all"]
    out["combined"] = eff["combined"]
    for rule in RULES:
        e = eff["rules"][rule]
        item: Dict[str, Any] = {"rejected": e["rejected"], "passed": e["passed"], "edge": e["edge"], "verdict": e["verdict"], "advice": _ADVICE.get(e["verdict"], "no_evidence"),
                                "current": th.get(_TH_KEY.get(rule, rule)), "walk_forward": None, "suggested": None}
        grid = CAL_GRIDS.get(rule)
        if grid and have.get(h, 0) >= 44:
            wf = walk_forward(samples, rule, grid, folds=folds, horizon=h, min_n=max(5, min_n // 2))
            item["walk_forward"] = {k: wf.get(k) for k in ("oos_edge", "oos_rejected_n", "oos_passed_n", "stable", "folds", "note")}
            picks = [f["threshold"] for f in wf.get("folds", []) if f.get("threshold") is not None]
            if wf.get("stable") and (wf.get("oos_edge") or 0) > 0.25 and picks:
                best = max(set(picks), key=picks.count)
                if best != item["current"]:
                    item["suggested"] = best
                    out["suggestions"].append({"rule": rule, "from": item["current"], "to": best, "oos_edge": wf.get("oos_edge")})
        out["rules"][rule] = item
    out["score"] = calibration(samples, "ms_tqs", bins=3, horizon=h, min_bin_n=max(5, min_n // 2))
    return out
