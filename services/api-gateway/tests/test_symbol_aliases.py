"""tests/test_symbol_aliases.py — coverage for api-gateway/symbol_aliases.py

The shared rename / delisted / non-equity / high-price symbol table plus the durable, KV-backed
"learned rename" machinery (failure streaks, learned-delisted, NSE announcement discovery).

No network and no real KV: `kv_cache` and `httpx` are replaced by small fakes in sys.modules (the
module imports both lazily, inside functions). The fake KV returns deep copies, so nothing in the
module can lean on object aliasing that a real Redis/DB round trip would not give it.

A separate class guards against drift: every name the rest of the gateway imports from this module
exists, the real kv_cache signatures match how this module calls them, and the rename table agrees
with market-data-service's SMART_SYMBOL_MAP (the module's docstring promises they stay in sync).

Run from services/api-gateway:
    python3 -m pytest tests/test_symbol_aliases.py -v
"""
from __future__ import annotations

import ast
import copy
import importlib.util
import inspect
import logging
import os
import re
import sys
import types

import pytest

import symbol_aliases as sa

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = os.path.dirname(_HERE)

RENAMES_KEY = sa._LEARNED_RENAMES_KEY
DELISTED_KEY = sa._LEARNED_DELISTED_KEY
COUNTS_KEY = sa._FAILURE_COUNTS_KEY


# ── fakes ─────────────────────────────────────────────────────────────────────

class FakeKV:
    def __init__(self):
        self.store = {}
        self.sets = []                  # (key, value, ttl)
        self.get_raises = None          # exception for every get
        self.set_raises = None          # exception for every set
        self.set_raises_for = {}        # key -> exception

    def module(self):
        kv = self
        m = types.ModuleType("kv_cache")

        def kv_get(key):
            if kv.get_raises is not None:
                raise kv.get_raises
            return copy.deepcopy(kv.store.get(key))

        def kv_set(key, value, ttl=None):
            if kv.set_raises is not None:
                raise kv.set_raises
            if key in kv.set_raises_for:
                raise kv.set_raises_for[key]
            kv.store[key] = copy.deepcopy(value)
            kv.sets.append((key, copy.deepcopy(value), ttl))

        m.kv_get = kv_get
        m.kv_set = kv_set
        return m

    def sets_for(self, key):
        return [(v, t) for k, v, t in self.sets if k == key]


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeHttpx:
    def __init__(self):
        self.home = FakeResp(200, {})
        self.home_raises = None
        self.api = FakeResp(200, [])
        self.api_raises = None
        self.client_kwargs = []
        self.calls = []                 # (url, params, timeout)

    def module(self):
        h = self
        m = types.ModuleType("httpx")

        class Client:
            def __init__(self, **kw):
                h.client_kwargs.append(kw)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, params=None, timeout=None):
                h.calls.append((url, params, timeout))
                if url == "https://www.nseindia.com":
                    if h.home_raises is not None:
                        raise h.home_raises
                    return h.home
                if h.api_raises is not None:
                    raise h.api_raises
                return h.api

        m.Client = Client
        return m


@pytest.fixture
def kv(monkeypatch):
    fake = FakeKV()
    monkeypatch.setitem(sys.modules, "kv_cache", fake.module())
    return fake


@pytest.fixture
def http(monkeypatch):
    fake = FakeHttpx()
    monkeypatch.setitem(sys.modules, "httpx", fake.module())
    return fake


# ── static tables ─────────────────────────────────────────────────────────────

class TestStaticTables:
    @pytest.mark.parametrize("old, new", [
        ("ZOMATO", "ETERNAL"), ("MINDTREE", "LTIM"), ("SRTRANSFIN", "SHRIRAMFIN"), ("GMRINFRA", "GMRAIRPORT"),
        ("MOTHERSUMI", "MOTHERSON"), ("CADILAHC", "ZYDUSLIFE"), ("PVR", "PVRINOX"),
        ("IBULHSGFIN", "SAMMAANCAP"), ("L&TFH", "LTF"), ("ADANITRANS", "ADANIENSOL"),
        ("NSPIRA", "NSIL"), ("KFINTECHNOLOGIES", "KFINTECH"), ("KPITTECHNOLOGIES", "KPITTECH"),
        ("ONE97", "PAYTM"), ("JUBILANT", "JUBLFOOD"), ("TATAMOTORS", "TMPV"), ("LTIM", "LTM"),
    ])
    def test_rename_entries(self, old, new):
        assert sa.SYMBOL_RENAMES[old] == new

    def test_unchanged_symbols_are_explicit_identity_entries(self):
        assert sa.SYMBOL_RENAMES["SBILIFE"] == "SBILIFE"
        assert sa.SYMBOL_RENAMES["JETAIRWAYS"] == "JETAIRWAYS"

    def test_tables_are_uppercase(self):
        for table in (sa.SYMBOL_RENAMES, sa.KNOWN_DELISTED):
            assert all(k == k.upper() for k in table)
        assert all(v == v.upper() for v in sa.SYMBOL_RENAMES.values())
        assert all(s == s.upper() for s in sa.KNOWN_NOT_ON_NSE | sa.KNOWN_HIGH_PRICE_SYMBOLS)

    def test_no_rename_targets_a_non_nse_or_delisted_symbol(self):
        for target in sa.SYMBOL_RENAMES.values():
            assert target not in sa.KNOWN_NOT_ON_NSE
            assert target not in sa.KNOWN_DELISTED

    def test_no_rename_key_is_also_skip_listed(self):
        for key in sa.SYMBOL_RENAMES:
            assert key not in sa.KNOWN_NOT_ON_NSE
            assert key not in sa.KNOWN_DELISTED

    def test_rename_chains_terminate(self):
        for start in sa.SYMBOL_RENAMES:
            seen, cur = {start}, start
            while sa.SYMBOL_RENAMES.get(cur) and sa.SYMBOL_RENAMES[cur] != cur:
                cur = sa.SYMBOL_RENAMES[cur]
                assert cur not in seen, f"rename cycle through {start}"
                seen.add(cur)

    def test_tatamtrdvr_is_delisted_not_renamed(self):
        assert "TATAMTRDVR" in sa.KNOWN_DELISTED and "TATAMTRDVR" not in sa.SYMBOL_RENAMES

    @pytest.mark.parametrize("sym", ["AAKASH", "ANNAPURNA"])
    def test_aakash_and_annapurna_are_delisted_not_renamed(self, sym):
        assert sym in sa.KNOWN_DELISTED and sym not in sa.SYMBOL_RENAMES
        assert sa.KNOWN_DELISTED[sym]          # carries a human-readable reason

    def test_max_failure_streak(self):
        assert sa.MAX_FAILURE_STREAK == 5


