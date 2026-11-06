"""tests/test_round223_watchdog_leg_log.py - round 223: the stop watchdog logs each super-order leg's state before it cancels them.

Run from services/position-stocks-service:  python3 -m pytest tests/test_round223_watchdog_leg_log.py -q
"""
from __future__ import annotations

import logging
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from execution import dhan_client
from orders import stop_watchdog

ROW = {
    "orderId": "777", "orderStatus": "PENDING",
    "legDetails": [
        {"legName": "ENTRY_LEG", "orderStatus": "TRADED", "price": 74.9, "averageTradedPrice": 74.9},
        {"legName": "TARGET_LEG", "orderStatus": "CANCELLED", "price": 76.0},
        {"legName": "STOP_LOSS_LEG", "orderStatus": "TRIGGERED", "price": 74.05, "triggerPrice": 74.05},
    ],
}


def _pos():
    return SimpleNamespace(id=5, symbol="EXCELSOFT", stop_price=74.05, dhan_super_order_id="777")


class TestDescribeLegs:
    def test_names_every_leg_with_status_and_price(self):
        out = stop_watchdog.describe_legs(ROW)
        assert "order status=PENDING" in out
        assert "STOP_LOSS_LEG: orderStatus=TRIGGERED price=74.05 triggerPrice=74.05" in out
        assert "TARGET_LEG: orderStatus=CANCELLED" in out

    def test_missing_or_odd_rows_never_raise(self):
        assert "no super-order row" in stop_watchdog.describe_legs(None)
        assert stop_watchdog.describe_legs({"legDetails": ["x", None]})   # junk legs skipped


class TestLogLegState:
    def test_logs_the_matching_row(self, monkeypatch, caplog):
        monkeypatch.setattr(dhan_client, "get_super_order_list", lambda db, release_db=False: [{"orderId": "1"}, ROW])
        with caplog.at_level(logging.WARNING, logger="position-stocks-stop-watchdog"):
            stop_watchdog.log_leg_state(None, _pos())
        assert "leg state before cancel" in caplog.text and "STOP_LOSS_LEG" in caplog.text and "EXCELSOFT" in caplog.text

    def test_no_matching_row_is_logged_not_raised(self, monkeypatch, caplog):
        monkeypatch.setattr(dhan_client, "get_super_order_list", lambda db, release_db=False: [{"orderId": "1"}])
        with caplog.at_level(logging.WARNING, logger="position-stocks-stop-watchdog"):
            stop_watchdog.log_leg_state(None, _pos())
        assert "no super-order row found" in caplog.text

    def test_a_dhan_error_never_blocks_the_exit(self, monkeypatch):
        def boom(db, release_db=False):
            raise RuntimeError("dhan down")
        monkeypatch.setattr(dhan_client, "get_super_order_list", boom)
        stop_watchdog.log_leg_state(None, _pos())      # must not raise

    def test_switch_off_makes_no_call(self, monkeypatch):
        calls = []
        monkeypatch.setattr(config, "STOP_WATCHDOG_LEG_LOG", False)
        monkeypatch.setattr(dhan_client, "get_super_order_list", lambda db, release_db=False: calls.append(1) or [])
        stop_watchdog.log_leg_state(None, _pos())
        assert calls == []
