"""
tests/test_dhan_client.py

Covers execution/dhan_client.py (session112 round 16) — previously 21%,
the lowest-coverage file left on round 13/14's priority list after db.py
was closed out in round 14/15. No test file existed for this module at
all before this round.

This is THE ONLY module allowed to hold a decrypted Dhan credential or
call Dhan's API, so a regression here is a silent trading-safety bug, not
a cosmetic one: a wrong tick-rounding, a swallowed post-placement
mismatch, or a Super Order MARKET payload that regresses back to
sending a nonzero price (the exact session41 incident this file's own
comments describe) all fail the same way — no exception, just an order
that either never fills or fills on the wrong side of a price band.

Structurally this module is a mix of:
  * pure functions (tick rounding/validation, the rejection-message
    classifiers, _extract_data, _clean_security_id/_add_security,
    _first_present) — tested directly, no mocking needed.
  * SDK-facing functions (get_funds, get_order_list, get_trade_history,
    place_order, cancel_order, the Super Order family, convert_position,
    the eDIS pair, place_cnc_stop_loss_market/cancel_cnc_stop_loss_order)
    — tested by monkeypatching `_get_sdk_client` to return a
    `SimpleNamespace`-based fake client (same technique
    tests/test_notifier_core.py already uses in this file for httpx), so
    the REAL function body runs end to end against a scripted fake
    instead of a real Dhan account.
  * `_get_sdk_client`/`_load_security_cache`/`edis_inquire` themselves,
    which additionally need `auth.dhan_credentials_ro.get_decrypted_
    credentials` and (for the CSV fallback / eDIS call) real `httpx`
    monkeypatched at the call site.

`dhanhq` (the third-party SDK `_get_sdk_client` imports) is not installed
in every environment this suite runs in; `TestGetSdkClient` tolerates
ImportError/RuntimeError there the same way real-trade-service's own
tests/test_dhan_client_remaining_coverage.py already does for its
identical probe — the import/fallback branch is still exercised either
way, it's only the "returns a real client" assertion that's conditional.

Run from services/position-stocks-service:
    python3 -m pytest tests/test_dhan_client.py -q \
        --cov=execution.dhan_client --cov-report=term-missing
"""
from __future__ import annotations

import logging
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import httpx
import pytest

import notifier
from auth import dhan_credentials_ro
from execution import dhan_client as dc

DB = object()  # never actually queried once _get_sdk_client / credentials are monkeypatched


def _wire_client(monkeypatch, client):
    monkeypatch.setattr(dc, "_get_sdk_client", lambda db: client)


def _wire_creds(monkeypatch, client_id="C1", token="T1"):
    monkeypatch.setattr(
        dhan_credentials_ro, "get_decrypted_credentials", lambda db: (client_id, token)
    )


def _wire_no_creds(monkeypatch):
    monkeypatch.setattr(dhan_credentials_ro, "get_decrypted_credentials", lambda db: None)


# ══════════════════════════════════════════════════════════════════════════
# tick_size_for_price / round_to_tick / is_valid_tick_price
# ══════════════════════════════════════════════════════════════════════════

class TestTickSizeForPrice:
    def test_below_250_is_1_paisa(self):
        assert dc.tick_size_for_price(87.13) == 0.01

    def test_250_to_1000_is_5_paisa(self):
        assert dc.tick_size_for_price(847.41) == 0.05

    def test_above_20000_is_5_rupees(self):
        assert dc.tick_size_for_price(999_999.0) == 5.0

    def test_zero_or_negative_falls_back_to_flat_default(self):
        assert dc.tick_size_for_price(0.0) == dc.TICK_SIZE
        assert dc.tick_size_for_price(-5.0) == dc.TICK_SIZE

    def test_non_numeric_falls_back_to_flat_default(self):
        assert dc.tick_size_for_price("bad") == dc.TICK_SIZE

    def test_no_band_matches_falls_through_to_default(self, monkeypatch):
        # Only reachable if _TICK_SIZE_BANDS has no inf sentinel.
        monkeypatch.setattr(dc, "_TICK_SIZE_BANDS", [(100.0, 0.05), (500.0, 0.10)])
        assert dc.tick_size_for_price(999_999.0) == dc.TICK_SIZE


class TestRoundToTick:
    def test_wockhardt_regression_is_an_exact_multiple(self):
        # session112's own docstring regression: 2026-09-07 binary-float
        # drift on a "clean" 2-decimal price. Decimal math must land exactly.
        result = dc.round_to_tick(2097.05, tick_size=0.05)
        assert result == 2097.05
        assert dc.is_valid_tick_price(result, tick_size=0.05)

    def test_uses_price_band_when_tick_size_not_given(self):
        # 87.13 is already a clean 0.01 multiple (its own band) -- unchanged.
        assert dc.round_to_tick(87.13) == 87.13

    def test_rounds_to_nearest_tick(self):
        assert dc.round_to_tick(847.43, tick_size=0.05) == 847.45

    def test_non_positive_price_returned_unchanged(self):
        assert dc.round_to_tick(0.0) == 0.0
        assert dc.round_to_tick(-5.0) == -5.0

    def test_non_positive_resolved_tick_returned_unchanged(self, monkeypatch):
        monkeypatch.setattr(dc, "tick_size_for_price", lambda p: 0.0)
        assert dc.round_to_tick(100.0) == 100.0

    def test_non_numeric_input_does_not_raise(self):
        assert dc.round_to_tick("bad") == "bad"

    def test_none_input_does_not_raise(self):
        assert dc.round_to_tick(None) is None


class TestIsValidTickPrice:
    def test_valid_multiple_is_true(self):
        assert dc.is_valid_tick_price(847.45, tick_size=0.05) is True

    def test_invalid_multiple_is_false(self):
        assert dc.is_valid_tick_price(847.43, tick_size=0.05) is False

    def test_non_positive_price_or_tick_is_true(self):
        assert dc.is_valid_tick_price(0.0, tick_size=0.05) is True
        assert dc.is_valid_tick_price(100.0, tick_size=0.0) is True

    def test_non_numeric_input_does_not_raise(self):
        assert dc.is_valid_tick_price("bad") is False


# ══════════════════════════════════════════════════════════════════════════
# _extract_data
# ══════════════════════════════════════════════════════════════════════════

