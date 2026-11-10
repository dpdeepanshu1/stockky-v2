"""capital_board.py - how ONE Dhan account is split between Real Trade, Position Stocks and F&O. Stdlib only, never raises. IDENTICAL copy in all three services
(scripts/check_shared_copies.py keeps them equal; the source is shared/capital_board.py).

THE RULE (Deepanshu, 8 Nov 2026): 25 % each to the three services = 75 % fixed; the last 25 % (the BONUS) goes to the service that earns more.
  A service that is not ACTIVE (F&O in DEMO / engine off, or a service that stopped publishing) gets 0 and its fixed part is shared equally by the active ones
  (two active = 37.5 % each), so no money sits reserved for a service that cannot spend it. One active service = 75 % + the bonus.
  score = net P&L after charges / the capital the service had, over the last `window_days`  (a RETURN, not rupees: a service that holds more money would always win in rupees).
  Eligible for the bonus: >= `min_trades` closed trades in the window AND score > 0 AND not frozen. "proportional" splits the bonus by score, "winner" gives it all to the best.
  Nobody eligible -> the bonus is split equally between the active ones. `max_pct` caps one service (default 50, unless it is the only active one);
  `max_step_pct` limits how far a share may move per rebalance.

HOW THE THREE SERVICES TALK: small JSON files in one Docker volume (/data/shared), each service writes ONLY its own file, atomically (tmp + rename):
  capital_stats_<service>.json   written by each service: net_pnl, trades, capital_base, exposure (rupees it has in open positions), active, mode, ts
  capital_alloc.json             written by Real Trade ONLY (it owns the Dhan balance): pcts, amounts (rupees), total_equity, free_cash, mode, day, ts
MODES (CAPITAL_ALLOC_MODE, per service): off = nothing written or read | shadow (default) = the board is written and shown, NO sizing or cap changes (except F&O REAL, which has no other
source of capital) | live = Real Trade and Position Stocks use their percentage in place of the fixed 50 %. A consumer applies the board only when its own mode AND the board's mode are live.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

SERVICES = ("real_trade", "position_stocks", "fno")
MODES = ("off", "shadow", "live")
ALLOC_FILE = "capital_alloc.json"
FNO_ACTIVE_MAX_AGE_S = 900.0              # round 308: F&O publishes every ~30 s, also outside market hours; an "active" older than this is a service that stopped, not one that is armed


def _f(v: Any) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def norm_mode(v: Any) -> str:
    s = str(v or "").strip().lower()
    return s if s in MODES else "off"        # a typo must never switch money movement on


# ── the rule (pure) ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def score(stats: Optional[dict]) -> Optional[float]:
    """Net return % on the capital the service had, or None when it cannot be told."""
    s = stats or {}
    pnl, cap = _f(s.get("net_pnl")), _f(s.get("capital_base"))
    if pnl is None or cap is None or cap <= 0:
        return None
    return 100.0 * pnl / cap


def allocate(stats: Dict[str, dict], base_pct: float = 25.0, min_trades: int = 20, bonus_mode: str = "proportional", frozen: Iterable[str] = (),
             active: Optional[Iterable[str]] = None, max_pct: float = 50.0, previous: Optional[Dict[str, float]] = None, max_step_pct: Optional[float] = None) -> Dict[str, float]:
    """{service: percent}, always summing to 100."""
    act: List[str] = [k for k in SERVICES if active is None or k in set(active)]
    if not act:
        act = list(SERVICES)                                  # nobody reports: plain thirds rather than nothing
    fixed = min(100.0, max(0.0, 3.0 * (_f(base_pct) if _f(base_pct) is not None else 25.0)))
    bonus = 100.0 - fixed
    frozen = set(frozen or ())
    out = {k: 0.0 for k in SERVICES}
    for k in act:
        out[k] = fixed / len(act)
    scores: Dict[str, float] = {}
    for k in act:
        sc = score((stats or {}).get(k))
        trades = _f(((stats or {}).get(k) or {}).get("trades")) or 0.0
        if sc is not None and sc > 0 and trades >= min_trades and k not in frozen:
            scores[k] = sc
    if bonus > 0:
        if not scores:
            for k in act:
                out[k] += bonus / len(act)
        elif bonus_mode == "winner":
            out[max(scores, key=lambda k: scores[k])] += bonus
        else:
            tot = sum(scores.values())
            for k, v in scores.items():
                out[k] += bonus * v / tot
    out = _cap(out, act, max_pct)
    prev_ok = previous and all(_f(previous.get(k)) is not None for k in SERVICES)
    if prev_ok and max_step_pct is not None:
        # every share moves toward its target by the SAME fraction, so the biggest move is max_step_pct and the total stays exactly 100
        delta = {k: out[k] - float(previous[k]) for k in SERVICES}
        biggest = max(abs(d) for d in delta.values())
        f = 1.0 if biggest <= 0 else min(1.0, max(0.0, float(max_step_pct)) / biggest)
        out = {k: float(previous[k]) + delta[k] * f for k in SERVICES}
    return {k: round(v, 4) for k, v in out.items()}


def _cap(pcts: Dict[str, float], act: List[str], max_pct: float) -> Dict[str, float]:
    """No ACTIVE service above max_pct (never below an equal split of the active ones); the excess goes to the other active ones in proportion to what they hold."""
    cap = max(100.0 / len(act), float(max_pct))
    p = dict(pcts)
    for _ in range(len(act)):
        over = {k: p[k] - cap for k in act if p[k] > cap + 1e-9}
        if not over:
            break
        room = {k: p[k] for k in act if p[k] < cap - 1e-9}
        if not room:
            break
        extra = sum(over.values())
        for k in over:
            p[k] = cap
        rs = sum(room.values()) or 1.0
        for k, v in room.items():
            p[k] = v + extra * v / rs
    return p


def amounts(total_equity: Any, pcts: Dict[str, float]) -> Dict[str, float]:
    t = _f(total_equity)
    if t is None or t <= 0:
        return {k: 0.0 for k in pcts}
    return {k: round(t * v / 100.0, 2) for k, v in pcts.items()}


# ── files ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def _path(d: str, name: str) -> str:
    return os.path.join(d, name)


def write_json(d: str, name: str, doc: Dict[str, Any]) -> bool:
    """Atomic write (tmp + rename). False on any error."""
    try:
        os.makedirs(d, exist_ok=True)
        tmp = _path(d, f".{name}.{os.getpid()}.{threading.get_ident()}.tmp")      # round 247: two threads of one process (loop + a route) shared one tmp name: one rename failed, or both wrote into the same file
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, separators=(",", ":"), default=str)
        os.replace(tmp, _path(d, name))
        return True
    except Exception:  # noqa: BLE001
        return False


def read_json(d: str, name: str, max_age_s: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """The document, or None when missing / unreadable / older than max_age_s (by its own `ts`)."""
    try:
        with open(_path(d, name), encoding="utf-8") as fh:
            doc = json.load(fh)
        if not isinstance(doc, dict):
            return None
        if max_age_s is not None:
            ts = _f(doc.get("ts"))
            if ts is None or time.time() - ts > float(max_age_s):
                return None
        return doc
    except Exception:  # noqa: BLE001
        return None


def stats_file(service: str) -> str:
    return f"capital_stats_{service}.json"


def write_stats(d: str, service: str, *, net_pnl: float, trades: int, capital_base: float, exposure: float, active: bool, mode: str = "", window_days: int = 30,
                now: Optional[float] = None) -> bool:
    if service not in SERVICES:
        return False
    return write_json(d, stats_file(service), {"service": service, "ts": now if now is not None else time.time(), "net_pnl": round(_f(net_pnl) or 0.0, 2), "trades": int(trades or 0),
                                               "capital_base": round(max(0.0, _f(capital_base) or 0.0), 2), "exposure": round(max(0.0, _f(exposure) or 0.0), 2),
                                               "active": bool(active), "mode": str(mode or ""), "window_days": int(window_days)})


def read_stats(d: str, service: str, max_age_s: Optional[float] = None) -> Optional[Dict[str, Any]]:
    return read_json(d, stats_file(service), max_age_s)


def exposure_of(d: str, service: str, max_age_s: float = 900.0) -> float:
    """Rupees the service has in open positions (0.0 when unknown or its figure is older than max_age_s)."""
    s = read_stats(d, service, max_age_s)
    return max(0.0, _f((s or {}).get("exposure")) or 0.0)


# ── the board (decided by Real Trade only) ──────────────────────────────────────────────────────────────────────────────────
def refresh(d: str, *, mode: str, day: str, free_cash: float, base_pct: float = 25.0, min_trades: int = 20, bonus_mode: str = "proportional", max_pct: float = 50.0,
            max_step_pct: Optional[float] = 10.0, frozen: Iterable[str] = ("fno",), stats_max_age_s: float = 3 * 86400.0, now: Optional[float] = None,
            fno_active_max_age_s: float = FNO_ACTIVE_MAX_AGE_S) -> Optional[Dict[str, Any]]:
    """Read every service's stats, (re)decide the percentages when the day or the active set changed, write capital_alloc.json with fresh rupee amounts. Returns the document."""
    mode = norm_mode(mode)
    if mode == "off":
        return None
    try:
        now = now if now is not None else time.time()
        stats = {k: read_stats(d, k, stats_max_age_s) for k in SERVICES}
        # a stock service whose figure is missing / old is treated as active (it keeps its share: it may simply be down for the night); F&O must say so itself, and recently (fno_active_max_age_s)
        active = [k for k in SERVICES if (stats[k] or {}).get("active", k != "fno")]
        f_ts = _f((stats["fno"] or {}).get("ts"))
        if "fno" in active and (f_ts is None or now - f_ts > float(fno_active_max_age_s)):
            active.remove("fno")                              # round 308: a stale "active: true" (F&O service down while armed) used to keep its fixed share reserved for up to 3 days
        prev = read_json(d, ALLOC_FILE)
        redecide = not prev or prev.get("day") != day or sorted(prev.get("active") or []) != sorted(active) or not isinstance(prev.get("pcts"), dict)
        if redecide:
            same_set = bool(prev) and sorted(prev.get("active") or []) == sorted(active) and isinstance(prev.get("pcts"), dict)
            pcts = allocate({k: (stats[k] or {}) for k in SERVICES}, base_pct, min_trades, bonus_mode, frozen, active, max_pct,
                            previous=prev.get("pcts") if same_set else None, max_step_pct=max_step_pct)
            decided_ts = now
        else:
            pcts, decided_ts = prev["pcts"], prev.get("decided_ts", now)
        exposures = {k: max(0.0, _f((stats[k] or {}).get("exposure")) or 0.0) for k in SERVICES}
        total = max(0.0, _f(free_cash) or 0.0) + sum(exposures.values())
        doc = {"ts": now, "day": day, "mode": mode, "decided_ts": decided_ts, "active": active, "pcts": pcts, "amounts": amounts(total, pcts), "total_equity": round(total, 2),
               "free_cash": round(max(0.0, _f(free_cash) or 0.0), 2), "exposure": {k: round(v, 2) for k, v in exposures.items()},
               "scores": {k: (None if score(stats[k]) is None else round(score(stats[k]), 3)) for k in SERVICES},
               "trades": {k: int((stats[k] or {}).get("trades") or 0) for k in SERVICES},
               "params": {"base_pct": base_pct, "min_trades": min_trades, "bonus_mode": bonus_mode, "max_pct": max_pct, "max_step_pct": max_step_pct, "frozen": sorted(frozen)}}
        write_json(d, ALLOC_FILE, doc)
        return doc
    except Exception:  # noqa: BLE001
        return None