# ── is_known_delisted ─────────────────────────────────────────────────────────

class TestIsKnownDelisted:
    @pytest.mark.parametrize("sym", ["TATAMTRDVR", "tatamtrdvr", " TATAMTRDVR.NS ", "TATAMTRDVR.BO",
                                     "AAKASH", "aakash.ns", "ANNAPURNA", " ANNAPURNA.BO "])
    def test_true(self, sym):
        assert sa.is_known_delisted(sym) is True

    @pytest.mark.parametrize("sym", ["TCS", "", None, "TATAMOTORS", "   "])
    def test_false(self, sym):
        assert sa.is_known_delisted(sym) is False


# ── is_non_equity_instrument ──────────────────────────────────────────────────

class TestIsNonEquityInstrument:
    @pytest.mark.parametrize("sym", [
        "ABC-RE", "ABC-RR", "ABC-PP", "ABC-P1", "ABC-P9", "ABC-R1", "ABC-R9", "ABC-W1", "ABC-W9",
        "ABC-WA", "ABC-E1", "ABC-E9", "ABC-N1", "ABC-N9", "ABC-NCD",
        "abc-re", "ABC-RE.NS", "ABC-PP.BO", " ABC-W2 ",
    ])
    def test_hyphenated_suffixes(self, sym):
        assert sa.is_non_equity_instrument(sym) is True

    @pytest.mark.parametrize("sym", [
        "APLAPOLLO29SEP26FUT", "ANGELONE29SEP26FUT", "BANKNIFTY29SEP2648000CE", "NIFTY26MAR2622500PE",
        "NIFTY26MAR2622500.5PE", "aplapollo29sep26fut", "APLAPOLLO29SEP26FUT.NS", "ABC01JAN27FUT",
        "ABC31DEC26100CE",
    ])
    def test_derivative_contracts(self, sym):
        assert sa.is_non_equity_instrument(sym) is True

    @pytest.mark.parametrize("month", ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT",
                                       "NOV", "DEC"])
    def test_every_contract_month(self, month):
        assert sa.is_non_equity_instrument(f"ABC15{month}26FUT") is True

    @pytest.mark.parametrize("sym", [
        "TCS", "BAJAJ-AUTO", "M&M", "RELIANCE", "RE", "ABC-P0", "ABC-W0", "ABC-N0", "ABC-E0", "ABC-R0",
        "ABC-REX", "ABC-NCDX", "ABC-WAX", "ABC-PPP", "NAM-INDIA", "360ONE", "ABC29SEP26", "ABC29SEP26FUTX",
        "ABC29XXX26FUT", "ABC29SEP2FUT", "ABC29SEP26CE",
    ])
    def test_legitimate_equities_are_kept(self, sym):
        assert sa.is_non_equity_instrument(sym) is False

    @pytest.mark.parametrize("sym", ["", None, "   ", ".NS", ".BO"])
    def test_empty_is_false(self, sym):
        assert sa.is_non_equity_instrument(sym) is False


# ── is_known_high_price ───────────────────────────────────────────────────────

class TestIsKnownHighPrice:
    @pytest.mark.parametrize("sym", ["MRF", "mrf", "MRF.NS", " MRF.BO ", "BAJAJ-AUTO", "ABB", "JSWHL", "OFSS",
                                     "ELCID", "GILLETTE", "APOLLOHOSP", "3MINDIA"])
    def test_true(self, sym):
        assert sa.is_known_high_price(sym) is True

    @pytest.mark.parametrize("sym", ["TCS", "", None, "MRFX", "YESBANK"])
    def test_false(self, sym):
        assert sa.is_known_high_price(sym) is False


# ── learned-store loaders ─────────────────────────────────────────────────────

class TestLoaders:
    def test_kv_helper_imports_kv_cache_lazily(self, kv):
        assert sa._kv() is sys.modules["kv_cache"]

    @pytest.mark.parametrize("loader, key", [(sa._load_learned_renames, RENAMES_KEY),
                                             (sa._load_learned_delisted, DELISTED_KEY)])
    def test_dict_is_returned(self, kv, loader, key):
        kv.store[key] = {"A": {"to": "B"}}
        assert loader() == {"A": {"to": "B"}}

    @pytest.mark.parametrize("loader", [sa._load_learned_renames, sa._load_learned_delisted])
    @pytest.mark.parametrize("value", [None, "x", [1], 5])
    def test_non_dict_becomes_empty(self, kv, loader, value):
        kv.store[RENAMES_KEY] = value
        kv.store[DELISTED_KEY] = value
        assert loader() == {}

    @pytest.mark.parametrize("loader", [sa._load_learned_renames, sa._load_learned_delisted])
    def test_kv_failure_becomes_empty(self, kv, loader):
        kv.get_raises = RuntimeError("redis down")
        assert loader() == {}

    @pytest.mark.parametrize("loader", [sa._load_learned_renames, sa._load_learned_delisted])
    def test_missing_kv_module_becomes_empty(self, monkeypatch, loader):
        monkeypatch.setitem(sys.modules, "kv_cache", None)
        assert loader() == {}


# ── is_learned_delisted ───────────────────────────────────────────────────────

class TestIsLearnedDelisted:
    def test_true_and_false(self, kv):
        kv.store[DELISTED_KEY] = {"DEAD": {"failures": 5}}
        assert sa.is_learned_delisted("DEAD") is True
        assert sa.is_learned_delisted("ALIVE") is False

    @pytest.mark.parametrize("sym", ["dead", "DEAD.NS", " dead.bo "])
    def test_symbol_is_normalised(self, kv, sym):
        kv.store[DELISTED_KEY] = {"DEAD": {}}
        assert sa.is_learned_delisted(sym) is True

    @pytest.mark.parametrize("sym", ["", None])
    def test_empty_is_false(self, kv, sym):
        kv.store[DELISTED_KEY] = {"DEAD": {}}
        assert sa.is_learned_delisted(sym) is False

    def test_kv_failure_is_false(self, kv):
        kv.get_raises = RuntimeError("x")
        assert sa.is_learned_delisted("DEAD") is False


