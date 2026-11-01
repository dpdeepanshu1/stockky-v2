"""E285 round 96: the new /live/state parts (IP guard, modify cap, volume curve) and the volume-curve warm-up.
Stand-ins for SQLAlchemy / httpx are installed when the real ones are missing (a bare sandbox); in Docker the real ones are used.
Plain runner: python3 tests/test_e285_round96_live_state.py (pytest also collects it)."""
from __future__ import annotations

import os
import sys
import time
import types

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)


def _stub(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m
    return m


try:
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
from orders import ip_guard, live_state, volume_profile


def test_modify_stats_counts_orders_the_reserve_and_refusals():
    dhan_client._MODIFY_COUNT.clear()
    dhan_client._MODIFY_STATS["refused"] = 0
    dhan_client._MODIFY_COUNT.update({"A": 5, "B": 22, "C": 25})
    s = dhan_client.modify_stats()
    assert s["cap"] == 25 and s["reserve"] == 3 and s["orders_counted"] == 3 and s["max_modifies_on_one_order"] == 25
    assert s["orders_trail_stopped"] == 2 and s["refused_at_cap"] == 0 and s["enabled"] is True      # B (3 left) and C (0 left) are inside the reserve
    dhan_client._MODIFY_COUNT.clear()
    assert dhan_client.modify_stats()["max_modifies_on_one_order"] == 0


def test_a_refused_modify_is_counted():
    dhan_client._MODIFY_COUNT.clear()
    dhan_client._MODIFY_STATS["refused"] = 0
    dhan_client._MODIFY_COUNT["X"] = 25
    try:
        dhan_client.modify_super_order(None, order_id="X", order_leg="STOP_LOSS_LEG", stop_loss_price=100.0)
        raise AssertionError("expected DhanModifyCapReached")
    except dhan_client.DhanModifyCapReached:
        pass
    assert dhan_client.modify_stats()["refused_at_cap"] == 1
    dhan_client._MODIFY_COUNT.clear()


def test_live_state_parts_are_the_real_counters():
    ip_guard.reset()
    ip_guard.on_rejection("DH-905 Invalid IP", now=1000.0)
    g = live_state.ip_guard_part()
    assert g["hits"] == 1 and g["enabled"] is True and "Invalid IP" in g["last_message"]
    ip_guard.reset()
    assert live_state.modify_cap_part()["cap"] == 25
    assert set(live_state.volume_part()) >= {"ready", "building", "days", "symbol_days"}
    out = live_state.snapshot(object())                    # the db-based parts fail alone; the three new ones still answer
    assert out["ip_guard"]["ok"] is True and out["modify_cap"]["ok"] is True and out["volume_profile"]["ok"] is True


def test_volume_snapshot_says_building_and_warm_starts_one_build(tmp_path, monkeypatch):
    volume_profile.reset()
    monkeypatch.setattr(config, "BAR_RECORD_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(config, "VOLUME_PROFILE_LEARNED_ENABLED", True, raising=False)
    calls = []
    gate = {"go": False}

    def slow_build(root):
        calls.append(root)
        t0 = time.time()
        while not gate["go"] and time.time() - t0 < 5:
            time.sleep(0.01)
        return {"curve": None, "days": 3, "symbol_days": 120, "files": {}}

    monkeypatch.setattr(volume_profile, "build", slow_build)
    volume_profile.warm()
    volume_profile.warm()                                   # a second call while building must not start another build
    s = volume_profile.snapshot()
    assert s["building"] is True and s["ready"] is False and s["days"] is None and s["built_age_s"] is None
    gate["go"] = True
    for _ in range(300):
        if not volume_profile.snapshot()["building"]:
            break
        time.sleep(0.01)
    s = volume_profile.snapshot()
    assert s["building"] is False and s["days"] == 3 and s["symbol_days"] == 120 and s["built_age_s"] is not None
    assert calls == [str(tmp_path)]
    volume_profile.reset()


def test_warm_does_nothing_when_the_switch_is_off(monkeypatch):
    volume_profile.reset()
    monkeypatch.setattr(config, "VOLUME_PROFILE_LEARNED_ENABLED", False, raising=False)
    monkeypatch.setattr(volume_profile, "build", lambda root: (_ for _ in ()).throw(AssertionError("must not build")))
    volume_profile.warm()
    assert volume_profile.snapshot()["building"] is False
    volume_profile.reset()


def test_main_warms_the_volume_profile_at_startup():
    src = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
    assert "_vp.warm()" in src and src.index("_vp.warm()") < src.index("await ws_client.start()")


if __name__ == "__main__":
    import inspect
    import tempfile
    import pathlib
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                kw = {}
                params = inspect.signature(fn).parameters
                mp = None
                if "tmp_path" in params:
                    kw["tmp_path"] = pathlib.Path(tempfile.mkdtemp())
                if "monkeypatch" in params:
                    class _MP:
                        def __init__(self): self.undo = []
                        def setattr(self, obj, k, v, raising=True):
                            self.undo.append((obj, k, getattr(obj, k, None), hasattr(obj, k))); setattr(obj, k, v)
                    mp = _MP(); kw["monkeypatch"] = mp
                fn(**kw)
                if mp:
                    for obj, k, old, had in reversed(mp.undo):
                        setattr(obj, k, old) if had else delattr(obj, k)
                print("PASS", name)
            except Exception as e:  # noqa: BLE001
                failed += 1
                print("FAIL", name, type(e).__name__, e)
    sys.exit(1 if failed else 0)
