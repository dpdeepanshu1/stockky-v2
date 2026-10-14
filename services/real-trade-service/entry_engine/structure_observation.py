"""entry_engine/structure_observation.py - WS-6 for real-trade (group 356, 2026-10-14): what the daily structure gate saw, and what the stock did next.

RECORD  (entry.py, right after the structure gate verdict): one row per symbol / mode / IST day (the first concrete verdict wins) in
        structure_observations - passed, rejected_shadow and rejected_enforced alike. `no_opinion` is not stored (the gate counters already
        count it). The row is added to the caller's session and committed with the decision; recording never raises and never changes an entry.
EVALUATE (background loop, every STRUCTURE_OBS_EVAL_INTERVAL_S): for rows that are not complete and at least one session old, fetch daily
        candles from market-data /history, and fill T+1/5/10/20 return, MFE, MAE and the first touch of the structure's own target vs stop
        (stockky_ms.observation.evaluate_row). A row is complete once T+20 exists; a row with no bars after STRUCTURE_OBS_GIVE_UP_DAYS is closed.
REPORT  (GET /structure/edge/{mode}): stockky_ms.observation.aggregate() over the stored rows - did what the gate turned away really do worse
        than what it let through? Read-only; nothing in the entry path reads it back.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import config

logger = logging.getLogger("real-trade-structure-observation")

SOURCE = "real_trade"
_seen: Dict[tuple, str] = {}
_stats: Dict[str, Any] = {}


def reset_stats() -> None:
    _stats.clear()
    _stats.update(recorded=0, skipped_dup=0, skipped_no_opinion=0, skipped_bad=0, errors=0, eval_passes=0, eval_rows=0, eval_completed=0,
                  eval_fetch_ok=0, eval_fetch_fail=0, eval_gave_up=0, last_pass_at=None, last_error=None, since=time.time())


reset_stats()


def reset() -> None:
    _seen.clear()
    reset_stats()


def stats() -> Dict[str, Any]:
    out = {k: v for k, v in _stats.items() if k != "since"}
    out["since_epoch"] = _stats.get("since")
    out["enabled"] = bool(getattr(config, "STRUCTURE_OBS_ENABLED", True))
    return out


def _ms():
    """The vendored engine's observation / candles modules (imported lazily so a broken copy can never stop the service booting)."""
    from stockky_ms import candles as _candles, observation as _observation
    return _observation, _candles


def build_row(symbol: str, mode: str, price: Any, verdict: Dict[str, Any], summary: Optional[Dict[str, Any]], gate_mode: str, enforced: bool,
              now_ts: Optional[float] = None, source: str = SOURCE) -> Optional[Dict[str, Any]]:
    """The row dict for one gate verdict, or None when it must not be stored (no opinion / no price). Pure."""
    _observation, _ = _ms()
    kind = _observation.classify(verdict.get("opinion"), verdict.get("would_reject"), enforced)
    if kind == "no_opinion":
        _stats["skipped_no_opinion"] += 1
        return None
    s = summary if isinstance(summary, dict) else {}
    row = _observation.new_observation(symbol, source, now_ts if now_ts is not None else time.time(), price, kind, stop=s.get("ms_stop"),
                                       target=s.get("ms_target"), atr=s.get("ms_atr"), mode=mode, gate_mode=gate_mode,
                                       reasons=verdict.get("reasons") or [], summary=s)
    if row is None:
        _stats["skipped_bad"] += 1
    return row


def record(db, symbol: str, mode: str, price: Any, verdict: Dict[str, Any], summary: Optional[Dict[str, Any]], gate_mode: str, enforced: bool,
           now_ts: Optional[float] = None) -> bool:
    """Add one StructureObservation to `db` (committed by the caller). True when a row was added. Never raises."""
    try:
        if not getattr(config, "STRUCTURE_OBS_ENABLED", True):
            return False
        row = build_row(symbol, mode, price, verdict, summary, gate_mode, enforced, now_ts)
        if row is None:
            return False
        key = (row["symbol"], str(mode).upper(), row["obs_day"])
        if key in _seen:
            _stats["skipped_dup"] += 1
            return False
        exists = _exists(db, str(mode).upper(), row["symbol"], row["obs_day"])
        _seen[key] = row["verdict"]
        if len(_seen) > 5000:
            for k in list(_seen)[:2500]:
                _seen.pop(k, None)
        if exists:
            _stats["skipped_dup"] += 1
            return False
        db.add(_model()(**_to_columns(row)))
        _stats["recorded"] += 1
        return True
    except Exception as e:  # noqa: BLE001 - an observation must never break an entry
        _stats["errors"] += 1
        _stats["last_error"] = str(e)[:160]
        logger.debug("structure observation record failed (ignored): %s", e)
        return False


