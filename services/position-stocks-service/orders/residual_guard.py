"""orders/residual_guard.py - E285 round 167: ALERT-ONLY check that Dhan's net INTRADAY position matches what the app believes it holds.

WHY (JYOTICNC, 3 Nov 2026): the app booked a short as closed while Dhan showed LONG 1 (a leftover leg order filled after the exit). Nothing compared the two, so the extra
share sat unmanaged, with no stop, until Dhan's own square-off. This check finds that kind of difference whatever its cause.

WHAT: at most once per RESIDUAL_GUARD_INTERVAL_S, when this service traded any stock today, read Dhan's positions once. For each INTRADAY row of a security this service
traded today with netQty != 0, compare it with the app's expectation: the sum of its OPEN / EXIT_LEGS_REJECTED positions in that security (+ quantity for a long, - for a short).
Different for RESIDUAL_GUARD_CONFIRM_S in a row -> ONE critical Telegram alert per (security, Dhan net, expected). A security with an exit still settling (a *_PENDING_RECONCILE row,
or closed in the last two minutes) is skipped. Dhan flat while the app thinks it is open is NOT alerted (stop fills are booked by reconcile). It never sends an order.
Known limit: a security that real-trade-service also holds INTRADAY on the same day can raise a false alert; it is only an alert. Never raises. RESIDUAL_GUARD_ENABLED=0 = off.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("position-stocks-residual-guard")

_IST = timezone(timedelta(hours=5, minutes=30))
_OPEN_STATUSES = ("OPEN", "EXIT_LEGS_REJECTED")
_last_run: List[float] = [0.0]
_seen: Dict[Tuple[str, int, int], float] = {}      # (security id, Dhan net, expected) -> monotonic time first seen
_alerted: set = set()


def reset_state() -> None:
    """Test helper."""
    _last_run[0] = 0.0
    _seen.clear()
    _alerted.clear()


def _cfg(name: str, default: Any) -> Any:
    try:
        import config
        return getattr(config, name, default)
    except Exception:  # noqa: BLE001
        return default


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)      # opened_at / closed_at are stored naive UTC


def expected_net(positions: list, sid: str) -> int:
    """Pure. Shares the app believes it holds in this security right now (+ long, - short)."""
    from orders import side as _side
    total = 0
    for p in positions:
        if str(getattr(p, "dhan_security_id", "") or "") != sid or getattr(p, "status", None) not in _OPEN_STATUSES:
            continue
        if getattr(p, "overnight_converted_to_cnc", False):
            continue
        q = int(getattr(p, "quantity", 0) or 0)
        total += -q if _side.is_short(p) else q
    return total


def _settling(positions: list, sid: str, now: datetime) -> bool:
    for p in positions:
        if str(getattr(p, "dhan_security_id", "") or "") != sid:
            continue
        if "PENDING_RECONCILE" in str(getattr(p, "error_message", "") or ""):
            return True
        c = _aware(getattr(p, "closed_at", None))
        if c is not None and (now - c).total_seconds() < 120:
            return True
    return False


def _dhan() -> Any:
    from execution import dhan_client
    return dhan_client


def _todays_positions(db: Any, now: datetime) -> list:
    from models import ScalpPosition
    start = now.astimezone(_IST).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = db.query(ScalpPosition).filter(ScalpPosition.opened_at >= (now - timedelta(hours=36)).replace(tzinfo=None)).all()
    return [p for p in rows if (_aware(p.opened_at) or now) >= start]


def run_residual_guard(db: Any, now: Optional[datetime] = None, mono: Optional[float] = None) -> int:
    """Returns the number of alerts sent this pass. Never raises."""
    try:
        return _run(db, now or datetime.now(timezone.utc), time.monotonic() if mono is None else mono)
    except Exception:  # noqa: BLE001
        logger.warning("residual guard failed", exc_info=True)
        return 0


def _run(db: Any, now: datetime, mono: float) -> int:
    if not _cfg("RESIDUAL_GUARD_ENABLED", True):
        return 0
    if _last_run[0] and mono - _last_run[0] < float(_cfg("RESIDUAL_GUARD_INTERVAL_S", 60.0)):
        return 0
    positions = _todays_positions(db, now)
    sids = {str(p.dhan_security_id) for p in positions if getattr(p, "dhan_security_id", None)}
    if not sids:
        return 0
    _last_run[0] = mono
    try:
        rows = _dhan().get_positions(db)
    except Exception as e:  # noqa: BLE001
        logger.warning("residual guard: Dhan positions unreadable (%s)", e)
        return 0
    confirm = float(_cfg("RESIDUAL_GUARD_CONFIRM_S", 90.0))
    names = {str(p.dhan_security_id): p.symbol for p in positions}
    alerts = 0
    live_keys = set()
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        sid = str(r.get("securityId") or r.get("security_id") or "")
        if sid not in sids or str(r.get("productType") or r.get("product_type") or "").upper() != "INTRADAY":
            continue
        try:
            net = int(float(r.get("netQty", r.get("net_qty")) or 0))
        except (TypeError, ValueError):
            continue
        exp = expected_net(positions, sid)
        if net == 0 or net == exp or _settling(positions, sid, now):
            continue
        key = (sid, net, exp)
        live_keys.add(key)
        first = _seen.setdefault(key, mono)
        if mono - first >= confirm and key not in _alerted:
            _alerted.add(key)
            alerts += 1
            sym = r.get("tradingSymbol") or names.get(sid) or sid
            what = "no open position" if exp == 0 else f"an open position of {exp:+d}"
            _notify(f"POSITION MISMATCH: Dhan shows {sym} net {net:+d} (INTRADAY, buy avg {r.get('buyAvg')}, sell avg {r.get('sellAvg')}) but this app has {what}. "
                    f"Nothing is managing the difference (no stop, no target). Check Dhan and close it by hand if it is not wanted.")
    for k in [k for k in _seen if k not in live_keys]:     # a mismatch that cleared is forgotten, so it can alert again later
        _seen.pop(k, None)
        _alerted.discard(k)
    return alerts


def _notify(text: str) -> None:
    try:
        import notifier
        notifier.notify_critical(text)
    except Exception:  # noqa: BLE001
        logger.warning("residual guard: alert not sent", exc_info=True)