# ── _apply_all_renames ────────────────────────────────────────────────────────

class TestApplyAllRenames:
    def test_static_rename(self, kv):
        assert sa._apply_all_renames("ZOMATO") == "ETERNAL"

    def test_untouched_symbol(self, kv):
        assert sa._apply_all_renames("TCS") == "TCS"

    def test_identity_entry_is_a_noop(self, kv):
        assert sa._apply_all_renames("SBILIFE") == "SBILIFE"

    def test_multi_hop_chain_is_followed_to_the_end(self, kv):
        assert sa._apply_all_renames("MINDTREE") == "LTM"       # MINDTREE -> LTIM -> LTM

    def test_learned_rename_is_applied(self, kv):
        kv.store[RENAMES_KEY] = {"OLDCO": {"to": "NEWCO"}}
        assert sa._apply_all_renames("OLDCO") == "NEWCO"

    def test_learned_rename_then_static_chain(self, kv):
        kv.store[RENAMES_KEY] = {"OLDCO": {"to": "ZOMATO"}}
        assert sa._apply_all_renames("OLDCO") == "ETERNAL"

    @pytest.mark.parametrize("entry", ["NEWCO", {"to": ""}, {"to": None}, {"source": "x"}, None, 5, ["NEWCO"]])
    def test_unusable_learned_entries_are_ignored(self, kv, entry):
        kv.store[RENAMES_KEY] = {"OLDCO": entry}
        assert sa._apply_all_renames("OLDCO") == "OLDCO"

    @pytest.mark.parametrize("entry", [{"to": 5}, {"to": "  "}])
    def test_non_string_or_blank_learned_target_is_ignored(self, kv, entry):
        kv.store[RENAMES_KEY] = {"OLDCO": entry}
        assert sa._apply_all_renames("OLDCO") == "OLDCO"

    def test_learned_target_is_stripped(self, kv):
        kv.store[RENAMES_KEY] = {"OLDCO": {"to": " NEWCO "}}
        assert sa._apply_all_renames("OLDCO") == "NEWCO"

    def test_cycle_is_broken(self, kv, monkeypatch):
        monkeypatch.setattr(sa, "SYMBOL_RENAMES", {"A": "B", "B": "A"})
        assert sa._apply_all_renames("A") == "B"

    def test_longer_cycle_is_broken(self, kv, monkeypatch):
        monkeypatch.setattr(sa, "SYMBOL_RENAMES", {"A": "B", "B": "C", "C": "A"})
        assert sa._apply_all_renames("A") == "C"

    def test_iteration_cap_bounds_a_pathological_table(self, kv, monkeypatch):
        # The guard is range(len(table) + 1). A well-formed dict can never exhaust it (each hop
        # consumes a distinct key), so fake a table that under-reports its length to prove the cap
        # itself stops the chase.
        class LyingDict(dict):
            def __len__(self):
                return 0

        monkeypatch.setattr(sa, "SYMBOL_RENAMES", LyingDict({"A": "B", "B": "C", "C": "D"}))
        assert sa._apply_all_renames("A") == "B"

    def test_kv_failure_falls_back_to_static_only(self, kv):
        kv.get_raises = RuntimeError("down")
        assert sa._apply_all_renames("ZOMATO") == "ETERNAL"


# ── resolve_ns_ticker / resolve_base_symbol ───────────────────────────────────

BOTH = pytest.mark.parametrize("fn, suffix", [(sa.resolve_ns_ticker, ".NS"), (sa.resolve_base_symbol, "")])


class TestResolve:
    @BOTH
    @pytest.mark.parametrize("sym", ["", None, "   ", ".NS", ".BO"])
    def test_empty_is_none(self, kv, fn, suffix, sym):
        assert fn(sym) is None

    @BOTH
    def test_plain_symbol(self, kv, fn, suffix):
        assert fn("TCS") == "TCS" + suffix

    @BOTH
    @pytest.mark.parametrize("raw", ["tcs", " tcs.ns ", "TCS.BO", "Tcs.Ns"])
    def test_normalisation(self, kv, fn, suffix, raw):
        assert fn(raw) == "TCS" + suffix

    @BOTH
    def test_hyphenated_equity_is_kept(self, kv, fn, suffix):
        assert fn("BAJAJ-AUTO") == "BAJAJ-AUTO" + suffix

    @BOTH
    @pytest.mark.parametrize("old, new", [("ZOMATO", "ETERNAL"), ("tatamotors.ns", "TMPV"), ("LTIM", "LTM"),
                                          ("MINDTREE", "LTM"), ("JUBILANT", "JUBLFOOD")])
    def test_static_renames(self, kv, fn, suffix, old, new):
        assert fn(old) == new + suffix

    @BOTH
    @pytest.mark.parametrize("sym", ["CISCO", "csco.ns", "AAPL", "NVDA"])
    def test_non_nse_symbols_are_skipped(self, kv, fn, suffix, sym):
        assert fn(sym) is None

    @BOTH
    def test_merged_away_symbol_is_skipped(self, kv, fn, suffix):
        assert fn("TATAMTRDVR") is None

    @BOTH
    @pytest.mark.parametrize("sym", ["AAKASH", "aakash.ns", "ANNAPURNA", "ANNAPURNA.BO"])
    def test_aakash_and_annapurna_are_skipped(self, kv, fn, suffix, sym):
        assert fn(sym) is None

    @BOTH
    @pytest.mark.parametrize("sym", ["ABC-RE", "ABC-W1", "ABC-NCD", "APLAPOLLO29SEP26FUT", "BANKNIFTY29SEP2648000CE"])
    def test_non_equity_instruments_are_skipped(self, kv, fn, suffix, sym):
        assert fn(sym) is None

    @BOTH
    def test_learned_delisted_is_skipped(self, kv, fn, suffix):
        kv.store[DELISTED_KEY] = {"DEADCO": {"failures": 5}}
        assert fn("deadco.ns") is None

    @BOTH
    def test_learned_rename_is_applied(self, kv, fn, suffix):
        kv.store[RENAMES_KEY] = {"OLDCO": {"to": "NEWCO"}}
        assert fn("OLDCO") == "NEWCO" + suffix

    @BOTH
    def test_learned_delisted_lookup_failure_does_not_block_resolution(self, kv, fn, suffix, monkeypatch):
        def boom(_s):
            raise RuntimeError("lookup broke")

        monkeypatch.setattr(sa, "is_learned_delisted", boom)
        assert fn("TCS") == "TCS" + suffix

    @BOTH
    def test_kv_down_still_resolves_static_names(self, kv, fn, suffix):
        kv.get_raises = RuntimeError("down")
        assert fn("ZOMATO") == "ETERNAL" + suffix

    def test_ns_ticker_and_base_symbol_agree(self, kv):
        kv.store[RENAMES_KEY] = {"OLDCO": {"to": "NEWCO"}}
        for s in ("TCS", "ZOMATO", "OLDCO", "MINDTREE", "CISCO", "ABC-RE", "TATAMTRDVR", "", None):
            ns, base = sa.resolve_ns_ticker(s), sa.resolve_base_symbol(s)
            assert (ns is None) == (base is None)
            if ns:
                assert ns == base + ".NS"


