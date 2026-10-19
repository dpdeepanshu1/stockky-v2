"""2026-10-19: pullback re-arm entries (orders/pullback_rearm.py)."""
import pytest

from orders import pullback_rearm as pr


@pytest.fixture(autouse=True)
def _clean():
    pr.reset()
    yield
    pr.reset()


T0 = 1_000_000.0
NEAR_HIGH = "RANGE_GATE:NEAR_DAY_HIGH:range_pos=0.97 >= floor=0.92"


def _arm(sym="JUSTDIAL", ltp=780.0, pct=9.0, **kw):
    return pr.arm(sym, kw.pop("reason", NEAR_HIGH), ltp, pct, 5, now=T0, **kw)


def _path(prices_by_t, vwap=None, dh=None):
    """Returns scan_at(t): evaluates the armed setups at time t against the scripted price path."""
    box = {"t": T0}

    def live(sym):
        px = None
        for t, p in prices_by_t:
            if t <= box["t"]:
                px = p
        return px, dh, vwap

    def scan_at(t):
        box["t"] = t
        return pr.scan(now=t, live_fn=live)
    return scan_at


def test_only_extended_or_near_high_reasons_arm():
    assert _arm()
    assert not pr.arm("A", "QUALITY_GATE:technical_score 32 < floor 40", 100, 5, 5, now=T0)
    assert not pr.arm("B", "VWAP_BELOW: x", 100, 5, 5, now=T0)
    assert pr.arm("C", "OPENING_GATE:EXTENDED_ABOVE_OR:1.28% over OR high", 100, 5, 5, now=T0)
    assert pr.arm("D", "OPENING_GATE:NEAR_DAY_HIGH:range_pos=0.95", 100, 5, 5, now=T0)
    assert not pr.arm("E", "PRICE_GUARD:DAY_GAIN_TOO_HIGH:up 8.67%", 100, 8.7, 5, now=T0)


def test_weak_day_move_or_duplicate_not_armed():
    assert not pr.arm("X", NEAR_HIGH, 100, 0.5, 5, now=T0)
    assert _arm()
    assert not _arm()                                # already armed


def test_no_trigger_while_just_waiting_at_the_high():
    _arm()
    scan_at = _path([(T0, 780.0), (T0 + 60, 781.0)])
    assert scan_at(T0 + 60) == []


def test_pullback_bounce_hold_triggers():
    _arm(ltp=780.0)
    # 780 -> low 770 (-1.28%) at +120s -> bounce to 773 (+0.39%) at +180s, low held 60 s
    scan_at = _path([(T0, 780.0), (T0 + 120, 770.0), (T0 + 180, 773.0)])
    assert scan_at(T0 + 120) == []                                # at the low: no bounce yet
    hits = scan_at(T0 + 180)
    assert len(hits) == 1 and hits[0]["symbol"] == "JUSTDIAL"
    assert hits[0]["trough"] == 770.0 and hits[0]["peak"] == 780.0
    assert "JUSTDIAL" in str(pr.status()["armed"])                # stays armed until consume()
    pr.consume("JUSTDIAL")
    assert pr.status()["armed"] == []


def test_low_must_have_held():
    _arm(ltp=780.0)
    scan_at = _path([(T0, 780.0), (T0 + 100, 770.0), (T0 + 110, 773.0)])
    scan_at(T0 + 100)
    assert scan_at(T0 + 110) == []                                # low only 10 s old
    assert len(scan_at(T0 + 140)) == 1                            # 40 s old, still bouncing


def test_back_at_high_is_not_a_trigger():
    _arm(ltp=780.0)
    scan_at = _path([(T0, 780.0), (T0 + 60, 770.0), (T0 + 200, 779.5)])
    scan_at(T0 + 60)
    assert scan_at(T0 + 200) == []                                # 0.06% under the peak: chasing again


def test_below_vwap_waits():
    _arm(ltp=780.0)
    scan_at = _path([(T0, 780.0), (T0 + 60, 770.0), (T0 + 200, 773.0)], vwap=776.0)
    scan_at(T0 + 60)
    assert scan_at(T0 + 200) == []


def test_too_deep_pullback_is_dropped():
    _arm(ltp=780.0)
    scan_at = _path([(T0, 780.0), (T0 + 60, 745.0)])              # -4.5 %
    scan_at(T0 + 60)
    assert pr.status()["armed"] == []


def test_expires_after_max_age():
    _arm(ltp=780.0)
    scan_at = _path([(T0, 780.0)])
    scan_at(T0 + 91 * 60)
    assert pr.status()["armed"] == []


def test_new_high_restarts_the_leg():
    _arm(ltp=780.0)
    scan_at = _path([(T0, 780.0), (T0 + 60, 772.0), (T0 + 120, 790.0), (T0 + 200, 784.0)])
    scan_at(T0 + 60)
    scan_at(T0 + 120)                                              # new peak 790, trough resets
    assert scan_at(T0 + 200) == []                                # only -0.76% from the new peak


def test_consume_counts_attempts_and_caps_rearming():
    for _ in range(2):
        assert _arm()
        pr.consume("JUSTDIAL")
    assert not _arm()                                              # REARM_MAX_ATTEMPTS = 2 a day


def test_structure_stop_is_under_the_low_never_wider_and_never_inside_noise():
    # entry 773, low 770 -> 0.39% + 0.15% buffer = 0.54% < 0.7% floor -> 0.7%
    assert pr.structure_stop_pct(773.0, 770.0, 1.5) == pytest.approx(0.7)
    # deeper low 760: 1.83% structural, adaptive stop 1.2% is tighter -> keep 1.2%
    assert pr.structure_stop_pct(773.0, 760.0, 1.2) == pytest.approx(1.2)
    # adaptive 2.0%, structural 1.83% -> 1.83%
    assert pr.structure_stop_pct(773.0, 760.0, 2.0) == pytest.approx(1.83, abs=0.02)


def test_waived_opening_reasons_are_only_the_extension_ones():
    assert pr.is_waived_opening_reason("OPENING_GATE:EXTENDED_ABOVE_OR:1.2%")
    assert pr.is_waived_opening_reason("OPENING_GATE:NEAR_DAY_HIGH:range_pos=0.9")
    assert not pr.is_waived_opening_reason("OPENING_GATE:BELOW_OPEN:ltp 1 < open 2")
    assert not pr.is_waived_opening_reason("OPENING_GATE:GAP_UP:+6%")
    assert not pr.is_waived_opening_reason("OPENING_GATE:STOP_TOO_TIGHT:...")


def test_disabled_switch_arms_nothing(monkeypatch):
    import config
    monkeypatch.setattr(config, "PULLBACK_REARM_ENABLED", False, raising=False)
    assert not _arm()
    assert pr.scan(now=T0) == []
