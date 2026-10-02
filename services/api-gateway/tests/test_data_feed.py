"""tests/test_data_feed.py — coverage for api-gateway/data_feed.py

The durable data-feed store (per-symbol payloads, index, meta, job progress), the hot-picks job
helpers, refresh locks, the bulk price feeders (market-data-service bulk quotes + NSE bhavcopy),
the shared bulk-quote cache and the price-alert store.

No network, no real KV: `kv_cache` and `httpx` are replaced by small fakes in sys.modules (the
module imports both lazily, inside functions). Every test starts from clean module globals.

Run from services/api-gateway:
    python3 -m pytest tests/test_data_feed.py -v
"""
from __future__ import annotations

import copy
import sys
import types
from datetime import datetime, timedelta

import pytest

import data_feed as df


# ── fakes ─────────────────────────────────────────────────────────────────────

class FakeKV:
    """In-memory stand-in for kv_cache. Returns deep copies, like a real round trip."""

    def __init__(self):
        self.store = {}
        self.sets = []              # (key, value, ttl)
        self.deleted = []
        self.get_raises = None
        self.set_raises = None
        self.get_raises_for = {}    # key -> exception
        self.set_raises_for = {}    # key -> exception
        self.delete_raises = None
        self.many_gets = []

    def module(self, with_many=True, with_set_many=True, get_many_raises=None,
               set_many_raises=None, legacy_names=False):
        kv = self
        m = types.ModuleType("kv_cache")

        def kv_get(key):
            if key in kv.get_raises_for:
                raise kv.get_raises_for[key]
            if kv.get_raises is not None:
                raise kv.get_raises
            return copy.deepcopy(kv.store.get(key))

        def kv_set(key, value, ttl=None):
            if key in kv.set_raises_for:
                raise kv.set_raises_for[key]
            if kv.set_raises is not None:
                raise kv.set_raises
            kv.sets.append((key, copy.deepcopy(value), ttl))
            kv.store[key] = copy.deepcopy(value)

        def kv_delete(key):
            if kv.delete_raises is not None:
                raise kv.delete_raises
            kv.deleted.append(key)
            kv.store.pop(key, None)

        def get_many(keys):
            kv.many_gets.append(list(keys))
            if get_many_raises is not None:
                raise get_many_raises
            return {k: copy.deepcopy(kv.store[k]) for k in keys if k in kv.store}

        def set_many(items, ttl=None):
            if set_many_raises is not None:
                raise set_many_raises
            for k, v in items.items():
                kv.sets.append((k, copy.deepcopy(v), ttl))
                kv.store[k] = copy.deepcopy(v)

        m.kv_get, m.kv_set, m.kv_delete = kv_get, kv_set, kv_delete
        if with_many:
            if legacy_names:
                m.kv_get_many = get_many
            else:
                m.get_many = get_many
        if with_set_many:
            if legacy_names:
                m.kv_set_many = set_many
            else:
                m.set_many = set_many
        return m


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    df._LOCAL_SYMBOLS.clear()
    df._LOCAL_META.clear()
    df._LOCAL_JOB.clear()
    df._LOCAL_INDEX.clear()
    df._MEM_LOCKS.clear()
    df._INDEX_WARMED = False
    df._feed_store_singleton = None
    df._BHAV_CACHE["data"] = None
    df._BHAV_CACHE["fetched_at"] = 0.0
    df._DATA_FEED_STOP_FLAG.clear()
    monkeypatch.setattr(df, "MAX_STOCK_PRICE", 0.0)
    yield
    df._LOCAL_SYMBOLS.clear()
    df._LOCAL_META.clear()
    df._LOCAL_JOB.clear()
    df._LOCAL_INDEX.clear()
    df._MEM_LOCKS.clear()
    df._INDEX_WARMED = False
    df._feed_store_singleton = None
    df._BHAV_CACHE["data"] = None
    df._BHAV_CACHE["fetched_at"] = 0.0
    df._DATA_FEED_STOP_FLAG.clear()


@pytest.fixture
def kv(monkeypatch):
    fake = FakeKV()
    monkeypatch.setitem(sys.modules, "kv_cache", fake.module())
    return fake


@pytest.fixture
def store(kv):
    return df.DataFeedStore(sys.modules["kv_cache"].kv_get, sys.modules["kv_cache"].kv_set)


def rich(**kw):
    base = {"symbol": "TCS", "price": 100.0, "sector": "IT", "pe_ratio": 20.0}
    base.update(kw)
    return base


# ── stop flag / local cache reset ────────────────────────────────────────────

class TestStopFlagAndReset:
    def test_stop_flag_roundtrip(self):
        assert df.data_feed_stop_requested() is False
        df.request_data_feed_stop()
        assert df.data_feed_stop_requested() is True
        df.clear_data_feed_stop()
        assert df.data_feed_stop_requested() is False

    def test_clear_local_caches_wipes_everything(self):
        df._LOCAL_SYMBOLS["k"] = {"a": 1}
        df._LOCAL_META["m"] = 1
        df._LOCAL_JOB["j"] = 1
        df._LOCAL_INDEX.add("X")
        df._INDEX_WARMED = True
        df.request_data_feed_stop()
        df.clear_local_data_feed_caches()
        assert not df._LOCAL_SYMBOLS and not df._LOCAL_META
        assert not df._LOCAL_JOB and not df._LOCAL_INDEX
        assert df._INDEX_WARMED is False
        assert df.data_feed_stop_requested() is False


# ── pure helpers ─────────────────────────────────────────────────────────────

class TestSmallHelpers:
    @pytest.mark.parametrize("raw,expected", [
        ("reliance.ns", "RELIANCE"), ("TCS.BO", "TCS"), ("  infy ", "INFY"),
        ("", ""), (None, ""), ("abc", "ABC"),
    ])
    def test_norm_sym(self, raw, expected):
        assert df._norm_sym(raw) == expected

    def test_now_iso_is_ist(self):
        s = df._now_iso()
        assert s.endswith("+05:30")
        datetime.fromisoformat(s)

    @pytest.mark.parametrize("raw,expected", [
        (None, 0.0), ("", 0.0), (5, 5.0), (5.5, 5.5), (0, 0.0), (-3, 0.0),
        (float("nan"), 0.0), ("5,123.45", 5123.45), ("1,20,000", 120000.0),
        ("\u00a012.5 ", 12.5), ("-", 0.0), ("NA", 0.0), ("n/a", 0.0), ("None", 0.0),
        ("null", 0.0), ("   ", 0.0), ("abc", 0.0), ("-4", 0.0), ("nan", 0.0),
        (object(), 0.0),
    ])
    def test_coerce_price(self, raw, expected):
        assert df._coerce_price(raw) == expected

    def test_payload_price_flat_order(self):
        assert df._payload_price({"close": 10, "price": 5}) == 5.0
        assert df._payload_price({"close": 10}) == 10.0
        assert df._payload_price({"prev_close": "7"}) == 7.0

    def test_payload_price_nested(self):
        assert df._payload_price({"metrics": {"ltp": 9}}) == 9.0
        assert df._payload_price({"quote": {"cmp": "1,000"}}) == 1000.0
        assert df._payload_price({"metrics": "notadict", "data": {"close": 3}}) == 3.0

    def test_payload_price_none_found(self):
        assert df._payload_price({"price": 0, "metrics": {"price": 0}}) == 0.0
        assert df._payload_price({}) == 0.0

    @pytest.mark.parametrize("bad", [None, [], "x", 3])
    def test_payload_price_non_dict(self, bad):
        assert df._payload_price(bad) == 0.0

    def test_strip_none_fields(self):
        assert df.strip_none_fields({"a": 1, "b": None, "c": 0}) == {"a": 1, "c": 0}
        assert df.strip_none_fields(None) == {}
        assert df.strip_none_fields([1]) == {}

    @pytest.mark.parametrize("payload,expected", [
        (None, False), ({}, False), ("x", False), ({"junk": 1}, False),
        ({"fundamental_score": 0}, True), ({"metrics": {"a": 1}}, True),
        ({"sector": "IT"}, True), ({"valuation": "cheap"}, True),
        ({"quality_score": 0}, True), ({"multi_quarter_score": 0}, True),
        ({"event_summary": "x"}, True), ({"bulk_deals": [1]}, True),
        ({"recent_insider_transactions": [1]}, True), ({"earnings_surprise": 0}, True),
        ({"next_earnings_date": "2026-01-01"}, True), ({"close": 0}, True),
        ({"price": 0}, True), ({"ltp": 0}, True), ({"combined_score": 0}, True),
        ({"technical_score": 0}, True), ({"decision": "BUY"}, True),
        ({"metrics": {}, "sector": None}, False),
    ])
    def test_payload_is_useful(self, payload, expected):
        assert df._payload_is_useful(payload) is expected

    def test_feed_key_helpers(self):
        assert df.feed_key("tcs.ns") == "stockky:data_feed:sym:TCS"
        assert df.feed_alias_key("tcs") == "feed:TCS"
        assert df.feed_legacy_key("TCS.BO") == "data_feed:TCS"


class TestNormalizeFeedPayload:
    def test_non_dict(self):
        assert df.normalize_feed_payload(None) == {}
        assert df.normalize_feed_payload([1]) == {}

    def test_key_mapping_and_numeric_coercion(self):
        out = df.normalize_feed_payload({
            "Price": "1,234.5", "P/E": 18, "Tech Score": 55, "ROCE": " 12 ",
            "Sector": "Auto", "52W_High": 200, "change%": "-1.5", "LTP": 9,
        })
        assert out["prev_close"] == 1234.5
        assert out["pe_ratio"] == 18.0 and isinstance(out["pe_ratio"], float)
        assert out["technical_score"] == 55.0
        assert out["roce"] == 12.0
        assert out["sector"] == "Auto"
        assert out["high_52w"] == 200.0
        assert out["change_pct"] == -1.5
        assert out["last_price"] == 9.0

    def test_unicode_digit_string_that_float_rejects_is_kept_verbatim(self):
        # "²".isdigit() is True but float("²") raises ValueError -> the except branch keeps it
        assert df.normalize_feed_payload({"x": "\u00b2"})["x"] == "\u00b2"

    def test_bool_kept_and_not_float(self):
        out = df.normalize_feed_payload({"flag": True})
        assert out["flag"] is True

    def test_non_numeric_string_kept_verbatim(self):
        out = df.normalize_feed_payload({"note": " hello ", "empty": "", "dash": "-"})
        assert out["note"] == " hello "
        assert out["empty"] == ""
        assert out["dash"] == "-"

    def test_other_types_pass_through_and_none_key_skipped(self):
        out = df.normalize_feed_payload({None: 1, "lst": [1, 2], "d": {"a": 1}, "n": None})
        assert None not in out
        assert out["lst"] == [1, 2] and out["d"] == {"a": 1} and out["n"] is None

    def test_unknown_key_lowercased_with_underscores(self):
        assert df.normalize_feed_payload({"My Field": 1})["my_field"] == 1.0


class TestExtractFeedPayload:
    def test_full_extraction(self):
        f = {
            "fundamental_score": 70, "valuation": "fair", "sector": "IT", "industry": "SW",
            "peer_relative_score": 1, "peer_relative": {"x": 1}, "peer_list": ["A"],
            "multi_quarter_score": 2, "multi_quarter_ok": True, "multi_quarter_detail": {},
            "quality_score": 3, "metrics": {"pe": 10}, "reasons": list("abcdefgh"),
            "fallback_used": False,
        }
        e = {
            "bulk_deals": list(range(9)), "insider": [1, 2], "earnings_surprise": 4,
            "next_earnings_date": "2026-11-01", "summary": "s", "count": 3,
            "has_positive_catalyst": True, "recent_event_score": 8,
        }
        p = df.extract_feed_payload("tcs.ns", f, e, {"extra_k": 1, "symbol": "IGNORED", "nn": None})
        assert p["symbol"] == "TCS"
        assert p["fundamental_reasons"] == list("abcdef")
        assert p["bulk_deals"] == [0, 1, 2, 3, 4]
        assert p["insider"] == [1, 2]
        assert p["recent_insider_transactions"] == [1, 2]
        assert p["earnings_surprise"] == 4 and p["event_summary"] == "s"
        assert p["events_count"] == 3 and p["has_positive_catalyst"] is True
        assert p["metrics"] == {"pe": 10}
        assert p["extra_k"] == 1 and "nn" not in p
        datetime.fromisoformat(p["updated_at"])

    def test_alternate_event_keys(self):
        e = {"bulk": [1], "insider_trades": [2], "event_summary": "es", "total": 7}
        p = df.extract_feed_payload("X", None, e)
        assert p["bulk_deals"] == [1]
        assert p["insider"] == [2]
        assert p["recent_insider_transactions"] == [2]
        assert p["event_summary"] == "es" and p["events_count"] == 7

    def test_non_dict_inputs(self):
        p = df.extract_feed_payload("X", "bad", "bad", "bad")
        assert p["symbol"] == "X"
        assert p["metrics"] == {}
        assert p["fundamental_reasons"] == []
        assert p["bulk_deals"] == [] and p["insider"] == [] and p["recent_insider_transactions"] == []
        assert p["earnings_surprise"] is None and p["event_summary"] is None
        assert p["events_count"] is None and p["next_earnings_date"] is None

    def test_metrics_non_dict_becomes_empty(self):
        assert df.extract_feed_payload("X", {"metrics": [1]})["metrics"] == {}


