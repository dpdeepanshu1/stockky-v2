"""Round 195: the 'same-day CNC row ... part sold today' line is not repeated on every holdings sync."""
from __future__ import annotations

import logging

import pytest

from portfolio import portfolio as pf

LOGGER = "real-trade-portfolio"


@pytest.fixture(autouse=True)
def _reset():
    pf._cnc_part_sold_logged.clear()
    yield
    pf._cnc_part_sold_logged.clear()


def _n(caplog, level):
    return [r for r in caplog.records if "part sold" in r.getMessage() and r.levelno == level]


def test_repeats_are_debug(caplog):
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        for _ in range(4):
            pf._log_cnc_part_sold("MRPL", 6, 3)
    assert len(_n(caplog, logging.INFO)) == 1 and len(_n(caplog, logging.DEBUG)) == 3


def test_new_quantities_log_info_again(caplog):
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        pf._log_cnc_part_sold("MRPL", 6, 3)
        pf._log_cnc_part_sold("MRPL", 6, 2)
        pf._log_cnc_part_sold("ONGC", 6, 3)
    assert len(_n(caplog, logging.INFO)) == 3


def test_info_again_after_interval(monkeypatch, caplog):
    t = {"v": 1000.0}
    monkeypatch.setattr(pf.time, "monotonic", lambda: t["v"])
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        pf._log_cnc_part_sold("MRPL", 6, 3)
        t["v"] += pf._CNC_PART_SOLD_LOG_EVERY_S + 1
        pf._log_cnc_part_sold("MRPL", 6, 3)
    assert len(_n(caplog, logging.INFO)) == 2
