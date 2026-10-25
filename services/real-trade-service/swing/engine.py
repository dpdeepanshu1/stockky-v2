"""swing/engine.py - E132 (plan step S3): the swing lane's EVIDENCE side. Orders nothing, carries nothing, blocks nothing.

Every evening (after SWING_SCAN_TIME_IST, once a NEW daily bar exists) `run_scan` does, in this order and idempotently (a crash in the middle and a retry is safe):
  1. settle   every open SHADOW trade gets the new daily bar (swing/lane.advance = the exact rules scripts/swing_backtest.py replayed) and a PLANNED one gets its
              entry: the open of the entry session + slippage, then the same bar's stop check;
  2. plan     the day's best screened stocks (up to SWING_SHADOW_PICKS_PER_DAY) become PLANNED shadow rows, minus the ones a veto refuses (results inside the
              window, negative news): those are recorded as SKIPPED with the reason, so the report shows what the vetoes did;
  3. save     the compact ranking of the day (the 15:00 re-check, the live entries and scripts/swing_shadow_report.py read it back).
`scan_in_thread` is how the schedule tick starts it: the fetch of a few hundred daily histories takes minutes and must never sit inside the trading locks.

Every external fact (the snapshot, the calendar, the cost model, the vetoes) is passed in, so the whole pass is testable with plain data (tests/test_swing_engine.py).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date, datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence
from zoneinfo import ZoneInfo

from swing import lane, stats

logger = logging.getLogger("real-trade-swing")
_IST = ZoneInfo("Asia/Kolkata")

KEEP_FIELDS = ("symbol", "status", "last_close", "last_open", "last_high", "last_low", "last_t", "rank", "pct_rank", "screen", "above_sma200", "atr14_pct",
               "sma10", "sma200_gap_pct", "ret_12_1", "ret_6_1", "mr_12", "mr_6", "adx14", "dist_52w_high_pct")
MIN_RANKED = 5


def _cfg(name: str, default: Any = None) -> Any:
    import config
    return getattr(config, name, default)


# ───────────────────────────── helpers ─────────────────────────────

def bar_day(last_t: Any) -> Optional[str]:
    """IST date text of a bar's epoch seconds (the same rule as the replay's ist_date)."""
    try:
        return datetime.fromtimestamp(float(last_t), tz=_IST).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def compact_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The ranking rows the lane keeps: only the fields it reads, plus the IST date of each stock's own last bar."""
    out = []
    for r in rows or []:
        if r.get("rank") is None and r.get("status") not in ("ok", "limited"):
            continue
        d = {k: r.get(k) for k in KEEP_FIELDS}
        d["last_bar_day"] = bar_day(r.get("last_t"))
        out.append(d)
    return out


def _builtin_universe() -> List[str]:
    """E133: the stocks the service already knows by name (market_context.sector_signal.NSE_SECTOR_MAP: the liquid sector-index names, ~118). Used only when
    neither SWING_UNIVERSE_SYMBOLS nor SWING_RANK_DEFAULT_SYMBOLS is set, so a fresh deploy in `shadow` records evidence from the first evening instead of
    stopping at `no_universe`. Smaller than the 300+ the plan wants and not the S2 replay's folder: set SWING_UNIVERSE_SYMBOLS (scripts/swing_universe.py) to
    rank the same stocks S2 tested. Empty list when the map cannot be read."""
    try:
        from market_context.sector_signal import NSE_SECTOR_MAP
        return sorted(str(s).upper() for s in NSE_SECTOR_MAP)
    except Exception as e:  # noqa: BLE001
        logger.debug("swing: built-in universe unavailable: %s", e)
        return []


def universe_source() -> str:
    """Where the evening scan's stock list comes from: 'env' (SWING_UNIVERSE_SYMBOLS), 'rank_default' (SWING_RANK_DEFAULT_SYMBOLS), 'builtin' (the fallback
    above) or 'none'."""
    from swing import ranker
    if [s for s in str(_cfg("SWING_UNIVERSE_SYMBOLS", "") or "").split(",") if s.strip()]:
        return "env"
    if ranker.DEFAULT_SYMBOLS:
        return "rank_default"
    return "builtin" if _builtin_universe() else "none"


