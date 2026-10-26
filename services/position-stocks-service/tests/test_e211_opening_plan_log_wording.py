"""E211: the "[opening-plan] built" line said "0 of 150 symbols have a previous-day volatility" when the previous-day cache was still cold, which read as
"no plan, no gating". The uncached symbols are still served (background fetch + per-lookup cache read), so the line now says so."""
import logging

import pytest

from screening import opening_plan
from tests.test_group391_opening_plan import MON, clean, pd_of, seed  # noqa: F401

_LOGGER = "position-stocks-opening-plan"


def _built_line(caplog):
    return next(r.getMessage() for r in caplog.records if "[opening-plan] built" in r.getMessage())


def test_cold_cache_says_the_rest_are_fetched_not_missing(caplog):
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        opening_plan.build(["AAA", "BBB"], now=MON(9, 10))
    line = _built_line(caplog)
    assert "0 of 2 symbols planned" in line and "2 not cached yet" in line and "background" in line
    assert "have a previous-day volatility" not in line


def test_fully_cached_has_no_missing_clause(caplog):
    seed("AAA", pd_of(4.0))
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        opening_plan.build(["AAA"], now=MON(9, 10))
    line = _built_line(caplog)
    assert "1 of 1 symbols planned" in line and "not cached yet" not in line


def test_partial_cache_counts_the_rest(caplog):
    seed("AAA", pd_of(4.0))
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        opening_plan.build(["AAA", "BBB", "CCC"], now=MON(9, 10))
    assert "1 of 3 symbols planned" in _built_line(caplog) and "2 not cached yet" in _built_line(caplog)


def test_uncached_symbol_is_still_served_once_the_cache_fills():
    opening_plan.build(["CCC"], now=MON(9, 10))
    seed("CCC", pd_of(2.0))
    assert opening_plan.bar_atr_pct("CCC", now=MON(9, 16)) == pytest.approx(0.5)
