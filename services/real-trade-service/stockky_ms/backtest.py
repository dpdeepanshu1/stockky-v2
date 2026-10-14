"""stockky_ms.backtest - did the structure rules actually help? (WS-7). Pure; no I/O, no clock, no env.

    forward_outcome(entry, stop, target, bars_after)   what the price did after an entry (T+1/5/10/20 returns, MFE, MAE, first touch)
    rule_flags(summary, thresholds)                    which gate rules a flat ms_* summary trips (same rules as the services' gates)
    build_samples(bars, params, step, ...)             replay a history -> [{i, ts, summary, outcome}] with no lookahead
    rule_effects(samples, horizon, thresholds)         per rule: the stocks it would have rejected vs the ones it lets through
    walk_forward(samples, rule, grid, ...)             pick a threshold on the past, test it on the next block, repeat (out-of-sample only)
    calibration(samples, score_key, bins, horizon)     does a higher score really mean a better forward return? (bucket table + monotonic flag)

Conventions: a long entry at `entry`; bars_after are the bars STRICTLY after the entry day, ascending. T+h is the close of the h-th bar after.
When a bar touches both stop and target the stop is assumed first (conservative) and flagged `ambiguous`. Returns are in percent.
"""
from __future__ import annotations

from statistics import mean, median
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .ms_types import C, H, L, finite
from .params import MSParams
from .replay import replay

HORIZONS = (1, 5, 10, 20)
HORIZON_MAX = max(HORIZONS)

# same defaults as real-trade's entry gate (config.STRUCTURE_*); callers pass their own to evaluate their own gate
DEFAULT_THRESHOLDS: Dict[str, Any] = {"chase_atr": 1.5, "stretch_atr": 3.0, "room_atr": 0.5, "rr": 1.2, "stop_wide": True,
                                      "bearish": True, "bearish_bos_bars": 5}
RULES = ("chase", "stretch", "room", "rr", "stop_wide", "bearish")


def _f(v: Any) -> Optional[float]:
    return float(v) if finite(v) else None


def forward_outcome(entry: Any, stop: Any, target: Any, bars_after: Sequence[Sequence[float]]) -> Dict[str, Any]:
    """Forward behaviour of a long entry. Keys: ret_1d/5d/10d/20d (None until that many bars exist), mfe_pct, mae_pct (over the first
    HORIZON_MAX bars; mae <= 0), first_touch ('target'|'stop'|None), ambiguous (same bar touched both), touch_bars, bars_seen, complete."""
    e, s, t = _f(entry), _f(stop), _f(target)
    out: Dict[str, Any] = {f"ret_{h}d": None for h in HORIZONS}
    out.update({"mfe_pct": None, "mae_pct": None, "first_touch": None, "ambiguous": False, "touch_bars": None, "bars_seen": 0, "complete": False})
    if e is None or e <= 0:
        return out
    win = [b for b in (bars_after or [])][:HORIZON_MAX]
    out["bars_seen"] = len(win)
    out["complete"] = len(win) >= HORIZON_MAX
    if not win:
        return out
    for h in HORIZONS:
        if len(win) >= h and finite(win[h - 1][C]):
            out[f"ret_{h}d"] = round((float(win[h - 1][C]) / e - 1.0) * 100.0, 3)
    hi = max(float(b[H]) for b in win if finite(b[H]))
    lo = min(float(b[L]) for b in win if finite(b[L]))
    out["mfe_pct"] = round((hi / e - 1.0) * 100.0, 3)
    out["mae_pct"] = round(min(0.0, (lo / e - 1.0) * 100.0), 3)
    for k, b in enumerate(win):
        hit_t = t is not None and t > e and finite(b[H]) and float(b[H]) >= t
        hit_s = s is not None and 0 < s < e and finite(b[L]) and float(b[L]) <= s
        if hit_s or hit_t:
            out["first_touch"] = "stop" if hit_s else "target"
            out["ambiguous"] = bool(hit_s and hit_t)
            out["touch_bars"] = k + 1
            break
    return out


