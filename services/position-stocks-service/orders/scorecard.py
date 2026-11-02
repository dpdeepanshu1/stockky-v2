"""orders/scorecard.py - E285 batch 6: the gate scorecard's logic (scripts/gate_scorecard.py is a thin wrapper: DB read, print).

Same samples as the nightly policy job (orders/gate_policy_job.py): one per (symbol, gate, side, IST day, 30-minute bucket), net60 = the 60-minute move on the recorded 1-minute bars
(label(), flipped for a short) minus the round-trip cost, and the SAME verdict rule (gate_policy_job.verdict), so KEEP / LOOSEN / SOFT mean one thing in the table and in the policy.
New in batch 6:
  * coverage: how many samples could not be measured, split by cause (no day folder / no file for the symbol / bars start after the skip / bars end before +60 min);
  * per gate: days, symbols, bootstrap lower bound, the lowest mean with one day removed, optional per-day lines;
  * random-entry baseline: net60 of random other symbols entered at the SAME times and side (`rand60`), and `edge` = gate net60 - rand60: tells the stock picking from the market that day;
  * long / short are never mixed: a gate with samples on both sides gets one row per side and a note.
Pure: every input is passed in. Nothing here raises into a caller that handles a normal result.
"""
from __future__ import annotations

import random
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import config
from orders import candidate_outcomes as co, gate_policy, gate_policy_job as job

IST = timedelta(hours=5, minutes=30)


def side_sign(reason: Optional[str], direction: str) -> int:
    """+1 = measure as a long, -1 = as a short (the move is flipped so positive always means the trade would have gained)."""
    if direction == "short":
        return -1
    if direction == "auto" and str(reason or "").strip().upper().startswith("SHORT_"):
        return -1
    return 1


def forward(bars: List[list], t0: float, sign: int = 1, target: float = 1.5, stop: float = 1.0):
    """(r30, r60, rclose, target_first) as % from the last bar closing at/before t0, flipped for a short (sign -1); None when not measurable.
    outcome: target | stop | neither (a bar touching both counts as the stop)."""
    idx = None
    for i, b in enumerate(bars):
        if b[0] <= t0:
            idx = i
        else:
            break
    if idx is None or idx >= len(bars) - 1:
        return None
    entry = float(bars[idx][4])
    if entry <= 0:
        return None

    def at(sec):
        j = idx
        for k in range(idx, len(bars)):
            if bars[k][0] <= t0 + sec:
                j = k
            else:
                break
        return sign * (float(bars[j][4]) / entry - 1) * 100

    outcome = "neither"
    for k in range(idx + 1, len(bars)):
        if bars[k][0] > t0 + 3600:
            break
        up = (float(bars[k][2]) / entry - 1) * 100
        dn = (float(bars[k][3]) / entry - 1) * 100
        hi, lo = (up, dn) if sign > 0 else (-dn, -up)        # a short gains when price falls
        if lo <= -stop:
            outcome = "stop"                                   # includes a bar that touches both: assume the worse
            break
        if hi >= target:
            outcome = "target"
            break
    return at(1800), at(3600), sign * (float(bars[-1][4]) / entry - 1) * 100, outcome


