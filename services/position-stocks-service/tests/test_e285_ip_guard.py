"""E285 round 95: Dhan's static-IP rule (orders/ip_guard.py, execution/dhan_client.py, GET /dhan/ip-status) and the 25-modifies-per-order counter.
Where SQLAlchemy / httpx are missing (a bare sandbox) tiny stand-ins are installed so the real dhan_client code still runs; in the Docker image the real ones are used.
Plain runner: python3 tests/test_e285_ip_guard.py (pytest also collects it)."""
from __future__ import annotations

import os
import sys
import types
from datetime import date
from types import SimpleNamespace

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)


def _stub(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m
    return m


try:                                              # the real packages when they exist (Docker), small stand-ins otherwise
    import sqlalchemy.orm  # noqa: F401
except Exception:  # noqa: BLE001
    _stub("sqlalchemy")
    _stub("sqlalchemy.orm", Session=object)
try:
    import httpx  # noqa: F401
except Exception:  # noqa: BLE001
    _stub("httpx", get=lambda *a, **k: None)
try:
    from auth import dhan_credentials_ro  # noqa: F401
except Exception:  # noqa: BLE001
    _stub("auth.dhan_credentials_ro", get_decrypted_credentials=lambda db: ("C1", "T1"))

import config
from execution import dhan_client
from orders import entry_pause, ip_guard


def _reset():
    ip_guard.reset()
    entry_pause._all_until, entry_pause._all_reason = 0.0, ""
    dhan_client._MODIFY_COUNT.clear()
    config.IP_GUARD_ENABLED = True
    config.DHAN_MODIFY_CAP_ENABLED = True
    config.DHAN_MODIFY_CAP_PER_ORDER = 25
    config.DHAN_MODIFY_RESERVE = 3
    config.ENTRY_IP_PAUSE_MINUTES = 15
    config.IP_ALERT_EVERY_MIN = 30


class _Notifier:
    def __init__(self):
        self.sent = []

    def notify_critical(self, text):
        self.sent.append(text)


def _with_notifier():
    n = _Notifier()
    saved = sys.modules.get("notifier")
    sys.modules["notifier"] = n
    return n, saved


def _restore_notifier(saved):
    if saved is not None:
        sys.modules["notifier"] = saved
    else:
        sys.modules.pop("notifier", None)


# ---------------------------------------------------------------- recognising the refusal
def test_ip_rejection_text():
    for t in ("Dhan API error: DH-905 Invalid IP", "invalid ip address", "IP not whitelisted for this client", "Static IP not registered",
              "Dhan API error: Unauthorized IP"):
        assert ip_guard.is_ip_rejection(t) and dhan_client.is_invalid_ip_error(t), t
    for t in ("Dhan API error: DH-905 Input_Exception: price is not a tick multiple", "Insufficient funds", "Rate Not Within Ckt Limit", "", None,
              "Invalid access token", "dh-905"):
        assert not ip_guard.is_ip_rejection(t), t               # DH-905 alone is NOT an IP marker (Dhan uses it for other input errors)


# ---------------------------------------------------------------- the pause and the alert
def test_rejection_pauses_new_entries_and_alerts_once_per_window():
    _reset()
    n, saved = _with_notifier()
    try:
        assert entry_pause.all_paused() is None
        assert ip_guard.on_rejection("Dhan API error: DH-905 Invalid IP", now=1000.0) is True
        why = entry_pause.all_paused()
        assert why and "INVALID_IP" in why
        assert len(n.sent) == 1 and "IP not registered" in n.sent[0] and "/dhan/ip-status" in n.sent[0]
        ip_guard.on_rejection("Invalid IP again", now=1000.0 + 60)            # inside the window: counted, not alerted again
        assert len(n.sent) == 1 and ip_guard.status()["hits"] == 2
        ip_guard.on_rejection("Invalid IP later", now=1000.0 + 30 * 60 + 1)   # a new window
        assert len(n.sent) == 2
        st = ip_guard.status()
        assert st["hits"] == 3 and "Invalid IP later" in st["last_message"]
    finally:
        _restore_notifier(saved)
        _reset()


def test_other_failures_and_the_switch_do_nothing():
    _reset()
    n, saved = _with_notifier()
    try:
        assert ip_guard.on_rejection("Insufficient funds") is False and entry_pause.all_paused() is None and n.sent == []
        config.IP_GUARD_ENABLED = False
        assert ip_guard.on_rejection("DH-905 Invalid IP") is False and entry_pause.all_paused() is None and n.sent == []
        assert ip_guard.on_rejection(object()) is False                       # never raises on garbage
    finally:
        _restore_notifier(saved)
        _reset()


def test_every_dhan_failure_passes_the_hook():
    _reset()
    n, saved = _with_notifier()
    try:
        try:
            dhan_client._extract_data({"status": "failure", "remarks": {"error_code": "DH-905", "error_type": "Invalid_IP", "error_message": "Invalid IP"}})
            assert False, "must raise"
        except RuntimeError as e:
            assert "Invalid IP" in str(e)
        assert entry_pause.all_paused() and len(n.sent) == 1
        _reset()
        n.sent.clear()
        try:
            dhan_client._extract_data({"status": "failure", "remarks": "Insufficient funds"})
        except RuntimeError:
            pass
        assert entry_pause.all_paused() is None and n.sent == []
        assert dhan_client._extract_data({"status": "success", "data": {"orderId": "9"}}) == {"orderId": "9"}
    finally:
        _restore_notifier(saved)
        _reset()


# ---------------------------------------------------------------- what Dhan says about the IP
def test_summary_verdicts():
    d = date(2026, 11, 1)
    ok = ip_guard.summarize_ip_status({"primaryIP": "1.2.3.4", "secondaryIP": "", "detectedIP": "1.2.3.4", "ipMatchStatus": "PRIMARY_MATCH", "ordersAllowed": True,
                                       "modifyDatePrimary": "2026-11-20", "modifyDateSecondary": "2026-10-01"}, "1.2.3.4", d)
    assert ok["verdict"] == "OK" and ok["ok"] is True and ok["primary_change_allowed"] is False and ok["secondary_change_allowed"] is True and ok["secondary_ip"] is None
    assert ip_guard.summarize_ip_status({"ipMatchStatus": "SECONDARY_MATCH"}, None, d)["verdict"] == "OK"
    mm = ip_guard.summarize_ip_status({"primaryIP": "1.1.1.1", "secondaryIP": "2.2.2.2", "detectedIP": "9.9.9.9", "ipMatchStatus": "MISMATCH", "modifyDatePrimary": "2026-11-05"}, "9.9.9.9", d)
    assert mm["verdict"] == "MISMATCH" and not mm["ok"] and "9.9.9.9" in mm["advice"] and "2026-11-05" in mm["advice"] and mm["primary_change_allowed"] is False
    mm2 = ip_guard.summarize_ip_status({"primaryIP": "1.1.1.1", "detectedIP": "9.9.9.9", "ipMatchStatus": "MISMATCH", "modifyDatePrimary": "2026-10-30"}, None, d)
    assert "change is allowed now" in mm2["advice"]
    assert ip_guard.summarize_ip_status({"ipMatchStatus": "NOT_CONFIGURED"}, None, d)["verdict"] == "NOT_CONFIGURED"
    na = ip_guard.summarize_ip_status({"ipMatchStatus": "PRIMARY_MATCH", "ordersAllowed": False}, None, d)
    assert na["verdict"] == "ORDERS_NOT_ALLOWED" and not na["ok"]
    diff = ip_guard.summarize_ip_status({"ipMatchStatus": "PRIMARY_MATCH", "detectedIP": "5.5.5.5"}, "6.6.6.6", d)
    assert "go by Dhan's" in diff["advice"] and diff["verdict"] == "OK"


def test_summary_unknown_shapes_are_never_ok():
    for bad in (None, [], "x", {}, {"ipMatchStatus": "SOMETHING_NEW"}, {"ipMatchStatus": None}, {"modifyDatePrimary": "not a date", "ipMatchStatus": 7}):
        r = ip_guard.summarize_ip_status(bad, "1.1.1.1", date(2026, 11, 1))
        assert r["verdict"] == "UNKNOWN" and r["ok"] is False, bad


def test_get_ip_status_reads_dhan_and_fails_soft():
    _reset()
    calls = {}

    class Resp:
        def __init__(self, data, bad=False):
            self.data, self.bad = data, bad
        def raise_for_status(self):
            if self.bad:
                raise RuntimeError("HTTP 401")
        def json(self):
            return self.data

    def fake_get(url, headers=None, timeout=None, **kw):
        calls.setdefault("urls", []).append(url)
        if "ipify" in url:
            return Resp({"ip": "7.7.7.7"})
        calls["headers"] = headers
        return Resp({"detectedIP": "7.7.7.7", "primaryIP": "7.7.7.7", "ipMatchStatus": "PRIMARY_MATCH", "ordersAllowed": True})

    real_httpx, real_creds = dhan_client.httpx, dhan_client.dhan_credentials_ro
    try:
        dhan_client.httpx = SimpleNamespace(get=fake_get)
        dhan_client.dhan_credentials_ro = SimpleNamespace(get_decrypted_credentials=lambda db: ("CID", "TOKEN"))
        db = SimpleNamespace(new=[], dirty=[], deleted=[], rollback=lambda: None)
        out = dhan_client.get_ip_status(db)
        assert out["verdict"] == "OK" and out["outbound_ip"] == "7.7.7.7"
        assert any(u.endswith("/v2/ip/getIP") for u in calls["urls"]) and calls["headers"]["access-token"] == "TOKEN" and calls["headers"]["client-id"] == "CID"
        # Dhan answers 401 (token expired): UNKNOWN with the reason, not an exception
        dhan_client.httpx = SimpleNamespace(get=lambda url, **k: Resp({"ip": "7.7.7.7"}) if "ipify" in url else Resp({}, bad=True))
        bad = dhan_client.get_ip_status(db)
        assert bad["verdict"] == "UNKNOWN" and "HTTP 401" in bad["error"] and "7.7.7.7" in bad["advice"]
        # no credentials stored
        dhan_client.dhan_credentials_ro = SimpleNamespace(get_decrypted_credentials=lambda db: None)
        try:
            dhan_client.get_ip_status(db)
            assert False, "must raise"
        except dhan_client.DhanNotConnectedError:
            pass
    finally:
        dhan_client.httpx, dhan_client.dhan_credentials_ro = real_httpx, real_creds


# ---------------------------------------------------------------- 25 modifies per order
class _FakeSdk:
    def __init__(self, fail=False):
        self.n, self.fail = 0, fail
    def modify_super_order(self, **kw):
        self.n += 1
        if self.fail:
            return {"status": "failure", "remarks": {"error_message": "refused"}}
        return {"status": "success", "data": {"orderId": kw["order_id"]}}


def _patched_modify(sdk):
    saved = (dhan_client._get_sdk_client, dhan_client.order_rate)
    dhan_client._get_sdk_client = lambda db: sdk
    dhan_client.order_rate = SimpleNamespace(acquire=lambda *a, **k: None)
    return saved


def _unpatch(saved):
    dhan_client._get_sdk_client, dhan_client.order_rate = saved


def test_modify_counter_counts_only_accepted_and_refuses_past_the_cap():
    _reset()
    sdk = _FakeSdk()
    saved = _patched_modify(sdk)
    try:
        for i in range(25):
            dhan_client.modify_super_order(None, order_id="A1", order_leg="STOP_LOSS_LEG", stop_loss_price=100.0 + i)
        assert dhan_client.modify_count("A1") == 25 and dhan_client.modify_room("A1") == 0 and sdk.n == 25
        try:
            dhan_client.modify_super_order(None, order_id="A1", order_leg="STOP_LOSS_LEG", stop_loss_price=200.0)
            assert False, "26th must be refused"
        except dhan_client.DhanModifyCapReached:
            pass
        assert sdk.n == 25                                                     # Dhan was NOT called a 26th time (no budget spent)
        dhan_client.modify_super_order(None, order_id="B2", order_leg="TARGET_LEG", target_price=120.0)      # another order has its own count
        assert dhan_client.modify_count("B2") == 1
        bad = _FakeSdk(fail=True)
        dhan_client._get_sdk_client = lambda db: bad
        try:
            dhan_client.modify_super_order(None, order_id="C3", order_leg="STOP_LOSS_LEG", stop_loss_price=99.0)
        except RuntimeError:
            pass
        assert dhan_client.modify_count("C3") == 0                             # a refused modify does not count
    finally:
        _unpatch(saved)
        _reset()


def test_trail_stops_short_of_the_cap_and_the_switch_ends_it():
    _reset()
    sdk = _FakeSdk()
    saved = _patched_modify(sdk)
    try:
        assert dhan_client.trail_has_room("X") is True
        for i in range(22):
            dhan_client.modify_super_order(None, order_id="X", order_leg="STOP_LOSS_LEG", stop_loss_price=100.0 + i)
        assert dhan_client.modify_room("X") == 3 and dhan_client.trail_has_room("X") is False        # 25 - 22 = 3 left = the reserve: the trail stops
        dhan_client.modify_super_order(None, order_id="X", order_leg="STOP_LOSS_LEG", stop_loss_price=150.0)   # a protective move still goes through
        config.DHAN_MODIFY_CAP_ENABLED = False
        assert dhan_client.trail_has_room("X") is True and dhan_client.modify_room("X") > 1000
        dhan_client.modify_super_order(None, order_id="X", order_leg="STOP_LOSS_LEG", stop_loss_price=151.0)   # off = no refusal
    finally:
        _unpatch(saved)
        _reset()


def test_counter_dict_stays_small():
    _reset()
    sdk = _FakeSdk()
    saved = _patched_modify(sdk)
    try:
        for i in range(2105):
            dhan_client.modify_super_order(None, order_id=f"O{i}", order_leg="TARGET_LEG", target_price=120.0)
        assert len(dhan_client._MODIFY_COUNT) <= 1200
    finally:
        _unpatch(saved)
        _reset()


# ---------------------------------------------------------------- wiring
def _src(*parts):
    with open(os.path.join(HERE, *parts), encoding="utf-8") as f:
        return f.read()


def test_wiring_route_trail_and_switches():
    m = _src("main.py")
    assert '@app.get("/dhan/ip-status")' in m and "dhan_client.get_ip_status(db)" in m and "Depends(require_admin)" in m.split('@app.get("/dhan/ip-status")')[1][:300]
    t = _src("orders", "trailing.py")
    assert t.index("trail_has_room") < t.index("dhan_client.modify_super_order(")            # checked before the modify
    d = _src("execution", "dhan_client.py")
    assert d.index("ip_guard.on_rejection") < d.index('raise RuntimeError(f"Dhan API error: {msg}")')
    from control import catalog
    keys = {f.key for f in catalog.CATALOG}
    assert {"IP_GUARD_ENABLED", "DHAN_MODIFY_CAP_ENABLED"} <= keys
    assert config.IP_GUARD_ENABLED is True and config.ENTRY_IP_PAUSE_MINUTES == 15 and config.DHAN_MODIFY_CAP_PER_ORDER == 25 and config.DHAN_MODIFY_RESERVE == 3


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v(); n += 1; print("ok", k)
    print(f"{n} passed")
