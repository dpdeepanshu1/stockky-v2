"""
NaN/Inf-safe JSON responses for the training service.

Why this exists: Starlette's default JSONResponse renders with
`json.dumps(..., allow_nan=False)`. Any float that is NaN, +Inf or -Inf
(which show up all the time in ML metrics — a Sharpe ratio with zero variance,
a win-rate with zero evaluated trades, a 0/0 return, an unbounded z-score)
raises `ValueError: Out of range float values are not JSON compliant`, which
FastAPI turns into a 500. That is exactly the
"Failed to trigger T+1 evaluation: 500: Out of range float values are not JSON
compliant" (and the T+5 / market-scan "drive not coming") errors in the logs.

Fix: sanitize the payload (replace non-finite floats with None, coerce numpy /
Decimal / datetime to JSON-native types) BEFORE dumping, then dump with
allow_nan=False so the wire is always strict, valid JSON the frontend can parse.

Usage (drop-in): import SafeJSONResponse as JSONResponse, and set it as the
FastAPI app's default_response_class so even endpoints that `return {...}`
directly get sanitized.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi.responses import JSONResponse

# Import numpy once (guarded) so the recursive walk stays cheap on big payloads.
try:  # pragma: no cover
    import numpy as _np
    _HAS_NP = True
except Exception:  # pragma: no cover
    _np = None
    _HAS_NP = False


def _clean(o: Any) -> Any:
    # Plain Python float: the core fix — non-finite -> None.
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, bool):  # keep before int (bool is a subclass of int)
        return o
    if isinstance(o, int):
        return o
    if o is None or isinstance(o, str):
        return o
    if isinstance(o, dict):
        return {(_clean(k) if not isinstance(k, str) else k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [_clean(v) for v in o]
    if isinstance(o, Decimal):
        f = float(o)
        return f if math.isfinite(f) else None
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if _HAS_NP:
        if isinstance(o, _np.floating):
            f = float(o)
            return f if math.isfinite(f) else None
        if isinstance(o, _np.integer):
            return int(o)
        if isinstance(o, _np.bool_):
            return bool(o)
        if isinstance(o, _np.ndarray):
            return [_clean(v) for v in o.tolist()]
    return o


def sanitize(obj: Any) -> Any:
    """Return a copy of obj safe for strict JSON: no NaN/Inf, no numpy scalars,
    datetimes as ISO strings. Never raises."""
    try:
        return _clean(obj)
    except Exception:
        # Last resort: stringify anything that still can't be walked.
        try:
            return json.loads(json.dumps(obj, default=str))
        except Exception:
            return None


class SafeJSONResponse(JSONResponse):
    """JSONResponse that can never emit non-compliant floats."""

    def render(self, content: Any) -> bytes:
        return json.dumps(
            sanitize(content),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
