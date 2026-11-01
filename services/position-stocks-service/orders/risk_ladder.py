"""orders/risk_ladder.py - E276 round 54: the Position Stocks risk ladder (pure). A setup earns a higher shared-heat limit only from its own closed trades.

Stage 0 (start): the base heat limit (HEAT_LIMIT_PCT).
Stage 1: >= STAGE1_TRADES closed trades of the setup (net, setup stamp readable) and the 90 % lower bound of its average R above 0.
Stage 2: >= STAGE2_TRADES closed trades and the lower bound above STAGE2_MIN_LB_R (+0.05R).
Stage 3 (E276 round 57): stage 2 now AND stage 2 on the setup's trades from before the last 60 trading days (sustained); heat base + 3 steps (8 %), risk and short cap as stage 2.
Brake: the stage drops one level when the last BRAKE_TRADES closed trades (all setups) are net negative; stage 0 never goes lower than the base limit.
heat_limit_pct(base, stage, step) = base + stage x step. Stage 3 (60 sustained trading days) and the risk-per-trade rise are a later round.
E276 round 56: risk_mult(stage) raises the per-trade risk (x1.25 stage 1, x1.5 stage 2; ledger.reserve_capital risk_mult).
E276 round 55: the live short-position cap steps up with the stage (short_cap: base, then 3, then 5), and a second brake drops one stage when the daily loss limit
was hit on 2 or more of the last 5 trading days (loss_days_brake, from the days' net closed P&L against the limit % of the pool).
No I/O, never raises: anything unreadable is stage 0."""
from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

STAGE1_TRADES = 100
STAGE2_TRADES = 300
STAGE2_MIN_LB_R = 0.05
BRAKE_TRADES = 20
MAX_STAGE = 3
SLOT_MIN_TRADES = 30                            # round 58: a time slot with this many closed trades and an average R below 0 holds the setup at stage 0 in that slot
SUSTAIN_DAYS = 60                               # stage 3: stage 2 already held on the trades from before the last 60 trading days, and holds now
LOSS_DAY_SESSIONS = 5
LOSS_DAY_HITS = 2
SHORT_CAP_BY_STAGE = (0, 3, 5, 5)                  # stage 0 keeps the configured base; stage 1 -> 3; stage 2 -> 5 (never below the base)


def _f(v: Any) -> Optional[float]:
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def stage_from_cell(cell: Optional[Dict[str, Any]]) -> int:
    """Stage (0..2) from one setup_stats cell ("<setup id>|ALL"): needs r_n (trades with a readable R) and lower_bound_r."""
    try:
        if not cell:
            return 0
        n = int(cell.get("r_n") if cell.get("r_n") is not None else (cell.get("n") or 0))
        lb = _f(cell.get("lower_bound_r"))
        if lb is None:
            return 0
        if n >= STAGE2_TRADES and lb > STAGE2_MIN_LB_R:
            return 2
        if n >= STAGE1_TRADES and lb > 0:
            return 1
        return 0
    except Exception:  # noqa: BLE001
        return 0


def brake(last_pnls: Iterable[Any]) -> bool:
    """True when the last BRAKE_TRADES closed trades (newest first, net P&L) are net negative. Needs a full window."""
    try:
        v = [x for x in (_f(p) for p in list(last_pnls)[:BRAKE_TRADES]) if x is not None]
        return len(v) >= BRAKE_TRADES and sum(v) < 0
    except Exception:  # noqa: BLE001
        return False


def slot_blocked(slot_cell: Optional[Dict[str, Any]]) -> bool:
    """E276 round 58: True when the setup's own cell for this time slot has SLOT_MIN_TRADES or more closed trades and an average R below 0 (a losing slot never earns the ladder).
    No cell, too few trades or an unreadable average = False (the slot does not block)."""
    try:
        if not slot_cell:
            return False
        n = int(slot_cell.get("r_n") if slot_cell.get("r_n") is not None else (slot_cell.get("n") or 0))
        a = _f(slot_cell.get("avg_r"))
        return a is not None and n >= SLOT_MIN_TRADES and a < 0
    except Exception:  # noqa: BLE001
        return False


def loss_days_brake(day_pnls: Iterable[Any], pool: Any, limit_pct: Any) -> bool:
    """True when, among the last LOSS_DAY_SESSIONS trading days (newest first, net closed P&L per day), at least LOSS_DAY_HITS lost the daily loss limit
    (a loss of limit_pct % of the pool or more). No pool, no limit or fewer readable days than a hit needs = False."""
    try:
        p, lim = _f(pool), _f(limit_pct)
        if not p or p <= 0 or not lim or lim <= 0:
            return False
        v = [x for x in (_f(d) for d in list(day_pnls)[:LOSS_DAY_SESSIONS]) if x is not None]
        return sum(1 for x in v if x <= -(lim / 100.0) * p) >= LOSS_DAY_HITS
    except Exception:  # noqa: BLE001
        return False


def stage(cell: Optional[Dict[str, Any]], last_pnls: Iterable[Any] = (), loss_days: bool = False, older_cell: Optional[Dict[str, Any]] = None) -> int:
    s = stage_from_cell(cell)
    if s == 2 and stage_from_cell(older_cell) == 2:       # stage 2 held 60 trading days ago and still holds
        s = 3
    if s > 0 and brake(last_pnls):
        s -= 1
    if s > 0 and loss_days:
        s -= 1
    return max(0, min(MAX_STAGE, s))


def risk_mult(stg: int, step: Any = 0.25) -> float:
    """Per-trade risk multiple at a stage: 1.0 at stage 0 (today's 2 %), 1 + step per stage (0.25 -> 1.25x = 2.5 %, 1.5x = 3 %). Never below 1.0."""
    try:
        return 1.0 + max(0, min(2, int(stg or 0))) * max(0.0, _f(step) or 0.0)       # 3 % cap: stage 3 risks like stage 2
    except Exception:  # noqa: BLE001
        return 1.0


def short_cap(base: Any, stg: int) -> int:
    """Live short-position cap at a stage: the configured base at stage 0, 3 at stage 1, 5 at stage 2, never below the base, never above 5."""
    try:
        b = int(_f(base) or 0)
    except Exception:  # noqa: BLE001
        b = 0
    s = max(0, min(MAX_STAGE, int(stg or 0)))
    return max(1, min(5, max(b, SHORT_CAP_BY_STAGE[s])))


def heat_limit_pct(base_pct: Any, stg: int, step_pct: Any = 1.0) -> float:
    b = _f(base_pct) or 0.0
    if b <= 0:                                  # 0 = no limit stays no limit
        return b
    return b + max(0, min(MAX_STAGE, int(stg or 0))) * max(0.0, _f(step_pct) or 0.0)