class TestMergeFeedPayload:
    def test_none_inputs(self):
        assert df.merge_feed_payload(None, None) == {}
        assert df.merge_feed_payload({"a": 1}, None) == {"a": 1}
        assert df.merge_feed_payload(None, {"a": 1}) == {"a": 1}

    def test_none_values_never_wipe(self):
        assert df.merge_feed_payload({"a": 1}, {"a": None, "b": 2}) == {"a": 1, "b": 2}

    def test_zero_protection(self):
        out = df.merge_feed_payload({"volume": 500, "rsi": 60}, {"volume": 0, "rsi": 0.0})
        assert out == {"volume": 500, "rsi": 60}

    def test_zero_allowed_when_old_zero_or_missing(self):
        assert df.merge_feed_payload({"volume": 0}, {"volume": 0})["volume"] == 0
        assert df.merge_feed_payload({}, {"volume": 0})["volume"] == 0

    def test_non_zero_incoming_overwrites(self):
        assert df.merge_feed_payload({"volume": 500}, {"volume": 9})["volume"] == 9

    def test_unparseable_zero_protected_values_fall_through(self):
        out = df.merge_feed_payload({"volume": "n/a"}, {"volume": 5})
        assert out["volume"] == 5
        out = df.merge_feed_payload({"volume": 3}, {"volume": "junk"})
        assert out["volume"] == "junk"

    def test_seed_does_not_overwrite_real_value(self):
        out = df.merge_feed_payload({"pe_ratio": 30.0}, {"pe_ratio": 22.5, "pe_ratio_seed": True})
        assert out["pe_ratio"] == 30.0

    def test_rejected_seed_does_not_leak_its_flag_onto_the_real_value(self):
        # FIXED: the seed flag used to fall through to `base[k] = v` when the real value was
        # kept, mislabelling the real 30.0 as a seed so the NEXT seed run could overwrite it.
        first = df.merge_feed_payload({"pe_ratio": 30.0}, {"pe_ratio": 22.5, "pe_ratio_seed": True})
        assert first == {"pe_ratio": 30.0}
        second = df.merge_feed_payload(first, {"pe_ratio": 22.5, "pe_ratio_seed": True})
        assert second == {"pe_ratio": 30.0}

    def test_rejected_seed_flag_is_dropped_whichever_key_comes_first(self):
        out = df.merge_feed_payload({"pe_ratio": 30.0}, {"pe_ratio_seed": True, "pe_ratio": 22.5})
        assert out == {"pe_ratio": 30.0}

    def test_rejected_seed_only_drops_the_flags_of_the_rejected_fields(self):
        out = df.merge_feed_payload(
            {"pe_ratio": 30.0},
            {"pe_ratio": 22.5, "pe_ratio_seed": True, "roce": 12.0, "roce_seed": True})
        assert out == {"pe_ratio": 30.0, "roce": 12.0, "roce_seed": True}

    def test_seed_refreshes_seed(self):
        out = df.merge_feed_payload(
            {"pe_ratio": 22.5, "pe_ratio_seed": True}, {"pe_ratio": 25.0, "pe_ratio_seed": True})
        assert out["pe_ratio"] == 25.0 and out["pe_ratio_seed"] is True

    def test_seed_fills_empty(self):
        out = df.merge_feed_payload({}, {"roce": 15.0, "roce_seed": True})
        assert out["roce"] == 15.0 and out["roce_seed"] is True

    def test_real_value_clears_seed_flag(self):
        out = df.merge_feed_payload(
            {"pe_ratio": 22.5, "pe_ratio_seed": True}, {"pe_ratio": 31.0})
        assert out["pe_ratio"] == 31.0
        assert "pe_ratio_seed" not in out

    def test_seed_flag_false_treated_as_real(self):
        out = df.merge_feed_payload({"sector": "IT"}, {"sector": "Auto", "sector_seed": False})
        assert out["sector"] == "Auto"
        assert out["sector_seed"] is False

    def test_does_not_mutate_inputs(self):
        old, new = {"a": 1}, {"b": 2}
        df.merge_feed_payload(old, new)
        assert old == {"a": 1} and new == {"b": 2}


# ── DataFeedStore: construction, warm, symbol reads ──────────────────────────

class TestStoreInit:
    def test_explicit_callables(self):
        g, s = (lambda k: None), (lambda k, v, ttl=None: None)
        st = df.DataFeedStore(g, s, "redis")
        assert st._get is g and st._set is s and st._redis == "redis"

    def test_binds_to_kv_cache_when_omitted(self, kv):
        st = df.DataFeedStore()
        st._set("k", {"a": 1})
        assert st._get("k") == {"a": 1}

    def test_partial_args_fill_from_kv_cache(self, kv):
        g = lambda k: "custom"
        st = df.DataFeedStore(redis_get=g)
        assert st._get is g
        st._set("k", {"a": 1})
        assert kv.store["k"] == {"a": 1}

    def test_raises_when_nothing_available(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "kv_cache", None)   # import raises ImportError
        with pytest.raises(TypeError):
            df.DataFeedStore()


class TestWarm:
    def test_warm_marks_index_warmed(self, kv, store):
        kv.store[df.DATA_FEED_INDEX_KEY] = {"symbols": ["TCS", "INFY"]}
        kv.store[df.DATA_FEED_META_KEY] = {"last_count": 2}
        store.warm()
        assert df._INDEX_WARMED is True
        assert {"TCS", "INFY"} <= df._LOCAL_INDEX

    def test_warm_swallows_errors(self, store, monkeypatch):
        def boom():
            raise RuntimeError("x")
        monkeypatch.setattr(store, "meta", boom)
        store.warm()
        assert df._INDEX_WARMED is False


class TestGetSymbol:
    def test_blank_symbol(self, store):
        assert store.get_symbol("") is None
        assert store.get_symbol(None) is None

    def test_local_hit_returns_copy(self, store):
        df._LOCAL_SYMBOLS[df.DATA_FEED_PREFIX + "TCS"] = rich()
        got = store.get_symbol("tcs.ns")
        assert got == rich()
        got["price"] = 1
        assert df._LOCAL_SYMBOLS[df.DATA_FEED_PREFIX + "TCS"]["price"] == 100.0

    def test_local_hit_on_alias_key(self, store):
        df._LOCAL_SYMBOLS[df.FEED_ALIAS_PREFIX + "TCS"] = rich()
        assert store.get_symbol("TCS")["sector"] == "IT"

    def test_useless_local_falls_through_to_durable(self, kv, store):
        df._LOCAL_SYMBOLS[df.DATA_FEED_PREFIX + "TCS"] = {"junk": 1}
        kv.store[df.DATA_FEED_PREFIX + "TCS"] = rich()
        assert store.get_symbol("TCS") == rich()

    def test_durable_hit_warms_all_aliases_and_index(self, kv, store):
        kv.store[df.FEED_LEGACY_PREFIX + "TCS"] = rich()
        got = store.get_symbol("TCS")
        assert got == rich()
        for pre in (df.DATA_FEED_PREFIX, df.FEED_ALIAS_PREFIX, df.FEED_LEGACY_PREFIX):
            assert df._LOCAL_SYMBOLS[pre + "TCS"] == rich()
        assert "TCS" in df._LOCAL_INDEX

    def test_get_error_on_one_key_tries_next(self, kv, store):
        kv.get_raises_for[df.DATA_FEED_PREFIX + "TCS"] = RuntimeError("neon down")
        kv.store[df.FEED_ALIAS_PREFIX + "TCS"] = rich()
        assert store.get_symbol("TCS") == rich()

    def test_all_misses(self, kv, store):
        assert store.get_symbol("NOPE") is None
        assert "NOPE" not in df._LOCAL_INDEX

    def test_empty_dict_and_non_dict_are_misses(self, kv, store):
        kv.store[df.DATA_FEED_PREFIX + "A"] = {}
        kv.store[df.DATA_FEED_PREFIX + "B"] = ["x"]
        assert store.get_symbol("A") is None
        assert store.get_symbol("B") is None

    def test_has_symbol(self, kv, store):
        kv.store[df.DATA_FEED_PREFIX + "TCS"] = rich()
        assert store.has_symbol("TCS") is True
        assert store.has_symbol("ZZZ") is False


class TestGetSymbolsBulk:
    def test_empty(self, store):
        assert store.get_symbols_bulk([]) == {}
        assert store.get_symbols_bulk(None) == {}

    def test_all_local_no_kv_round_trip(self, kv, store):
        df._LOCAL_SYMBOLS[df.DATA_FEED_PREFIX + "TCS"] = rich()
        df._LOCAL_SYMBOLS[df.FEED_ALIAS_PREFIX + "INFY"] = rich(symbol="INFY")
        out = store.get_symbols_bulk(["TCS", "infy.ns"])
        assert set(out) == {"TCS", "INFY"}
        assert kv.many_gets == []

    def test_blank_symbols_skipped(self, kv, store):
        assert store.get_symbols_bulk(["", None, "  "]) == {}

    def test_bulk_fetch_requests_all_three_keys(self, kv, store):
        kv.store[df.DATA_FEED_PREFIX + "TCS"] = rich()
        out = store.get_symbols_bulk(["TCS"])
        assert out == {"TCS": rich()}
        assert kv.many_gets == [[
            df.DATA_FEED_PREFIX + "TCS", df.FEED_ALIAS_PREFIX + "TCS", df.FEED_LEGACY_PREFIX + "TCS",
        ]]
        for pre in (df.DATA_FEED_PREFIX, df.FEED_ALIAS_PREFIX, df.FEED_LEGACY_PREFIX):
            assert pre + "TCS" in df._LOCAL_SYMBOLS
        assert "TCS" in df._LOCAL_INDEX

    def test_first_hit_wins_over_thinner_alias(self, kv, store):
        kv.store[df.DATA_FEED_PREFIX + "TCS"] = rich(price=111.0)
        kv.store[df.FEED_ALIAS_PREFIX + "TCS"] = rich(price=222.0)
        assert store.get_symbols_bulk(["TCS"])["TCS"]["price"] == 111.0

    def test_useless_first_hit_replaced_by_useful_alias(self, kv, store):
        kv.store[df.DATA_FEED_PREFIX + "TCS"] = {"junk": 1}
        kv.store[df.FEED_ALIAS_PREFIX + "TCS"] = rich(price=222.0)
        assert store.get_symbols_bulk(["TCS"])["TCS"]["price"] == 222.0

    def test_non_dict_and_empty_values_ignored(self, kv, store):
        kv.store[df.DATA_FEED_PREFIX + "A"] = ["list"]
        kv.store[df.FEED_ALIAS_PREFIX + "A"] = {}
        assert store.get_symbols_bulk(["A"]) == {}

    def test_legacy_function_names_supported(self, monkeypatch):
        fake = FakeKV()
        monkeypatch.setitem(sys.modules, "kv_cache", fake.module(legacy_names=True))
        fake.store[df.DATA_FEED_PREFIX + "TCS"] = rich()
        st = df.DataFeedStore(sys.modules["kv_cache"].kv_get, sys.modules["kv_cache"].kv_set)
        assert st.get_symbols_bulk(["TCS"])["TCS"] == rich()

    def test_falls_back_to_serial_gets_when_get_many_missing(self, monkeypatch):
        fake = FakeKV()
        monkeypatch.setitem(sys.modules, "kv_cache", fake.module(with_many=False))
        fake.store[df.FEED_ALIAS_PREFIX + "TCS"] = rich()
        fake.get_raises_for[df.FEED_LEGACY_PREFIX + "TCS"] = RuntimeError("x")
        st = df.DataFeedStore(sys.modules["kv_cache"].kv_get, sys.modules["kv_cache"].kv_set)
        assert st.get_symbols_bulk(["TCS"])["TCS"] == rich()

    def test_falls_back_to_serial_gets_when_get_many_raises(self, monkeypatch):
        fake = FakeKV()
        monkeypatch.setitem(sys.modules, "kv_cache", fake.module(get_many_raises=RuntimeError("x")))
        fake.store[df.DATA_FEED_PREFIX + "TCS"] = rich()
        fake.get_raises_for[df.FEED_ALIAS_PREFIX + "TCS"] = RuntimeError("x")
        st = df.DataFeedStore(sys.modules["kv_cache"].kv_get, sys.modules["kv_cache"].kv_set)
        assert st.get_symbols_bulk(["TCS"])["TCS"] == rich()

    def test_key_not_in_map_derives_base_from_key(self, kv, store, monkeypatch):
        # A backend that returns keys we did not ask for still resolves to a base symbol.
        def odd_get_many(keys):
            return {
                df.DATA_FEED_PREFIX + "ZED": rich(symbol="ZED"),
                df.FEED_ALIAS_PREFIX + "YAK": rich(symbol="YAK"),
                "plainkey": rich(symbol="PLAIN"),
                df.DATA_FEED_PREFIX: rich(),   # empty base → skipped
            }
        monkeypatch.setattr(sys.modules["kv_cache"], "get_many", odd_get_many)
        out = store.get_symbols_bulk(["TCS"])
        assert set(out) == {"ZED", "YAK", "PLAINKEY"}


# ── DataFeedStore: writes ────────────────────────────────────────────────────

class TestPrepareSymbolPayload:
    def test_merges_normalizes_and_defaults(self, store):
        out = store._prepare_symbol_payload("TCS", {"Price": "10", "note": None}, {"sector": "IT"})
        assert out["sector"] == "IT" and out["prev_close"] == 10.0
        assert out["symbol"] == "TCS"
        assert "note" not in out
        datetime.fromisoformat(out["updated_at"])

    def test_existing_symbol_and_updated_at_kept(self, store):
        out = store._prepare_symbol_payload(
            "TCS", {"symbol": "OTHER", "updated_at": "2020-01-01"}, None)
        assert out["symbol"] == "OTHER" and out["updated_at"] == "2020-01-01"

    def test_over_cap_strips_volatile_fields_and_keeps_durable(self, store, monkeypatch):
        monkeypatch.setattr(df, "MAX_STOCK_PRICE", 500.0)
        out = store._prepare_symbol_payload(
            "TCS", {"price": 900, "volume": 5, "day_high": 1, "pe_ratio": 20, "sector": "IT"}, None)
        assert out["price_over_cap"] is True and out["price_cap"] == 500.0
        for k in ("price", "volume", "day_high"):
            assert k not in out
        assert out["pe_ratio"] == 20.0

    @pytest.mark.parametrize("key", ["price", "close", "cmp", "prev_close", "prevclose"])
    def test_over_cap_price_is_not_left_behind_as_prev_close(self, store, monkeypatch, key):
        # FIXED: normalize_feed_payload renames price/close/cmp to prev_close BEFORE the over-cap
        # strip runs, and prev_close was not in the strip list, so the over-cap price was still
        # persisted and readable through _payload_price. It is now stripped too.
        monkeypatch.setattr(df, "MAX_STOCK_PRICE", 500.0)
        out = store._prepare_symbol_payload("TCS", {key: 900, "sector": "IT"}, None)
        assert out["price_over_cap"] is True
        assert "prev_close" not in out
        assert df._payload_price(out) == 0.0

    def test_over_cap_without_durable_fields_returns_none(self, store, monkeypatch):
        monkeypatch.setattr(df, "MAX_STOCK_PRICE", 500.0)
        assert store._prepare_symbol_payload("TCS", {"price": 900}, None) is None

    def test_under_cap_untouched(self, store, monkeypatch):
        monkeypatch.setattr(df, "MAX_STOCK_PRICE", 500.0)
        out = store._prepare_symbol_payload("TCS", {"price": 100}, None)
        # normalize_feed_payload renames price/close/cmp -> prev_close
        assert out["prev_close"] == 100.0 and "price" not in out and "price_over_cap" not in out


class TestPutSymbol:
    def test_writes_local_durable_alias_legacy_and_index(self, kv, store):
        store.put_symbol("tcs.ns", rich(), ttl=99)
        assert df.DATA_FEED_PREFIX + "TCS" in df._LOCAL_SYMBOLS
        assert df.FEED_ALIAS_PREFIX + "TCS" in df._LOCAL_SYMBOLS
        keys = {k for k, _, _ in kv.sets}
        assert {df.DATA_FEED_PREFIX + "TCS", df.FEED_ALIAS_PREFIX + "TCS",
                df.FEED_LEGACY_PREFIX + "TCS", df.DATA_FEED_INDEX_KEY} <= keys
        assert all(t == 99 for _, _, t in kv.sets)
        assert kv.store[df.DATA_FEED_INDEX_KEY]["symbols"] == ["TCS"]

    def test_merges_with_existing(self, kv, store):
        kv.store[df.DATA_FEED_PREFIX + "TCS"] = {"sector": "IT", "volume": 500}
        store.put_symbol("TCS", {"price": 10, "volume": 0})
        saved = kv.store[df.DATA_FEED_PREFIX + "TCS"]
        assert saved["sector"] == "IT" and saved["volume"] == 500 and saved["prev_close"] == 10.0

    @pytest.mark.parametrize("sym,payload", [("", {"a": 1}), (None, {"a": 1}), ("TCS", None), ("TCS", "x")])
    def test_invalid_input_is_noop(self, kv, store, sym, payload):
        store.put_symbol(sym, payload)
        assert kv.sets == [] and not df._LOCAL_SYMBOLS

    def test_get_symbol_failure_treated_as_no_existing(self, kv, store, monkeypatch):
        monkeypatch.setattr(store, "get_symbol", lambda s: (_ for _ in ()).throw(RuntimeError("x")))
        store.put_symbol("TCS", rich())
        assert df.DATA_FEED_PREFIX + "TCS" in kv.store

    def test_prepare_none_writes_nothing(self, kv, store, monkeypatch):
        monkeypatch.setattr(df, "MAX_STOCK_PRICE", 10.0)
        store.put_symbol("TCS", {"price": 900})
        assert kv.sets == [] and not df._LOCAL_SYMBOLS

    def test_each_durable_write_failure_is_swallowed(self, kv, store):
        kv.set_raises_for[df.DATA_FEED_PREFIX + "TCS"] = RuntimeError("a")
        kv.set_raises_for[df.FEED_ALIAS_PREFIX + "TCS"] = RuntimeError("b")
        kv.set_raises_for[df.FEED_LEGACY_PREFIX + "TCS"] = RuntimeError("c")
        kv.set_raises_for[df.DATA_FEED_INDEX_KEY] = RuntimeError("d")
        store.put_symbol("TCS", rich())
        assert df.DATA_FEED_PREFIX + "TCS" in df._LOCAL_SYMBOLS
        assert kv.sets == []


