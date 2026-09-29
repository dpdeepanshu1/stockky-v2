"""
tests/test_event_depth.py — coverage for event/event_depth.py
Pure stdlib + math. No network, no DB.
"""
from __future__ import annotations
import math, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "event"))
from datetime import datetime, timedelta, timezone
import pytest
import event_depth as ed


_NOW = datetime(2026, 9, 28, 10, 0, 0)


# ── classify_text ─────────────────────────────────────────────────────────────

class TestClassifyText:
    def test_results_detected(self):
        assert "results" in ed.classify_text("Q3 quarterly earnings release")

    def test_bulk_detected(self):
        assert "bulk_block" in ed.classify_text("bulk deal reported")

    def test_insider_detected(self):
        assert "insider" in ed.classify_text("promoter buying increased stake")

    def test_board_detected(self):
        assert "board" in ed.classify_text("board meeting approves dividend")

    def test_empty_text_no_tags(self):
        assert ed.classify_text("") == []

    def test_multiple_tags(self):
        tags = ed.classify_text("Q3 results and bulk deal")
        assert "results" in tags and "bulk_block" in tags

    def test_case_insensitive(self):
        assert "results" in ed.classify_text("QUARTERLY EARNINGS")


# ── summarize_event_block ─────────────────────────────────────────────────────

class TestSummarizeEventBlock:
    def test_next_earnings_date(self):
        result = ed.summarize_event_block({"next_earnings_date": "2026-10-05"}, "TCS")
        assert "results/earnings date" in result

    def test_earnings_beat(self):
        result = ed.summarize_event_block(
            {"earnings_surprise": {"surprise_pct": 8.5}}, "RELIANCE")
        assert "beat" in result

    def test_earnings_miss(self):
        result = ed.summarize_event_block(
            {"earnings_surprise": {"surprise_pct": -6.0}}, "X")
        assert "missed" in result

    def test_insider_buys(self):
        ins = [{"side": "buy"}, {"side": "buy"}]
        result = ed.summarize_event_block({"recent_insider_transactions": ins}, "Y")
        assert "buying" in result

    def test_insider_sells(self):
        ins = [{"side": "sell"}]
        result = ed.summarize_event_block({"recent_insider_transactions": ins}, "Y")
        assert "selling" in result

    def test_insider_ambiguous_side(self):
        ins = [{"side": "other"}]
        result = ed.summarize_event_block({"recent_insider_transactions": ins}, "Y")
        assert "transaction" in result.lower()

    def test_bulk_deals(self):
        result = ed.summarize_event_block({"bulk_deals": [{"side": "buy"}]}, "Z")
        assert "bulk" in result.lower()

    def test_upcoming_events(self):
        result = ed.summarize_event_block({"upcoming": ["AGM", "Board Meeting"]}, "A")
        assert "upcoming" in result.lower()

    def test_recent_events_fallback(self):
        result = ed.summarize_event_block({"recent": ["News1"]}, "B")
        assert "recent" in result.lower()

    def test_no_events(self):
        result = ed.summarize_event_block({}, "Z")
        assert "No major" in result

    def test_symbol_prefixed(self):
        result = ed.summarize_event_block({"bulk_deals": [{}]}, "HDFC")
        assert result.startswith("HDFC:")

    def test_surprise_bad_pct_skipped(self):
        result = ed.summarize_event_block(
            {"earnings_surprise": {"surprise_pct": "not_a_number"}}, "X")
        # Should not crash; no beat/miss mention
        assert "beat" not in result and "missed" not in result


# ── _age_days ─────────────────────────────────────────────────────────────────

class TestAgeDays:
    def test_same_day_is_zero(self):
        assert ed._age_days("2026-09-28", _NOW) == 0

    def test_ten_days_ago(self):
        assert ed._age_days("2026-09-18", _NOW) == 10

    def test_none_returns_none(self):
        assert ed._age_days(None, _NOW) is None

    def test_invalid_returns_none(self):
        assert ed._age_days("not-a-date", _NOW) is None

    def test_strips_time_component(self):
        assert ed._age_days("2026-09-28T09:15:00", _NOW) == 0


# ── _decay ────────────────────────────────────────────────────────────────────

