"""E285 round 165: Dhan's own positions / order book are checked before a REAL BUY (JSW Energy ended with 4 shares). Stdlib + the service's modules."""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent / "scripts"))

from entry_engine import broker_dup_guard as G  # noqa: E402


def _pos(sym, net, sid="1"):
    return {"tradingSymbol": sym, "securityId": sid, "netQty": net}


def _ord(sym, side, status, sid="1", oid="9"):
    return {"tradingSymbol": sym, "securityId": sid, "transactionType": side, "orderStatus": status, "orderId": oid}


def test_net_long_position_blocks():
    assert "net long position of 2" in G.find_conflict([_pos("JSWENERGY", 2)], [], "JSWENERGY")


def test_flat_position_after_full_sell_does_not_block():
    assert G.find_conflict([_pos("JSWENERGY", 0)], [_ord("JSWENERGY", "BUY", "TRADED")], "JSWENERGY") is None


def test_short_position_is_not_a_long_conflict():
    assert G.find_conflict([_pos("JSWENERGY", -1)], [], "JSWENERGY") is None


def test_live_buy_order_blocks_each_live_status():
    for st in ("PENDING", "TRANSIT", "OPEN", "PART_TRADED"):
        assert "still live" in G.find_conflict([], [_ord("ITC", "BUY", st)], "ITC")


def test_dead_or_traded_orders_and_sells_do_not_block():
    for side, st in (("BUY", "REJECTED"), ("BUY", "CANCELLED"), ("BUY", "EXPIRED"), ("BUY", "TRADED"), ("SELL", "PENDING")):
        assert G.find_conflict([], [_ord("ITC", side, st)], "ITC") is None


def test_other_symbols_do_not_block():
    assert G.find_conflict([_pos("ITC", 14, "2")], [_ord("ITC", "BUY", "PENDING", "2")], "JSWENERGY", "1") is None


def test_matches_by_security_id_and_by_eq_suffix():
    assert G.find_conflict([_pos("JSWENERGY-EQ", 2, "99")], [], "JSWENERGY", "12345") is not None   # symbol with suffix
    assert G.find_conflict([_pos("OTHER", 2, "12345")], [], "JSWENERGY", "12345") is not None        # same security id


class _Fake:
    def __init__(self, positions=None, orders=None, boom=False):
        self._p, self._o, self._boom = positions or [], orders or [], boom

    def get_positions(self, db):
        if self._boom:
            raise RuntimeError("timeout")
        return self._p

    def get_order_list(self, db):
        return self._o


def test_check_blocks_with_fake_dhan(monkeypatch):
    monkeypatch.setattr(G, "_dhan", lambda: _Fake([_pos("JSWENERGY", 2)]))
    blocked, why = G.check(None, "JSWENERGY", "1")
    assert blocked and "net long" in why


def test_check_allows_clean_symbol(monkeypatch):
    monkeypatch.setattr(G, "_dhan", lambda: _Fake())
    assert G.check(None, "JSWENERGY", "1") == (False, "")


def test_unreadable_dhan_allows_by_default_and_blocks_when_fail_closed(monkeypatch):
    monkeypatch.setattr(G, "_dhan", lambda: _Fake(boom=True))
    monkeypatch.delenv("ENTRY_BROKER_DUP_CHECK_FAIL_CLOSED", raising=False)
    assert G.check(None, "ITC") == (False, "")
    monkeypatch.setenv("ENTRY_BROKER_DUP_CHECK_FAIL_CLOSED", "true")
    blocked, why = G.check(None, "ITC")
    assert blocked and "fail-closed" in why


def test_switch_off_skips_the_check(monkeypatch):
    monkeypatch.setenv("ENTRY_BROKER_DUP_CHECK", "false")
    assert G.check(None, "ITC") == (False, "")


def test_entry_wires_the_guard_and_keeps_the_existing_lock_on_a_block():
    src = (HERE / "entry_engine" / "entry.py").read_text()
    assert "broker_dup_guard.check(db, cand.symbol, security_id)" in src
    assert "_uncertain or _blocked_uncertain or _blocked_dup" in src   # a refused duplicate must NOT release the symbol lock of the real position