class TestPutSymbolsBulk:
    def test_empty_and_all_invalid(self, kv, store):
        assert store.put_symbols_bulk({}) == 0
        assert store.put_symbols_bulk(None) == 0
        assert store.put_symbols_bulk({"": {"a": 1}, "TCS": "notdict"}) == 0
        assert kv.sets == []

    def test_batched_write_uses_set_many_and_one_index_persist(self, kv, store):
        n = store.put_symbols_bulk({"tcs": rich(), "infy.ns": rich(symbol="INFY")}, ttl=77)
        assert n == 2
        keys = [k for k, _, _ in kv.sets]
        assert keys.count(df.DATA_FEED_INDEX_KEY) == 1
        for b in ("TCS", "INFY"):
            for pre in (df.DATA_FEED_PREFIX, df.FEED_ALIAS_PREFIX, df.FEED_LEGACY_PREFIX):
                assert pre + b in kv.store
        assert kv.store[df.DATA_FEED_INDEX_KEY]["symbols"] == ["INFY", "TCS"]
        assert all(t == 77 for _, _, t in kv.sets)

    def test_merge_against_existing_from_bulk_read(self, kv, store):
        kv.store[df.DATA_FEED_PREFIX + "TCS"] = {"sector": "IT", "volume": 500}
        store.put_symbols_bulk({"TCS": {"price": 10, "volume": 0}})
        assert kv.store[df.DATA_FEED_PREFIX + "TCS"]["sector"] == "IT"
        assert kv.store[df.DATA_FEED_PREFIX + "TCS"]["volume"] == 500

    def test_bulk_read_failure_proceeds_without_merge(self, kv, store, monkeypatch):
        monkeypatch.setattr(store, "get_symbols_bulk", lambda b: (_ for _ in ()).throw(RuntimeError("x")))
        assert store.put_symbols_bulk({"TCS": rich()}) == 1

    def test_over_cap_symbol_skipped(self, kv, store, monkeypatch):
        monkeypatch.setattr(df, "MAX_STOCK_PRICE", 10.0)
        assert store.put_symbols_bulk({"BIG": {"price": 900}}) == 0
        assert kv.sets == []

    def test_serial_fallback_when_set_many_missing(self, monkeypatch):
        fake = FakeKV()
        monkeypatch.setitem(sys.modules, "kv_cache", fake.module(with_set_many=False))
        st = df.DataFeedStore(sys.modules["kv_cache"].kv_get, sys.modules["kv_cache"].kv_set)
        assert st.put_symbols_bulk({"TCS": rich()}) == 1
        assert df.DATA_FEED_PREFIX + "TCS" in fake.store

    def test_legacy_set_many_name(self, monkeypatch):
        fake = FakeKV()
        monkeypatch.setitem(sys.modules, "kv_cache", fake.module(legacy_names=True))
        st = df.DataFeedStore(sys.modules["kv_cache"].kv_get, sys.modules["kv_cache"].kv_set)
        assert st.put_symbols_bulk({"TCS": rich()}) == 1

    def test_set_many_failure_falls_back_to_serial_and_swallows_errors(self, monkeypatch):
        fake = FakeKV()
        monkeypatch.setitem(sys.modules, "kv_cache", fake.module(set_many_raises=RuntimeError("x")))
        fake.set_raises_for[df.FEED_ALIAS_PREFIX + "TCS"] = RuntimeError("y")
        st = df.DataFeedStore(sys.modules["kv_cache"].kv_get, sys.modules["kv_cache"].kv_set)
        assert st.put_symbols_bulk({"TCS": rich()}) == 1
        assert df.DATA_FEED_PREFIX + "TCS" in fake.store
        assert df.FEED_ALIAS_PREFIX + "TCS" not in fake.store

    def test_index_persist_failure_swallowed(self, kv, store, monkeypatch):
        kv.set_raises_for[df.DATA_FEED_INDEX_KEY] = RuntimeError("x")
        assert store.put_symbols_bulk({"TCS": rich()}) == 1


# ── DataFeedStore: delete / index / meta / job ───────────────────────────────

class TestDeleteSymbol:
    def test_blank(self, store):
        assert store.delete_symbol("") is False

    def test_removes_everywhere(self, kv, store):
        store.put_symbol("TCS", rich())
        kv.sets.clear()
        assert store.delete_symbol("tcs.ns") is True
        assert set(kv.deleted) == {
            df.DATA_FEED_PREFIX + "TCS", df.FEED_ALIAS_PREFIX + "TCS", df.FEED_LEGACY_PREFIX + "TCS"}
        assert not any(k.endswith("TCS") for k in df._LOCAL_SYMBOLS)
        assert not any(k.endswith("TCS") for k in kv.store if k != df.DATA_FEED_INDEX_KEY)

    def test_deleted_symbol_leaves_the_local_and_durable_index(self, kv, store):
        # FIXED: _persist_index() went through list_symbols(), which re-unioned the stale durable
        # index and put the symbol straight back (count_symbols() stayed inflated). delete_symbol
        # now excludes it from the rewritten index and from the local index.
        store.put_symbol("TCS", rich())
        assert store.delete_symbol("TCS") is True
        assert "TCS" not in df._LOCAL_INDEX
        assert kv.store[df.DATA_FEED_INDEX_KEY]["symbols"] == []
        assert store.list_symbols() == []
        assert store.count_symbols() == 0
        assert store.get_symbol("TCS") is None

    def test_deleting_one_symbol_keeps_the_others_in_the_index(self, kv, store):
        store.put_symbol("TCS", rich())
        store.put_symbol("INFY", rich(symbol="INFY"))
        store.delete_symbol("TCS")
        assert kv.store[df.DATA_FEED_INDEX_KEY]["symbols"] == ["INFY"]
        assert store.list_symbols() == ["INFY"]

    def test_persist_index_exclude_drops_only_that_symbol(self, kv, store):
        df._LOCAL_INDEX.update({"A", "B", "C"})
        store._persist_index(ttl=5, exclude="B")
        assert kv.store[df.DATA_FEED_INDEX_KEY]["symbols"] == ["A", "C"]
        assert kv.store[df.DATA_FEED_INDEX_KEY]["count"] == 2

    def test_unknown_symbol_returns_false(self, kv, store):
        assert store.delete_symbol("NOPE") is False

    def test_found_via_local_index_only(self, kv, store):
        df._LOCAL_INDEX.add("GHOST")
        assert store.delete_symbol("GHOST") is True

    def test_kv_delete_error_is_swallowed(self, kv, store):
        store.put_symbol("TCS", rich())
        kv.delete_raises = RuntimeError("x")
        assert store.delete_symbol("TCS") is True

    def test_kv_cache_import_failure_is_swallowed(self, kv, store, monkeypatch):
        store.put_symbol("TCS", rich())
        monkeypatch.setitem(sys.modules, "kv_cache", None)
        assert store.delete_symbol("TCS") is True

    def test_index_persist_failure_is_swallowed(self, kv, store):
        store.put_symbol("TCS", rich())
        kv.set_raises_for[df.DATA_FEED_INDEX_KEY] = RuntimeError("x")
        assert store.delete_symbol("TCS") is True


class TestListSymbols:
    def test_union_of_local_and_durable_list(self, kv, store):
        df._LOCAL_INDEX.add("LOCAL")
        kv.store[df.DATA_FEED_INDEX_KEY] = ["tcs.ns", "", None, "INFY"]
        assert store.list_symbols() == ["INFY", "LOCAL", "TCS"]
        assert df._INDEX_WARMED is True
        assert "TCS" in df._LOCAL_INDEX

    def test_durable_dict_form(self, kv, store):
        kv.store[df.DATA_FEED_INDEX_KEY] = {"symbols": ["A", "b"]}
        assert store.list_symbols() == ["A", "B"]

    def test_durable_dict_without_list_and_junk_types(self, kv, store):
        kv.store[df.DATA_FEED_INDEX_KEY] = {"symbols": "nope"}
        assert store.list_symbols() == []
        kv.store[df.DATA_FEED_INDEX_KEY] = "junk"
        assert store.list_symbols() == []

    def test_read_error_falls_back_to_local(self, kv, store):
        df._LOCAL_INDEX.add("LOCAL")
        kv.get_raises = RuntimeError("x")
        assert store.list_symbols() == ["LOCAL"]
        assert df._INDEX_WARMED is False

    def test_count_symbols(self, kv, store):
        df._LOCAL_INDEX.update({"A", "B"})
        assert store.count_symbols() == 2

    def test_persist_index_dedupes_and_sorts(self, kv, store):
        df._LOCAL_INDEX.update({"B", "A"})
        store._persist_index(ttl=5)
        key, payload, ttl = kv.sets[-1]
        assert key == df.DATA_FEED_INDEX_KEY and ttl == 5
        assert payload["symbols"] == ["A", "B"] and payload["count"] == 2
        datetime.fromisoformat(payload["updated_at"])


class TestMeta:
    def test_default_when_nothing_stored(self, store):
        m = store.meta()
        assert m == {"last_success_at": None, "last_count": 0,
                     "last_message": "No data feed run yet", "source": None}

    def test_durable_meta_and_non_dict_durable(self, kv, store):
        kv.store[df.DATA_FEED_META_KEY] = {"last_count": 4, "last_success_at": "t"}
        assert store.meta()["last_count"] == 4
        kv.store[df.DATA_FEED_META_KEY] = "junk"
        assert store.meta()["last_message"] == "No data feed run yet"

    def test_read_error_treated_as_empty(self, kv, store):
        kv.get_raises_for[df.DATA_FEED_META_KEY] = RuntimeError("x")
        assert store.meta()["last_count"] == 0

    def test_local_overrides_durable(self, kv, store):
        kv.store[df.DATA_FEED_META_KEY] = {"last_count": 4, "note": "d"}
        df._LOCAL_META.update({"last_count": 9})
        m = store.meta()
        assert m["last_count"] == 9 and m["note"] == "d"

    def test_heals_count_from_index(self, kv, store):
        kv.store[df.DATA_FEED_META_KEY] = {"last_count": 0, "last_success_at": "t"}
        df._LOCAL_INDEX.update({"A", "B", "C"})
        assert store.meta()["last_count"] == 3

    def test_heals_last_success_from_index_timestamp(self, kv, store):
        kv.store[df.DATA_FEED_META_KEY] = {"last_count": 5}
        kv.store[df.DATA_FEED_INDEX_KEY] = {"symbols": ["A"], "updated_at": "2026-01-01T00:00:00"}
        assert store.meta()["last_success_at"] == "2026-01-01T00:00:00"

    def test_no_heal_timestamp_when_index_not_dict_or_no_stamp(self, kv, store):
        kv.store[df.DATA_FEED_INDEX_KEY] = ["A"]
        assert store.meta()["last_success_at"] is None

    def test_heal_failure_is_swallowed(self, kv, store, monkeypatch):
        monkeypatch.setattr(store, "count_symbols", lambda: (_ for _ in ()).throw(RuntimeError("x")))
        assert store.meta()["last_count"] == 0

    def test_set_meta_persists_and_updates_local(self, kv, store):
        m = store.set_meta(last_message="hello", last_count=3)
        assert m["last_message"] == "hello"
        datetime.fromisoformat(m["updated_at"])
        assert df._LOCAL_META == m
        key, payload, ttl = kv.sets[-1]
        assert key == df.DATA_FEED_META_KEY and ttl == 7 * 86400 and payload == m

    def test_set_meta_raises_count_to_index_size(self, kv, store):
        df._LOCAL_INDEX.update({"A", "B", "C"})
        assert store.set_meta(last_count=1)["last_count"] == 3

    def test_set_meta_count_error_and_write_error_swallowed(self, kv, store, monkeypatch):
        calls = {"n": 0}
        real = store.count_symbols

        def flaky():
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("x")
            return real()
        monkeypatch.setattr(store, "count_symbols", flaky)
        kv.set_raises_for[df.DATA_FEED_META_KEY] = RuntimeError("y")
        m = store.set_meta(a=1)
        assert m["a"] == 1


