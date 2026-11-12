"""
shared/return_sanity.py  §6 of the master implementation prompt.

Corporate-action clamp for ATR/percentile inputs.

Genuine single-day price discontinuities (demergers, new-listing volatility,
large corporate actions) sit inside a rolling 14-day ATR window for two weeks
after they happen, inflating volatility readings and potentially triggering
false gap-down/emergency-exit logic or blocking entries for weeks.

Apply clamp_for_atr() to every daily return before feeding into:
  - ATR(14) in technical/main.py
  - Exit trailing-stop ATR in exit_engine/exit.py
  - Entry drift sizing ATR in entry_engine/entry.py
  - Any rolling window feeding adaptive_gate / hybrid_gate
"""
from __future__ import annotations
import math
from typing import Optional

# If a single day's move exceeds this %, treat it as a corporate-action
# discontinuity and exclude it from ATR/percentile inputs.
# Cross-check against symbol_master's corporate-action data once that table
# exists — this is the safe default before it does.
def _env_float(name: str, default: float) -> float:
    """E504: blank / non-numeric / NaN / inf setting -> default. A typo in CORPORATE_ACTION_JUMP_THRESHOLD used to stop the importing service at start-up.
    Self-contained on purpose: this file is copied into more than one service."""
    try:
        v = float((__import__("os").getenv(name) or "").strip() or default)
    except (TypeError, ValueError):
        return float(default)
    return v if math.isfinite(v) and v > 0 else float(default)   # E507: a zero / negative jump threshold flags every move (or none)


CORPORATE_ACTION_JUMP_THRESHOLD = _env_float("CORPORATE_ACTION_JUMP_THRESHOLD", 30.0)


def clamp_for_atr(daily_return_pct: float) -> Optional[float]:
    """
    Return None (exclude from ATR/percentile windows) if a move looks like
    a corporate-action discontinuity rather than real trading.
    Return the value unchanged if within the normal trading range.

    ATR(14) is safe with excluded values — it simply uses fewer inputs.
    Zero divide-by-zero risk; only the warmup period shortens slightly.
    """
    # E328: NaN / inf are excluded too (`abs(nan) > 30` is False, so NaN used to pass straight into the ATR / trail maths)
    if not math.isfinite(daily_return_pct) or abs(daily_return_pct) > CORPORATE_ACTION_JUMP_THRESHOLD:
        return None
    return daily_return_pct


def atr_input_series(daily_returns: list) -> list:
    """
    Filter a list of daily return percentages, excluding corporate-action
    jumps. Feed this clamped series into ATR(14) instead of raw returns.

    Usage in entry_engine/entry.py and exit_engine/exit.py:
        from return_sanity import atr_input_series
        clean = atr_input_series(raw_daily_returns)
        # compute ATR(14) on `clean`
    """
    return [r for r in (clamp_for_atr(x) for x in daily_returns) if r is not None]


def clamp_series(values: list, threshold: float = CORPORATE_ACTION_JUMP_THRESHOLD) -> list:
    """Generic clamper — same logic, configurable threshold."""
    return [v for v in values if v is not None and math.isfinite(v) and abs(v) <= threshold]
