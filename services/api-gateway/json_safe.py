"""NaN/Inf-safe JSON helpers for FastAPI responses."""
from __future__ import annotations

import math
from typing import Any


def sanitize(obj: Any) -> Any:
    if obj is None:
        return None
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        # json.dumps cannot serialise sets. Sort for a stable wire order;
        # fall back to iteration order when members are not mutually orderable.
        try:
            items = sorted(obj)
        except Exception:
            items = list(obj)
        return [sanitize(v) for v in items]
    try:
        import numpy as np
        if isinstance(obj, (np.floating,)):
            f = float(obj)
            return None if (math.isnan(f) or math.isinf(f)) else f
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, np.bool_):
            # np.bool_ is not a bool subclass; json.dumps rejects it.
            return bool(obj)
        if isinstance(obj, np.ndarray):
            return [sanitize(x) for x in obj.tolist()]
    except Exception:
        pass
    return obj