class TestJob:
    def test_default_idle(self, store):
        j = store.job()
        assert j["status"] == "idle" and j["processed"] == 0 and j["message"] == "Idle"

    def test_durable_job(self, kv, store):
        kv.store[df.DATA_FEED_JOB_KEY] = {"status": "done", "processed": 5}
        assert store.job()["processed"] == 5

    def test_read_error_and_non_dict(self, kv, store):
        kv.get_raises_for[df.DATA_FEED_JOB_KEY] = RuntimeError("x")
        assert store.job()["status"] == "idle"
        kv.get_raises_for.clear()
        kv.store[df.DATA_FEED_JOB_KEY] = "junk"
        assert store.job()["status"] == "idle"

    def test_local_running_overrides_durable(self, kv, store):
        kv.store[df.DATA_FEED_JOB_KEY] = {"status": "done", "processed": 1, "keep": "d"}
        df._LOCAL_JOB.update({"status": "running", "processed": 7})
        j = store.job()
        assert j["status"] == "running" and j["processed"] == 7 and j["keep"] == "d"

    def test_local_non_running_still_overrides(self, kv, store):
        kv.store[df.DATA_FEED_JOB_KEY] = {"status": "running"}
        df._LOCAL_JOB.update({"status": "stopped"})
        assert store.job()["status"] == "stopped"

    def test_set_job_running_computes_eta(self, kv, store):
        started = (datetime.now(df.IST) - timedelta(seconds=100)).isoformat()
        j = store.set_job(status="running", started_at=started, processed=10, total=50)
        assert 99 <= j["elapsed_sec"] <= 105
        assert j["estimated_remaining_sec"] == pytest.approx(j["elapsed_sec"] / 10 * 40, abs=1)
        assert kv.store[df.DATA_FEED_JOB_KEY]["status"] == "running"

    def test_set_job_naive_started_at_treated_as_ist(self, store):
        started = (datetime.now(df.IST) - timedelta(seconds=50)).replace(tzinfo=None).isoformat()
        j = store.set_job(status="running", started_at=started, processed=1, total=2)
        assert 49 <= j["elapsed_sec"] <= 55

    def test_set_job_no_eta_when_nothing_processed(self, store):
        started = datetime.now(df.IST).isoformat()
        j = store.set_job(status="running", started_at=started, processed=0, total=5)
        assert "estimated_remaining_sec" in j and j["estimated_remaining_sec"] is None

    def test_set_job_bad_started_at_swallowed(self, store):
        j = store.set_job(status="running", started_at="not-a-date", processed=1, total=2)
        assert j["started_at"] == "not-a-date"

    def test_set_job_durable_write_failure_swallowed(self, kv, store):
        kv.set_raises_for[df.DATA_FEED_JOB_KEY] = RuntimeError("x")
        assert store.set_job(status="running", ok_count=2)["status"] == "running"

    def test_set_job_syncs_meta_while_running(self, kv, store):
        store.set_job(status="running", ok_count=4, message="working")
        m = kv.store[df.DATA_FEED_META_KEY]
        assert m["last_count"] == 4 and m["last_message"] == "working"
        assert m["source"] == "job_progress" and m["last_success_at"]

    def test_set_job_done_uses_finished_at(self, kv, store):
        store.set_job(status="done", ok_count=3, finished_at="2026-01-01T00:00:00")
        assert kv.store[df.DATA_FEED_META_KEY]["last_success_at"] == "2026-01-01T00:00:00"

    def test_set_job_done_without_finished_at_uses_updated_at(self, kv, store):
        j = store.set_job(status="stopped", ok_count=0)
        assert kv.store[df.DATA_FEED_META_KEY]["last_success_at"] == j["updated_at"]

    def test_set_job_error_status_syncs_meta_without_success_stamp(self, kv, store):
        store.set_job(status="error", message="boom")
        m = kv.store[df.DATA_FEED_META_KEY]
        assert m["last_message"] == "boom"
        assert m["last_success_at"] is None

    def test_set_job_running_zero_progress_writes_no_meta(self, kv, store):
        store.set_job(status="running", processed=0)
        assert df.DATA_FEED_META_KEY not in kv.store

    def test_set_job_meta_sync_failure_swallowed(self, kv, store, monkeypatch):
        monkeypatch.setattr(store, "set_meta", lambda **k: (_ for _ in ()).throw(RuntimeError("x")))
        assert store.set_job(status="done", ok_count=1)["status"] == "done"


# ── hot-picks job helpers ────────────────────────────────────────────────────

class TestHotJobRecompute:
    def test_non_dict_and_not_running_returned_untouched(self):
        assert df._hot_job_recompute("x") == "x"
        j = {"status": "done", "started_at": "2020-01-01"}
        assert df._hot_job_recompute(j) is j and "elapsed_sec" not in j
        j = {"status": "running"}
        assert "elapsed_sec" not in df._hot_job_recompute(j)

    def test_live_eta(self):
        started = (datetime.now(df.IST) - timedelta(seconds=60)).isoformat()
        j = df._hot_job_recompute({"status": "running", "started_at": started, "processed": 6, "total": 16})
        assert j["elapsed_sec"] >= 59
        assert j["estimated_remaining_sec"] == pytest.approx(j["elapsed_sec"] / 6 * 10, abs=2)

    def test_overshoot_clamped_to_zero(self):
        started = (datetime.now(df.IST) - timedelta(seconds=60)).isoformat()
        j = df._hot_job_recompute({"status": "running", "started_at": started, "processed": 20, "total": 10})
        assert j["estimated_remaining_sec"] == 0

    def test_unknown_eta_when_nothing_done(self):
        started = datetime.now(df.IST).isoformat()
        j = df._hot_job_recompute({"status": "running", "started_at": started, "processed": 0, "total": 10})
        assert j["estimated_remaining_sec"] is None

    def test_naive_started_at_and_no_total_leaves_eta_alone(self):
        started = (datetime.now(df.IST) - timedelta(seconds=30)).replace(tzinfo=None).isoformat()
        j = df._hot_job_recompute(
            {"status": "running", "started_at": started, "processed": 3, "total": 0,
             "estimated_remaining_sec": 123})
        assert j["elapsed_sec"] >= 29 and j["estimated_remaining_sec"] == 123

    def test_future_started_at_clamps_elapsed(self):
        started = (datetime.now(df.IST) + timedelta(seconds=500)).isoformat()
        j = df._hot_job_recompute({"status": "running", "started_at": started, "processed": 0})
        assert j["elapsed_sec"] == 0

    def test_garbage_started_at_swallowed(self):
        j = df._hot_job_recompute({"status": "running", "started_at": "garbage"})
        assert j["status"] == "running"


class TestHotJobStore:
    def test_get_default_and_stored_copy(self):
        assert df.hot_job_get(lambda k: None)["message"].startswith("Idle")
        stored = {"status": "done", "processed": 3}
        got = df.hot_job_get(lambda k: stored)
        assert got == stored and got is not stored

    def test_get_read_error_gives_default(self):
        def boom(k):
            raise RuntimeError("x")
        assert df.hot_job_get(boom)["status"] == "idle"

    def test_get_uses_hot_job_key(self):
        seen = []
        df.hot_job_get(lambda k: seen.append(k))
        assert seen == [df.HOT_JOB_KEY]

    def test_set_merges_recomputes_and_persists(self):
        written = {}
        started = (datetime.now(df.IST) - timedelta(seconds=10)).isoformat()
        j = df.hot_job_set(lambda k, v, ttl=None: written.update({k: (v, ttl)}), lambda k: None,
                           status="running", started_at=started, processed=1, total=3)
        assert j["elapsed_sec"] >= 9 and "updated_at" in j
        assert written[df.HOT_JOB_KEY][1] == 7 * 86400

    def test_set_write_failure_swallowed(self):
        def boom(k, v, ttl=None):
            raise RuntimeError("x")
        assert df.hot_job_set(boom, lambda k: None, status="done")["status"] == "done"

    def test_premarket_get_default_stored_and_error(self):
        assert "Premarket" in df.hot_premarket_job_get(lambda k: None)["message"]
        assert df.hot_premarket_job_get(lambda k: {"status": "done"})["status"] == "done"

        def boom(k):
            raise RuntimeError("x")
        assert df.hot_premarket_job_get(boom)["status"] == "idle"

    def test_premarket_uses_own_key(self):
        seen = []
        df.hot_premarket_job_get(lambda k: seen.append(k))
        assert seen == [df.HOT_PREMARKET_JOB_KEY]
        assert df.HOT_PREMARKET_JOB_KEY != df.HOT_JOB_KEY

    def test_premarket_set_persists_and_swallows_failure(self):
        written = {}
        df.hot_premarket_job_set(lambda k, v, ttl=None: written.update({k: v}), lambda k: None, status="running")
        assert written[df.HOT_PREMARKET_JOB_KEY]["status"] == "running"

        def boom(k, v, ttl=None):
            raise RuntimeError("x")
        assert df.hot_premarket_job_set(boom, lambda k: None, status="done")["status"] == "done"


class TestHotResult:
    def test_get_primary_legacy_and_missing(self):
        assert df.hot_result_get(lambda k: {"a": 1} if k == df.HOT_RESULT_KEY else None) == {"a": 1}
        assert df.hot_result_get(lambda k: {"b": 2} if k == "stockky:hot_result" else None) == {"b": 2}
        assert df.hot_result_get(lambda k: None) is None
        assert df.hot_result_get(lambda k: "junk") is None

    def test_get_error(self):
        def boom(k):
            raise RuntimeError("x")
        assert df.hot_result_get(boom) is None

    def test_set_mirrors_to_both_keys(self):
        written = {}
        payload = {"x": 1}
        df.hot_result_set(lambda k, v, ttl=None: written.update({k: (v, ttl)}), payload, ttl=5)
        assert set(written) == {df.HOT_RESULT_KEY, "stockky:hot_result"}
        assert written[df.HOT_RESULT_KEY][1] == 5
        assert "persisted_at" in written[df.HOT_RESULT_KEY][0] and "persisted_at" not in payload

    def test_set_keeps_existing_persisted_at(self):
        written = {}
        df.hot_result_set(lambda k, v, ttl=None: written.update({k: v}), {"persisted_at": "old"})
        assert written[df.HOT_RESULT_KEY]["persisted_at"] == "old"

    def test_set_non_dict_noop_and_failure_swallowed(self):
        called = []
        df.hot_result_set(lambda k, v, ttl=None: called.append(k), "junk")
        assert called == []

        def boom(k, v, ttl=None):
            raise RuntimeError("x")
        df.hot_result_set(boom, {"a": 1})


# ── refresh locks ────────────────────────────────────────────────────────────

class FakeRedis:
    def __init__(self, set_result=True, set_raises=None, ttl_val=None, ttl_raises=None,
                 del_raises=None, type_error_first=False):
        self.set_result, self.set_raises, self.ttl_val = set_result, set_raises, ttl_val
        self.ttl_raises, self.del_raises = ttl_raises, del_raises
        self.type_error_first = type_error_first
        self.set_calls, self.deleted = [], []

    def set(self, key, val, **kw):
        self.set_calls.append((key, kw))
        if self.type_error_first and len(self.set_calls) == 1:
            raise TypeError("bad kwargs")
        if self.set_raises:
            raise self.set_raises
        return self.set_result

    def ttl(self, key):
        if self.ttl_raises:
            raise self.ttl_raises
        return self.ttl_val

    def delete(self, key):
        if self.del_raises:
            raise self.del_raises
        self.deleted.append(key)


class TestRefreshLocks:
    def test_no_redis_process_lock_only(self):
        assert df.try_refresh_lock(None, "tcs.ns") is True
        assert df.try_refresh_lock(None, "TCS") is False
        df.release_refresh_lock(None, "TCS")
        assert df.try_refresh_lock(None, "TCS") is True

    def test_expired_process_lock_can_be_retaken(self):
        df._MEM_LOCKS[df.LOCK_PREFIX + "TCS"] = 1.0
        assert df.try_refresh_lock(None, "TCS") is True

    def test_redis_nx_result_returned(self):
        r = FakeRedis(set_result=True)
        assert df.try_refresh_lock(r, "TCS", ttl_sec=7) is True
        assert r.set_calls[0][1] == {"nx": True, "ex": 7}
        r2 = FakeRedis(set_result=None)
        assert df.try_refresh_lock(r2, "INFY") is False

    def test_type_error_retries_with_alternate_kwargs(self):
        r = FakeRedis(type_error_first=True)
        assert df.try_refresh_lock(r, "TCS") is True
        assert len(r.set_calls) == 2

    def test_type_error_twice_fails_open(self):
        class R:
            def set(self, *a, **k):
                raise TypeError("nope")
        assert df.try_refresh_lock(R(), "TCS") is True

    def test_redis_error_fails_open(self):
        assert df.try_refresh_lock(FakeRedis(set_raises=RuntimeError("x")), "TCS") is True

    def test_release_deletes_redis_key_and_swallows_errors(self):
        r = FakeRedis()
        df.try_refresh_lock(r, "TCS")
        df.release_refresh_lock(r, "TCS")
        assert r.deleted == [df.LOCK_PREFIX + "TCS"]
        assert df.LOCK_PREFIX + "TCS" not in df._MEM_LOCKS
        df.release_refresh_lock(FakeRedis(del_raises=RuntimeError("x")), "TCS")

    @pytest.mark.parametrize("client,expected", [
        (None, False), (FakeRedis(ttl_val=5), True), (FakeRedis(ttl_val=10), True),
        (FakeRedis(ttl_val=11), False), (FakeRedis(ttl_val=0), False),
        (FakeRedis(ttl_val=-1), False), (FakeRedis(ttl_val="5"), False),
        (FakeRedis(ttl_raises=RuntimeError("x")), False),
    ])
    def test_soft_ttl_should_refresh(self, client, expected):
        assert df.soft_ttl_should_refresh(client, "k") is expected

    def test_soft_ttl_custom_window(self):
        assert df.soft_ttl_should_refresh(FakeRedis(ttl_val=30), "k", soft_window=60) is True


# ── module-level store helpers ───────────────────────────────────────────────

class TestSingletonAndWrappers:
    def test_singleton_bound_to_kv_and_reused(self, kv):
        a = df.get_data_feed_store()
        assert a is df.get_data_feed_store()
        a._set("k", {"x": 1})
        assert kv.store["k"] == {"x": 1}

    def test_singleton_construction_fallback_when_kv_import_fails(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "kv_cache", None)
        with pytest.raises(TypeError):
            df.get_data_feed_store()
        assert df._feed_store_singleton is None

    def test_singleton_falls_back_to_default_constructor(self, kv, monkeypatch):
        class Stub:
            calls = 0

            def __init__(self, *a):
                Stub.calls += 1
                if a:
                    raise RuntimeError("positional args refused")
        monkeypatch.setattr(df, "DataFeedStore", Stub)
        assert isinstance(df.get_data_feed_store(), Stub)
        assert Stub.calls == 2   # positional attempt raised, bare constructor succeeded

    def test_get_all_stock_feeds(self, kv):
        kv.store[df.DATA_FEED_PREFIX + "TCS"] = rich()
        assert df.get_all_stock_feeds(["TCS", "NOPE"]) == {"TCS": rich()}

    def test_get_all_stock_feeds_failure_returns_empty(self, monkeypatch):
        monkeypatch.setattr(df, "get_data_feed_store", lambda: (_ for _ in ()).throw(RuntimeError("x")))
        assert df.get_all_stock_feeds(["TCS"]) == {}

    def test_save_stock_feed_writes_through(self, kv):
        df.save_stock_feed("TCS", rich(), ttl=11)
        assert df.DATA_FEED_PREFIX + "TCS" in kv.store
        assert (df.DATA_FEED_PREFIX + "TCS", kv.store[df.DATA_FEED_PREFIX + "TCS"], 11) in kv.sets


class TestScoreFromPayload:
    @pytest.mark.parametrize("data,expected", [
        (None, 0.0), ("x", 0.0), ({}, 0.0),
        ({"conviction_score": 61}, 61.0), ({"conviction": "62.5"}, 62.5),
        ({"combined_score": 60}, 60.0), ({"score": 59}, 59.0), ({"decision_score": 58}, 58.0),
        ({"conviction_score": 0, "score": 5}, 5.0),
        ({"conviction_score": "junk", "score": 7}, 7.0),
        ({"metrics": {"conviction": 63}}, 63.0), ({"data": {"score": 64}}, 64.0),
        ({"decision": {"combined_score": 65}}, 65.0),
        ({"metrics": {"score": "junk"}, "data": {"score": 8}}, 8.0),
        ({"metrics": "notadict"}, 0.0), ({"metrics": {"score": 0}}, 0.0),
    ])
    def test_score(self, data, expected):
        assert df._score_from_payload(data) == expected