class TestDecay:
    def test_zero_age_is_one(self):
        assert ed._decay(0) == pytest.approx(1.0)

    def test_none_age_is_one(self):
        assert ed._decay(None) == 1.0

    def test_negative_age_is_one(self):
        assert ed._decay(-5) == 1.0

    def test_half_life_decay(self):
        result = ed._decay(10, half_life=10)
        assert result == pytest.approx(0.5, abs=0.01)

    def test_floor_at_015(self):
        result = ed._decay(1000, half_life=10)
        assert result == pytest.approx(0.15)

    def test_ceiling_at_one(self):
        assert ed._decay(0.001) == pytest.approx(1.0, abs=0.01)


# ── _utcnow / _naive_utc (deprecation fix; must stay naive) ───────────────────

class TestUtcHelpers:
    def test_utcnow_is_naive_and_current(self):
        t = ed._utcnow()
        assert t.tzinfo is None
        assert abs((datetime.now(timezone.utc).replace(tzinfo=None) - t).total_seconds()) < 5

    def test_naive_utc_none_uses_now(self):
        assert ed._naive_utc(None).tzinfo is None

    def test_naive_utc_passes_naive_through(self):
        assert ed._naive_utc(_NOW) is _NOW

    def test_naive_utc_converts_aware_to_naive_utc(self):
        ist = timezone(timedelta(hours=5, minutes=30))
        out = ed._naive_utc(datetime(2026, 9, 28, 15, 30, tzinfo=ist))
        assert out == datetime(2026, 9, 28, 10, 0) and out.tzinfo is None

    def test_age_days_accepts_aware_now(self):
        aware = datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)
        assert ed._age_days("2026-09-18", aware) == 10

    def test_compute_event_score_accepts_aware_now(self):
        aware = datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)
        out = ed.compute_event_score({"next_earnings_date": "2026-10-01"}, now=aware)
        assert any(b["type"] == "earnings_imminent_risk" for b in out["event_score_breakdown"])

    def test_default_now_does_not_warn(self):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            ed._age_days("2026-01-01")
            ed.compute_event_score({})


class TestDecayAndScoreFallbackBranches:
    def test_decay_bad_half_life_falls_back_to_full_weight(self):
        # max(0.5, None) raises TypeError -> caught, returns 1.0 (lines 118-119)
        assert ed._decay(5, half_life=None) == 1.0

    def test_unparseable_next_earnings_date_is_ignored(self):
        out = ed.compute_event_score({"next_earnings_date": "not-a-date"}, now=_NOW)
        assert out["event_score_breakdown"] == []

    def test_non_numeric_surprise_pct_is_ignored(self):
        out = ed.compute_event_score(
            {"earnings_surprise": {"surprise_pct": "abc", "date": "2026-09-27"}}, now=_NOW)
        assert not any("earnings" in b["type"] for b in out["event_score_breakdown"])

# ── compute_event_score ───────────────────────────────────────────────────────