class TestExtractData:
    def test_non_dict_passthrough(self):
        assert dc._extract_data([1, 2, 3]) == [1, 2, 3]

    def test_success_returns_key(self):
        assert dc._extract_data({"status": "success", "data": {"a": 1}}) == {"a": 1}

    def test_missing_key_returns_none(self):
        assert dc._extract_data({"status": "success"}) is None

    def test_failure_with_string_remarks_raises(self):
        with pytest.raises(RuntimeError, match="boom"):
            dc._extract_data({"status": "failure", "remarks": "boom"})

    def test_failure_with_dict_remarks_extracts_error_message(self):
        with pytest.raises(RuntimeError, match="bad price"):
            dc._extract_data({"status": "failure", "remarks": {"error_message": "bad price"}})

    def test_failure_with_dict_remarks_missing_error_message_stringifies(self):
        with pytest.raises(RuntimeError):
            dc._extract_data({"status": "failure", "remarks": {"code": 7}})


# ══════════════════════════════════════════════════════════════════════════
# _clean_security_id / _add_security
# ══════════════════════════════════════════════════════════════════════════

class TestCleanSecurityId:
    def test_strips_trailing_float_zeros(self):
        assert dc._clean_security_id("11536.0") == "11536"
        assert dc._clean_security_id("11536.00000") == "11536"

    def test_non_matching_string_unchanged(self):
        assert dc._clean_security_id("11536") == "11536"

    def test_none_becomes_empty_string(self):
        assert dc._clean_security_id(None) == ""


class TestAddSecurity:
    def test_adds_new_symbol(self):
        fresh: dict[str, str] = {}
        dc._add_security(fresh, "TCS", "11536.0")
        assert fresh == {"TCS": "11536"}

    def test_blank_symbol_or_id_is_skipped(self):
        fresh: dict[str, str] = {}
        dc._add_security(fresh, "", "11536")
        dc._add_security(fresh, "TCS", "")
        assert fresh == {}

    def test_collision_keeps_first_seen_and_counts(self, monkeypatch, caplog):
        monkeypatch.setattr(dc, "_security_collision_count", 0)
        fresh = {"TCS": "11536"}
        with caplog.at_level(logging.WARNING):
            dc._add_security(fresh, "TCS", "99999")
        assert fresh == {"TCS": "11536"}  # first-seen kept
        assert dc._security_collision_count == 1
        assert "2+ distinct security_ids" in caplog.text


# ══════════════════════════════════════════════════════════════════════════
# _first_present
# ══════════════════════════════════════════════════════════════════════════

class TestFirstPresent:
    def test_first_matching_key_wins(self):
        assert dc._first_present({"aprvdQty": "5"}, dc._EDIS_APPROVED_KEYS) == 5.0

    def test_later_key_used_when_earlier_absent(self):
        assert dc._first_present({"approved_qty": 3}, dc._EDIS_APPROVED_KEYS) == 3.0

    def test_none_value_is_skipped(self):
        assert dc._first_present({"aprvdQty": None, "approvedQty": 7}, dc._EDIS_APPROVED_KEYS) == 7.0

    def test_unparseable_value_is_skipped(self):
        assert dc._first_present({"aprvdQty": "not-a-number", "approvedQty": 4}, dc._EDIS_APPROVED_KEYS) == 4.0

    def test_no_key_present_returns_none(self):
        assert dc._first_present({}, dc._EDIS_APPROVED_KEYS) is None


# ══════════════════════════════════════════════════════════════════════════
# Rejection classifiers
# ══════════════════════════════════════════════════════════════════════════

class TestClassifiers:
    def test_intraday_cutoff(self):
        assert dc.is_intraday_cutoff_error("Intraday orders cannot be placed at this time")
        assert dc.is_intraday_cutoff_error("Market is closed for intraday square off time")
        assert not dc.is_intraday_cutoff_error("Rate Not Within Ckt Limit 1 To 2")

    def test_security_intraday_restricted(self):
        assert dc.is_security_intraday_restricted_error(
            "This security is not allowed to be traded in intraday"
        )
        assert not dc.is_security_intraday_restricted_error(
            "Intraday orders cannot be placed at this time"
        )

    def test_insufficient_funds(self):
        assert dc.is_insufficient_funds_error("RMS:1:Insufficient funds. Add Rs.500 to trade.")
        assert not dc.is_insufficient_funds_error("Rate Not Within Ckt Limit 1 To 2")

    def test_circuit_limit(self):
        assert dc.is_circuit_limit_error("RMS:1:Rate Not Within Ckt Limit 395.25 To 592.85")
        assert not dc.is_circuit_limit_error("Insufficient funds. Add Rs.500 to trade.")

    def test_circuit_limit_freeze_wording(self):
        """session112 round 22 — real, live Dhan RMS rejection (Allied
        Digital Services, PB FinTech/POLICYBZR incident): "circuit freeze"
        is a distinct phrasing from "Ckt Limit"/"circuit limit" that the
        original marker list didn't catch."""
        assert dc.is_circuit_limit_error(
            "RMS:351260925312407:Order rejected, Stock in circuit freeze. "
            "Place order within 78.95 to 113.60."
        )
        assert dc.is_circuit_limit_error("Order rejected, stock is in circuit freeze.")

    def test_none_message_never_raises(self):
        assert dc.is_intraday_cutoff_error(None) is False
        assert dc.is_circuit_limit_error(None) is False


# ══════════════════════════════════════════════════════════════════════════
# _get_sdk_client
# ══════════════════════════════════════════════════════════════════════════

