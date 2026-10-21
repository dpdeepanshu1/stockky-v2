"""group479: opt-in resting broker stop for a same-day INTRADAY (MIS) position (BROKER_STOP_INTRADAY, ships OFF)."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import config
from execution import broker_stop as B


def pos(**kw):
    base = dict(status="OPEN", qty_open=5, current_stop=95.0, entry_product_type="INTRADAY", broker_imported=False,
                opened_at=datetime.now(timezone.utc))
    base.update(kw)
    return SimpleNamespace(**base)


def test_default_is_off_and_intraday_is_still_skipped():
    assert config.BROKER_STOP_INTRADAY is False
    assert B.skip_reason(pos()) == "intraday"


def test_flag_on_makes_a_same_day_intraday_position_eligible(monkeypatch):
    monkeypatch.setattr(config, "BROKER_STOP_INTRADAY", True, raising=False)
    assert B.skip_reason(pos()) is None
    assert B.skip_reason(pos(entry_product_type="MIS")) is None


def test_flag_on_keeps_the_other_skip_rules(monkeypatch):
    monkeypatch.setattr(config, "BROKER_STOP_INTRADAY", True, raising=False)
    assert B.skip_reason(pos(status="CLOSED")) == "status"
    assert B.skip_reason(pos(qty_open=0)) == "no_qty"
    assert B.skip_reason(pos(current_stop=0)) == "no_stop"


def test_same_day_cnc_still_skipped_with_flag_on(monkeypatch):
    monkeypatch.setattr(config, "BROKER_STOP_INTRADAY", True, raising=False)
    assert B.skip_reason(pos(entry_product_type="CNC")) == "same_day_cdsl"


def test_stop_order_uses_the_positions_product(monkeypatch):
    seen = {}

    def fake_place(db, **kw):
        seen.update(kw)
        return SimpleNamespace(ok=True)

    monkeypatch.setattr(B.dhan_client, "place_order", fake_place)
    B._send_stop(None, "123", pos(), 94.0, "STOP_LOSS_MARKET")
    assert seen["product_type"] == "INTRADAY" and seen["transaction_type"] == "SELL"
    B._send_stop(None, "123", pos(entry_product_type="CNC"), 94.0, "STOP_LOSS_MARKET")
    assert seen["product_type"] == "CNC"
