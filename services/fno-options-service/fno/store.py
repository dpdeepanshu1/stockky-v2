"""fno/store.py - the service's own small database (SQLite, one file in FNO_DATA_DIR, a docker volume). Stdlib only.

Why SQLite and not the shared Oracle / Postgres engine of the trading services: this service keeps a journal of positions and events, a few thousand rows a month,
and the same file doubles as the offline evidence that the backtests and the Calibration page read. State survives a restart; losing the volume loses only the journal.
Tables: positions (one JSON document per position, plus indexed columns), events (signals / decisions / errors), kv (small settings), snapshots (index of chain files)."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .paper import CLOSED, OPEN, Position

_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT NOT NULL, underlying TEXT NOT NULL, opt_type TEXT NOT NULL, status TEXT NOT NULL,
  opened_ts REAL NOT NULL, closed_ts REAL, day TEXT NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_positions_day ON positions(day, mode);
CREATE INDEX IF NOT EXISTS ix_positions_status ON positions(status, mode);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL, underlying TEXT, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_events_kind ON events(kind, ts);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, day TEXT NOT NULL, underlying TEXT NOT NULL, expiry TEXT NOT NULL, path TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_snap_day ON snapshots(day, underlying);
"""


IST = timezone(timedelta(hours=5, minutes=30))


def day_of(ts: float) -> str:
    """The trading day (IST calendar date) of an epoch timestamp, whatever timezone the container runs in."""
    return datetime.fromtimestamp(ts, IST).strftime("%Y-%m-%d")


