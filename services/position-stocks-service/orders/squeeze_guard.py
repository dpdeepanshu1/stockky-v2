"""orders/squeeze_guard.py - E274 round 41: no short while the stock is being squeezed (plan: "Squeeze guard").

A short has no ceiling on its loss, so it must not be opened while price sits at or above the opening-range HIGH, the volume of the latest completed 1-minute bars is
rising against the opening range, and the tape shows buyers: that is a stock being bought, not one that has failed. It blocks a short that matches no catalyst setup (the
legacy shortlist path; S2 / S3 shorts when they exist). It does NOT block S4_CATALYST_DOWN: S4 already needs price below the pre-open price and a completed bar closed below
the opening-range low.

  rising_volume(...)  pure: the last-3-bar volume as a multiple of the opening-range average when it is rising, else None.
  evaluate(...)       pure: the refusal text or None. A fact that is missing is "not true": the guard only blocks on something it can see (the other short gates stay fail-closed).
  reason(...)         reads the bars, the opening range and the tape verdict and calls evaluate. Switch SHORT_SQUEEZE_GUARD_ENABLED. Never raises.

Rising volume = the average volume of the last 3 completed bars after the opening range is above the opening-range bars' average AND above the 3 bars before them (when there are 6).
"""
from __future__ import annotations

import json
import logging
from typing import Any, List, Optional, Sequence, Tuple

import config

logger = logging.getLogger("position-stocks-squeeze-guard")

LAST_N = 3


def enabled() -> bool:
    return bool(getattr(config, "SHORT_SQUEEZE_GUARD_ENABLED", True))


def _num(v: Any) -> Optional[float]:
    try:
        x = float(v)
        return x if x == x and abs(x) != float("inf") else None
    except (TypeError, ValueError):
        return None


def rising_volume(range_vols: Sequence[float], after_vols: Sequence[float]) -> Optional[float]:
    """Latest-3-bar average volume as a multiple of the opening-range average when it is rising, else None."""
    try:
        rv = [float(v) for v in range_vols if v is not None and float(v) >= 0]
        av = [float(v) for v in after_vols if v is not None and float(v) >= 0]
        if not rv or len(av) < LAST_N:
            return None
        base = sum(rv) / len(rv)
        last = sum(av[-LAST_N:]) / LAST_N
        if base <= 0 or last <= base:
            return None
        if len(av) >= 2 * LAST_N and last <= sum(av[-2 * LAST_N:-LAST_N]) / LAST_N:
            return None
        return round(last / base, 2)
    except (TypeError, ValueError):
        return None


def evaluate(*, price: Any, or_high: Any, range_vols: Sequence[float], after_vols: Sequence[float], tape: int, s4: bool = False) -> Optional[str]:
    """The refusal text, or None. `tape` is +1 buying / -1 selling / 0 unknown (setup_live.tape_sign). `s4` = an S4_CATALYST_DOWN match: never blocked."""
    try:
        if s4:
            return None
        p, rh = _num(price), _num(or_high)
        if p is None or rh is None or p < rh:
            return None
        if not (isinstance(tape, (int, float)) and not isinstance(tape, bool) and tape > 0):
            return None
        ratio = rising_volume(range_vols, after_vols)
        if ratio is None:
            return None
        return f"SQUEEZE: price {p:.2f} is at or above the opening-range high {rh:.2f}, volume rising ({ratio:.1f}x the opening range) and the tape shows buyers"
    except Exception:  # noqa: BLE001
        return None


def _split(bars: Any, now_ts: float, open_ts: float, end_ts: float) -> Tuple[List[float], List[float]]:
    """Volumes of the completed 1-minute bars inside the opening range and after it."""
    from orders import setup_inputs
    rows = setup_inputs.closed_bars(bars, now_ts)
    return [r[5] for r in rows if open_ts <= r[0] < end_ts], [r[5] for r in rows if r[0] >= end_ts]


def reason(*, symbol: str, price: Any, now: Any, tape_json: Any, s4: bool = False, bars: Any = None) -> Optional[str]:
    """The guard over live inputs. The opening-range high comes from the recorded bars (orb range helper, cached per day, not limited to the ORB entry window);
    `tape_json` is the flow_json the short plan's tape check returned (dict or JSON text). Never raises."""
    try:
        if not enabled() or s4:
            return None
        from tz_utils import ist_now
        from orders import orb_shadow, setup_live
        n = ist_now(now)
        sym = str(symbol or "").strip().upper()
        if bars is None:
            from feed import bar_store
            bars = bar_store.get_bars(sym)
        rng = orb_shadow.bar_range(sym, now, bars)
        if not rng or _num(rng.get("high")) is None:
            return None
        re_t = orb_shadow._t("ORB_RANGE_END_IST", 9, 20)
        open_ts = n.replace(hour=9, minute=15, second=0, microsecond=0).timestamp()
        end_ts = n.replace(hour=re_t.hour, minute=re_t.minute, second=0, microsecond=0).timestamp()
        rvols, avols = _split(bars, n.timestamp(), open_ts, end_ts)
        tj = tape_json
        if isinstance(tj, str):
            tj = json.loads(tj)
        tape = setup_live.tape_sign(tj.get("tape") if isinstance(tj, dict) else None)
        return evaluate(price=price, or_high=rng.get("high"), range_vols=rvols, after_vols=avols, tape=tape, s4=s4)
    except Exception as e:  # noqa: BLE001
        logger.debug("squeeze guard failed for %s: %s", symbol, e)
        return None