class TestGetSdkClient:
    def test_no_creds_raises_not_connected(self, monkeypatch):
        _wire_no_creds(monkeypatch)
        with pytest.raises(dc.DhanNotConnectedError):
            dc._get_sdk_client(DB)

    def test_valid_creds_returns_client_or_exercises_sdk_probe(self, monkeypatch):
        # dhanhq may or may not be installed in this environment -- either
        # way, the no-creds guard above and the import/probe below it both
        # ran; only the "got a real client back" assertion is conditional.
        _wire_creds(monkeypatch)
        try:
            client = dc._get_sdk_client(DB)
            assert client is not None
        except (ImportError, RuntimeError):
            pass

    def test_dhanhq_not_installed_raises_runtime_error(self, monkeypatch):
        # Covers lines 156-157: `from dhanhq import dhanhq` raising
        # ImportError -> re-raised as RuntimeError. A `None` entry in
        # sys.modules is the standard way to force an import to fail
        # without needing the package to actually be absent.
        _wire_creds(monkeypatch)
        monkeypatch.setitem(sys.modules, "dhanhq", None)
        with pytest.raises(RuntimeError, match="dhanhq SDK not installed"):
            dc._get_sdk_client(DB)

    def test_pre_2_1_sdk_without_dhancontext_falls_back_to_two_arg_form(self, monkeypatch):
        # Covers lines 161-163: `from dhanhq import DhanContext` raising
        # ImportError (old SDK, e.g. pinned 2.0.2) -> falls back to the
        # legacy two-positional-arg constructor. Fakes the `dhanhq` module
        # with a `dhanhq` class but no `DhanContext` attribute, since the
        # real installed SDK version on any given box may already be >=2.1.
        _wire_creds(monkeypatch)
        calls = []

        class _FakeDhanhqClass:
            def __init__(self, *args):
                calls.append(args)

        fake_module = SimpleNamespace(dhanhq=_FakeDhanhqClass)
        # No DhanContext attribute at all -> `from dhanhq import DhanContext`
        # raises ImportError, exactly like a pre-2.1 SDK install.
        monkeypatch.setitem(sys.modules, "dhanhq", fake_module)
        client = dc._get_sdk_client(DB)
        assert isinstance(client, _FakeDhanhqClass)
        assert calls == [("C1", "T1")]  # old two-arg positional form, not DhanContext-wrapped


# ══════════════════════════════════════════════════════════════════════════
# _load_security_cache / get_security_id
# ══════════════════════════════════════════════════════════════════════════

class _FakeDF:
    def __init__(self, rows):
        self._rows = rows
        self.empty = len(rows) == 0

    def iterrows(self):
        return enumerate(self._rows)


class TestLoadSecurityCache:
    def _reset_cache(self, monkeypatch):
        monkeypatch.setattr(dc, "_security_cache", {})
        monkeypatch.setattr(dc, "_security_cache_loaded_at", 0.0)
        monkeypatch.setattr(dc, "_security_collision_count", 0)

    def test_sdk_path_populates_cache(self, monkeypatch):
        self._reset_cache(monkeypatch)
        rows = [{
            "SEM_EXM_EXCH_ID": "NSE", "SEM_INSTRUMENT_NAME": "EQUITY", "SEM_SERIES": "EQ",
            "SEM_TRADING_SYMBOL": "tcs", "SEM_SMST_SECURITY_ID": "11536.0",
        }]
        _wire_client(monkeypatch, SimpleNamespace(fetch_security_list=lambda mode: _FakeDF(rows)))
        dc._load_security_cache(DB)
        assert dc._security_cache == {"TCS": "11536"}

    def test_sdk_skips_non_nse_or_non_equity_rows(self, monkeypatch):
        self._reset_cache(monkeypatch)
        rows = [
            {"SEM_EXM_EXCH_ID": "BSE", "SEM_INSTRUMENT_NAME": "EQUITY", "SEM_SERIES": "EQ",
             "SEM_TRADING_SYMBOL": "X", "SEM_SMST_SECURITY_ID": "1"},
            {"SEM_EXM_EXCH_ID": "NSE", "SEM_INSTRUMENT_NAME": "FUTURE", "SEM_SERIES": "EQ",
             "SEM_TRADING_SYMBOL": "Y", "SEM_SMST_SECURITY_ID": "2"},
        ]
        _wire_client(monkeypatch, SimpleNamespace(fetch_security_list=lambda mode: _FakeDF(rows)))
        dc._load_security_cache(DB)
        assert dc._security_cache == {}

    def test_sdk_row_exception_is_skipped_not_fatal(self, monkeypatch):
        self._reset_cache(monkeypatch)

        class BoomRow:
            def get(self, *a, **k):
                raise RuntimeError("bad row")

        good = {"SEM_EXM_EXCH_ID": "NSE", "SEM_INSTRUMENT_NAME": "EQUITY", "SEM_SERIES": "EQ",
                "SEM_TRADING_SYMBOL": "TCS", "SEM_SMST_SECURITY_ID": "1"}
        _wire_client(monkeypatch, SimpleNamespace(fetch_security_list=lambda mode: _FakeDF([BoomRow(), good])))
        dc._load_security_cache(DB)
        assert dc._security_cache == {"TCS": "1"}

    def test_sdk_fails_falls_back_to_csv_download(self, monkeypatch):
        self._reset_cache(monkeypatch)

        def boom(mode):
            raise RuntimeError("SDK down")

        _wire_client(monkeypatch, SimpleNamespace(fetch_security_list=boom))
        _wire_creds(monkeypatch)
        csv_text = (
            "SEM_EXM_EXCH_ID,SEM_INSTRUMENT_NAME,SEM_SERIES,SEM_TRADING_SYMBOL,SEM_SMST_SECURITY_ID\r\n"
            "NSE,EQUITY,EQ,TCS,11536.0\r\n"
            "NSE,EQUITY,BE,SKIP,999\r\n"
        )
        monkeypatch.setattr(
            dc.httpx, "get",
            lambda url, headers, timeout: httpx.Response(200, text=csv_text, request=httpx.Request("GET", url)),
        )
        dc._load_security_cache(DB)
        assert dc._security_cache == {"TCS": "11536"}

    def test_csv_fallback_skips_non_nse_non_equity_and_row_exceptions(self, monkeypatch):
        # Covers the CSV-fallback per-row filter/exception lines (244, 246,
        # 252-253) — the existing `test_sdk_fails_falls_back_to_csv_download`
        # only exercises the SEM_SERIES filter (line 248) via its "BE" row;
        # the exchange filter, instrument filter, and per-row except/continue
        # in this loop were never hit.
        self._reset_cache(monkeypatch)

        def boom(mode):
            raise RuntimeError("SDK down")

        _wire_client(monkeypatch, SimpleNamespace(fetch_security_list=boom))
        _wire_creds(monkeypatch)
        csv_text = (
            "SEM_EXM_EXCH_ID,SEM_INSTRUMENT_NAME,SEM_SERIES,SEM_TRADING_SYMBOL,SEM_SMST_SECURITY_ID\r\n"
            "BSE,EQUITY,EQ,WRONGEXCH,1\r\n"
            "NSE,FUTURE,EQ,WRONGINSTR,2\r\n"
            "NSE,EQUITY,EQ,BOOM,3\r\n"
            "NSE,EQUITY,EQ,TCS,11536.0\r\n"
        )
        monkeypatch.setattr(
            dc.httpx, "get",
            lambda url, headers, timeout: httpx.Response(200, text=csv_text, request=httpx.Request("GET", url)),
        )
        real_add_security = dc._add_security

        def flaky_add_security(fresh, sym, sec_id_raw):
            if sym == "BOOM":
                raise RuntimeError("bad row")
            real_add_security(fresh, sym, sec_id_raw)

        monkeypatch.setattr(dc, "_add_security", flaky_add_security)
        dc._load_security_cache(DB)
        # Only the well-formed NSE/EQUITY row survives all three filters
        # plus the per-row exception guard.
        assert dc._security_cache == {"TCS": "11536"}

    def test_csv_fallback_with_no_creds_raises(self, monkeypatch):
        self._reset_cache(monkeypatch)

        def boom(mode):
            raise RuntimeError("SDK down")

        _wire_client(monkeypatch, SimpleNamespace(fetch_security_list=boom))
        _wire_no_creds(monkeypatch)
        with pytest.raises(dc.DhanNotConnectedError):
            dc._load_security_cache(DB)

    def test_both_paths_fail_cache_kept_and_error_logged(self, monkeypatch, caplog):
        self._reset_cache(monkeypatch)
        monkeypatch.setattr(dc, "_security_cache", {"OLD": "1"})

        def boom(mode):
            raise RuntimeError("SDK down")

        _wire_client(monkeypatch, SimpleNamespace(fetch_security_list=boom))
        _wire_creds(monkeypatch)

        def boom_get(url, headers, timeout):
            raise httpx.ConnectError("network down")

        monkeypatch.setattr(dc.httpx, "get", boom_get)
        with caplog.at_level(logging.ERROR):
            dc._load_security_cache(DB)
        assert dc._security_cache == {"OLD": "1"}  # untouched, no rows fetched
        assert "also failed" in caplog.text

    def test_collision_logs_summary_warning(self, monkeypatch, caplog):
        self._reset_cache(monkeypatch)  # ensures monkeypatch owns + reverts these globals
        rows = [
            {"SEM_EXM_EXCH_ID": "NSE", "SEM_INSTRUMENT_NAME": "EQUITY", "SEM_SERIES": "EQ",
             "SEM_TRADING_SYMBOL": "TCS", "SEM_SMST_SECURITY_ID": "1"},
            {"SEM_EXM_EXCH_ID": "NSE", "SEM_INSTRUMENT_NAME": "EQUITY", "SEM_SERIES": "EQ",
             "SEM_TRADING_SYMBOL": "TCS", "SEM_SMST_SECURITY_ID": "2"},
        ]
        _wire_client(monkeypatch, SimpleNamespace(fetch_security_list=lambda mode: _FakeDF(rows)))
        with caplog.at_level(logging.WARNING):
            dc._load_security_cache(DB)
        assert "1 symbol(s) had 2+ distinct" in caplog.text