class TestFindPrepareToBuyCandidates:
    def _seed(self, kv, rows):
        for sym, payload in rows.items():
            kv.store[df.DATA_FEED_PREFIX + sym] = payload
            df._LOCAL_INDEX.add(sym)

    def test_band_is_half_open(self, kv):
        self._seed(kv, {
            "LOW": {"sector": "x", "combined_score": 57.9},
            "EDGELO": {"sector": "x", "combined_score": 58.0},
            "MID": {"sector": "x", "combined_score": 67.9},
            "EDGEHI": {"sector": "x", "combined_score": 68.0},
        })
        assert sorted(df.find_prepare_to_buy_candidates()) == ["EDGELO", "MID"]

    def test_decision_or_action_prepare_matches_outside_band(self, kv):
        self._seed(kv, {
            "A": {"sector": "x", "decision": "Prepare to Buy"},
            "B": {"sector": "x", "action": "prepare to buy", "combined_score": 10},
            "C": {"sector": "x", "decision": "HOLD"},
        })
        assert sorted(df.find_prepare_to_buy_candidates()) == ["A", "B"]

    def test_custom_band(self, kv):
        self._seed(kv, {"A": {"sector": "x", "combined_score": 80}})
        assert df.find_prepare_to_buy_candidates(min_score=75, max_score=90) == ["A"]

    def test_pulls_from_last_scan_blobs_and_dedupes(self, kv):
        self._seed(kv, {"A": {"sector": "x", "combined_score": 60}})
        kv.store["stockky:last_full_scan"] = {"results": [
            {"symbol": "A", "combined_score": 60},          # duplicate of feed row
            {"symbol": "B.NS", "combined_score": 61},
            {"symbol": "C", "decision": "PREPARE TO BUY"},
            {"symbol": "D", "combined_score": 10},
            {"symbol": "", "combined_score": 60}, "junk", {"combined_score": 60},
        ]}
        kv.store["stockky:hot_result_db"] = {"recommendations": [{"symbol": "E", "action": "PREPARE"}]}
        kv.store["stockky:hot_result"] = [{"symbol": "F", "score": 65}]
        assert sorted(df.find_prepare_to_buy_candidates()) == ["A", "B", "C", "E", "F"]

    def test_blob_row_alternate_shapes(self, kv):
        kv.store["stockky:last_full_scan"] = {"all_results": [{"symbol": "X", "score": 60}]}
        kv.store["stockky:hot_result_db"] = {"data": [{"symbol": "Y", "score": 60}]}
        kv.store["stockky:hot_result"] = {"data": "notalist"}
        assert sorted(df.find_prepare_to_buy_candidates()) == ["X", "Y"]

    def test_blank_and_duplicate_feed_symbols_skipped(self, kv, store, monkeypatch):
        self._seed(kv, {"A": {"sector": "x", "combined_score": 60}})
        monkeypatch.setattr(store, "list_symbols", lambda: ["A", "a.ns", "", "  "])
        monkeypatch.setattr(df, "get_data_feed_store", lambda: store)
        assert df.find_prepare_to_buy_candidates() == ["A"]

    def test_list_symbols_failure_and_feed_load_failure(self, kv, store, monkeypatch):
        monkeypatch.setattr(df, "get_data_feed_store", lambda: store)
        monkeypatch.setattr(store, "list_symbols", lambda: (_ for _ in ()).throw(RuntimeError("x")))
        assert df.find_prepare_to_buy_candidates() == []
        monkeypatch.setattr(store, "list_symbols", lambda: ["A"])
        monkeypatch.setattr(df, "get_all_stock_feeds", lambda s: (_ for _ in ()).throw(RuntimeError("x")))
        assert df.find_prepare_to_buy_candidates() == []

    def test_kv_get_failure_on_blob_swallowed(self, kv):
        kv.get_raises = RuntimeError("x")
        assert df.find_prepare_to_buy_candidates() == []


class TestPatchFeedPrice:
    def test_blank_and_bad_prices(self, kv):
        assert df.patch_feed_price("", 10) is False
        for bad in ("abc", None, 0, -5, "0"):
            assert df.patch_feed_price("TCS", bad) is False
        assert kv.sets == []

    def test_over_cap_skipped(self, kv, monkeypatch):
        monkeypatch.setattr(df, "MAX_STOCK_PRICE", 100.0)
        assert df.patch_feed_price("TCS", 500) is False
        assert kv.sets == []

    def test_updates_existing_row_and_keeps_other_fields(self, kv):
        kv.store[df.DATA_FEED_PREFIX + "TCS"] = rich(sector="IT", pe_ratio=25.0)
        assert df.patch_feed_price("tcs.ns", 123.5) is True
        saved = kv.store[df.DATA_FEED_PREFIX + "TCS"]
        assert saved["sector"] == "IT" and saved["pe_ratio"] == 25.0
        assert saved["last_price"] == 123.5 and saved["current_price"] == 123.5
        assert "price_refreshed_at" in saved

    def test_creates_row_when_none_exists(self, kv):
        assert df.patch_feed_price("NEW", "50") is True
        assert df.DATA_FEED_PREFIX + "NEW" in kv.store

    def test_non_dict_existing_treated_as_empty(self, kv, store, monkeypatch):
        monkeypatch.setattr(df, "get_data_feed_store", lambda: store)
        monkeypatch.setattr(store, "get_symbol", lambda s: ["junk"])
        assert df.patch_feed_price("TCS", 10) is True

    def test_put_failure_returns_false(self, kv, store, monkeypatch):
        monkeypatch.setattr(df, "get_data_feed_store", lambda: store)
        monkeypatch.setattr(store, "put_symbol", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        assert df.patch_feed_price("TCS", 10) is False


# ── small yahoo helpers ──────────────────────────────────────────────────────

class TestRateLimitDetect:
    @pytest.mark.parametrize("msg,expected", [
        ("HTTP 429", True), ("Too Many Requests", True), ("rate limit hit", True),
        ("YFRateLimitError: x", True), ("request throttled", True),
        ("connection reset", False), ("", False),
    ])
    def test_signatures(self, msg, expected):
        assert df._is_rate_limit_error(Exception(msg)) is expected


class TestComputeRsi:
    def test_all_gains_is_100(self):
        assert df.compute_rsi_from_closes(list(range(1, 20))) == 100.0

    def test_known_value(self):
        closes = [10, 11] * 8   # alternating +1/-1 → equal gain & loss
        assert df.compute_rsi_from_closes(closes) == pytest.approx(50.0, abs=0.01)

    def test_all_losses_is_zero(self):
        assert df.compute_rsi_from_closes(list(range(20, 1, -1))) == 0.0

    def test_too_few_points_and_nan_filtering(self):
        assert df.compute_rsi_from_closes([1, 2, 3]) is None
        assert df.compute_rsi_from_closes([float("nan")] * 30) is None
        closes = list(range(1, 20)) + [float("nan")]
        assert df.compute_rsi_from_closes(closes) == 100.0

    def test_custom_period_and_bad_input(self):
        assert df.compute_rsi_from_closes([1, 2, 3, 2, 3], period=3) is not None
        assert df.compute_rsi_from_closes(["a", "b"]) is None
        assert df.compute_rsi_from_closes(None) is None


class TestTimeSleep:
    def test_sleeps_clamped_at_zero(self, monkeypatch):
        import time
        seen = []
        monkeypatch.setattr(time, "sleep", lambda s: seen.append(s))
        df._time_module_sleep(-5)
        df._time_module_sleep("1.5")
        assert seen == [0.0, 1.5]


class TestYfCloseVolume:
    def _pd(self):
        return pytest.importorskip("pandas") if hasattr(pytest, "importorskip") else __import__("pandas")

    def test_none_frame(self):
        assert df._yf_close_volume(None, "X.NS") == (None, None)

    def test_single_frame(self):
        pd = self._pd()
        frame = pd.DataFrame({"Close": [1.0, 2.0, None], "Volume": [10, 20, None]})
        assert df._yf_close_volume(frame, "X.NS") == (2.0, 20)

    def test_multiindex_frame(self):
        pd = self._pd()
        cols = pd.MultiIndex.from_product([["A.NS", "B.NS"], ["Close", "Volume"]])
        frame = pd.DataFrame([[1.5, 100, 2.5, 200]], columns=cols)
        assert df._yf_close_volume(frame, "B.NS") == (2.5, 200)
        assert df._yf_close_volume(frame, "Z.NS") == (None, None)

    def test_empty_frame(self):
        pd = self._pd()
        assert df._yf_close_volume(pd.DataFrame({"Close": [], "Volume": []}), "X") == (None, None)

    def test_series_only(self):
        pd = self._pd()
        assert df._yf_close_volume(pd.Series([1.0, 3.0]), "X") == (3.0, None)

    def test_nonpositive_close_dropped_and_bad_volume(self):
        pd = self._pd()
        frame = pd.DataFrame({"Close": [0.0], "Volume": [5]})
        assert df._yf_close_volume(frame, "X") == (None, 5)          # non-positive close discarded
        frame = pd.DataFrame({"Close": [5.0], "Volume": ["abc"]})
        assert df._yf_close_volume(frame, "X") == (5.0, None)        # unparseable volume -> None
        # FIXED: int(float(inf)) raises OverflowError, which the volume cast did not catch, so the
        # outer handler used to throw away a perfectly good close with it. Volume -> None only.
        frame = pd.DataFrame({"Close": [5.0], "Volume": [float("inf")]})
        assert df._yf_close_volume(frame, "X") == (5.0, None)
        frame = pd.DataFrame({"Close": [5.0], "Volume": [float("-inf")]})
        assert df._yf_close_volume(frame, "X") == (5.0, None)

    def test_all_nan_columns(self):
        pd = self._pd()
        frame = pd.DataFrame({"Close": [None], "Volume": [None]})
        assert df._yf_close_volume(frame, "X") == (None, None)

    def test_weird_object_returns_none_pair(self):
        class Bad:
            empty = False
            columns = None
        assert df._yf_close_volume(Bad(), "X") == (None, None)

    def test_no_pandas_import_still_works_for_none(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "pandas", None)
        assert df._yf_close_volume(None, "X") == (None, None)


# ── bulk price feeder: market-data-service /quotes/bulk ──────────────────────

class FakeResp:
    def __init__(self, status_code=200, body=None, text="", content=None):
        self.status_code = status_code
        self._body = body
        self.text = text
        self.content = content if content is not None else (b"x" if (body is not None or text) else b"")

    def json(self):
        return self._body


def fake_httpx(post=None, client_factory=None):
    m = types.ModuleType("httpx")
    if post is not None:
        m.post = post
    if client_factory is not None:
        m.Client = client_factory
    return m


class TestBulkYahooDownloadPrices:
    def test_httpx_missing_returns_empty(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "httpx", None)
        assert df.bulk_yahoo_download_prices(["TCS"]) == {}

    def test_no_symbols(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "httpx", fake_httpx(post=lambda *a, **k: 1 / 0))
        assert df.bulk_yahoo_download_prices([]) == {}
        assert df.bulk_yahoo_download_prices(None) == {}
        assert df.bulk_yahoo_download_prices(["", "  "]) == {}

    def test_success_seeds_and_shapes_records(self, monkeypatch):
        calls = []
        body = {"quotes": [
            {"symbol": "tcs.ns", "price": 100.0, "fetched_at": "T0", "extra": None},
            {"symbol": "INFY", "cmp": "50", "pe_ratio": 30.0, "roce": 12.0,
             "sentiment_score": 0.9, "source": "custom", "price_refreshed_at": "T1"},
            {"symbol": "ZERO", "price": 0},
            {"symbol": "BAD", "price": "abc"},
            {"price": 5}, {"symbol": ".NS", "price": 5}, "junk",
        ]}

        def post(url, json=None, timeout=None):
            calls.append((url, json, timeout))
            return FakeResp(200, body)
        monkeypatch.setitem(sys.modules, "httpx", fake_httpx(post=post))
        monkeypatch.setenv("MARKET_DATA_URL", "https://mds.example/")
        out = df.bulk_yahoo_download_prices(["tcs.ns", "TCS", "INFY", "MISSING"])
        assert calls[0][0] == "https://mds.example/quotes/bulk"
        assert calls[0][1] == {"symbols": ["TCS", "INFY", "MISSING"]}
        assert calls[0][2] == 60.0
        tcs = out["TCS"]
        assert tcs["source"] == "yahoo_bulk" and tcs["price_refreshed_at"] == "T0"
        assert tcs["pe_ratio"] == 22.5 and tcs["pe_ratio_seed"] is True
        assert tcs["roce"] == 15.0 and tcs["roce_seed"] is True
        assert tcs["sentiment_score"] == 0.65 and tcs["sentiment_seed"] is True
        assert "extra" not in tcs
        infy = out["INFY"]
        assert infy["pe_ratio"] == 30.0 and "pe_ratio_seed" not in infy
        assert infy["source"] == "custom" and infy["price_refreshed_at"] == "T1"
        assert out["MISSING"]["source"] == "yahoo_missing" and out["MISSING"]["pe_ratio_seed"] is True
        assert set(out) == {"TCS", "INFY", "MISSING"}

    def test_null_seed_fields_get_seeded(self, monkeypatch):
        body = {"quotes": [{"symbol": "A", "price": 1, "pe_ratio": None, "roce": None, "sentiment_score": None}]}
        monkeypatch.setitem(sys.modules, "httpx", fake_httpx(post=lambda *a, **k: FakeResp(200, body)))
        rec = df.bulk_yahoo_download_prices(["A"])["A"]
        assert rec["pe_ratio_seed"] and rec["roce_seed"] and rec["sentiment_seed"]

    def test_price_cap_skips_and_seeds_placeholder(self, monkeypatch):
        monkeypatch.setattr(df, "MAX_STOCK_PRICE", 50.0)
        body = {"quotes": [{"symbol": "BIG", "price": 900}, {"symbol": "OK", "price": 10}]}
        monkeypatch.setitem(sys.modules, "httpx", fake_httpx(post=lambda *a, **k: FakeResp(200, body)))
        out = df.bulk_yahoo_download_prices(["BIG", "OK"])
        assert out["OK"]["source"] == "yahoo_bulk"
        assert out["BIG"]["source"] == "yahoo_missing"

    def test_empty_body_and_missing_quotes_key(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "httpx", fake_httpx(post=lambda *a, **k: FakeResp(200, None, content=b"")))
        assert df.bulk_yahoo_download_prices(["A"])["A"]["source"] == "yahoo_missing"
        monkeypatch.setitem(sys.modules, "httpx", fake_httpx(post=lambda *a, **k: FakeResp(200, {})))
        assert df.bulk_yahoo_download_prices(["A"])["A"]["source"] == "yahoo_missing"

    def test_http_error_seeds_placeholders(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "httpx",
                            fake_httpx(post=lambda *a, **k: FakeResp(503, text="upstream down" * 50)))
        out = df.bulk_yahoo_download_prices(["A", "B"])
        assert {v["source"] for v in out.values()} == {"yahoo_missing"}

    def test_exception_seeds_placeholders(self, monkeypatch):
        def post(*a, **k):
            raise RuntimeError("network down")
        monkeypatch.setitem(sys.modules, "httpx", fake_httpx(post=post))
        assert df.bulk_yahoo_download_prices(["A"])["A"]["source"] == "yahoo_missing"

    def test_default_market_data_url(self, monkeypatch):
        seen = []
        monkeypatch.delenv("MARKET_DATA_URL", raising=False)
        monkeypatch.setitem(sys.modules, "httpx",
                            fake_httpx(post=lambda url, **k: seen.append(url) or FakeResp(200, {"quotes": []})))
        df.bulk_yahoo_download_prices(["A"])
        assert seen == ["https://market-data-service-r6d7.onrender.com/quotes/bulk"]


# ── NSE bhavcopy ─────────────────────────────────────────────────────────────

BHAV_CSV = (
    "SYMBOL, SERIES, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, CLOSE_PRICE, PREV_CLOSE, TTL_TRD_QNTY\n"
    "TCS, EQ, 99, 105, 98, 102.5, 100, 1000\n"
    "INFY, BE, 10, 12, 9, 11, , 5\n"
    "ITC, XX, 1, 1, 1, 1, 1, 1\n"
    ", EQ, 1, 1, 1, 1, 1, 1\n"
    "ZERO, EQ, 1, 1, 1, 0, 1, 1\n"
    "NOCLOSE, EQ, 1, 1, 1, , 1, 1\n"
    'COMMA,EQ,"1,000","1,100",900,"1,050.5","1,000","2,000"\n'
    "BADNUM, EQ, x, y, z, 20, junk, w\n"
)


class FakeClient:
    """httpx.Client stand-in. `routes` maps url -> FakeResp (or exception); default 404."""

    instances = []

    def __init__(self, routes=None, home_raises=False, on_get=None, **kw):
        self.routes = routes if routes is not None else {}
        self.home_raises = home_raises
        self.on_get = on_get
        self.kw = kw
        self.gets = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url):
        self.gets.append(url)
        if url == "https://www.nseindia.com":
            if self.home_raises:
                raise RuntimeError("blocked")
            return FakeResp(200, text="ok")
        if self.on_get:
            self.on_get(url)
        r = self.routes.get(url, FakeResp(404))
        if isinstance(r, Exception):
            raise r
        return r