class Store:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, timeout=15, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ── positions ──
    def insert_position(self, pos: Position) -> int:
        with self._lock:
            cur = self._db.execute("INSERT INTO positions(mode, underlying, opt_type, status, opened_ts, closed_ts, day, data) VALUES (?,?,?,?,?,?,?,?)",
                                   (pos.mode, pos.underlying, pos.opt_type, pos.status, pos.entry_ts, pos.exit_ts, day_of(pos.entry_ts), json.dumps(pos.to_json())))
            self._db.commit()
            pos.id = int(cur.lastrowid)
            self._db.execute("UPDATE positions SET data=? WHERE id=?", (json.dumps(pos.to_json()), pos.id))
            self._db.commit()
            return pos.id

    def update_position(self, pos: Position) -> None:
        if pos.id is None:
            raise ValueError("position has no id; insert_position first")
        with self._lock:
            self._db.execute("UPDATE positions SET status=?, closed_ts=?, data=? WHERE id=?", (pos.status, pos.exit_ts, json.dumps(pos.to_json()), pos.id))
            self._db.commit()

    @staticmethod
    def _pos(row: sqlite3.Row) -> Position:
        p = Position.from_json(json.loads(row["data"]))
        p.id = int(row["id"])
        return p

    def get_position(self, pid: int) -> Optional[Position]:
        with self._lock:
            r = self._db.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone()
        return self._pos(r) if r else None

    def open_positions(self, mode: Optional[str] = None) -> List[Position]:
        q, a = "SELECT * FROM positions WHERE status=?", [OPEN]
        if mode:
            q += " AND mode=?"; a.append(mode)
        with self._lock:
            rows = self._db.execute(q + " ORDER BY id", a).fetchall()
        return [self._pos(r) for r in rows]

    def positions(self, mode: Optional[str] = None, day: Optional[str] = None, since_day: Optional[str] = None, limit: int = 500) -> List[Position]:
        q, a = "SELECT * FROM positions WHERE 1=1", []
        if mode:
            q += " AND mode=?"; a.append(mode)
        if day:
            q += " AND day=?"; a.append(day)
        if since_day:
            q += " AND day>=?"; a.append(since_day)
        with self._lock:
            rows = self._db.execute(q + " ORDER BY id DESC LIMIT ?", a + [int(limit)]).fetchall()
        return [self._pos(r) for r in rows]

    # ── events ──
    def add_event(self, kind: str, data: Dict[str, Any], underlying: str = "", ts: Optional[float] = None) -> None:
        with self._lock:
            self._db.execute("INSERT INTO events(ts, kind, underlying, data) VALUES (?,?,?,?)", (ts or time.time(), kind, underlying, json.dumps(data, default=str)))
            self._db.commit()

    def events(self, kind: Optional[str] = None, limit: int = 200, underlying: Optional[str] = None) -> List[Dict[str, Any]]:
        q, a = "SELECT * FROM events WHERE 1=1", []
        if kind:
            q += " AND kind=?"; a.append(kind)
        if underlying:
            q += " AND underlying=?"; a.append(underlying)
        with self._lock:
            rows = self._db.execute(q + " ORDER BY id DESC LIMIT ?", a + [int(limit)]).fetchall()
        return [{"id": r["id"], "ts": r["ts"], "kind": r["kind"], "underlying": r["underlying"], **json.loads(r["data"])} for r in rows]

    def prune_events(self, keep_days: int = 30) -> int:
        cutoff = time.time() - keep_days * 86400
        with self._lock:
            n = self._db.execute("DELETE FROM events WHERE ts < ?", (cutoff,)).rowcount
            self._db.commit()
        return n

    # ── kv ──
    def kv_get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            r = self._db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default

    def kv_set(self, key: str, value: Any) -> None:
        with self._lock:
            self._db.execute("INSERT INTO kv(key, value, updated) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
                             (key, json.dumps(value, default=str), time.time()))
            self._db.commit()

    # ── snapshots ──
    def add_snapshot(self, ts: float, underlying: str, expiry: str, path: str) -> None:
        with self._lock:
            self._db.execute("INSERT INTO snapshots(ts, day, underlying, expiry, path) VALUES (?,?,?,?,?)", (ts, day_of(ts), underlying, expiry, path))
            self._db.commit()

    def snapshot_days(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT day, underlying, COUNT(*) n, MIN(ts) first_ts, MAX(ts) last_ts FROM snapshots GROUP BY day, underlying ORDER BY day").fetchall()
        return [dict(r) for r in rows]

    def closed_from_earlier(self, day: str, mode: str) -> List[Position]:
        """Round 249: positions entered on an EARLIER day that were closed during `day` (IST). Their P&L is booked on the entry day (reports unchanged), but the day's loss limit has to see it."""
        try:
            start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=IST).timestamp()
        except ValueError:
            return []
        with self._lock:
            rows = self._db.execute("SELECT * FROM positions WHERE mode=? AND status=? AND day<? AND closed_ts>=? AND closed_ts<?", (mode, CLOSED, day, start, start + 86400)).fetchall()
        return [self._pos(r) for r in rows]

    # ── summaries ──
    def day_summary(self, day: str, mode: str) -> Dict[str, Any]:
        ps = self.positions(mode=mode, day=day, limit=2000)
        closed = [p for p in ps if p.status == CLOSED]
        stop_ts = max((p.exit_ts for p in closed if p.exit_reason == "SL" and p.exit_ts), default=None)
        same: Dict[str, int] = {}
        for p in ps:
            if p.status == OPEN:
                same[p.key] = same.get(p.key, 0) + 1
        carried = [p for p in self.open_positions(mode) if day_of(p.entry_ts) < day]     # round 248: still open from an EARLIER day: it counts against today's open-position and same-side gates
        for p in carried:
            same[p.key] = same.get(p.key, 0) + 1
        roots = [p for p in ps if p.parent_id is None]        # a partial-exit child row is part of its parent's trade, not a new trade
        fam: Dict[Any, List[Position]] = {}                    # win / loss is judged per TRADE: the parent and its partial-exit children together, once the whole trade is closed
        for q in ps:
            fam.setdefault(q.parent_id if q.parent_id is not None else q.id, []).append(q)
        done = [sum(x.net_pnl or 0.0 for x in rows) for rows in fam.values() if all(x.status == CLOSED for x in rows)]
        return {"trades": len(roots), "open": sum(1 for p in ps if p.status == OPEN) + len(carried), "carried_open": len(carried), "carried_closed_net": round(sum(p.net_pnl or 0.0 for p in self.closed_from_earlier(day, mode)), 2), "realized_net": round(sum(p.net_pnl or 0.0 for p in closed), 2),
                "realized_gross": round(sum(p.gross_pnl or 0.0 for p in closed), 2), "charges": round(sum(p.charges or 0.0 for p in closed), 2),
                "wins": sum(1 for n in done if n > 0), "losses": sum(1 for n in done if n <= 0),
                "last_stopout_ts": stop_ts, "open_same_side": same,
                "by_strategy": self._by_strategy(ps)}

    @staticmethod
    def _by_strategy(ps: List[Position]) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for p in ps:
            r = out.setdefault(p.strategy or "CORE", {"entries": 0, "open": 0, "closed": 0, "net": 0.0})
            if p.parent_id is None:
                r["entries"] += 1
            if p.status == OPEN:
                r["open"] += 1
            else:
                r["closed"] += 1
                r["net"] = round(r["net"] + (p.net_pnl or 0.0), 2)
        return out
