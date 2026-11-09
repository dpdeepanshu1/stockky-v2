"""fno/recorder.py - saves option-chain snapshots to disk (one JSON line per snapshot, one file per day and underlying) so the replay backtest can run on REAL
recorded premiums. Layout: <DATA_DIR>/snapshots/<YYYY-MM-DD>/<UNDERLYING>.jsonl . Pure file I/O, stdlib; a failed write never stops the engine."""
from __future__ import annotations

import json
import os
import threading
from typing import Any, Dict, Iterator, List, Optional

from .models import Chain
from .store import day_of

_lock = threading.Lock()


def snapshot_path(root: str, day: str, underlying: str) -> str:
    return os.path.join(root, "snapshots", day, f"{underlying.upper()}.jsonl")


def record(root: str, chain: Chain, ts: float, extra: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Append one snapshot. Returns the file path, or None when it could not be written."""
    try:
        path = snapshot_path(root, day_of(ts), chain.underlying)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        line = json.dumps({"ts": ts, "chain": chain.to_json(), **(extra or {})}, separators=(",", ":"))
        with _lock, open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return path
    except OSError:
        return None


def read_day(root: str, day: str, underlying: str) -> Iterator[Dict[str, Any]]:
    """Yield the snapshots of one day in time order; a corrupt line is skipped, not raised."""
    path = snapshot_path(root, day, underlying)
    if not os.path.exists(path):
        return
    rows: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    rows.sort(key=lambda r: r.get("ts", 0))
    yield from rows


def list_days(root: str) -> List[str]:
    base = os.path.join(root, "snapshots")
    try:
        return sorted(d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d)))
    except OSError:
        return []
