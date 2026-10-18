"""_intraday_data.py - group441: read the 1-minute bar folders written by services/position-stocks-service/feed/bar_recorder.py.

    <BAR_DIR>/<YYYY-MM-DD>/<quote(SYMBOL)>.json  = {"v": 1, "symbol", "date", "tf": 1, "first_ts", "last_ts", "n", "backfilled", "bars": [[t, o, h, l, c, v], ...]}

    load_dir(path)                    -> {symbol: {date: Day}}   Day = {"bars", "complete", "coverage", "backfilled", "minutes"}
    previous_day(days, date, max_gap) -> the date of the latest earlier recorded day within max_gap calendar days, or None
    open_ts(date)                     -> epoch second of 09:15 IST on that date
Stdlib only. A damaged file is skipped and counted, never fatal. A day is `complete` only when it started within 5 minutes of the open and covers at least
80 % of the 375 session minutes (shared/market_structure/intraday.session_info): VWAP, the day range and the previous close mean nothing on a day that started late.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))

from market_structure import intraday as _it  # noqa: E402

_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
IST_OFFSET_S = 19800


def open_ts(date: str) -> float:
    """Epoch second of 09:15 IST on `date` (YYYY-MM-DD)."""
    d = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return d.timestamp() - IST_OFFSET_S + _it.SESSION_OPEN_S


def date_of(ts: float) -> str:
    return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=float(ts) + IST_OFFSET_S)).strftime("%Y-%m-%d")


def load_dir(path: str, symbols: Optional[List[str]] = None, stats: Optional[Dict[str, int]] = None) -> Dict[str, Dict[str, Dict[str, Any]]]:
    st = stats if stats is not None else {}
    st.setdefault("files", 0)
    st.setdefault("bad_files", 0)
    root = Path(path)
    want = {s.upper() for s in symbols} if symbols else None
    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    if not root.is_dir():
        return out
    for dd in sorted(p for p in root.iterdir() if p.is_dir() and _DAY.match(p.name)):
        for f in sorted(dd.glob("*.json")):
            sym = unquote(f.stem)
            if want is not None and sym.upper() not in want:
                continue
            try:
                doc = json.loads(f.read_text())
                bars = [tuple(float(x) for x in b[:6]) for b in doc["bars"]]
                if not bars:
                    raise ValueError("no bars")
            except Exception:  # noqa: BLE001
                st["bad_files"] += 1
                continue
            st["files"] += 1
            info = _it.session_info(bars)
            rec = next(iter(info.values()), None) if len(info) == 1 else None
            if rec is None:                                   # a file spanning two IST days cannot be a recorder file: treat as not complete
                rec = {"minutes": len(bars), "coverage": 0.0, "complete": False}
            out.setdefault(sym, {})[dd.name] = {"bars": bars, "complete": bool(rec["complete"]), "coverage": rec["coverage"],
                                                  "minutes": rec["minutes"], "backfilled": bool(doc.get("backfilled"))}
    return out


def previous_day(days: Dict[str, Any], date: str, max_gap: int = 6) -> Optional[str]:
    """Latest recorded date before `date`, at most `max_gap` calendar days earlier (the opening gate's own staleness limit is 6)."""
    d0 = datetime.strptime(date, "%Y-%m-%d")
    best = None
    for k in days:
        if k < date and (best is None or k > best):
            best = k
    if best is None:
        return None
    return best if (d0 - datetime.strptime(best, "%Y-%m-%d")).days <= max_gap else None
