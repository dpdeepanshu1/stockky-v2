"""fno/capital.py - F&O's side of the three-way capital split (round 242; the rule and the files are in capital_board.py, the same file in all three services).

Before this round F&O sized every order from FNO_DEMO_CAPITAL (a virtual Rs 5,00,000) even in REAL mode, never read Dhan's balance and the other two services did not know it existed.
Now: (1) publish() writes this service's figures (net P&L after charges over the window, closed REAL trades, capital, open premium, and whether it can spend real money) to the board folder;
(2) real_capital() gives REAL sizing the rupee amount Real Trade's board allots to F&O. No fresh board, or no capital for F&O = 0 = no REAL entry (fail closed). DEMO is unchanged.
Everything here never raises."""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import capital_board

from . import execution
from .store import IST, Store, day_of

SERVICE = "fno"
_last_pub = {"t": 0.0}
PUBLISH_EVERY_S = 30.0


def _mode(cfg: Any) -> str:
    return capital_board.norm_mode(getattr(cfg, "CAPITAL_ALLOC_MODE", "off"))


def real_capital(cfg: Any) -> Tuple[float, str]:
    """(rupees REAL sizing may use, why). 0.0 means no REAL entry."""
    try:
        fixed = float(getattr(cfg, "FNO_REAL_FIXED_CAPITAL", 0.0) or 0.0)      # round 387 (E403)
        if fixed > 0 and fixed == fixed and fixed != float("inf"):
            return fixed, "fixed F&O amount (FNO_REAL_FIXED_CAPITAL)"
        if _mode(cfg) == "off":
            return 0.0, "CAPITAL_ALLOC_MODE is off on F&O: REAL sizing needs the capital board (set shadow or live; Real Trade must write it)"
        amt, why = capital_board.amount_for(str(getattr(cfg, "CAPITAL_ALLOC_DIR", "/data/shared")), SERVICE, float(getattr(cfg, "CAPITAL_ALLOC_MAX_AGE_S", 21600.0)))
        return (float(amt), "capital board") if amt else (0.0, why)
    except Exception as e:  # noqa: BLE001
        return 0.0, f"capital board unreadable ({type(e).__name__})"


def report_capital(cfg: Any, mode: Any) -> float:
    """Round 251: the capital the history / strategy reports measure return % and max drawdown % against. DEMO (and any other mode) = FNO_DEMO_CAPITAL; REAL = what the capital board allots to F&O,
    or 0.0 when there is no fresh board (the report then leaves the % out, it never shows a % of a made-up Rs 5,00,000). Never raises."""
    try:
        if str(mode or "").upper() == "REAL":
            return float(real_capital(cfg)[0])
        return float(getattr(cfg, "FNO_DEMO_CAPITAL", 0.0) or 0.0)
    except Exception:  # noqa: BLE001
        return 0.0


def can_spend_real(cfg: Any, armed_day: Optional[str], today: str) -> bool:
    """True when every REAL lock is open today (engine on, mode REAL, REAL enabled, armed): only then does F&O hold a share of the real account."""
    try:
        return bool(execution.real_allowed(cfg, armed_day, today)[0])
    except Exception:  # noqa: BLE001
        return False


def stats(store: Store, cfg: Any, today: Optional[str] = None) -> Dict[str, Any]:
    """REAL net P&L after charges and closed-trade count over the window, and the premium paid for the REAL positions still open.
    Round 300: `trades` counts TRADES, like `Store.day_summary`: a NOT_FILLED entry (booked CLOSED with net 0.0) is not a trade, and a partial-exit child row belongs to its parent's trade
    (counted once, only when the whole family is closed). The capital board gives the bonus share only from `min_trades` closed trades, so counting rows let F&O earn it early.
    Net P&L still sums every closed row (a partial exit's money is real the moment it is sold)."""
    today = today or day_of(time.time())
    since = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=int(getattr(cfg, "CAPITAL_ALLOC_WINDOW_DAYS", 30)))).strftime("%Y-%m-%d")
    net = 0.0
    fam: Dict[Any, List[Any]] = {}
    for p in store.positions("REAL", since_day=since, limit=5000):
        if p.exit_reason == "NOT_FILLED":
            continue
        fam.setdefault(p.parent_id if p.parent_id is not None else p.id, []).append(p)
        if p.status == "CLOSED" and p.net_pnl is not None:
            net += float(p.net_pnl)
    n = sum(1 for rows in fam.values() if all(x.status == "CLOSED" and x.net_pnl is not None for x in rows))
    exposure = sum(float(p.entry_premium) * p.qty for p in store.open_positions("REAL"))
    return {"net_pnl": net, "trades": n, "exposure": exposure}


def publish(store: Store, cfg: Any, armed_day: Optional[str], now: Optional[float] = None, force: bool = False) -> bool:
    """Write F&O's figures to the board folder (at most every PUBLISH_EVERY_S). Never raises."""
    try:
        mode = _mode(cfg)
        if mode == "off":
            return False
        t = now if now is not None else time.time()
        if not force and t - _last_pub["t"] < PUBLISH_EVERY_S:
            return False
        _last_pub["t"] = t
        today = day_of(t)
        s = stats(store, cfg, today)
        amt, _why = capital_board.amount_for(str(getattr(cfg, "CAPITAL_ALLOC_DIR", "/data/shared")), SERVICE, float(getattr(cfg, "CAPITAL_ALLOC_MAX_AGE_S", 21600.0)))
        return capital_board.write_stats(str(getattr(cfg, "CAPITAL_ALLOC_DIR", "/data/shared")), SERVICE, net_pnl=s["net_pnl"], trades=s["trades"], capital_base=amt or 0.0, exposure=s["exposure"],
                                         active=can_spend_real(cfg, armed_day, today), mode=mode, window_days=int(getattr(cfg, "CAPITAL_ALLOC_WINDOW_DAYS", 30)), now=t)
    except Exception:  # noqa: BLE001
        return False


def status(cfg: Any, armed_day: Optional[str], today: str) -> Dict[str, Any]:
    mode = _mode(cfg)
    d = str(getattr(cfg, "CAPITAL_ALLOC_DIR", "/data/shared"))
    board = capital_board.read_alloc(d, float(getattr(cfg, "CAPITAL_ALLOC_MAX_AGE_S", 21600.0))) if mode != "off" else None
    cap, why = real_capital(cfg)
    return {"mode": mode, "this_service": SERVICE, "active_for_real": can_spend_real(cfg, armed_day, today), "real_sizing_capital": cap, "real_sizing_source": why,
            "demo_capital": float(getattr(cfg, "FNO_DEMO_CAPITAL", 0.0)), "board": board}
