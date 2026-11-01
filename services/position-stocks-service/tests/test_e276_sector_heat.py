"""E276 round 60: Position Stocks counts a same-sector entry 1.5x in the shared heat check (capital/heat_gate.py), ledger / peer stubbed.
Plain runner: python3 tests/test_e276_sector_heat.py (pytest also collects it)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from capital import portfolio_heat as ph
from feed import sector_map
from test_e276_heat_gate import _Stub


class _SectorStub(_Stub):
    def __init__(self, own_sector=None, peer_sector=None, **kw):
        super().__init__(**kw)
        self.own_sector, self.peer_sector = own_sector or {}, peer_sector or {}

    def __enter__(self):
        super().__enter__()
        self.ledger.own_sector_risk = lambda db: self.own_sector
        from capital import shared_heat
        shared_heat.get_peer_sectors = lambda db, max_age_s=900.0: self.peer_sector
        return self


def test_pure_maths():
    assert ph.sector_surcharge(0, 100) == 0.0
    assert abs(ph.sector_surcharge(100, 100, 1.5) - 100.0) < 1e-9
    assert ph.sector_key("Banks", "SHORT") == "Banks:SHORT" and ph.sector_key("Banks", "LONG") == "Banks:LONG"
    assert ph.check(100, 0, 0, 100, 10000, 0, 2.5, 100.0)[0] is False and ph.check(100, 0, 0, 100, 10000, 0, 2.5)[0] is True


def test_sector_map_has_banks_and_other():
    assert sector_map.sector_of("SBIN") == "Banks" and sector_map.sector_of("NOSUCHSTOCK") == "Other"


def test_second_bank_is_refused_where_another_sector_fits():
    # pool 7288, 5 % cap = 364.4. Open 250 (all Banks). New risk 50: plain total 300 fits; a second bank adds 0.5 x (250 + 50) = 150 -> 450 > 364.4.
    with _SectorStub(own=(250.0, 0.0, 7288.0), own_sector={"Banks:LONG": 250.0}) as st:
        assert st.gate.reject_reason(None, 100.0, 98.0, 25, "LONG", None, "TCS") is None
        r = st.gate.reject_reason(None, 100.0, 98.0, 25, "LONG", None, "SBIN")
        assert r and r.startswith("HEAT_LIMIT:") and "sector=Banks" in r, r


def test_peer_sector_counts_and_other_side_does_not():
    with _SectorStub(own=(0.0, 0.0, 7288.0), peer=(250.0, 7288.0, 0.0), peer_sector={"Banks:LONG": 250.0}) as st:   # pool 14576, cap 728.8
        assert st.gate.reject_reason(None, 100.0, 98.0, 25, "LONG", None, "TCS") is None
        assert st.gate.reject_reason(None, 100.0, 98.0, 25, "LONG", None, "SBIN") is None                              # 300 + 150 = 450 fits
        assert st.gate.reject_reason(None, 100.0, 98.0, 150, "LONG", None, "SBIN") is not None                         # 550 + 0.5 x 550 = 825 > 728.8


def test_short_is_not_stacked_on_a_long_in_the_same_sector():
    with _SectorStub(own=(250.0, 0.0, 7288.0), own_sector={"Banks:LONG": 250.0}) as st:
        assert st.gate.reject_reason(None, 100.0, 102.0, 25, "SHORT", None, "SBIN") is None


def test_off_unlisted_and_failure_are_open():
    with _SectorStub(own=(250.0, 0.0, 7288.0), own_sector={"Banks:LONG": 250.0}) as st:
        config.SECTOR_HEAT_ENABLED = False
        try:
            assert st.gate.reject_reason(None, 100.0, 98.0, 25, "LONG", None, "SBIN") is None
        finally:
            config.SECTOR_HEAT_ENABLED = True
        assert st.gate.reject_reason(None, 100.0, 98.0, 25, "LONG", None, "NOSUCHSTOCK") is None
        assert st.gate.reject_reason(None, 100.0, 98.0, 25, "LONG", None, None) is None
        st.ledger.own_sector_risk = lambda db: (_ for _ in ()).throw(RuntimeError("db down"))
        assert st.gate.reject_reason(None, 100.0, 98.0, 25, "LONG", None, "SBIN") is None


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v(); n += 1
    print(f"OK - {n} tests")
