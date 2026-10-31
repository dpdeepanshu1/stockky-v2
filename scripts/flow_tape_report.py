#!/usr/bin/env python3
"""E271: order-flow tape report. READ-ONLY, stdlib only. Changes nothing.

Answers two questions from the trades' own records:

  ENTRY  Is FLOW_GATE_MIN_AGAINST right?  Trades that carry the tape verdict seen at entry (flow_json["tape"]: position-stocks longs since E268, live shorts
         since E269; real-trade positions through their ENTER decision) are bucketed by how many of the six checks were against them (0, 1, 2, 3+) and the
         average result of each bucket is compared, with a 90 % interval whose standard error is taken ACROSS SESSIONS (trades of one day share the same market).
  EXIT   Did the tape's stop / target tightening (E270) help?  Trades with a tape move (position-stocks flow_json["tape_exit"], real-trade TAPE_TIGHTEN events)
         are compared with the others, and for position-stocks trades that closed on the tightened stop the loss avoided against the ORIGINAL stop is
         reported as an UPPER BOUND (the price might not have gone on to the old stop).

Inputs (any mix, repeat the flag):
  --ps FILE     position-stocks GET /trades/history (JSON object with "trades", or a bare list)
  --rt FILE     real-trade GET /positions/REAL/history (JSON object with "positions", or a bare list)
  --store FILE  JSONL file that rows are APPENDED to (de-duplicated) and read back, because position-stocks deletes closed trades after
                TRADE_HISTORY_RETENTION_DAYS (3), so run this once a day with --store to keep the evidence.

IMPORTANT LIMIT: with FLOW_GATE_MODE=live a trade with 2 or more adverse checks is REFUSED, so such trades only exist when the gate was off / log, the tape was
unreliable, or the row predates the gate. If the "blocked" bucket is empty the report says so and prints NOT_ENOUGH_DATA; to judge the cut-off set
FLOW_GATE_MODE=log for a sample of sessions (it records the verdict without refusing).

Usage: python3 scripts/flow_tape_report.py --ps ps.json --rt rt.json [--store tape_rows.jsonl] [--min-n 30] [--min-days 5] [--json]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from typing import Dict, Iterable, List, Optional

Z90 = 1.645
BUCKETS = ("0", "1", "2", "3+")


def _f(v) -> Optional[float]:
    if isinstance(v, bool) or v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _rows_of(doc) -> List[dict]:
    if isinstance(doc, list):
        return [r for r in doc if isinstance(r, dict)]
    if isinstance(doc, dict):
        for k in ("trades", "positions", "rows"):
            if isinstance(doc.get(k), list):
                return [r for r in doc[k] if isinstance(r, dict)]
    return []


def normalise(row: dict, service: str) -> Optional[dict]:
    """One flat record per closed trade, or None when the trade has no usable result. Pure."""
    if not isinstance(row, dict):
        return None
    if service == "ps":
        if str(row.get("status") or "").upper() in ("OPEN", "ERROR") or _f(row.get("exit_price")) is None:
            return None
        pnl, pct = _f(row.get("realized_pnl")), _f(row.get("realized_pnl_pct"))
        side = str(row.get("side") or "LONG").upper()
        stamp = row.get("closed_at") or row.get("opened_at")
        tape_exit = row.get("tape_exit") if isinstance(row.get("tape_exit"), list) else None
        tightens = len(tape_exit) if tape_exit else 0
    else:
        pnl = _f(row.get("net_realized_pnl")) if _f(row.get("net_realized_pnl")) is not None else _f(row.get("realized_pnl"))
        pct = None
        side = "LONG"
        stamp = row.get("closed_at") or row.get("opened_at")
        tape_exit = None
        tightens = int(row.get("tape_tightens") or 0)
    if pnl is None and pct is None:
        return None
    tape = row.get("tape") if isinstance(row.get("tape"), dict) else None
    against = None
    if tape is not None:
        a = tape.get("against")
        against = int(a) if isinstance(a, (int, float)) and not isinstance(a, bool) else None
    day = str(stamp)[:10] if stamp else "unknown"
    return {"service": service, "id": row.get("id"), "symbol": row.get("symbol"), "side": side, "day": day, "pnl": pnl, "pct": pct,
            "against": against, "reliable": bool(tape.get("reliable")) if tape else None, "tightens": tightens, "tape_exit": tape_exit,
            "status": str(row.get("status") or "").upper(), "entry": _f(row.get("entry_price") or row.get("avg_entry_price")),
            "exit": _f(row.get("exit_price")), "opened_at": row.get("opened_at")}


def bucket_of(against: Optional[int]) -> Optional[str]:
    if against is None or against < 0:
        return None
    return "3+" if against >= 3 else str(against)


def value_of(rec: dict) -> Optional[float]:
    """The per-trade number the buckets are compared on: percent when known (position-stocks), else the rupee result."""
    return rec["pct"] if rec.get("pct") is not None else rec.get("pnl")


def summarize(recs: Iterable[dict]) -> dict:
    """n, wins, win rate, mean value and a 90 % interval for the mean. The standard error is clustered by session day:
    SE = sqrt(sum over days of (sum of deviations)^2) / n. Pure."""
    vals = [(r["day"], value_of(r)) for r in recs if value_of(r) is not None]
    n = len(vals)
    if n == 0:
        return {"n": 0, "days": 0, "wins": 0, "win_rate_pct": None, "mean": None, "se": None, "lo90": None, "hi90": None}
    mean = sum(v for _, v in vals) / n
    by_day: Dict[str, float] = defaultdict(float)
    for d, v in vals:
        by_day[d] += v - mean
    days = len(by_day)
    se = math.sqrt(sum(x * x for x in by_day.values())) / n if days >= 2 else None
    wins = sum(1 for _, v in vals if v > 0)
    return {"n": n, "days": days, "wins": wins, "win_rate_pct": round(100.0 * wins / n, 1), "mean": round(mean, 4), "se": None if se is None else round(se, 4),
            "lo90": None if se is None else round(mean - Z90 * se, 4), "hi90": None if se is None else round(mean + Z90 * se, 4)}


def compare(allowed: dict, blocked: dict, min_n: int, min_days: int) -> dict:
    """Is the group the gate would refuse clearly worse (KEEP the cut-off), clearly better (RELAX it) or neither (INCONCLUSIVE)? Pure.
    diff = mean(allowed) - mean(blocked); the standard errors of the two groups are added in quadrature."""
    if blocked["n"] < min_n or allowed["n"] < min_n or blocked["days"] < min_days or allowed["days"] < min_days or blocked["se"] is None or allowed["se"] is None:
        return {"verdict": "NOT_ENOUGH_DATA", "diff": None, "lo90": None, "hi90": None,
                "why": f"needs at least {min_n} trades and {min_days} sessions in each group (blocked {blocked['n']} trades / {blocked['days']} days, "
                       f"allowed {allowed['n']} / {allowed['days']})"}
    diff = allowed["mean"] - blocked["mean"]
    se = math.sqrt(allowed["se"] ** 2 + blocked["se"] ** 2)
    lo, hi = diff - Z90 * se, diff + Z90 * se
    verdict = "KEEP" if lo > 0 else ("RELAX" if hi < 0 else "INCONCLUSIVE")
    return {"verdict": verdict, "diff": round(diff, 4), "lo90": round(lo, 4), "hi90": round(hi, 4), "why": ""}


def entry_report(recs: List[dict], min_n: int, min_days: int, min_against: int = 2) -> dict:
    with_tape = [r for r in recs if bucket_of(r["against"]) is not None and r.get("reliable")]
    groups = {b: [r for r in with_tape if bucket_of(r["against"]) == b] for b in BUCKETS}
    blocked = [r for r in with_tape if r["against"] >= min_against]
    allowed = [r for r in with_tape if r["against"] < min_against]
    one_vs_zero = compare(summarize(groups["0"]), summarize(groups["1"]), min_n, min_days)
    return {"trades_with_reliable_tape": len(with_tape), "trades_total": len(recs),
            "buckets": {b: summarize(g) for b, g in groups.items()},
            "cutoff": {"min_against": min_against, "blocked": summarize(blocked), "allowed": summarize(allowed),
                       **compare(summarize(allowed), summarize(blocked), min_n, min_days)},
            "one_vs_zero": {"note": "would MIN_AGAINST=1 help? (0-adverse trades compared with 1-adverse trades)", **one_vs_zero}}


def saved_pct(rec: dict) -> Optional[float]:
    """Position-stocks trade closed on a stop the tape had tightened: the loss avoided against the ORIGINAL stop, in percent of entry (an upper bound:
    the price might not have gone on to the old stop). None when it does not apply."""
    tx = rec.get("tape_exit")
    if not tx or rec.get("entry") in (None, 0) or rec.get("exit") is None:
        return None
    first = next((m for m in tx if isinstance(m, dict) and _f(m.get("old_stop")) is not None and m.get("new_stop") is not None
                  and _f(m.get("new_stop")) != _f(m.get("old_stop"))), None)
    if first is None or rec["status"] not in ("STOP_HIT", "STOPPED", "STOP_LOSS_HIT", "SL_HIT"):
        return None
    last_stop = _f(tx[-1].get("new_stop"))
    if last_stop is None or abs(rec["exit"] - last_stop) > 0.01 * rec["entry"]:
        return None                       # it did not close on the tape's stop
    old = _f(first.get("old_stop"))
    s = -1.0 if rec["side"] == "SHORT" else 1.0
    gain = s * (rec["exit"] - old) / rec["entry"] * 100.0
    return round(gain, 4) if gain > 0 else 0.0


def exit_report(recs: List[dict], min_n: int, min_days: int) -> dict:
    tightened = [r for r in recs if r["tightens"] > 0]
    others = [r for r in recs if r["tightens"] == 0]
    saved = [x for x in (saved_pct(r) for r in tightened) if x is not None]
    return {"tightened": summarize(tightened), "not_tightened": summarize(others),
            "stop_exits_after_tighten": len(saved), "avoided_loss_upper_bound_pct_total": round(sum(saved), 3) if saved else None,
            "note": "tightened and not-tightened trades are different trades (a tape move needs an adverse tape), so their averages are not a like-for-like "
                    "comparison; the avoided-loss figure is an upper bound and says nothing about winners the tighter stop or nearer target may have cut. "
                    "A full counterfactual needs the recorded 1-minute bars (scripts/exit_followup_study.py style) and is not built.",
            **({"verdict": "NOT_ENOUGH_DATA", "why": f"fewer than {min_n} tape-tightened trades"} if len(tightened) < min_n else {"verdict": "READ_THE_NUMBERS", "why": ""})}


def load_store(path: str) -> List[dict]:
    out: List[dict] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        j = json.loads(line)
                        if isinstance(j, dict):
                            out.append(j)
                    except ValueError:
                        continue
    except OSError:
        pass
    return out


def row_key(rec: dict) -> str:
    return f"{rec.get('service')}|{rec.get('id')}|{rec.get('symbol')}|{rec.get('opened_at')}"


def merge(existing: List[dict], new: List[dict]) -> List[dict]:
    """New rows replace stored rows with the same key (a trade can gain a tape_exit while open); order kept. Pure."""
    seen = {row_key(r): i for i, r in enumerate(existing)}
    out = list(existing)
    for r in new:
        k = row_key(r)
        if k in seen:
            out[seen[k]] = r
        else:
            seen[k] = len(out)
            out.append(r)
    return out


def build(ps_docs: List, rt_docs: List, stored: List[dict], min_n: int, min_days: int, min_against: int = 2) -> dict:
    fresh: List[dict] = []
    for d in ps_docs:
        fresh += [x for x in (normalise(r, "ps") for r in _rows_of(d)) if x]
    for d in rt_docs:
        fresh += [x for x in (normalise(r, "rt") for r in _rows_of(d)) if x]
    recs = merge(stored, fresh)
    return {"records": recs, "entry": entry_report(recs, min_n, min_days, min_against), "exit": exit_report(recs, min_n, min_days),
            "limits": {"min_n": min_n, "min_days": min_days}}


def render(rep: dict) -> str:
    e, x = rep["entry"], rep["exit"]
    L = ["ORDER-FLOW TAPE REPORT (read-only)", "",
         f"Trades read: {e['trades_total']}. With a RELIABLE entry tape: {e['trades_with_reliable_tape']}.", "", "ENTRY - result by adverse checks at entry"]
    for b in BUCKETS:
        s = e["buckets"][b]
        L.append(f"  against {b:>2}: n={s['n']:<4} days={s['days']:<3} win={s['win_rate_pct']}%  mean={s['mean']}  90% [{s['lo90']}, {s['hi90']}]")
    c = e["cutoff"]
    L += ["", f"  cut-off FLOW_GATE_MIN_AGAINST={c['min_against']}: allowed n={c['allowed']['n']} mean={c['allowed']['mean']}; "
          f"blocked n={c['blocked']['n']} mean={c['blocked']['mean']}",
          f"  verdict: {c['verdict']}" + (f"  (allowed - blocked = {c['diff']}, 90% [{c['lo90']}, {c['hi90']}])" if c["diff"] is not None else f" - {c['why']}")]
    if c["blocked"]["n"] == 0:
        L.append("  note: no trade with that many adverse checks exists. With FLOW_GATE_MODE=live such trades are refused; set log for a sample of sessions to judge the cut-off.")
    o = e["one_vs_zero"]
    L.append(f"  MIN_AGAINST=1 check: {o['verdict']}" + (f"  (0-adverse minus 1-adverse = {o['diff']}, 90% [{o['lo90']}, {o['hi90']}])" if o["diff"] is not None else f" - {o['why']}"))
    L += ["", "EXIT - tape tightening of stop / target",
          f"  tightened:     n={x['tightened']['n']} win={x['tightened']['win_rate_pct']}% mean={x['tightened']['mean']}",
          f"  not tightened: n={x['not_tightened']['n']} win={x['not_tightened']['win_rate_pct']}% mean={x['not_tightened']['mean']}",
          f"  stop exits after a tighten: {x['stop_exits_after_tighten']}, loss avoided vs the original stop (upper bound, sum of %): {x['avoided_loss_upper_bound_pct_total']}",
          f"  verdict: {x['verdict']}" + (f" - {x['why']}" if x.get("why") else ""), f"  {x['note']}"]
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ps", action="append", default=[])
    ap.add_argument("--rt", action="append", default=[])
    ap.add_argument("--store", default=None)
    ap.add_argument("--min-n", type=int, default=30)
    ap.add_argument("--min-days", type=int, default=5)
    ap.add_argument("--min-against", type=int, default=2)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    def _load(paths):
        docs = []
        for p in paths:
            try:
                with open(p, encoding="utf-8") as fh:
                    docs.append(json.load(fh))
            except (OSError, ValueError) as e:
                print(f"cannot read {p}: {e}", file=sys.stderr)
        return docs

    stored = load_store(a.store) if a.store else []
    rep = build(_load(a.ps), _load(a.rt), stored, a.min_n, a.min_days, a.min_against)
    if a.store:
        try:
            with open(a.store, "w", encoding="utf-8") as fh:
                for r in rep["records"]:
                    fh.write(json.dumps(r, separators=(",", ":"), default=str) + "\n")
        except OSError as e:
            print(f"cannot write {a.store}: {e}", file=sys.stderr)
    print(json.dumps({k: v for k, v in rep.items() if k != "records"}, indent=2) if a.json else render(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
