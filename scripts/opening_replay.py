#!/usr/bin/env python3
"""opening_replay.py - group393: what would the group 392 opening protection have done to YOUR real opening trades?

  curl -s "http://127.0.0.1:8006/trades/history?limit=1000" > hist_$(date +%F).json     # position-stocks trade ledger (kept ~3 days by retention)
  python3 scripts/opening_replay.py hist_*.json [--size-mult 0.5] [--pause-stopouts 2] [--until 09:45] [--min-n 30] [--stop-mults 0.5,0.75] [--out DIR]

Input: one or more files, each the /trades/history response (an object with a "trades" list) or a bare list of trades. Several files are merged by trade id,
so run the curl once a day and the replay sees more than the 3-day retention window.
Trades replayed: SETTLED ones (real exit price and P&L, not ERROR / pending reconcile) opened between 09:15 and --until (IST). Per IST day, in opened_at order:
  size-only   quantity cut to --size-mult (nearest share, trades of 1 share unchanged, never under 1), P&L scaled by the new/old quantity;
  pause-only  a trade is skipped when --pause-stopouts or more STOP_HIT / STOP_BREACH_EXIT trades (of the trades that scenario took) had already CLOSED before it opened;
  both        the two together (what group 392 does).
READ THIS BEFORE TRUSTING IT: P&L scaling by quantity ignores slippage and charges that depend on size, and a skipped trade is assumed to leave no trace (the freed
slot could have been used by a later candidate, which is not replayed). The ledger is a few days deep, so the sample is small: below --min-n opening trades the verdict is
`insufficient_sample` and the numbers are only a first look. No significance test; the verdict is the sign of the P&L difference. Stdlib only.
Stop section (group395): when the export carries the trade's own stop (adaptive_stop_pct, max_gain_pct / max_drawdown_pct, optionally stop_basis = bars / opening_plan / legacy), the
report adds, per stop basis: stop-outs, the median stop %, stop-outs that had already run at least 0.5R in favour, and non-stop-out trades whose drawdown came within 0.2R of their
stop. It also replays a TIGHTER stop (--stop-mults 0.5,0.75 = that fraction of the trade's own stop): a trade whose recorded drawdown reached the tighter level would certainly have
been stopped there, so that what-if is exact apart from tick sampling, slippage and charges. A WIDER stop cannot be replayed (the ledger has no price path after the real stop), so
only the indicators speak to "was the stop too tight". Rows exported before group 395 have no stop data and are counted but not used.
Output: prints markdown; with --out also writes opening_replay.json and opening_replay.md.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

IST = timezone(timedelta(hours=5, minutes=30))
STOP_STATUSES = ("STOP_HIT", "STOP_BREACH_EXIT")
BAD_STATUSES = ("ERROR", "OPEN", "EXIT_LEGS_REJECTED")


def parse_ts(v) -> Optional[datetime]:
    try:
        if not v:
            return None
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def hhmm(v: str, default: dtime) -> dtime:
    try:
        h, m = str(v).strip().split(":")
        return dtime(int(h), int(m))
    except (TypeError, ValueError):
        return default


def load(paths: List[str]) -> List[dict]:
    by_id: Dict[Any, dict] = {}
    for p in paths:
        data = json.loads(Path(p).read_text(encoding="utf-8"))
        rows = data.get("trades") if isinstance(data, dict) else data
        for r in rows or []:
            if isinstance(r, dict):
                by_id[r.get("id", (r.get("symbol"), r.get("opened_at")))] = r
    return list(by_id.values())


def num(v) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def settled(r: dict) -> bool:
    st = str(r.get("status") or "").upper()
    if st in BAD_STATUSES or "PENDING_RECONCILE" in str(r.get("error_message") or "").upper():
        return False
    try:
        return r.get("exit_price") is not None and r.get("realized_pnl") is not None and float(r.get("quantity") or 0) > 0
    except (TypeError, ValueError):
        return False


def opening_trades(rows: List[dict], until: dtime) -> List[dict]:
    out = []
    for r in rows:
        o, c = parse_ts(r.get("opened_at")), parse_ts(r.get("closed_at"))
        if not settled(r) or o is None or c is None:
            continue
        oi = o.astimezone(IST)
        if dtime(9, 15) <= oi.time() < until and oi.weekday() < 5:
            out.append({"id": r.get("id"), "symbol": r.get("symbol"), "status": str(r.get("status")).upper(), "qty": float(r["quantity"]),
                        "pnl": float(r["realized_pnl"]), "opened": o, "closed": c, "day": oi.date().isoformat(),
                        "entry": num(r.get("entry_price")), "stop_pct": num(r.get("adaptive_stop_pct")), "gain_pct": num(r.get("max_gain_pct")),
                        "dd_pct": num(r.get("max_drawdown_pct")), "basis": str(r.get("stop_basis") or "unknown")})
    return sorted(out, key=lambda t: t["opened"])


def scaled_qty(qty: float, mult: float) -> float:
    if mult >= 1.0 or qty <= 1:
        return qty
    return max(1.0, float(int(qty * mult + 0.5)))


def simulate(trades: List[dict], mult: float, limit: int) -> dict:
    """One scenario: mult 1.0 = no size cut, limit 0 = no pause. Per day, in opened order."""
    taken: List[dict] = []
    skipped: List[dict] = []
    for t in trades:
        if limit > 0:
            n_stop = sum(1 for x in taken if x["day"] == t["day"] and x["status"] in STOP_STATUSES and x["closed"] <= t["opened"])
            if n_stop >= limit:
                skipped.append(t)
                continue
        q = scaled_qty(t["qty"], mult)
        taken.append({**t, "pnl": t["pnl"] * q / t["qty"], "qty": q})
    pnls = [x["pnl"] for x in taken]
    return {"trades": len(taken), "skipped": len(skipped), "pnl": round(sum(pnls), 2), "wins": sum(1 for p in pnls if p > 0),
            "stop_outs": sum(1 for x in taken if x["status"] in STOP_STATUSES), "worst": round(min(pnls), 2) if pnls else 0.0,
            "skipped_pnl_if_taken": round(sum(x["pnl"] for x in skipped), 2)}


def _median(xs: List[float]) -> Optional[float]:
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return None
    return round(xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0, 3)


def parse_mults(v: str) -> List[float]:
    out: List[float] = []
    for part in str(v or "").split(","):
        try:
            k = float(part)
        except ValueError:
            continue
        if 0.1 <= k < 1.0 and k not in out:
            out.append(k)
    return out


def stop_report(trades: List[dict], mults=(0.5, 0.75)) -> dict:
    """Stop indicators + the tighter-stop what-if over the replayed opening trades that carry their own stop. See the module docstring."""
    usable = [t for t in trades if t.get("entry") and t["entry"] > 0 and t.get("stop_pct") and t["stop_pct"] > 0
              and t.get("dd_pct") is not None and t.get("gain_pct") is not None]
    groups: Dict[str, List[dict]] = {"all": usable}
    for t in usable:
        groups.setdefault(t["basis"], []).append(t)
    rep: Dict[str, Any] = {"opening_trades": len(trades), "with_stop_data": len(usable), "mults": list(mults), "groups": {}}
    for name, ts in groups.items():
        stopped = [t for t in ts if t["status"] in STOP_STATUSES]
        rest = [t for t in ts if t["status"] not in STOP_STATUSES]
        g = {"trades": len(ts), "stop_outs": len(stopped), "median_stop_pct": _median([t["stop_pct"] for t in ts]),
             "stopped_after_running_half_r": sum(1 for t in stopped if t["gain_pct"] >= 0.5 * t["stop_pct"]),
             "survivors_within_02r_of_stop": sum(1 for t in rest if t["dd_pct"] <= -0.8 * t["stop_pct"]),
             "pnl_actual": round(sum(t["pnl"] for t in ts), 2), "tighter": []}
        for k in mults:
            hit_ids = {id(t) for t in ts if t["dd_pct"] <= -k * t["stop_pct"]}
            new_pnl = sum(-(k * t["stop_pct"] / 100.0) * t["entry"] * t["qty"] if id(t) in hit_ids else t["pnl"] for t in ts)
            g["tighter"].append({"mult": k, "stopped_at_tighter_level": len(hit_ids), "pnl_tighter": round(new_pnl, 2),
                                 "pnl_difference": round(new_pnl - g["pnl_actual"], 2)})
        rep["groups"][name] = g
    return rep


def replay(rows: List[dict], mult: float = 0.5, limit: int = 2, until: str = "09:45", min_n: int = 30, stop_mults=(0.5, 0.75)) -> dict:
    tr = opening_trades(rows, hhmm(until, dtime(9, 45)))
    sc = {"actual": simulate(tr, 1.0, 0), "size_only": simulate(tr, mult, 0), "pause_only": simulate(tr, 1.0, limit), "both": simulate(tr, mult, limit)}
    diff = round(sc["both"]["pnl"] - sc["actual"]["pnl"], 2)
    if len(tr) < min_n:
        verdict = "insufficient_sample"
    else:
        verdict = "protection_helped" if diff > 0 else "protection_hurt" if diff < 0 else "no_change"
    return {"opening_trades": len(tr), "days": len({t["day"] for t in tr}), "size_mult": mult, "pause_stopouts": limit, "until": until,
            "min_n": min_n, "scenarios": sc, "pnl_difference_both_vs_actual": diff, "verdict": verdict, "stops": stop_report(tr, stop_mults)}


def stops_markdown(st: dict, min_n: int) -> List[str]:
    if not st or not st.get("with_stop_data"):
        n = (st or {}).get("opening_trades", 0)
        return ["## Stops", "", f"No stop data in these exports ({n} opening trades): they predate group 395. Export again from the new build: rows still in the ledger then carry their stop and drawdown figures too (stop basis \"unknown\" for trades opened before the build), and new trades are tagged bars / opening_plan / legacy.", ""]
    lines = ["## Stops", "", f"{st['with_stop_data']} of {st['opening_trades']} opening trades carry their own stop (stop basis: bars = live 5-minute bars, opening_plan = group 391, legacy = old path, unknown = not recorded).",
             "", "| basis | trades | stop-outs | median stop % | stopped after running 0.5R | survivors within 0.2R of stop | P&L |", "|---|---|---|---|---|---|---|"]
    for name, g in st["groups"].items():
        lines.append(f"| {name} | {g['trades']} | {g['stop_outs']} | {g['median_stop_pct']} | {g['stopped_after_running_half_r']} | {g['survivors_within_02r_of_stop']} | {g['pnl_actual']} |")
    if st.get("mults"):
        lines += ["", "Tighter stop what-if (a trade whose recorded drawdown reached the tighter level is counted as stopped there; exact apart from tick sampling, slippage and charges):", "",
                  "| basis | stop x | stopped at tighter level | P&L tighter | difference vs actual |", "|---|---|---|---|---|"]
        for name, g in st["groups"].items():
            for w in g["tighter"]:
                lines.append(f"| {name} | {w['mult']} | {w['stopped_at_tighter_level']} | {w['pnl_tighter']} | {w['pnl_difference']} |")
    lines += ["", "A WIDER stop cannot be replayed: the ledger has no price path after the real stop. \"Stopped after running 0.5R\" and \"survivors within 0.2R\" are indicators, not a P&L."]
    if st["with_stop_data"] < min_n:
        lines.append(f"Fewer than {min_n} opening trades with stop data = a first look only.")
    lines.append("")
    return lines


def to_markdown(rep: dict) -> str:
    s = rep["scenarios"]
    lines = [f"# Opening protection replay", "",
             f"{rep['opening_trades']} settled trades opened 09:15-{rep['until']} IST over {rep['days']} day(s); size x{rep['size_mult']}, pause after {rep['pause_stopouts']} stop-outs.",
             "", "| scenario | trades | skipped | P&L | wins | stop-outs | worst |", "|---|---|---|---|---|---|---|"]
    for k in ("actual", "size_only", "pause_only", "both"):
        v = s[k]
        lines.append(f"| {k} | {v['trades']} | {v['skipped']} | {v['pnl']} | {v['wins']} | {v['stop_outs']} | {v['worst']} |")
    lines += ["", f"P&L difference (both vs actual): {rep['pnl_difference_both_vs_actual']}", f"Verdict: **{rep['verdict']}** (fewer than {rep['min_n']} opening trades = insufficient_sample; no significance test)", ""]
    lines += stops_markdown(rep.get("stops") or {}, rep["min_n"])
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("files", nargs="+")
    ap.add_argument("--size-mult", type=float, default=0.5)
    ap.add_argument("--pause-stopouts", type=int, default=2)
    ap.add_argument("--until", default="09:45")
    ap.add_argument("--min-n", type=int, default=30)
    ap.add_argument("--stop-mults", default="0.5,0.75", help="tighter-stop what-if: fractions of each trade's own stop (0.1 to 0.99), comma list; blank = skip")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    rep = replay(load(a.files), a.size_mult, a.pause_stopouts, a.until, a.min_n, parse_mults(a.stop_mults))
    md = to_markdown(rep)
    print(md)
    if a.out:
        d = Path(a.out)
        d.mkdir(parents=True, exist_ok=True)
        (d / "opening_replay.json").write_text(json.dumps(rep, indent=2), encoding="utf-8")
        (d / "opening_replay.md").write_text(md, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
