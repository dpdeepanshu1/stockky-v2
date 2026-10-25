"""swing/repo.py - E132: the swing lane's storage, behind a tiny interface so the engine can be tested without a database.

SqlRepo  = the real thing (models.SwingTrade / models.SwingRun through a SQLAlchemy session). Rows come back as plain dicts.
MemRepo  = the same interface in memory (tests and dry runs).
Every method commits its own write; none raises into the caller except through the caller's own try/except (the engine wraps every pass).
"""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

TRADE_FIELDS = ("kind", "mode", "symbol", "status", "signal_day", "rank", "pct_rank", "signal_close", "atr_pct", "stop_pct", "entry_day", "entry_price",
                "stop_px", "sessions", "last_day", "last_close", "exit_day", "exit_price", "exit_reason", "gap_through", "gross_pct", "cost_pct", "net_pct",
                "qty", "size_value", "position_id", "order_id", "closed_at", "skip_reason", "note")


def _clean(fields: Dict[str, Any]) -> Dict[str, Any]:
    bad = [k for k in fields if k not in TRADE_FIELDS]
    if bad:
        raise KeyError(f"unknown swing ledger field(s): {bad}")
    return dict(fields)


class MemRepo:
    """In-memory twin of SqlRepo."""

    def __init__(self) -> None:
        self._t: Dict[int, Dict[str, Any]] = {}
        self._r: Dict[int, Dict[str, Any]] = {}
        self._tid = itertools.count(1)
        self._rid = itertools.count(1)

    def add_trade(self, **f: Any) -> int:
        f = _clean(f)
        tid = next(self._tid)
        row = {k: None for k in TRADE_FIELDS}
        row.update({"sessions": 0, "gap_through": False})
        row.update(f)
        row["id"] = tid
        row["created_at"] = datetime.now(timezone.utc)
        self._t[tid] = row
        return tid

    def update_trade(self, tid: int, **f: Any) -> None:
        self._t[int(tid)].update(_clean(f))

    def trades(self, *, kind: Optional[str] = None, status: Optional[str] = None, mode: Optional[str] = None, signal_day: Optional[str] = None,
               symbol: Optional[str] = None, limit: Optional[int] = None, newest_first: bool = False) -> List[Dict[str, Any]]:
        rows = [dict(r) for r in self._t.values()
                if (kind is None or r["kind"] == kind) and (status is None or r["status"] == status) and (mode is None or r["mode"] == mode)
                and (signal_day is None or r["signal_day"] == signal_day) and (symbol is None or r["symbol"] == symbol)]
        rows.sort(key=lambda r: r["id"], reverse=newest_first)
        return rows[:limit] if limit else rows

    def add_run(self, kind: str, day: str, mode: Optional[str] = None, ok: bool = True, summary: str = "", detail: Optional[str] = None) -> int:
        rid = next(self._rid)
        self._r[rid] = {"id": rid, "kind": kind, "day": day, "mode": mode, "ok": bool(ok), "summary": summary, "detail": detail,
                        "created_at": datetime.now(timezone.utc)}
        return rid

    def latest_run(self, kind: str, mode: Optional[str] = None, ok_only: bool = True, day: Optional[str] = None) -> Optional[Dict[str, Any]]:
        rows = [r for r in self._r.values() if r["kind"] == kind and (mode is None or r["mode"] == mode) and (not ok_only or r["ok"])
                and (day is None or r["day"] == day)]
        rows.sort(key=lambda r: (r["day"], r["id"]))
        return dict(rows[-1]) if rows else None

    def has_run(self, kind: str, day: str, mode: Optional[str] = None, ok_only: bool = True) -> bool:
        return self.latest_run(kind, mode, ok_only, day) is not None

    def prune(self, keep_days: int) -> int:
        cut = datetime.now(timezone.utc) - timedelta(days=int(keep_days))
        n = 0
        for store in (self._r, self._t):
            for k in [k for k, v in store.items() if v["created_at"] < cut]:
                del store[k]
                n += 1
        return n


class SqlRepo:
    """models.SwingTrade / models.SwingRun through a SQLAlchemy session (the caller owns and closes the session)."""

    def __init__(self, db: Any) -> None:
        self.db = db

    @staticmethod
    def _d(obj: Any) -> Dict[str, Any]:
        return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}

    def add_trade(self, **f: Any) -> int:
        import models
        row = models.SwingTrade(**_clean(f))
        if row.sessions is None:
            row.sessions = 0
        if row.gap_through is None:
            row.gap_through = False
        self.db.add(row)
        self.db.commit()
        return int(row.id)

    def update_trade(self, tid: int, **f: Any) -> None:
        import models
        row = self.db.get(models.SwingTrade, int(tid))
        if row is None:
            return
        for k, v in _clean(f).items():
            setattr(row, k, v)
        self.db.commit()

    def trades(self, *, kind: Optional[str] = None, status: Optional[str] = None, mode: Optional[str] = None, signal_day: Optional[str] = None,
               symbol: Optional[str] = None, limit: Optional[int] = None, newest_first: bool = False) -> List[Dict[str, Any]]:
        import models
        T = models.SwingTrade
        q = self.db.query(T)
        if kind is not None:
            q = q.filter(T.kind == kind)
        if status is not None:
            q = q.filter(T.status == status)
        if mode is not None:
            q = q.filter(T.mode == mode)
        if signal_day is not None:
            q = q.filter(T.signal_day == signal_day)
        if symbol is not None:
            q = q.filter(T.symbol == symbol)
        q = q.order_by(T.id.desc() if newest_first else T.id.asc())
        if limit:
            q = q.limit(int(limit))
        return [self._d(r) for r in q.all()]

    def add_run(self, kind: str, day: str, mode: Optional[str] = None, ok: bool = True, summary: str = "", detail: Optional[str] = None) -> int:
        import models
        row = models.SwingRun(kind=kind, day=day, mode=mode, ok=bool(ok), summary=(summary or "")[:300], detail=detail)
        self.db.add(row)
        self.db.commit()
        return int(row.id)

    def latest_run(self, kind: str, mode: Optional[str] = None, ok_only: bool = True, day: Optional[str] = None) -> Optional[Dict[str, Any]]:
        import models
        R = models.SwingRun
        q = self.db.query(R).filter(R.kind == kind)
        if mode is not None:
            q = q.filter(R.mode == mode)
        if ok_only:
            q = q.filter(R.ok == True)  # noqa: E712  Oracle-safe `= 1` form (group150)
        if day is not None:
            q = q.filter(R.day == day)
        row = q.order_by(R.day.desc(), R.id.desc()).first()
        return self._d(row) if row is not None else None

    def has_run(self, kind: str, day: str, mode: Optional[str] = None, ok_only: bool = True) -> bool:
        import models
        R = models.SwingRun
        q = self.db.query(R.id).filter(R.kind == kind, R.day == day)
        if mode is not None:
            q = q.filter(R.mode == mode)
        if ok_only:
            q = q.filter(R.ok == True)  # noqa: E712  Oracle-safe `= 1` form (group150)
        return q.first() is not None

    def prune(self, keep_days: int) -> int:
        import models
        cut = datetime.now(timezone.utc) - timedelta(days=int(keep_days))
        n = 0
        for M in (models.SwingRun, models.SwingTrade):
            n += self.db.query(M).filter(M.created_at < cut).delete(synchronize_session=False)
        self.db.commit()
        return int(n)
