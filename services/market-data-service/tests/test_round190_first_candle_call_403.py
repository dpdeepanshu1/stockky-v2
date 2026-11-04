"""Round 190 (issue #5): a candle 403 on the FIRST candle call this process ever sent says so in the log, because then the limit was used up by
something outside the process (another login on the same credentials, or the previous container). Diagnostics only: no limit or cooldown changes.

Run from services/market-data-service:  python3 -m pytest tests/test_round190_first_candle_call_403.py -q
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import angelone_budget as b
import rate_limiter as rl


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("ANGELONE_BUDGET", "ANGELONE_SPLIT_COOLDOWN", "ANGELONE_CANDLE_SLOWDOWN_S", "ANGELONE_CANDLE_SLOWDOWN_FACTOR"):
        monkeypatch.delenv(k, raising=False)
    rl._buckets.pop("angelone_candle", None)
    b._reset()
    yield
    rl._buckets.pop("angelone_candle", None)
    b._reset()


def _ctx(caplog):
    return [r.getMessage() for r in caplog.records if "candle 403 context" in r.getMessage()]


def test_first_call_of_the_process_is_flagged(caplog):
    b.note_candle_sent()
    with caplog.at_level(logging.WARNING, logger="angelone-budget"):
        assert b.trip("getCandleData") > 0
    lines = _ctx(caplog)
    assert len(lines) == 1
    assert "FIRST candle call since this process started" in lines[0]
    assert "1 since start" in lines[0] and "no earlier candle cooldown" in lines[0]


def test_later_calls_are_not_flagged(caplog):
    b.note_candle_sent(); b.note_candle_sent()
    with caplog.at_level(logging.WARNING, logger="angelone-budget"):
        b.trip("getCandleData")
    assert "FIRST candle call" not in _ctx(caplog)[0]


def test_quote_trips_get_no_candle_context(caplog):
    with caplog.at_level(logging.WARNING, logger="angelone-budget"):
        b.trip("quote")
    assert _ctx(caplog) == []