class TestComputeEventScore:
    def test_empty_events_returns_neutral(self):
        result = ed.compute_event_score({}, _NOW)
        assert result["event_score"] == pytest.approx(50.0)
        assert result["event_risk"] is False
        assert result["event_score_breakdown"] == []

    def test_earnings_strong_beat_raises_score(self):
        events = {"earnings_surprise": {"surprise_pct": 12.0, "date": "2026-09-25"}}
        result = ed.compute_event_score(events, _NOW)
        assert result["event_score"] > 50.0
        types = [b["type"] for b in result["event_score_breakdown"]]
        assert "earnings_strong_beat" in types

    def test_earnings_mild_beat(self):
        events = {"earnings_surprise": {"surprise_pct": 3.0}}
        result = ed.compute_event_score(events, _NOW)
        assert result["event_score"] > 50.0
        types = [b["type"] for b in result["event_score_breakdown"]]
        assert "earnings_mild_beat" in types

    def test_earnings_miss_lowers_score(self):
        events = {"earnings_surprise": {"surprise_pct": -8.0}}
        result = ed.compute_event_score(events, _NOW)
        assert result["event_score"] < 50.0

    def test_earnings_mild_miss(self):
        events = {"earnings_surprise": {"surprise_pct": -2.0}}
        result = ed.compute_event_score(events, _NOW)
        assert result["event_score"] < 50.0
        types = [b["type"] for b in result["event_score_breakdown"]]
        assert "earnings_mild_miss" in types

    def test_earnings_imminent_risk_flag(self):
        events = {"next_earnings_date": (_NOW + timedelta(days=2)).strftime("%Y-%m-%d")}
        result = ed.compute_event_score(events, _NOW)
        assert result["event_risk"] is True
        types = [b["type"] for b in result["event_score_breakdown"]]
        assert "earnings_imminent_risk" in types

    def test_pre_results_momentum(self):
        events = {"next_earnings_date": (_NOW + timedelta(days=7)).strftime("%Y-%m-%d")}
        result = ed.compute_event_score(events, _NOW)
        types = [b["type"] for b in result["event_score_breakdown"]]
        assert "pre_results_momentum" in types

    def test_bonus_or_split(self):
        events = {"corporate_actions": [{"type": "bonus issue", "date": "2026-09-25"}]}
        result = ed.compute_event_score(events, _NOW)
        assert result["event_score"] > 50.0

    def test_buyback(self):
        events = {"corporate_actions": [{"type": "buyback", "date": "2026-09-25"}]}
        result = ed.compute_event_score(events, _NOW)
        assert result["event_score"] > 50.0

    def test_rights_issue_dilutive(self):
        events = {"corporate_actions": [{"type": "rights issue", "date": "2026-09-25"}]}
        result = ed.compute_event_score(events, _NOW)
        assert result["event_score"] < 50.0

    def test_ma_activity(self):
        events = {"corporate_actions": [{"type": "merger", "date": "2026-09-25"}]}
        result = ed.compute_event_score(events, _NOW)
        assert result["event_score"] > 50.0

    def test_delisting_risk(self):
        events = {"corporate_actions": [{"type": "delisting", "date": "2026-09-25"}]}
        result = ed.compute_event_score(events, _NOW)
        assert result["event_risk"] is True
        assert result["event_score"] < 50.0

    def test_dividend(self):
        events = {"last_dividend": {"amount": 5.0, "date": "2026-09-20"}}
        result = ed.compute_event_score(events, _NOW)
        assert result["event_score"] > 50.0

    def test_analyst_upgrade(self):
        events = {"recent_analyst_actions": [{"action": "upgrade", "date": "2026-09-25"}]}
        result = ed.compute_event_score(events, _NOW)
        assert result["event_score"] > 50.0

    def test_analyst_downgrade(self):
        events = {"recent_analyst_actions": [{"action": "downgrade", "date": "2026-09-25"}]}
        result = ed.compute_event_score(events, _NOW)
        assert result["event_score"] < 50.0

    def test_analyst_buy_grade(self):
        events = {"recent_analyst_actions": [{"to_grade": "buy", "date": "2026-09-25"}]}
        result = ed.compute_event_score(events, _NOW)
        assert result["event_score"] > 50.0

    def test_insider_large_buy(self):
        events = {"recent_insider_transactions": [
            {"transaction": "buy", "shares": 5000, "date": "2026-09-25"}
        ]}
        result = ed.compute_event_score(events, _NOW)
        assert result["event_score"] > 50.0
        types = [b["type"] for b in result["event_score_breakdown"]]
        assert "insider_buying" in types

    def test_insider_small_buy(self):
        events = {"recent_insider_transactions": [
            {"transaction": "buy", "shares": 100, "date": "2026-09-25"}
        ]}
        result = ed.compute_event_score(events, _NOW)
        assert result["event_score"] > 50.0

    def test_insider_sell(self):
        events = {"recent_insider_transactions": [
            {"side": "s", "shares": 2000, "date": "2026-09-25"}
        ]}
        result = ed.compute_event_score(events, _NOW)
        assert result["event_score"] < 50.0

    def test_bulk_buy(self):
        events = {"bulk_deals": [{"buy_sell": "buy", "date": "2026-09-28"}]}
        result = ed.compute_event_score(events, _NOW)
        assert result["event_score"] > 50.0

    def test_bulk_sell(self):
        events = {"bulk_deals": [{"buy_sell": "s", "date": "2026-09-28"}]}
        result = ed.compute_event_score(events, _NOW)
        assert result["event_score"] < 50.0

    def test_fii_inflow(self):
        events = {"fii_dii_net_flow": {"net": 100.0}}
        result = ed.compute_event_score(events, _NOW)
        assert result["event_score"] > 50.0

    def test_fii_outflow(self):
        events = {"fii_dii_net_flow": {"net": -100.0}}
        result = ed.compute_event_score(events, _NOW)
        assert result["event_score"] < 50.0

    def test_fii_bad_value_skipped(self):
        events = {"fii_dii_net_flow": {"net": "N/A"}}
        result = ed.compute_event_score(events, _NOW)
        assert result["event_score"] == pytest.approx(50.0)

    def test_regulatory_action(self):
        events = {"regulatory_actions": [
            {"description": "SEBI probe", "date": "2026-09-20"}
        ]}
        result = ed.compute_event_score(events, _NOW)
        assert result["event_risk"] is True
        assert result["event_score"] < 50.0

    def test_board_meeting_no_ca(self):
        events = {"board_meeting_date": "2026-10-01"}
        result = ed.compute_event_score(events, _NOW)
        assert result["event_score"] > 50.0

    def test_score_clamped_0_100(self):
        many_buys = {
            "earnings_surprise": {"surprise_pct": 50.0},
            "recent_insider_transactions": [
                {"transaction": "buy", "shares": 9999} for _ in range(5)
            ],
            "bulk_deals": [{"buy_sell": "buy"} for _ in range(5)],
            "corporate_actions": [{"type": "buyback"}],
        }
        result = ed.compute_event_score(many_buys, _NOW)
        assert 0.0 <= result["event_score"] <= 100.0

    def test_earnings_days_out_populated(self):
        future = (_NOW + timedelta(days=7)).strftime("%Y-%m-%d")
        result = ed.compute_event_score({"next_earnings_date": future}, _NOW)
        assert result["earnings_days_out"] in (6, 7)   # depends on hour boundary

    def test_block_deals_key(self):
        events = {"block_deals": [{"buy_sell": "buy", "date": "2026-09-28"}]}
        result = ed.compute_event_score(events, _NOW)
        assert result["event_score"] > 50.0

    def test_insider_transactions_key(self):
        events = {"insider_transactions": [
            {"transaction": "sell", "shares": 500, "date": "2026-09-25"}
        ]}
        result = ed.compute_event_score(events, _NOW)
        assert result["event_score"] < 50.0


