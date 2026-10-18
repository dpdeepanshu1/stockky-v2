#!/usr/bin/env python3
"""ml_v2_walkforward.py - group 441: does a model on the FEATURE_COLUMNS_V2 structure features rank entries better than chance, out of sample? EVIDENCE ONLY.

  python3 scripts/ml_v2_walkforward.py HIST_DIR [--out DIR] [--step 3] [--horizon 10] [--cost-pct 0.30] [--slip-pct 0.10] [--folds 4] [--embargo-days 32]
                                       [--top-pct 30] [--max-train 20000] [--ridge 10] [--min-n 500] [--boot 500] [--jobs 1] [--sample 400 --seed 7] [--all-days] [--no-save]

This is the offline "versioned retrain" step. It does NOT touch the live model (training/train.py keeps its own columns and registry) and writes nothing a service loads.
  1. Rows: every replayed day (no lookahead, the same engine and replay as the A/B report; days the entry gates reject and days outside 0 <= stretch <= 3 ATR are left
     out unless --all-days) -> shared/market_structure/ml_features.build_features_v2 vector. Label = net return at --horizon sessions > 0 (entry = next open + slippage, cost subtracted).
     The weekly / volume-profile / Wyckoff columns stay empty here (a constant column is dropped by the fit).
  2. Walk-forward: rows sorted by time, cut into folds+1 blocks. Fold k trains on blocks 0..k, tests on block k+1, and the training rows whose outcome window could overlap the
     test block (the last --embargo-days before it) are removed, so no training label sees the test period.
  3. Model: ridge regression on the 0/1 label (a linear probability score; stdlib only, closed form). Missing values -> the TRAIN mean; standardised with TRAIN statistics only.
     Baseline to beat: ms_tqs alone (the engine's own score). A random --max-train subsample (seeded) keeps the solve fast.
  4. Out-of-sample read-out: AUC per fold and pooled (model vs ms_tqs); the days whose score is in the top --top-pct of the TRAIN scores are the "picked" ones: edge = mean net of picked
     - mean net of all test days (pp), cluster bootstrap (symbol x month, seed 7).
Verdict: no_data | insufficient_sample (< --min-n test rows, < 3 folds with both classes, or no interval) | pass (edge > 0, bootstrap 5th pct > 0, pooled AUC > 0.5 and above ms_tqs,
more than half of the folds have edge > 0, picked mean net > 0) | hurts (95th pct < 0) | no_edge.
A final model fitted on ALL rows is saved as ml_v2_model.json with a version id (columns hash + engine version + params hash + data hash). It is a record of what would be trained,
not a deployable artifact: promoting it needs the registry, the evidence gates and a live-model comparison. Output: ml_v2_report.json / .md / ml_v2_model.json in --out (default HIST_DIR/ml_v2_out).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from statistics import mean
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ms_ab_report as ab  # noqa: E402
from market_structure.backtest import HORIZON_MAX, HORIZONS, forward_outcome, rule_flags  # noqa: E402
from market_structure.candles import ist_date  # noqa: E402
from market_structure.ml_features import FEATURE_COLUMNS_V2, assert_comparable, build_features_v2, columns_hash, vector  # noqa: E402
from market_structure.ms_types import O, T, finite  # noqa: E402
from market_structure.replay import replay  # noqa: E402

_BENCH: Optional[Dict[str, Any]] = None
TQS_COL = FEATURE_COLUMNS_V2.index("ms_tqs")
DAY_S = 86400.0


# ------------------------------------------------------------------------------------------------ rows
def _rows(bars: Sequence[Sequence[float]], sym: str, step: int, horizon: int, cost: float, slip: float, all_days: bool) -> List[Dict[str, Any]]:
    if len(bars) < ab.MIN_BARS + HORIZON_MAX + 2:
        return []
    out: List[Dict[str, Any]] = []
    for r in replay(bars, step=step):
        i = r["i"]
        if r["quality"] not in ("ok", "limited") or i + 1 >= len(bars):
            continue
        s = r["summary"]
        if not all_days:
            st = s.get("ms_stretch_atr")
            if not finite(st) or not (0.0 <= float(st) <= 3.0) or any(rule_flags(s).values()):
                continue
        eo = float(bars[i + 1][O])
        if not finite(eo) or eo <= 0:
            continue
        entry = eo * (1.0 + slip / 100.0)
        oc = forward_outcome(entry, s.get("ms_stop"), s.get("ms_target"), bars[i + 1:])
        if oc["ret_20d"] is None or oc[f"ret_{horizon}d"] is None:
            continue
        built = build_features_v2(s)
        ret = round(oc[f"ret_{horizon}d"] - cost, 3)
        out.append({"sym": sym, "ts": float(bars[i][T]), "month": ist_date(bars[i][T])[:7], "x": vector(built), "meta": built["meta"], "ret": ret,
                    "y": 1 if ret > 0 else 0})
    return out


def _job(args: Tuple[str, str, int, int, float, float, bool]) -> Dict[str, Any]:
    path, sym, step, horizon, cost, slip, all_days = args
    try:
        bars = ab.load_bars(Path(path))
    except Exception as e:  # noqa: BLE001
        return {"sym": sym, "rows": [], "err": str(e)[:120]}
    return {"sym": sym, "rows": _rows(bars, sym, step, horizon, cost, slip, all_days), "err": None}


# ------------------------------------------------------------------------------------------------ model (stdlib)
def auc(scores: Sequence[float], labels: Sequence[int]) -> Optional[float]:
    """Rank-based AUC with tie handling; None when one class is missing."""
    pos = sum(labels)
    neg = len(labels) - pos
    if pos == 0 or neg == 0:
        return None
    order = sorted(range(len(scores)), key=lambda k: scores[k])
    rank_sum = 0.0
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            if labels[order[k]] == 1:
                rank_sum += avg
        i = j + 1
    return (rank_sum - pos * (pos + 1) / 2.0) / (pos * neg)


def _solve(a: List[List[float]], b: List[float]) -> List[float]:
    """Gauss-Jordan with partial pivoting; a is (p x p) positive definite after the ridge term."""
    p = len(b)
    m = [row[:] + [b[k]] for k, row in enumerate(a)]
    for c in range(p):
        piv = max(range(c, p), key=lambda r: abs(m[r][c]))
        m[c], m[piv] = m[piv], m[c]
        d = m[c][c]
        if abs(d) < 1e-12:
            d = 1e-12
        for k in range(c, p + 1):
            m[c][k] /= d
        for r in range(p):
            if r != c and m[r][c] != 0.0:
                f = m[r][c]
                for k in range(c, p + 1):
                    m[r][k] -= f * m[c][k]
    return [m[k][p] for k in range(p)]


def fit(x_rows: Sequence[Sequence[Optional[float]]], y: Sequence[int], ridge: float = 10.0, max_rows: int = 20000, seed: int = 7) -> Optional[Dict[str, Any]]:
    """Ridge on the 0/1 label. Columns with < 20 % observed values or no spread are dropped. Returns {cols, mu, sd, w, b} or None when nothing is usable."""
    n = len(y)
    if n < 50:
        return None
    idx = list(range(n))
    if n > max_rows:
        idx = sorted(random.Random(seed).sample(idx, max_rows))
    p_all = len(x_rows[0])
    cols, mu, sd = [], [], []
    for c in range(p_all):
        v = [x_rows[k][c] for k in idx if x_rows[k][c] is not None]
        if len(v) < 0.2 * len(idx):
            continue
        m = sum(v) / len(v)
        s = (sum((t - m) ** 2 for t in v) / len(v)) ** 0.5
        if s < 1e-9:
            continue
        cols.append(c)
        mu.append(m)
        sd.append(s)
    if not cols:
        return None
    p = len(cols)
    ybar = sum(y[k] for k in idx) / len(idx)
    xtx = [[0.0] * p for _ in range(p)]
    xty = [0.0] * p
    for k in idx:
        z = _z(x_rows[k], cols, mu, sd)
        yc = y[k] - ybar
        for a in range(p):
            za = z[a]
            if za == 0.0:
                continue
            xty[a] += za * yc
            row = xtx[a]
            for b in range(a, p):
                row[b] += za * z[b]
    for a in range(p):
        for b in range(a):
            xtx[a][b] = xtx[b][a]
        xtx[a][a] += ridge
    w = _solve(xtx, xty)
    return {"cols": cols, "mu": mu, "sd": sd, "w": w, "b": ybar}


def _z(row: Sequence[Optional[float]], cols: Sequence[int], mu: Sequence[float], sd: Sequence[float]) -> List[float]:
    out = []
    for j, c in enumerate(cols):
        v = row[c]
        z = 0.0 if v is None else (v - mu[j]) / sd[j]
        out.append(max(-5.0, min(5.0, z)))              # a missing value is the train mean (z = 0); outliers are clipped
    return out


def score(model: Dict[str, Any], row: Sequence[Optional[float]]) -> float:
    z = _z(row, model["cols"], model["mu"], model["sd"])
    return model["b"] + sum(w * v for w, v in zip(model["w"], z))


# ------------------------------------------------------------------------------------------------ walk-forward
def make_folds(rows: Sequence[Dict[str, Any]], folds: int, embargo_days: float) -> List[Tuple[List[int], List[int]]]:
    """(train_idx, test_idx) per fold over rows sorted by ts. Blocks have equal row counts; training drops rows within `embargo_days` before the test block."""
    order = sorted(range(len(rows)), key=lambda k: rows[k]["ts"])
    nb = folds + 1
    size = len(order) // nb
    out = []
    if size < 1:
        return out
    for f in range(1, nb):
        test = order[f * size: (f + 1) * size if f < nb - 1 else len(order)]
        t0 = rows[test[0]]["ts"]
        train = [k for k in order[: f * size] if rows[k]["ts"] < t0 - embargo_days * DAY_S]
        if train and test:
            out.append((train, test))
    return out


def evaluate(rows: List[Dict[str, Any]], folds: int, embargo_days: float, top_pct: float, ridge: float, max_train: int, seed: int) -> Dict[str, Any]:
    per_fold: List[Dict[str, Any]] = []
    picked_views: List[Dict[str, Any]] = []
    pooled = {"m": [], "t": [], "y": []}
    for f, (tr, te) in enumerate(make_folds(rows, folds, embargo_days)):
        model = fit([rows[k]["x"] for k in tr], [rows[k]["y"] for k in tr], ridge, max_train, seed + f)
        if model is None:
            continue
        tr_scores = sorted(score(model, rows[k]["x"]) for k in tr)
        cut = tr_scores[min(len(tr_scores) - 1, int(len(tr_scores) * (1.0 - top_pct / 100.0)))]
        sc = [score(model, rows[k]["x"]) for k in te]
        ys = [rows[k]["y"] for k in te]
        tq = [rows[k]["x"][TQS_COL] if rows[k]["x"][TQS_COL] is not None else 0.0 for k in te]
        picked = [rows[k] for k, s_ in zip(te, sc) if s_ >= cut]
        allr = [rows[k] for k in te]
        edge = round(mean(r["ret"] for r in picked) - mean(r["ret"] for r in allr), 3) if picked else None
        per_fold.append({"fold": f + 1, "train": len(tr), "test": len(te), "picked": len(picked), "auc_model": _r(auc(sc, ys)), "auc_tqs": _r(auc(tq, ys)),
                         "picked_mean_net": round(mean(r["ret"] for r in picked), 3) if picked else None, "all_mean_net": round(mean(r["ret"] for r in allr), 3),
                         "edge_pp": edge, "cols_used": len(model["cols"])})
        pooled["m"] += sc
        pooled["t"] += tq
        pooled["y"] += ys
        pk = {id(r) for r in picked}
        for r in allr:
            picked_views.append({"sym": r["sym"], "month": r["month"], "ts": r["ts"], "r": r["ret"], "rej": [] if id(r) in pk else ["not_picked"]})
    return {"folds": per_fold, "pooled_auc_model": _r(auc(pooled["m"], pooled["y"])), "pooled_auc_tqs": _r(auc(pooled["t"], pooled["y"])), "views": picked_views}


def _r(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(v, 4)


def verdict(n_test: int, folds: Sequence[Dict[str, Any]], edge: Optional[float], ci: Optional[List[float]], auc_m: Optional[float], auc_t: Optional[float],
            picked_mean: Optional[float], min_n: int) -> str:
    if n_test == 0:
        return "no_data"
    usable = [f for f in folds if f["auc_model"] is not None]
    if n_test < min_n or len(usable) < 3 or edge is None or ci is None:
        return "insufficient_sample"
    if ci[1] < 0:
        return "hurts"
    pos = sum(1 for f in folds if f["edge_pp"] is not None and f["edge_pp"] > 0)
    if (edge > 0 and ci[0] > 0 and auc_m is not None and auc_m > 0.5 and (auc_t is None or auc_m > auc_t) and pos * 2 > len(folds)
            and picked_mean is not None and picked_mean > 0):
        return "pass"
    return "no_edge"


def _hash_rows(rows: Sequence[Dict[str, Any]]) -> str:
    h = hashlib.sha1()
    for r in rows:
        h.update(f"{r['sym']}|{r['ts']}|{r['ret']}".encode())
    return h.hexdigest()[:8]


def build_report(rows: List[Dict[str, Any]], a: Dict[str, Any], meta: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    ev = evaluate(rows, a["folds"], a["embargo_days"], a["top_pct"], a["ridge"], a["max_train"], a["seed"]) if rows else {"folds": [], "views": [],
                                                                                                                       "pooled_auc_model": None, "pooled_auc_tqs": None}
    views = ev.pop("views")
    base = views
    cand = [v for v in views if not v["rej"]]
    for v in views:
        v[f"r{a['horizon']}"] = v["r"]
    edge = ab._edge(base, cand, a["horizon"]) if base and cand else None
    boot = ab.bootstrap_edge(base, a["horizon"], a["boot"]) if base else {"clusters": 0, "ci90": None, "p_edge_positive": None, "resamples": 0}
    picked_mean = round(mean(v["r"] for v in cand), 3) if cand else None
    comparable = assert_comparable(rows) if rows else True
    rep: Dict[str, Any] = {"meta": {**meta, "columns_hash": columns_hash(), "comparable": comparable}, "horizon": a["horizon"], "rows": len(rows),
                           "oos_rows": len(base), "picked_rows": len(cand), "all_mean_net": round(mean(v["r"] for v in base), 3) if base else None,
                           "picked_mean_net": picked_mean, "edge_pp": edge, "bootstrap": boot, **ev}
    rep["verdict"] = "no_data" if not rows else ("insufficient_sample" if not comparable else
                                                 verdict(len(base), ev["folds"], edge, boot.get("ci90"), ev["pooled_auc_model"], ev["pooled_auc_tqs"], picked_mean, a["min_n"]))
    model = None
    if rows and comparable:
        m = fit([r["x"] for r in rows], [r["y"] for r in rows], a["ridge"], a["max_train"], a["seed"])
        if m is not None:
            mt = rows[0]["meta"]
            vid = f"v2-{columns_hash()}-{mt.get('engine_version')}-{mt.get('params_hash')}-{_hash_rows(rows)}"
            model = {"version_id": vid, "kind": "ridge_linear_probability", "status": "evidence_only_not_loadable_by_live_services", "columns_hash": columns_hash(),
                     "engine_version": mt.get("engine_version"), "params_hash": mt.get("params_hash"), "label": f"net_return_T+{a['horizon']}d > 0",
                     "train_rows": len(rows), "train_range": [ist_date(min(r["ts"] for r in rows)), ist_date(max(r["ts"] for r in rows))],
                     "columns": [FEATURE_COLUMNS_V2[c] for c in m["cols"]], "mu": m["mu"], "sd": m["sd"], "weights": m["w"], "intercept": m["b"],
                     "oos_verdict": rep["verdict"], "oos_auc": ev["pooled_auc_model"], "oos_edge_pp": edge}
            rep["model_version_id"] = vid
    return rep, model


def to_markdown(rep: Dict[str, Any]) -> str:
    f = lambda v: "-" if v is None else str(v)  # noqa: E731
    m = rep["meta"]
    L = [f"# ML V2 walk-forward evidence (T+{rep['horizon']}, net of costs)", "", f"**Verdict: `{rep['verdict']}`**", "",
         f"- rows {rep['rows']}, out-of-sample rows {rep['oos_rows']}, picked {rep['picked_rows']} (top {m['top_pct']}% of train scores); columns hash `{m['columns_hash']}`, comparable rows: {m['comparable']}",
         f"- pooled AUC: model {f(rep['pooled_auc_model'])} vs ms_tqs alone {f(rep['pooled_auc_tqs'])} (0.5 = chance)",
         f"- picked mean net {f(rep['picked_mean_net'])}% vs all {f(rep['all_mean_net'])}%: edge **{f(rep['edge_pp'])} pp**, 90% CI {f(rep['bootstrap'].get('ci90'))}, P(edge>0) {f(rep['bootstrap'].get('p_edge_positive'))}",
         f"- model version: {f(rep.get('model_version_id'))}", "", "| fold | train | test | picked | AUC model | AUC tqs | picked net % | all net % | edge pp |", "|---|---|---|---|---|---|---|---|---|"]
    for x in rep["folds"]:
        L.append(f"| {x['fold']} | {x['train']} | {x['test']} | {x['picked']} | {f(x['auc_model'])} | {f(x['auc_tqs'])} | {f(x['picked_mean_net'])} | {f(x['all_mean_net'])} | {f(x['edge_pp'])} |")
    L += ["", "Training rows within the embargo before each test block are dropped; missing values use the train mean; nothing is tuned on the test block. `pass` is evidence on this history only: "
              "the live model keeps its own columns, and a V2 model would still have to beat it in a registry comparison before anything is promoted. `insufficient_sample` means nothing may be concluded."]
    return "\n".join(L) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dir")
    ap.add_argument("--out", default="")
    ap.add_argument("--step", type=int, default=3)
    ap.add_argument("--horizon", type=int, default=10, choices=HORIZONS)
    ap.add_argument("--cost-pct", type=float, default=0.30)
    ap.add_argument("--slip-pct", type=float, default=0.10)
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--embargo-days", type=float, default=32.0)
    ap.add_argument("--top-pct", type=float, default=30.0)
    ap.add_argument("--max-train", type=int, default=20000)
    ap.add_argument("--ridge", type=float, default=10.0)
    ap.add_argument("--min-n", type=int, default=500)
    ap.add_argument("--boot", type=int, default=500)
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--all-days", action="store_true")
    ap.add_argument("--no-save", action="store_true")
    a = ap.parse_args(argv)
    d = Path(a.dir)
    files = [f for f in sorted(d.glob("*.json")) if f.stem.upper() not in ab.BENCH_NAMES]
    pool = len(files)
    if a.sample > 0 and pool > a.sample:
        files = sorted(random.Random(a.seed).sample(files, a.sample))
    jobs = [(str(f), f.stem.upper(), a.step, a.horizon, a.cost_pct, a.slip_pct, a.all_days) for f in files]
    if a.jobs > 1:
        with ProcessPoolExecutor(max_workers=a.jobs) as ex:
            results = list(ex.map(_job, jobs, chunksize=2))
    else:
        results = [_job(j) for j in jobs]
    rows: List[Dict[str, Any]] = []
    skipped = 0
    for res in results:
        if res["err"]:
            skipped += 1
            print(f"skip {res['sym']}: {res['err']}", file=sys.stderr)
        rows += res["rows"]
    meta = {"symbols": len(jobs) - skipped, "skipped": skipped, "step": a.step, "cost_pct": a.cost_pct, "slip_pct": a.slip_pct, "folds": a.folds, "embargo_days": a.embargo_days,
            "top_pct": a.top_pct, "ridge": a.ridge, "all_days": a.all_days, "sample": {"requested": a.sample, "pool": pool, "drawn": len(jobs), "seed": a.seed if a.sample > 0 else None}}
    rep, model = build_report(rows, vars(a), meta)
    out = Path(a.out) if a.out else d / "ml_v2_out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "ml_v2_report.json").write_text(json.dumps(rep, indent=1))
    md = to_markdown(rep)
    (out / "ml_v2_report.md").write_text(md)
    if model is not None and not a.no_save:
        (out / "ml_v2_model.json").write_text(json.dumps(model, indent=1))
    print(md)
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