# ── learn_rename ──────────────────────────────────────────────────────────────

class TestLearnRename:
    def test_persists_the_rename(self, kv):
        sa.learn_rename("oldco.ns", " newco.bo ", source="unit")
        (value, ttl), = kv.sets_for(RENAMES_KEY)
        assert ttl is None
        entry = value["OLDCO"]
        assert entry["to"] == "NEWCO" and entry["source"] == "unit"
        assert entry["learned_at"].endswith("+00:00")

    def test_default_source_is_manual(self, kv):
        sa.learn_rename("A", "B")
        assert kv.store[RENAMES_KEY]["A"]["source"] == "manual"

    def test_existing_renames_are_preserved(self, kv):
        kv.store[RENAMES_KEY] = {"X": {"to": "Y"}}
        sa.learn_rename("A", "B")
        assert set(kv.store[RENAMES_KEY]) == {"X", "A"}

    def test_relearning_overwrites_the_target(self, kv):
        sa.learn_rename("A", "B")
        sa.learn_rename("A", "C")
        assert kv.store[RENAMES_KEY]["A"]["to"] == "C"

    @pytest.mark.parametrize("old, new", [("", "B"), ("A", ""), (None, "B"), ("A", None), ("A", "A"),
                                          ("a.ns", "A"), (".NS", "B")])
    def test_invalid_pairs_are_ignored(self, kv, old, new):
        sa.learn_rename(old, new)
        assert kv.sets == []

    def test_logs_the_rename(self, kv, caplog):
        with caplog.at_level(logging.INFO, logger="symbol-aliases"):
            sa.learn_rename("A", "B", source="s")
        assert any("learned rename A -> B (source=s)" in r.getMessage() for r in caplog.records)

    def test_resolution_picks_it_up_immediately(self, kv):
        sa.learn_rename("OLDCO", "NEWCO")
        assert sa.resolve_ns_ticker("OLDCO") == "NEWCO.NS"

    def test_clears_a_matching_learned_delisted_guess(self, kv):
        kv.store[DELISTED_KEY] = {"OLDCO": {"failures": 5}, "OTHER": {"failures": 5}}
        sa.learn_rename("OLDCO", "NEWCO")
        (value, ttl), = kv.sets_for(DELISTED_KEY)
        assert value == {"OTHER": {"failures": 5}} and ttl is None

    def test_leaves_the_delisted_store_alone_when_there_is_no_match(self, kv):
        kv.store[DELISTED_KEY] = {"OTHER": {"failures": 5}}
        sa.learn_rename("OLDCO", "NEWCO")
        assert kv.sets_for(DELISTED_KEY) == []

    def test_delisted_cleanup_failure_is_swallowed_and_rename_kept(self, kv):
        kv.store[DELISTED_KEY] = {"OLDCO": {"failures": 5}}
        kv.set_raises_for[DELISTED_KEY] = RuntimeError("delisted write failed")
        sa.learn_rename("OLDCO", "NEWCO")
        assert kv.store[RENAMES_KEY]["OLDCO"]["to"] == "NEWCO"

    def test_persist_failure_is_logged_not_raised(self, kv, caplog):
        kv.set_raises = RuntimeError("write boom")
        with caplog.at_level(logging.WARNING, logger="symbol-aliases"):
            sa.learn_rename("A", "B")
        assert any("could not persist learned rename A->B" in r.getMessage() and "write boom" in r.getMessage()
                   for r in caplog.records)

    def test_kv_read_failure_still_writes_the_new_entry(self, kv):
        kv.get_raises = RuntimeError("read boom")
        sa.learn_rename("A", "B")
        assert kv.store[RENAMES_KEY]["A"]["to"] == "B"


# ── record_resolution_failure ─────────────────────────────────────────────────