def universe_symbols(extra: Iterable[str] = ()) -> List[str]:
    """SWING_UNIVERSE_SYMBOLS (else SWING_RANK_DEFAULT_SYMBOLS, else the built-in list), capped at SWING_UNIVERSE_MAX, plus any symbol a swing position holds
    (it must keep being ranked)."""
    from swing import ranker
    raw = [s for s in str(_cfg("SWING_UNIVERSE_SYMBOLS", "") or "").split(",") if s.strip()] or list(ranker.DEFAULT_SYMBOLS) or _builtin_universe()
    base = ranker.clean_symbols(raw, cap=int(_cfg("SWING_UNIVERSE_MAX", 450)))
    seen = set(base)
    for s in ranker.clean_symbols(list(extra), cap=1000):
        if s not in seen:
            base.append(s)
            seen.add(s)
    return base


def shadow_cost_pct(entry: float, exit_price: float, qty: int) -> float:
    """Round-trip delivery cost as % of the buy value, from the real cost model (the same constants the live trade pays). A carried position always sells
    settled holdings, so the delivery rate and the flat DP charge apply (the replay does the same for every trade)."""
    import cost_model
    est = cost_model.estimate_round_trip_cost(entry_price=entry, qty=max(1, int(qty)), exit_price=exit_price, product_type="CNC", is_delivery_sell=True)
    return float(est.total) / (float(entry) * max(1, int(qty))) * 100.0


def entry_vetoes(symbol: str, today: date) -> List[str]:
    """Reasons the lane would refuse to hold `symbol` through the next few days (blocking: runs on the scan thread). Unknown = no objection."""
    out: List[str] = []
    try:
        from entry_engine import event_risk
        b = lane.earnings_block(event_risk.lookup(symbol), today, int(_cfg("SWING_EARNINGS_WINDOW_DAYS", 7)))
        if b:
            out.append(b)
        n = event_risk.news_reason(symbol)
        if n:
            out.append(str(n))
    except Exception as e:  # noqa: BLE001 - a veto lookup must never stop the scan
        logger.debug("swing: veto lookup for %s failed (no objection): %s", symbol, e)
    return out


def calendar_fns():
    """(is_trading_day, next_trading_day) from the entry calendar (weekends and the NSE holidays in tz_utils)."""
    from entry_engine import calendar_gate as cg
    return cg._is_trading_day, cg._next_trading_day


# ───────────────────────────── step 1: settle ─────────────────────────────

def _bar(row: Dict[str, Any], day: str) -> Dict[str, Any]:
    return {"o": row.get("last_open"), "l": row.get("last_low"), "c": row.get("last_close"), "day": day}


