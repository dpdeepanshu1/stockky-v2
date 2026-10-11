"""group320: the decision service's cache TTL treats a price-proven closed day as closed (fail-open).

Same ast-extraction approach as test_market_open_holiday.py (no fastapi/httpx needed).
Run from services/decision-prediction-service/decision:  python3 -m pytest tests/test_session_proof_closed.py -q
"""
from __future__ import annotations

import ast
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_DIR, "main.py")
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)
import session_proof_client as spc  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


def _is_market_open_at(when):
    tree = ast.parse(open(_SRC, encoding="utf-8").read())
    keep = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name == "_is_market_open")
            or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "_NSE_HOLIDAYS_2026" for t in n.targets))]
    assert len(keep) == 2

    class _FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return when.astimezone(tz) if tz else when

    ns = {"datetime": _FakeDT, "IST": IST}
    exec(compile(ast.Module(body=keep, type_ignores=[]), _SRC, "exec"), ns)
    return ns["_is_market_open"]()


@pytest.fixture(autouse=True)
def _clean():
    spc.reset_state()
    yield
    spc.reset_state()


def test_open_without_verdict():
    assert _is_market_open_at(datetime(2026, 10, 12, 11, 0, tzinfo=IST)) is True


def test_closed_verdict_closes_the_day():
    spc.set_verdict("2026-10-12", True)
    assert _is_market_open_at(datetime(2026, 10, 12, 11, 0, tzinfo=IST)) is False


def test_verdict_for_another_day_is_ignored():
    spc.set_verdict("2026-10-09", True)
    assert _is_market_open_at(datetime(2026, 10, 12, 11, 0, tzinfo=IST)) is True
