"""2026-10-19: stale pre-gap signal price made every cycle read 'chasing' (JUSTDIAL 711 signal, 780 price)."""
from entry_engine import entry as e


def test_stale_signal_but_near_vwap_is_allowed():
    ok, _r, d, lim = e._entry_drift_ok(779.6, 711.35, 2.5, vwap=770.0)
    assert ok and d < lim


def test_extended_above_vwap_still_refused():
    ok, r, _d, _l = e._entry_drift_ok(800.0, 711.35, 2.5, vwap=760.0)     # +5.3% over VWAP, limit 1.9%
    assert not ok and "Chasing" in r


def test_no_vwap_or_vwap_below_price_logic_unchanged():
    assert not e._entry_drift_ok(779.6, 711.35, 2.5)[0]
    assert not e._entry_drift_ok(779.6, 711.35, 2.5, vwap=None)[0]
    assert not e._entry_drift_ok(779.6, 711.35, 2.5, vwap=790.0)[0]       # price under VWAP: no rescue
    assert not e._entry_drift_ok(779.6, 711.35, 2.5, vwap=600.0)[0]       # VWAP below signal / insane


def test_normal_drift_unchanged():
    assert e._entry_drift_ok(712.0, 711.35, 2.5, vwap=700.0)[0]