class TestRecordResolutionFailure:
    @pytest.mark.parametrize("sym", ["", None, "   ", ".NS"])
    def test_empty_symbol_is_zero_and_writes_nothing(self, kv, sym):
        assert sa.record_resolution_failure(sym) == 0
        assert kv.sets == []

    def test_first_failure(self, kv):
        assert sa.record_resolution_failure("dead.ns") == 1
        (value, ttl), = kv.sets_for(COUNTS_KEY)
        assert value == {"DEAD": 1} and ttl == 30 * 86400

    def test_counts_accumulate_per_symbol(self, kv):
        assert [sa.record_resolution_failure("A") for _ in range(3)] == [1, 2, 3]
        assert sa.record_resolution_failure("B") == 1
        assert kv.store[COUNTS_KEY] == {"A": 3, "B": 1}

    @pytest.mark.parametrize("bad", ["junk", [1], 5, None])
    def test_non_dict_counts_store_restarts_the_streak(self, kv, bad):
        kv.store[COUNTS_KEY] = bad
        assert sa.record_resolution_failure("A") == 1

    def test_below_the_streak_nothing_is_marked_delisted(self, kv):
        for _ in range(sa.MAX_FAILURE_STREAK - 1):
            sa.record_resolution_failure("DEAD")
        assert kv.sets_for(DELISTED_KEY) == []
        assert sa.is_learned_delisted("DEAD") is False

    def test_reaching_the_streak_marks_it_delisted(self, kv, caplog):
        with caplog.at_level(logging.INFO, logger="symbol-aliases"):
            for _ in range(sa.MAX_FAILURE_STREAK):
                n = sa.record_resolution_failure("DEAD")
        assert n == 5
        (value, ttl), = kv.sets_for(DELISTED_KEY)
        assert ttl is None
        assert value["DEAD"]["failures"] == 5 and value["DEAD"]["marked_at"].endswith("+00:00")
        assert sa.is_learned_delisted("DEAD") is True
        assert any("DEAD hit 5 consecutive resolution failures" in r.getMessage() for r in caplog.records)

    def test_failures_past_the_streak_keep_the_mark_fresh(self, kv):
        kv.store[COUNTS_KEY] = {"DEAD": 5}
        assert sa.record_resolution_failure("DEAD") == 6
        assert kv.store[DELISTED_KEY]["DEAD"]["failures"] == 6

    def test_existing_delisted_entries_survive(self, kv):
        kv.store[DELISTED_KEY] = {"OLDDEAD": {"failures": 9}}
        kv.store[COUNTS_KEY] = {"DEAD": 4}
        sa.record_resolution_failure("DEAD")
        assert set(kv.store[DELISTED_KEY]) == {"OLDDEAD", "DEAD"}

    def test_a_marked_symbol_stops_resolving(self, kv):
        kv.store[COUNTS_KEY] = {"DEAD": 4}
        sa.record_resolution_failure("DEAD")
        assert sa.resolve_ns_ticker("DEAD") is None

    @pytest.mark.parametrize("bad", ["junk", None, [1]])
    def test_unparseable_stored_count_returns_zero(self, kv, bad):
        kv.store[COUNTS_KEY] = {"DEAD": bad}
        assert sa.record_resolution_failure("DEAD") == 0

    def test_write_failure_returns_zero(self, kv):
        kv.set_raises = RuntimeError("write boom")
        assert sa.record_resolution_failure("DEAD") == 0

    def test_read_failure_returns_zero_and_writes_nothing(self, kv):
        kv.get_raises = RuntimeError("read boom")
        assert sa.record_resolution_failure("DEAD") == 0
        assert kv.sets == []

    def test_delisted_write_failure_returns_zero(self, kv):
        kv.store[COUNTS_KEY] = {"DEAD": 4}
        kv.set_raises_for[DELISTED_KEY] = RuntimeError("boom")
        assert sa.record_resolution_failure("DEAD") == 0


# ── clear_resolution_failures ─────────────────────────────────────────────────

class TestClearResolutionFailures:
    def test_removes_only_that_symbol(self, kv):
        kv.store[COUNTS_KEY] = {"A": 3, "B": 2}
        sa.clear_resolution_failures("a.ns")
        (value, ttl), = kv.sets_for(COUNTS_KEY)
        assert value == {"B": 2} and ttl == 30 * 86400

    def test_unknown_symbol_writes_nothing(self, kv):
        kv.store[COUNTS_KEY] = {"A": 3}
        sa.clear_resolution_failures("Z")
        assert kv.sets == []

    @pytest.mark.parametrize("bad", [None, "junk", [1], 5])
    def test_non_dict_store_is_a_noop(self, kv, bad):
        kv.store[COUNTS_KEY] = bad
        sa.clear_resolution_failures("A")
        assert kv.sets == []

    @pytest.mark.parametrize("fail", ["get_raises", "set_raises"])
    def test_kv_failure_is_swallowed(self, kv, fail):
        kv.store[COUNTS_KEY] = {"A": 3}
        setattr(kv, fail, RuntimeError("boom"))
        sa.clear_resolution_failures("A")

    def test_clearing_lets_the_streak_restart(self, kv):
        for _ in range(3):
            sa.record_resolution_failure("A")
        sa.clear_resolution_failures("A")
        assert sa.record_resolution_failure("A") == 1

    def test_empty_symbol_is_a_noop(self, kv):
        kv.store[COUNTS_KEY] = {"A": 3}
        sa.clear_resolution_failures(None)
        assert kv.sets == []


# ── try_discover_rename ───────────────────────────────────────────────────────

def _notice(subject=None, desc=None, attachment=None):
    row = {}
    if subject is not None:
        row["subject"] = subject
    if desc is not None:
        row["desc"] = desc
    if attachment is not None:
        row["attachment"] = attachment
    return row


class TestTryDiscoverRenameGuards:
    @pytest.mark.parametrize("sym", ["", None, "   ", ".NS"])
    def test_empty_symbol(self, kv, http, sym):
        assert sa.try_discover_rename(sym) is None
        assert http.calls == []

    @pytest.mark.parametrize("sym", ["CISCO", "aapl.ns"])
    def test_known_non_nse_symbols_never_hit_the_network(self, kv, http, sym):
        assert sa.try_discover_rename(sym) is None
        assert http.calls == [] and http.client_kwargs == []

    def test_missing_httpx_is_none(self, kv, monkeypatch):
        monkeypatch.setitem(sys.modules, "httpx", None)
        assert sa.try_discover_rename("OLDCO") is None