class TestGetSecurityId:
    def test_cache_hit_returns_cleaned_id(self, monkeypatch):
        monkeypatch.setattr(dc, "_security_cache", {"TCS": "11536.0"})
        monkeypatch.setattr(dc, "_security_cache_loaded_at", dc.time.time())
        assert dc.get_security_id(DB, "tcs.NS") == "11536"

    def test_stale_cache_triggers_reload(self, monkeypatch):
        monkeypatch.setattr(dc, "_security_cache", {})
        monkeypatch.setattr(dc, "_security_cache_loaded_at", 0.0)
        calls = []
        monkeypatch.setattr(dc, "_load_security_cache", lambda db: (
            calls.append(1), dc._security_cache.update({"TCS": "1"})
        ))
        result = dc.get_security_id(DB, "TCS")
        assert result == "1"
        assert calls == [1]

    def test_not_found_raises(self, monkeypatch):
        monkeypatch.setattr(dc, "_security_cache", {"TCS": "1"})
        monkeypatch.setattr(dc, "_security_cache_loaded_at", dc.time.time())
        with pytest.raises(dc.SecurityNotResolvedError):
            dc.get_security_id(DB, "NOPE")


# ══════════════════════════════════════════════════════════════════════════
# get_funds / get_order_list / get_trade_history
# ══════════════════════════════════════════════════════════════════════════

class TestGetFunds:
    def test_returns_dict_payload(self, monkeypatch):
        _wire_client(monkeypatch, SimpleNamespace(
            get_fund_limits=lambda: {"status": "success", "data": {"availableBalance": 1000}}
        ))
        assert dc.get_funds(DB) == {"availableBalance": 1000}

    def test_non_dict_payload_becomes_empty_dict(self, monkeypatch):
        _wire_client(monkeypatch, SimpleNamespace(
            get_fund_limits=lambda: {"status": "success", "data": [1, 2]}
        ))
        assert dc.get_funds(DB) == {}


class TestGetOrderList:
    def test_returns_list_payload(self, monkeypatch):
        _wire_client(monkeypatch, SimpleNamespace(
            get_order_list=lambda: {"status": "success", "data": [{"orderId": "1"}]}
        ))
        assert dc.get_order_list(DB) == [{"orderId": "1"}]

    def test_non_list_payload_becomes_empty_list(self, monkeypatch):
        _wire_client(monkeypatch, SimpleNamespace(
            get_order_list=lambda: {"status": "success", "data": {"not": "a list"}}
        ))
        assert dc.get_order_list(DB) == []


