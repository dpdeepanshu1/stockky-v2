"""Drift guard: api-gateway's KNOWN_DELISTED and market-data-service's KNOWN_DELISTED_SYMBOLS must name
the same symbols.

market-data-service already short-circuited AAKASH and ANNAPURNA (confirmed dead via Yahoo 404 logs,
2026-09-01) while the gateway's list held only TATAMTRDVR, so the gateway kept resolving them to
`<SYM>.NS` and sending them to yfinance. The two lists are separate definitions (the services do not
import each other), so this test reads both as source rather than importing market-data's main.py
(which would pull in its whole dependency set).
"""
import ast
import os

import symbol_aliases as sa

SERVICES = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
MARKET_DATA_MAIN = os.path.join(SERVICES, "market-data-service", "main.py")


def _market_data_delisted():
    with open(MARKET_DATA_MAIN, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "KNOWN_DELISTED_SYMBOLS" for t in node.targets
        ):
            return {e.value for e in node.value.elts if isinstance(e, ast.Constant)}
    raise AssertionError("KNOWN_DELISTED_SYMBOLS not found in market-data-service/main.py")


def test_the_guard_actually_found_the_market_data_list():
    found = _market_data_delisted()
    # TATAMTRDVR, AAKASH, ANNAPURNA at the time of writing; an empty/short set means the parse broke.
    assert {"TATAMTRDVR", "AAKASH", "ANNAPURNA"} <= found


def test_gateway_and_market_data_delisted_lists_match():
    md = _market_data_delisted()
    gw = set(sa.KNOWN_DELISTED)
    assert gw == md, (
        f"only in gateway: {sorted(gw - md)}; only in market-data-service: {sorted(md - gw)} "
        "— add the missing symbols to the other list (and keep a reason string in the gateway dict)"
    )