def read_alloc(d: str, max_age_s: float = 3 * 86400.0) -> Optional[Dict[str, Any]]:
    doc = read_json(d, ALLOC_FILE, max_age_s)
    return doc if doc and isinstance(doc.get("pcts"), dict) else None


def effective_share(d: str, service: str, own_mode: Any, fixed_pct: float, max_age_s: float = 6 * 3600.0) -> Tuple[float, str]:
    """(percent this service may use, why). The board's percentage only when this service's mode AND the board's mode are live and the board is fresh; otherwise the fixed percent."""
    try:
        if norm_mode(own_mode) != "live":
            return float(fixed_pct), "fixed (mode not live)"
        doc = read_alloc(d, max_age_s)
        if not doc:
            return float(fixed_pct), "fixed (no fresh board)"
        if norm_mode(doc.get("mode")) != "live":
            return float(fixed_pct), "fixed (board not live)"
        v = _f((doc.get("pcts") or {}).get(service))
        if v is None or v <= 0:
            return float(fixed_pct), "fixed (no figure for this service)"
        return float(v), "board"
    except Exception:  # noqa: BLE001
        return float(fixed_pct), "fixed (error)"


def amount_for(d: str, service: str, max_age_s: float = 6 * 3600.0) -> Tuple[Optional[float], str]:
    """(rupees the board gives this service, why). None when the board is missing / old / off. Used by F&O REAL, which has no other source of capital."""
    doc = read_alloc(d, max_age_s)
    if not doc:
        return None, "no fresh capital board (Real Trade writes it; CAPITAL_ALLOC_MODE must be shadow or live there)"
    v = _f((doc.get("amounts") or {}).get(service))
    if v is None or v <= 0:
        return None, "the board gives this service no capital"
    return v, "board"