class TestGetTradeHistory:
    def test_single_page_collects_rows(self, monkeypatch):
        _wire_client(monkeypatch, SimpleNamespace(
            get_trade_history=lambda f, t, page: (
                {"status": "success", "data": [{"orderId": "1", "exchangeTradeId": "t1"}]}
                if page == 0 else {"status": "success", "data": []}
            )
        ))
        rows = dc.get_trade_history(DB, "2026-09-01", "2026-09-02")
        assert rows == [{"orderId": "1", "exchangeTradeId": "t1"}]

    def test_empty_first_page_returns_empty(self, monkeypatch):
        _wire_client(monkeypatch, SimpleNamespace(
            get_trade_history=lambda f, t, page: {"status": "success", "data": []}
        ))
        assert dc.get_trade_history(DB, "2026-09-01", "2026-09-02") == []

    def test_no_trade_error_breaks_cleanly(self, monkeypatch):
        def raiser(f, t, page):
            return {"status": "failure", "remarks": "No trade found for the date range"}

        _wire_client(monkeypatch, SimpleNamespace(get_trade_history=raiser))
        assert dc.get_trade_history(DB, "2026-09-01", "2026-09-02") == []

    def test_other_runtime_error_propagates(self, monkeypatch):
        def raiser(f, t, page):
            return {"status": "failure", "remarks": "some other broker error"}

        _wire_client(monkeypatch, SimpleNamespace(get_trade_history=raiser))
        with pytest.raises(RuntimeError):
            dc.get_trade_history(DB, "2026-09-01", "2026-09-02")

    def test_repeated_page_breaks_pagination(self, monkeypatch):
        row = {"orderId": "1", "exchangeTradeId": "t1"}
        _wire_client(monkeypatch, SimpleNamespace(
            get_trade_history=lambda f, t, page: {"status": "success", "data": [row]}
        ))
        rows = dc.get_trade_history(DB, "2026-09-01", "2026-09-02", max_pages=5)
        assert rows == [row]  # page 1 repeats page 0's key -> stop after page 0

    def test_dict_wrapped_rows_are_unwrapped(self, monkeypatch):
        _wire_client(monkeypatch, SimpleNamespace(
            get_trade_history=lambda f, t, page: (
                {"status": "success", "data": {"data": [{"orderId": "1", "createTime": "c1"}]}}
                if page == 0 else {"status": "success", "data": []}
            )
        ))
        rows = dc.get_trade_history(DB, "2026-09-01", "2026-09-02")
        assert rows == [{"orderId": "1", "createTime": "c1"}]


# ══════════════════════════════════════════════════════════════════════════
# place_order
# ══════════════════════════════════════════════════════════════════════════

def _order_client(place_result=None, order_list=None, place_error=None):
    calls = {"place_order": [], "get_order_list": 0}

    def place_order(**kwargs):
        calls["place_order"].append(kwargs)
        if place_error:
            raise place_error
        return place_result or {"status": "success", "data": {"orderId": "OID1"}}

    def get_order_list():
        calls["get_order_list"] += 1
        return {"status": "success", "data": order_list or []}

    client = SimpleNamespace(place_order=place_order, get_order_list=get_order_list)
    return client, calls


class TestPlaceOrder:
    def test_not_armed_raises(self, monkeypatch):
        client, _ = _order_client()
        _wire_client(monkeypatch, client)
        with pytest.raises(dc.DhanNotArmedError):
            dc.place_order(
                DB, is_armed=False, security_id="1", exchange_segment=dc.NSE_EQ_SEGMENT,
                transaction_type="BUY", quantity=1, order_type="MARKET", price=0,
            )

    def test_invalid_order_type_raises(self, monkeypatch):
        client, _ = _order_client()
        _wire_client(monkeypatch, client)
        with pytest.raises(ValueError):
            dc.place_order(
                DB, is_armed=True, security_id="1", exchange_segment=dc.NSE_EQ_SEGMENT,
                transaction_type="BUY", quantity=1, order_type="BOGUS", price=0,
            )

    def test_market_order_forces_price_to_zero(self, monkeypatch, caplog):
        client, calls = _order_client()
        _wire_client(monkeypatch, client)
        with caplog.at_level(logging.WARNING):
            dc.place_order(
                DB, is_armed=True, security_id="1", exchange_segment=dc.NSE_EQ_SEGMENT,
                transaction_type="BUY", quantity=1, order_type="MARKET", price=123.45,
            )
        assert calls["place_order"][0]["price"] == 0.0
        assert "forcing price to 0" in caplog.text

    def test_limit_order_rounds_price_to_tick(self, monkeypatch):
        client, calls = _order_client()
        _wire_client(monkeypatch, client)
        dc.place_order(
            DB, is_armed=True, security_id="1", exchange_segment=dc.NSE_EQ_SEGMENT,
            transaction_type="BUY", quantity=1, order_type="LIMIT", price=847.43,
        )
        assert calls["place_order"][0]["price"] == 847.45

    def test_limit_order_invalid_tick_after_rounding_raises(self, monkeypatch):
        client, _ = _order_client()
        _wire_client(monkeypatch, client)
        monkeypatch.setattr(dc, "is_valid_tick_price", lambda p, tick_size=None: False)
        with pytest.raises(ValueError):
            dc.place_order(
                DB, is_armed=True, security_id="1", exchange_segment=dc.NSE_EQ_SEGMENT,
                transaction_type="BUY", quantity=1, order_type="LIMIT", price=847.43,
            )

    def test_no_order_id_in_result_skips_verification(self, monkeypatch):
        client, calls = _order_client(place_result={"status": "success", "data": {}})
        _wire_client(monkeypatch, client)
        dc.place_order(
            DB, is_armed=True, security_id="1", exchange_segment=dc.NSE_EQ_SEGMENT,
            transaction_type="BUY", quantity=1, order_type="MARKET", price=0,
        )
        assert calls["get_order_list"] == 0

    def test_market_echoed_as_limit_is_not_flagged(self, monkeypatch):
        client, _ = _order_client(order_list=[{"orderId": "OID1", "orderType": "LIMIT", "price": 100}])
        _wire_client(monkeypatch, client)
        notify_calls = []
        monkeypatch.setattr(notifier, "notify_critical", lambda msg: notify_calls.append(msg))
        dc.place_order(
            DB, is_armed=True, security_id="1", exchange_segment=dc.NSE_EQ_SEGMENT,
            transaction_type="BUY", quantity=1, order_type="MARKET", price=0,
        )
        assert notify_calls == []

    def test_genuine_type_mismatch_is_flagged_critical(self, monkeypatch, caplog):
        client, _ = _order_client(order_list=[{"orderId": "OID1", "orderType": "STOP_LOSS", "price": 100}])
        _wire_client(monkeypatch, client)
        notify_calls = []
        monkeypatch.setattr(notifier, "notify_critical", lambda msg: notify_calls.append(msg))
        with caplog.at_level(logging.CRITICAL):
            dc.place_order(
                DB, is_armed=True, security_id="1", exchange_segment=dc.NSE_EQ_SEGMENT,
                transaction_type="BUY", quantity=1, order_type="LIMIT", price=100,
            )
        assert len(notify_calls) == 1
        assert "BROKER ORDER TYPE MISMATCH" in notify_calls[0]

    def test_verification_failure_is_swallowed_not_raised(self, monkeypatch, caplog):
        def boom_get_order_list():
            raise RuntimeError("network blip")

        client = SimpleNamespace(
            place_order=lambda **kw: {"status": "success", "data": {"orderId": "OID1"}},
            get_order_list=boom_get_order_list,
        )
        _wire_client(monkeypatch, client)
        with caplog.at_level(logging.WARNING):
            result = dc.place_order(
                DB, is_armed=True, security_id="1", exchange_segment=dc.NSE_EQ_SEGMENT,
                transaction_type="BUY", quantity=1, order_type="MARKET", price=0,
            )
        assert result["orderId"] == "OID1"
        assert "verification failed" in caplog.text