def rule_flags(summary: Optional[Dict[str, Any]], th: Optional[Dict[str, Any]] = None) -> Dict[str, bool]:
    """Which rules the flat ms_* summary trips (at the structure close - no live adjustment). A missing metric never trips a rule."""
    t = dict(DEFAULT_THRESHOLDS)
    t.update(th or {})
    s = summary or {}
    chase, stretch, room, rr = _f(s.get("ms_chase_atr")), _f(s.get("ms_stretch_atr")), _f(s.get("ms_room_atr")), _f(s.get("ms_rr"))
    bars = _f(s.get("ms_bos_bars"))
    flags = s.get("ms_rr_flags") or []
    return {
        "chase": bool(t["chase_atr"] and chase is not None and chase > t["chase_atr"]),
        "stretch": bool(t["stretch_atr"] and stretch is not None and stretch > t["stretch_atr"]),
        "room": bool(t["room_atr"] and room is not None and room < t["room_atr"]),
        "rr": bool(t["rr"] and rr is not None and rr < t["rr"]),
        "stop_wide": bool(t["stop_wide"] and "stop_too_wide" in flags),
        "bearish": bool(t["bearish"] and (s.get("ms_trend") == "bearish" or (
            s.get("ms_bos_dir") == "bearish" and not s.get("ms_bos_failed") and bars is not None and bars <= t["bearish_bos_bars"]))),
    }


def build_samples(bars: Sequence[Sequence[float]], params: Optional[MSParams] = None, step: int = 1, warmup: Optional[int] = None,
                  only_quality: Sequence[str] = ("ok", "limited")) -> List[Dict[str, Any]]:
    """Replay `bars` and attach the forward outcome of buying at each replayed close with the engine's own structural stop / target.
    The last HORIZON_MAX replayed days have partial outcomes (complete=False) - callers filter on that when they need full ones."""
    out: List[Dict[str, Any]] = []
    for r in replay(bars, params=params, warmup=warmup, step=step):
        if r["quality"] not in only_quality:
            continue
        sm = r["summary"]
        out.append({"i": r["i"], "ts": r["ts"], "price": r["price"], "summary": sm,
                    "outcome": forward_outcome(r["price"], sm.get("ms_stop"), sm.get("ms_target"), bars[r["i"] + 1:])})
    return out


def _stats(rets: List[float]) -> Dict[str, Any]:
    if not rets:
        return {"n": 0, "mean": None, "median": None, "win_pct": None}
    return {"n": len(rets), "mean": round(mean(rets), 3), "median": round(median(rets), 3),
            "win_pct": round(100.0 * sum(1 for x in rets if x > 0) / len(rets), 1)}


def _ret(sample: Dict[str, Any], horizon: int) -> Optional[float]:
    v = (sample.get("outcome") or {}).get(f"ret_{horizon}d")
    return float(v) if finite(v) else None


def rule_effects(samples: Iterable[Dict[str, Any]], horizon: int = 10, thresholds: Optional[Dict[str, Any]] = None,
                 min_n: int = 20) -> Dict[str, Any]:
    """For every rule: forward stats of the samples it WOULD reject vs the samples it lets through, and edge = passed_mean - rejected_mean
    (positive = the rule rejected the worse entries). `verdict` is 'insufficient_sample' below min_n on either side."""
    rows = [(rule_flags(s.get("summary"), thresholds), _ret(s, horizon)) for s in samples]
    rows = [(f, r) for f, r in rows if r is not None]
    out: Dict[str, Any] = {"horizon": horizon, "n": len(rows), "all": _stats([r for _, r in rows]), "rules": {}}
    for rule in RULES:
        rej = [r for f, r in rows if f[rule]]
        ok = [r for f, r in rows if not f[rule]]
        a, b = _stats(rej), _stats(ok)
        edge = round(b["mean"] - a["mean"], 3) if a["mean"] is not None and b["mean"] is not None else None
        if a["n"] < min_n or b["n"] < min_n:
            verdict = "insufficient_sample"
        else:
            verdict = "helps" if edge > 0.25 else "hurts" if edge < -0.25 else "inconclusive"
        out["rules"][rule] = {"rejected": a, "passed": b, "edge": edge, "verdict": verdict}
    anyf = [r for f, r in rows if any(f.values())]
    none = [r for f, r in rows if not any(f.values())]
    a, b = _stats(anyf), _stats(none)
    out["combined"] = {"rejected": a, "passed": b, "edge": round(b["mean"] - a["mean"], 3) if a["mean"] is not None and b["mean"] is not None else None}
    return out


