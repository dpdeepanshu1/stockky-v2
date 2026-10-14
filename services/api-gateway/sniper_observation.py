"""group361 (WS-6 for the Buy Sniper): what the daily-structure gate saw on the Hot Picks candidates, and what those stocks did next.

real-trade and position-stocks already store one StructureObservation per symbol / day (group 356). The sniper had none, so there was no
evidence that its structure gate (group 357, enforce by default) helps. This module adds the same measurement here, using the shared pure
engine `stockky_ms.observation` (classify / new_observation / due / bars_after / evaluate_row / aggregate), so all three services are judged
by the same rules.

RECORD   after every /scan/find-buys answer: one row per symbol per IST day (the first concrete verdict wins)
           * surfaced cards that carry a structure verdict -> `passed`
           * `structure_rejected` setups (enforce mode)    -> `rejected_enforced`
           * no gate / no opinion / no price               -> nothing stored
         Recording never raises, never delays the response (the route hands it to a worker thread) and never changes a card.
EVALUATE for rows that are at least one session old: daily bars from market-data /history, T+1/5/10/20 return, MFE, MAE, first touch of the
         structure's own target vs stop. A row is complete at T+20; a row with no bars after SNIPER_OBS_GIVE_UP_DAYS is closed.
REPORT   per-verdict stats and the gate edge (passed minus rejected) + the newest rows: GET /api/scan/find-buys/edge. Read-only.

Storage: kv_cache (durable in Neon), one shard per IST day `stockky:sniper_obs:day:YYYY-MM-DD` ({"rows": {SYMBOL: row}}) and an index
`stockky:sniper_obs:index` ({"days": {day: {"n": rows, "open": rows not yet complete}}}), so the evaluator only reads days that still have
open rows. Process-local lock: the gateway is a single process (a second instance could lose a same-day write, which only costs one row).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("sniper-observation")

SOURCE = "sniper"
DAY_PREFIX = "stockky:sniper_obs:day:"
INDEX_KEY = "stockky:sniper_obs:index"

_LOCK = threading.Lock()
_stats: Dict[str, Any] = {}


def reset_stats() -> None:
    _stats.clear()
    _stats.update(calls=0, recorded=0, skipped_dup=0, skipped_no_opinion=0, skipped_bad=0, skipped_cap=0, errors=0, eval_passes=0,
                  eval_rows=0, eval_completed=0, eval_fetch_ok=0, eval_fetch_fail=0, eval_gave_up=0, last_pass_at=None, last_error=None,
                  since=time.time())


reset_stats()


def stats() -> Dict[str, Any]:
    out = {k: v for k, v in _stats.items() if k != "since"}
    out["since_epoch"] = _stats.get("since")
    out["enabled"] = enabled()
    return out


# ----------------------------------------------------------------------------- config
def enabled() -> bool:
    return (os.getenv("SNIPER_OBS_ENABLED") or "1").strip().lower() not in ("0", "false", "no", "off")


def _env_num(name: str, default: float, lo: float = 0.0) -> float:
    try:
        return max(lo, float((os.getenv(name) or "").strip() or default))
    except (TypeError, ValueError):
        return max(lo, float(default))


def _max_per_day() -> int:
    return int(_env_num("SNIPER_OBS_MAX_PER_DAY", 60, lo=1))


def _retention_days() -> int:
    return int(_env_num("SNIPER_OBS_RETENTION_DAYS", 120, lo=7))


def _give_up_days() -> float:
    return _env_num("SNIPER_OBS_GIVE_UP_DAYS", 45, lo=5)


def _batch() -> int:
    return int(_env_num("SNIPER_OBS_BATCH", 40, lo=1))


def _history_timeout() -> float:
    return _env_num("SNIPER_OBS_HISTORY_TIMEOUT_S", 8.0, lo=1.0)


def _ms():
    """The vendored engine's observation / candles modules (lazy: a broken copy must never stop the gateway booting)."""
    from stockky_ms import candles as _candles, observation as _observation
    return _observation, _candles


# ----------------------------------------------------------------------------- storage (kv_cache by default; tests pass a fake)
class KvStore:
    """Thin wrapper so tests can swap the backend: get(key) -> Any, set(key, value, ttl)."""

    def __init__(self, get: Optional[Callable[[str], Any]] = None, set_: Optional[Callable[..., None]] = None):
        if get is None or set_ is None:
            import kv_cache
            get, set_ = kv_cache.kv_get, kv_cache.kv_set
        self.get, self.set = get, set_