def install_client(monkeypatch, **cfg):
    holder = {}

    def factory(**kw):
        c = FakeClient(**cfg)
        c.kw = kw
        holder["client"] = c
        return c
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx(client_factory=factory))
    return holder


def any_url_serves(text, status=200, content=None):
    class Routes(dict):
        def get(self, url, default=None):
            return FakeResp(status, text=text, content=content)
    return Routes()


class TestBhavHelpers:
    def _freeze(self, monkeypatch, y, m, d, hh):
        real = datetime

        class FakeDT(real):
            @classmethod
            def now(cls, tz=None):
                return real(y, m, d, hh, 0, 0, tzinfo=tz)
        monkeypatch.setattr(df, "datetime", FakeDT)

    def test_before_6pm_starts_yesterday_and_skips_weekends(self, monkeypatch):
        self._freeze(monkeypatch, 2026, 9, 30, 10)      # Wed 10:00 IST
        dates = df._bhav_candidate_dates(4)
        assert [d.isoformat() for d in dates] == ["2026-09-29", "2026-09-28", "2026-09-25", "2026-09-24"]

    def test_after_6pm_starts_today(self, monkeypatch):
        self._freeze(monkeypatch, 2026, 9, 30, 19)
        assert df._bhav_candidate_dates(1)[0].isoformat() == "2026-09-30"

    def test_weekend_start_skips_to_friday(self, monkeypatch):
        self._freeze(monkeypatch, 2026, 9, 27, 20)      # Sunday evening
        assert df._bhav_candidate_dates(1)[0].isoformat() == "2026-09-25"

    def test_default_count_is_six(self, monkeypatch):
        self._freeze(monkeypatch, 2026, 9, 30, 19)
        assert len(df._bhav_candidate_dates()) == 6

    def test_urls_for_date(self):
        urls = df._bhav_urls_for_date(datetime(2026, 9, 5).date())
        assert len(urls) == 3
        assert all("sec_bhavdata_full_05092026.csv" in u for u in urls)


class TestDownloadBhavcopy:
    def _dates(self, monkeypatch, n=1):
        monkeypatch.setattr(df, "_bhav_candidate_dates",
                            lambda k=6: [datetime(2026, 9, 29 - i).date() for i in range(n)])

    def test_parses_csv_and_caches(self, monkeypatch):
        self._dates(monkeypatch)
        holder = install_client(monkeypatch, routes=any_url_serves(BHAV_CSV))
        out = df.download_nse_bhavcopy_bulk()
        assert set(out) == {"TCS", "INFY", "COMMA", "BADNUM"}
        tcs = out["TCS"]
        assert tcs["price"] == tcs["close"] == tcs["cmp"] == tcs["ltp"] == 102.5
        assert tcs["previous_close"] == 100.0 and tcs["day_change_pct"] == 2.5
        assert tcs["open"] == 99.0 and tcs["day_high"] == 105.0 and tcs["day_low"] == 98.0
        assert tcs["volume"] == 1000 and tcs["source"] == "nse_bhavcopy"
        assert out["INFY"]["previous_close"] == 11.0 and out["INFY"]["day_change_pct"] == 0.0
        assert out["COMMA"]["price"] == 1050.5 and out["COMMA"]["volume"] == 2000
        bad = out["BADNUM"]
        assert bad["price"] == 20.0 and bad["previous_close"] == 20.0
        assert "open" not in bad and "volume" not in bad
        assert holder["client"].kw["follow_redirects"] is True
        # second call is served from cache, no new client
        holder.clear()
        assert df.download_nse_bhavcopy_bulk() is out
        assert holder == {}

    def test_force_bypasses_cache_and_expired_cache_refetches(self, monkeypatch):
        self._dates(monkeypatch)
        df._BHAV_CACHE["data"] = {"OLD": {}}
        df._BHAV_CACHE["fetched_at"] = __import__("time").time()
        install_client(monkeypatch, routes=any_url_serves(BHAV_CSV))
        assert "TCS" in df.download_nse_bhavcopy_bulk(force=True)
        df._BHAV_CACHE["data"] = {"OLD": {}}
        df._BHAV_CACHE["fetched_at"] = 1.0
        assert "TCS" in df.download_nse_bhavcopy_bulk()

    def test_fresh_cache_returned_without_download(self, monkeypatch):
        df._BHAV_CACHE["data"] = {"CACHED": {}}
        df._BHAV_CACHE["fetched_at"] = __import__("time").time()
        monkeypatch.setitem(sys.modules, "httpx", None)
        assert df.download_nse_bhavcopy_bulk() == {"CACHED": {}}

    def test_price_cap_filters_rows(self, monkeypatch):
        self._dates(monkeypatch)
        monkeypatch.setattr(df, "MAX_STOCK_PRICE", 50.0)
        install_client(monkeypatch, routes=any_url_serves(BHAV_CSV))
        assert set(df.download_nse_bhavcopy_bulk()) == {"INFY", "BADNUM"}

    def test_alternate_column_names_and_missing_series_column(self, monkeypatch):
        self._dates(monkeypatch)
        csv_text = "SYMBOL,CLOSE,OPEN,HIGH,LOW,PREVCLOSE,TOTTRDQTY\nABC,10,9,11,8,9,7\n"
        install_client(monkeypatch, routes=any_url_serves(csv_text))
        out = df.download_nse_bhavcopy_bulk()
        assert out["ABC"]["price"] == 10.0 and out["ABC"]["volume"] == 7
        assert out["ABC"]["previous_close"] == 9.0

    def test_no_prev_or_volume_columns(self, monkeypatch):
        self._dates(monkeypatch)
        install_client(monkeypatch, routes=any_url_serves("SYMBOL,CLOSE\nABC,10\n"))
        rec = df.download_nse_bhavcopy_bulk()["ABC"]
        assert rec["previous_close"] == 10.0 and "volume" not in rec and "open" not in rec

    @pytest.mark.parametrize("text,status,content", [
        ("SYMBOL,CLOSE\nA,1\n", 404, None),           # non-200
        ("SYMBOL,CLOSE\nA,1\n", 200, b""),            # empty content
        ("<html>blocked</html>", 200, None),          # no SYMBOL in head
        ("\nSYMBOL,CLOSE\nA,1\n", 200, None),         # blank first row → no fieldnames
        ("SYMBOL\nA\n", 200, None),                   # no close column
        ("SYMBOL,CLOSE\nA,0\nB,\n", 200, None),       # rows all filtered → empty out
    ])
    def test_unusable_responses_return_empty_and_are_not_cached(self, monkeypatch, text, status, content):
        self._dates(monkeypatch)
        install_client(monkeypatch, routes=any_url_serves(text, status, content))
        assert df.download_nse_bhavcopy_bulk() == {}
        assert df._BHAV_CACHE["data"] is None

    def test_url_exception_continues_to_next_url(self, monkeypatch):
        self._dates(monkeypatch)
        urls = df._bhav_urls_for_date(datetime(2026, 9, 29).date())
        routes = {urls[0]: RuntimeError("boom"), urls[1]: FakeResp(200, text="SYMBOL,CLOSE\nA,5\n")}
        install_client(monkeypatch, routes=routes)
        assert "A" in df.download_nse_bhavcopy_bulk()

    def test_falls_through_to_older_date(self, monkeypatch):
        self._dates(monkeypatch, n=2)
        newer = df._bhav_urls_for_date(datetime(2026, 9, 29).date())
        older = df._bhav_urls_for_date(datetime(2026, 9, 28).date())
        routes = {older[2]: FakeResp(200, text="SYMBOL,CLOSE\nOLD,5\n")}
        holder = install_client(monkeypatch, routes=routes)
        assert "OLD" in df.download_nse_bhavcopy_bulk()
        assert holder["client"].gets[1:4] == newer

    def test_homepage_warmup_failure_ignored(self, monkeypatch):
        self._dates(monkeypatch)
        install_client(monkeypatch, routes=any_url_serves("SYMBOL,CLOSE\nA,5\n"), home_raises=True)
        assert "A" in df.download_nse_bhavcopy_bulk()

    def test_deadline_stops_before_any_fetch(self, monkeypatch):
        self._dates(monkeypatch)
        monkeypatch.setenv("BHAV_BULK_MAX_SEC", "-1")
        holder = install_client(monkeypatch, routes=any_url_serves(BHAV_CSV))
        assert df.download_nse_bhavcopy_bulk() == {}
        assert holder["client"].gets == ["https://www.nseindia.com"]

    def test_stop_flag_before_start(self, monkeypatch):
        self._dates(monkeypatch)
        df.request_data_feed_stop()
        holder = install_client(monkeypatch, routes=any_url_serves(BHAV_CSV))
        assert df.download_nse_bhavcopy_bulk() == {}
        assert holder["client"].gets == ["https://www.nseindia.com"]

    def test_stop_flag_mid_urls_breaks_inner_then_outer(self, monkeypatch):
        self._dates(monkeypatch, n=3)
        holder = install_client(monkeypatch, on_get=lambda url: df.request_data_feed_stop())
        assert df.download_nse_bhavcopy_bulk() == {}
        # first URL fetched (which set the flag); nothing else attempted afterwards
        assert len(holder["client"].gets) == 2

    def test_client_construction_failure_returns_empty(self, monkeypatch):
        def factory(**kw):
            raise RuntimeError("no client")
        monkeypatch.setitem(sys.modules, "httpx", fake_httpx(client_factory=factory))
        assert df.download_nse_bhavcopy_bulk() == {}


# ── run_bulk_yahoo_price_feed ────────────────────────────────────────────────

def bhav_rows(*syms, price=10.0):
    return {s: {"symbol": s, "price": price, "close": price, "source": "nse_bhavcopy"} for s in syms}


@pytest.fixture
def feed(kv, store, monkeypatch):
    """Store bound into the module + hooks for the two network phases and the market clock."""
    monkeypatch.setattr(df, "get_data_feed_store", lambda: store)
    state = types.SimpleNamespace(bhav={}, bhav_raises=None, yahoo={}, yahoo_raises=None,
                                  open=False, bhav_calls=0, yahoo_calls=[])

    def fake_bhav(*a, **k):
        state.bhav_calls += 1
        if state.bhav_raises:
            raise state.bhav_raises
        return state.bhav

    def fake_yahoo(targets):
        state.yahoo_calls.append(list(targets))
        if state.yahoo_raises:
            raise state.yahoo_raises
        return state.yahoo

    monkeypatch.setattr(df, "download_nse_bhavcopy_bulk", fake_bhav)
    monkeypatch.setattr(df, "bulk_yahoo_download_prices", fake_yahoo)
    monkeypatch.setattr(df, "_is_nse_session_open", lambda: state.open)
    state.kv, state.store = kv, store
    return state