def build(rows: Iterable[Dict[str, Any]], load_bars: Callable[[str, str], Optional[List[list]]], list_symbols: Callable[[str], Optional[List[str]]], *, cost: float = 0.15,
          target: float = 1.5, stop: float = 1.0, direction: str = "long", baseline_k: int = 3, seed: int = 285, policy_states: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """rows: dicts with symbol, decision, reason, created_ts (UTC epoch s). Returns {coverage, groups, notes}; render() prints it."""
    cov: Dict[str, Any] = {"rows": 0, "deduped": 0, "unique": 0, "measured": 0, "no_day_folder": defaultdict(int), "no_file": 0, "starts_after": 0, "ends_before": 0, "no_bars": 0}
    G: Dict[Tuple[str, str], Dict[str, Any]] = {}
    seen = set()
    short_rows_as_long = 0
    n_boot, conf = job._i("GATE_POLICY_BOOT_N", 2000), job._f("GATE_POLICY_CONFIDENCE", 0.95)
    sym_cache: Dict[str, Optional[List[str]]] = {}
    for r in rows:
        try:
            cov["rows"] += 1
            gk = job.gate_key(r.get("reason"), r.get("decision"))
            sign = side_sign(r.get("reason"), direction)
            side = "SHORT" if sign < 0 else "LONG"
            if direction == "long" and str(r.get("reason") or "").strip().upper().startswith("SHORT_"):
                short_rows_as_long += 1
            ts = float(r["created_ts"])
            sym = str(r["symbol"]).upper()
            day = datetime.fromtimestamp(ts, timezone.utc).astimezone(timezone(IST)).strftime("%Y-%m-%d")
            key = (sym, gk, day, int(ts // 1800), side)
            if key in seen:
                cov["deduped"] += 1
                continue
            seen.add(key)
            cov["unique"] += 1
            bars = load_bars(sym, day)
            if not bars:
                if day not in sym_cache:
                    sym_cache[day] = list_symbols(day)
                if sym_cache[day] is None:
                    cov["no_day_folder"][day] += 1
                else:
                    cov["no_file"] += 1
                continue
            why = co.no_bar_reason(bars, ts)
            if why != "ok":
                cov[why] += 1
                continue
            lab = co.label(bars, ts, side)
            r60 = lab.get("r60") if lab.get("status") == "ok" else None
            fw = forward(bars, ts, sign, target, stop)
            if r60 is None or fw is None:
                cov["no_bars"] += 1
                continue
            g = G.setdefault((gk, side), {"vals": [], "outcomes": [], "points": [], "a30": [], "aeod": []})
            g["vals"].append((day, sym, round(float(r60) - cost, 4)))
            g["outcomes"].append(fw[3])
            g["a30"].append(fw[0])
            g["aeod"].append(fw[2])
            g["points"].append((day, ts, sym))
            cov["measured"] += 1
        except Exception:  # noqa: BLE001
            continue
    rng = random.Random(seed)
    out_groups: List[Dict[str, Any]] = []
    for (gk, side), g in G.items():
        st = job.summarize(g["vals"], n_boot, conf)
        n = st["n"]
        by_day: Dict[str, List[float]] = defaultdict(list)
        for d, _s, v in g["vals"]:
            by_day[d].append(v)
        rand: List[float] = []
        if baseline_k > 0 and not gk.startswith("ENTERED"):
            for day, ts, sym in g["points"]:
                if day not in sym_cache:
                    sym_cache[day] = list_symbols(day)
                rand += co.baseline_values(day, ts, sym, side, sym_cache.get(day), load_bars, baseline_k, cost, rng)
        rmean = round(sum(rand) / len(rand), 4) if rand else None
        out_groups.append({
            "gate": gk, "side": side, "stats": st, "avg30": round(sum(g["a30"]) / n, 4), "avgeod": round(sum(g["aeod"]) / n, 4),
            "avg60": round(st["mean"] + cost, 4) if st["mean"] is not None else None,
            "target_first": 100.0 * sum(1 for o in g["outcomes"] if o == "target") / n, "stop_first": 100.0 * sum(1 for o in g["outcomes"] if o == "stop") / n,
            "rand60": rmean, "rand_n": len(rand), "edge": (round(st["mean"] - rmean, 4) if (rmean is not None and st["mean"] is not None) else None),
            "by_day": {d: (len(v), round(sum(v) / len(v), 4)) for d, v in sorted(by_day.items())},
            "policy": (policy_states or {}).get(gk), "verdict": job.verdict(st, is_baseline=gk.startswith("ENTERED")),
        })
    out_groups.sort(key=lambda x: -x["stats"]["n"])
    notes: List[str] = []
    sides: Dict[str, set] = defaultdict(set)
    for x in out_groups:
        sides[x["gate"]].add(x["side"])
    mixed = sorted(k for k, v in sides.items() if len(v) > 1)
    if mixed:
        notes.append("long and short samples share these gates and are shown on separate rows, never averaged: " + ", ".join(mixed))
    if short_rows_as_long:
        notes.append(f"{short_rows_as_long} SHORT_ rows were measured as longs (--direction long) and read INVERTED; use --direction auto to measure each side its own way")
    unique = cov["unique"]
    if unique and cov["measured"] / unique < 0.8:
        notes.append(f"only {100.0 * cov['measured'] / unique:.0f}% of the samples could be measured: every verdict below rests on that subset (see coverage)")
    for x in out_groups:
        if x["verdict"].startswith("LOOSEN") and (x["edge"] is None or x["edge"] <= 0):
            notes.append(f"{x['gate']} ({x['side']}) passes LOOSEN but its edge over random entries at the same times is {'unknown' if x['edge'] is None else format(x['edge'], '+.2f') + '%'}: the day's market may explain the gain")
    cov["no_day_folder"] = dict(cov["no_day_folder"])
    return {"coverage": cov, "groups": out_groups, "notes": notes, "cost": cost, "target": target, "stop": stop, "direction": direction}


def coverage_lines(cov: Dict[str, Any]) -> List[str]:
    unique, ok = cov["unique"], cov["measured"]
    nd = sum(cov["no_day_folder"].values())
    L = [f"rows read: {cov['rows']}   unique samples (symbol, gate, side, day, 30 min): {unique}   measured: {ok} ({(100.0 * ok / unique) if unique else 0:.0f}%)   not measured: {unique - ok}"]
    if unique - ok:
        L.append(f"  not measured because: no bar folder for the day {nd} ({', '.join(sorted(cov['no_day_folder'])[:6])}{' ...' if len(cov['no_day_folder']) > 6 else ''}); "
                 f"folder exists but no file for the symbol {cov['no_file']} (symbol never got ticks or was not noted); bars start after the skip {cov['starts_after']}; "
                 f"bars end before +60 min {cov['ends_before']}; other {cov['no_bars']}")
    return L


def render(res: Dict[str, Any], days: int, min_n: int, by_day: bool = False) -> str:
    L = [f"\nGate scorecard ({res['direction']}) - last {days} days, target +{res['target']}% / stop -{res['stop']}% within 60 min, cost {res['cost']}% round trip"]
    L += coverage_lines(res["coverage"])
    L.append("")
    hdr = f"{'gate':<42}{'side':<6}{'n':>5}{'days':>5}{'syms':>5}{'avg60':>7}{'net60':>7}{'lb95':>7}{'loo':>7}{'rand60':>8}{'edge':>7}{'tgt%':>6}{'stp%':>6}  {'policy':<6} verdict"
    L += [hdr, "-" * len(hdr)]

    def f(v, w, spec="+.2f"):
        return f"{'-' if v is None else format(v, spec):>{w}}"
    for x in res["groups"]:
        s = x["stats"]
        L.append(f"{x['gate'][:41]:<42}{x['side']:<6}{s['n']:>5}{s['days']:>5}{s['symbols']:>5}{f(x['avg60'], 7)}{f(s['mean'], 7)}{f(s['lb'], 7)}{f(s['loo_min'], 7)}"
                 f"{f(x['rand60'], 8)}{f(x['edge'], 7)}{x['target_first']:>6.0f}{x['stop_first']:>6.0f}  {(x['policy'] or '-'):<6} {x['verdict']}")
        if by_day and s["n"] >= min_n:
            L.append("    by day: " + "  ".join(f"{d[5:]} n={n} {m:+.2f}%" for d, (n, m) in x["by_day"].items()))
    L.append("")
    L.append("Reading it: net60 = mean 60-minute move after costs; lb95 = bootstrap lower bound of net60; loo = lowest net60 with any ONE day removed (a gain that needs one day is not a gain);")
    L.append("rand60 = net60 of random other symbols entered at the same times (same side): edge = net60 - rand60. Verdict = the nightly policy's own rule: LOOSEN passes HARD -> SOFT")
    L.append(f"(n >= {job._i('GATE_POLICY_MIN_N', 30)}, >= {job._i('GATE_POLICY_MIN_DAYS', 5)} days, >= {job._i('GATE_POLICY_MIN_SYMBOLS', 20)} symbols, lb95 > 0, loo > 0); KEEP = net60 under -{job._f('GATE_POLICY_HYST_PCT', 0.10):.2f}%; NO CALL = neither.")
    for n in res["notes"]:
        L.append("NOTE: " + n)
    return "\n".join(L)