class TestTryDiscoverRenameRequests:
    def test_client_configuration_and_two_requests(self, kv, http):
        sa.try_discover_rename("oldco.ns", timeout=7.5)
        (kw,) = http.client_kwargs
        assert kw == {"timeout": 7.5, "headers": sa._NSE_HEADERS, "follow_redirects": True}
        assert http.calls == [
            ("https://www.nseindia.com", None, 7.5),
            ("https://www.nseindia.com/api/corporate-announcements",
             {"index": "equities", "symbol": "OLDCO"}, 7.5),
        ]

    def test_default_timeout_is_ten_seconds(self, kv, http):
        sa.try_discover_rename("OLDCO")
        assert http.client_kwargs[0]["timeout"] == 10.0
        assert all(c[2] == 10.0 for c in http.calls)

    def test_cookie_handshake_failure_is_ignored(self, kv, http):
        http.home_raises = RuntimeError("handshake blocked")
        http.api = FakeResp(200, [_notice("Change of Symbol to NEWCO")])
        assert sa.try_discover_rename("OLDCO") == "NEWCO"

    @pytest.mark.parametrize("status", [403, 404, 429, 500, 204])
    def test_non_200_is_none_and_logged(self, kv, http, status, caplog):
        http.api = FakeResp(status, [_notice("Change of Symbol to NEWCO")])
        with caplog.at_level(logging.INFO, logger="symbol-aliases"):
            assert sa.try_discover_rename("OLDCO") is None
        assert any(f"HTTP {status}" in r.getMessage() for r in caplog.records)
        assert kv.sets == []

    def test_api_request_failure_is_none(self, kv, http, caplog):
        http.api_raises = RuntimeError("timed out")
        with caplog.at_level(logging.INFO, logger="symbol-aliases"):
            assert sa.try_discover_rename("OLDCO") is None
        assert any("try_discover_rename(OLDCO) failed" in r.getMessage() and "timed out" in r.getMessage()
                   for r in caplog.records)

    def test_bad_json_is_none(self, kv, http):
        http.api = FakeResp(200, ValueError("not json"))
        assert sa.try_discover_rename("OLDCO") is None

    def test_data_key_of_a_dict_body_is_used(self, kv, http):
        http.api = FakeResp(200, {"data": [_notice("Change of Symbol to NEWCO")]})
        assert sa.try_discover_rename("OLDCO") == "NEWCO"

    @pytest.mark.parametrize("body", [[], {}, {"data": None}, {"data": []}, {"other": [1]}])
    def test_no_rows_is_none(self, kv, http, body):
        http.api = FakeResp(200, body)
        assert sa.try_discover_rename("OLDCO") is None


class TestTryDiscoverRenameParsing:
    def _run(self, http, rows, sym="OLDCO"):
        http.api = FakeResp(200, rows)
        return sa.try_discover_rename(sym)

    @pytest.mark.parametrize("subject, expected", [
        ("Change of Symbol to NEWCO", "NEWCO"),
        ("CHANGE OF TRADING SYMBOL - NEWCO", "NEWCO"),
        ("Change of Symbol from OLDCO to NEWZ", "NEWZ"),
        ("Company name change; New Symbol: NEWD", "NEWD"),
        ("New Symbol - NEWE", "NEWE"),
        ("Revised Symbol NEWF", "NEWF"),
        ("Revised Symbol: NEW-G", "NEW-G"),
        ("Change of Symbol to M&NEW", "M&NEW"),
    ])
    def test_notice_wordings(self, kv, http, subject, expected):
        assert self._run(http, [_notice(subject)]) == expected

    def test_desc_is_used_when_subject_is_missing(self, kv, http):
        assert self._run(http, [_notice(desc="Change of Symbol to NEWCO")]) == "NEWCO"

    def test_desc_is_used_when_subject_is_blank(self, kv, http):
        assert self._run(http, [{"subject": "", "desc": "Change of Symbol to NEWCO"}]) == "NEWCO"

    def test_subject_beats_desc(self, kv, http):
        row = _notice("Change of Symbol to FROMSUBJ", desc="Change of Symbol to FROMDESC")
        assert self._run(http, [row]) == "FROMSUBJ"

    def test_attachment_text_is_searched_too(self, kv, http):
        assert self._run(http, [_notice("Intimation", attachment="Revised Symbol NEWA")]) == "NEWA"

    def test_none_attachment_is_fine(self, kv, http):
        assert self._run(http, [{"subject": "Change of Symbol to NEWCO", "attachment": None}]) == "NEWCO"

    def test_a_notice_without_subject_or_desc_is_skipped(self, kv, http):
        assert self._run(http, [{}, {"subject": None, "desc": None, "attachment": None}]) is None

    def test_unrelated_notices_are_skipped(self, kv, http):
        rows = [_notice("Board meeting outcome, symbol and name unchanged"), _notice("Dividend declared")]
        assert self._run(http, rows) is None

    def test_change_without_symbol_or_name_is_skipped(self, kv, http):
        assert self._run(http, [_notice("Change in dividend record date")]) is None

    def test_name_only_notice_passes_the_filter_but_has_no_pattern(self, kv, http):
        assert self._run(http, [_notice("Change of Name to NewCo Limited")]) is None

    def test_symbol_notice_matching_no_pattern_is_skipped(self, kv, http):
        assert self._run(http, [_notice("Change in symbol under review")]) is None

    def test_same_symbol_is_not_a_rename(self, kv, http):
        assert self._run(http, [_notice("Change of Symbol to OLDCO")]) is None
        assert kv.sets == []

    def test_non_nse_target_is_rejected(self, kv, http):
        assert self._run(http, [_notice("Change of Symbol to CISCO")]) is None
        assert kv.sets == []

    def test_edge_hyphens_are_stripped_from_the_target(self, kv, http):
        assert self._run(http, [_notice("Revised Symbol: ABC-")]) == "ABC"

    def test_target_that_is_only_punctuation_is_rejected(self, kv, http):
        assert self._run(http, [_notice("Revised Symbol: &-")]) is None

    def test_scanning_continues_past_rejected_notices(self, kv, http):
        rows = [_notice("Change of Symbol to OLDCO"), _notice("Change of Symbol to CISCO"),
                _notice("Dividend"), _notice("Change of Symbol to NEWCO")]
        assert self._run(http, rows) == "NEWCO"

    def test_first_qualifying_notice_wins(self, kv, http):
        rows = [_notice("Change of Symbol to FIRST"), _notice("Change of Symbol to SECOND")]
        assert self._run(http, rows) == "FIRST"
        assert kv.store[RENAMES_KEY]["OLDCO"]["to"] == "FIRST"

    def test_a_hit_is_learned_durably_with_its_source(self, kv, http):
        assert self._run(http, [_notice("Change of Symbol to NEWCO")], sym="oldco.ns") == "NEWCO"
        entry = kv.store[RENAMES_KEY]["OLDCO"]
        assert entry["to"] == "NEWCO" and entry["source"] == "nse_corp_announcements"
        assert kv.sets_for(RENAMES_KEY)[0][1] is None
        assert sa.resolve_ns_ticker("OLDCO") == "NEWCO.NS"      # zero further network calls needed

    def test_no_hit_learns_nothing(self, kv, http):
        assert self._run(http, [_notice("Dividend")]) is None
        assert kv.sets == []