def _load_index(kv: KvStore) -> Dict[str, Dict[str, int]]:
    try:
        raw = kv.get(INDEX_KEY)
        days = raw.get("days") if isinstance(raw, dict) else None
        if isinstance(days, dict):
            return {str(d): {"n": int((v or {}).get("n") or 0), "open": int((v or {}).get("open") or 0)} for d, v in days.items() if isinstance(v, dict)}
    except Exception:  # noqa: BLE001
        pass
    return {}


def _save_index(kv: KvStore, days: Dict[str, Dict[str, int]]) -> None:
    keep = sorted(days)[-_retention_days():]
    kv.set(INDEX_KEY, {"days": {d: days[d] for d in keep}}, ttl=_retention_days() * 86400)


def _load_day(kv: KvStore, day: str) -> Dict[str, Dict[str, Any]]:
    try:
        raw = kv.get(DAY_PREFIX + day)
        rows = raw.get("rows") if isinstance(raw, dict) else None
        return {k: v for k, v in rows.items() if isinstance(v, dict)} if isinstance(rows, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_day(kv: KvStore, day: str, rows: Dict[str, Dict[str, Any]]) -> None:
    kv.set(DAY_PREFIX + day, {"rows": rows}, ttl=_retention_days() * 86400)


def _count_open(rows: Dict[str, Dict[str, Any]]) -> int:
    return sum(1 for r in rows.values() if not r.get("complete"))


# ----------------------------------------------------------------------------- RECORD
def _norm(sym: Any) -> str:
    return str(sym or "").upper().replace(".NS", "").replace(".BO", "").strip()


def build_rows(payload: Any, result: Any, now_ts: Optional[float] = None) -> List[Dict[str, Any]]:
    """The observation rows for one find-buys call (at most one per symbol). Pure: reads the scan rows the route received and the response
    it produced. Never raises; anything odd is skipped."""
    out: List[Dict[str, Any]] = []
    try:
        import buy_sniper
        _observation, _ = _ms()
        now = float(now_ts if now_ts is not None else time.time())
        rows_by_sym: Dict[str, Dict[str, Any]] = {}
        for r in buy_sniper._payload_rows(payload):
            if isinstance(r, dict) and not r.get("_meta"):
                s = _norm(r.get("symbol"))
                if s and s not in rows_by_sym:
                    rows_by_sym[s] = r
        mode = str((result or {}).get("structure_mode") or "")
        seen = set()

        def _add(sym: str, price: Any, verdict: str, reasons: Any) -> None:
            if sym in seen:
                return
            sm = buy_sniper._structure_summary(rows_by_sym.get(sym) or {}) or {}
            row = _observation.new_observation(sym, SOURCE, now, price, verdict, stop=sm.get("ms_stop"), target=sm.get("ms_target"),
                                               atr=sm.get("ms_atr"), mode=None, gate_mode=mode or None, reasons=reasons or [], summary=sm)
            if row is None:
                _stats["skipped_bad"] += 1
                return
            seen.add(sym)
            out.append(row)

        for card in (result or {}).get("suggestions") or []:
            if not isinstance(card, dict):
                continue
            st = card.get("structure")
            sym = _norm(card.get("symbol"))
            if not isinstance(st, dict) or not sym:
                continue
            kind = _observation.classify(st.get("opinion"), st.get("would_reject"), False)  # a surfaced card was never enforced-rejected
            if kind == "no_opinion":
                _stats["skipped_no_opinion"] += 1
                continue
            _add(sym, card.get("price"), kind, st.get("reasons"))
        for rej in (result or {}).get("structure_rejected") or []:
            if not isinstance(rej, dict):
                continue
            sym = _norm(rej.get("symbol"))
            src = rows_by_sym.get(sym)
            if not sym or src is None:
                _stats["skipped_bad"] += 1
                continue
            _add(sym, rej.get("price") if rej.get("price") is not None else buy_sniper._price(src), "rejected_enforced", rej.get("reasons"))
    except Exception as e:  # noqa: BLE001
        _stats["errors"] += 1
        _stats["last_error"] = str(e)[:160]
        logger.debug("sniper observation build failed (ignored): %s", e)
    return out


def record(payload: Any, result: Any, kv: Optional[KvStore] = None, now_ts: Optional[float] = None) -> int:
    """Store the rows of one find-buys call. Returns the number of NEW rows written. Blocking (kv) - call from a worker thread. Never raises."""
    try:
        if not enabled():
            return 0
        _stats["calls"] += 1
        rows = build_rows(payload, result, now_ts)
        if not rows:
            return 0
        kv = kv or KvStore()
        added = 0
        with _LOCK:
            idx = _load_index(kv)
            touched: Dict[str, Dict[str, Dict[str, Any]]] = {}
            for row in rows:
                day = row["obs_day"]
                shard = touched.get(day)
                if shard is None:
                    shard = touched[day] = _load_day(kv, day)
                if row["symbol"] in shard:
                    _stats["skipped_dup"] += 1        # the first concrete verdict of the day wins
                    continue
                if len(shard) >= _max_per_day():
                    _stats["skipped_cap"] += 1
                    continue
                shard[row["symbol"]] = row
                added += 1
            if added:
                for day, shard in touched.items():
                    _save_day(kv, day, shard)
                    idx[day] = {"n": len(shard), "open": _count_open(shard)}
                _save_index(kv, idx)
        _stats["recorded"] += added
        return added
    except Exception as e:  # noqa: BLE001 - an observation must never break find-buys
        _stats["errors"] += 1
        _stats["last_error"] = str(e)[:160]
        logger.debug("sniper observation record failed (ignored): %s", e)
        return 0


# ----------------------------------------------------------------------------- EVALUATE
def fetch_daily_bars(symbol: str, lookback_days: int) -> List[Any]:
    """Daily bars from market-data /history (sync). Raises on any failure."""
    import httpx
    _, _candles = _ms()
    base = (os.getenv("MARKET_DATA_URL") or "https://market-data-service-r6d7.onrender.com").strip().rstrip("/")
    period = "3mo" if lookback_days <= 80 else "6mo"
    with httpx.Client(timeout=_history_timeout()) as client:
        r = client.get(f"{base}/history/{_norm(symbol)}", params={"period": period, "interval": "1d"})
    if r.status_code != 200:
        raise RuntimeError(f"history HTTP {r.status_code}")
    return _candles.candles_to_bars((r.json() or {}).get("candles") or [])


def _recently_evaluated(row: Dict[str, Any], now: float) -> bool:
    """A row that already has bars and was evaluated within 20 h gains nothing from another fetch (a new bar arrives once a day)."""
    try:
        ev, seen = row.get("evaluated_at"), int(row.get("bars_seen") or 0)
        if not ev or not seen:
            return False
        t = datetime.fromisoformat(str(ev))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return 0 <= now - t.timestamp() < 20 * 3600.0
    except Exception:  # noqa: BLE001
        return False


def evaluate_pending(kv: Optional[KvStore] = None, now_ts: Optional[float] = None, batch: Optional[int] = None,
                     fetch: Optional[Callable[[str, int], List[Any]]] = None) -> Dict[str, Any]:
    """One evaluator pass (blocking). Fills outcomes on open rows whose symbol history could be fetched. Returns pass counters. Never raises."""
    res = {"pending": 0, "symbols": 0, "updated": 0, "completed": 0, "gave_up": 0, "fetch_fail": 0}
    try:
        _observation, _ = _ms()
        kv = kv or KvStore()
        fetch = fetch or fetch_daily_bars
        now = float(now_ts if now_ts is not None else time.time())
        limit_syms = int(batch or _batch())
        with _LOCK:
            idx = _load_index(kv)
            shards: Dict[str, Dict[str, Dict[str, Any]]] = {}
            by_sym: Dict[str, List[Tuple[str, str, int]]] = {}
            for day in sorted(d for d, v in idx.items() if v.get("open")):
                shard = shards[day] = _load_day(kv, day)
                for sym, row in shard.items():
                    if row.get("complete"):
                        continue
                    res["pending"] += 1
                    d = _observation.due(row, now)
                    if d["due"] and not _recently_evaluated(row, now):
                        by_sym.setdefault(sym, []).append((day, sym, d["lookback_days"]))
            dirty = set()
            for sym in list(by_sym)[:limit_syms]:
                res["symbols"] += 1
                group = by_sym[sym]
                try:
                    bars = fetch(sym, max(lb for _, _, lb in group))
                    _stats["eval_fetch_ok"] += 1
                except Exception as e:  # noqa: BLE001 - history down / unknown symbol: next pass
                    _stats["eval_fetch_fail"] += 1
                    res["fetch_fail"] += 1
                    logger.debug("sniper observation: history for %s failed: %s", sym, e)
                    bars = None
                for day, s, _lb in group:
                    row = shards[day][s]
                    after = _observation.bars_after(bars, row.get("obs_ts") or 0) if bars else []
                    if after:
                        row.update(_observation.evaluate_row(row, after))
                        row["evaluated_at"] = datetime.fromtimestamp(now, tz=timezone.utc).replace(tzinfo=None).isoformat()
                        res["updated"] += 1
                        dirty.add(day)
                        if row.get("complete"):
                            res["completed"] += 1
                    elif bars is not None and (now - float(row.get("obs_ts") or 0)) / 86400.0 > _give_up_days():
                        row["complete"] = True        # history answered but has nothing after the day (delisted / halted): stop asking
                        res["gave_up"] += 1
                        dirty.add(day)
            for day in dirty:
                _save_day(kv, day, shards[day])
                idx[day] = {"n": len(shards[day]), "open": _count_open(shards[day])}
            if dirty:
                _save_index(kv, idx)
        _stats["eval_passes"] += 1
        _stats["eval_rows"] += res["updated"]
        _stats["eval_completed"] += res["completed"]
        _stats["eval_gave_up"] += res["gave_up"]
        _stats["last_pass_at"] = now
    except Exception as e:  # noqa: BLE001
        _stats["errors"] += 1
        _stats["last_error"] = str(e)[:160]
        res["error"] = str(e)[:160]
        logger.debug("sniper observation evaluate failed (ignored): %s", e)
    return res


# ----------------------------------------------------------------------------- REPORT
def _loads(raw: Any) -> list:
    try:
        v = json.loads(raw) if raw else []
        return v if isinstance(v, list) else []
    except Exception:  # noqa: BLE001
        return []


def _gate_thresholds() -> Dict[str, Any]:
    """The sniper gate's own live thresholds (same env names and defaults as buy_sniper.structure_verdict), so calibrate() replays THIS gate."""
    def f(name: str, default: float) -> float:
        try:
            v = float((os.getenv(name) or "").strip() or default)
            return v if v == v and abs(v) != float("inf") else default
        except (TypeError, ValueError):
            return default
    return {"chase_atr": f("STRUCTURE_MAX_CHASE_ATR", 1.5), "stretch_atr": f("STRUCTURE_MAX_STRETCH_ATR", 3.0), "room_atr": f("STRUCTURE_MIN_ROOM_ATR", 0.5),
            "rr": f("STRUCTURE_MIN_RR", 1.2), "bearish_bos_bars": int(f("STRUCTURE_BEARISH_BOS_BARS", 5.0))}


def report(kv: Optional[KvStore] = None, days: int = 60, recent: int = 40, min_n: int = 20, now_ts: Optional[float] = None) -> Dict[str, Any]:
    """Edge report over the last `days` IST days: aggregate stats (per verdict, gate edge, conclusion) + the newest rows. Read-only. Never raises."""
    days = min(max(int(days), 1), 365)
    out: Dict[str, Any] = {"source": SOURCE, "days": days, "min_n": min_n, "stats": None, "calibration": None, "recent": [], "process": stats()}
    try:
        _observation, _candles = _ms()
        kv = kv or KvStore()
        now = float(now_ts if now_ts is not None else time.time())
        first_day = _candles.ist_date(now - days * 86400.0)
        idx = _load_index(kv)
        rows: List[Dict[str, Any]] = []
        for day in sorted((d for d in idx if d >= first_day), reverse=True):
            rows.extend(sorted(_load_day(kv, day).values(), key=lambda r: -float(r.get("obs_ts") or 0)))
        out["stats"] = _observation.aggregate(rows, min_n=min_n)
        try:  # group362: advisory calibration from the same rows; never lets the edge report fail
            out["calibration"] = _observation.calibrate(rows, thresholds=_gate_thresholds(), min_n=min_n)
        except Exception as e:  # noqa: BLE001
            out["calibration"] = {"advisory": True, "error": str(e)[:160]}
        keep = ("symbol", "obs_day", "price", "stop", "target", "verdict", "gate_mode", "ret_1d", "ret_5d", "ret_10d", "ret_20d", "mfe_pct",
                "mae_pct", "first_touch", "bars_seen", "complete")
        for r in rows[:max(0, int(recent))]:
            item = {k: r.get(k) for k in keep}
            item["reasons"] = _loads(r.get("reasons_json"))
            out["recent"].append(item)
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)[:160]
    return out