class TestCancelOrder:
    def test_delegates_to_client_and_extracts_data(self, monkeypatch):
        calls = []
        client = SimpleNamespace(cancel_order=lambda oid: (calls.append(oid), {
            "status": "success", "data": {"orderId": oid, "status": "CANCELLED"}
        })[1])
        _wire_client(monkeypatch, client)
        result = dc.cancel_order(DB, is_armed=False, dhan_order_id="OID1")
        assert calls == ["OID1"]
        assert result["status"] == "CANCELLED"


# ══════════════════════════════════════════════════════════════════════════
# place_super_order
# ══════════════════════════════════════════════════════════════════════════

class TestPlaceSuperOrderMarket:
    def _kwargs(self, **overrides):
        base = dict(
            db=DB, is_armed=True, security_id="1", exchange_segment=dc.NSE_EQ_SEGMENT,
            transaction_type="BUY", quantity=1, order_type="MARKET",
            price=100.0, target_price=105.0, stop_loss_price=98.0,
        )
        base.update(overrides)
        return base

    def test_not_armed_raises(self, monkeypatch):
        with pytest.raises(dc.DhanNotArmedError):
            dc.place_super_order(**self._kwargs(is_armed=False))

    def test_no_dhan_http_raises_runtime_error(self, monkeypatch):
        _wire_client(monkeypatch, SimpleNamespace())  # no dhan_http attribute
        with pytest.raises(RuntimeError):
            dc.place_super_order(**self._kwargs())

    def test_success_posts_direct_http_with_no_price_key(self, monkeypatch):
        posts = []
        dhan_http = SimpleNamespace(post=lambda path, payload: (
            posts.append((path, payload)),
            {"status": "success", "data": {"orderId": "SO1"}},
        )[1])
        _wire_client(monkeypatch, SimpleNamespace(dhan_http=dhan_http))
        result = dc.place_super_order(**self._kwargs(tag="scalp-1"))
        assert result["orderId"] == "SO1"
        path, payload = posts[0]
        assert path == "/super/orders"
        assert "price" not in payload
        assert payload["orderType"] == "MARKET"
        assert payload["correlationId"] == "scalp-1"

    def test_target_bumped_above_ref_when_collapsed_by_rounding(self, monkeypatch):
        posts = []
        dhan_http = SimpleNamespace(post=lambda path, payload: (
            posts.append(payload), {"status": "success", "data": {}}
        )[1])
        _wire_client(monkeypatch, SimpleNamespace(dhan_http=dhan_http))
        dc.place_super_order(**self._kwargs(price=100.0, target_price=100.0, stop_loss_price=98.0))
        assert posts[0]["targetPrice"] > 100.0

    def test_stop_clamped_below_ref_for_buy_when_collapsed_by_rounding(self, monkeypatch):
        posts = []
        dhan_http = SimpleNamespace(post=lambda path, payload: (
            posts.append(payload), {"status": "success", "data": {}}
        )[1])
        _wire_client(monkeypatch, SimpleNamespace(dhan_http=dhan_http))
        dc.place_super_order(
            **self._kwargs(transaction_type="BUY", price=100.0, target_price=105.0, stop_loss_price=100.0)
        )
        assert posts[0]["stopLossPrice"] < 100.0


class TestPlaceSuperOrderLimit:
    def _kwargs(self, **overrides):
        base = dict(
            db=DB, is_armed=True, security_id="1", exchange_segment=dc.NSE_EQ_SEGMENT,
            transaction_type="BUY", quantity=1, order_type="LIMIT",
            price=100.0, target_price=105.0, stop_loss_price=98.0,
        )
        base.update(overrides)
        return base

    def test_missing_price_raises(self, monkeypatch):
        _wire_client(monkeypatch, SimpleNamespace())
        with pytest.raises(ValueError):
            dc.place_super_order(**self._kwargs(price=0))

    def test_invalid_tick_after_rounding_raises(self, monkeypatch):
        _wire_client(monkeypatch, SimpleNamespace())
        monkeypatch.setattr(dc, "is_valid_tick_price", lambda p, tick_size=None: False)
        with pytest.raises(ValueError):
            dc.place_super_order(**self._kwargs())

    def test_success_calls_sdk_with_tag(self, monkeypatch):
        calls = []

        def place_super_order(**kw):
            calls.append(kw)
            return {"status": "success", "data": {"orderId": "SO2"}}

        _wire_client(monkeypatch, SimpleNamespace(place_super_order=place_super_order))
        result = dc.place_super_order(**self._kwargs(tag="scalp-2"))
        assert result["orderId"] == "SO2"
        assert calls[0]["tag"] == "scalp-2"

    def test_typeerror_on_tag_retries_without_it(self, monkeypatch):
        calls = []

        def place_super_order(**kw):
            calls.append(kw)
            if "tag" in kw:
                raise TypeError("unexpected keyword argument 'tag'")
            return {"status": "success", "data": {"orderId": "SO3"}}

        _wire_client(monkeypatch, SimpleNamespace(place_super_order=place_super_order))
        result = dc.place_super_order(**self._kwargs(tag="scalp-3"))
        assert result["orderId"] == "SO3"
        assert len(calls) == 2
        assert "tag" not in calls[1]