class TestRunBulkYahooPriceFeed:
    def test_no_symbols_and_empty_index(self, feed):
        r = df.run_bulk_yahoo_price_feed()
        assert r["status"] == "error" and r["tracked_stocks"] == 0 and r["symbols"] == []

    def test_index_read_failure_treated_as_empty(self, feed, monkeypatch):
        monkeypatch.setattr(feed.store, "list_symbols", lambda: (_ for _ in ()).throw(RuntimeError("x")))
        assert df.run_bulk_yahoo_price_feed()["status"] == "error"

    def test_symbols_default_to_feed_index(self, feed):
        df._LOCAL_INDEX.update({"A", "B"})
        feed.bhav = bhav_rows("A", "B")
        r = df.run_bulk_yahoo_price_feed()
        assert r["status"] == "success" and r["symbols"] == ["A", "B"]

    def test_market_closed_good_coverage_is_bhavcopy_only(self, feed):
        feed.bhav = bhav_rows("A", "B", "C")
        r = df.run_bulk_yahoo_price_feed(["a.ns", "A", "B", "C", "D", ""])
        assert r["bulk_mode"] == "bhavcopy_only" and r["tracked_stocks"] == 3
        assert r["bhavcopy_hits"] == 3 and r["yahoo_hits"] == 0 and r["market_open"] is False
        assert r["requested"] == 6 and r["symbols"] == ["A", "B", "C"]
        assert feed.yahoo_calls == []
        # seeds were written with defaults and the seed marker
        row = feed.kv.store[df.DATA_FEED_PREFIX + "A"]
        assert row["rsi"] == 50.0 and row["sentiment_score"] == 50.0 and row["_seed"] is True
        assert "pe_ratio" not in row and "roce" not in row       # None values are stripped
        assert feed.kv.store[df.DATA_FEED_JOB_KEY]["status"] == "running"

    def test_seed_keeps_existing_rsi_and_source(self, feed):
        feed.bhav = {"A": {"symbol": "A", "price": 5, "rsi": 61.0, "pe_ratio": 9.0, "source": ""}}
        df.run_bulk_yahoo_price_feed(["A"])
        row = feed.kv.store[df.DATA_FEED_PREFIX + "A"]
        assert row["rsi"] == 61.0 and row["pe_ratio"] == 9.0 and row["source"] == "nse_bhavcopy"

    def test_low_coverage_closed_market_falls_to_yahoo_for_missed_only(self, feed):
        feed.bhav = bhav_rows("A")
        feed.yahoo = {"B": {"symbol": "B", "price": 7, "source": "yahoo_bulk"},
                      "C": {"symbol": "C", "source": "yahoo_missing"}}
        r = df.run_bulk_yahoo_price_feed(["A", "B", "C", "D", "E", "F", "G", "H"])
        assert feed.yahoo_calls == [["B", "C", "D", "E", "F", "G", "H"]]
        assert r["bulk_mode"] == "bhavcopy+yahoo_bulk" and r["yahoo_hits"] == 2
        assert r["yahoo_calls_needed_for"] == 7 and r["tracked_stocks"] == 3
        assert r["symbols"] == ["A", "B", "C"]

    def test_market_open_sends_everything_to_yahoo_and_counts_new_symbols_once(self, feed):
        feed.open = True
        feed.bhav = bhav_rows("A", "B")
        feed.yahoo = {"A": {"symbol": "A", "price": 11}, "B": {"symbol": "B", "price": 12}}
        r = df.run_bulk_yahoo_price_feed(["A", "B"])
        assert feed.yahoo_calls == [["A", "B"]]
        assert r["tracked_stocks"] == 2               # already saved via bhavcopy, not double-counted
        assert r["market_open"] is True and r["bulk_mode"] == "bhavcopy+yahoo_bulk"

    def test_yahoo_returns_nothing_is_bhavcopy_only_mode(self, feed):
        feed.open = True
        feed.bhav = bhav_rows("A")
        r = df.run_bulk_yahoo_price_feed(["A"])
        assert r["bulk_mode"] == "bhavcopy_only" and r["yahoo_hits"] == 0

    def test_bhav_download_failure_falls_back_to_yahoo(self, feed):
        feed.bhav_raises = RuntimeError("nse blocked")
        feed.yahoo = {"A": {"symbol": "A", "price": 3}}
        r = df.run_bulk_yahoo_price_feed(["A"])
        assert r["bhavcopy_hits"] == 0 and r["tracked_stocks"] == 1 and feed.yahoo_calls == [["A"]]

    def test_bhavcopy_disabled(self, feed):
        feed.bhav = bhav_rows("A")
        feed.yahoo = {"A": {"symbol": "A", "price": 3}}
        r = df.run_bulk_yahoo_price_feed(["A"], use_bhavcopy_baseline=False)
        assert feed.bhav_calls == 0 and r["bhavcopy_hits"] == 0 and r["tracked_stocks"] == 1

    def test_bhav_returning_none_treated_as_empty(self, feed):
        feed.bhav = None
        r = df.run_bulk_yahoo_price_feed(["A"])
        assert r["bhavcopy_hits"] == 0

    def test_unpreparable_bhav_row_is_skipped_and_counted(self, feed):
        feed.bhav = {"A": 5, "B": {"symbol": "B", "price": 1}}
        r = df.run_bulk_yahoo_price_feed(["A", "B"])
        assert r["skipped"] == 1 and r["bhavcopy_hits"] == 1

    def test_stop_after_bhavcopy_phase(self, feed):
        feed.bhav = bhav_rows("A", "B")
        real = feed.store.put_symbols_bulk

        def stop_then_write(*a, **k):
            n = real(*a, **k)
            df.request_data_feed_stop()
            return n
        feed.store.put_symbols_bulk = stop_then_write
        r = df.run_bulk_yahoo_price_feed(["A", "B", "C"])
        assert r["status"] == "stopped" and r["tracked_stocks"] == 2 and r["requested"] == 3
        assert r["symbols"] == ["A", "B"] and feed.yahoo_calls == []

    def test_bhav_bulk_write_failure_falls_back_per_symbol(self, feed, monkeypatch):
        feed.bhav = bhav_rows("A", "B")
        monkeypatch.setattr(feed.store, "put_symbols_bulk", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        real_put = feed.store.put_symbol

        def flaky_put(sym, payload, *a, **k):
            if sym == "B":
                raise RuntimeError("write failed")
            return real_put(sym, payload, *a, **k)
        monkeypatch.setattr(feed.store, "put_symbol", flaky_put)
        r = df.run_bulk_yahoo_price_feed(["A", "B"])
        assert r["skipped"] == 1 and r["tracked_stocks"] == 1 and r["symbols"] == ["A"]

    def test_yahoo_exception_yields_no_prices(self, feed):
        feed.open = True
        feed.yahoo_raises = RuntimeError("mds down")
        r = df.run_bulk_yahoo_price_feed(["A"])
        assert r["yahoo_hits"] == 0 and r["tracked_stocks"] == 0

    def test_yahoo_bulk_write_failure_falls_back_per_symbol(self, feed, monkeypatch):
        feed.open = True
        feed.yahoo = {"A": {"symbol": "A", "price": 1}, "B": {"symbol": "B", "price": 2}}
        monkeypatch.setattr(feed.store, "put_symbols_bulk", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        real_put = feed.store.put_symbol

        def flaky_put(sym, payload, *a, **k):
            if sym == "B":
                raise RuntimeError("write failed")
            return real_put(sym, payload, *a, **k)
        monkeypatch.setattr(feed.store, "put_symbol", flaky_put)
        r = df.run_bulk_yahoo_price_feed(["A", "B"])
        assert r["tracked_stocks"] == 1 and r["skipped"] == 1 and r["symbols"] == ["A"]

    def test_yahoo_fallback_does_not_double_count_saved_symbol(self, feed, monkeypatch):
        feed.open = True
        feed.bhav = bhav_rows("A")
        feed.yahoo = {"A": {"symbol": "A", "price": 1}}
        real_bulk = feed.store.put_symbols_bulk
        calls = {"n": 0}

        def bulk(*a, **k):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("second write fails")
            return real_bulk(*a, **k)
        monkeypatch.setattr(feed.store, "put_symbols_bulk", bulk)
        assert df.run_bulk_yahoo_price_feed(["A"])["tracked_stocks"] == 1

    def test_progress_failures_are_swallowed(self, feed, monkeypatch):
        feed.bhav = bhav_rows("A")
        monkeypatch.setattr(feed.store, "set_job", lambda **k: (_ for _ in ()).throw(RuntimeError("x")))
        assert df.run_bulk_yahoo_price_feed(["A"])["status"] == "success"

    def test_market_clock_failure_uses_surprise_scanner_clock(self, feed, monkeypatch):
        monkeypatch.setattr(df, "_is_nse_session_open", lambda: (_ for _ in ()).throw(RuntimeError("x")))
        fake = types.ModuleType("surprise_scanner")
        fake.is_market_open_ist = lambda: True
        monkeypatch.setitem(sys.modules, "surprise_scanner", fake)
        feed.bhav = bhav_rows("A")
        feed.yahoo = {"A": {"symbol": "A", "price": 1}}
        assert df.run_bulk_yahoo_price_feed(["A"])["market_open"] is True

    def test_both_market_clocks_failing_means_closed(self, feed, monkeypatch):
        monkeypatch.setattr(df, "_is_nse_session_open", lambda: (_ for _ in ()).throw(RuntimeError("x")))
        monkeypatch.setitem(sys.modules, "surprise_scanner", None)
        feed.bhav = bhav_rows("A")
        assert df.run_bulk_yahoo_price_feed(["A"])["market_open"] is False

    def test_message_mentions_counts(self, feed):
        feed.bhav = bhav_rows("A")
        r = df.run_bulk_yahoo_price_feed(["A"])
        assert "1/1" in r["message"]


class TestBootHealAndUniverse:
    def test_running_job_is_healed_and_stop_flag_cleared(self, feed):
        feed.kv.store[df.DATA_FEED_JOB_KEY] = {"status": "running"}
        df.request_data_feed_stop()
        r = df.clear_stuck_feed_job_on_boot()
        assert r == {"healed": True, "previous_status": "running"}
        assert feed.kv.store[df.DATA_FEED_JOB_KEY]["status"] == "idle"
        assert df.data_feed_stop_requested() is False

    def test_stopping_status_also_healed(self, feed):
        feed.kv.store[df.DATA_FEED_JOB_KEY] = {"status": "Stopping"}
        assert df.clear_stuck_feed_job_on_boot()["healed"] is True

    def test_idle_job_not_touched_but_stop_flag_cleared(self, feed):
        feed.kv.store[df.DATA_FEED_JOB_KEY] = {"status": "done"}
        df.request_data_feed_stop()
        r = df.clear_stuck_feed_job_on_boot()
        assert r == {"healed": False, "status": "done", "reason": "job not stuck"}
        assert df.data_feed_stop_requested() is False
        assert feed.kv.store[df.DATA_FEED_JOB_KEY]["status"] == "done"

    def test_no_job_at_all_is_idle(self, feed):
        assert df.clear_stuck_feed_job_on_boot()["status"] == "idle"

    def test_clear_stop_failure_swallowed_when_not_stuck(self, feed, monkeypatch):
        monkeypatch.setattr(df, "clear_data_feed_stop", lambda: (_ for _ in ()).throw(RuntimeError("x")))
        assert df.clear_stuck_feed_job_on_boot()["healed"] is False

    def test_job_read_failure(self, feed, monkeypatch):
        monkeypatch.setattr(feed.store, "job", lambda: (_ for _ in ()).throw(RuntimeError("cannot read " + "x" * 300)))
        r = df.clear_stuck_feed_job_on_boot()
        assert r["healed"] is False and len(r["reason"]) == 120

    def test_heal_write_failure(self, feed, monkeypatch):
        feed.kv.store[df.DATA_FEED_JOB_KEY] = {"status": "running"}
        monkeypatch.setattr(feed.store, "set_job", lambda **k: (_ for _ in ()).throw(RuntimeError("nope")))
        assert df.clear_stuck_feed_job_on_boot() == {"healed": False, "reason": "nope"}

    def test_universe_price_filter(self, feed):
        for sym, px in {"CHEAP": 10, "EDGE": 100, "PRICEY": 500, "UNKNOWN": 0}.items():
            feed.kv.store[df.DATA_FEED_PREFIX + sym] = {"sector": "x", "price": px}
            df._LOCAL_INDEX.add(sym)
        assert sorted(df.list_feed_symbols_from_neon_under_max_price(100)) == ["CHEAP", "EDGE", "UNKNOWN"]

    def test_universe_uses_module_cap_by_default_and_no_cap_keeps_all(self, feed, monkeypatch):
        feed.kv.store[df.DATA_FEED_PREFIX + "PRICEY"] = {"sector": "x", "price": 500}
        df._LOCAL_INDEX.add("PRICEY")
        assert df.list_feed_symbols_from_neon_under_max_price() == ["PRICEY"]      # cap 0 = unrestricted
        monkeypatch.setattr(df, "MAX_STOCK_PRICE", 100.0)
        assert df.list_feed_symbols_from_neon_under_max_price() == []
        assert df.list_feed_symbols_from_neon_under_max_price(0) == ["PRICEY"]

    def test_universe_edge_cases(self, feed, monkeypatch):
        assert df.list_feed_symbols_from_neon_under_max_price(10) == []           # empty index
        monkeypatch.setattr(feed.store, "list_symbols", lambda: ["A", "", "b.ns"])
        monkeypatch.setattr(df, "get_all_stock_feeds", lambda s: (_ for _ in ()).throw(RuntimeError("x")))
        assert df.list_feed_symbols_from_neon_under_max_price(10) == ["A", "B"]    # unknown price kept
        monkeypatch.setattr(feed.store, "list_symbols", lambda: (_ for _ in ()).throw(RuntimeError("x")))
        assert df.list_feed_symbols_from_neon_under_max_price(10) == []

    def test_universe_non_dict_feed_row(self, feed, monkeypatch):
        monkeypatch.setattr(feed.store, "list_symbols", lambda: ["A"])
        monkeypatch.setattr(df, "get_all_stock_feeds", lambda s: {"A": "junk"})
        assert df.list_feed_symbols_from_neon_under_max_price(10) == ["A"]


# ── NSE session clock ────────────────────────────────────────────────────────

class TestNseSessionOpen:
    def _freeze(self, monkeypatch, y, m, d, hh, mm):
        import datetime as dtmod
        real = dtmod.datetime

        class FakeDT(real):
            @classmethod
            def now(cls, tz=None):
                return real(y, m, d, hh, mm, 0, tzinfo=tz)
        monkeypatch.setattr(dtmod, "datetime", FakeDT)

    def _holidays(self, monkeypatch, fn):
        fake = types.ModuleType("nse_holidays")
        fake.is_nse_holiday = fn
        monkeypatch.setitem(sys.modules, "nse_holidays", fake)

    @pytest.mark.parametrize("hh,mm,expected", [
        (9, 14, False), (9, 15, True), (12, 0, True), (15, 30, True), (15, 31, False), (3, 0, False),
    ])
    def test_weekday_boundaries(self, monkeypatch, hh, mm, expected):
        self._freeze(monkeypatch, 2026, 9, 30, hh, mm)             # Wednesday
        self._holidays(monkeypatch, lambda d: False)
        assert df._is_nse_session_open() is expected

    def test_weekend_closed(self, monkeypatch):
        self._freeze(monkeypatch, 2026, 9, 26, 11, 0)              # Saturday
        assert df._is_nse_session_open() is False

    def test_holiday_closed(self, monkeypatch):
        self._freeze(monkeypatch, 2026, 9, 30, 11, 0)
        self._holidays(monkeypatch, lambda d: True)
        assert df._is_nse_session_open() is False

    def test_holiday_lookup_failure_is_ignored(self, monkeypatch):
        self._freeze(monkeypatch, 2026, 9, 30, 11, 0)
        self._holidays(monkeypatch, lambda d: (_ for _ in ()).throw(RuntimeError("x")))
        assert df._is_nse_session_open() is True

    def test_clock_failure_means_closed(self, monkeypatch):
        import datetime as dtmod

        class Boom(dtmod.datetime):
            @classmethod
            def now(cls, tz=None):
                raise RuntimeError("no clock")
        monkeypatch.setattr(dtmod, "datetime", Boom)
        assert df._is_nse_session_open() is False


# ── shared bulk quote cache ──────────────────────────────────────────────────

class TestBulkQuoteCache:
    def test_get_returns_dict_only(self, kv):
        assert df.get_bulk_quote_cache() == {}
        kv.store[df.BULK_QUOTE_CACHE_KEY] = {"quotes": {"A": {"price": 1}}}
        assert df.get_bulk_quote_cache() == {"quotes": {"A": {"price": 1}}}
        kv.store[df.BULK_QUOTE_CACHE_KEY] = ["junk"]
        assert df.get_bulk_quote_cache() == {}

    def test_get_failure_returns_empty(self, kv):
        kv.get_raises = RuntimeError("x")
        assert df.get_bulk_quote_cache() == {}

    def test_set_market_open_uses_short_ttl(self, kv, monkeypatch):
        monkeypatch.setattr(df, "_is_nse_session_open", lambda: True)
        payload = df.set_bulk_quote_cache({"tcs": {"price": 1}, "_skip": 1, "INFY": {"price": 2}}, source="x")
        assert payload["_meta"]["ttl_sec"] == df.BULK_QUOTE_OPEN_TTL
        assert payload["_meta"]["market_open"] is True and payload["_meta"]["source"] == "x"
        assert payload["_meta"]["count"] == 2
        assert set(payload["quotes"]) == {"TCS", "INFY"}
        key, stored, ttl = kv.sets[-1]
        assert key == df.BULK_QUOTE_CACHE_KEY and ttl == df.BULK_QUOTE_OPEN_TTL and stored == payload

    def test_set_market_closed_uses_long_ttl(self, kv, monkeypatch):
        monkeypatch.setattr(df, "_is_nse_session_open", lambda: False)
        payload = df.set_bulk_quote_cache({"A": {"price": 1}})
        assert payload["_meta"]["ttl_sec"] == df.BULK_QUOTE_CLOSED_TTL
        assert payload["_meta"]["source"] == "yahoo_bulk"

    def test_set_write_failure_still_returns_payload(self, kv, monkeypatch):
        monkeypatch.setattr(df, "_is_nse_session_open", lambda: False)
        kv.set_raises = RuntimeError("x")
        assert df.set_bulk_quote_cache({"A": {"price": 1}})["_meta"]["count"] == 1

    @pytest.mark.parametrize("bad", [None, [], "x", 5])
    def test_set_non_dict_quotes_is_an_empty_cache_write(self, kv, monkeypatch, bad):
        # FIXED: `quotes.keys()` was evaluated before the `quotes or {}` guard, so None raised
        # AttributeError. Non-dict input is now treated as no quotes.
        monkeypatch.setattr(df, "_is_nse_session_open", lambda: False)
        payload = df.set_bulk_quote_cache(bad)
        assert payload["quotes"] == {} and payload["_meta"]["count"] == 0
        assert kv.store[df.BULK_QUOTE_CACHE_KEY]["quotes"] == {}

    def test_get_cached_quote(self, kv):
        kv.store[df.BULK_QUOTE_CACHE_KEY] = {"quotes": {"TCS": {"price": 5}, "NOPX": {"price": 0}, "BAD": "x"}}
        assert df.get_cached_quote("tcs.ns") == {"price": 5}
        assert df.get_cached_quote("NOPX") is None
        assert df.get_cached_quote("BAD") is None
        assert df.get_cached_quote("MISSING") is None

    def test_get_cached_quote_quotes_not_a_dict(self, kv):
        kv.store[df.BULK_QUOTE_CACHE_KEY] = {"quotes": ["junk"]}
        assert df.get_cached_quote("TCS") is None

    def test_cache_age(self, kv):
        assert df.bulk_cache_age_sec() is None
        kv.store[df.BULK_QUOTE_CACHE_KEY] = {"_meta": {}}
        assert df.bulk_cache_age_sec() is None
        kv.store[df.BULK_QUOTE_CACHE_KEY] = {"_meta": {"timestamp": "junk"}}
        assert df.bulk_cache_age_sec() is None
        kv.store[df.BULK_QUOTE_CACHE_KEY] = {"_meta": {"timestamp": __import__("time").time() - 30}}
        assert 29 <= df.bulk_cache_age_sec() <= 32

    def test_should_refresh(self, kv, monkeypatch):
        assert df.should_refresh_bulk_cache() is True                    # no cache
        kv.store[df.BULK_QUOTE_CACHE_KEY] = {"_meta": {"timestamp": __import__("time").time() - 200}}
        monkeypatch.setattr(df, "_is_nse_session_open", lambda: True)
        assert df.should_refresh_bulk_cache() is True                    # 200s > 120s open TTL
        assert df.should_refresh_bulk_cache(max_age_open=500) is False
        monkeypatch.setattr(df, "_is_nse_session_open", lambda: False)
        assert df.should_refresh_bulk_cache() is False                   # 200s < 6h closed TTL
        assert df.should_refresh_bulk_cache(max_age_closed=100) is True


class TestRunBulkYahooPriceFeedCached:
    def test_warm_cache_short_circuits_and_merges_into_store(self, feed, monkeypatch):
        monkeypatch.setattr(df, "should_refresh_bulk_cache", lambda *a, **k: False)
        feed.kv.store[df.BULK_QUOTE_CACHE_KEY] = {
            "_meta": {"timestamp": __import__("time").time()},
            "quotes": {"A": {"symbol": "A", "price": 5}, "B": {"symbol": "B", "price": 0}},
        }
        r = df.run_bulk_yahoo_price_feed_cached(["a", "B", "C"])
        assert r["source"] == "cache" and r["tracked_stocks"] == 2 and r["requested"] == 3
        assert r["symbols"] == ["A", "B"] and "Bulk quote cache hit" in r["message"]
        assert df.DATA_FEED_PREFIX + "A" in feed.kv.store
        assert df.DATA_FEED_PREFIX + "B" not in feed.kv.store            # no price → not merged

    def test_warm_cache_without_symbols_or_merge(self, feed, monkeypatch):
        monkeypatch.setattr(df, "should_refresh_bulk_cache", lambda *a, **k: False)
        feed.kv.store[df.BULK_QUOTE_CACHE_KEY] = {"quotes": {"A": {"price": 5}}}
        assert df.run_bulk_yahoo_price_feed_cached()["requested"] == 0
        r = df.run_bulk_yahoo_price_feed_cached(["A"], merge_existing=False)
        assert r["source"] == "cache" and df.DATA_FEED_PREFIX + "A" not in feed.kv.store

    def test_warm_cache_merge_failure_swallowed(self, feed, monkeypatch):
        monkeypatch.setattr(df, "should_refresh_bulk_cache", lambda *a, **k: False)
        feed.kv.store[df.BULK_QUOTE_CACHE_KEY] = {"quotes": {"A": {"price": 5}}}
        monkeypatch.setattr(feed.store, "put_symbol", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        assert df.run_bulk_yahoo_price_feed_cached(["A"])["source"] == "cache"

    def test_warm_but_empty_cache_falls_through_to_live_run(self, feed, monkeypatch):
        monkeypatch.setattr(df, "should_refresh_bulk_cache", lambda *a, **k: False)
        feed.bhav = bhav_rows("A")
        r = df.run_bulk_yahoo_price_feed_cached(["A"])
        assert r["source"] == "live" and r["status"] == "success"

    def test_force_runs_live_and_writes_cache_for_priced_rows(self, feed, monkeypatch):
        monkeypatch.setattr(df, "_is_nse_session_open", lambda: False)
        feed.bhav = bhav_rows("A", "B")
        feed.store.put_symbol("B", {"sector": "IT"})    # B ends up priced too via merge; make it explicit below
        r = df.run_bulk_yahoo_price_feed_cached(["A", "B"], force=True)
        assert r["source"] == "live" and r["cache_written"] is True
        cache = feed.kv.store[df.BULK_QUOTE_CACHE_KEY]
        assert set(cache["quotes"]) == {"A", "B"}
        assert cache["quotes"]["A"]["price"] == 10.0 and cache["quotes"]["A"]["source"] == "nse_bhavcopy"
        assert all(v is not None for v in cache["quotes"]["A"].values())

    def test_live_run_with_no_priced_rows_writes_no_cache(self, feed, monkeypatch):
        monkeypatch.setattr(df, "run_bulk_yahoo_price_feed",
                            lambda *a, **k: {"status": "success", "symbols": ["A"], "source": "given"})
        r = df.run_bulk_yahoo_price_feed_cached(["A"], force=True)
        assert r["cache_written"] is False and r["source"] == "given"
        assert df.BULK_QUOTE_CACHE_KEY not in feed.kv.store

    def test_stale_cache_triggers_live_run(self, feed, monkeypatch):
        monkeypatch.setattr(df, "should_refresh_bulk_cache", lambda *a, **k: True)
        monkeypatch.setattr(df, "run_bulk_yahoo_price_feed", lambda *a, **k: {"symbols": []})
        assert df.run_bulk_yahoo_price_feed_cached(["A"])["source"] == "live"


# ── price alerts ─────────────────────────────────────────────────────────────

class TestPriceAlertStore:
    def test_list_shapes(self, kv):
        assert df.list_price_alerts() == []
        kv.store[df.PRICE_ALERTS_KEY] = [{"id": "1"}]
        assert df.list_price_alerts() == [{"id": "1"}]
        kv.store[df.PRICE_ALERTS_KEY] = {"alerts": [{"id": "2"}]}
        assert df.list_price_alerts() == [{"id": "2"}]
        kv.store[df.PRICE_ALERTS_KEY] = {"alerts": "junk"}
        assert df.list_price_alerts() == []
        kv.store[df.PRICE_ALERTS_KEY] = "junk"
        assert df.list_price_alerts() == []

    def test_list_failure(self, kv):
        kv.get_raises = RuntimeError("x")
        assert df.list_price_alerts() == []

    def test_save_cleans_and_persists_without_ttl(self, kv):
        raw = [
            {"symbol": "tcs.ns", "target": "100", "direction": "BELOW", "note": "n" * 200,
             "enabled": False, "trigger_count": "3", "last_triggered_at": "L", "created_at": "C", "id": 7},
            {"symbol": "INFY", "target_price": 5, "direction": "sideways"},
            {"symbol": "ZERO", "target_price": 0}, {"symbol": "NEG", "target_price": -1},
            {"symbol": "BADP", "target_price": "abc"}, {"symbol": "", "target_price": 5},
            {"target_price": 5}, "junk", None,
        ]
        clean = df.save_price_alerts(raw)
        assert [a["symbol"] for a in clean] == ["TCS", "INFY"]
        a, b = clean
        assert a["id"] == "7" and a["target_price"] == 100.0 and a["direction"] == "below"
        assert a["enabled"] is False and len(a["note"]) == 120 and a["trigger_count"] == 3
        assert a["created_at"] == "C" and a["last_triggered_at"] == "L"
        assert b["id"] == "INFY-above-5.0" and b["direction"] == "above" and b["enabled"] is True
        assert b["note"] == "" and b["trigger_count"] == 0
        datetime.fromisoformat(b["created_at"])
        key, stored, ttl = kv.sets[-1]
        assert key == df.PRICE_ALERTS_KEY and stored == {"alerts": clean} and ttl is None

    def test_save_none_and_empty(self, kv):
        assert df.save_price_alerts(None) == []
        assert kv.store[df.PRICE_ALERTS_KEY] == {"alerts": []}

    def test_save_failure_returns_input(self, kv):
        kv.set_raises = RuntimeError("x")
        raw = [{"symbol": "A", "target_price": 1}]
        assert df.save_price_alerts(raw) is raw
        assert df.save_price_alerts(None) == []

    def test_save_bad_trigger_count_falls_to_failure_path(self, kv):
        # int("x") is not guarded inside the per-alert try, so the outer handler returns the raw list
        raw = [{"symbol": "A", "target_price": 1, "trigger_count": "x"}]
        assert df.save_price_alerts(raw) is raw
        assert df.PRICE_ALERTS_KEY not in kv.store

    def test_add_alert(self, kv):
        e = df.add_price_alert("tcs.ns", "150", direction="below", note="watch")
        assert e["symbol"] == "TCS" and e["target_price"] == 150.0 and e["direction"] == "below"
        assert e["note"] == "watch" and e["trigger_count"] == 0 and e["last_triggered_at"] is None
        assert e["id"].startswith("TCS-below-150-")
        assert len(df.list_price_alerts()) == 1
        e2 = df.add_price_alert("INFY", 5, direction="weird", note=None)
        assert e2["direction"] == "above" and e2["note"] == ""
        assert len(df.list_price_alerts()) == 2

    def test_delete_alert(self, kv):
        kv.store[df.PRICE_ALERTS_KEY] = {"alerts": [
            {"id": "1", "symbol": "A", "target_price": 1}, {"id": "2", "symbol": "B", "target_price": 2}]}
        assert df.delete_price_alert("nope") is False
        assert df.delete_price_alert(1) is True
        assert [a["id"] for a in df.list_price_alerts()] == ["2"]


def stored_alert(**kw):
    a = {"id": "a1", "symbol": "TCS", "target_price": 100.0, "direction": "above", "enabled": True,
         "note": "", "created_at": "C", "last_triggered_at": None, "trigger_count": 0}
    a.update(kw)
    return a


class TestEvaluatePriceAlerts:
    def _put(self, kv, *alerts):
        kv.store[df.PRICE_ALERTS_KEY] = {"alerts": list(alerts)}

    def test_no_alerts(self, kv):
        assert df.evaluate_price_alerts({"TCS": 1}) == []

    def test_above_and_below_directions(self, kv):
        self._put(kv, stored_alert(), stored_alert(id="b", symbol="INFY", direction="below", target_price=50))
        out = df.evaluate_price_alerts({"TCS": 100.0, "INFY": 49.0})
        assert {a["id"] for a in out} == {"a1", "b"}
        a1 = next(a for a in out if a["id"] == "a1")
        assert a1["current_price"] == 100.0 and a1["trigger_count"] == 1
        assert a1["triggered_at"] == a1["last_triggered_at"]
        saved = {a["id"]: a for a in df.list_price_alerts()}
        assert saved["a1"]["trigger_count"] == 1 and saved["a1"]["last_triggered_at"]

    def test_not_hit_disabled_and_missing_prices_skipped(self, kv):
        self._put(kv, stored_alert(), stored_alert(id="d", enabled=False),
                  stored_alert(id="z", symbol="ZZ"), stored_alert(id="p", symbol="PP"))
        out = df.evaluate_price_alerts({"TCS": 99.0, "ZZ": 0, "PP": None})
        assert out == []
        assert kv.sets == []                       # nothing changed → nothing saved

    def test_cooldown_iso_epoch_and_z_suffix(self, kv):
        now = datetime.now(df.IST)
        recent = (now - timedelta(minutes=5)).isoformat()
        recent_z = (now - timedelta(minutes=5)).astimezone(__import__("datetime").timezone.utc) \
            .replace(tzinfo=None).isoformat() + "Z"
        old = (now - timedelta(minutes=20)).isoformat()
        self._put(
            kv,
            stored_alert(id="iso", last_triggered_at=recent),
            stored_alert(id="z", last_triggered_at=recent_z),
            stored_alert(id="epoch", last_triggered_at=__import__("time").time() - 60),
            stored_alert(id="old", last_triggered_at=old),
            stored_alert(id="oldepoch", last_triggered_at=__import__("time").time() - 5000),
        )
        out = df.evaluate_price_alerts({"TCS": 200.0})
        assert {a["id"] for a in out} == {"old", "oldepoch"}

    def test_unparseable_last_trigger_does_not_block(self, kv):
        self._put(kv, stored_alert(last_triggered_at="garbage"))
        assert len(df.evaluate_price_alerts({"TCS": 200.0})) == 1

    def test_prices_from_bulk_cache_when_no_map(self, kv):
        self._put(kv, stored_alert())
        kv.store[df.BULK_QUOTE_CACHE_KEY] = {"quotes": {"tcs": {"price": 150}, "JUNK": "x", "NOPX": {"price": 0}}}
        out = df.evaluate_price_alerts()
        assert out[0]["current_price"] == 150.0

    def test_prices_from_feed_store_when_cache_empty(self, kv, monkeypatch, store):
        monkeypatch.setattr(df, "get_data_feed_store", lambda: store)
        self._put(kv, stored_alert(), stored_alert(id="b", symbol="NOPE"))
        kv.store[df.DATA_FEED_PREFIX + "TCS"] = {"sector": "x", "price": 180}
        out = df.evaluate_price_alerts({})
        assert [a["id"] for a in out] == ["a1"] and out[0]["current_price"] == 180.0

    def test_cache_that_is_not_a_dict_of_quotes(self, kv, monkeypatch, store):
        monkeypatch.setattr(df, "get_data_feed_store", lambda: store)
        monkeypatch.setattr(df, "get_bulk_quote_cache", lambda: ["junk"])
        self._put(kv, stored_alert())
        kv.store[df.DATA_FEED_PREFIX + "TCS"] = {"sector": "x", "price": 180}
        assert len(df.evaluate_price_alerts()) == 1

    def test_default_direction_and_missing_target(self, kv):
        a = stored_alert()
        a["direction"] = None
        self._put(kv, a)
        # save_price_alerts normalised direction on write, so craft the raw list directly
        kv.store[df.PRICE_ALERTS_KEY] = [{"id": "x", "symbol": "TCS", "target_price": 10, "direction": None}]
        out = df.evaluate_price_alerts({"TCS": 20.0})
        assert len(out) == 1
        kv.store[df.PRICE_ALERTS_KEY] = [{"id": "y", "symbol": "TCS"}]
        assert len(df.evaluate_price_alerts({"TCS": 20.0})) == 1      # target 0 → 20 >= 0
