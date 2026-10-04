"""group 140: the decision service's cache TTL treats an NSE weekday holiday as closed.

`_is_market_open()` only checked weekday + hours, so on e.g. Fri 2026-10-02 decisions were cached for the
short open-session TTL all day. The real function and the holiday literal are extracted from the source
with ast, so this runs without fastapi/httpx.
Run from services/decision-prediction-service/decision:  python3 -m pytest tests/test_market_open_holiday.py -q
"""
from __future__ import annotations

import ast
import os
import textwrap
from datetime import datetime
from zoneinfo import ZoneInfo

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
IST = ZoneInfo("Asia/Kolkata")


def _is_market_open_at(when):
    src = open(_SRC, encoding="utf-8").read()
    tree = ast.parse(src)
    keep = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name == "_is_market_open":
            keep.append(n)
        elif isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "_NSE_HOLIDAYS_2026" for t in n.targets):
            keep.append(n)
    assert len(keep) == 2, "holiday literal and _is_market_open must both exist"

    class _FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return when.astimezone(tz) if tz else when

    ns = {"datetime": _FakeDT, "IST": IST}
    exec(compile(ast.Module(body=keep, type_ignores=[]), _SRC, "exec"), ns)
    return ns["_is_market_open"]()


def _ist(y, mo, d, h=11, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=IST)


def test_holiday_in_session_hours_is_closed():
    assert _is_market_open_at(_ist(2026, 10, 2)) is False        # Fri, Gandhi Jayanti


def test_normal_weekday_in_session_hours_is_open():
    assert _is_market_open_at(_ist(2026, 10, 1)) is True


def test_normal_weekday_after_close_and_weekend_closed():
    assert _is_market_open_at(_ist(2026, 10, 1, 16, 0)) is False
    assert _is_market_open_at(_ist(2026, 10, 3)) is False


def test_session_boundaries_unchanged_on_a_trading_day():
    assert _is_market_open_at(_ist(2026, 10, 1, 9, 15)) is True
    assert _is_market_open_at(_ist(2026, 10, 1, 15, 30)) is True
    assert _is_market_open_at(_ist(2026, 10, 1, 9, 14)) is False
