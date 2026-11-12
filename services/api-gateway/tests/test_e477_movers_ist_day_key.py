"""E477: `_get_nifty50_data` keyed the day's movers cache and stamped the last-known label with the server's local `datetime.now()`. The
host runs UTC (no TZ is set in docker-compose.yml or the Dockerfile), so the day key rolled at 05:30 IST instead of midnight and the
`stale_since` label shown to users was 5.5 h behind. Now both use `datetime.now(IST)`. Needs fastapi (imports main). Runs with pytest."""
from __future__ import annotations

import ast
import os
import sys
from datetime import datetime as _real_dt, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = os.path.dirname(_HERE)
if _SERVICE not in sys.path:
    sys.path.insert(0, _SERVICE)

import main as gw  # noqa: E402

# 2026-11-11 20:00 UTC == 2026-11-12 01:30 IST
_NOW_UTC = _real_dt(2026, 11, 11, 20, 0, tzinfo=timezone.utc)


class _FakeDT:
    @staticmethod
    def now(tz=None):
        return _NOW_UTC.astimezone(tz) if tz is not None else _NOW_UTC.replace(tzinfo=None)


def test_day_key_is_the_ist_date(monkeypatch):
    seen = []
    monkeypatch.setattr(gw, "datetime", _FakeDT)
    monkeypatch.setattr(gw, "_redis_get", lambda k: (seen.append(k), [{"symbol": "X"}])[1])
    assert gw._get_nifty50_data() == [{"symbol": "X"}]
    assert seen == [f"{gw.MARKET_MOVERS_CACHE_PREFIX}2026-11-12"]


def test_no_naive_datetime_now_left_in_the_movers_function():
    src = open(os.path.join(_SERVICE, "main.py"), encoding="utf-8").read()
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "_get_nifty50_data")
    bad = [c.lineno for c in ast.walk(fn) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
           and c.func.attr == "now" and not c.args and not c.keywords]
    assert bad == [], bad
