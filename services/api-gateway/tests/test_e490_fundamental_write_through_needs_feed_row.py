"""E490: `_refresh_fundamental_upstream` created a fundamentals-only feed row for a symbol that had none.

E436 stopped `_fetch_events_cached` from creating an events-only row (no price, no fundamentals; it entered the feed index and the Health Audit as
incomplete) and listed the fundamentals write-through as "not changed, not read". It had the same gap: with no stored row it still called
`put_symbol` with a payload built from the fundamentals reply alone. It now writes through only when a row already exists. The answer is still
returned and still kept in the short Redis copy (`STATIC_PARAM_TTL`), so a symbol with no row is served from Redis until the feed job or Repair
gives it a price.

No fastapi needed (function lifted from main.py by AST, run against fakes). Runs with pytest or `python3 tests/test_e490_...py`."""
from __future__ import annotations

import ast
import asyncio
import logging
import os
import types
from typing import Optional

_SERVICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TREE = ast.parse(open(os.path.join(_SERVICE, "main.py"), encoding="utf-8").read())


class _CircuitOpenError(Exception):
    pass


class _Resp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data

    def json(self):
        return self._data


def _refresh(rows, resp, get_raises=None, put_raises=None):
    fn = [n for n in _TREE.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_refresh_fundamental_upstream"]
    assert len(fn) == 1
    puts, sets, logs = [], [], []

    class Feed:
        def get_symbol(self, symbol):
            if get_raises is not None:
                raise get_raises
            return rows.get(symbol)

        def put_symbol(self, symbol, payload, ttl=None):
            if put_raises is not None:
                raise put_raises
            puts.append((symbol, payload, ttl))
            rows[symbol] = payload

    async def cb_get(client, name, url, timeout=5.0, **kw):
        return resp

    class _Log:
        def debug(self, msg, *a):
            logs.append(("debug", msg % a if a else msg))

        def warning(self, msg, *a):
            logs.append(("warning", msg % a if a else msg))

    ns = dict(
        asyncio=asyncio, Optional=Optional, httpx=types.SimpleNamespace(AsyncClient=object), logger=_Log(),
        _cb_get=cb_get, _redis_set=lambda k, v, ttl=None: sets.append((k, v, ttl)), _feed_store=lambda: Feed(),
        extract_feed_payload=lambda s, fundamental=None, events=None: {"symbol": s, "sector": (fundamental or {}).get("sector"), "updated_at": "T"},
        FUNDAMENTAL_URL="http://fund", STATIC_PARAM_TTL=111, DATA_FEED_TTL=222,
        CircuitOpenError=_CircuitOpenError, _log_circuit_skip=lambda *a: None,
    )
    exec(compile(ast.Module(body=fn, type_ignores=[]), "main.py", "exec"), ns)
    out = asyncio.run(ns["_refresh_fundamental_upstream"]("TCS", None, "KEY"))
    return out, puts, sets, logs, rows


_REPLY = {"fundamental_score": 66, "metrics": {"pe": 1}, "fallback_used": False, "sector": "IT"}


def test_no_feed_row_means_no_fundamentals_only_row_is_created():
    (data, fb), puts, sets, _logs, rows = _refresh({}, _Resp(200, dict(_REPLY)))
    assert puts == [] and "TCS" not in rows
    assert fb is False and data["fundamental_score"] == 66 and data["from_data_feed"] is False      # the answer is unchanged
    assert len(sets) == 1 and sets[0][0] == "KEY" and sets[0][2] == 111 and sets[0][1]["full"]["sector"] == "IT"   # still cached in Redis


def test_an_existing_row_is_still_written_and_keeps_its_event_fields():
    rows = {"TCS": {"price": 100, "bulk_deals": [1], "event_summary": "old", "earnings_surprise": None}}
    (data, _fb), puts, _sets, _logs, _rows = _refresh(rows, _Resp(200, dict(_REPLY)))
    assert len(puts) == 1 and puts[0][0] == "TCS" and puts[0][2] == 222
    payload = puts[0][1]
    assert payload["sector"] == "IT" and payload["bulk_deals"] == [1] and payload["event_summary"] == "old"
    assert "earnings_surprise" not in payload                                                       # an existing None is not copied
    assert data["fundamental_score"] == 66


def test_a_feed_read_error_is_swallowed_and_the_answer_is_still_cached():
    (data, _fb), puts, sets, logs, _rows = _refresh({}, _Resp(200, dict(_REPLY)), get_raises=RuntimeError("read down"))
    assert data["fundamental_score"] == 66 and puts == [] and len(sets) == 1
    assert any(lvl == "debug" and "data feed write-through fund" in m for lvl, m in logs)


def test_a_feed_write_error_for_an_existing_row_is_swallowed():
    (data, _fb), puts, sets, logs, _rows = _refresh({"TCS": {"price": 1}}, _Resp(200, dict(_REPLY)), put_raises=RuntimeError("write down"))
    assert data["fundamental_score"] == 66 and puts == [] and len(sets) == 1
    assert any(lvl == "debug" and "data feed write-through fund" in m for lvl, m in logs)


def test_a_non_200_reply_touches_neither_the_cache_nor_the_feed():
    (data, fb), puts, sets, _logs, _rows = _refresh({"TCS": {"price": 1}}, _Resp(503, {}))
    assert (data, fb) == ({}, True) and puts == [] and sets == []


if __name__ == "__main__":
    _failed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print("ok  ", _name)
            except Exception as _e:  # noqa: BLE001
                _failed += 1
                print("FAIL", _name, repr(_e))
    raise SystemExit(1 if _failed else 0)