def walk_forward(samples: Sequence[Dict[str, Any]], rule: str, grid: Sequence[float], folds: int = 4, horizon: int = 10,
                 min_train: int = 40, min_n: int = 10) -> Dict[str, Any]:
    """Expanding-window walk-forward for one rule threshold. Samples are sorted by time and cut into folds+1 blocks; for each fold the
    threshold with the best edge on ALL earlier blocks is chosen, then its edge is measured on the next block only (out-of-sample).
    rule in {chase, stretch, room, rr}; `grid` = candidate thresholds. Returns the per-fold picks and the pooled out-of-sample edge."""
    key = {"chase": "chase_atr", "stretch": "stretch_atr", "room": "room_atr", "rr": "rr"}.get(rule)
    ss = sorted([s for s in samples if _ret(s, horizon) is not None], key=lambda s: s["ts"])
    out: Dict[str, Any] = {"rule": rule, "horizon": horizon, "folds": [], "oos_edge": None, "oos_rejected_n": 0, "oos_passed_n": 0, "stable": None}
    if key is None or not grid or len(ss) < min_train + folds:
        out["note"] = "insufficient_sample" if key else "unknown_rule"
        return out
    k = max(2, int(folds)) + 1
    size = len(ss) // k
    pooled_rej: List[float] = []
    pooled_ok: List[float] = []
    for f in range(1, k):
        train, test = ss[: f * size], ss[f * size: (f + 1) * size if f < k - 1 else len(ss)]
        if len(train) < min_train:
            continue
        best_v, best_edge = None, None
        for v in grid:
            eff = rule_effects(train, horizon, {key: v}, min_n=min_n)["rules"][rule]
            if eff["edge"] is not None and eff["rejected"]["n"] >= min_n and (best_edge is None or eff["edge"] > best_edge):
                best_v, best_edge = v, eff["edge"]
        if best_v is None:
            continue
        rows = [(rule_flags(s.get("summary"), {key: best_v})[rule], _ret(s, horizon)) for s in test]
        rej, ok = [r for fl, r in rows if fl], [r for fl, r in rows if not fl]
        edge = round(mean(ok) - mean(rej), 3) if rej and ok else None
        pooled_rej += rej
        pooled_ok += ok
        out["folds"].append({"fold": f, "threshold": best_v, "train_edge": best_edge, "test_rejected_n": len(rej), "test_passed_n": len(ok), "test_edge": edge})
    if pooled_rej and pooled_ok:
        out["oos_edge"] = round(mean(pooled_ok) - mean(pooled_rej), 3)
    out["oos_rejected_n"], out["oos_passed_n"] = len(pooled_rej), len(pooled_ok)
    edges = [x["test_edge"] for x in out["folds"] if x["test_edge"] is not None]
    out["stable"] = (len(edges) >= 2 and all(e > 0 for e in edges)) if edges else None
    return out


def calibration(samples: Iterable[Dict[str, Any]], score_key: str = "ms_tqs", bins: int = 5, horizon: int = 10, min_bin_n: int = 10) -> Dict[str, Any]:
    """Equal-count buckets of `score_key` (low -> high) with the forward stats of each. `monotonic` is True when the mean forward return
    never falls from one bucket to the next (a score that does not rank outcomes is not worth a gate). None below min_bin_n per bucket."""
    pts = sorted([(float(s["summary"][score_key]), _ret(s, horizon)) for s in samples
                  if isinstance(s.get("summary"), dict) and finite(s["summary"].get(score_key)) and _ret(s, horizon) is not None])
    out: Dict[str, Any] = {"score": score_key, "horizon": horizon, "n": len(pts), "bins": [], "monotonic": None, "spread": None}
    nb = max(2, int(bins))
    if len(pts) < nb * min_bin_n:
        out["note"] = "insufficient_sample"
        return out
    size = len(pts) // nb
    for b in range(nb):
        chunk = pts[b * size: (b + 1) * size if b < nb - 1 else len(pts)]
        st = _stats([r for _, r in chunk])
        out["bins"].append({"lo": round(chunk[0][0], 3), "hi": round(chunk[-1][0], 3), **st})
    means = [x["mean"] for x in out["bins"]]
    out["monotonic"] = all(means[i + 1] >= means[i] for i in range(len(means) - 1))
    out["spread"] = round(means[-1] - means[0], 3)
    return out
