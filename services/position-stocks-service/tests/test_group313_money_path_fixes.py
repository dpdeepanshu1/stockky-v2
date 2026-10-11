"""group313: ambiguous BUY-placement recovery (execution/dhan_client.py + orders/entry.py) and board-runner step isolation."""
from datetime import datetime, timedelta, timezone

import pytest

import config
from execution import dhan_client
from feed import bar_backfill, bar_store
from orders import entry
from screening import board, board_log, board_runner

IST = timezone(timedelta(hours=5, minutes=30))


class _ReadTimeout(Exception):
    pass


_ReadTimeout.__name__ = "ReadTimeout"


class _ConnectTimeout(_ReadTimeout):
    pass


_ConnectTimeout.__name__ = "ConnectTimeout"


def test_ambiguous_error_classification():
    f = dhan_client.is_ambiguous_placement_error
    assert f(_ReadTimeout("x")) is True
    assert f(RuntimeError("HTTPSConnectionPool: Read timed out. (read timeout=10)")) is True
    assert f(RuntimeError("502 Bad Gateway")) is True
    assert f(RuntimeError("Connection reset by peer")) is True
    assert f(RuntimeError("HTTP 503")) is True
    assert f(RuntimeError("RMS:221260915045037:Rate Not Within Ckt Limit 395.25 To 592.85")) is False   # 504 inside an id is not HTTP 504
    assert f(_ConnectTimeout("never sent")) is False                      # request never left: safe to call it failed
    assert f(RuntimeError("Connection refused")) is False
    assert f(RuntimeError("RMS:123:Rate Not Within Ckt Limit 395 To 592")) is False
    assert f(RuntimeError("Insufficient Funds")) is False
    assert f(ValueError("order_type=LIMIT requires a positive price")) is False


def _row(oid="111", side="BUY", sec="1333", qty=10, status="TRADED", age_s=20, now=None):
    now = now or datetime(2026, 10, 12, 10, 0, 0, tzinfo=IST)
    t = (now - timedelta(seconds=age_s)).strftime("%Y-%m-%d %H:%M:%S")
    return {"orderId": oid, "transactionType": side, "securityId": sec, "quantity": qty, "orderStatus": status, "createTime": t}


NOW = datetime(2026, 10, 12, 10, 0, 0, tzinfo=IST)


def test_match_recent_buy_order_rules():
    m = lambda rows, **k: dhan_client.match_recent_buy_order(rows, "1333", 10, now=NOW, **k)  # noqa: E731
    assert m([_row()])["orderId"] == "111"
    assert m([_row(side="SELL")]) is None and m([_row(sec="9")]) is None and m([_row(qty=11)]) is None
    assert m([_row(status="REJECTED")]) is None and m([_row(status="CANCELLED")]) is None
    assert m([_row(age_s=900)]) is None                                    # an old order of the same size is not this one
    assert m([_row(oid="111")], exclude_ids={"111"}) is None               # already tracked as an earlier position
    assert m([_row(oid="a", age_s=100), _row(oid="b", age_s=5)])["orderId"] == "b"   # newest wins
    no_time = _row(); no_time.pop("createTime")
    assert m([no_time]) is None                                            # no timestamp -> cannot prove it is young -> no match
    assert m([None, "x", {}]) is None and m([]) is None and m(None) is None


class _Q:
    def __init__(self, rows): self.rows = rows
    def filter(self, *a, **k): return self
    def all(self): return self.rows


class _Db:
    def __init__(self, rows=()): self.rows = list(rows)
    def query(self, *a): return _Q(self.rows)


def test_recover_returns_ids_only_for_ambiguous_errors(monkeypatch):
    calls = []
    monkeypatch.setattr(dhan_client, "find_recent_buy_order",
                        lambda db, sec, qty, super_order, exclude_ids: calls.append((sec, qty, super_order, set(exclude_ids))) or {"orderId": "777", "orderStatus": "PENDING"})
    monkeypatch.setattr(config, "USE_SUPER_ORDER", True)
    assert entry._recover_ambiguous_buy(_Db([("S1", "E1")]), _ReadTimeout("t"), "1333", 10, "HDFCBANK") == ("777", None)
    assert calls == [("1333", 10, True, {"S1", "E1"})]
    assert entry._recover_ambiguous_buy(_Db(), RuntimeError("Insufficient Funds"), "1333", 10, "HDFCBANK") is None
    assert len(calls) == 1                                                  # a clean rejection never even asks Dhan
    monkeypatch.setattr(config, "USE_SUPER_ORDER", False)
    assert entry._recover_ambiguous_buy(_Db(), _ReadTimeout("t"), "1333", 10, "HDFCBANK") == (None, "777")


def test_recover_fails_open(monkeypatch):
    monkeypatch.setattr(dhan_client, "find_recent_buy_order", lambda *a, **k: None)
    assert entry._recover_ambiguous_buy(_Db(), _ReadTimeout("t"), "1333", 10, "X") is None   # nothing at Dhan -> still "failed"
    def boom(*a, **k): raise RuntimeError("dhan down")
    monkeypatch.setattr(dhan_client, "find_recent_buy_order", boom)
    assert entry._recover_ambiguous_buy(_Db(), _ReadTimeout("t"), "1333", 10, "X") is None   # lookup error -> old behaviour, no raise
    class BadDb:
        def query(self, *a): raise RuntimeError("db gone")
    assert entry._recover_ambiguous_buy(BadDb(), _ReadTimeout("t"), "1333", 10, "X") is None


def test_board_runner_backfill_failure_no_longer_blocks_snapshot_and_fill(monkeypatch):
    board_runner.reset()
    monkeypatch.setattr(config, "BOARD_BACKFILL_ENABLED", True)
    monkeypatch.setattr(bar_backfill, "pending_count", lambda: 3)
    monkeypatch.setattr(board, "build", lambda **k: {"bullish": [1], "bearish": []})
    snaps, fills = [], []
    monkeypatch.setattr(board_log, "record_snapshot", lambda p, now: snaps.append(now) or 1)
    monkeypatch.setattr(bar_store, "prune", lambda: 0)
    monkeypatch.setattr(board_log, "fill_forward", lambda s, day, now: fills.append(day) or 2)

    class _Sess:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def bad_backfill():
        raise RuntimeError("market-data down")

    now = 1_800_000_000.0
    out = board_runner.run_once(now, True, open_symbols_fn=lambda: [], session_factory=lambda: _Sess(), today_fn=lambda: "2026-10-12", backfill_fn=bad_backfill)
    assert out["snapshot"] is True and snaps == [now] and out["fill"] == 2 and fills == ["2026-10-12"]
    st = board_runner.state()
    assert st["errors"] == 1 and st["last_error"].startswith("backfill:") and "market-data down" in st["last_error"]
    board_runner.reset()
