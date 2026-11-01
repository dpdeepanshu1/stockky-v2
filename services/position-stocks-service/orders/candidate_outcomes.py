"""E272: candidate outcomes - what price did AFTER every scan candidate (traded or skipped), at +5, +15, +30 and +60 minutes.

Pure labelling over the recorded 1-minute bars (`[[epoch_s, open, high, low, close, ...], ...]`, the same file the gate scorecard reads) plus one function that
joins `scalp_candidate_log` rows with the bar files and saves a day file beside bar-records (`<bars_dir>/outcomes/<YYYY-MM-DD>.json`). Live input for the
learned values (setup_stats, adaptive params): a skipped row fills a cell the same way a trade does, so a cell gets thousands of samples, not 22.
`label()` never raises; a candidate without bars after it is labelled `no_bars` and counted, never guessed. Run: scripts/candidate_outcomes_job.py.
"""
from __future__ import annotations

import calendar
import json
import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

HORIZONS_MIN = (5, 15, 30, 60)
DEDUPE_BUCKET_S = 300


def _bar_idx(bars: List[list], t0: float) -> Optional[int]:
    idx = None
    for i, b in enumerate(bars):
        if b[0] <= t0:
            idx = i
        else:
            break
    return idx


def label(bars: Optional[List[list]], t0: float, side: str = "LONG") -> Dict[str, Any]:
    """Forward moves in % from the close of the last bar at or before t0. For SHORT the sign is flipped (positive = the short would have gained).
    Keys: status ok | no_bars; r5 r15 r30 r60; mfe60 (best move in the trade's favour within 60 min), mae60 (worst move against it, <= 0)."""
    try:
        if not bars:
            return {"status": "no_bars"}
        idx = _bar_idx(bars, t0)
        if idx is None or idx >= len(bars) - 1:
            return {"status": "no_bars"}
        entry = float(bars[idx][4])
        if entry <= 0:
            return {"status": "no_bars"}
        sgn = -1.0 if str(side).upper() == "SHORT" else 1.0
        out: Dict[str, Any] = {"status": "ok"}
        for h in HORIZONS_MIN:
            j = idx
            for k in range(idx, len(bars)):
                if bars[k][0] <= t0 + h * 60:
                    j = k
                else:
                    break
            out[f"r{h}"] = None if bars[-1][0] + 60 < t0 + h * 60 else round(sgn * (float(bars[j][4]) / entry - 1.0) * 100.0, 4)   # None: data ends first
        hi = lo = 0.0
        for k in range(idx + 1, len(bars)):
            if bars[k][0] > t0 + 3600:
                break
            up = (float(bars[k][2]) / entry - 1.0) * 100.0
            dn = (float(bars[k][3]) / entry - 1.0) * 100.0
            hi, lo = max(hi, up), min(lo, dn)
        out["mfe60"] = round(hi if sgn > 0 else -lo, 4)
        out["mae60"] = round(lo if sgn > 0 else -hi, 4)
        return out
    except Exception:  # noqa: BLE001
        return {"status": "no_bars"}


def _gate_key(reason: Optional[str], decision: Optional[str]) -> str:
    if decision == "ENTERED":
        return "ENTERED"
    r = (reason or "").strip()
    return ":".join(r.split(":")[:2])[:60] if r else "SKIPPED"


def label_rows(rows: Iterable[Dict[str, Any]], load_bars: Callable[[str, str], Optional[List[list]]], dedupe: bool = True) -> Dict[str, Any]:
    """rows: dicts with symbol, decision, reason, created_ts (epoch seconds, UTC), optional side. load_bars(symbol, 'YYYY-MM-DD' IST day) -> bars or None.
    Returns {"rows": [...labelled...], "counts": {"rows", "labelled", "no_bars", "deduped"}}. One row per (symbol, gate, 5-minute bucket) when dedupe."""
    seen = set()
    out: List[Dict[str, Any]] = []
    c = {"rows": 0, "labelled": 0, "no_bars": 0, "deduped": 0}
    for r in rows or []:
        try:
            c["rows"] += 1
            ts = float(r["created_ts"])
            sym = str(r["symbol"]).upper()
            gate = _gate_key(r.get("reason"), r.get("decision"))
            if dedupe:
                key = (sym, gate, int(ts // DEDUPE_BUCKET_S))
                if key in seen:
                    c["deduped"] += 1
                    continue
                seen.add(key)
            day = datetime.fromtimestamp(ts + 19800, timezone.utc).strftime("%Y-%m-%d")
            lab = label(load_bars(sym, day), ts, r.get("side") or "LONG")
            c["labelled" if lab.get("status") == "ok" else "no_bars"] += 1
            out.append({"symbol": sym, "ts": int(ts), "gate": gate, "decision": r.get("decision"), "side": (r.get("side") or "LONG"), **lab})
        except Exception:  # noqa: BLE001
            continue
    return {"rows": out, "counts": c}


def file_loader(bars_dir: str) -> Callable[[str, str], Optional[List[list]]]:
    cache: Dict[tuple, Any] = {}

    def _load(sym: str, day: str):
        k = (sym, day)
        if k not in cache:
            try:
                with open(os.path.join(bars_dir, day, f"{sym}.json"), encoding="utf-8") as f:
                    cache[k] = json.load(f)["bars"]
            except Exception:  # noqa: BLE001
                cache[k] = None
        return cache[k]
    return _load


def save_day(bars_dir: str, day: str, result: Dict[str, Any]) -> Optional[str]:
    """Write <bars_dir>/outcomes/<day>.json (atomic replace). Returns the path, or None when the directory is not writable."""
    try:
        d = os.path.join(bars_dir, "outcomes")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{day}.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(result, f, separators=(",", ":"))
        os.replace(tmp, path)
        return path
    except Exception:  # noqa: BLE001
        return None


def run_day(rows: Iterable[Dict[str, Any]], bars_dir: str, day: str) -> Dict[str, Any]:
    """Label `rows` and save the day file. Returns the counts plus the saved path."""
    res = label_rows(rows, file_loader(bars_dir))
    return {**res["counts"], "path": save_day(bars_dir, day, res)}


def rows_from_log(session: Any, models: Any, day: str) -> List[Dict[str, Any]]:
    """scalp_candidate_log rows of one IST day as plain dicts (created_at is naive UTC)."""
    from datetime import timedelta
    d0 = datetime.strptime(day, "%Y-%m-%d") - timedelta(hours=5, minutes=30)
    M = models.ScalpCandidateLog
    q = session.query(M).filter(M.created_at >= d0, M.created_at < d0 + timedelta(days=1)).order_by(M.id)
    return [{"symbol": x.symbol, "decision": x.decision, "reason": x.reason, "created_ts": calendar.timegm(x.created_at.timetuple()),
             "side": "SHORT" if str(x.reason or "").upper().startswith("SHORT_") else "LONG"} for x in q.all()]
