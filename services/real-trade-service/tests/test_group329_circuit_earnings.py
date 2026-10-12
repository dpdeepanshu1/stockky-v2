"""tests/test_group329_circuit_earnings.py - entry_engine/circuit_gate.py and entry_engine/event_risk.py (group 329).

Circuit band: no BUY next to the upper circuit / close to the lower circuit; unknown or nonsense limits never block.
Results day: no BUY on a stock reporting today; no overnight carry into results today / next trading day; every failure fails open.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from entry_engine import circuit_gate, event_risk
from market_feed.feed import Tick, _safe_circuit


@pytest.fixture(autouse=True)
def defaults(monkeypatch):
    monkeypatch.setattr(config, "CIRCUIT_GATE_ENABLED", True)
    monkeypatch.setattr(config, "CIRCUIT_UPPER_BUFFER_PCT", 1.0)
    monkeypatch.setattr(config, "CIRCUIT_LOWER_BUFFER_PCT", 2.0)
    monkeypatch.setattr(config, "EARNINGS_DAY_BLOCK", True)
    monkeypatch.setattr(config, "EARNINGS_CARRY_BLOCK", True)
    event_risk.reset()
    yield
    event_risk.reset()


class TestCircuitGate:
    def test_price_in_the_middle_of_the_band_passes(self):
        assert circuit_gate.reject_reason(100.0, 110.0, 90.0) is None

    def test_at_the_upper_circuit_is_rejected(self):
        r = circuit_gate.reject_reason(110.0, 110.0, 90.0)
        assert r and r.startswith("CIRCUIT_UPPER")

    def test_inside_the_upper_buffer_is_rejected_and_just_outside_passes(self):
        assert circuit_gate.reject_reason(108.95, 110.0, 90.0) and circuit_gate.reject_reason(108.95, 110.0, 90.0).startswith("CIRCUIT_UPPER")
        assert circuit_gate.reject_reason(108.8, 110.0, 90.0) is None        # 110 * 0.99 = 108.9

    def test_close_to_the_lower_circuit_is_rejected(self):
        r = circuit_gate.reject_reason(91.5, 110.0, 90.0)                    # 90 * 1.02 = 91.8
        assert r and r.startswith("CIRCUIT_LOWER")
        assert circuit_gate.reject_reason(92.0, 110.0, 90.0) is None

    @pytest.mark.parametrize("price,upper,lower", [
        (100.0, None, None), (100.0, 0, 0), (100.0, "x", "y"), (100.0, float("nan"), float("nan")),
        (None, 110.0, 90.0), (0, 110.0, 90.0), (-5, 110.0, 90.0),
    ])
    def test_unknown_or_unusable_numbers_never_block(self, price, upper, lower):
        assert circuit_gate.reject_reason(price, upper, lower) is None

    def test_upper_not_above_lower_is_nonsense_and_allowed(self):
        assert circuit_gate.reject_reason(100.0, 90.0, 110.0) is None

    def test_price_far_outside_the_band_means_stale_limits_and_allows(self):
        assert circuit_gate.reject_reason(130.0, 110.0, 90.0) is None
        assert circuit_gate.reject_reason(70.0, 110.0, 90.0) is None

    def test_only_one_limit_known_still_checks_that_side(self):
        assert circuit_gate.reject_reason(110.0, 110.0, None).startswith("CIRCUIT_UPPER")
        assert circuit_gate.reject_reason(90.5, None, 90.0).startswith("CIRCUIT_LOWER")

    def test_a_zero_buffer_turns_that_side_off(self, monkeypatch):
        monkeypatch.setattr(config, "CIRCUIT_UPPER_BUFFER_PCT", 0.0)
        assert circuit_gate.reject_reason(110.0, 110.0, 90.0) is None
        assert circuit_gate.reject_reason(90.5, 110.0, 90.0) is not None

    def test_disabled_gate_allows_everything(self, monkeypatch):
        monkeypatch.setattr(config, "CIRCUIT_GATE_ENABLED", False)
        assert circuit_gate.reject_reason(110.0, 110.0, 90.0) is None

    def test_tick_carries_the_limits_and_the_tick_helper_reads_them(self):
        t = Tick("ABC", 110.0, datetime.now(timezone.utc), None, "t", upper_circuit=110.0, lower_circuit=90.0)
        assert t.upper_circuit == 110.0 and t.lower_circuit == 90.0
        assert circuit_gate.reject_reason_tick(t).startswith("CIRCUIT_UPPER")
        bare = Tick("ABC", 110.0, datetime.now(timezone.utc), None, "t")
        assert bare.upper_circuit is None and circuit_gate.reject_reason_tick(bare) is None
        assert circuit_gate.reject_reason_tick(object()) is None

    def test_safe_circuit_parsing(self):
        assert _safe_circuit("110.5") == 110.5
        for bad in (None, "", 0, -1, "abc", float("nan"), float("inf")):
            assert _safe_circuit(bad) is None


class TestEarningsParsing:
    def test_reads_upcoming_and_recent(self):
        up = {"upcoming": [{"type": "earnings_date", "date": "2026-10-20"}], "recent": []}
        rec = {"upcoming": [], "recent": [{"type": "dividend", "date": "2026-10-12"}, {"type": "earnings_date", "date": "2026-10-12"}]}
        assert event_risk.parse_earnings_date(up) == date(2026, 10, 20)
        assert event_risk.parse_earnings_date(rec) == date(2026, 10, 12)

    @pytest.mark.parametrize("body", [None, {}, [], "x", {"upcoming": None, "recent": None},
                                      {"upcoming": [{"type": "earnings_date", "date": "garbage!!!"}]},
                                      {"upcoming": [{"type": "earnings_date"}]}])
    def test_junk_is_none_not_an_exception(self, body):
        assert event_risk.parse_earnings_date(body) is None


class TestEarningsDecision:
    TODAY = date(2026, 10, 12)       # a Monday, trading day

    def test_results_today_blocks_entry(self):
        r = event_risk.reason_for_date(self.TODAY, today=self.TODAY)
        assert r and r.startswith("EARNINGS_TODAY")

    def test_results_tomorrow_does_not_block_entry_but_blocks_carry(self):
        tomorrow = date(2026, 10, 13)
        assert event_risk.reason_for_date(tomorrow, today=self.TODAY) is None
        assert event_risk.reason_for_date(tomorrow, carry=True, today=self.TODAY).startswith("EARNINGS_NEXT_SESSION")

    def test_far_or_past_dates_do_not_block(self):
        assert event_risk.reason_for_date(date(2026, 10, 30), carry=True, today=self.TODAY) is None
        assert event_risk.reason_for_date(date(2026, 10, 1), carry=True, today=self.TODAY) is None
        assert event_risk.reason_for_date(None, carry=True, today=self.TODAY) is None

    def test_friday_carry_looks_at_monday(self):
        fri = date(2026, 10, 9)
        assert event_risk.next_trading_day(fri) == date(2026, 10, 12)
        assert event_risk.reason_for_date(date(2026, 10, 12), carry=True, today=fri).startswith("EARNINGS_NEXT_SESSION")
        # weekend results hit Monday's open, so a Friday carry is blocked for them too; Tuesday's results are not
        assert event_risk.reason_for_date(date(2026, 10, 10), carry=True, today=fri).startswith("EARNINGS_NEXT_SESSION")
        assert event_risk.reason_for_date(date(2026, 10, 13), carry=True, today=fri) is None

    def test_next_trading_day_skips_an_nse_holiday(self):
        assert event_risk.next_trading_day(date(2026, 11, 9)) == date(2026, 11, 11)               # 2026-11-10 is a holiday (tz_utils)

    def test_switches(self, monkeypatch):
        monkeypatch.setattr(config, "EARNINGS_DAY_BLOCK", False)
        assert event_risk.reason_for_date(self.TODAY, today=self.TODAY) is None
        monkeypatch.setattr(config, "EARNINGS_CARRY_BLOCK", False)
        assert event_risk.reason_for_date(date(2026, 10, 13), carry=True, today=self.TODAY) is None


class TestLookup:
    def test_fetches_once_per_day_and_caches(self, monkeypatch):
        calls = []

        def fake(sym):
            calls.append(sym)
            return {"recent": [{"type": "earnings_date", "date": "2026-10-12"}]}
        monkeypatch.setattr(event_risk, "_fetch", fake)
        assert event_risk.lookup("ABC") == date(2026, 10, 12)
        assert event_risk.lookup("ABC") == date(2026, 10, 12)
        assert calls == ["ABC"]

    def test_a_symbol_with_no_date_is_cached_as_none(self, monkeypatch):
        calls = []
        monkeypatch.setattr(event_risk, "_fetch", lambda s: calls.append(s) or {})
        assert event_risk.lookup("ABC") is None and event_risk.lookup("ABC") is None
        assert calls == ["ABC"]

    def test_failure_fails_open_and_is_not_retried_immediately(self, monkeypatch):
        calls = []

        def boom(sym):
            calls.append(sym)
            raise TimeoutError("event service down")
        monkeypatch.setattr(event_risk, "_fetch", boom)
        assert event_risk.lookup("ABC") is None
        assert event_risk.lookup("ABC") is None
        assert event_risk.reject_reason("ABC") is None
        assert calls == ["ABC"]

    def test_reject_reason_end_to_end_with_today_as_the_date(self, monkeypatch):
        from tz_utils import ist_now
        today = ist_now().date().isoformat()
        monkeypatch.setattr(event_risk, "_fetch", lambda s: {"recent": [{"type": "earnings_date", "date": today}]})
        assert event_risk.reject_reason("ABC").startswith("EARNINGS_TODAY")
        assert event_risk.reject_reason("ABC", carry=True).startswith("EARNINGS_TODAY")

    def test_both_switches_off_skips_the_network_entirely(self, monkeypatch):
        monkeypatch.setattr(config, "EARNINGS_DAY_BLOCK", False)
        monkeypatch.setattr(config, "EARNINGS_CARRY_BLOCK", False)
        monkeypatch.setattr(event_risk, "_fetch", lambda s: (_ for _ in ()).throw(AssertionError("must not be called")))
        assert event_risk.reject_reason("ABC") is None and event_risk.reject_reason("ABC", carry=True) is None