# ── the only SQLAlchemy touch points (tests replace these with in-memory fakes) ──────────────────────────────────────────────────────
def _model():
    import models
    return models.StructureObservation


def _exists(db, mode: str, symbol: str, obs_day: str) -> bool:
    M = _model()
    return db.query(M.id).filter(M.mode == mode, M.symbol == symbol, M.obs_day == obs_day).first() is not None


def _pending(db, limit: int) -> list:
    M = _model()
    return db.query(M).filter(M.is_complete.is_(False)).order_by(M.obs_ts.asc()).limit(limit).all()


def _since(db, mode: str, since_ts: float, limit: int = 5000) -> list:
    M = _model()
    return db.query(M).filter(M.mode == mode, M.obs_ts >= since_ts).order_by(M.obs_ts.desc()).limit(limit).all()


def _recently_evaluated(r: Any, now: float) -> bool:
    """A row that already has bars and was evaluated within the last 20 h gains nothing from another fetch (new bars arrive once a day)."""
    try:
        if not r.evaluated_at or not (r.bars_seen or 0):
            return False
        ev = r.evaluated_at.replace(tzinfo=timezone.utc).timestamp()
        return (now - ev) < 20 * 3600.0
    except Exception:  # noqa: BLE001
        return False


def _to_columns(row: Dict[str, Any]) -> Dict[str, Any]:
    """Engine row dict -> StructureObservation constructor kwargs (`complete` is the column `is_complete`)."""
    cols = dict(row)
    cols["is_complete"] = bool(cols.pop("complete", False))
    cols.pop("evaluated_at", None)
    return cols


def _row_dict(r: Any) -> Dict[str, Any]:
    return {"symbol": r.symbol, "mode": r.mode, "source": r.source, "obs_day": r.obs_day, "obs_ts": r.obs_ts, "price": r.price, "stop": r.stop,
            "target": r.target, "atr": r.atr, "verdict": r.verdict, "gate_mode": r.gate_mode, "ret_1d": r.ret_1d, "ret_5d": r.ret_5d,
            "ret_10d": r.ret_10d, "ret_20d": r.ret_20d, "mfe_pct": r.mfe_pct, "mae_pct": r.mae_pct, "first_touch": r.first_touch,
            "bars_seen": r.bars_seen or 0, "complete": bool(r.is_complete)}