# ── resolve_with_fallback ─────────────────────────────────────────────────────

class TestResolveWithFallback:
    @pytest.mark.parametrize("sym", ["", None, "   "])
    def test_empty(self, kv, http, sym):
        assert sa.resolve_with_fallback(sym) == (None, {"resolution": "empty"})

    @pytest.mark.parametrize("sym", ["CISCO", "csco.ns"])
    def test_not_nse(self, kv, http, sym):
        assert sa.resolve_with_fallback(sym) == (None, {"resolution": "skip_not_nse"})

    def test_merged_delisted_carries_the_detail(self, kv, http):
        t, info = sa.resolve_with_fallback("tatamtrdvr.ns")
        assert t is None
        assert info == {"resolution": "skip_delisted_merged", "detail": sa.KNOWN_DELISTED["TATAMTRDVR"]}

    @pytest.mark.parametrize("sym", ["AAKASH", "annapurna.ns", "ANNAPURNA.BO"])
    def test_aakash_annapurna_skip_without_a_network_call(self, kv, http, sym):
        t, info = sa.resolve_with_fallback(sym)
        base = sym.upper().replace(".NS", "").replace(".BO", "")
        assert t is None
        assert info == {"resolution": "skip_delisted_merged", "detail": sa.KNOWN_DELISTED[base]}
        assert http.calls == []

    @pytest.mark.parametrize("sym", ["ABC-RE", "APLAPOLLO29SEP26FUT"])
    def test_non_equity(self, kv, http, sym):
        assert sa.resolve_with_fallback(sym) == (None, {"resolution": "skip_non_equity"})

    def test_learned_delisted(self, kv, http):
        kv.store[DELISTED_KEY] = {"DEADCO": {}}
        assert sa.resolve_with_fallback("DEADCO") == (None, {"resolution": "skip_delisted"})

    def test_static_rename(self, kv, http):
        assert sa.resolve_with_fallback("zomato") == ("ETERNAL.NS", {"resolution": "static_rename", "to": "ETERNAL"})
        assert http.calls == []

    def test_learned_rename(self, kv, http):
        kv.store[RENAMES_KEY] = {"OLDCO": {"to": "NEWCO"}}
        assert sa.resolve_with_fallback("OLDCO") == ("NEWCO.NS", {"resolution": "learned_rename", "to": "NEWCO"})
        assert http.calls == []

    def test_static_multi_hop_returns_the_final_ticker(self, kv, http):
        # MINDTREE -> LTIM -> LTM. Used to stop at LTIM, disagreeing with resolve_ns_ticker().
        assert sa.resolve_with_fallback("mindtree") == ("LTM.NS", {"resolution": "static_rename", "to": "LTM"})
        assert sa.resolve_with_fallback("MINDTREE")[0] == sa.resolve_ns_ticker("MINDTREE")
        assert http.calls == []

    def test_one_hop_static_rename_is_unchanged(self, kv, http):
        assert sa.resolve_with_fallback("LTIM") == ("LTM.NS", {"resolution": "static_rename", "to": "LTM"})

    def test_identity_static_entry_reports_itself(self, kv, http):
        assert sa.resolve_with_fallback("SBILIFE") == ("SBILIFE.NS", {"resolution": "static_rename", "to": "SBILIFE"})

    def test_learned_rename_is_chased_through_the_static_chain(self, kv, http):
        kv.store[RENAMES_KEY] = {"OLDCO": {"to": "MINDTREE"}}
        assert sa.resolve_with_fallback("OLDCO") == ("LTM.NS", {"resolution": "learned_rename", "to": "LTM"})
        assert sa.resolve_with_fallback("OLDCO")[0] == sa.resolve_ns_ticker("OLDCO")

    def test_static_cycle_terminates(self, kv, http, monkeypatch):
        monkeypatch.setattr(sa, "SYMBOL_RENAMES", {"A": "B", "B": "A"})
        assert sa.resolve_with_fallback("A") == ("B.NS", {"resolution": "static_rename", "to": "B"})

    @pytest.mark.parametrize("entry", ["NEWCO", {"to": ""}, {"to": "  "}, {"to": None}, {"to": 5}, {"source": "x"},
                                       None, 5, ["NEWCO"]])
    def test_malformed_learned_entry_is_ignored_not_raised(self, kv, http, entry):
        # A bare string / list / None / number used to raise AttributeError on .get("to").
        kv.store[RENAMES_KEY] = {"OLDCO": entry}
        http.api = FakeResp(200, [_notice("Change of Symbol to NEWCO")])
        assert sa.resolve_with_fallback("OLDCO") == ("NEWCO.NS", {"resolution": "discovered_rename", "to": "NEWCO"})

    def test_malformed_learned_entry_with_no_discovery_ends_unresolved(self, kv, http):
        kv.store[RENAMES_KEY] = {"OLDCO": "NEWCO"}
        t, info = sa.resolve_with_fallback("OLDCO")
        assert t == "OLDCO.NS" and info["resolution"] == "unresolved"

    def test_learned_target_is_stripped(self, kv, http):
        kv.store[RENAMES_KEY] = {"OLDCO": {"to": " NEWCO "}}
        assert sa.resolve_with_fallback("OLDCO") == ("NEWCO.NS", {"resolution": "learned_rename", "to": "NEWCO"})

    def test_learned_entry_without_a_target_falls_through_to_discovery(self, kv, http):
        kv.store[RENAMES_KEY] = {"OLDCO": {"source": "x"}}
        http.api = FakeResp(200, [_notice("Change of Symbol to NEWCO")])
        assert sa.resolve_with_fallback("OLDCO") == ("NEWCO.NS", {"resolution": "discovered_rename", "to": "NEWCO"})

    def test_discovered_rename_is_learned_for_next_time(self, kv, http):
        http.api = FakeResp(200, [_notice("Change of Symbol to NEWCO")])
        sa.resolve_with_fallback("OLDCO")
        http.calls.clear()
        assert sa.resolve_with_fallback("OLDCO")[1]["resolution"] == "learned_rename"
        assert http.calls == []

    def test_discovery_result_is_used(self, kv, monkeypatch):
        monkeypatch.setattr(sa, "try_discover_rename", lambda s: "FOUND")
        assert sa.resolve_with_fallback("X1") == ("FOUND.NS", {"resolution": "discovered_rename", "to": "FOUND"})

    def test_unresolved_bumps_the_failure_streak(self, kv, http):
        t, info = sa.resolve_with_fallback("mystery.ns")
        assert t == "MYSTERY.NS"
        assert info == {"resolution": "unresolved", "failure_streak": 1}
        assert kv.store[COUNTS_KEY] == {"MYSTERY": 1}

    def test_repeated_failures_end_in_a_skip(self, kv, http):
        streaks = []
        for _ in range(sa.MAX_FAILURE_STREAK):
            t, info = sa.resolve_with_fallback("MYSTERY")
            assert t == "MYSTERY.NS"
            streaks.append(info["failure_streak"])
        assert streaks == [1, 2, 3, 4, 5]
        assert sa.resolve_with_fallback("MYSTERY") == (None, {"resolution": "skip_delisted"})

    def test_a_skipped_symbol_makes_no_network_call(self, kv, http):
        kv.store[DELISTED_KEY] = {"DEADCO": {}}
        sa.resolve_with_fallback("DEADCO")
        assert http.calls == []

    def test_unresolved_when_kv_is_down_reports_streak_zero(self, kv, http):
        kv.get_raises = RuntimeError("down")
        kv.set_raises = RuntimeError("down")
        assert sa.resolve_with_fallback("MYSTERY") == ("MYSTERY.NS", {"resolution": "unresolved",
                                                                       "failure_streak": 0})

    def test_precedence_not_nse_beats_non_equity(self, kv, http):
        assert sa.resolve_with_fallback("CISCO")[1]["resolution"] == "skip_not_nse"

    def test_precedence_static_beats_learned(self, kv, http):
        kv.store[RENAMES_KEY] = {"ZOMATO": {"to": "SOMETHINGELSE"}}
        assert sa.resolve_with_fallback("ZOMATO")[0] == "ETERNAL.NS"

    def test_precedence_learned_delisted_beats_static_rename(self, kv, http):
        kv.store[DELISTED_KEY] = {"ZOMATO": {}}
        assert sa.resolve_with_fallback("ZOMATO") == (None, {"resolution": "skip_delisted"})


