"""tests/test_instant_scanner.py — coverage for api-gateway/instant_scanner.py

The zero-HTTP "instant score" composite behind the lite market scan: feed + live-tick feature bag,
technical / fundamental sub-scores, decision bands, the AVOID card, and the async single-stock
worker that optionally asks the decision engine first.

No network. `price_resolver` is the real module (it is already 100%-covered and pure); the
exception fallbacks are exercised by swapping it for a broken fake in sys.modules. The async worker
runs under asyncio.run() with a fake HTTP client (no pytest-asyncio needed). Every test loads a FRESH
copy of the module with MAX_STOCK_PRICE / VALUE_BUY_THRESHOLD cleared, so a VM that exports either
can never change an assertion here.

A separate class guards against drift: the names main.py imports exist, and the price_resolver
calls this module makes still match the real signatures.

Run from services/api-gateway:
    python3 -m pytest tests/test_instant_scanner.py -v
"""
from __future__ import annotations

import ast
import asyncio
import importlib.util
import inspect
import logging
import os
import sys
import types

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = os.path.dirname(_HERE)
_MOD_PATH = os.path.join(_SERVICE, "instant_scanner.py")

_ENV_KEYS = ("MAX_STOCK_PRICE", "VALUE_BUY_THRESHOLD")


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
        spec = importlib.util.spec_from_file_location(f"instant_scanner_under_test_{self.n}", _MOD_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


@pytest.fixture
def load(monkeypatch):
    return Loader(monkeypatch)


@pytest.fixture
def ins(load):
    return load()


@pytest.fixture
def broken_resolver(monkeypatch):
    """price_resolver whose every entry point raises — forces every fallback branch."""
    fake = types.ModuleType("price_resolver")

    def boom(*a, **k):
        raise RuntimeError("resolver down")

    fake.extract_safe_price = boom
    fake.resolve_display_price = boom
    fake.apply_price_aliases = boom
    monkeypatch.setitem(sys.modules, "price_resolver", fake)
    return fake


@pytest.fixture
def no_resolver(monkeypatch):
    monkeypatch.setitem(sys.modules, "price_resolver", None)


# ── constants / _price_capped ─────────────────────────────────────────────────

class TestConstants:
    def test_defaults(self, ins):
        assert ins.MAX_STOCK_PRICE == 0.0
        assert ins.VALUE_BUY_THRESHOLD == 2000.0

    def test_env_overrides(self, load):
        m = load(MAX_STOCK_PRICE="750", VALUE_BUY_THRESHOLD="900")
        assert (m.MAX_STOCK_PRICE, m.VALUE_BUY_THRESHOLD) == (750.0, 900.0)

    def test_blank_values_use_the_defaults(self, load):
        m = load(MAX_STOCK_PRICE="", VALUE_BUY_THRESHOLD="")
        assert (m.MAX_STOCK_PRICE, m.VALUE_BUY_THRESHOLD) == (0.0, 2000.0)

    def test_static_indicator_defaults(self, ins):
        assert ins.DEFAULT_STATIC_INDICATORS == {"rsi": 52.0, "pe_ratio": 22.0, "roce": 15.0, "macd_hist": 0.05,
                                                 "ema20": None, "market_cap": "MID"}


class TestPriceCapped:
    @pytest.mark.parametrize("price", [0, 1, 5000, 1_000_000])
    def test_no_cap_configured_never_caps(self, ins, price):
        assert ins._price_capped(price) is False

    def test_cap_applies_strictly_above(self, load):
        m = load(MAX_STOCK_PRICE="500")
        assert m._price_capped(500.0) is False
        assert m._price_capped(500.01) is True
        assert m._price_capped(10) is False


# ── _avoid_payload ────────────────────────────────────────────────────────────

class TestAvoidPayload:
    def test_no_price_card(self, ins):
        out = ins._avoid_payload("tcs.ns")
        assert out["symbol"] == "TCS"
        assert out["price"] == 0.0 and out["cmp"] == 0.0
        for k in ("close", "current_price", "ltp", "last_price"):
            assert out[k] is None
        assert out["action"] == out["decision"] == out["status"] == "AVOID"
        assert out["conviction_score"] == 0.0 and out["conviction"] == 0.0 and out["combined_score"] == 0
        assert out["technical_score"] == 0.0 and out["fundamental_score"] == 0.0
        assert out["confidence"] == "Low" and out["change_pct"] == 0.0
        assert out["skipped_high_price"] is False and out["data_insufficient"] is True
        assert out["max_stock_price"] == 0.0
        assert out["lite_fastpath"] is True and out["instant_scanner"] is True and out["from_data_feed"] is False
        assert out["verdict"] == "PRICE > 5000 FILTER / NO DATA"
        assert out["natural_language_summary"] == "TCS: AVOID — PRICE > 5000 FILTER / NO DATA"
        assert out["reasons"] == {"lite": [out["verdict"]], "technical": [out["verdict"]],
                                  "fundamental": [out["verdict"]]}
        assert "prev_close" not in out              # aliases are only stamped when there is a price

    def test_custom_reason(self, ins):
        out = ins._avoid_payload("A", 0, "because")
        assert out["verdict"] == "because" and out["natural_language_summary"] == "A: AVOID — because"

    @pytest.mark.parametrize("raw", ["abc", " abc.bo ", "ABC.NS", "Abc"])
    def test_symbol_normalisation(self, ins, raw):
        assert ins._avoid_payload(raw)["symbol"] == "ABC"

    @pytest.mark.parametrize("raw", [None, ""])
    def test_empty_symbol(self, ins, raw):
        assert ins._avoid_payload(raw)["symbol"] == ""

    def test_with_a_price_every_alias_is_stamped(self, ins):
        out = ins._avoid_payload("A", 123.456, "capped")
        assert out["price"] == 123.46 and out["data_insufficient"] is False
        for k in ("close", "price", "cmp", "current_price", "ltp", "last_price", "prev_close"):
            assert out[k] == 123.46
        assert out["decision"] == "AVOID"

    @pytest.mark.parametrize("price", [-5, -0.01, None, 0, ""])
    def test_negative_or_missing_price_is_zero(self, ins, price):
        out = ins._avoid_payload("A", price)
        assert out["price"] == 0.0 and out["close"] is None and out["data_insufficient"] is True

    def test_skipped_high_price_follows_the_cap(self, load):
        m = load(MAX_STOCK_PRICE="500")
        hi, lo = m._avoid_payload("A", 600.0), m._avoid_payload("A", 400.0)
        assert hi["skipped_high_price"] is True and lo["skipped_high_price"] is False
        assert hi["max_stock_price"] == 500.0

    def test_alias_failure_is_swallowed(self, ins, broken_resolver):
        out = ins._avoid_payload("A", 50.0, "r")
        assert out["price"] == 50.0 and out["close"] == 50.0 and out["decision"] == "AVOID"
        assert "prev_close" not in out              # the resolver never got to stamp it

    def test_missing_resolver_is_swallowed(self, ins, no_resolver):
        assert ins._avoid_payload("A", 50.0)["decision"] == "AVOID"


# ── _f / _metrics ─────────────────────────────────────────────────────────────

class TestF:
    @pytest.mark.parametrize("val, expected", [(5, 5.0), ("2.5", 2.5), (0, 0.0), (-3, -3.0)])
    def test_numbers(self, ins, val, expected):
        assert ins._f(val) == expected

    @pytest.mark.parametrize("val", [None, ""])
    def test_none_and_empty_use_the_default(self, ins, val):
        assert ins._f(val) == 0.0 and ins._f(val, 9.0) == 9.0

    @pytest.mark.parametrize("val", ["abc", [], {}, object()])
    def test_garbage_uses_the_default(self, ins, val):
        assert ins._f(val, 7.0) == 7.0


class TestMetrics:
    def test_dict_metrics(self, ins):
        assert ins._metrics({"metrics": {"pe": 1}}) == {"pe": 1}

    @pytest.mark.parametrize("feed", [{}, {"metrics": None}, {"metrics": "x"}, {"metrics": [1]}, {"metrics": 5}])
    def test_anything_else_is_empty(self, ins, feed):
        assert ins._metrics(feed) == {}


# ── _extract_price ────────────────────────────────────────────────────────────

class TestExtractPrice:
    """_extract_price(sym, feed, tick): the live tick wins over the feed."""

    def test_tick_wins(self, ins):
        assert ins._extract_price("A", {"price": 40}, {"price": 50}) == 50.0

    def test_feed_price(self, ins):
        assert ins._extract_price("A", {"close": 40.0}, None) == 40.0

    def test_nested_feed_shapes(self, ins):
        assert ins._extract_price("A", {"metrics": {"price": 33.0}}, None) == 33.0

    def test_nothing_is_zero(self, ins):
        assert ins._extract_price("A", {}, None) == 0.0
        assert ins._extract_price("A", {"price": 0, "close": None}, {"price": -1}) == 0.0

    def test_result_is_rounded_by_the_resolver(self, ins):
        assert ins._extract_price("A", {"price": 12.345}, {}) == 12.35

    def test_fallback_walks_tick_then_feed_in_key_order(self, ins, broken_resolver):
        assert ins._extract_price("A", {"price": 10}, {"price": 50, "cmp": 60}) == 50.0
        assert ins._extract_price("A", {"close": 10}, {"cmp": 60, "ltp": 70}) == 60.0
        assert ins._extract_price("A", {"cmp": 11, "ltp": 12}, {}) == 11.0

    @pytest.mark.parametrize("key", ["price", "cmp", "last_price", "ltp", "close", "current_price", "prev_close"])
    def test_fallback_reads_every_key(self, ins, broken_resolver, key):
        assert ins._extract_price("A", {}, {key: 25.5}) == 25.5
        assert ins._extract_price("A", {key: 25.5}, None) == 25.5

    def test_fallback_skips_unusable_values(self, ins, broken_resolver):
        assert ins._extract_price("A", {}, {"price": 0, "cmp": "x", "ltp": -1, "close": 9}) == 9.0

    def test_fallback_with_nothing_is_zero(self, ins, broken_resolver):
        assert ins._extract_price("A", {}, {}) == 0.0
        assert ins._extract_price("A", {}, None) == 0.0

    def test_missing_resolver_uses_the_fallback(self, ins, no_resolver):
        assert ins._extract_price("A", {"price": 3}, {"cmp": 7}) == 7.0


# Group 49: a None / non-dict feed or tick used to raise AttributeError in the fallback walk (and in the two
# score functions and _metrics). Real callers always pass a dict, so this was latent, but the fallback only
# runs when the price resolver is already failing - the worst moment to crash instead of returning 0.
BAD_SHAPES = [None, "x", [1, 2], 5, 0, ()]


class TestNonDictInputsDoNotCrash:
    @pytest.mark.parametrize("bad", BAD_SHAPES)
    def test_as_dict(self, ins, bad):
        assert ins._as_dict(bad) == {}

    def test_as_dict_keeps_a_dict_itself(self, ins):
        d = {"a": 1}
        assert ins._as_dict(d) is d

    @pytest.mark.parametrize("bad", BAD_SHAPES)
    def test_metrics(self, ins, bad):
        assert ins._metrics(bad) == {}

    @pytest.mark.parametrize("bad", BAD_SHAPES)
    def test_extract_price_fallback_with_a_bad_feed(self, ins, broken_resolver, bad):
        assert ins._extract_price("A", bad, None) == 0.0
        assert ins._extract_price("A", bad, {}) == 0.0

    @pytest.mark.parametrize("bad", BAD_SHAPES)
    def test_extract_price_fallback_with_a_bad_tick(self, ins, broken_resolver, bad):
        assert ins._extract_price("A", {}, bad) == 0.0

    @pytest.mark.parametrize("bad", BAD_SHAPES)
    def test_a_bad_feed_does_not_hide_a_good_tick(self, ins, broken_resolver, bad):
        assert ins._extract_price("A", bad, {"price": 25.5}) == 25.5

    @pytest.mark.parametrize("bad", BAD_SHAPES)
    def test_a_bad_tick_does_not_hide_a_good_feed(self, ins, broken_resolver, bad):
        assert ins._extract_price("A", {"close": 40.0}, bad) == 40.0

    @pytest.mark.parametrize("bad", BAD_SHAPES)
    def test_missing_resolver_with_a_bad_feed(self, ins, no_resolver, bad):
        assert ins._extract_price("A", bad, None) == 0.0
        assert ins._extract_price("A", bad, {"cmp": 7}) == 7.0

    @pytest.mark.parametrize("bad", BAD_SHAPES)
    def test_compute_technical_score_with_a_bad_feed_equals_the_empty_feed(self, ins, bad):
        assert ins.compute_technical_score(bad, 100.0, 100.0) == ins.compute_technical_score({}, 100.0, 100.0)

    @pytest.mark.parametrize("bad", BAD_SHAPES)
    def test_compute_fundamental_score_with_a_bad_feed_equals_the_empty_feed(self, ins, bad):
        assert ins.compute_fundamental_score(bad) == ins.compute_fundamental_score({})
        assert ins.compute_fundamental_score(bad) == 48 + 22 + 14  # the documented default baseline

    @pytest.mark.parametrize("bad", BAD_SHAPES)
    def test_compute_instant_scores_with_a_bad_feed_and_tick(self, ins, bad):
        out = ins.compute_instant_scores("TCS", bad, bad)
        assert out["decision"] == "AVOID" and out["verdict"] == "NO DATA / MISSING FROM FEED"
        # ...and a good tick alone still gives a priced, non-crashing card
        priced = ins.compute_instant_scores("TCS", bad, {"price": 120.0})
        assert priced["price"] == 120.0 and priced["from_data_feed"] is False

    @pytest.mark.parametrize("bad", BAD_SHAPES)
    def test_process_single_stock_with_a_bad_feed_row(self, ins, bad):
        out = asyncio.run(ins.process_single_stock("TCS", {"TCS": bad}, asyncio.Semaphore(1), None, "", ""))
        assert out["decision"] == "AVOID" and out["data_insufficient"] is True


# ── resolve_stock_features ────────────────────────────────────────────────────

class TestResolveStockFeatures:
    def test_empty_inputs_use_the_documented_defaults(self, ins):
        f = ins.resolve_stock_features("A", {}, None)
        assert f["symbol"] == "A" and f["price"] == 0.0 and f["prev_close"] == 0.0
        assert (f["rsi"], f["pe_ratio"], f["roce"], f["macd_hist"]) == (52.0, 22.0, 15.0, 0.05)
        assert f["ema20"] is None and f["sentiment_score"] == 0.0
        assert f["has_explicit_tech"] is False and f["has_explicit_fund"] is False
        assert f["stored_tech"] is None and f["stored_fund"] is None
        assert f["metrics"] == {} and f["raw_feed"] == {}
        assert f["sector"] is None and f["industry"] is None and f["valuation"] is None
        assert f["news_score"] is None and f["event_risk"] is None

    @pytest.mark.parametrize("feed, live", [("x", "y"), (None, None), ([1], [2]), (5, 5)])
    def test_non_dict_inputs_are_treated_as_empty(self, ins, feed, live):
        f = ins.resolve_stock_features("A", feed, live)
        assert f["price"] == 0.0 and f["raw_feed"] == {}

    def test_price_from_the_live_quote(self, ins):
        assert ins.resolve_stock_features("A", {"close": 40}, {"price": 50})["price"] == 50.0

    def test_price_from_the_feed(self, ins):
        assert ins.resolve_stock_features("A", {"close": 40}, None)["price"] == 40.0

    @pytest.mark.parametrize("feed, metrics_pc, live, expected", [
        ({"prev_close": 90, "close": 80}, 70, {"prev_close": 60}, 90.0),
        ({"close": 80}, 70, {"prev_close": 60}, 70.0),
        ({"close": 80}, None, {"prev_close": 60}, 60.0),
        ({"close": 80}, None, {}, 80.0),
    ])
    def test_prev_close_priority(self, ins, feed, metrics_pc, live, expected):
        feed = dict(feed)
        if metrics_pc is not None:
            feed["metrics"] = {"prev_close": metrics_pc}
        live = dict(live, price=100)
        assert ins.resolve_stock_features("A", feed, live)["prev_close"] == expected

    def test_prev_close_falls_back_to_price(self, ins):
        assert ins.resolve_stock_features("A", {}, {"price": 100})["prev_close"] == 100.0

    def test_no_price_and_no_prev_close_stays_zero(self, ins):
        assert ins.resolve_stock_features("A", {"rsi": 50}, {})["prev_close"] == 0.0

    def test_indicator_values_from_the_top_level(self, ins):
        f = ins.resolve_stock_features("A", {"rsi": 61, "pe_ratio": 18, "roce": 21, "macd_hist": 0.4}, {"price": 10})
        assert (f["rsi"], f["pe_ratio"], f["roce"], f["macd_hist"]) == (61.0, 18.0, 21.0, 0.4)

    def test_indicator_values_from_metrics(self, ins):
        feed = {"metrics": {"rsi": 33, "pe_ratio": 9, "roce": 12, "macd_hist": -0.2}}
        f = ins.resolve_stock_features("A", feed, {"price": 10})
        assert (f["rsi"], f["pe_ratio"], f["roce"], f["macd_hist"]) == (33.0, 9.0, 12.0, -0.2)

    def test_top_level_beats_metrics(self, ins):
        f = ins.resolve_stock_features("A", {"rsi": 61, "metrics": {"rsi": 33}}, {"price": 10})
        assert f["rsi"] == 61.0

    def test_pe_aliases(self, ins):
        assert ins.resolve_stock_features("A", {"pe": 14}, {"price": 10})["pe_ratio"] == 14.0
        assert ins.resolve_stock_features("A", {"metrics": {"pe": 15}}, {"price": 10})["pe_ratio"] == 15.0
        assert ins.resolve_stock_features("A", {"pe_ratio": 13, "pe": 14}, {"price": 10})["pe_ratio"] == 13.0

    def test_macd_alias(self, ins):
        assert ins.resolve_stock_features("A", {"macd": 0.7}, {"price": 10})["macd_hist"] == 0.7
        assert ins.resolve_stock_features("A", {"macd_hist": 0.1, "macd": 0.7}, {"price": 10})["macd_hist"] == 0.1

    def test_unparseable_indicators_fall_back_to_defaults(self, ins):
        f = ins.resolve_stock_features("A", {"rsi": "x", "pe_ratio": [], "roce": "", "macd_hist": "n/a"}, {"price": 10})
        assert (f["rsi"], f["pe_ratio"], f["roce"], f["macd_hist"]) == (52.0, 22.0, 15.0, 0.05)

    def test_ema20_sources(self, ins):
        assert ins.resolve_stock_features("A", {"ema20": 95}, {"price": 100})["ema20"] == 95.0
        assert ins.resolve_stock_features("A", {"metrics": {"ema20": 96}}, {"price": 100})["ema20"] == 96.0
        assert ins.resolve_stock_features("A", {"ema_20": 97}, {"price": 100})["ema20"] == 97.0

    def test_ema20_defaults_to_prev_close_then_price(self, ins):
        assert ins.resolve_stock_features("A", {"prev_close": 90}, {"price": 100})["ema20"] == 90.0
        assert ins.resolve_stock_features("A", {}, {"price": 100})["ema20"] == 100.0

    @pytest.mark.parametrize("raw", [0, -5, "0"])
    def test_non_positive_ema20_is_replaced_by_the_price(self, ins, raw):
        assert ins.resolve_stock_features("A", {"ema20": raw}, {"price": 100})["ema20"] == 100.0

    def test_non_positive_ema20_without_a_price_is_none(self, ins):
        assert ins.resolve_stock_features("A", {"ema20": 0}, {})["ema20"] is None

    def test_explicit_flags(self, ins):
        f = lambda feed: ins.resolve_stock_features("A", feed, {})          # noqa: E731
        assert f({"technical_score": 60})["has_explicit_tech"] is True
        assert f({"technical_score": 0})["has_explicit_tech"] is True
        assert f({"technical_score": 60})["has_explicit_fund"] is False
        for feed in ({"fundamental_score": 50}, {"metrics": {}}, {"pe_ratio": 20}, {"roce": 10}):
            assert f(feed)["has_explicit_fund"] is True, feed

    def test_pass_through_fields(self, ins):
        feed = {"technical_score": 61, "fundamental_score": 62, "sector": "Banking", "industry": "PSU Bank",
                "valuation": "cheap", "news_score": 7, "event_risk": "low", "sentiment_score": "55",
                "metrics": {"pe_ratio": 9}}
        f = ins.resolve_stock_features("SBIN", feed, {"price": 10})
        assert (f["stored_tech"], f["stored_fund"]) == (61, 62)
        assert (f["sector"], f["industry"], f["valuation"]) == ("Banking", "PSU Bank", "cheap")
        assert (f["news_score"], f["event_risk"], f["sentiment_score"]) == (7, "low", 55.0)
        assert f["metrics"] == {"pe_ratio": 9} and f["raw_feed"] is feed


# ── compute_technical_score ───────────────────────────────────────────────────

def tech(ins, feed=None, price=100.0, prev=100.0):
    return ins.compute_technical_score(feed if feed is not None else {}, price, prev)


class TestComputeTechnicalScore:
    def test_neutral_baseline(self, ins):
        # 48 + 12 (price >= ema20) + 14 (rsi 52) + 6 (macd 0.05 defaults) = 80
        assert tech(ins, {"ema20": 100}) == 80

    @pytest.mark.parametrize("ema, delta", [(98.0, 18), (100.0, 12), (99.5, 12), (100.5, -4), (98.0 * 1.06, -12),
                                            (110.0, -12), (103.0, -4)])
    def test_ema_bands(self, ins, ema, delta):
        assert tech(ins, {"ema20": ema}) == 48 + delta + 14 + 6

    def test_ema_band_edges_are_exact(self, ins):
        # price exactly 1% above EMA20 is the top band; exactly 3% below is only the mild-drop band
        assert tech(ins, {"ema20": 100.0}, price=101.0, prev=101.0) == 48 + 18 + 14 + 6
        assert tech(ins, {"ema20": 100.0}, price=97.0, prev=97.0) == 48 - 4 + 14 + 6

    @pytest.mark.parametrize("rsi, delta", [
        (45, 14), (52, 14), (65, 14), (44.9, 6), (35, 6), (65.1, 6), (72, 6), (34.9, 0), (30, 0), (33, 0),
        (72.1, 0), (75, 0), (29.9, 10), (10, 10), (75.1, -10), (90, -10),
    ])
    def test_rsi_bands(self, ins, rsi, delta):
        assert tech(ins, {"ema20": 100, "rsi": rsi}) == 48 + 12 + 6 + delta

    @pytest.mark.parametrize("macd, delta", [(0.06, 12), (0.5, 12), (0.05, 6), (0.01, 6), (0.0, 0), (-0.05, 0),
                                             (-0.06, -8), (-2, -8)])
    def test_macd_bands(self, ins, macd, delta):
        assert tech(ins, {"ema20": 100, "macd_hist": macd}) == 48 + 12 + 14 + delta

    @pytest.mark.parametrize("price, delta", [(102, 10), (100.5, 5), (100.3, 0), (99.5, 0), (99, -6), (98.5, -6),
                                              (98, -12), (97, -12), (90, -12)])
    def test_price_change_bands(self, ins, price, delta):
        assert tech(ins, {"ema20": price}, price=price, prev=100.0) == 48 + 12 + 14 + 6 + delta

    def test_no_price_skips_the_price_based_parts(self, ins):
        assert tech(ins, {}, price=0, prev=0) == 48 + 14 + 6
        assert tech(ins, {}, price=0, prev=100) == 48 + 14 + 6

    def test_the_score_is_capped_at_95(self, ins):
        assert tech(ins, {"ema20": 90, "rsi": 55, "macd_hist": 1.0}, price=102, prev=100) == 95     # would be 102

    def test_the_score_is_floored_at_10(self, ins):
        assert tech(ins, {"ema20": 200, "rsi": 90, "macd_hist": -1}, price=97, prev=100) == 10      # would be 6

    def test_result_is_an_int(self, ins):
        assert isinstance(tech(ins, {"ema20": 100}), int)

    def test_metrics_indicators_are_read(self, ins):
        feed = {"metrics": {"ema20": 100, "rsi": 20, "macd_hist": 0.0}}
        assert tech(ins, feed) == 48 + 12 + 10 + 0

    # ── stored technical score ───────────────────────────────────────────────
    def test_stored_score_gets_both_bonuses(self, ins):
        assert tech(ins, {"technical_score": 70, "ema20": 90}, price=100, prev=100) == 78

    def test_stored_score_price_at_ema_gets_only_the_momentum_bonus(self, ins):
        assert tech(ins, {"technical_score": 70, "ema20": 100}, price=100, prev=100) == 73

    def test_stored_score_below_prev_close_and_ema_gets_no_bonus(self, ins):
        assert tech(ins, {"technical_score": 70, "ema20": 105}, price=100, prev=101) == 70

    def test_stored_score_above_ema_but_below_prev_close(self, ins):
        assert tech(ins, {"technical_score": 70, "ema20": 90}, price=100, prev=101) == 75

    def test_stored_score_without_a_price_gets_no_bonus(self, ins):
        assert tech(ins, {"technical_score": 70, "ema20": 90}, price=0, prev=100) == 70

    def test_stored_score_without_prev_close_gets_no_momentum_bonus(self, ins):
        assert tech(ins, {"technical_score": 70, "ema20": 100}, price=100, prev=0) == 70

    @pytest.mark.parametrize("stored, expected", [(70, 78), ("70.9", 78), (70.9, 78), (87, 95), (95, 95), (100, 95),
                                                  (1, 9)])
    def test_stored_score_parsing_and_upper_clamp(self, ins, stored, expected):
        assert tech(ins, {"technical_score": stored, "ema20": 90}, price=100, prev=100) == expected

    def test_stored_score_lower_clamp(self, ins):
        assert tech(ins, {"technical_score": 3, "ema20": 200}, price=100, prev=101) == 5

    @pytest.mark.parametrize("stored", [0, -5, 101, 250, "abc", "", [], {}, float("nan")])
    def test_unusable_stored_score_falls_back_to_the_computed_one(self, ins, stored):
        assert tech(ins, {"technical_score": stored, "ema20": 100}) == 80

    def test_none_stored_score_is_computed(self, ins):
        assert tech(ins, {"technical_score": None, "ema20": 100}) == 80


# ── compute_fundamental_score ─────────────────────────────────────────────────

def fund(ins, **feed):
    return ins.compute_fundamental_score(feed)


class TestComputeFundamentalScore:
    def test_default_baseline(self, ins):
        assert fund(ins) == 48 + 22 + 14                        # pe 22 -> +22, roce 15 -> +14

    @pytest.mark.parametrize("pe, delta", [(8, 22), (15, 22), (28, 22), (28.1, 10), (40, 10), (40.1, 0), (50, 0),
                                           (50.1, -10), (120, -10), (7.9, 14), (0.1, 14), (0, 0), (-5, 0)])
    def test_pe_bands(self, ins, pe, delta):
        assert fund(ins, pe_ratio=pe, roce=9) == 48 + delta

    @pytest.mark.parametrize("roce, delta", [(18, 20), (30, 20), (17.9, 14), (14, 14), (13.9, 6), (10, 6),
                                             (9.9, 0), (8, 0), (7.9, -6), (0.1, -6), (0, 0), (-1, 0)])
    def test_roce_bands(self, ins, roce, delta):
        assert fund(ins, pe_ratio=45, roce=roce) == 48 + delta

    @pytest.mark.parametrize("roe, delta", [(15, 8), (40, 8), (14.9, 4), (10, 4), (9.9, 0), (0, 0), (-3, 0)])
    def test_roe_bands(self, ins, roe, delta):
        assert fund(ins, pe_ratio=45, roce=9, roe=roe) == 48 + delta

    @pytest.mark.parametrize("q, delta", [(70, 8), (99, 8), (69.9, 4), (50, 4), (49.9, 0), (0, 0)])
    def test_quality_bands(self, ins, q, delta):
        assert fund(ins, pe_ratio=45, roce=9, quality_score=q) == 48 + delta

    def test_metrics_fallbacks(self, ins):
        feed = {"metrics": {"pe_ratio": 45, "roce": 9, "roe": 15, "quality_score": 70}}
        assert ins.compute_fundamental_score(feed) == 48 + 8 + 8

    def test_pe_alias_in_top_level_and_metrics(self, ins):
        assert fund(ins, pe=10, roce=9) == 48 + 22
        assert ins.compute_fundamental_score({"metrics": {"pe": 60, "roce": 9}}) == 48 - 10

    def test_top_level_beats_metrics(self, ins):
        feed = {"pe_ratio": 45, "roce": 9, "metrics": {"pe_ratio": 10, "roce": 30}}
        assert ins.compute_fundamental_score(feed) == 48

    def test_unparseable_inputs_use_the_defaults(self, ins):
        assert fund(ins, pe_ratio="x", roce="", roe="?", quality_score=[]) == 48 + 22 + 14

    def test_capped_at_95(self, ins):
        assert fund(ins, pe_ratio=20, roce=25, roe=20, quality_score=80) == 95         # would be 106

    def test_result_is_an_int(self, ins):
        assert isinstance(fund(ins), int)

    @pytest.mark.parametrize("stored, expected", [(60, 60), ("60.5", 60), (60.9, 60), (95, 95), (98, 95), (100, 95),
                                                  (3, 5), (1, 5)])
    def test_stored_score(self, ins, stored, expected):
        assert fund(ins, fundamental_score=stored) == expected

    @pytest.mark.parametrize("stored", [0, -5, 101, 999, "abc", "", [], {}, float("nan")])
    def test_unusable_stored_score_is_computed_instead(self, ins, stored):
        assert fund(ins, fundamental_score=stored) == 84

    def test_none_stored_score_is_computed(self, ins):
        assert fund(ins, fundamental_score=None) == 84


# ── derive_decision ───────────────────────────────────────────────────────────

class TestDeriveDecision:
    @pytest.mark.parametrize("combined, change, tech_, fund_, expected", [
        (85, 0.4, 82, 0, ("BUY NOW", "High")),
        (95, 5.0, 95, 95, ("BUY NOW", "High")),
        (85, 0.39, 82, 0, ("PREPARE TO BUY", "Medium")),      # first rule misses on change, mid-tier catches
        (85, 0.4, 81, 0, ("PREPARE TO BUY", "Medium")),       # ... or on tech
        (84, 0.8, 75, 0, ("BUY NOW", "High")),
        (84, 0.79, 75, 0, ("PREPARE TO BUY", "Medium")),
        (84, 0.8, 74, 0, ("PREPARE TO BUY", "Medium")),
        (79, 0.8, 75, 0, ("PREPARE TO BUY", "Medium")),       # combined 79 < 80
        (72, 0.0, 60, 70, ("PREPARE TO BUY", "Medium")),
        (72, -0.1, 60, 70, ("HOLD", "Medium")),
        (72, 0.0, 60, 69, ("HOLD", "Medium")),
        (71, 0.0, 60, 99, ("HOLD", "Medium")),
        (68, 0.3, 60, 0, ("PREPARE TO BUY", "Medium")),
        (68, 0.29, 60, 0, ("HOLD", "Medium")),
        (68, 0.3, 59, 0, ("HOLD", "Medium")),
        (67, 0.3, 60, 0, ("HOLD", "Medium")),
        (60, -2.5, 60, 60, ("AVOID", "Medium")),
        (60, -2.49, 60, 60, ("HOLD", "Medium")),
        (90, -3.0, 90, 90, ("AVOID", "Medium")),
        (44, -0.1, 30, 30, ("AVOID", "Medium")),
        (44, 0.0, 30, 30, ("HOLD", "Low")),
        (45, -0.1, 30, 30, ("HOLD", "Low")),
        (49, 0.0, 30, 30, ("HOLD", "Low")),
        (50, 0.0, 30, 30, ("HOLD", "Medium")),
        (55, 1.0, 50, 50, ("HOLD", "Medium")),
    ])
    def test_bands(self, ins, combined, change, tech_, fund_, expected):
        assert ins.derive_decision(combined, change, tech_, fund_) == expected


# ── compute_instant_scores ────────────────────────────────────────────────────

def scores(ins, symbol="TCS", feed=None, tick=None):
    return ins.compute_instant_scores(symbol, feed, tick)


class TestInstantScoresGuards:
    def test_no_price_and_no_feed_is_an_avoid_card(self, ins):
        out = scores(ins, "xyz.ns", {}, {})
        assert out["decision"] == "AVOID" and out["symbol"] == "XYZ"
        assert out["verdict"] == "NO DATA / MISSING FROM FEED" and out["data_insufficient"] is True

    @pytest.mark.parametrize("feed, tick", [(None, None), ("x", "y"), ([1], [2]), (5, 5)])
    def test_non_dict_inputs_are_treated_as_empty(self, ins, feed, tick):
        assert scores(ins, "XYZ", feed, tick)["decision"] == "AVOID"

    def test_a_feed_with_only_unrelated_keys_counts_as_no_feed(self, ins):
        out = scores(ins, "XYZ", {"sector": "Auto", "symbol": "XYZ"}, {})
        assert out["decision"] == "AVOID" and out["verdict"] == "NO DATA / MISSING FROM FEED"

    @pytest.mark.parametrize("raw, expected", [("tcs", "TCS"), (" tcs.ns ", "TCS"), ("TCS.BO", "TCS")])
    def test_symbol_normalisation(self, ins, raw, expected):
        assert scores(ins, raw, {"rsi": 50}, {"price": 100})["symbol"] == expected

    def test_symbol_falls_back_to_the_feed(self, ins):
        out = scores(ins, "", {"symbol": "tcs.ns", "rsi": 50}, {"price": 100})
        assert out["symbol"] == "TCS"
        assert scores(ins, None, {"symbol": "infy", "rsi": 50}, {"price": 100})["symbol"] == "INFY"

    def test_explicit_symbol_beats_the_feed_symbol(self, ins):
        assert scores(ins, "wipro", {"symbol": "infy", "rsi": 50}, {"price": 100})["symbol"] == "WIPRO"

    def test_price_cap_returns_the_avoid_card(self, load):
        m = load(MAX_STOCK_PRICE="500")
        out = m.compute_instant_scores("MRF", {"rsi": 50}, {"price": 600.0})
        assert out["decision"] == "AVOID" and out["skipped_high_price"] is True
        assert out["verdict"] == "PRICE > ₹500 FILTER (₹600.00)"
        assert out["price"] == 600.0 and out["max_stock_price"] == 500.0

    def test_price_at_the_cap_is_scored_normally(self, load):
        m = load(MAX_STOCK_PRICE="500")
        out = m.compute_instant_scores("X", {"rsi": 50}, {"price": 500.0})
        assert "skipped_high_price" not in out and "verdict" not in out
        assert out["price"] == 500.0 and out["status"] == "READY"

    def test_no_cap_means_expensive_stocks_are_scored(self, ins):
        out = scores(ins, "MRF", {"rsi": 50}, {"price": 150_000.0})
        assert out["price"] == 150_000.0 and out["value_buy"] is False and "skipped_high_price" not in out


class TestInstantScoresHasFeed:
    @pytest.mark.parametrize("feed", [
        {"fundamental_score": 50}, {"technical_score": 50}, {"metrics": {"pe_ratio": 10}},
        {"combined_score": 50}, {"rsi": 50}, {"prev_close": 100}, {"pe_ratio": 20}, {"roce": 12},
        {"price": 100}, {"close": 100}, {"fundamental_score": 0}, {"rsi": 0},
    ])
    def test_each_recognised_key_marks_a_real_feed(self, ins, feed):
        out = scores(ins, "X", feed, {"price": 100})
        assert bool(out["from_data_feed"]) is True
        assert out["provisional_defaults"] is False
        assert not out["reasons"]["fundamental"][0].endswith("(defaults)")

    @pytest.mark.parametrize("feed", [{}, {"metrics": {}}, {"metrics": None}, {"sector": "Auto"}, {"news_score": 3}])
    def test_otherwise_it_is_price_only(self, ins, feed):
        out = scores(ins, "X", feed, {"price": 100})
        assert bool(out["from_data_feed"]) is False
        assert out["provisional_defaults"] is True
        assert out["reasons"]["fundamental"][0].endswith("(defaults)")

    def test_none_valued_keys_do_not_count(self, ins):
        out = scores(ins, "X", {"rsi": None, "technical_score": None, "metrics": None}, {"price": 100})
        assert out["provisional_defaults"] is True


class TestInstantScoresCard:
    def test_full_card_for_a_mid_tier_setup(self, ins):
        feed = {"technical_score": 70, "fundamental_score": 60, "prev_close": 100, "sector": "IT",
                "industry": "Software", "valuation": "fair", "metrics": {"pe_ratio": 20}, "news_score": 7,
                "event_risk": "low"}
        out = ins.compute_instant_scores("tcs.ns", feed, {"price": 102})
        assert out["symbol"] == "TCS"
        assert (out["technical_score"], out["fundamental_score"], out["combined_score"], out["conviction"]) == (78, 60, 69, 69)
        assert (out["decision"], out["confidence"]) == ("PREPARE TO BUY", "Medium")
        assert out["change_pct"] == 2.0 and out["prev_close"] == 100.0
        for k in ("close", "price", "cmp", "current_price", "ltp", "last_price"):
            assert out[k] == 102.0
        assert out["target"] == 108.63 and out["stop_loss"] == 98.74
        assert out["entry_range"] == {"low": 101.49, "high": 102.82}
        assert out["holding_period"] == "3-7 Days"
        assert (out["sector"], out["industry"], out["valuation"]) == ("IT", "Software", "fair")
        assert out["fundamental_metrics"] == {"pe_ratio": 20}
        assert (out["news_score"], out["event_risk"]) == (7, "low")
        assert out["rsi"] == 52.0 and out["pe_ratio"] == 20.0 and out["roce"] == 15.0
        assert out["from_data_feed"] is True and out["data_insufficient"] is False
        assert out["provisional_defaults"] is False and out["value_buy"] is True
        assert out["lite_fastpath"] is True and out["instant_scanner"] is True and out["status"] == "READY"
        assert out["reasons"] == {
            "technical": ["Tech score 78/100 from stored indicators + price vs EMA/momentum"],
            "fundamental": ["Fund score 60/100 from stored quarterly / valuation metrics"],
            "lite": ["Instant scanner: DB data-feed + live quote (no downstream HTTP)"],
        }
        assert out["natural_language_summary"] == "TCS: instant — PREPARE TO BUY · tech 78 · fund 60 · combined 69 · Δ +2.00%"

    def test_a_strong_setup_is_buy_now(self, ins):
        out = ins.compute_instant_scores("TCS", {"technical_score": 90, "fundamental_score": 90, "prev_close": 100},
                                         {"price": 102})
        assert (out["decision"], out["confidence"]) == ("BUY NOW", "High")
        assert (out["technical_score"], out["combined_score"]) == (95, 89)

    def test_a_falling_weak_stock_is_avoid(self, ins):
        out = ins.compute_instant_scores("TCS", {"technical_score": 30, "fundamental_score": 30, "prev_close": 100},
                                         {"price": 96})
        assert out["decision"] == "AVOID" and out["change_pct"] == -4.0
        assert out["combined_score"] == 32

    def test_price_only_symbol_is_marked_provisional(self, ins):
        out = scores(ins, "XYZ", {}, {"price": 100})
        assert out["provisional_defaults"] is True and out["from_data_feed"] is False
        assert out["status"] == "READY" and out["price"] == 100.0 and out["change_pct"] == 0.0
        assert out["prev_close"] == 100.0
        assert out["reasons"]["fundamental"][0].endswith("stored quarterly / valuation metrics (defaults)")

    def test_feed_without_a_price_is_provisional_low_confidence(self, ins):
        out = scores(ins, "XYZ", {"rsi": 50}, {})
        assert out["confidence"] == "Low" and out["status"] == "READY"
        assert out["data_insufficient"] is True and out["from_data_feed"] is True
        for k in ("close", "price", "cmp", "current_price", "ltp", "last_price", "prev_close", "target",
                  "stop_loss", "entry_range"):
            assert out[k] is None
        assert out["value_buy"] is False and out["change_pct"] == 0.0

    def test_live_tick_beats_the_feed_price(self, ins):
        out = scores(ins, "X", {"price": 90, "prev_close": 90}, {"price": 99})
        assert out["price"] == 99.0 and out["change_pct"] == 10.0

    def test_feed_price_is_used_without_a_tick(self, ins):
        out = scores(ins, "X", {"close": 105, "prev_close": 100}, None)
        assert out["price"] == 105.0 and out["change_pct"] == 5.0

    def test_change_pct_is_rounded_to_two_decimals(self, ins):
        out = scores(ins, "X", {"prev_close": 3, "rsi": 50}, {"price": 4})
        assert out["change_pct"] == 33.33

    def test_prev_close_is_rounded(self, ins):
        assert scores(ins, "X", {"prev_close": 100.126, "rsi": 50}, {"price": 101})["prev_close"] == 100.13

    def test_sector_and_industry_pass_through(self, ins):
        out = scores(ins, "X", {"sector": "Auto", "industry": "Cars", "valuation": "rich", "rsi": 50}, {"price": 10})
        assert (out["sector"], out["industry"], out["valuation"]) == ("Auto", "Cars", "rich")

    def test_missing_optional_fields_are_none(self, ins):
        out = scores(ins, "X", {"rsi": 50}, {"price": 10})
        for k in ("sector", "industry", "valuation", "fundamental_metrics", "news_score", "event_risk"):
            assert out[k] is None

    def test_targets_and_entry_range(self, ins):
        out = scores(ins, "X", {"rsi": 50}, {"price": 200.0})
        assert out["target"] == 213.0 and out["stop_loss"] == 193.6
        assert out["entry_range"] == {"low": 199.0, "high": 201.6}

    @pytest.mark.parametrize("price, expected", [(2000.0, True), (2000.01, False), (10.0, True), (0.5, True)])
    def test_value_buy_tag(self, ins, price, expected):
        assert scores(ins, "X", {"rsi": 50}, {"price": price})["value_buy"] is expected

    def test_value_buy_threshold_follows_the_env(self, load):
        m = load(VALUE_BUY_THRESHOLD="100")
        assert m.compute_instant_scores("X", {"rsi": 50}, {"price": 100})["value_buy"] is True
        assert m.compute_instant_scores("X", {"rsi": 50}, {"price": 101})["value_buy"] is False

    def test_indicators_are_reported_from_the_feed(self, ins):
        out = scores(ins, "X", {"rsi": 61, "pe_ratio": 18, "roce": 21}, {"price": 10})
        assert (out["rsi"], out["pe_ratio"], out["roce"]) == (61.0, 18.0, 21.0)

    def test_inputs_are_not_mutated(self, ins):
        feed, tick = {"rsi": 50, "metrics": {"pe": 10}}, {"price": 10}
        scores(ins, "X", feed, tick)
        assert feed == {"rsi": 50, "metrics": {"pe": 10}} and tick == {"price": 10}


class TestInstantScoresPrevCloseGuard:
    def test_zero_prev_close_with_a_price_is_replaced_by_the_price(self, ins, monkeypatch):
        # resolve_stock_features already does this, so the second guard in compute_instant_scores is
        # defensive dead code today; prove it still holds if the features layer ever stops doing it.
        real = ins.resolve_stock_features

        def no_prev(symbol, feed, live):
            f = real(symbol, feed, live)
            f["prev_close"] = 0.0
            return f

        monkeypatch.setattr(ins, "resolve_stock_features", no_prev)
        out = scores(ins, "X", {"rsi": 50}, {"price": 100})
        assert out["change_pct"] == 0.0 and out["prev_close"] == 100.0


class TestInstantScoresMomentum:
    def _combined(self, ins, price):
        feed = {"technical_score": 60, "fundamental_score": 60, "prev_close": 100}
        return ins.compute_instant_scores("X", feed, {"price": price})

    def test_small_moves_add_no_tilt(self, ins):
        out = self._combined(ins, 100.2)                          # +0.2%: tech 68, no tilt
        assert (out["technical_score"], out["combined_score"]) == (68, 63)

    def test_big_up_move_tilt_is_capped_at_plus_10(self, ins):
        out = self._combined(ins, 120.0)                          # +20% -> 24 -> capped to +10
        assert (out["technical_score"], out["combined_score"]) == (68, 64)

    def test_big_down_move_tilt_is_capped_at_minus_8(self, ins):
        out = self._combined(ins, 80.0)                           # -20% -> -24 -> capped to -8
        assert (out["technical_score"], out["combined_score"]) == (60, 58)

    def test_mid_sized_move_is_scaled_by_1_2(self, ins):
        out = self._combined(ins, 105.0)                          # +5% -> +6.0
        assert (out["technical_score"], out["combined_score"]) == (68, 64)

    def test_a_falling_stock_scores_below_a_rising_one(self, ins):
        assert self._combined(ins, 95.0)["combined_score"] < self._combined(ins, 105.0)["combined_score"]


class TestInstantScoresResolverFailures:
    def test_alias_failure_is_swallowed_and_the_card_still_carries_the_price(self, ins, broken_resolver):
        out = scores(ins, "X", {"rsi": 50, "prev_close": 100}, {"price": 102})
        assert out["price"] == 102.0 and out["close"] == 102.0 and out["decision"]

    def test_missing_resolver_is_swallowed(self, ins, no_resolver):
        out = scores(ins, "X", {"rsi": 50, "prev_close": 100}, {"price": 102})
        assert out["price"] == 102.0 and out["prev_close"] == 100.0

    def test_resolver_failure_still_finds_the_price_via_the_local_fallback(self, ins, broken_resolver):
        out = scores(ins, "X", {"close": 40, "rsi": 50}, None)
        assert out["price"] == 40.0


# ── process_single_stock ──────────────────────────────────────────────────────

class FakeResp:
    def __init__(self, status=200, body=None, raises=None):
        self.status_code = status
        self._body = body
        self._raises = raises

    def json(self):
        if self._raises is not None:
            raise self._raises
        return self._body


class FakeClient:
    def __init__(self, resp=None, raises=None):
        self.resp = resp
        self.raises = raises
        self.calls = []

    async def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if self.raises is not None:
            raise self.raises
        return self.resp


class CountingSemaphore:
    def __init__(self):
        self.entered = 0
        self.exited = 0

    async def __aenter__(self):
        self.entered += 1

    async def __aexit__(self, *a):
        self.exited += 1
        return False


def run_worker(ins, symbol, feed_data, client=None, url="http://d/decision", sem=None):
    async def go():
        return await ins.process_single_stock(symbol, feed_data, sem or asyncio.Semaphore(2), client,
                                              "http://market-data", url)

    return asyncio.run(go())


FEED = {"TCS": {"price": 100.0, "rsi": 61, "pe_ratio": 18, "roce": 21, "sentiment_score": 70}}


class TestProcessSingleStockFeed:
    def test_semaphore_is_held_for_the_call(self, ins):
        sem = CountingSemaphore()
        run_worker(ins, "TCS", FEED, sem=sem)
        assert (sem.entered, sem.exited) == (1, 1)

    def test_semaphore_is_released_when_the_call_raises(self, ins, monkeypatch):
        sem = CountingSemaphore()
        monkeypatch.setattr(ins, "compute_instant_scores", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        with pytest.raises(RuntimeError):
            run_worker(ins, "TCS", FEED, sem=sem)
        assert (sem.entered, sem.exited) == (1, 1)

    def test_feed_row_is_found_by_the_clean_symbol(self, ins):
        out = run_worker(ins, "tcs.ns", FEED)
        assert out["symbol"] == "TCS" and out["price"] == 100.0

    def test_feed_row_is_found_by_the_raw_key(self, ins):
        out = run_worker(ins, "tcs.ns", {"tcs.ns": {"price": 55.0}})
        assert out["price"] == 55.0

    def test_clean_key_beats_the_raw_key(self, ins):
        out = run_worker(ins, "tcs.ns", {"TCS": {"price": 10.0}, "tcs.ns": {"price": 99.0}})
        assert out["price"] == 10.0

    @pytest.mark.parametrize("feed_data", [None, "x", [1], 5, {}, {"TCS": None}, {"TCS": "junk"}, {"TCS": [1]}])
    def test_missing_or_bad_feed_is_an_avoid_card(self, ins, feed_data):
        out = run_worker(ins, "TCS", feed_data)
        assert out["decision"] == "AVOID" and out["verdict"] == "NO DATA / MISSING FROM FEED"
        assert out["symbol"] == "TCS" and out["data_insufficient"] is True

    def test_feed_row_without_a_price_is_an_avoid_card(self, ins):
        out = run_worker(ins, "TCS", {"TCS": {"rsi": 50}})
        assert out["decision"] == "AVOID" and out["verdict"] == "NO DATA / MISSING FROM FEED"

    def test_price_cap_is_an_avoid_card(self, load):
        m = load(MAX_STOCK_PRICE="500")
        out = run_worker(m, "MRF", {"MRF": {"price": 900.0}})
        assert out["decision"] == "AVOID" and out["verdict"] == "PRICE > ₹500 FILTER (₹900.00)"
        assert out["skipped_high_price"] is True

    def test_no_cap_by_default(self, ins):
        assert run_worker(ins, "MRF", {"MRF": {"price": 90_000.0}})["price"] == 90_000.0

    def test_cached_price_falls_back_to_the_local_extractor(self, ins, monkeypatch):
        fake = types.ModuleType("price_resolver")

        def down(*a, **k):
            raise RuntimeError("down")

        fake.resolve_display_price = down
        fake.extract_safe_price = lambda *a, **k: 77.0
        fake.apply_price_aliases = lambda row, px: row
        monkeypatch.setitem(sys.modules, "price_resolver", fake)
        assert run_worker(ins, "TCS", {"TCS": {"close": 1.0}})["price"] == 77.0


class TestProcessSingleStockDecisionEngine:
    def test_payload_sent_to_the_engine(self, ins):
        client = FakeClient(FakeResp(200, {"decision": "BUY"}))
        run_worker(ins, "tcs.ns", FEED, client=client)
        (call,) = client.calls
        assert call["json"] == {"symbol": "TCS", "price": 100.0, "rsi": 61.0, "pe_ratio": 18.0, "roce": 21.0,
                                "sentiment_score": 70.0}
        assert call["timeout"] == 3.0

    def test_payload_defaults_for_missing_indicators(self, ins):
        client = FakeClient(FakeResp(200, {}))
        run_worker(ins, "TCS", {"TCS": {"price": 100.0}}, client=client)
        assert client.calls[0]["json"] == {"symbol": "TCS", "price": 100.0, "rsi": 52.0, "pe_ratio": 22.0,
                                           "roce": 15.0, "sentiment_score": 50.0}

    def test_pe_alias_is_used(self, ins):
        client = FakeClient(FakeResp(200, {}))
        run_worker(ins, "TCS", {"TCS": {"price": 100.0, "pe": 14}}, client=client)
        assert client.calls[0]["json"]["pe_ratio"] == 14.0

    @pytest.mark.parametrize("url, expected", [
        ("http://d/decision", "http://d/decision/evaluate"),
        ("http://d/decision/", "http://d/decision/evaluate"),
        ("http://d", "http://d/decision/evaluate"),
        ("http://d/", "http://d/decision/evaluate"),
        ("http://d/api", "http://d/api/decision/evaluate"),
    ])
    def test_evaluate_url(self, ins, url, expected):
        client = FakeClient(FakeResp(200, {}))
        run_worker(ins, "TCS", FEED, client=client, url=url)
        assert client.calls[0]["url"] == expected

    def test_engine_result_is_returned_with_our_price(self, ins):
        client = FakeClient(FakeResp(200, {"decision": "BUY NOW", "conviction": 88, "price": 1, "cmp": 2}))
        out = run_worker(ins, "TCS", FEED, client=client)
        assert out["decision"] == "BUY NOW" and out["conviction"] == 88
        assert out["price"] == 100.0 and out["cmp"] == 100.0
        assert out["close"] == 100.0 and out["symbol"] == "TCS" and out["instant_scanner"] is True

    def test_engine_values_win_for_the_setdefault_fields(self, ins):
        client = FakeClient(FakeResp(200, {"close": 99.5, "symbol": "TCS.NS", "instant_scanner": False}))
        out = run_worker(ins, "TCS", FEED, client=client)
        assert out["close"] == 99.5 and out["symbol"] == "TCS.NS" and out["instant_scanner"] is False
        assert out["price"] == 100.0

    @pytest.mark.parametrize("resp", [
        FakeResp(500, {"decision": "BUY"}), FakeResp(404, {}), FakeResp(204, None), FakeResp(429, {}),
        FakeResp(200, [1, 2]), FakeResp(200, "text"), FakeResp(200, None),
    ])
    def test_unusable_answers_fall_back_to_local_scoring(self, ins, resp):
        out = run_worker(ins, "TCS", FEED, client=FakeClient(resp))
        assert out["lite_fastpath"] is True and out["instant_scanner"] is True and out["price"] == 100.0
        assert "decision" in out and out["symbol"] == "TCS"

    def test_a_json_error_falls_back_to_local_scoring(self, ins):
        out = run_worker(ins, "TCS", FEED, client=FakeClient(FakeResp(200, raises=ValueError("bad json"))))
        assert out["lite_fastpath"] is True

    def test_a_request_error_is_logged_and_falls_back(self, ins, caplog):
        with caplog.at_level(logging.DEBUG, logger="instant-scanner"):
            out = run_worker(ins, "TCS", FEED, client=FakeClient(raises=RuntimeError("timeout")))
        assert out["lite_fastpath"] is True
        assert any("process_single_stock decision call TCS" in r.getMessage() and "timeout" in r.getMessage()
                   for r in caplog.records)

    def test_no_client_goes_straight_to_local_scoring(self, ins):
        out = run_worker(ins, "TCS", FEED, client=None)
        assert out["lite_fastpath"] is True and out["price"] == 100.0

    @pytest.mark.parametrize("url", ["", None])
    def test_no_decision_url_goes_straight_to_local_scoring(self, ins, url):
        client = FakeClient(FakeResp(200, {"decision": "BUY"}))
        out = run_worker(ins, "TCS", FEED, client=client, url=url)
        assert client.calls == [] and out["lite_fastpath"] is True

    def test_local_fallback_is_fed_the_cached_price_as_the_tick(self, ins):
        out = run_worker(ins, "TCS", {"TCS": {"price": 100.0, "prev_close": 95.0, "rsi": 50}}, client=None)
        assert out["price"] == 100.0 and out["prev_close"] == 95.0 and out["change_pct"] == 5.26


# ── drift guards against the collaborators ───────────────────────────────────

class TestDrift:
    def test_names_main_imports_from_this_module_exist(self):
        with open(os.path.join(_SERVICE, "main.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        wanted = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "instant_scanner":
                wanted |= {a.name for a in node.names}
        assert "compute_instant_scores" in wanted
        with open(_MOD_PATH, encoding="utf-8") as fh:
            defined = {n.name for n in ast.parse(fh.read()).body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert wanted <= defined

    def test_price_resolver_signatures_match_the_calls(self):
        import price_resolver as pr
        inspect.signature(pr.extract_safe_price).bind("A", tick={}, feed={}, decision=None)
        inspect.signature(pr.resolve_display_price).bind("A", {}, {})
        inspect.signature(pr.apply_price_aliases).bind({}, 1.0)

    def test_the_compute_signature_main_uses_is_stable(self):
        import instant_scanner
        inspect.signature(instant_scanner.compute_instant_scores).bind("SYM", {}, {})

    def test_every_price_alias_the_resolver_writes_is_on_the_card(self, ins):
        import price_resolver as pr
        card = scores(ins, "X", {"rsi": 50}, {"price": 10})
        stamped = pr.apply_price_aliases({}, 10.0)
        for key in ("close", "price", "cmp", "current_price", "ltp", "last_price"):
            assert key in stamped and key in card

    def test_decision_labels_are_the_ones_buy_sniper_understands(self, ins):
        import buy_sniper
        labels = {ins.derive_decision(c, ch, t, f)[0] for c, ch, t, f in [(90, 1, 90, 90), (72, 0, 60, 70),
                                                                        (50, 0, 50, 50), (50, -3, 50, 50)]}
        assert labels == {"BUY NOW", "PREPARE TO BUY", "HOLD", "AVOID"}
        for label in ("BUY NOW", "PREPARE TO BUY"):
            assert buy_sniper._is_buy_candidate({"price": 100, "decision": label, "conviction": 70}) is True
        for label in ("AVOID",):
            assert buy_sniper._is_buy_candidate({"price": 100, "decision": label, "conviction": 70}) is False