class TestSuperOrderReadModify:
    def test_get_super_order_list(self, monkeypatch):
        _wire_client(monkeypatch, SimpleNamespace(
            get_super_order_list=lambda: {"status": "success", "data": [{"orderId": "1"}]}
        ))
        assert dc.get_super_order_list(DB) == [{"orderId": "1"}]

    def test_cancel_super_order(self, monkeypatch):
        calls = []
        client = SimpleNamespace(cancel_super_order=lambda oid, leg: (calls.append((oid, leg)), {
            "status": "success", "data": {}
        })[1])
        _wire_client(monkeypatch, client)
        dc.cancel_super_order(DB, order_id="SO1", order_leg="TARGET_LEG")
        assert calls == [("SO1", "TARGET_LEG")]

    def test_modify_unsupported_leg_raises(self, monkeypatch):
        with pytest.raises(ValueError):
            dc.modify_super_order(DB, order_id="SO1", order_leg="ENTRY_LEG")

    def test_modify_stop_loss_leg_requires_price(self, monkeypatch):
        with pytest.raises(ValueError):
            dc.modify_super_order(DB, order_id="SO1", order_leg="STOP_LOSS_LEG")

    def test_modify_target_leg_requires_price(self, monkeypatch):
        with pytest.raises(ValueError):
            dc.modify_super_order(DB, order_id="SO1", order_leg="TARGET_LEG")

    def test_modify_stop_loss_leg_success(self, monkeypatch):
        calls = []
        _wire_client(monkeypatch, SimpleNamespace(
            modify_super_order=lambda **kw: (calls.append(kw), {"status": "success", "data": {}})[1]
        ))
        dc.modify_super_order(DB, order_id="SO1", order_leg="STOP_LOSS_LEG", stop_loss_price=95.03)
        assert calls[0]["leg_name"] == "STOP_LOSS_LEG"
        assert calls[0]["stopLossPrice"] == dc.round_to_tick(95.03)

    def test_modify_target_leg_success(self, monkeypatch):
        calls = []
        _wire_client(monkeypatch, SimpleNamespace(
            modify_super_order=lambda **kw: (calls.append(kw), {"status": "success", "data": {}})[1]
        ))
        dc.modify_super_order(DB, order_id="SO1", order_leg="TARGET_LEG", target_price=110.03)
        assert calls[0]["leg_name"] == "TARGET_LEG"
        assert calls[0]["targetPrice"] == dc.round_to_tick(110.03)


# ══════════════════════════════════════════════════════════════════════════
# convert_position
# ══════════════════════════════════════════════════════════════════════════

class TestConvertPosition:
    def _kwargs(self, **overrides):
        base = dict(
            db=DB, is_armed=True, security_id="1", exchange_segment=dc.NSE_EQ_SEGMENT,
            position_type="LONG", convert_qty=5,
        )
        base.update(overrides)
        return base

    def test_not_armed_raises(self):
        with pytest.raises(dc.DhanNotArmedError):
            dc.convert_position(**self._kwargs(is_armed=False))

    def test_no_dhan_http_raises(self, monkeypatch):
        _wire_client(monkeypatch, SimpleNamespace())
        with pytest.raises(RuntimeError):
            dc.convert_position(**self._kwargs())

    def test_success_posts_conversion_payload(self, monkeypatch):
        posts = []
        dhan_http = SimpleNamespace(post=lambda path, payload: (
            posts.append((path, payload)), {"status": "success", "data": {"ok": True}}
        )[1])
        _wire_client(monkeypatch, SimpleNamespace(dhan_http=dhan_http, client_id="CID1"))
        result = dc.convert_position(**self._kwargs())
        assert result == {"ok": True}
        path, payload = posts[0]
        assert path == "/positions/convert"
        assert payload["dhanClientId"] == "CID1"
        assert payload["toProductType"] == "CNC"
        assert payload["fromProductType"] == "INTRADAY"


# ══════════════════════════════════════════════════════════════════════════
# edis_inquire / edis_verification_summary
# ══════════════════════════════════════════════════════════════════════════

class TestEdisInquire:
    def test_no_creds_raises(self, monkeypatch):
        _wire_no_creds(monkeypatch)
        with pytest.raises(dc.DhanNotConnectedError):
            dc.edis_inquire(DB)

    def test_success_returns_json(self, monkeypatch):
        _wire_creds(monkeypatch)
        monkeypatch.setattr(
            dc.httpx, "get",
            lambda url, headers, timeout: httpx.Response(
                200, json={"data": [{"isin": "X", "aprvdQty": 5, "totalQty": 5}]},
                request=httpx.Request("GET", url),
            ),
        )
        result = dc.edis_inquire(DB)
        assert result["data"][0]["isin"] == "X"