def settle_shadow(repo: Any, rows_by_symbol: Dict[str, Dict[str, Any]], asof: str, next_td: Callable[[date], date],
                  cost_fn: Callable[[float, float, int], float] = shadow_cost_pct) -> Dict[str, int]:
    """Give every PLANNED / OPEN shadow trade the bar of `asof`. Returns counters."""
    hold = int(_cfg("SWING_MAX_HOLD_DAYS", 5))
    slip = float(_cfg("SWING_SLIPPAGE_PCT", 0.10))
    max_gap = float(_cfg("SWING_MAX_GAP_UP_PCT", 5.0))
    size = float(_cfg("SWING_MAX_TRADE_VALUE", 20000.0))
    asof_d = date.fromisoformat(asof)
    n = {"entered": 0, "closed": 0, "advanced": 0, "skipped": 0, "void": 0}
    for t in repo.trades(kind="SHADOW", status="PLANNED"):
        sd = date.fromisoformat(t["signal_day"])
        if sd >= asof_d:
            continue
        expected = next_td(sd)
        if asof_d != expected:
            if asof_d > expected:
                repo.update_trade(t["id"], status="SKIPPED", skip_reason="the scan missed the entry session")
                n["skipped"] += 1
            continue
        row = rows_by_symbol.get(t["symbol"])
        if not row or row.get("last_bar_day") != asof or lane._f(row.get("last_open")) is None:
            repo.update_trade(t["id"], status="SKIPPED", skip_reason="no bar for the entry session")
            n["skipped"] += 1
            continue
        if lane.gap_too_big(row.get("last_open"), t.get("signal_close"), max_gap):
            g = lane.gap_up_pct(row.get("last_open"), t.get("signal_close"))
            repo.update_trade(t["id"], status="SKIPPED", skip_reason=f"opened {g:.1f}% above the signal close (limit {max_gap:g}%)")
            n["skipped"] += 1
            continue
        lv = lane.entry_levels(row.get("last_open"), slip, t.get("stop_pct") or 3.0)
        if lv is None:
            repo.update_trade(t["id"], status="SKIPPED", skip_reason="unusable open price")
            n["skipped"] += 1
            continue
        qty = max(1, int(size // lv["entry"])) if size > 0 else 1
        state = {"entry": lv["entry"], "stop_pct": lv["stop_pct"], "stop_px": lv["stop_px"], "sessions": 0}
        st = lane.advance(state, _bar(row, asof), hold)
        if st.get("bad_bar"):
            repo.update_trade(t["id"], status="SKIPPED", skip_reason="unusable bar for the entry session")
            n["skipped"] += 1
            continue
        base = {"entry_day": asof, "entry_price": round(lv["entry"], 4), "qty": qty, "size_value": round(qty * lv["entry"], 2)}
        n["entered"] += 1
        _store(repo, t["id"], st, base, cost_fn, n)
    for t in repo.trades(kind="SHADOW", status="OPEN"):
        last = t.get("last_day") or t.get("entry_day")
        if not last or date.fromisoformat(last) >= asof_d:
            continue
        expected = next_td(date.fromisoformat(last))
        if asof_d != expected:
            if asof_d > expected:
                repo.update_trade(t["id"], status="VOID", skip_reason="the scan missed a session while the trade was open")
                n["void"] += 1
            continue
        row = rows_by_symbol.get(t["symbol"])
        if not row or row.get("last_bar_day") != asof:
            repo.update_trade(t["id"], status="VOID", skip_reason="no bar for a session of the open trade")
            n["void"] += 1
            continue
        state = {"entry": t["entry_price"], "stop_pct": t["stop_pct"], "stop_px": t["stop_px"], "sessions": t.get("sessions") or 0}
        st = lane.advance(state, _bar(row, asof), hold)
        if st.get("bad_bar"):
            repo.update_trade(t["id"], status="VOID", skip_reason="unusable bar for a session of the open trade")
            n["void"] += 1
            continue
        n["advanced"] += 1
        _store(repo, t["id"], st, {}, cost_fn, n, t)
    return n


def _store(repo: Any, tid: int, st: Dict[str, Any], base: Dict[str, Any], cost_fn: Callable[[float, float, int], float], n: Dict[str, int],
           trade: Optional[Dict[str, Any]] = None) -> None:
    f = dict(base)
    f.update(sessions=st["sessions"], stop_px=round(st["stop_px"], 4), last_day=st.get("last_day"), last_close=round(st["last_close"], 4))
    if st.get("closed"):
        entry = float(st["entry"])
        qty = int(f.get("qty") or (trade or {}).get("qty") or 1)
        pct = lane.result_pcts(entry, st["exit_price"], cost_fn(entry, float(st["exit_price"]), qty))
        f.update(status="CLOSED", exit_day=st.get("last_day"), exit_price=round(float(st["exit_price"]), 4), exit_reason=st.get("exit_reason"),
                 gap_through=bool(st.get("gap_through")), **pct)
        n["closed"] += 1
    else:
        f["status"] = "OPEN"
    repo.update_trade(tid, **f)


# ───────────────────────────── step 2: plan ─────────────────────────────

def plan_shadow(repo: Any, rows: Sequence[Dict[str, Any]], asof: str, vetoes_fn: Callable[[str, date], List[str]],
                picks_n: Optional[int] = None) -> Dict[str, int]:
    """Record the day's picks as PLANNED shadow rows (idempotent for one signal day). Returns counters."""
    picks_n = int(picks_n if picks_n is not None else _cfg("SWING_SHADOW_PICKS_PER_DAY", 3))
    asof_d = date.fromisoformat(asof)
    live_syms = {t["symbol"] for t in repo.trades(kind="SHADOW") if t["status"] in ("PLANNED", "OPEN")}
    today_rows = repo.trades(kind="SHADOW", signal_day=asof)
    done_syms = {t["symbol"] for t in today_rows}
    have = sum(1 for t in today_rows if t["status"] == "PLANNED")
    out = {"planned": 0, "vetoed": 0, "candidates": 0}
    need = picks_n - have
    if need <= 0:
        return out
    cands = lane.pick_candidates(rows, exclude=live_syms | done_syms, n=picks_n * 4, asof=asof)
    out["candidates"] = len(cands)
    mult = float(_cfg("SWING_STOP_ATR_MULT", 1.5))
    lo, hi = float(_cfg("SWING_STOP_MIN_PCT", 2.0)), float(_cfg("SWING_STOP_MAX_PCT", 6.0))
    for r in cands:
        if out["planned"] >= need:
            break
        v = vetoes_fn(r["symbol"], asof_d)
        common = dict(kind="SHADOW", symbol=r["symbol"], signal_day=asof, rank=r.get("rank"), pct_rank=r.get("pct_rank"), signal_close=r.get("last_close"),
                      atr_pct=r.get("atr14_pct"), stop_pct=round(lane.stop_pct_from_atr(r.get("atr14_pct"), mult, lo, hi), 3))
        if v:
            repo.add_trade(status="SKIPPED", skip_reason="; ".join(v)[:160], **common)
            out["vetoed"] += 1
            continue
        repo.add_trade(status="PLANNED", **common)
        out["planned"] += 1
    return out


# ───────────────────────────── the whole pass ─────────────────────────────

def run_scan(repo: Any, snapshot: Dict[str, Any], *, vetoes_fn: Callable[[str, date], List[str]] = entry_vetoes,
             calendar: Optional[Callable[[], Any]] = None, cost_fn: Callable[[float, float, int], float] = shadow_cost_pct,
             universe: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One evening pass over a snapshot (swing/ranker.build_snapshot output). Never raises. Returns {ok, status, asof, ...}."""
    try:
        asof = snapshot.get("asof_bar_date")
        rows = list(snapshot.get("rows") or [])
        n_ranked = sum(1 for r in rows if r.get("rank") is not None)
        if not asof or n_ranked < MIN_RANKED:
            return {"ok": False, "status": "nothing_ranked", "detail": f"ranked {n_ranked} of {snapshot.get('n_requested')} (bar date {asof}); notes {snapshot.get('notes')}"}
        if repo.has_run("scan", asof):
            return {"ok": True, "status": "already_done", "asof": asof}
        _is_td, next_td = (calendar or calendar_fns)()
        compact = compact_rows(rows)
        by_sym = {r["symbol"]: r for r in compact}
        settled = settle_shadow(repo, by_sym, asof, next_td, cost_fn)
        planned = plan_shadow(repo, compact, asof, vetoes_fn)
        n_screen = sum(1 for r in compact if r.get("screen"))
        detail = json.dumps({"asof": asof, "n_requested": snapshot.get("n_requested"), "n_ranked": n_ranked, "n_screen": n_screen,
                             "failed": len(snapshot.get("failed") or []), "universe": universe, "rows": compact}, separators=(",", ":"), default=str)
        summary = (f"{asof}: ranked {n_ranked}, screen {n_screen}, planned {planned['planned']} (vetoed {planned['vetoed']}), settled "
                   f"{settled['entered']} entered / {settled['advanced']} advanced / {settled['closed']} closed")
        repo.add_run("scan", asof, None, True, summary, detail)
        try:
            repo.prune(int(_cfg("SWING_LEDGER_KEEP_DAYS", 400)))
        except Exception as e:  # noqa: BLE001
            logger.debug("swing: prune failed: %s", e)
        logger.info("swing scan: %s", summary)
        return {"ok": True, "status": "scanned", "asof": asof, "summary": summary, "settled": settled, "planned": planned, "n_ranked": n_ranked, "n_screen": n_screen, "universe": universe}
    except Exception as e:  # noqa: BLE001
        logger.exception("swing scan failed")
        return {"ok": False, "status": "error", "detail": f"{type(e).__name__}: {str(e)[:160]}"}


def latest_scan(repo: Any) -> Optional[Dict[str, Any]]:
    """{'asof', 'rows' (by symbol), 'n_ranked', 'n_screen', 'created_at'} of the newest successful scan, or None."""
    run = repo.latest_run("scan")
    if not run:
        return None
    try:
        d = json.loads(run.get("detail") or "{}")
    except ValueError:
        return None
    return {"asof": d.get("asof") or run.get("day"), "rows": {r["symbol"]: r for r in d.get("rows") or []}, "n_ranked": d.get("n_ranked"),
            "n_screen": d.get("n_screen"), "universe": d.get("universe"), "created_at": run.get("created_at")}


# ───────────────────────────── scheduling (called by the tick) ─────────────────────────────

_state_lock = threading.Lock()
_state: Dict[str, Any] = {"running": False, "last_attempt": 0.0, "last_result": None}


def scan_due(now_ist: datetime, repo: Any, now_mono: Optional[float] = None) -> bool:
    """True when an evening scan should be started now: lane on, a weekday trading day, past SWING_SCAN_TIME_IST, no successful scan stored for a bar date
    of today yet, nothing running, and the last attempt at least SWING_SCAN_RETRY_MIN minutes ago."""
    if lane.norm_mode(_cfg("SWING_MODE", "shadow")) == "off":
        return False
    from tz_utils import parse_hhmm
    hh = parse_hhmm(str(_cfg("SWING_SCAN_TIME_IST", "16:00")), 16, 0)
    if (now_ist.hour, now_ist.minute) < (hh.hour, hh.minute) or now_ist.hour >= 23:
        return False
    with _state_lock:
        if _state["running"]:
            return False
        mono = time.monotonic() if now_mono is None else now_mono
        if mono - _state["last_attempt"] < float(_cfg("SWING_SCAN_RETRY_MIN", 20)) * 60.0:
            return False
    try:
        return not repo.has_run("scan", now_ist.date().isoformat())
    except Exception:  # noqa: BLE001
        return False


def scan_in_thread(session_factory: Callable[[], Any], held_symbols_fn: Optional[Callable[[Any], Iterable[str]]] = None, force: bool = False) -> bool:
    """Start one scan on a daemon thread (own DB session). False when one is already running. The thread never raises."""
    with _state_lock:
        if _state["running"]:
            return False
        _state["running"] = True
        _state["last_attempt"] = time.monotonic()

    def work() -> None:
        db = None
        try:
            from swing import ranker
            from swing.repo import SqlRepo
            db = session_factory()
            repo = SqlRepo(db)
            held = list(held_symbols_fn(db)) if held_symbols_fn else []
            syms = universe_symbols(held)
            if not syms:
                res = {"ok": False, "status": "no_universe", "detail": "no built-in list could be read: set SWING_UNIVERSE_SYMBOLS (python3 scripts/swing_universe.py HIST_DIR prints it)"}
            else:
                snap = ranker.build_snapshot(syms, None, None, float(_cfg("SWING_TOP_PCT", 80.0)), max_symbols=len(syms))
                res = run_scan(repo, snap, universe={"source": universe_source(), "size": len(syms)})
                if res.get("status") == "scanned":
                    try:
                        from swing import live
                        res["live"] = live.after_scan(db, repo, latest_scan(repo))
                    except Exception as e:  # noqa: BLE001
                        logger.exception("swing live reconcile failed")
                        res["live"] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
            res.setdefault("universe", {"source": universe_source(), "size": len(syms)})
            _state["last_result"] = {k: v for k, v in res.items() if k != "rows"}
            if not res.get("ok"):
                logger.warning("swing scan not stored: %s %s", res.get("status"), res.get("detail", ""))
        except Exception as e:  # noqa: BLE001
            logger.exception("swing scan thread failed")
            _state["last_result"] = {"ok": False, "status": "error", "detail": f"{type(e).__name__}: {str(e)[:160]}"}
        finally:
            try:
                if db is not None:
                    db.close()
            except Exception:  # noqa: BLE001
                pass
            with _state_lock:
                _state["running"] = False

    threading.Thread(target=work, name="swing-scan", daemon=True).start()
    return True


def runtime_state() -> Dict[str, Any]:
    with _state_lock:
        return {"running": _state["running"], "last_result": _state["last_result"]}


# ───────────────────────────── status / report (read-only) ─────────────────────────────

def closed_for_stats(repo: Any, kind: str = "SHADOW", mode: Optional[str] = None, since: Optional[str] = None) -> List[Dict[str, Any]]:
    """Closed trades with a net result. `since` (YYYY-MM-DD) keeps only trades whose signal day is on or after it (e.g. the day the stock list changed)."""
    return [t for t in repo.trades(kind=kind, status="CLOSED", mode=mode) if t.get("net_pct") is not None and (not since or str(t.get("signal_day") or "") >= since)]


def report(repo: Any, kind: str = "SHADOW", mode: Optional[str] = None, min_n: int = 30, min_days: int = 20, n_boot: int = 500,
           since: Optional[str] = None) -> Dict[str, Any]:
    if since:
        try:
            date.fromisoformat(since)
        except ValueError:
            return {"error": f"since must be YYYY-MM-DD, got {since!r}"}
    out = stats.verdict(closed_for_stats(repo, kind, mode, since), min_n, min_days, n_boot)
    allr = [t for t in repo.trades(kind=kind, mode=mode) if not since or str(t.get("signal_day") or "") >= since]
    out["since"] = since or None
    out["by_status"] = {}
    for t in allr:
        out["by_status"][t["status"]] = out["by_status"].get(t["status"], 0) + 1
    out["kind"] = kind
    out["note"] = ("what the lane WOULD have done: entry = next open + slippage, ATR stop ratcheted up after every close, last session at its close, "
                   "costs at the shadow size; daily bars cannot show intraday stop-outs. NOT_ENOUGH_DATA is the normal answer for weeks.")
    return out
