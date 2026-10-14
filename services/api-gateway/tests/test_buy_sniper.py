"""tests/test_buy_sniper.py — coverage for api-gateway/buy_sniper.py

Hot Picks scoring -> buy-suggestion card builder -> actionable filter. Pure functions: no network,
no I/O. The module reads its thresholds from the environment at import time, so every test loads a
FRESH copy with every SNIPER_* / MAX_PRICE / VALUE_BUY_THRESHOLD variable cleared (a VM that has
any of them exported can never change an assertion here); env-override tests pass their own values.

A separate class guards against drift: the card carries every required field of the frontend's
BuySuggestion interface, and the names main.py imports from this module exist.

Run from services/api-gateway:
    python3 -m pytest tests/test_buy_sniper.py -v
"""
from __future__ import annotations

import ast
import importlib.util
import logging
import os
import re

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = os.path.dirname(_HERE)
_MOD_PATH = os.path.join(_SERVICE, "buy_sniper.py")
_REPO = os.path.dirname(os.path.dirname(_SERVICE))

_ENV_KEYS = ("SNIPER_MIN_CONVICTION", "SNIPER_MIN_PRICE", "MAX_PRICE", "VALUE_BUY_THRESHOLD",
             "SNIPER_MIN_REWARD_RISK", "SNIPER_SECTOR_BONUS", "SNIPER_SECTOR_PENALTY")


