"""fno/probecheck.py - Round 265: read the real-order probe's saved result and check its charges against Dhan's contract note, line by line.

The probe (scripts/fno_real_order_probe.py, fno/realprobe.py) writes data/fno_real_probe/probe_<time>.json on the host; docker-compose mounts that folder read-only at PROBE_DIR for this service.
`summarize` turns one result into plain facts (what was sent, what Dhan said, what filled, gross / charges / net); `compare` sets the model's six charge lines against the six numbers you copy
from the contract note and, for a line that differs, says which FNO_* rate to change and what the note implies. Pure and read-only: it never changes a rate, never confirms the readiness
item `probe_charges` for you (that stays your click), never sends anything. One probe is one data point: a matching note proves the model for that trade size, not for every price.
"""
from __future__ import annotations

import json
import math
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from . import costs, realprobe
from .params import Params

PROBE_DIR = "/data/fno_real_probe"
NAME_RE = re.compile(r"^probe_\d{8}_\d{6}\.json$")
LINES = ("brokerage", "stt", "exchange", "sebi", "gst", "stamp")
ROUNDED = ("stt", "stamp")                    # round 458 (E479): Dhan's contract note prints these two in whole rupees (STT 5.855 -> 6.00, stamp 0.119 -> 0.00)
LABELS = {"brokerage": "Brokerage", "stt": "STT", "exchange": "Exchange transaction", "sebi": "SEBI fee", "gst": "GST", "stamp": "Stamp duty"}
TOL = 0.05                                    # rupees: a contract note rounds each line to the paisa; five paise is rounding, not a wrong rate
EXIT_TEXT = {0: "round trip done (or a dry run)", 2: "refused, or the BUY was rejected: nothing is open", 3: "A POSITION IS STILL OPEN: close it in the Dhan app",
             4: "in doubt (an order's state, or Dhan shows flat but our SELLs sold less than was bought): look in the Dhan app", 5: "the BUY did not fill and was cancelled: nothing is open"}


def _num(x: Any) -> Optional[float]:
    if x is None or isinstance(x, bool):
        return None
    try:
        s = str(x).replace(",", "").replace("\u20b9", "").replace("Rs", "").strip()
        v = float(s) if s else None
        return v if v is not None and math.isfinite(v) else None       # round 307: "nan" / "inf" parse as floats and would reach the JSON answer as NaN (a 500)
    except (TypeError, ValueError):
        return None


def clean_note_lines(raw: Dict[str, Any]) -> Tuple[Dict[str, float], Optional[str]]:
    """Round 309: the contract-note lines the page sends, checked BEFORE they are stored. (lines, None) when fine; ({}, reason) when any value is not a finite number or is negative.
    pydantic accepts NaN / Infinity as a float and `nan < 0` is False, so such a line used to be saved, and `GET /fno/probe` then echoed it: the JSON answer could not be built (500) until another note replaced it."""
    out: Dict[str, float] = {}
    for k, v in (raw or {}).items():
        if v is None:
            continue
        x = _num(v)
        if x is None:
            return {}, f"{k}: not a finite number"
        if x < 0:
            return {}, "a charge cannot be negative"
        out[k] = x
    return out, None


def clean_saved(saved: Any) -> Optional[Dict[str, Any]]:
    """A stored note with any non-finite or non-numeric line dropped (a note saved before round 309 may hold one). None stays None."""
    if not isinstance(saved, dict):
        return None
    lines = saved.get("lines") if isinstance(saved.get("lines"), dict) else {}
    return {**saved, "lines": {k: x for k, x in ((k, _num(v)) for k, v in lines.items()) if x is not None}}


def list_files(dir_: str = PROBE_DIR) -> List[str]:
    """probe_<time>.json names, newest first (the stamp sorts as text)."""
    try:
        return sorted((f for f in os.listdir(dir_) if NAME_RE.match(f)), reverse=True)
    except OSError:
        return []


