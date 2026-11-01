"""E276 round 51: Position Stocks refuses an entry that would push shared open risk past HEAT_LIMIT_PCT (capital/heat_gate.py), ledger / peer stubbed.
Plain runner: python3 tests/test_e276_heat_gate.py (pytest also collects it)."""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

_KEYS = ("sqlalchemy", "sqlalchemy.orm", "models", "capital.ledger", "capital.shared_heat", "capital.heat_gate")


class _Stub:
    """Installs the stubs only while a test runs (never at import / collection time) and puts the real modules back afterwards, so other tests collected by
    the same pytest run keep the real capital.ledger. The sandbox has no sqlalchemy; the gate itself never touches it."""

    def __init__(self, own=(0.0, 0.0, 7288.0), peer=(0.0, 0.0, 0.0)):
        self.own, self.peer = own, peer

    def __enter__(self):
        self._orig = {k: sys.modules.get(k) for k in _KEYS}
        try:
            import sqlalchemy.orm  # noqa: F401
            import models  # noqa: F401
        except Exception:  # noqa: BLE001
            sa = types.ModuleType("sqlalchemy"); orm = types.ModuleType("sqlalchemy.orm"); orm.Session = object; sa.orm = orm
            m = types.ModuleType("models"); m.SharedHeat = object; m.SharedHeatSectors = object
            sys.modules.update({"sqlalchemy": sa, "sqlalchemy.orm": orm, "models": m})
        import capital as _cap
        # E283: `from capital import ledger` reads the PACKAGE ATTRIBUTE first, and with the real sqlalchemy installed an earlier test has already set it to the
        # real module, so the stub in sys.modules was never seen (the gate then called the real ledger with db=None and failed open). Swap the attributes too.
        self._attrs = {k: _cap.__dict__.get(k) for k in ("ledger", "shared_heat", "heat_gate")}
        self.ledger = types.ModuleType("capital.ledger")
        self.ledger.own_heat = lambda db: self.own
        sys.modules["capital.ledger"] = self.ledger
        _cap.ledger = self.ledger
        for k in ("capital.shared_heat", "capital.heat_gate"):
            sys.modules.pop(k, None)
        _cap.__dict__.pop("shared_heat", None)
        _cap.__dict__.pop("heat_gate", None)
        from capital import heat_gate, shared_heat
        shared_heat.get_peer = lambda db, max_age_s=900.0: self.peer
        self.gate = heat_gate
        return self

    def __exit__(self, *a):
        import capital as _cap
        for k, v in self._attrs.items():
            if v is None:
                _cap.__dict__.pop(k, None)
            else:
                setattr(_cap, k, v)
        for k, v in self._orig.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _r(st, *args):
    return st.gate.reject_reason(None, *args)


def test_allowed_under_limit_and_refused_over():
    with _Stub(own=(100.0, 0.0, 7288.0), peer=(100.0, 7288.0, 0.0)) as _st:                 # pool 14576, 5 % = 728.8, open 200
        assert _r(_st, 100.0, 98.0, 50) is None              # new risk 100 -> 300
        r = _r(_st, 100.0, 90.0, 60)                         # new risk 600 -> 800 > 728.8
        assert r and r.startswith("HEAT_LIMIT:"), r


def test_gap_allowance_and_peer_count():
    with _Stub(own=(100.0, 150.0, 7288.0), peer=(300.0, 7288.0, 0.0)) as _st:               # 550 open incl. gap, cap 728.8
        assert _r(_st, 100.0, 98.0, 80) is None               # +160 = 710
        assert _r(_st, 100.0, 98.0, 100) is not None          # +200 = 750 > 728.8


def test_no_peer_uses_own_pool_alone():
    with _Stub(own=(300.0, 0.0, 7288.0), peer=(0.0, 0.0, 0.0)) as _st:                      # cap 364.4
        assert _r(_st, 100.0, 98.0, 30) is None               # 360
        assert _r(_st, 100.0, 98.0, 40) is not None           # 380


def test_switch_off_zero_limit_and_failure_are_open():
    with _Stub(own=(900.0, 0.0, 7288.0)) as _st:
        config.HEAT_GATE_ENABLED = False
        try:
            assert _r(_st, 100.0, 90.0, 100) is None
        finally:
            config.HEAT_GATE_ENABLED = True
        config.HEAT_LIMIT_PCT = 0.0
        try:
            assert _r(_st, 100.0, 90.0, 100) is None
        finally:
            config.HEAT_LIMIT_PCT = 5.0
    with _Stub() as _st:
        _st.ledger.own_heat = lambda db: (_ for _ in ()).throw(RuntimeError("db down"))
        assert _r(_st, 100.0, 90.0, 100) is None


def test_stop_at_or_above_entry_risks_nothing():
    with _Stub(own=(360.0, 0.0, 7288.0)) as _st:
        assert _r(_st, 100.0, 100.0, 100) is None


def test_short_side_risk_is_stop_above_entry():
    with _Stub(own=(300.0, 0.0, 7288.0)) as _st:                                     # cap 364.4
        assert _r(_st, 100.0, 102.0, 30, "SHORT") is None                           # risk 60 -> 360
        assert _r(_st, 100.0, 102.0, 40, "SHORT") is not None                       # risk 80 -> 380
        assert _r(_st, 100.0, 98.0, 1000, "SHORT") is None                          # stop below entry on a short risks nothing


if __name__ == "__main__":
    n = 0
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f(); n += 1
    print(f"{n} passed")