# ── drift guards against the collaborators ───────────────────────────────────

def _parse(path):
    with open(path, encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _top_level_names(tree):
    names = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.Assign):
            names |= {t.id for t in n.targets if isinstance(t, ast.Name)}
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            names.add(n.target.id)
    return names


def _literal_dict(tree, name):
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in n.targets):
            return ast.literal_eval(n.value)
    raise AssertionError(f"{name} not found")


class TestDrift:
    def test_every_name_the_gateway_imports_from_this_module_exists(self):
        defined = _top_level_names(_parse(os.path.join(_SERVICE, "symbol_aliases.py")))
        wanted = set()
        for fname in os.listdir(_SERVICE):
            if not fname.endswith(".py") or fname == "symbol_aliases.py":
                continue
            for node in ast.walk(_parse(os.path.join(_SERVICE, fname))):
                if isinstance(node, ast.ImportFrom) and node.module == "symbol_aliases":
                    wanted |= {a.name for a in node.names}
        assert {"resolve_ns_ticker", "resolve_base_symbol", "is_known_delisted", "is_known_high_price",
                "is_learned_delisted", "resolve_with_fallback", "MAX_FAILURE_STREAK",
                "is_non_equity_instrument"} <= wanted
        assert wanted <= defined

    def test_attributes_rate_limiter_uses_exist(self):
        src = open(os.path.join(_SERVICE, "rate_limiter.py"), encoding="utf-8").read()
        used = set(re.findall(r"\bsa\.([A-Za-z_]+)", src))
        assert {"is_learned_delisted", "is_known_high_price", "try_discover_rename",
                "clear_resolution_failures", "record_resolution_failure"} <= used
        assert all(hasattr(sa, name) for name in used)

    def test_resolution_labels_main_reads_are_produced_by_the_module(self):
        # main.py reports info.get("resolution"); every label the module can emit must be a plain str
        labels = set()
        for node in ast.walk(_parse(os.path.join(_SERVICE, "symbol_aliases.py"))):
            if isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    if isinstance(k, ast.Constant) and k.value == "resolution" and isinstance(v, ast.Constant):
                        labels.add(v.value)
        assert labels == {"empty", "skip_not_nse", "skip_delisted_merged", "skip_non_equity", "skip_delisted",
                          "static_rename", "learned_rename", "discovered_rename", "unresolved"}

    def test_real_kv_cache_signatures_match_the_calls(self):
        spec = importlib.util.spec_from_file_location("kv_cache_for_alias_drift",
                                                      os.path.join(_SERVICE, "kv_cache.py"))
        real = importlib.util.module_from_spec(spec)
        sys.modules["kv_cache_for_alias_drift"] = real
        try:
            spec.loader.exec_module(real)
        finally:
            sys.modules.pop("kv_cache_for_alias_drift", None)
        inspect.signature(real.kv_get).bind("key")
        inspect.signature(real.kv_set).bind("key", {"a": 1}, ttl=None)
        inspect.signature(real.kv_set).bind("key", {"a": 1}, ttl=30 * 86400)

    def test_rename_table_agrees_with_market_data_service(self):
        path = os.path.join(os.path.dirname(_SERVICE), "market-data-service", "main.py")
        if not os.path.isfile(path):
            pytest.skip("market-data-service not present")
        smart = _literal_dict(_parse(path), "SMART_SYMBOL_MAP")
        for old, new in sa.SYMBOL_RENAMES.items():
            if old == new:
                continue                                   # explicit identity entries are gateway-only
            assert smart.get(old) == new, f"{old}: gateway -> {new}, market-data -> {smart.get(old)}"

    def test_market_data_targets_never_disagree_with_the_gateway(self):
        path = os.path.join(os.path.dirname(_SERVICE), "market-data-service", "main.py")
        if not os.path.isfile(path):
            pytest.skip("market-data-service not present")
        smart = _literal_dict(_parse(path), "SMART_SYMBOL_MAP")
        for old, new in smart.items():
            if old in sa.SYMBOL_RENAMES:
                assert sa.SYMBOL_RENAMES[old] == new