class TestEdisVerificationSummary:
    def test_not_connected_returns_none_verified(self, monkeypatch):
        monkeypatch.setattr(dc, "edis_inquire", lambda db, isin="ALL": (_ for _ in ()).throw(
            dc.DhanNotConnectedError("no creds")
        ))
        result = dc.edis_verification_summary(DB)
        assert result["verified_today"] is None
        assert "no creds" in result["detail"]

    def test_generic_failure_returns_none_verified(self, monkeypatch):
        monkeypatch.setattr(dc, "edis_inquire", lambda db, isin="ALL": (_ for _ in ()).throw(
            RuntimeError("timeout")
        ))
        result = dc.edis_verification_summary(DB)
        assert result["verified_today"] is None
        assert "eDIS inquire call failed" in result["detail"]

    def test_no_rows_returns_none_verified(self, monkeypatch):
        monkeypatch.setattr(dc, "edis_inquire", lambda db, isin="ALL": {})
        result = dc.edis_verification_summary(DB)
        assert result["verified_today"] is None
        assert result["holdings_total"] == 0

    def test_all_approved(self, monkeypatch):
        monkeypatch.setattr(dc, "edis_inquire", lambda db, isin="ALL": {
            "data": [{"isin": "X", "aprvdQty": 5, "totalQty": 5}]
        })
        result = dc.edis_verification_summary(DB)
        assert result["verified_today"] is True
        assert result["holdings_pending"] == 0

    def test_some_pending(self, monkeypatch):
        monkeypatch.setattr(dc, "edis_inquire", lambda db, isin="ALL": {
            "data": [{"isin": "X", "aprvdQty": 2, "totalQty": 5}]
        })
        result = dc.edis_verification_summary(DB)
        assert result["verified_today"] is False
        assert result["pending_symbols"] == ["X"]

    def test_unrecognized_shape_returns_none(self, monkeypatch):
        monkeypatch.setattr(dc, "edis_inquire", lambda db, isin="ALL": {
            "data": [{"foo": "bar"}]
        })
        result = dc.edis_verification_summary(DB)
        assert result["verified_today"] is None
        assert "unrecognized shape" in result["detail"]

    def test_holdings_key_used_when_data_absent(self, monkeypatch):
        monkeypatch.setattr(dc, "edis_inquire", lambda db, isin="ALL": {
            "holdings": [{"isin": "X", "aprvdQty": 5, "totalQty": 5}]
        })
        result = dc.edis_verification_summary(DB)
        assert result["holdings_total"] == 1

    def test_list_response_used_directly(self, monkeypatch):
        monkeypatch.setattr(dc, "edis_inquire", lambda db, isin="ALL": [
            {"isin": "X", "aprvdQty": 5, "totalQty": 5}
        ])
        result = dc.edis_verification_summary(DB)
        assert result["verified_today"] is True

    def test_non_dict_row_is_counted_unrecognized_not_fatal(self, monkeypatch):
        # Covers lines 960-961 (`if not isinstance(row, dict): unrecognized
        # += 1; continue`) — every other test's rows are always dicts, well-
        # or ill-shaped, so a row that isn't a dict at all (a malformed/
        # unexpected API shape) was never hit.
        monkeypatch.setattr(dc, "edis_inquire", lambda db, isin="ALL": {
            "data": ["not-a-dict-row", {"isin": "X", "aprvdQty": 5, "totalQty": 5}]
        })
        result = dc.edis_verification_summary(DB)
        assert result["verified_today"] is True  # the one real row is fully approved
        assert result["holdings_total"] == 2  # non-dict row still counted in the total


# ══════════════════════════════════════════════════════════════════════════
# place_cnc_stop_loss_market / cancel_cnc_stop_loss_order
# ══════════════════════════════════════════════════════════════════════════

def _stop_kwargs(**overrides):
    base = dict(
        db=DB, is_armed=True, security_id="1", exchange_segment=dc.NSE_EQ_SEGMENT,
        quantity=5, trigger_price=95.03,
    )
    base.update(overrides)
    return base


class TestPlaceCncStopLossMarket:
    def test_not_armed_raises(self):
        with pytest.raises(dc.DhanNotArmedError):
            dc.place_cnc_stop_loss_market(**_stop_kwargs(is_armed=False))

    def test_non_positive_trigger_raises(self, monkeypatch):
        with pytest.raises(ValueError):
            dc.place_cnc_stop_loss_market(**_stop_kwargs(trigger_price=0))

    def test_no_order_id_in_result_raises(self, monkeypatch):
        _wire_client(monkeypatch, SimpleNamespace(
            place_order=lambda **kw: {"status": "success", "data": {}}
        ))
        with pytest.raises(RuntimeError, match="did not return an orderId"):
            dc.place_cnc_stop_loss_market(**_stop_kwargs())

    def test_get_order_list_failure_during_verification_raises(self, monkeypatch):
        def boom():
            raise RuntimeError("network blip")

        _wire_client(monkeypatch, SimpleNamespace(
            place_order=lambda **kw: {"status": "success", "data": {"orderId": "SL1"}},
            get_order_list=boom,
        ))
        with pytest.raises(RuntimeError, match="cannot confirm stop is live"):
            dc.place_cnc_stop_loss_market(**_stop_kwargs())

    def test_order_not_found_in_broker_book_raises(self, monkeypatch):
        _wire_client(monkeypatch, SimpleNamespace(
            place_order=lambda **kw: {"status": "success", "data": {"orderId": "SL1"}},
            get_order_list=lambda: {"status": "success", "data": []},
        ))
        with pytest.raises(RuntimeError, match="NOT found"):
            dc.place_cnc_stop_loss_market(**_stop_kwargs())

    def test_order_type_mismatch_raises(self, monkeypatch):
        _wire_client(monkeypatch, SimpleNamespace(
            place_order=lambda **kw: {"status": "success", "data": {"orderId": "SL1"}},
            get_order_list=lambda: {"status": "success", "data": [
                {"orderId": "SL1", "orderType": "LIMIT", "orderStatus": "PENDING"}
            ]},
        ))
        with pytest.raises(RuntimeError, match="ORDER TYPE MISMATCH"):
            dc.place_cnc_stop_loss_market(**_stop_kwargs())

    def test_non_live_status_raises(self, monkeypatch):
        _wire_client(monkeypatch, SimpleNamespace(
            place_order=lambda **kw: {"status": "success", "data": {"orderId": "SL1"}},
            get_order_list=lambda: {"status": "success", "data": [
                {"orderId": "SL1", "orderType": "STOP_LOSS_MARKET", "orderStatus": "REJECTED"}
            ]},
        ))
        with pytest.raises(RuntimeError, match="not a live pending state"):
            dc.place_cnc_stop_loss_market(**_stop_kwargs())

    def test_success_returns_confirmed_order(self, monkeypatch):
        place_calls = []
        _wire_client(monkeypatch, SimpleNamespace(
            place_order=lambda **kw: (place_calls.append(kw), {
                "status": "success", "data": {"orderId": "SL1"}
            })[1],
            get_order_list=lambda: {"status": "success", "data": [
                {"orderId": "SL1", "orderType": "STOP_LOSS_MARKET", "orderStatus": "PENDING"}
            ]},
        ))
        result = dc.place_cnc_stop_loss_market(**_stop_kwargs())
        assert result["orderId"] == "SL1"
        assert place_calls[0]["order_type"] == "STOP_LOSS_MARKET"
        assert place_calls[0]["product_type"] == "CNC"
        assert place_calls[0]["price"] == 0.0


class TestCancelCncStopLossOrder:
    def test_delegates_to_cancel_order(self, monkeypatch):
        calls = []
        client = SimpleNamespace(cancel_order=lambda oid: (calls.append(oid), {
            "status": "success", "data": {"status": "CANCELLED"}
        })[1])
        _wire_client(monkeypatch, client)
        result = dc.cancel_cnc_stop_loss_order(DB, order_id="SL1")
        assert calls == ["SL1"]
        assert result["status"] == "CANCELLED"
