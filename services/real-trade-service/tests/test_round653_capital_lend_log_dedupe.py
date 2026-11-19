"""Round 653: the "equity_sync: capital lend" line is INFO on the first sync, a changed share, lent cash moved 5% or more, or after 5 minutes; otherwise DEBUG.
Only the log level changes; sizing is untouched. sqlalchemy / httpx are stubbed when missing (this sandbox); the VM image has them."""
from __future__ import annotations

import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _n in ("httpx", "sqlalchemy", "sqlalchemy.orm", "sqlalchemy.exc", "sqlalchemy.ext", "sqlalchemy.ext.declarative", "sqlalchemy.sql", "sqlalchemy.pool"):
    try:
        __import__(_n)
    except Exception:  # noqa: BLE001
        sys.modules[_n] = mock.MagicMock(name=_n)

import pytest  # noqa: E402

from execution import equity_sync as es  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh():
    es._lend_logged.clear()


def test_first_line_is_new_repeat_is_not():
    assert es._lend_line_is_new(50.0, 10000.0, now=1000.0) is True
    assert es._lend_line_is_new(50.0, 10000.0, now=1010.0) is False
    assert es._lend_line_is_new(50.0, 10200.0, now=1020.0) is False        # +2% is under 5%


def test_lent_cash_moving_5pct_is_new_and_becomes_the_reference():
    es._lend_line_is_new(50.0, 10000.0, now=1000.0)
    assert es._lend_line_is_new(50.0, 10600.0, now=1010.0) is True
    assert es._lend_line_is_new(50.0, 10700.0, now=1020.0) is False        # measured from 10600 now


def test_changed_share_is_new():
    es._lend_line_is_new(50.0, 10000.0, now=1000.0)
    assert es._lend_line_is_new(40.0, 10000.0, now=1001.0) is True


def test_new_again_after_the_window():
    es._lend_line_is_new(50.0, 10000.0, now=1000.0)
    assert es._lend_line_is_new(50.0, 10000.0, now=1000.0 + es._LEND_LOG_REPEAT_S + 1) is True


def test_zero_lent_does_not_divide_by_zero():
    es._lend_line_is_new(50.0, 0.0, now=1000.0)
    assert es._lend_line_is_new(50.0, 0.0, now=1001.0) is False
    assert es._lend_line_is_new(50.0, 5.0, now=1002.0) is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
