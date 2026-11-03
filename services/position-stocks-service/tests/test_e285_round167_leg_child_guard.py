"""E285 round 167: (1) a Dhan-made leg order (tag VTT...) is cancelled before an exit is sent, so it cannot fill later and leave the opposite position
(JYOTICNC, 3 Nov); (2) alert-only mismatch check between Dhan's net INTRADAY position and the app's. Stdlib + the service's own modules, fake Dhan."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import config  # noqa: E402
from orders import leg_child_guard as L  # noqa: E402
from orders import residual_guard as R  # noqa: E402

OPENED = datetime(2026, 11, 3, 6, 35, 16)       # naive UTC = 12:05:16 IST


def _pos(**over):
    base = dict(id=399, symbol="JYOTICNC", dhan_security_id="21334", side="SHORT", quantity=1, status="OPEN", opened_at=OPENED, closed_at=None,
                dhan_super_order_id="323261103320675", plain_stop_order_id=None, overnight_converted_to_cnc=False, error_message=None)
    base.update(over)
    return SimpleNamespace(**base)


def _row(oid, side, status, tag, created, filled=0, qty=1, sid="21334"):
    return {"orderId": oid, "transactionType": side, "orderStatus": status, "correlationId": tag, "createTime": created, "quantity": qty, "filledQty": filled, "securityId": sid}


ENTRY = _row("323261103320675", "SELL", "TRADED", "SCALP", "2026-11-03 12:05:16", 1)
CHILD = "311261103482207"


class _Dhan:
    """Order book that changes when an order is cancelled. cancel_works False = the cancel call is accepted but the order stays live."""
    def __init__(self, rows, cancel_works=True, fill_on_cancel=0, book_error=False):
        self.rows = [dict(r) for r in rows]
        self.cancel_works, self.fill_on_cancel, self.book_error = cancel_works, fill_on_cancel, book_error
        self.cancelled, self.reads = [], 0

    def get_order_list(self, db):
        self.reads += 1
        if self.book_error:
            raise RuntimeError("book down")
        return [dict(r) for r in self.rows]

    def cancel_order(self, db, *, is_armed, dhan_order_id):
        self.cancelled.append(dhan_order_id)
        for r in self.rows:
            if r["orderId"] == dhan_order_id:
                if self.fill_on_cancel == 1:
                    r["orderStatus"], r["filledQty"] = "TRADED", 1
                elif self.fill_on_cancel == 2:
                    r["orderStatus"], r["filledQty"] = "PART_TRADED", 1
                elif self.cancel_works:
                    r["orderStatus"] = "CANCELLED"
        return {}


def _install(mp, dhan, alerts=None):
    mp.setattr(L, "_dhan", lambda: dhan)
    mp.setattr(L, "_alert", lambda text: (alerts if alerts is not None else []).append(text))


def _live_child(**over):
    return _row(CHILD, "BUY", "PENDING", "VTT_SQROFF", "2026-11-03 12:09:11", **over)


def test_jyoticnc_replay_the_resting_leg_order_is_cancelled_before_the_cover(monkeypatch):
    d = _Dhan([ENTRY, _live_child()])
    _install(monkeypatch, d)
    state, why = L.release_children(None, _pos(), sleep=lambda s: None)
    assert state == "cancelled" and d.cancelled == [CHILD] and CHILD in why


def test_a_leg_order_that_already_filled_the_position_means_no_exit(monkeypatch):
    d = _Dhan([ENTRY, _row(CHILD, "BUY", "TRADED", "VTT_SQROFF", "2026-11-03 12:09:11", filled=1)])
    _install(monkeypatch, d)
    state, why = L.release_children(None, _pos(), sleep=lambda s: None)
    assert state == "filled" and d.cancelled == []


def test_filled_while_being_cancelled_means_no_exit(monkeypatch):
    d = _Dhan([ENTRY, _live_child()], fill_on_cancel=1)
    _install(monkeypatch, d)
    assert L.release_children(None, _pos(), sleep=lambda s: None)[0] == "filled"


def test_part_filled_blocks_the_exit(monkeypatch):
    d = _Dhan([ENTRY, _live_child()], fill_on_cancel=2)
    _install(monkeypatch, d)
    state, why = L.release_children(None, _pos(quantity=2), sleep=lambda s: None)
    assert state == "error" and "in part" in why


def test_an_order_that_cannot_be_cancelled_lets_the_exit_go_and_alerts(monkeypatch):
    d = _Dhan([ENTRY, _live_child()], cancel_works=False)
    alerts = []
    _install(monkeypatch, d, alerts)
    state, why = L.release_children(None, _pos(), sleep=lambda s: None)
    assert state == "live" and CHILD in why and len(alerts) == 1 and "STILL LIVE" in alerts[0]


def test_only_dhan_made_exit_side_orders_of_this_position_count(monkeypatch):
    rows = [
        ENTRY,
        _row("1", "BUY", "PENDING", "EOD_SQUAREOFF", "2026-11-03 12:09:11"),     # the app's own exit: left to the existing retry logic
        _row("2", "SELL", "PENDING", "VTT_SQROFF", "2026-11-03 12:09:11"),        # wrong side for a short
        _row("3", "BUY", "PENDING", "VTT_SQROFF", "2026-11-03 12:09:11", sid="999"),   # other security
        _row("4", "BUY", "PENDING", "VTT_SQROFF", "2026-11-03 11:00:00"),         # before this position opened
        _row("5", "BUY", "CANCELLED", "VTT_SQROFF", "2026-11-03 12:09:11"),       # already dead
    ]
    d = _Dhan(rows)
    _install(monkeypatch, d)
    assert L.release_children(None, _pos(), sleep=lambda s: None) == ("none", "")
    assert d.cancelled == []


def test_a_long_looks_for_sell_side_orders(monkeypatch):
    d = _Dhan([_row("7", "SELL", "PENDING", "VTT_SQROFF", "2026-11-03 12:09:11")])
    _install(monkeypatch, d)
    assert L.release_children(None, _pos(side="LONG"), sleep=lambda s: None)[0] == "cancelled" and d.cancelled == ["7"]


def test_positions_without_a_plain_super_order_are_skipped_without_a_dhan_call(monkeypatch):
    d = _Dhan([_live_child()])
    _install(monkeypatch, d)
    for over in ({"dhan_super_order_id": None}, {"plain_stop_order_id": "55"}, {"overnight_converted_to_cnc": True}):
        assert L.release_children(None, _pos(**over), sleep=lambda s: None) == ("none", "")
    assert d.reads == 0


def test_unreadable_book_and_switch_off_fail_open(monkeypatch):
    d = _Dhan([_live_child()], book_error=True)
    _install(monkeypatch, d)
    assert L.release_children(None, _pos(), sleep=lambda s: None) == ("none", "")
    d2 = _Dhan([_live_child()])
    _install(monkeypatch, d2)
    monkeypatch.setattr(config, "LEG_CHILD_GUARD", False)
    assert L.release_children(None, _pos(), sleep=lambda s: None) == ("none", "") and d2.reads == 0


def test_exit_path_calls_the_guard_before_the_exit_order():
    src = (HERE / "orders" / "eod_squareoff.py").read_text()
    assert "leg_child_guard.release_children(db, pos)" in src
    assert src.index("leg_child_guard.release_children(db, pos)") < src.index("exit_side = _side.exit_transaction_type(pos)   # SELL closes a long")


# ------------------------------------------------------------------ residual guard
NOW = datetime(2026, 11, 3, 8, 0, 0, tzinfo=timezone.utc)


def _setup(mp, positions, dhan_rows, sent):
    R.reset_state()
    mp.setattr(R, "_todays_positions", lambda db, now: positions)
    mp.setattr(R, "_dhan", lambda: SimpleNamespace(get_positions=lambda db: dhan_rows))
    mp.setattr(R, "_notify", lambda text: sent.append(text))
    mp.setattr(config, "RESIDUAL_GUARD_ENABLED", True)
    mp.setattr(config, "RESIDUAL_GUARD_INTERVAL_S", 60.0)
    mp.setattr(config, "RESIDUAL_GUARD_CONFIRM_S", 90.0)


def _dhan_pos(net, product="INTRADAY", sid="21334"):
    return {"securityId": sid, "tradingSymbol": "JYOTICNC", "productType": product, "netQty": net, "buyAvg": 933.35, "sellAvg": 941.65}


def test_jyoticnc_mismatch_alerts_once_after_it_has_lasted(monkeypatch):
    sent = []
    closed = _pos(status="STOP_BREACH_EXIT", closed_at=NOW.replace(tzinfo=None) - timedelta(hours=2))
    _setup(monkeypatch, [closed], [_dhan_pos(1)], sent)
    assert R.run_residual_guard(None, NOW, mono=1000.0) == 0          # first sight: wait
    assert R.run_residual_guard(None, NOW, mono=1030.0) == 0          # inside the read interval
    assert R.run_residual_guard(None, NOW, mono=1100.0) == 1          # 100 s old: alert
    assert R.run_residual_guard(None, NOW, mono=1200.0) == 0          # not repeated
    assert len(sent) == 1 and "JYOTICNC" in sent[0] and "+1" in sent[0] and "no open position" in sent[0]


def test_matching_flat_pending_and_other_products_do_not_alert(monkeypatch):
    sent = []
    open_short = _pos(status="OPEN")
    _setup(monkeypatch, [open_short], [_dhan_pos(-1)], sent)                      # app short 1, Dhan short 1
    R.run_residual_guard(None, NOW, mono=1000.0); R.run_residual_guard(None, NOW, mono=1100.0)
    _setup(monkeypatch, [_pos(status="STOP_BREACH_EXIT", closed_at=NOW.replace(tzinfo=None) - timedelta(hours=1))], [_dhan_pos(0)], sent)   # Dhan flat
    R.run_residual_guard(None, NOW, mono=1000.0); R.run_residual_guard(None, NOW, mono=1100.0)
    _setup(monkeypatch, [_pos(status="STOP_BREACH_EXIT", closed_at=NOW.replace(tzinfo=None) - timedelta(hours=1))], [_dhan_pos(1, product="CNC")], sent)   # delivery holding
    R.run_residual_guard(None, NOW, mono=1000.0); R.run_residual_guard(None, NOW, mono=1100.0)
    _setup(monkeypatch, [_pos(status="STOP_BREACH_EXIT", closed_at=NOW.replace(tzinfo=None) - timedelta(hours=1))], [_dhan_pos(1, sid="555")], sent)   # not traded here
    R.run_residual_guard(None, NOW, mono=1000.0); R.run_residual_guard(None, NOW, mono=1100.0)
    assert sent == []


def test_an_exit_that_is_still_settling_is_not_alerted(monkeypatch):
    sent = []
    just_closed = _pos(status="STOP_BREACH_EXIT", closed_at=NOW.replace(tzinfo=None) - timedelta(seconds=30))
    _setup(monkeypatch, [just_closed], [_dhan_pos(1)], sent)
    R.run_residual_guard(None, NOW, mono=1000.0); R.run_residual_guard(None, NOW, mono=1100.0)
    pending = _pos(status="STOP_BREACH_EXIT", error_message="STOP_BREACH_EXIT_PENDING_RECONCILE", closed_at=None)
    _setup(monkeypatch, [pending], [_dhan_pos(1)], sent)
    R.run_residual_guard(None, NOW, mono=1000.0); R.run_residual_guard(None, NOW, mono=1100.0)
    assert sent == []


def test_expected_net_signs_and_unreadable_dhan(monkeypatch):
    assert R.expected_net([_pos(status="OPEN", side="SHORT", quantity=3), _pos(status="OPEN", side="LONG", quantity=1), _pos(status="TARGET_HIT")], "21334") == -2
    sent = []
    _setup(monkeypatch, [_pos(status="STOP_BREACH_EXIT", closed_at=NOW.replace(tzinfo=None) - timedelta(hours=1))], [], sent)
    monkeypatch.setattr(R, "_dhan", lambda: SimpleNamespace(get_positions=lambda db: (_ for _ in ()).throw(RuntimeError("down"))))
    assert R.run_residual_guard(None, NOW, mono=1000.0) == 0 and sent == []
    monkeypatch.setattr(config, "RESIDUAL_GUARD_ENABLED", False)
    assert R.run_residual_guard(None, NOW, mono=5000.0) == 0


def test_main_loop_runs_the_residual_guard():
    src = (HERE / "main.py").read_text()
    assert "residual_guard.run_residual_guard" in src