def fetch_daily_bars(symbol: str, lookback_days: int) -> List[Any]:
    """Daily bars for `symbol` from market-data /history (sync; call from a worker thread). Raises on any failure."""
    import httpx
    from market_feed.feed import MARKET_DATA_URL
    _, _candles = _ms()
    sym = (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
    period = "3mo" if lookback_days <= 80 else "6mo"
    with httpx.Client(timeout=float(config.STRUCTURE_OBS_HISTORY_TIMEOUT_S)) as client:
        r = client.get(f"{MARKET_DATA_URL}/history/{sym}", params={"period": period, "interval": "1d"})
    if r.status_code != 200:
        raise RuntimeError(f"history HTTP {r.status_code}")
    return _candles.candles_to_bars((r.json() or {}).get("candles") or [])


def evaluate_pending(db, now_ts: Optional[float] = None, batch: Optional[int] = None,
                     fetch: Optional[Callable[[str, int], List[Any]]] = None) -> Dict[str, Any]:
    """One evaluator pass (sync). Fills outcomes on pending rows whose symbol history could be fetched; commits once. Returns pass counters."""
    _observation, _ = _ms()
    fetch = fetch or fetch_daily_bars
    now = float(now_ts if now_ts is not None else time.time())
    limit_syms = int(batch or config.STRUCTURE_OBS_BATCH)
    res = {"pending": 0, "symbols": 0, "updated": 0, "completed": 0, "gave_up": 0, "fetch_fail": 0}
    rows = _pending(db, limit_syms * 12)
    res["pending"] = len(rows)
    by_sym: Dict[str, list] = {}
    for r in rows:
        d = _observation.due(_row_dict(r), now)
        if d["due"] and _recently_evaluated(r, now):
            continue
        if d["due"]:
            by_sym.setdefault(r.symbol, []).append((r, d["lookback_days"]))
    for sym in list(by_sym)[:limit_syms]:
        res["symbols"] += 1
        group = by_sym[sym]
        try:
            bars = fetch(sym, max(lb for _, lb in group))
            _stats["eval_fetch_ok"] += 1
        except Exception as e:  # noqa: BLE001 - history down / unknown symbol: try again next pass
            _stats["eval_fetch_fail"] += 1
            res["fetch_fail"] += 1
            logger.debug("structure observation: history for %s failed: %s", sym, e)
            bars = None
        for r, _lb in group:
            after = _observation.bars_after(bars, r.obs_ts) if bars else []
            if after:
                out = _observation.evaluate_row(_row_dict(r), after)
                for k in ("ret_1d", "ret_5d", "ret_10d", "ret_20d", "mfe_pct", "mae_pct", "first_touch", "bars_seen"):
                    setattr(r, k, out[k])
                r.is_complete = bool(out["complete"])
                r.evaluated_at = datetime.now(timezone.utc).replace(tzinfo=None)
                res["updated"] += 1
                if r.is_complete:
                    res["completed"] += 1
            elif bars is not None and (now - float(r.obs_ts)) / 86400.0 > float(config.STRUCTURE_OBS_GIVE_UP_DAYS):
                r.is_complete = True      # history answered but has nothing after the day (delisted / halted): stop asking
                res["gave_up"] += 1
    if res["updated"] or res["gave_up"]:
        db.commit()
    _stats["eval_passes"] += 1
    _stats["eval_rows"] += res["updated"]
    _stats["eval_completed"] += res["completed"]
    _stats["eval_gave_up"] += res["gave_up"]
    _stats["last_pass_at"] = now
    return res


def report(db, mode: str, days: int = 60, recent: int = 40, min_n: int = 20, now_ts: Optional[float] = None) -> Dict[str, Any]:
    """Edge report for one mode over the last `days`: aggregate stats + the most recent rows (newest first) + the rejected symbols' outcomes."""
    _observation, _ = _ms()
    days = min(max(int(days), 1), 365)
    since = float(now_ts if now_ts is not None else time.time()) - days * 86400.0
    q = _since(db, str(mode).upper(), since)
    rows = [_row_dict(r) for r in q]
    agg = _observation.aggregate(rows, min_n=min_n)
    keep = ("symbol", "obs_day", "price", "stop", "target", "verdict", "gate_mode", "ret_1d", "ret_5d", "ret_10d", "ret_20d", "mfe_pct", "mae_pct",
            "first_touch", "bars_seen", "complete")
    out_rows = []
    for r, raw in list(zip(rows, q))[:max(0, int(recent))]:
        item = {k: r.get(k) for k in keep}
        item["reasons"] = _loads(raw.reasons_json)
        out_rows.append(item)
    return {"mode": str(mode).upper(), "days": days, "min_n": min_n, "stats": agg, "recent": out_rows, "process": stats()}


def _loads(raw: Any) -> list:
    try:
        import json
        v = json.loads(raw) if raw else []
        return v if isinstance(v, list) else []
    except Exception:  # noqa: BLE001
        return []


async def run_loop(session_factory: Callable[[], Any]) -> None:
    """Background evaluator: sleeps STRUCTURE_OBS_EVAL_INTERVAL_S between passes; a pass runs on a worker thread so the event loop never
    waits on history HTTP or the DB. Exits quietly when cancelled; any error is logged and the loop carries on."""
    await asyncio.sleep(90.0)        # let the service finish booting first
    while True:
        try:
            if getattr(config, "STRUCTURE_OBS_ENABLED", True):
                def _pass() -> Dict[str, Any]:
                    db = session_factory()
                    try:
                        return evaluate_pending(db)
                    finally:
                        db.close()
                res = await asyncio.to_thread(_pass)
                if res.get("updated") or res.get("gave_up"):
                    logger.info("structure observation pass: %s", res)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            _stats["errors"] += 1
            _stats["last_error"] = str(e)[:160]
            logger.warning("structure observation pass failed (will retry): %s", e)
        await asyncio.sleep(float(config.STRUCTURE_OBS_EVAL_INTERVAL_S))
