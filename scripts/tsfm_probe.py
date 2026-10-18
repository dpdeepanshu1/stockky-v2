#!/usr/bin/env python3
"""tsfm_probe.py - Phase 1 of the TimesFM plan (group 434): does a forecast model predict the 5-day RANGE and the 5-day VOLUME of a stock better than
the econometric baselines, out of sample? That is Gate A. Stdlib only; the model is reached over HTTP (timesfm-forecast-service POST /forecast).

  python3 scripts/tsfm_probe.py HIST_DIR [--out DIR] [--seed 7] [--per-symbol 40] [--horizon 5] [--tsfm-url http://localhost:8011] [--no-model]
        [--holdout-months 6] [--holdout-only] [--boot 2000] [--max-forecasts 0] [--min-context 300] [--min-cal 300]
  python3 scripts/tsfm_probe.py --combine out_7/probe_report.json out_11/probe_report.json      # Gate A across seeds

HIST_DIR is the folder scripts/fetch_history_for_backtest.py writes (one JSON per symbol). With --no-model only the baselines run (a useful first
look: does HAR beat ATR at all?). Run once per seed, then --combine.

METHOD (nothing here looks ahead):
  decision   per symbol, --per-symbol random days t (seeded); the forecast sees bars up to and including t only.
  targets    range = (max high - min low over t+1..t+h) / close_t in %;   volume = mean volume over t+1..t+h.
  predictors each gives a POINT m_t on its own scale: ATR(14) %, EWMA(0.94) of daily range %, HAR (OLS on 1/5/22-day mean daily range, refit per
             decision on targets already known at t); model = the forecast fan's envelope width (range) / forecast volume (volume).
             volume baselines: trailing 20-day mean, EWMA(0.9) of volume.
  quantiles  every predictor is turned into q10/q50/q90 the SAME way: m_t x the quantiles of past outcome/prediction ratios, pooled over decisions whose
             outcome was already known (embargo of 2h calendar days). So the comparison is about information in m_t, not about scale.
  loss       mean pinball loss over q10/q50/q90. The model is compared with the BEST baseline on the same decisions (a harsh choice, on purpose).
  interval   bootstrap over decision DATES (all stocks share a market day, so rows of one day are one draw); 90% interval of mean(loss_base - loss_model).
  holdout    the last --holdout-months of the data are NOT scored (frozen); --holdout-only scores only that part, once, after Gate A passed.
Verdict per comparison: pass = mean improvement > 0 with the interval's lower end > 0 AND positive in both history halves; hurts = upper end < 0;
insufficient = too few decisions; no_edge otherwise. GATE A = pass on the RANGE target in every report and at least 2 reports (seeds).
What it would fix: range -> stop / target width; volume -> the volume-pace gate. Stop-touch probability is not scored here (needs intraday paths).
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import random
import sys
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

TAUS = (0.1, 0.5, 0.9)
BENCH = ("^NSEI", "NIFTY", "NSEI", "NIFTY50")
PASS, NO_EDGE, HURTS, INSUFFICIENT = "pass", "no_edge", "hurts", "insufficient"
RANGE_BASELINES, VOLUME_BASELINES = ("atr", "ewma", "har"), ("vol20", "ewmav")


# ── data ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def load_symbol(path: Path) -> List[Dict[str, Any]]:
    doc = json.loads(path.read_text())
    rows = doc.get("candles") if isinstance(doc, dict) else doc
    out = []
    for r in rows or []:
        try:
            lo = {str(k).lower(): v for k, v in r.items()}
            out.append({"date": str(lo["date"])[:10], "open": float(lo["open"]), "high": float(lo["high"]), "low": float(lo["low"]),
                        "close": float(lo["close"]), "volume": float(lo.get("volume") or 0.0)})
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    out = [r for r in out if r["close"] > 0 and r["high"] >= r["low"] > 0]
    out.sort(key=lambda r: r["date"])
    dedup: Dict[str, Dict[str, Any]] = {r["date"]: r for r in out}
    return [dedup[k] for k in sorted(dedup)]


def load_dir(d: Path) -> Dict[str, List[Dict[str, Any]]]:
    return {f.stem.upper(): load_symbol(f) for f in sorted(d.glob("*.json")) if f.stem.upper() not in BENCH}


def _ord(s: str) -> int:
    return date.fromisoformat(s).toordinal()


# ── per-symbol feature arrays (value at index k uses bars <= k only) ─────────────────────────────────────────────────────────────────
class SymbolSeries:
    def __init__(self, bars: List[Dict[str, Any]], h: int):
        n = len(bars)
        self.bars, self.h, self.n = bars, h, n
        c = [b["close"] for b in bars]
        self.rng = [(b["high"] - b["low"]) / b["close"] * 100.0 for b in bars]                      # daily range %
        tr = [bars[0]["high"] - bars[0]["low"]] + [max(bars[i]["high"] - bars[i]["low"], abs(bars[i]["high"] - c[i - 1]), abs(bars[i]["low"] - c[i - 1])) for i in range(1, n)]
        pre = [0.0]
        for x in tr:
            pre.append(pre[-1] + x)
        self.atr = [((pre[k + 1] - pre[k - 13]) / 14.0) / c[k] * 100.0 if k >= 14 else None for k in range(n)]
        self.ewma, e = [], None
        for x in self.rng:
            e = x if e is None else 0.94 * e + 0.06 * x
            self.ewma.append(e)
        self.ewmav, ev = [], None
        for b in bars:
            ev = b["volume"] if ev is None else 0.9 * ev + 0.1 * b["volume"]
            self.ewmav.append(ev)
        vp = [0.0]
        for b in bars:
            vp.append(vp[-1] + b["volume"])
        self.vol20 = [(vp[k + 1] - vp[k - 19]) / 20.0 if k >= 19 else None for k in range(n)]
        rp = [0.0]
        for x in self.rng:
            rp.append(rp[-1] + x)
        self.w = [(rp[k + 1] - rp[k - 4]) / 5.0 if k >= 4 else None for k in range(n)]
        self.m = [(rp[k + 1] - rp[k - 21]) / 22.0 if k >= 21 else None for k in range(n)]
        # forward targets; entry k is only used by a decision at t >= k + h
        self.y_range: List[Optional[float]] = [None] * n
        self.y_vol: List[Optional[float]] = [None] * n
        for k in range(n - h):
            fut = bars[k + 1:k + 1 + h]
            self.y_range[k] = (max(b["high"] for b in fut) - min(b["low"] for b in fut)) / c[k] * 100.0
            self.y_vol[k] = sum(b["volume"] for b in fut) / h

    def har_point(self, t: int, train: int = 500) -> Optional[float]:
        lo, hi = max(22, t - self.h - train), t - self.h
        if hi - lo < 60 or self.w[t] is None or self.m[t] is None:
            return None
        xs, ys = [], []
        for k in range(lo, hi + 1):
            if self.y_range[k] is not None and self.w[k] is not None and self.m[k] is not None:
                xs.append((1.0, self.rng[k], self.w[k], self.m[k]))
                ys.append(self.y_range[k])
        if len(xs) < 60:
            return None
        beta = _ols(xs, ys)
        if beta is None:
            return None
        pred = sum(b * x for b, x in zip(beta, (1.0, self.rng[t], self.w[t], self.m[t])))
        return max(pred, 0.05)


def _ols(xs: Sequence[Sequence[float]], ys: Sequence[float]) -> Optional[List[float]]:
    p = len(xs[0])
    a = [[0.0] * (p + 1) for _ in range(p)]
    for x, y in zip(xs, ys):
        for i in range(p):
            for j in range(p):
                a[i][j] += x[i] * x[j]
            a[i][p] += x[i] * y
    for i in range(p):
        a[i][i] += 1e-9
    for col in range(p):
        piv = max(range(col, p), key=lambda r: abs(a[r][col]))
        if abs(a[piv][col]) < 1e-12:
            return None
        a[col], a[piv] = a[piv], a[col]
        for r in range(p):
            if r != col:
                f = a[r][col] / a[col][col]
                for c_ in range(col, p + 1):
                    a[r][c_] -= f * a[col][c_]
    return [a[i][p] / a[i][i] for i in range(p)]


# ── calibration and scoring ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def pinball(y: float, q: float, tau: float) -> float:
    d = y - q
    return max(tau * d, (tau - 1.0) * d)


def _quantile(sorted_vals: List[float], tau: float) -> float:
    n = len(sorted_vals)
    pos = tau * (n - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def score_predictor(decisions: List[Dict[str, Any]], name: str, target: str, embargo_days: int, min_cal: int) -> List[Dict[str, Any]]:
    """For one predictor: walk the decisions in date order; each is scored with ratio quantiles from decisions whose outcome was known `embargo_days` earlier.
    Returns scored rows {date, loss, in80, below50, sym}."""
    key_m, key_y = f"m_{name}", "y_range" if target == "range" else "y_vol"
    rows = sorted((d for d in decisions if d.get(key_m) not in (None, 0) and d.get(key_y) is not None and d[key_m] > 0), key=lambda d: d["ord"])
    cal: List[float] = []
    ptr = 0
    out: List[Dict[str, Any]] = []
    for d in rows:
        while ptr < len(rows) and rows[ptr]["ord"] <= d["ord"] - embargo_days:
            r = rows[ptr]
            bisect.insort(cal, r[key_y] / r[key_m])
            ptr += 1
        if len(cal) < min_cal:
            continue
        qs = [d[key_m] * _quantile(cal, t) for t in TAUS]
        y = d[key_y]
        out.append({"ord": d["ord"], "date": d["date"], "sym": d["sym"], "id": d["id"], "loss": sum(pinball(y, q, t) for q, t in zip(qs, TAUS)) / len(TAUS),
                    "in80": qs[0] <= y <= qs[2], "below50": y <= qs[1]})
    return out


def cluster_bootstrap(diffs_by_date: Dict[int, List[float]], n_boot: int, seed: int) -> Optional[List[float]]:
    dates = sorted(diffs_by_date)
    if len(dates) < 20:
        return None
    rng = random.Random(seed)
    sums = {d: (sum(v), len(v)) for d, v in diffs_by_date.items()}
    means = []
    for _ in range(n_boot):
        tot = cnt = 0
        for _i in range(len(dates)):
            s, c = sums[dates[rng.randrange(len(dates))]]
            tot += s
            cnt += c
        means.append(tot / cnt)
    means.sort()
    return [round(means[int(0.05 * (n_boot - 1))], 6), round(means[int(0.95 * (n_boot - 1))], 6)]


def compare(model_rows: List[Dict[str, Any]], base_rows: List[Dict[str, Any]], n_boot: int, seed: int, min_n: int, need_halves: bool) -> Dict[str, Any]:
    bm = {r["id"]: r for r in base_rows}
    pairs = [(m, bm[m["id"]]) for m in model_rows if m["id"] in bm]
    out: Dict[str, Any] = {"n": len(pairs), "improvement_pct": None, "mean_diff": None, "ci90": None, "halves": {"first": None, "second": None}, "verdict": INSUFFICIENT}
    if len(pairs) < min_n:
        return out
    diffs = [(b["loss"] - m["loss"], m["ord"]) for m, b in pairs]
    mean_diff = sum(d for d, _ in diffs) / len(diffs)
    mean_base = sum(b["loss"] for _, b in pairs) / len(pairs)
    by_date: Dict[int, List[float]] = {}
    for d, o in diffs:
        by_date.setdefault(o, []).append(d)
    ci = cluster_bootstrap(by_date, n_boot, seed)
    cut = sorted(o for _, o in diffs)[len(diffs) // 2]
    h1 = [d for d, o in diffs if o < cut]
    h2 = [d for d, o in diffs if o >= cut]
    halves = {"first": round(sum(h1) / len(h1), 6) if h1 else None, "second": round(sum(h2) / len(h2), 6) if h2 else None}
    out.update(mean_diff=round(mean_diff, 6), improvement_pct=round(100.0 * mean_diff / mean_base, 3) if mean_base else None, ci90=ci, halves=halves)
    if ci is None:
        out["verdict"] = INSUFFICIENT
    elif ci[1] < 0:
        out["verdict"] = HURTS
    else:
        halves_ok = (halves["first"] or 0) > 0 and (halves["second"] or 0) > 0
        out["verdict"] = PASS if mean_diff > 0 and ci[0] > 0 and (halves_ok or not need_halves) else NO_EDGE
    return out


def summarise(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not rows:
        return {"n": 0, "mean_loss": None, "coverage_80": None, "below_q50": None}
    n = len(rows)
    return {"n": n, "mean_loss": round(sum(r["loss"] for r in rows) / n, 6), "coverage_80": round(sum(r["in80"] for r in rows) / n, 4),
            "below_q50": round(sum(r["below50"] for r in rows) / n, 4)}


# ── the run ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
ModelFn = Callable[[str, List[Dict[str, Any]]], Tuple[Optional[float], Optional[float]]]      # (symbol, bars up to t) -> (range point, volume point)


def build_decisions(data: Dict[str, List[Dict[str, Any]]], h: int, per_symbol: int, seed: int, min_context: int, models: Dict[str, ModelFn],
                    max_forecasts: int = 0, log: Callable[[str], None] = lambda s: None) -> List[Dict[str, Any]]:
    decisions: List[Dict[str, Any]] = []
    forecasts = 0
    for sym, bars in sorted(data.items()):
        n = len(bars)
        if n < min_context + h + 5:
            continue
        ser = SymbolSeries(bars, h)
        eligible = list(range(min_context - 1, n - 1 - h))
        picks = sorted(random.Random(f"{seed}|{sym}").sample(eligible, min(per_symbol, len(eligible))))
        for t in picks:
            if ser.y_range[t] is None or ser.atr[t] is None or ser.vol20[t] is None or not ser.vol20[t] > 0:
                continue
            d: Dict[str, Any] = {"id": f"{sym}|{bars[t]['date']}", "sym": sym, "date": bars[t]["date"], "ord": _ord(bars[t]["date"]),
                                 "y_range": ser.y_range[t], "y_vol": ser.y_vol[t], "m_atr": ser.atr[t], "m_ewma": ser.ewma[t],
                                 "m_har": ser.har_point(t), "m_vol20": ser.vol20[t], "m_ewmav": ser.ewmav[t]}
            for name, fn in models.items():
                if max_forecasts and forecasts >= max_forecasts:
                    d[f"m_{name}"] = d[f"mv_{name}"] = None
                    continue
                try:
                    r, v = fn(sym, bars[max(0, t - 511):t + 1])
                except Exception as e:  # noqa: BLE001 - one bad forecast must not stop the probe
                    log(f"{name} failed for {sym} {bars[t]['date']}: {str(e)[:100]}")
                    r = v = None
                forecasts += 1
                d[f"m_{name}"], d[f"mv_{name}"] = r, v
            decisions.append(d)
        log(f"{sym}: {len(picks)} decisions ({forecasts} forecasts so far)")
    return decisions


def evaluate(decisions: List[Dict[str, Any]], h: int, model_names: Sequence[str], n_boot: int, seed: int, holdout_months: float, holdout_only: bool,
             min_cal: int, min_n: int = 200) -> Dict[str, Any]:
    if not decisions:
        return {"targets": {}, "gate_a": {"status": "NO_INPUT", "reasons": ["no decisions"]}, "meta": {"n_decisions": 0}}
    max_ord = max(d["ord"] for d in decisions)
    cut = max_ord - int(round(holdout_months * 30.4)) if holdout_months > 0 else max_ord + 1
    scored_set = [d for d in decisions if (d["ord"] > cut) == bool(holdout_only)]
    cal_set = decisions if holdout_only else scored_set                       # a holdout run may calibrate on everything before it
    embargo = max(2 * h, 8)
    report: Dict[str, Any] = {"meta": {"horizon": h, "n_decisions": len(decisions), "n_scored_pool": len(scored_set), "holdout_months": holdout_months,
                                       "holdout_only": holdout_only, "holdout_cut": date.fromordinal(cut).isoformat() if holdout_months > 0 else None,
                                       "embargo_days": embargo, "min_cal": min_cal, "seed": seed}, "targets": {}}
    for target, base_names in (("range", RANGE_BASELINES), ("volume", VOLUME_BASELINES)):
        t_rows: Dict[str, List[Dict[str, Any]]] = {}
        for nm in list(base_names) + list(model_names):
            if target == "volume" and nm in model_names:
                pool = [{**d, "m_" + nm: d.get("mv_" + nm)} for d in cal_set]
            else:
                pool = cal_set
            rows = score_predictor(pool, nm, target, embargo, min_cal)
            if holdout_only:
                keep = {d["id"] for d in scored_set}
                rows = [r for r in rows if r["id"] in keep]
            t_rows[nm] = rows
        common = set.intersection(*[{r["id"] for r in rows} for rows in t_rows.values()]) if t_rows else set()
        t_rows = {nm: [r for r in rows if r["id"] in common] for nm, rows in t_rows.items()}
        base_sum = {nm: summarise(t_rows[nm]) for nm in base_names}
        best = min(base_names, key=lambda nm: base_sum[nm]["mean_loss"] if base_sum[nm]["mean_loss"] is not None else float("inf"))
        info: Dict[str, Any] = {"common_n": len(common), "baselines": base_sum, "best_baseline": best if base_sum[best]["mean_loss"] is not None else None,
                                "models": {nm: summarise(t_rows[nm]) for nm in model_names}, "comparisons": {}}
        if info["best_baseline"]:
            info["baseline_vs_atr"] = {nm: compare(t_rows[nm], t_rows["atr"], n_boot, seed, min_n, not holdout_only) for nm in base_names if nm != "atr"} if target == "range" else {}
            for nm in model_names:
                info["comparisons"][nm] = compare(t_rows[nm], t_rows[best], n_boot, seed, min_n, not holdout_only)
        report["targets"][target] = info
    report["gate_a"] = gate_a_single(report, model_names)
    return report


def gate_a_single(report: Dict[str, Any], model_names: Sequence[str]) -> Dict[str, Any]:
    if not model_names:
        return {"status": "NO_INPUT", "reasons": ["no model was run (--no-model): baselines only"]}
    rng = (report.get("targets") or {}).get("range") or {}
    res: Dict[str, Any] = {}
    for nm in model_names:
        c = (rng.get("comparisons") or {}).get(nm)
        res[nm] = c["verdict"] if c else INSUFFICIENT
    status = "PASS" if all(v == PASS for v in res.values()) else "FAIL" if any(v in (HURTS, NO_EDGE) for v in res.values()) else "NOT_ENOUGH_DATA"
    return {"status": status, "range_verdict": res, "reasons": [f"{nm}: range {v} vs best baseline {rng.get('best_baseline')}" for nm, v in res.items()]}


def combine(reports: List[Dict[str, Any]], names: Optional[List[str]] = None) -> Dict[str, Any]:
    """Gate A across seeds: a model passes only if its RANGE comparison is `pass` in EVERY report and there are at least 2 reports."""
    if not reports:
        return {"gate": "A", "status": "NO_INPUT", "reasons": ["no probe report given"], "models": {}}
    models = sorted({nm for r in reports for nm in ((r.get("targets") or {}).get("range") or {}).get("comparisons", {})})
    if not models:
        return {"gate": "A", "status": "NO_INPUT", "reasons": ["the reports hold no model comparison (run without --no-model)"], "models": {}}
    out: Dict[str, Any] = {}
    reasons: List[str] = []
    for nm in models:
        per = []
        for i, r in enumerate(reports):
            c = (((r.get("targets") or {}).get("range") or {}).get("comparisons") or {}).get(nm) or {}
            vol = (((r.get("targets") or {}).get("volume") or {}).get("comparisons") or {}).get(nm) or {}
            per.append({"report": (names or [])[i] if names and i < len(names) else f"report{i + 1}", "range": c.get("verdict", INSUFFICIENT),
                        "range_improvement_pct": c.get("improvement_pct"), "range_ci90": c.get("ci90"), "halves": c.get("halves"),
                        "volume": vol.get("verdict", INSUFFICIENT), "volume_improvement_pct": vol.get("improvement_pct")})
        vs = {p["range"] for p in per}
        status = ("NOT_ENOUGH_DATA" if len(per) < 2 else "PASS" if vs == {PASS} else "FAIL" if vs & {HURTS, NO_EDGE} else "NOT_ENOUGH_DATA")
        if len(per) < 2:
            reasons.append(f"{nm}: only one report; Gate A needs at least 2 seeds")
        vol_ok = len(per) >= 2 and {p["volume"] for p in per} == {PASS}
        out[nm] = {"status": status, "per_report": per, "volume_target_pass": vol_ok}
        reasons.append(f"{nm}: range {sorted(vs)} -> {status}; volume pace input {'usable' if vol_ok else 'not shown to beat the baseline'}")
    overall = "PASS" if any(v["status"] == "PASS" for v in out.values()) else "FAIL" if any(v["status"] == "FAIL" for v in out.values()) else "NOT_ENOUGH_DATA"
    return {"gate": "A", "status": overall, "models": out, "reasons": reasons}


# ── model over HTTP ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def http_model(url: str, horizon: int, timeout: float = 120.0, cache: Optional[Dict[str, Any]] = None,
               cache_file: Optional[Path] = None) -> ModelFn:
    def fn(sym: str, bars: List[Dict[str, Any]]) -> Tuple[Optional[float], Optional[float]]:
        key = f"{sym}|{bars[-1]['date']}|{horizon}"
        if cache is not None and key in cache:
            c = cache[key]
            return c["r"], c["v"]
        body = json.dumps({"symbol": sym, "candles": bars, "horizon": horizon}).encode()
        req = urllib.request.Request(url.rstrip("/") + "/forecast", data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            j = json.loads(resp.read().decode())
        r, v = j.get("fan_range_pct"), j.get("volume_forecast")
        if cache is not None:
            cache[key] = {"r": r, "v": v}
            if cache_file is not None:
                with cache_file.open("a") as fh:
                    fh.write(json.dumps({"k": key, "r": r, "v": v}) + "\n")
        return r, v
    return fn


def load_cache(path: Path) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                j = json.loads(line)
                out[j["k"]] = {"r": j["r"], "v": j["v"]}
            except (ValueError, KeyError):
                continue
    return out


# ── output ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def to_markdown(rep: Dict[str, Any]) -> str:
    f = lambda v: "-" if v is None else str(v)  # noqa: E731
    m = rep["meta"]
    L = [f"# TimesFM probe (T+{m.get('horizon')}, seed {m.get('seed')})", "", f"Decisions {m.get('n_decisions')}, scored pool {m.get('n_scored_pool')}, "
         f"holdout cut {f(m.get('holdout_cut'))}{' (HOLDOUT ONLY)' if m.get('holdout_only') else ' (holdout not scored)'}", "",
         f"**Gate A (this report): {rep['gate_a']['status']}**"] + [f"- {x}" for x in rep["gate_a"].get("reasons", [])] + [""]
    for target, info in rep["targets"].items():
        L += [f"## {target}  (common decisions {info['common_n']}, best baseline: {f(info['best_baseline'])})", "", "| predictor | n | mean pinball | q10-q90 coverage (want 0.80) | below q50 (want 0.50) |", "|---|---|---|---|---|"]
        for nm, s in {**info["baselines"], **info["models"]}.items():
            L.append(f"| {nm} | {s['n']} | {f(s['mean_loss'])} | {f(s['coverage_80'])} | {f(s['below_q50'])} |")
        if info.get("baseline_vs_atr"):
            L += ["", "Baselines against ATR (positive = better than ATR):"]
            for nm, c in info["baseline_vs_atr"].items():
                L.append(f"- {nm}: {f(c['improvement_pct'])}%  90% CI {f(c['ci90'])}  -> {c['verdict']}")
        if info["comparisons"]:
            L += ["", "Model against the best baseline:", "", "| model | improvement % | 90% CI of loss difference | first half | second half | verdict |", "|---|---|---|---|---|---|"]
            for nm, c in info["comparisons"].items():
                L.append(f"| {nm} | {f(c['improvement_pct'])} | {f(c['ci90'])} | {f(c['halves']['first'])} | {f(c['halves']['second'])} | **{c['verdict']}** |")
        L.append("")
    L.append("Gate A needs `pass` on the range target in every seed and at least 2 seeds (`--combine`). It says nothing about trading profit: that is Gate B (replay).")
    return "\n".join(L)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("hist_dir", nargs="?", default="")
    ap.add_argument("--combine", nargs="+", default=[], help="probe_report.json files (one per seed): print Gate A across them")
    ap.add_argument("--out", default="tsfm_probe_out")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--per-symbol", type=int, default=40)
    ap.add_argument("--horizon", type=int, default=5)
    ap.add_argument("--min-context", type=int, default=300)
    ap.add_argument("--min-cal", type=int, default=300)
    ap.add_argument("--boot", type=int, default=2000)
    ap.add_argument("--holdout-months", type=float, default=6.0)
    ap.add_argument("--holdout-only", action="store_true")
    ap.add_argument("--tsfm-url", default="http://localhost:8011")
    ap.add_argument("--no-model", action="store_true")
    ap.add_argument("--max-forecasts", type=int, default=0)
    ap.add_argument("--timeout", type=float, default=120.0)
    a = ap.parse_args(argv)
    if a.combine:
        reps = [json.loads(Path(p).read_text()) for p in a.combine]
        res = combine(reps, a.combine)
        print(json.dumps(res, indent=2))
        return 0 if res["status"] in ("PASS", "NOT_ENOUGH_DATA", "NO_INPUT") else 1
    if not a.hist_dir:
        ap.error("HIST_DIR or --combine is required")
    out = Path(a.out)
    data = load_dir(Path(a.hist_dir))
    if not data:
        print(f"no usable history in {a.hist_dir}", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)          # created only once there is something to write (an empty history leaves no stray folder)
    models: Dict[str, ModelFn] = {}
    if not a.no_model:
        cache_file = out / "tsfm_forecasts.jsonl"
        models["tsfm"] = http_model(a.tsfm_url, a.horizon, a.timeout, load_cache(cache_file), cache_file)
    dec = build_decisions(data, a.horizon, a.per_symbol, a.seed, a.min_context, models, a.max_forecasts, log=lambda s: print(s, file=sys.stderr))
    rep = evaluate(dec, a.horizon, list(models), a.boot, a.seed, a.holdout_months, a.holdout_only, a.min_cal)
    (out / "probe_report.json").write_text(json.dumps(rep, indent=2))
    md = to_markdown(rep) if rep.get("targets") else "no decisions"
    (out / "probe_report.md").write_text(md)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
