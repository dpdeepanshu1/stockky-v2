"""E502: crash-proof numeric settings read from the environment.

The gateway modules read their tuning knobs with `int(((os.getenv("X") or "").strip() or "D"))` (blank-safe, not typo-safe) or, in a few places,
`int(os.getenv("X", "D"))` (not even blank-safe: `X=` in an env_file or a blank dashboard variable). Those lines run when the module is imported,
so one typo or one blank variable stopped the whole gateway from booting. These helpers never raise:

  * unset                              -> `default`
  * blank / whitespace only            -> `default` (or `blank`, when a site always treated blank differently from unset)
  * non-numeric text, NaN, +/-inf      -> `default`
  * a valid number                     -> that number (`env_int_safe` also accepts "5.0" and truncates it)

A valid value gives exactly what the old expression gave.

E506: optional range arguments. A value that parses but would stall or break the code that uses it (a pool size of 0, a zero or negative
timeout / interval, a ratio above 1, a percentage above 100) is treated like a typo: it falls back to `default`, with one log line per setting.
  * lo=X        -> a value below X is rejected          * hi=X   -> a value above X is rejected
  * positive=True -> a value <= 0 is rejected (for settings where 0 would stall something)
With no range argument the behaviour is the E502 one, unchanged.
"""
from __future__ import annotations

import logging
import math
import os

_log = logging.getLogger(__name__)
_warned = set()


def _out_of_range(name, raw, v, lo, hi, positive):
    why = None
    if positive and not v > 0:
        why = "must be greater than 0"
    elif lo is not None and v < lo:
        why = f"must be at least {lo}"
    elif hi is not None and v > hi:
        why = f"must be at most {hi}"
    if why and (name, raw) not in _warned:
        _warned.add((name, raw))
        try:
            _log.warning("env %s=%r ignored (%s); using the default", name, raw, why)
        except Exception:
            pass
    return why is not None


def env_float_safe(name: str, default: float, blank: float = None, lo: float = None, hi: float = None, positive: bool = False) -> float:
    raw = os.getenv(name)
    if raw is None:
        return float(default)
    raw = raw.strip()
    if not raw:
        return float(default if blank is None else blank)
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(v):
        return float(default)
    if (lo is not None or hi is not None or positive) and _out_of_range(name, raw, v, lo, hi, positive):
        return float(default)
    return v


def env_int_safe(name: str, default: int, blank: int = None, lo: int = None, hi: int = None, positive: bool = False) -> int:
    raw = os.getenv(name)
    if raw is None:
        return int(default)
    raw = raw.strip()
    if not raw:
        return int(default if blank is None else blank)
    try:
        v = int(raw)
    except (TypeError, ValueError):
        try:
            f = float(raw)
        except (TypeError, ValueError):
            return int(default)
        if not math.isfinite(f):
            return int(default)
        v = int(f)
    if (lo is not None or hi is not None or positive) and _out_of_range(name, raw, v, lo, hi, positive):
        return int(default)
    return v
