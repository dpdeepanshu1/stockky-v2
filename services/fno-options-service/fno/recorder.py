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
    """Append one snapshot. Returns the file path, or None when it could not be written (a disk error, or a value that cannot be written as JSON: round 302, it used to raise).
    round 302: when the file does not end in a newline (an earlier write was cut short: disk full, a kill mid-write) the new line starts on a fresh line, so the cut line stays the only
    one lost; before, the next good snapshot was appended to it and was lost as well."""
    try:
        path = snapshot_path(root, day_of(ts), chain.underlying)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        line = json.dumps({"ts": ts, "chain": chain.to_json(), **(extra or {})}, separators=(",", ":"))
        with _lock:
            prefix = ""
            try:
                with open(path, "rb") as rf:
                    if rf.seek(0, os.SEEK_END) > 0:
                        rf.seek(-1, os.SEEK_END)
                        if rf.read(1) != b"\n":
                            prefix = "\n"
            except FileNotFoundError:
                pass
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(prefix + line + "\n")
        return path
    except (OSError, TypeError, ValueError):
        return None


def read_day(root: str, day: str, underlying: str) -> Iterator[Dict[str, Any]]:
    """Yield the snapshots of one day in time order; a corrupt line (bad JSON, bad bytes, or a line that is not a snapshot object) is skipped, not raised.
    A file that cannot be opened yields nothing (round 302: it raised and stopped the whole replay)."""
    path = snapshot_path(root, day, underlying)
    rows: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict):
                    rows.append(r)
    except OSError:
        return
    rows.sort(key=lambda r: r.get("ts", 0) if isinstance(r.get("ts", 0), (int, float)) else 0)
    yield from rows


def list_days(root: str) -> List[str]:
    base = os.path.join(root, "snapshots")
    try:
        return sorted(d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d)))
    except OSError:
        return []
