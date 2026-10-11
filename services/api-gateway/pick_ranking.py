"""pick_ranking.py - value-adjusted ranking of scan picks (extracted from main.py, group 320).

First step of splitting api-gateway/main.py (14k lines): a block with no I/O, no module state and no dependency on
anything else in main.py, so it moves verbatim. main.py re-exports every name (`from pick_ranking import ...`), so
`main._select_top_picks` etc. keep working for the routes and the tests. Not extracted on purpose: helpers that tests
patch through `main` (datetime, httpx, the Redis helpers) - moving those would silently stop the patches working.
"""
from __future__ import annotations

# ── Value-buying adjustment for top-pick ranking ────────────────────────────
# A stock priced under Rs 2000 with already-decent fundamentals gets a score
# bonus (bigger the further under Rs 2000), so a Rs 200 stock with solid
# fundamentals is favored over a Rs 1900 stock, all else equal. Cheap alone
# isn't rewarded — only cheap + fundamentally sound. Stocks over Rs 2000 are
# excluded entirely from "recommendations" (top picks), though they still
# appear normally in all_results — this only changes which ones get
# surfaced as headline picks (and what Send Top 5 / notifications send).
VALUE_PRICE_CAP = 2000.0
VALUE_BONUS_MAX = 8.0
VALUE_MIN_FUNDAMENTAL_FOR_BONUS = 50.0

def _value_adjusted_score(r: dict):
    """Returns (adjusted_score, eligible_for_top_pick).
    Price > VALUE_PRICE_CAP no longer hard-excludes a name — only removes
    the cheap-stock bonus. High-score large-caps must still surface as picks.
    """
    price = r.get("close")
    combined = r.get("combined_score", 0) or 0
    if price is None or price <= 0:
        return combined, True
    fundamental = r.get("fundamental_score", 0) or 0
    bonus = 0.0
    if price <= VALUE_PRICE_CAP and fundamental >= VALUE_MIN_FUNDAMENTAL_FOR_BONUS:
        bonus = (1 - price / VALUE_PRICE_CAP) * VALUE_BONUS_MAX
    return combined + bonus, True

def _select_top_picks(actionable: list, limit: int = 5) -> list:
    """Value-adjusted ranking for recommendations specifically — the raw
    combined_score sort on results/all_results is left untouched."""
    eligible = [r for r in actionable if _value_adjusted_score(r)[1]]
    eligible.sort(key=lambda r: _value_adjusted_score(r)[0], reverse=True)
    return eligible[:limit]

def _board_rank_key(row: dict) -> float:
    """Sort key for a horizon board: the horizon score plus the same cheap-stock
    value bonus the verdict's `_select_top_picks` uses, so the list people see is
    ranked the way the verdict counts it. `_hz_score` itself stays the raw score."""
    try:
        sc = float(row.get("_hz_score") or 0)
    except (TypeError, ValueError):
        sc = 0.0
    try:
        combined = float(row.get("combined_score") or 0)
        bonus = float(_value_adjusted_score(row)[0]) - combined
    except Exception:
        bonus = 0.0
    return sc + bonus