def attempted(probe: Dict[str, Any]) -> bool:
    """True when the run got as far as sending at least one order. A refused run (wrong time, no token, wide spread ...) and a dry run sent nothing: they are a log, not a result."""
    return bool(probe.get("live")) and any(isinstance(o, dict) for o in probe.get("orders") or [])


def _read(dir_: str, n: str) -> Optional[Dict[str, Any]]:
    try:
        with open(os.path.join(dir_, n), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def load(dir_: str = PROBE_DIR, name: Optional[str] = None) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """One saved result by name (only a real probe file name is accepted: no path can be injected), or the newest run that SENT an order.
    Round 269: a later refused or dry run must not hide the real round trip, so with no name the newest `attempted` probe wins; when none ever sent an order the newest file is returned
    (a refusal and its reason still show). An unreadable NEWEST file gives (None, None), as before; an unreadable older one is skipped. (None, None) when there is none."""
    if name is not None and not NAME_RE.match(str(name)):
        return None, None
    if name:
        d = _read(dir_, name)
        return (name, d) if d is not None else (None, None)
    newest: Tuple[Optional[str], Optional[Dict[str, Any]]] = (None, None)
    for i, n in enumerate(list_files(dir_)[:60]):
        d = _read(dir_, n)
        if d is None:
            if i == 0:
                return None, None
            continue
        if newest[0] is None:
            newest = (n, d)
        if attempted(d):
            return n, d
    return newest


def summarize(probe: Dict[str, Any], name: str = "") -> Dict[str, Any]:
    """Plain facts of one probe result. Never raises on a partial or old file."""
    orders = []
    hint = ""
    for o in probe.get("orders") or []:
        if not isinstance(o, dict):
            continue
        h = realprobe.dhan_hint(str(o.get("status") or ""), str(o.get("message") or "")) if (o.get("ok") is False or not o.get("filled_qty")) else ""
        hint = hint or h
        orders.append({"side": o.get("side"), "price": o.get("price"), "qty": o.get("qty"), "ok": o.get("ok"), "order_id": o.get("order_id"), "status": o.get("status"), "message": o.get("message"),
                       "filled_qty": o.get("filled_qty"), "avg_price": o.get("avg_price"), "hint": h})
    code = probe.get("exit_code")
    pnl = probe.get("pnl") if isinstance(probe.get("pnl"), dict) else None
    return {"file": name, "live": bool(probe.get("live")), "underlying": probe.get("underlying"), "side": probe.get("side"), "contract": probe.get("contract"),
            "exit_code": code, "reason": str(probe.get("reason") or "")[:400], "outcome": EXIT_TEXT.get(code, "unknown outcome") if probe.get("live") else "dry run: nothing was sent",
            "orders": orders, "bought": probe.get("bought"), "sold": probe.get("sold"), "dhan_net_position_after": probe.get("dhan_net_position_after"), "pnl": pnl, "dhan_hint": hint,
            "still_open": code == 3}


def expected_lines(probe: Dict[str, Any], params: Optional[Params] = None) -> Optional[Dict[str, Any]]:
    """What the contract note should show if our model is right: the six charge lines of the probe's own prices, plus the flat brokerage (and GST on it) of any extra SELL order that filled."""
    pnl = probe.get("pnl")
    if not isinstance(pnl, dict) or not isinstance(pnl.get("charges_detail"), dict):
        return None
    p = params or Params()
    d = {k: _num(pnl["charges_detail"].get(k)) for k in LINES}
    if any(v is None and pnl["charges_detail"].get(k) not in (None, "", 0) for k, v in d.items()):
        return None                                                    # round 307: a garbled charges line in an old / hand-edited file means "not comparable", never a crash
    d = {k: (v or 0.0) for k, v in d.items()}
    filled_sells = [o for o in probe.get("orders") or [] if isinstance(o, dict) and o.get("side") == "SELL" and o.get("filled_qty")]
    extra = max(0, len(filled_sells) - 1)
    d["brokerage"] += extra * p.brokerage_per_order
    d["gst"] += extra * p.brokerage_per_order * p.gst_pct / 100.0
    qty, ba, sa = _num(pnl.get("qty")) or 0.0, _num(pnl.get("bought_avg")) or 0.0, _num(pnl.get("sold_avg")) or 0.0
    return {"lines": d, "buy_turnover": ba * qty, "sell_turnover": sa * qty, "orders": 1 + len(filled_sells), "extra_sells": extra}


def _current(line: str, und: str, p: Params) -> Tuple[str, float]:
    if line == "exchange":
        return ("FNO_EXCH_TXN_PCT_BSE", p.exch_txn_pct_bse) if und == "SENSEX" else ("FNO_EXCH_TXN_PCT_NSE", p.exch_txn_pct_nse)
    return {"brokerage": ("FNO_BROKERAGE_PER_ORDER", p.brokerage_per_order), "stt": ("FNO_STT_SELL_PCT", p.stt_sell_pct), "sebi": ("FNO_SEBI_PCT", p.sebi_pct),
            "gst": ("FNO_GST_PCT", p.gst_pct), "stamp": ("FNO_STAMP_BUY_PCT", p.stamp_buy_pct)}[line]


def compare(probe: Dict[str, Any], note: Dict[str, Any], params: Optional[Params] = None, tol: float = TOL) -> Dict[str, Any]:
    """Our six lines against yours. verdict: MATCHED (all six given and within `tol`) | DIFFERS | INCOMPLETE (every given line matches, some missing) | NO_NOTE | NOT_COMPARABLE (the probe made no full round trip)."""
    exp = expected_lines(probe, params)
    if exp is None:
        return {"verdict": "NOT_COMPARABLE", "lines": [], "total": None, "tol": tol, "text": "This probe has no completed round trip (dry run, refused, or not filled), so there are no charges to compare."}
    p = params or Params()
    und = str(probe.get("underlying") or "NIFTY").upper()
    given = {k: _num((note or {}).get(k)) for k in LINES}
    both = exp["buy_turnover"] + exp["sell_turnover"]

    def implied(k: str) -> Optional[float]:
        v = given[k]
        if v is None:
            return None
        if k == "brokerage":
            return v / exp["orders"] if exp["orders"] else None
        if k == "stt":
            return v / exp["sell_turnover"] * 100.0 if exp["sell_turnover"] > 0 else None
        if k in ("exchange", "sebi"):
            return v / both * 100.0 if both > 0 else None
        if k == "stamp":
            return v / exp["buy_turnover"] * 100.0 if exp["buy_turnover"] > 0 else None
        base = sum(given[x] if given[x] is not None else exp["lines"][x] for x in ("brokerage", "exchange", "sebi"))      # gst
        return v / base * 100.0 if base > 0 else None

    out: List[Dict[str, Any]] = []
    for k in LINES:
        exact, n = round(exp["lines"][k], 2), given[k]
        m = costs.rupee(exp["lines"][k]) if k in ROUNDED else exact          # what the note should print; a note that shows the paisa figure (a P&L report) also matches `exact`
        ok = None if n is None else (abs(n - m) <= tol or abs(n - exact) <= tol)
        row: Dict[str, Any] = {"line": k, "label": LABELS[k], "model": m, "model_exact": exact, "rounded": k in ROUNDED, "note": n, "diff": None if n is None else round(n - (exact if ok and abs(n - m) > tol else m), 2), "ok": ok, "param": None, "implied": None, "current": None}
        if n is not None and not row["ok"]:
            param, cur = _current(k, und, p)
            imp = implied(k)
            row.update(param=param, current=cur, implied=None if imp is None else round(imp, 6))
        out.append(row)
    tot_note = _num((note or {}).get("total"))
    tot_exact = sum(exp["lines"].values())
    tot_rounded = sum(r["model"] for r in out)                                # the sum of the printed lines, as the note adds them
    total = None if tot_note is None else {"model": round(tot_rounded, 2), "note": tot_note, "diff": round(tot_note - tot_rounded, 2), "ok": min(abs(tot_note - tot_rounded), abs(tot_note - tot_exact)) <= tol * 2}
    n_given = sum(1 for r in out if r["note"] is not None)
    bad = [r for r in out if r["ok"] is False] + ([total] if total and not total["ok"] else [])
    verdict = "NO_NOTE" if n_given == 0 and total is None else "DIFFERS" if bad else "MATCHED" if n_given == len(LINES) else "INCOMPLETE"
    if verdict == "MATCHED":
        text = f"All six lines are within Rs {tol:.2f} of the contract note for this one trade (STT and stamp duty compared as whole rupees, the way Dhan prints them). That is evidence for this size and price, not proof for every trade."
    elif verdict == "DIFFERS":
        text = "These lines differ: " + "; ".join(f"{r['label']} (ours {r['model']:.2f}, note {r['note']:.2f})" for r in out if r["ok"] is False) + ". The rate the note implies is next to each; a rounded paisa makes it approximate, so check it against Dhan's published rate before changing anything."
    elif verdict == "INCOMPLETE":
        text = "The lines you gave match, but " + ", ".join(r["label"] for r in out if r["note"] is None) + " " + ("is" if sum(1 for r in out if r["note"] is None) == 1 else "are") + " still missing: it is not a match until all six are compared."
    else:
        text = "Enter the six charge lines from Dhan's contract note (Reports > P&L / contract note) to compare."
    if exp["extra_sells"]:
        text += f" (The SELL needed {exp['extra_sells']} extra order(s): one more flat brokerage + GST each is already in the numbers above.)"
    return {"verdict": verdict, "lines": out, "total": total, "tol": tol, "text": text, "orders": exp["orders"]}


def summary_text(s: Dict[str, Any], c: Optional[Dict[str, Any]] = None) -> str:
    """The short plain-text summary kept next to the probe file (plan item A4)."""
    L = [f"F&O real-order probe summary: {s.get('file') or '(no file)'}", f"{'LIVE' if s.get('live') else 'DRY RUN'}  {s.get('underlying')} {s.get('side')}  {s.get('contract') or ''}".strip(),
         f"outcome: {s.get('outcome')}"]
    for o in s.get("orders") or []:
        L.append(f"  {o.get('side')}: limit {o.get('price')} x {o.get('qty')}  status {o.get('status')}  filled {o.get('filled_qty')} at {o.get('avg_price')}  order id {o.get('order_id')}" + (f"  message: {o.get('message')}" if o.get("message") else ""))
    if s.get("dhan_hint"):
        L.append(f"Dhan error, in plain words: {s['dhan_hint']}")
    pn = s.get("pnl")
    if pn:
        L.append(f"gross Rs {pn.get('gross')}  charges Rs {pn.get('charges')}  net Rs {pn.get('net')}  (market move {pn.get('market_move')}, spread BUY -{pn.get('spread_buy')}, spread SELL -{pn.get('spread_sell')})")
    if c:
        L.append(f"charges vs contract note: {c['verdict']}")
        for r in c.get("lines") or []:
            L.append(f"  {r['label']:<22} ours {r['model']:>9.2f}  note {'-' if r['note'] is None else format(r['note'], '9.2f'):>9}" + ("" if r["ok"] is not False else f"  DIFF {r['diff']:+.2f}  -> {r['param']}: now {r['current']}, note implies {r['implied']}"))
        if c.get("total"):
            L.append(f"  {'TOTAL':<22} ours {c['total']['model']:>9.2f}  note {c['total']['note']:>9.2f}")
        L.append(c["text"])
    return "\n".join(L) + "\n"


def write_summary(dir_: str, name: str, s: Dict[str, Any], c: Optional[Dict[str, Any]] = None) -> str:
    """Writes <probe name>_summary.txt beside the probe file (host side only: the service mounts the folder read-only)."""
    path = os.path.join(dir_, name[:-5] + "_summary.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(summary_text(s, c))
    return path
