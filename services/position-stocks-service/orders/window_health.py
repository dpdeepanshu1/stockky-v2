"""orders/window_health.py - losing-window auto-disable and stop/target width advice (group 336, 2026-10-12).

LOSING-WINDOW AUTO-DISABLE
  The scalper scans several windows (1m / 5m / 15m / 60m). orders/review_stats.py already shows how each window did; nothing acted on it, so a
  window that kept losing kept getting entries. Now: from the closed trades of the last WINDOW_AUTODISABLE_DAYS days, a window is paused when it has
  >= WINDOW_AUTODISABLE_MIN_TRADES settled trades, a win rate below WINDOW_AUTODISABLE_MAX_WIN_RATE_PCT AND a negative total P&L. Candidates from a
  paused window are left out of the cycle (listed in the cycle summary). No state is stored: the window comes back by itself when its losing
  trades age out of the look-back, so a pause can never become permanent. The window with the best P&L is never paused (at least one window
  always trades). Manual trades and rows without a window are ignored. Off with WINDOW_AUTODISABLE_ENABLED=0; fails open.

STOP / TARGET WIDTH ADVICE  (advice only - nothing is applied)
  From the same closed trades' real excursions (highest / lowest price each trade reached): the target half of the trades actually reached, a stop
  wide enough that 80 % of the winners would not have been stopped out, how many losers had been >= half way to target, and the stop / target widths
  actually used. GET /tuning/stop-target. Needs >= WINDOW_TUNING_MIN_TRADES trades before it states numbers.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, Iterable, List, Optional, Tuple

import config
from orders import excursion, review_stats, trade_stats

logger = logging.getLogger("position-stocks-window-health")

_CACHE_TTL_S = 300.0
_cache: dict = {"at": 0.0, "disabled": {}}


def reset() -> None:
    _cache.update({"at": 0.0, "disabled": {}})


def _closed(rows: Iterable) -> list:
    return [r for r in rows
            if r.status not in review_stats._NOT_CLOSED and r.realized_pnl is not None and r.opened_at is not None
            and trade_stats.classify(r) in ("win", "loss", "breakeven")]


def evaluate(rows: Iterable) -> Dict[str, dict]:
    """window -> {trades, wins, win_rate_pct, total_pnl, disabled, reason}. Pure; never raises."""
    try:
        by: Dict[str, list] = {}
        for r in _closed(rows):
            w = str(getattr(r, "window_source", "") or "")
            if w and w != "?" and w.lower() != "manual":
                by.setdefault(w, []).append(r)
        out: Dict[str, dict] = {}
        min_trades = int(config.WINDOW_AUTODISABLE_MIN_TRADES)
        max_wr = float(config.WINDOW_AUTODISABLE_MAX_WIN_RATE_PCT)
        for w, g in by.items():
            pnls = [float(x.realized_pnl) for x in g]
            wins = sum(1 for p in pnls if p > 0)
            wr = 100.0 * wins / len(g)
            total = sum(pnls)
            bad = len(g) >= min_trades and wr < max_wr and total < 0
            out[w] = {"trades": len(g), "wins": wins, "win_rate_pct": round(wr, 1), "total_pnl": round(total, 2), "disabled": bad,
                      "reason": (f"{wins}/{len(g)} wins ({wr:.0f}% < {max_wr:.0f}%), net Rs {total:,.0f}" if bad else None)}
        if out and all(v["disabled"] for v in out.values()):      # at least one window always trades: spare the least-bad one
            keep = max(out, key=lambda k: out[k]["total_pnl"])
            out[keep].update(disabled=False, reason=None)
        return out
    except Exception as e:  # noqa: BLE001
        logger.debug("window evaluate failed (nothing disabled): %s", e)
        return {}


def _load_rows(db) -> list:
    from datetime import datetime, timedelta, timezone
    from models import ScalpPosition
    cutoff = (datetime.now(timezone.utc) - timedelta(days=int(config.WINDOW_AUTODISABLE_DAYS))).replace(tzinfo=None)
    return db.query(ScalpPosition).filter(ScalpPosition.opened_at >= cutoff).all()


def disabled_windows(db) -> Dict[str, str]:
    """{window: reason} for the windows paused right now (cached _CACHE_TTL_S). Never raises, {} when off / on any error."""
    try:
        if not config.WINDOW_AUTODISABLE_ENABLED:
            return {}
        now = time.monotonic()
        if _cache["at"] and now - _cache["at"] < _CACHE_TTL_S:
            return dict(_cache["disabled"])
        ev = evaluate(_load_rows(db))
        _cache.update(at=now, disabled={w: v["reason"] for w, v in ev.items() if v["disabled"]})
        return dict(_cache["disabled"])
    except Exception as e:  # noqa: BLE001
        logger.debug("window disabled_windows failed (nothing disabled): %s", e)
        try:
            db.rollback()
        except Exception as e2:  # noqa: BLE001
            logger.debug("window disabled_windows rollback failed: %s", e2)
        return {}


def filter_candidates(db, candidates: list) -> Tuple[list, List[str]]:
    """(kept candidates, ['SYMBOL:window', ...] dropped). Never raises; returns the input unchanged on any problem."""
    try:
        dis = disabled_windows(db)
        if not dis:
            return candidates, []
        kept: list = []
        dropped: List[str] = []
        for c in candidates:
            w = getattr(c, "window_label", None)
            if w in dis:
                dropped.append(f"{getattr(c, 'symbol', '?')}:{w}")
            else:
                kept.append(c)
        if dropped:
            logger.info("position-stocks: window(s) paused for losing - %s; %d candidate(s) left out (%s)",
                        "; ".join(f"{w} {r}" for w, r in dis.items()), len(dropped), ", ".join(dropped[:8]))
        return kept, dropped
    except Exception:  # noqa: BLE001
        return candidates, []


def _pct(values: List[float], q: float) -> Optional[float]:
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    k = (len(v) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def suggest(rows: Iterable) -> dict:
    """Advice on stop / target width from real excursions. Advice only. Never raises."""
    try:
        closed = _closed(rows)
        n = len(closed)
        out: dict = {"trades": n, "applied_automatically": False}
        need = int(config.WINDOW_TUNING_MIN_TRADES)
        if n < need:
            out["note"] = f"{n} settled trades with the lookback given; at least {need} are needed before any number is stated"
            return out
        exc = {id(r): excursion.excursion_pcts(r) for r in closed}
        gains = [g for g in (exc[id(r)]["max_gain_pct"] for r in closed) if g is not None]
        win_dd = [d for d in (exc[id(r)]["max_drawdown_pct"] for r in closed if trade_stats.classify(r) == "win") if d is not None]
        used_t = [float(r.adaptive_target_pct) for r in closed if r.adaptive_target_pct]
        used_s = [float(r.adaptive_stop_pct) for r in closed if r.adaptive_stop_pct]
        losers = [r for r in closed if trade_stats.classify(r) == "loss"]
        half = [r for r in losers if r.adaptive_target_pct and (exc[id(r)]["max_gain_pct"] or 0.0) >= 0.5 * float(r.adaptive_target_pct)]
        reached = sum(1 for r in closed if r.adaptive_target_pct and (exc[id(r)]["max_gain_pct"] or 0.0) >= float(r.adaptive_target_pct))
        t50 = _pct(gains, 0.5)
        s80 = _pct([abs(d) for d in win_dd], 0.8)
        out.update({
            "target_pct_used_avg": round(sum(used_t) / len(used_t), 2) if used_t else None,
            "stop_pct_used_avg": round(sum(used_s) / len(used_s), 2) if used_s else None,
            "suggested_target_pct": round(t50, 2) if t50 is not None else None,
            "suggested_stop_pct": round(s80, 2) if s80 is not None else None,
            "trades_that_reached_used_target_pct": round(100.0 * reached / n, 1),
            "losers": len(losers), "losers_that_got_halfway_to_target": len(half),
            "how_to_read": ("suggested_target_pct = the gain half of all trades reached at some point (a target above it is hit by fewer than half); "
                            "suggested_stop_pct = a stop wide enough that 80% of the winners would not have been stopped out. "
                            "A tighter stop than the suggestion cuts winners; losers that got halfway to target point at a "
                            "breakeven / trailing step rather than a wider target. Advice only - change the knobs yourself."),
        })
        return out
    except Exception as e:  # noqa: BLE001
        logger.debug("window suggest failed: %s", e)
        return {"trades": 0, "applied_automatically": False, "note": f"could not compute ({type(e).__name__})"}