# ── enrich_events ─────────────────────────────────────────────────────────────

class TestEnrichEvents:
    def test_adds_event_summary(self):
        out = ed.enrich_events({"earnings_surprise": {"surprise_pct": 10.0}}, "TCS")
        assert "event_summary" in out
        assert "beat" in out["event_summary"]

    def test_adds_event_score(self):
        out = ed.enrich_events({}, "X")
        assert "event_score" in out
        assert "event_score_breakdown" in out
        assert "event_risk" in out

    def test_backward_compat_recent_event_score(self):
        out = ed.enrich_events({}, "X")
        assert "recent_event_score" in out
        assert 0.0 <= out["recent_event_score"] <= 1.0

    def test_has_positive_catalyst_false_neutral(self):
        out = ed.enrich_events({}, "X")
        assert out["has_positive_catalyst"] is False

    def test_has_positive_catalyst_true_on_strong_beat(self):
        out = ed.enrich_events(
            {"earnings_surprise": {"surprise_pct": 20.0}}, "X")
        assert out["has_positive_catalyst"] is True

    def test_earnings_days_out_propagated(self):
        future = (_NOW + timedelta(days=7)).strftime("%Y-%m-%d")
        out = ed.enrich_events({"next_earnings_date": future})
        # enrich_events calls datetime.utcnow() internally so days_out varies slightly
        assert out.get("earnings_days_out") in range(4, 9)

    def test_event_score_raw_delta_present(self):
        out = ed.enrich_events({"earnings_surprise": {"surprise_pct": 5.0}}, "Y")
        assert "event_score_raw_delta" in out

    def test_none_input(self):
        out = ed.enrich_events(None, "Z")
        assert "event_summary" in out

    def test_preserves_original_fields(self):
        out = ed.enrich_events({"custom_field": "hello"}, "X")
        assert out["custom_field"] == "hello"