class Loader:
    def __init__(self, monkeypatch):
        self.mp = monkeypatch
        self.n = 0

    def __call__(self, **env):
        for k in _ENV_KEYS:
            self.mp.delenv(k, raising=False)
        for k, v in env.items():
            self.mp.setenv(k, v)
        self.n += 1
        spec = importlib.util.spec_from_file_location(f"buy_sniper_under_test_{self.n}", _MOD_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


@pytest.fixture
def load(monkeypatch):
    return Loader(monkeypatch)


@pytest.fixture
def bs(load):
    return load()


def row(**kw):
    """A scan row that is a clean BUY candidate at ₹100 unless overridden."""
    base = {"symbol": "ABC", "price": 100.0, "decision": "BUY", "conviction": 70.0}
    base.update(kw)
    return base


# ── import-time constants ─────────────────────────────────────────────────────

class TestConstants:
    def test_defaults(self, bs):
        assert bs.MIN_CONVICTION == 62
        assert bs.MIN_PRICE == 20.0
        assert bs.MAX_PRICE == 0.0
        assert bs.VALUE_BUY_THRESHOLD == 500.0
        assert bs.MIN_REWARD_RISK == 1.8
        assert bs.SECTOR_BONUS == 3 and bs.SECTOR_PENALTY == 3
        assert bs.DEFAULT_TARGET_COUNT == 4
        assert (bs.ATR_STOP_MULTIPLIER, bs.ATR_TARGET_MULTIPLIER) == (1.5, 3.0)
        assert (bs.MIN_STOP_PCT, bs.MAX_STOP_PCT, bs.MIN_TARGET_PCT, bs.MAX_TARGET_PCT) == (2.0, 6.0, 4.0, 12.0)
        assert (bs.EST_PROFIT_PCT, bs.STOP_LOSS_PCT) == (6.5, 3.2)

    def test_env_overrides(self, load):
        m = load(SNIPER_MIN_CONVICTION="70", SNIPER_MIN_PRICE="35.5", MAX_PRICE="1500",
                 VALUE_BUY_THRESHOLD="800", SNIPER_MIN_REWARD_RISK="2.5", SNIPER_SECTOR_BONUS="5",
                 SNIPER_SECTOR_PENALTY="1")
        assert (m.MIN_CONVICTION, m.MIN_PRICE, m.MAX_PRICE, m.VALUE_BUY_THRESHOLD) == (70, 35.5, 1500.0, 800.0)
        assert (m.MIN_REWARD_RISK, m.SECTOR_BONUS, m.SECTOR_PENALTY) == (2.5, 5, 1)

    def test_blank_max_price_means_no_ceiling(self, load):
        assert load(MAX_PRICE="").MAX_PRICE == 0.0

    def test_blank_value_buy_threshold_falls_back_to_500(self, load):
        assert load(VALUE_BUY_THRESHOLD="").VALUE_BUY_THRESHOLD == 500.0

    def test_sector_sets_are_lowercase(self, bs):
        assert all(s == s.lower() for s in bs._OUTPERFORMING_SECTORS | bs._UNDERPERFORMING_SECTORS)

    def test_no_sector_key_is_both_good_and_bad(self, bs):
        assert not (bs._OUTPERFORMING_SECTORS & bs._UNDERPERFORMING_SECTORS)


# ── _atr_pct ──────────────────────────────────────────────────────────────────

class TestAtrPct:
    def test_atr_key(self, bs):
        assert bs._atr_pct({"atr": 2.0}, 100.0) == pytest.approx(2.0)

    def test_daily_atr_key(self, bs):
        assert bs._atr_pct({"daily_atr": 3.0}, 100.0) == pytest.approx(3.0)

    def test_atr_beats_daily_atr(self, bs):
        assert bs._atr_pct({"atr": 2.0, "daily_atr": 9.0}, 100.0) == pytest.approx(2.0)

    def test_string_numbers_are_accepted(self, bs):
        assert bs._atr_pct({"atr": "4"}, 200.0) == pytest.approx(2.0)

    @pytest.mark.parametrize("price", [0, -5, -0.01])
    def test_non_positive_price_is_none(self, bs, price):
        assert bs._atr_pct({"atr": 2.0}, price) is None

    @pytest.mark.parametrize("bad", [None, "abc", "", [], {}])
    def test_unparseable_atr_falls_through_to_the_next_key(self, bs, bad):
        assert bs._atr_pct({"atr": bad, "daily_atr": 2.0}, 100.0) == pytest.approx(2.0)

    @pytest.mark.parametrize("v", [0, -1, "-3"])
    def test_non_positive_atr_falls_through(self, bs, v):
        assert bs._atr_pct({"atr": v, "daily_atr": 2.0}, 100.0) == pytest.approx(2.0)

    @pytest.mark.parametrize("v", [0.05, 30.0])
    def test_out_of_band_atr_falls_through_to_the_next_key(self, bs, v):
        assert bs._atr_pct({"atr": v, "daily_atr": 2.0}, 100.0) == pytest.approx(2.0)

    @pytest.mark.parametrize("v, expected", [(0.1, 0.1), (25.0, 25.0)])
    def test_band_edges_are_inclusive(self, bs, v, expected):
        assert bs._atr_pct({"atr": v}, 100.0) == pytest.approx(expected)

    @pytest.mark.parametrize("v", [0.0999, 25.01])
    def test_just_outside_the_band(self, bs, v):
        assert bs._atr_pct({"atr": v}, 100.0) is None

    def test_nothing_usable_is_none(self, bs):
        assert bs._atr_pct({}, 100.0) is None
        assert bs._atr_pct({"atr": None, "daily_atr": "x"}, 100.0) is None


# ── _num ──────────────────────────────────────────────────────────────────────

class TestNum:
    @pytest.mark.parametrize("val, expected", [(5, 5.0), ("2.5", 2.5), (0, 0.0), (-3, -3.0), (True, 1.0)])
    def test_numbers(self, bs, val, expected):
        assert bs._num(val) == expected

    @pytest.mark.parametrize("val", [None, ""])
    def test_none_and_empty_use_the_default(self, bs, val):
        assert bs._num(val) == 0.0
        assert bs._num(val, default=7.0) == 7.0

    @pytest.mark.parametrize("val", ["abc", [], {}, object(), "1,000"])
    def test_garbage_uses_the_default(self, bs, val):
        assert bs._num(val) == 0.0
        assert bs._num(val, default=-1.0) == -1.0


# ── _decision_label ───────────────────────────────────────────────────────────

class TestDecisionLabel:
    @pytest.mark.parametrize("s, expected", [
        ({"decision": "buy now"}, "BUY NOW"),
        ({"decision": " Hold "}, "HOLD"),
        ({"signal": "wait"}, "WAIT"),
        ({"decision": "buy", "signal": "sell"}, "BUY"),
        ({"decision": "", "signal": "sell"}, "SELL"),
        ({"decision": None, "signal": None}, ""),
        ({}, ""),
        ({"decision": 5}, "5"),
    ])
    def test_cases(self, bs, s, expected):
        assert bs._decision_label(s) == expected


# ── _conviction ───────────────────────────────────────────────────────────────

class TestConviction:
    @pytest.mark.parametrize("key", ["conviction", "combined_score", "conviction_score"])
    def test_each_key(self, bs, key):
        assert bs._conviction({key: 66}) == 66.0

    def test_priority_order(self, bs):
        assert bs._conviction({"conviction": 60, "combined_score": 70, "conviction_score": 80}) == 60.0
        assert bs._conviction({"combined_score": 70, "conviction_score": 80}) == 70.0

    @pytest.mark.parametrize("bad", [0, -5, None, "abc", ""])
    def test_unusable_key_falls_through(self, bs, bad):
        assert bs._conviction({"conviction": bad, "combined_score": 64}) == 64.0

    def test_blend_of_tech_and_fundamental(self, bs):
        assert bs._conviction({"technical_score": 80, "fundamental_score": 60}) == pytest.approx(0.55 * 80 + 0.45 * 60)

    def test_tech_only_assumes_neutral_fundamentals(self, bs):
        assert bs._conviction({"technical_score": 80}) == pytest.approx(0.55 * 80 + 0.45 * 50)

    def test_fundamental_only_assumes_neutral_technicals(self, bs):
        assert bs._conviction({"fundamental_score": 60}) == pytest.approx(0.55 * 50 + 0.45 * 60)

    def test_a_direct_conviction_beats_the_blend(self, bs):
        assert bs._conviction({"conviction": 10, "technical_score": 99, "fundamental_score": 99}) == 10.0

    @pytest.mark.parametrize("s", [{}, {"technical_score": 0, "fundamental_score": 0},
                                   {"technical_score": -5}, {"technical_score": "x"}])
    def test_nothing_usable_is_zero(self, bs, s):
        assert bs._conviction(s) == 0.0


# ── _price ────────────────────────────────────────────────────────────────────

class TestPrice:
    @pytest.mark.parametrize("key", ["price", "cmp", "last_price", "ltp", "close", "current_price"])
    def test_each_key(self, bs, key):
        assert bs._price({key: 123.4}) == 123.4

    def test_priority_order(self, bs):
        assert bs._price({"close": 5, "ltp": 4, "cmp": 3, "price": 2}) == 2.0
        assert bs._price({"close": 5, "ltp": 4}) == 4.0

    @pytest.mark.parametrize("bad", [0, -1, None, "abc", ""])
    def test_unusable_price_falls_through(self, bs, bad):
        assert bs._price({"price": bad, "cmp": 50}) == 50.0

    def test_nothing_usable_is_zero(self, bs):
        assert bs._price({}) == 0.0
        assert bs._price({"price": 0, "cmp": None}) == 0.0


# ── _sector_adjusted_conviction ───────────────────────────────────────────────

class TestSectorAdjustedConviction:
    @pytest.mark.parametrize("sector", [None, "", "   ", 0])
    def test_no_sector_is_unchanged(self, bs, sector):
        assert bs._sector_adjusted_conviction({"sector": sector}, 65.0) == 65.0

    def test_missing_sector_key_is_unchanged(self, bs):
        assert bs._sector_adjusted_conviction({}, 65.0) == 65.0

    @pytest.mark.parametrize("sector", ["Banking", "PSU Bank", "Public Sector Bank", "Auto", "Automobile",
                                        "Automotive Ancillaries", "Metals & Mining", "Steel", "Private Bank",
                                        "  BANKS  ", "private sector bank"])
    def test_outperformers_get_the_bonus(self, bs, sector):
        assert bs._sector_adjusted_conviction({"sector": sector}, 65.0) == 68.0

    @pytest.mark.parametrize("sector", ["IT", "Information Technology", "Tech", "Pharma", "Pharmaceuticals",
                                        "Energy", "Oil & Gas", "Power"])
    def test_laggards_get_the_penalty(self, bs, sector):
        assert bs._sector_adjusted_conviction({"sector": sector}, 65.0) == 62.0

    @pytest.mark.parametrize("sector", ["FMCG", "Realty", "Textiles", "Chemicals"])
    def test_neutral_sectors_are_unchanged(self, bs, sector):
        assert bs._sector_adjusted_conviction({"sector": sector}, 65.0) == 65.0

    def test_penalty_never_goes_below_zero(self, bs):
        assert bs._sector_adjusted_conviction({"sector": "IT"}, 2.0) == 0.0
        assert bs._sector_adjusted_conviction({"sector": "IT"}, 0.0) == 0.0

    def test_bonus_is_not_capped(self, bs):
        assert bs._sector_adjusted_conviction({"sector": "Banking"}, 99.0) == 102.0

    def test_outperformer_check_wins_over_laggard_check(self, bs):
        assert bs._sector_adjusted_conviction({"sector": "Power Bank"}, 65.0) == 68.0

    def test_bonus_and_penalty_follow_the_env(self, load):
        m = load(SNIPER_SECTOR_BONUS="7", SNIPER_SECTOR_PENALTY="10")
        assert m._sector_adjusted_conviction({"sector": "bank"}, 60.0) == 67.0
        assert m._sector_adjusted_conviction({"sector": "it"}, 60.0) == 50.0

    def test_non_string_sector_is_stringified(self, bs):
        assert bs._sector_adjusted_conviction({"sector": 123}, 65.0) == 65.0


# ── _is_buy_candidate ─────────────────────────────────────────────────────────

class TestIsBuyCandidate:
    @pytest.mark.parametrize("s", [None, [], "x", 5, ()])
    def test_non_dict_is_rejected(self, bs, s):
        assert bs._is_buy_candidate(s) is False

    @pytest.mark.parametrize("decision", ["BUY NOW", "BUY", "PREPARE TO BUY", "buy", " prepare to buy "])
    def test_buy_labels_pass(self, bs, decision):
        assert bs._is_buy_candidate(row(decision=decision)) is True

    @pytest.mark.parametrize("decision", ["SELL", "AVOID", "STRONG SELL", "ERROR", "MAYBE"])
    def test_other_labels_fail_even_with_momentum(self, bs, decision):
        assert bs._is_buy_candidate(row(decision=decision, conviction=90, change_pct=5)) is False

    def test_price_floor(self, bs):
        assert bs._is_buy_candidate(row(price=19.99)) is False
        assert bs._is_buy_candidate(row(price=20.0)) is True

    def test_no_price_is_rejected(self, bs):
        assert bs._is_buy_candidate({"decision": "BUY", "conviction": 90}) is False

    def test_price_ceiling_is_off_by_default(self, bs):
        assert bs._is_buy_candidate(row(price=1_000_000)) is True

    def test_price_ceiling_when_set(self, load):
        m = load(MAX_PRICE="500")
        assert m._is_buy_candidate(row(price=500.0)) is True
        assert m._is_buy_candidate(row(price=500.01)) is False

    def test_conviction_floor(self, bs):
        assert bs._is_buy_candidate(row(conviction=61.9)) is False
        assert bs._is_buy_candidate(row(conviction=62.0)) is True

    def test_explicit_min_conviction_argument(self, bs):
        assert bs._is_buy_candidate(row(conviction=70), min_conviction=71) is False
        assert bs._is_buy_candidate(row(conviction=70), min_conviction=70) is True
        assert bs._is_buy_candidate(row(conviction=50), min_conviction=40) is True

    def test_sector_bonus_can_lift_a_row_over_the_bar(self, bs):
        assert bs._is_buy_candidate(row(conviction=60)) is False
        assert bs._is_buy_candidate(row(conviction=60, sector="Banking")) is True

    def test_sector_penalty_can_push_a_row_under_the_bar(self, bs):
        assert bs._is_buy_candidate(row(conviction=64, sector="IT")) is False

    def test_the_default_bar_comes_from_the_env(self, load):
        m = load(SNIPER_MIN_CONVICTION="80")
        assert m._is_buy_candidate(row(conviction=79)) is False
        assert m._is_buy_candidate(row(conviction=80)) is True

    @pytest.mark.parametrize("decision", ["HOLD", "WAIT", "", "hold"])
    def test_early_breakout_on_a_non_buy_label(self, bs, decision):
        assert bs._is_buy_candidate(row(decision=decision, conviction=72, change_pct=1.2)) is True

    def test_breakout_via_signal_field(self, bs):
        s = {"symbol": "A", "price": 100, "signal": "wait", "conviction": 75, "change_pct": 2}
        assert bs._is_buy_candidate(s) is True

    def test_breakout_needs_conviction_72(self, bs):
        assert bs._is_buy_candidate(row(decision="HOLD", conviction=71.9, change_pct=3)) is False

    def test_breakout_needs_change_of_1_2_pct(self, bs):
        assert bs._is_buy_candidate(row(decision="HOLD", conviction=80, change_pct=1.19)) is False
        assert bs._is_buy_candidate(row(decision="HOLD", conviction=80, change_pct=-2)) is False

    def test_breakout_uses_the_sector_adjusted_conviction(self, bs):
        assert bs._is_buy_candidate(row(decision="HOLD", conviction=70, change_pct=2, sector="Auto")) is True
        assert bs._is_buy_candidate(row(decision="HOLD", conviction=74, change_pct=2, sector="IT")) is False

    def test_missing_change_pct_counts_as_zero(self, bs):
        assert bs._is_buy_candidate(row(decision="HOLD", conviction=90)) is False


# ── _action_for ───────────────────────────────────────────────────────────────

class TestActionFor:
    @pytest.mark.parametrize("s, expected", [
        ({"decision": "BUY NOW"}, "BUY NOW"),
        ({"decision": "buy"}, "BUY NOW"),
        ({"signal": "BUY"}, "BUY NOW"),
        ({"decision": "PREPARE TO BUY"}, "BUY ON 15M BREAKOUT"),
        ({"decision": "HOLD"}, "BUY ON CONFIRMATION"),
        ({"decision": ""}, "BUY ON CONFIRMATION"),
        ({}, "BUY ON CONFIRMATION"),
    ])
    def test_cases(self, bs, s, expected):
        assert bs._action_for(s) == expected


# ── _market_context_note / _rationale ─────────────────────────────────────────

class TestRationale:
    def test_market_note(self, bs):
        note = bs._market_context_note()
        assert note.startswith("Market: Nifty") and "high-conviction" in note.lower()

    def test_buy_now(self, bs):
        out = bs._rationale({"technical_score": 80, "fundamental_score": 60, "sector": "Banking"}, "BUY NOW", 71.9)
        assert out == ("Conviction 71/100, tech 80, fund 60, Banking. High-conviction setup — aligned momentum and "
                       f"fundamentals. {bs._market_context_note()}")

    def test_prepare_to_buy(self, bs):
        out = bs._rationale({"sector": "Auto"}, "BUY ON 15M BREAKOUT", 65)
        assert out == ("Conviction 65/100, Auto. Prepare-to-buy: wait for 15m breakout above range with volume. "
                       f"{bs._market_context_note()}")

    def test_confirmation(self, bs):
        out = bs._rationale({}, "BUY ON CONFIRMATION", 65)
        assert out == f"Conviction 65/100, sector. Confirmation required before entry. {bs._market_context_note()}"

    def test_zero_scores_are_left_out(self, bs):
        out = bs._rationale({"technical_score": 0, "fundamental_score": "x", "sector": "Metal"}, "BUY NOW", 70)
        assert out.startswith("Conviction 70/100, Metal. ")

    def test_only_tech_or_only_fund(self, bs):
        assert bs._rationale({"technical_score": 55.9}, "BUY NOW", 70).startswith("Conviction 70/100, tech 55, sector. ")
        assert bs._rationale({"fundamental_score": 44}, "BUY NOW", 70).startswith("Conviction 70/100, fund 44, sector. ")

    def test_empty_sector_uses_the_placeholder(self, bs):
        assert ", sector. " in bs._rationale({"sector": ""}, "BUY NOW", 70)
        assert ", sector. " in bs._rationale({"sector": None}, "BUY NOW", 70)

    def test_unknown_action_is_the_confirmation_text(self, bs):
        assert "Confirmation required" in bs._rationale({}, "SOMETHING ELSE", 70)


# ── build_suggestion ──────────────────────────────────────────────────────────

class TestBuildSuggestionGuards:
    @pytest.mark.parametrize("s", [None, [], {}, row(decision="SELL"), row(price=10), row(conviction=10)])
    def test_not_a_candidate_is_none(self, bs, s):
        assert bs.build_suggestion(s) is None

    def test_zero_price_after_a_zero_floor_is_none(self, load):
        m = load(SNIPER_MIN_PRICE="0")
        assert m.build_suggestion({"symbol": "A", "decision": "BUY", "conviction": 90}) is None


class TestBuildSuggestionCard:
    def test_full_card_with_flat_fallbacks(self, bs):
        card = bs.build_suggestion(row(technical_score=80, fundamental_score=65, sector="FMCG", change_pct=1.5,
                                       holding_period="1 week"))
        assert card == {
            "symbol": "ABC",
            "action": "BUY NOW",
            "buy_price_range": "₹99.5 - ₹100.8",
            "buy_price_low": 99.5,
            "buy_price_high": 100.8,
            "entry_time": "Next Trading Session (09:25 AM - 09:45 AM)",
            "entry_window": "09:25 AM - 09:45 AM IST",
            "target_price": 106.5,
            "stop_loss": 96.8,
            "estimated_profit": "+6.5% (₹6.5/share)",
            "estimated_profit_pct": 6.5,
            "reward_risk_ratio": 2.03,
            "holding_duration": "2 to 5 Trading Days",
            "holding_period": "1 week",
            "conviction_score": 70,
            "technical_score": 80,
            "fundamental_score": 65,
            "change_pct": 1.5,
            "price": 100.0,
            "sector": "FMCG",
            "rationale": bs._rationale(row(technical_score=80, fundamental_score=65, sector="FMCG"), "BUY NOW", 70.0),
            "decision": "BUY",
            "value_buy": True,
            "market_context": "Nifty correction — only high-R:R setups surfaced",
        }

    def test_defaults_for_missing_scores_and_holding_period(self, bs):
        card = bs.build_suggestion(row())
        assert card["technical_score"] == 70 and card["fundamental_score"] == 70
        assert card["holding_period"] == "2-5 Days"
        assert card["change_pct"] == 0.0 and card["sector"] is None

    @pytest.mark.parametrize("blank", [None, ""])
    def test_blank_holding_period_uses_the_default(self, bs, blank):
        assert bs.build_suggestion(row(holding_period=blank))["holding_period"] == "2-5 Days"

    def test_zero_scores_also_get_the_default(self, bs):
        card = bs.build_suggestion(row(technical_score=0, fundamental_score=0))
        assert (card["technical_score"], card["fundamental_score"]) == (70, 70)

    @pytest.mark.parametrize("raw", ["abc", "ABC.NS", " abc.bo ", "Abc"])
    def test_symbol_is_normalised(self, bs, raw):
        assert bs.build_suggestion(row(symbol=raw))["symbol"] == "ABC"

    @pytest.mark.parametrize("raw", [None, ""])
    def test_missing_symbol_is_an_empty_string(self, bs, raw):
        assert bs.build_suggestion(row(symbol=raw))["symbol"] == ""

    def test_price_is_taken_from_the_first_usable_key(self, bs):
        s = {"symbol": "A", "cmp": 250.0, "decision": "BUY", "conviction": 70}
        card = bs.build_suggestion(s)
        assert card["price"] == 250.0 and card["buy_price_low"] == 248.75 and card["buy_price_high"] == 252.0

    def test_action_and_decision_fields(self, bs):
        assert bs.build_suggestion(row(decision="PREPARE TO BUY"))["action"] == "BUY ON 15M BREAKOUT"
        card = bs.build_suggestion(row(decision="HOLD", conviction=80, change_pct=2))
        assert card["action"] == "BUY ON CONFIRMATION" and card["decision"] == "HOLD"

    def test_decision_falls_back_to_the_action_when_unlabelled(self, bs):
        card = bs.build_suggestion(row(decision="", conviction=80, change_pct=2))
        assert card["decision"] == "BUY ON CONFIRMATION"

    def test_conviction_score_is_rounded_and_sector_adjusted(self, bs):
        assert bs.build_suggestion(row(conviction=70.6))["conviction_score"] == 71
        assert bs.build_suggestion(row(conviction=70, sector="Banking"))["conviction_score"] == 73
        assert bs.build_suggestion(row(conviction=70, sector="Pharma"))["conviction_score"] == 67

    def test_the_source_row_is_not_mutated(self, bs):
        s = row(technical_score=80, sector="Auto")
        before = dict(s)
        bs.build_suggestion(s)
        assert s == before


class TestBuildSuggestionValueBuy:
    @pytest.mark.parametrize("price, expected", [(20.0, True), (499.99, True), (500.0, True), (500.01, False),
                                                 (1500.0, False)])
    def test_range(self, bs, price, expected):
        assert bs.build_suggestion(row(price=price))["value_buy"] is expected

    def test_threshold_follows_the_env(self, load):
        m = load(VALUE_BUY_THRESHOLD="1000")
        assert m.build_suggestion(row(price=900.0))["value_buy"] is True


class TestBuildSuggestionTargetAndStop:
    def test_atr_drives_target_and_stop(self, bs):
        card = bs.build_suggestion(row(atr=2.0))               # 2% ATR -> target 6%, stop 3%
        assert card["target_price"] == 106.0 and card["stop_loss"] == 97.0
        assert card["estimated_profit_pct"] == 6.0 and card["reward_risk_ratio"] == 2.0

    def test_daily_atr_key_is_used_too(self, bs):
        card = bs.build_suggestion(row(daily_atr=2.0))
        assert card["target_price"] == 106.0 and card["stop_loss"] == 97.0

    def test_tiny_atr_is_clamped_up(self, bs):
        card = bs.build_suggestion(row(atr=0.5))               # 0.5% -> target 1.5%->4 ; stop 0.75%->2
        assert card["target_price"] == 104.0 and card["stop_loss"] == 98.0

    def test_huge_atr_is_clamped_down(self, bs):
        card = bs.build_suggestion(row(atr=10.0))              # 10% -> target 30%->12 ; stop 15%->6
        assert card["target_price"] == 112.0 and card["stop_loss"] == 94.0

    def test_out_of_band_atr_falls_back_to_flat_percentages(self, bs):
        card = bs.build_suggestion(row(atr=40.0))
        assert card["target_price"] == 106.5 and card["stop_loss"] == 96.8

    def test_est_profit_argument_replaces_the_flat_target(self, bs):
        card = bs.build_suggestion(row(), est_profit_pct=8.0)
        assert card["target_price"] == 108.0 and card["estimated_profit_pct"] == 8.0

    def test_est_profit_argument_is_ignored_when_atr_exists(self, bs):
        card = bs.build_suggestion(row(atr=2.0), est_profit_pct=9.0)
        assert card["target_price"] == 106.0

    def test_model_target_is_used(self, bs):
        card = bs.build_suggestion(row(target=110.0))
        assert card["target_price"] == 110.0 and card["estimated_profit_pct"] == 10.0
        assert card["stop_loss"] == 96.8                        # stop still flat

    def test_target_price_key_is_the_fallback_name(self, bs):
        assert bs.build_suggestion(row(target_price=110.0))["target_price"] == 110.0

    def test_target_beats_target_price(self, bs):
        assert bs.build_suggestion(row(target=110.0, target_price=120.0))["target_price"] == 110.0

    def test_zero_target_falls_back_to_target_price(self, bs):
        assert bs.build_suggestion(row(target=0, target_price=110.0))["target_price"] == 110.0

    def test_unusable_target_is_replaced_by_the_computed_one(self, bs):
        assert bs.build_suggestion(row(target="abc"))["target_price"] == 106.5

    def test_model_target_percentage_is_rounded_to_two_decimals(self, bs):
        card = bs.build_suggestion(row(target=108.333, stop_loss=96.0))
        assert card["estimated_profit_pct"] == 8.33

    def test_model_stop_is_used(self, bs):
        card = bs.build_suggestion(row(stop_loss=97.0))        # flat 6.5% target vs a 3.0 risk
        assert card["stop_loss"] == 97.0 and card["target_price"] == 106.5
        assert card["reward_risk_ratio"] == 2.17

    def test_model_target_and_stop_together(self, bs):
        card = bs.build_suggestion(row(target=110.0, stop_loss=95.0))
        assert card["reward_risk_ratio"] == 2.0 and card["stop_loss"] == 95.0

    def test_non_positive_model_stop_is_recomputed(self, bs):
        assert bs.build_suggestion(row(stop_loss=0))["stop_loss"] == 96.8
        assert bs.build_suggestion(row(stop_loss=-4))["stop_loss"] == 96.8

    def test_estimated_profit_text_uses_the_effective_percentage(self, bs):
        card = bs.build_suggestion(row(target=110.0, stop_loss=95.0))
        assert card["estimated_profit"] == "+10.0% (₹10.0/share)"


class TestBuildSuggestionRewardRisk:
    def test_low_reward_risk_is_dropped(self, bs):
        assert bs.build_suggestion(row(target=103.0, stop_loss=98.0)) is None      # 1.5

    def test_model_stop_that_makes_the_flat_target_too_thin_is_dropped(self, bs):
        assert bs.build_suggestion(row(stop_loss=95.0)) is None                    # 6.5 / 5.0 = 1.3

    def test_exactly_at_the_minimum_passes(self, bs):
        card = bs.build_suggestion(row(target=118.0, stop_loss=90.0))              # 18 / 10 = 1.8
        assert card is not None and card["reward_risk_ratio"] == 1.8

    def test_just_under_the_minimum_fails(self, bs):
        assert bs.build_suggestion(row(target=117.9, stop_loss=90.0)) is None

    @pytest.mark.parametrize("stop", [100.0, 101.0, 150.0])
    def test_stop_at_or_above_entry_is_dropped(self, bs, stop):
        assert bs.build_suggestion(row(target=130.0, stop_loss=stop)) is None

    def test_target_below_entry_is_dropped(self, bs):
        assert bs.build_suggestion(row(target=90.0)) is None

    def test_the_minimum_follows_the_env(self, load):
        m = load(SNIPER_MIN_REWARD_RISK="2.5")
        assert m.build_suggestion(row(atr=2.0)) is None                            # 2.0 < 2.5
        assert m.build_suggestion(row(target=115.0, stop_loss=94.0)) is not None   # 15/6 = 2.5

    def test_the_skip_is_logged_at_debug(self, bs, caplog):
        with caplog.at_level(logging.DEBUG, logger="buy-sniper"):
            bs.build_suggestion(row(symbol="LOWRR", target=103.0, stop_loss=98.0))
        assert any("LOWRR skipped" in r.getMessage() and "R:R 1.50" in r.getMessage() for r in caplog.records)

    def test_the_skip_log_survives_a_non_positive_risk(self, bs, caplog):
        with caplog.at_level(logging.DEBUG, logger="buy-sniper"):
            assert bs.build_suggestion(row(symbol="BADSTOP", target=130.0, stop_loss=105.0)) is None
        assert any("BADSTOP skipped" in r.getMessage() for r in caplog.records)


# ── filter_actionable_buy_suggestions ─────────────────────────────────────────

def rows(*specs):
    out = []
    for i, (sym, conv) in enumerate(specs):
        out.append(row(symbol=sym, conviction=conv))
    return out


class TestFilterActionable:
    @pytest.mark.parametrize("bad", [None, {}, "x", 5, ()])
    def test_non_list_input_is_empty(self, bs, bad):
        assert bs.filter_actionable_buy_suggestions(bad) == []

    def test_empty_list(self, bs):
        assert bs.filter_actionable_buy_suggestions([]) == []

    def test_sorted_by_conviction_descending(self, bs):
        out = bs.filter_actionable_buy_suggestions(rows(("A", 65), ("B", 90), ("C", 75)))
        assert [c["symbol"] for c in out] == ["B", "C", "A"]

    def test_reward_risk_breaks_conviction_ties(self, bs):
        s = [row(symbol="LOW", conviction=70, target=110.0, stop_loss=94.0),        # 10/6 = 1.67 -> dropped
             row(symbol="MID", conviction=70, target=115.0, stop_loss=94.0),        # 2.5
             row(symbol="TOP", conviction=70, target=120.0, stop_loss=94.0)]        # 3.33
        out = bs.filter_actionable_buy_suggestions(s)
        assert [c["symbol"] for c in out] == ["TOP", "MID"]

    def test_default_target_count_is_four(self, bs):
        s = rows(*[(f"S{i}", 65 + i) for i in range(8)])
        assert len(bs.filter_actionable_buy_suggestions(s)) == 4

    @pytest.mark.parametrize("tc, expected", [(1, 1), (2, 2), (10, 10), (11, 10), (99, 10), (0, 4), (None, 4),
                                              (-3, 1), ("3", 3), (2.9, 2)])
    def test_target_count_is_clamped(self, bs, tc, expected):
        s = rows(*[(f"S{i}", 65 + (i % 30)) for i in range(15)])
        assert len(bs.filter_actionable_buy_suggestions(s, target_count=tc)) == expected

    def test_fewer_candidates_than_the_count(self, bs):
        assert len(bs.filter_actionable_buy_suggestions(rows(("A", 70), ("B", 71)), target_count=5)) == 2

    def test_non_dict_rows_are_skipped(self, bs):
        out = bs.filter_actionable_buy_suggestions([None, "x", 5, [1], row(symbol="A")])
        assert [c["symbol"] for c in out] == ["A"]

    def test_meta_rows_are_skipped(self, bs):
        out = bs.filter_actionable_buy_suggestions([row(symbol="META", _meta=True), row(symbol="A")])
        assert [c["symbol"] for c in out] == ["A"]

    @pytest.mark.parametrize("label", ["ERROR", "error", "Error"])
    def test_error_rows_are_skipped(self, bs, label):
        assert bs.filter_actionable_buy_suggestions([row(decision=label)]) == []

    def test_non_candidates_are_skipped(self, bs):
        s = [row(symbol="CHEAP", price=10), row(symbol="WEAK", conviction=30), row(symbol="SELL", decision="SELL"),
             row(symbol="OK")]
        assert [c["symbol"] for c in bs.filter_actionable_buy_suggestions(s)] == ["OK"]

    def test_rows_that_fail_reward_risk_are_skipped(self, bs):
        s = [row(symbol="THIN", target=101.0, stop_loss=99.0), row(symbol="OK")]
        assert [c["symbol"] for c in bs.filter_actionable_buy_suggestions(s)] == ["OK"]

    def test_rows_without_a_symbol_are_skipped(self, bs):
        s = [row(symbol=""), row(symbol=None), {"price": 100, "decision": "BUY", "conviction": 70}, row(symbol="OK")]
        assert [c["symbol"] for c in bs.filter_actionable_buy_suggestions(s)] == ["OK"]

    def test_duplicate_symbols_collapse_to_one_card(self, bs):
        s = [row(symbol="dup"), row(symbol="DUP.NS"), row(symbol="Dup.BO"), row(symbol="OTHER")]
        out = bs.filter_actionable_buy_suggestions(s)
        assert sorted(c["symbol"] for c in out) == ["DUP", "OTHER"]

    def test_a_higher_min_conviction_raises_the_bar(self, bs):
        s = rows(("A", 65), ("B", 75), ("C", 85))
        out = bs.filter_actionable_buy_suggestions(s, min_conviction=80)
        assert [c["symbol"] for c in out] == ["C"]

    def test_min_conviction_is_applied_before_the_card_is_built(self, bs, monkeypatch):
        built = []
        real = bs.build_suggestion
        monkeypatch.setattr(bs, "build_suggestion", lambda s, *a, **k: built.append(s["symbol"]) or real(s, *a, **k))
        bs.filter_actionable_buy_suggestions(rows(("A", 65), ("B", 85)), min_conviction=80)
        assert built == ["B"]

    def test_ties_keep_a_stable_order(self, bs):
        out = bs.filter_actionable_buy_suggestions(rows(("A", 70), ("B", 70), ("C", 70)))
        assert [c["symbol"] for c in out] == ["A", "B", "C"]

    def test_conviction_used_for_sorting_is_the_sector_adjusted_one(self, bs):
        s = [row(symbol="PLAIN", conviction=72), row(symbol="BANK", conviction=70, sector="Banking")]
        out = bs.filter_actionable_buy_suggestions(s)
        assert [c["symbol"] for c in out] == ["BANK", "PLAIN"]      # 73 vs 72


# ── suggestions_from_scan_payload ─────────────────────────────────────────────

GOOD = [row(symbol="A", conviction=80), row(symbol="B", conviction=70)]


class TestSuggestionsFromPayload:
    def test_response_shape(self, bs):
        out = bs.suggestions_from_scan_payload({"stocks": GOOD})
        assert set(out) == {"ok", "count", "suggestions", "scanned_input", "min_conviction", "target_count",
                            "market_context", "message"}
        assert out["ok"] is True and out["count"] == 2 and out["scanned_input"] == 2
        assert [c["symbol"] for c in out["suggestions"]] == ["A", "B"]
        assert out["min_conviction"] == 62 and out["target_count"] == 4
        assert out["market_context"] == bs._market_context_note()
        assert out["message"] is None

    @pytest.mark.parametrize("key", ["stocks", "all_results", "results", "recommendations", "rows"])
    def test_every_recognised_list_key(self, bs, key):
        out = bs.suggestions_from_scan_payload({key: GOOD})
        assert out["count"] == 2 and out["scanned_input"] == 2

    def test_key_priority(self, bs):
        out = bs.suggestions_from_scan_payload({"stocks": [row(symbol="FIRST")], "rows": [row(symbol="LAST")]})
        assert [c["symbol"] for c in out["suggestions"]] == ["FIRST"]

    def test_empty_list_falls_through_to_the_next_key(self, bs):
        out = bs.suggestions_from_scan_payload({"stocks": [], "results": GOOD})
        assert out["count"] == 2

    def test_non_list_value_is_ignored(self, bs):
        out = bs.suggestions_from_scan_payload({"stocks": {"a": 1}, "rows": GOOD})
        assert out["count"] == 2

    def test_data_list_is_the_last_resort(self, bs):
        assert bs.suggestions_from_scan_payload({"data": GOOD})["count"] == 2

    def test_data_is_ignored_when_a_named_key_has_rows(self, bs):
        out = bs.suggestions_from_scan_payload({"stocks": [row(symbol="NAMED")], "data": [row(symbol="DATA")]})
        assert [c["symbol"] for c in out["suggestions"]] == ["NAMED"]

    @pytest.mark.parametrize("data", [{"a": 1}, "x", None, 5])
    def test_non_list_data_is_ignored(self, bs, data):
        out = bs.suggestions_from_scan_payload({"data": data})
        assert out["count"] == 0 and out["scanned_input"] == 0

    def test_bare_list_payload(self, bs):
        out = bs.suggestions_from_scan_payload(GOOD)
        assert out["count"] == 2 and out["scanned_input"] == 2

    @pytest.mark.parametrize("payload", [None, "x", 5, {}, []])
    def test_unusable_payloads_give_an_empty_ok_response(self, bs, payload):
        out = bs.suggestions_from_scan_payload(payload)
        assert out["ok"] is True and out["count"] == 0 and out["suggestions"] == [] and out["scanned_input"] == 0
        assert out["message"] == "No setups meet conviction / R:R / decision criteria"

    def test_no_qualifying_rows_gives_the_message(self, bs):
        out = bs.suggestions_from_scan_payload({"stocks": [row(decision="SELL")]})
        assert out["count"] == 0 and out["scanned_input"] == 1
        assert out["message"] == "No setups meet conviction / R:R / decision criteria"

    def test_target_count_from_the_payload(self, bs):
        s = rows(*[(f"S{i}", 65 + i) for i in range(8)])
        out = bs.suggestions_from_scan_payload({"stocks": s, "target_count": 2})
        assert out["count"] == 2 and out["target_count"] == 2

    def test_target_count_string_is_parsed(self, bs):
        assert bs.suggestions_from_scan_payload({"stocks": GOOD, "target_count": "1"})["target_count"] == 1

    @pytest.mark.parametrize("bad", ["many", [], {}])
    def test_bad_payload_target_count_uses_the_argument(self, bs, bad):
        out = bs.suggestions_from_scan_payload({"stocks": GOOD, "target_count": bad}, target_count=3)
        assert out["target_count"] == 3

    def test_none_payload_target_count_uses_the_argument(self, bs):
        assert bs.suggestions_from_scan_payload({"stocks": GOOD, "target_count": None}, target_count=3)["target_count"] == 3

    def test_target_count_argument_for_a_list_payload(self, bs):
        assert bs.suggestions_from_scan_payload(GOOD, target_count=1)["count"] == 1

    def test_min_conviction_from_the_payload_raises_the_bar(self, bs):
        out = bs.suggestions_from_scan_payload({"stocks": GOOD, "min_conviction": 75})
        assert out["min_conviction"] == 75.0 and [c["symbol"] for c in out["suggestions"]] == ["A"]

    def test_min_conviction_string_is_parsed(self, bs):
        assert bs.suggestions_from_scan_payload({"stocks": GOOD, "min_conviction": "75.5"})["min_conviction"] == 75.5

    @pytest.mark.parametrize("bad", ["high", [], {}])
    def test_bad_min_conviction_uses_the_module_default(self, bs, bad):
        assert bs.suggestions_from_scan_payload({"stocks": GOOD, "min_conviction": bad})["min_conviction"] == 62

    def test_none_min_conviction_uses_the_module_default(self, bs):
        assert bs.suggestions_from_scan_payload({"stocks": GOOD, "min_conviction": None})["min_conviction"] == 62

    def test_module_default_comes_from_the_env(self, load):
        m = load(SNIPER_MIN_CONVICTION="75")
        out = m.suggestions_from_scan_payload({"stocks": GOOD})
        assert out["min_conviction"] == 75 and [c["symbol"] for c in out["suggestions"]] == ["A"]

    def test_scanned_input_counts_rows_not_suggestions(self, bs):
        out = bs.suggestions_from_scan_payload({"stocks": GOOD + [row(decision="SELL"), None, "x"]})
        assert out["scanned_input"] == 5 and out["count"] == 2


# ── drift guards against the collaborators ───────────────────────────────────

class TestDrift:
    def test_main_imports_names_that_exist(self):
        with open(os.path.join(_SERVICE, "main.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        wanted = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "buy_sniper":
                wanted |= {a.name for a in node.names}
        assert "suggestions_from_scan_payload" in wanted
        with open(_MOD_PATH, encoding="utf-8") as fh:
            defined = {n.name for n in ast.parse(fh.read()).body if isinstance(n, ast.FunctionDef)}
        assert wanted <= defined

    def test_card_has_every_required_field_of_the_frontend_interface(self, bs):
        path = os.path.join(_REPO, "frontend", "src", "components", "BuySniperModal.tsx")
        if not os.path.isfile(path):
            pytest.skip("frontend not present")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        block = re.search(r"export interface BuySuggestion \{(.*?)\n\}", src, re.S).group(1)
        fields = re.findall(r"^\s+(\w+)(\?)?:", block, re.M)
        assert fields, "could not parse the BuySuggestion interface"
        card = bs.build_suggestion(row(technical_score=80, fundamental_score=65))
        for name, optional in fields:
            assert name in card, f"card is missing frontend field {name}"
        required = {n for n, opt in fields if not opt}
        assert required <= set(card)

    def test_endpoint_defaults_match_the_module_defaults(self, bs):
        # main.py's find-buys route passes only the raw payload, so the module's own defaults apply
        out = bs.suggestions_from_scan_payload({"stocks": GOOD})
        assert out["target_count"] == bs.DEFAULT_TARGET_COUNT and out["min_conviction"] == bs.MIN_CONVICTION
