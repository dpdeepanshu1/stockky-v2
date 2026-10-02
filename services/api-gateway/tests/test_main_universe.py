"""tests/test_main_universe.py — coverage for api-gateway/main.py, slice 2 (lines 801-2000)

Pass 60. The scan-universe builders:

* symbol hygiene: `_clean_equity_symbol`, `_filter_equities`;
* the individual universe sources: `_get_all_nse_securities`, `_get_nifty_indices`,
  `_get_recent_ipos`, `_get_momentum_movers` (+ the rotating `_next_general_pool_slice`
  cursor), `_get_news_mentioned_symbols`, `_get_event_symbols`, `_get_bulk_deal_symbols`,
  `_get_52w_extreme_symbols`;
* the price gate: `_row_price_over_cap`, `_filter_symbols_under_max_price`;
* the assembler and its caches: `_build_scan_universe`, `_get_all_known_symbols`,
  `_resolve_symbol`.

No network, no real KV / DB: `_fetch_from_nse_api`, `httpx.get`, `feedparser.parse`, the
scanner routes, `data_feed`, `price_resolver`, `symbol_aliases` and the KV helpers are replaced
with small fakes. `random.shuffle` is made deterministic where order matters, and tests never
depend on the iteration order of a `set` (string hashing is randomised per process).

Findings that are still open are pinned as current behaviour and marked ``NOT FIXED``; the seven low-severity ones
found in this slice (IPO equity filter, pool-change key, flat-mover pChange, news substring matches, price-cap
parsing, non-dict price rows, unguarded known-symbol sources) are fixed in main.py and their tests pin the fixed
behaviour.

Run from services/api-gateway:
    python3 -m pytest tests/test_main_universe.py -v
"""
from __future__ import annotations

import datetime as _dtmod
import os
import sys
import types
from datetime import datetime as _RealDatetime

import pytest

_IMPORT_TIME_ENV = ("USE_REDIS", "DISABLE_REDIS", "DISABLE_UPSTASH",
                    "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN")
_saved_env = {k: os.environ.pop(k) for k in _IMPORT_TIME_ENV if k in os.environ}
try:
    import main as gw
finally:
    os.environ.update(_saved_env)


# ── shared fakes ─────────────────────────────────────────────────────────────

class RecLogger:
    """Records formatted messages per level so tests can assert on log-only branches."""

    def __init__(self):
        self.msgs = {"debug": [], "info": [], "warning": [], "error": []}

    def _rec(self, level, msg, args):
        try:
            self.msgs[level].append(msg % args if args else msg)
        except Exception:
            self.msgs[level].append(str(msg))

    def debug(self, msg, *a, **k):
        self._rec("debug", msg, a)

    def info(self, msg, *a, **k):
        self._rec("info", msg, a)

    def warning(self, msg, *a, **k):
        self._rec("warning", msg, a)

    def error(self, msg, *a, **k):
        self._rec("error", msg, a)

    def any(self, level, fragment):
        return any(fragment in m for m in self.msgs[level])


class FakeAliases:
    """Stand-in for symbol_aliases (the module is imported lazily inside the helpers)."""

    def __init__(self):
        self.delisted = set()
        self.resolve_map = {}          # sym -> resolved (None = drop)
        self.high_price = set()
        self.resolve_raises = None
        self.high_raises = None

    def module(self):
        me = self
        m = types.ModuleType("symbol_aliases")

        def is_known_delisted(s):
            return s in me.delisted

        def resolve_base_symbol(s):
            if me.resolve_raises is not None:
                raise me.resolve_raises
            return me.resolve_map.get(s, s)

        def is_known_high_price(s):
            if me.high_raises is not None:
                raise me.high_raises
            return s in me.high_price

        m.is_known_delisted, m.resolve_base_symbol = is_known_delisted, resolve_base_symbol
        m.is_known_high_price = is_known_high_price
        m.resolve_ns_ticker = lambda s: s
        return m


@pytest.fixture(autouse=True)
def aliases(monkeypatch):
    fa = FakeAliases()
    monkeypatch.setitem(sys.modules, "symbol_aliases", fa.module())
    return fa


@pytest.fixture
def log(monkeypatch):
    rec = RecLogger()
    monkeypatch.setattr(gw, "logger", rec)
    return rec


class Resp:
    def __init__(self, status=200, payload=None, json_raises=None, raise_status=None):
        self.status_code, self._payload = status, payload
        self._json_raises, self._raise_status = json_raises, raise_status

    def json(self):
        if self._json_raises is not None:
            raise self._json_raises
        return self._payload

    def raise_for_status(self):
        if self._raise_status is not None:
            raise self._raise_status


def S(n, prefix="S"):
    return [f"{prefix}{i:03d}" for i in range(n)]


# ── _clean_equity_symbol / _filter_equities ──────────────────────────────────

@pytest.mark.parametrize("raw", [None, "", "   ", "-", " - ", 0, "NIFTY 50", "BANK NIFTY", "A B"])
def test_clean_rejects_empty_dash_and_spaced_names(raw):
    assert gw._clean_equity_symbol(raw) is None


@pytest.mark.parametrize("raw", ["NIFTY50", "banknifty", " sensex ", "INDIAVIX", "NIFTYIT"])
def test_clean_rejects_index_pseudo_tokens(raw):
    assert gw._clean_equity_symbol(raw) is None


@pytest.mark.parametrize("raw", ["APLAPOLLO29SEP26FUT", "BANKNIFTY29SEP2648000CE", "nifty29sep2612.5pe"])
def test_clean_rejects_derivative_contracts(raw):
    assert gw._clean_equity_symbol(raw) is None


def test_clean_local_rename_table_wins_before_symbol_aliases(aliases):
    aliases.resolve_map["MOTHERSUMI"] = "SHOULD_NOT_BE_USED"
    assert gw._clean_equity_symbol("mothersumi") == "MOTHERSON"
    assert gw._clean_equity_symbol("SRTRANSFIN") == "SHRIRAMFIN"
    assert gw._clean_equity_symbol("IBULHSGFIN") is None          # None in the table means "drop"
    assert gw._clean_equity_symbol("MCDOWELL-N") is None


def test_clean_known_delisted_is_dropped(aliases):
    aliases.delisted.add("DEADCO")
    assert gw._clean_equity_symbol("deadco") is None


def test_clean_uses_symbol_aliases_resolution(aliases):
    aliases.resolve_map["OLDNAME"] = "NEWNAME"
    aliases.resolve_map["GONE"] = None
    assert gw._clean_equity_symbol(" oldname ") == "NEWNAME"
    assert gw._clean_equity_symbol("GONE") is None
    assert gw._clean_equity_symbol("RELIANCE") == "RELIANCE"
    assert gw._clean_equity_symbol("M&M") == "M&M"
    assert gw._clean_equity_symbol("BAJAJ-AUTO") == "BAJAJ-AUTO"


def test_clean_coerces_non_strings():
    assert gw._clean_equity_symbol(532540) == "532540"


def test_clean_falls_back_to_the_symbol_when_symbol_aliases_errors(aliases, monkeypatch):
    aliases.resolve_raises = RuntimeError("kv down")
    assert gw._clean_equity_symbol("infy") == "INFY"
    monkeypatch.setitem(sys.modules, "symbol_aliases", None)     # import itself fails
    assert gw._clean_equity_symbol("tcs") == "TCS"


def test_filter_equities_cleans_dedupes_and_keeps_order(aliases):
    aliases.resolve_map["OLD"] = "NEW"
    raw = ["tcs", "NIFTY 50", "TCS", "OLD", "NEW", None, "", "-", "infy", "BANKNIFTY"]
    assert gw._filter_equities(raw) == ["TCS", "NEW", "INFY"]


def test_filter_equities_accepts_none_and_empty():
    assert gw._filter_equities(None) == []
    assert gw._filter_equities([]) == []


# ── _get_all_nse_securities ──────────────────────────────────────────────────

@pytest.fixture
def nse_api(monkeypatch):
    env = types.SimpleNamespace(responses={}, calls=[], raises={})

    def fetch(endpoint, cache_key, ttl=21600):
        env.calls.append((endpoint, cache_key, ttl))
        if endpoint in env.raises:
            raise env.raises[endpoint]
        return env.responses.get(endpoint)

    monkeypatch.setattr(gw, "_fetch_from_nse_api", fetch)
    return env


@pytest.fixture
def http(monkeypatch):
    env = types.SimpleNamespace(calls=[], responses={}, raises={}, default=None)

    def get(url, timeout=None, **k):
        env.calls.append((url, timeout))
        for frag, exc in env.raises.items():
            if frag in url:
                raise exc
        for frag, resp in env.responses.items():
            if frag in url:
                return resp
        if env.default is not None:
            return env.default
        raise RuntimeError("unexpected http.get " + url)

    monkeypatch.setattr(gw.httpx, "get", get)
    return env


SECURITIES_EP = "equity-stock-indices?index=SECURITIES%20IN%20NSE"


def test_securities_from_nse_live_api(nse_api, http):
    nse_api.responses[SECURITIES_EP] = {"data": [
        {"symbol": "reliance"}, {"symbol": "TCS"}, {"symbol": ""}, {"nosym": 1}, "junk",
        {"symbol": "NIFTY 50"}, {"symbol": "tcs"},
    ]}
    assert gw._get_all_nse_securities() == ["RELIANCE", "TCS"]
    assert nse_api.calls == [(SECURITIES_EP, "nse:all_securities", 21600)]
    assert http.calls == []                                     # live API worked: no fallback


@pytest.mark.parametrize("bad", [None, {}, {"data": "x"}, {"data": []}, {"other": [1]}])
def test_securities_unusable_nse_response_uses_bhavcopy(nse_api, http, log, bad):
    nse_api.responses[SECURITIES_EP] = bad
    http.responses["/bhavcopy/universe"] = Resp(200, {"symbols": ["abc", "DEF", "NIFTY50"]})
    assert gw._get_all_nse_securities() == ["ABC", "DEF"]
    url, timeout = http.calls[0]
    assert url == f"{gw.MARKET_DATA_URL}/bhavcopy/universe" and timeout == 15.0
    assert log.any("warning", "bhavcopy universe fallback")


def test_securities_bhavcopy_symbols_are_cleaned_and_uppercased(nse_api, http):
    http.responses["/bhavcopy/universe"] = Resp(200, {"symbols": ["abc", "DEF", "NIFTY50"]})
    assert gw._get_all_nse_securities() == ["ABC", "DEF"]


@pytest.mark.parametrize("payload", [{"symbols": None}, {"symbols": []}, {}])
def test_securities_empty_bhavcopy_falls_to_static_list(nse_api, http, log, payload):
    http.responses["/bhavcopy/universe"] = Resp(200, payload)
    out = gw._get_all_nse_securities()
    assert "RELIANCE" in out and "GROWW" in out and "M&M" in out
    assert len(out) == len(set(out)) and len(out) >= 60
    assert log.any("warning", "static last-resort list")


def test_securities_bhavcopy_errors_fall_to_static_list(nse_api, http, log):
    http.raises["/bhavcopy/universe"] = RuntimeError("timeout")
    out = gw._get_all_nse_securities()
    assert "TCS" in out and log.any("error", "bhavcopy universe fallback failed")


def test_securities_bhavcopy_http_error_and_bad_json_fall_to_static_list(nse_api, http, log):
    http.responses["/bhavcopy/universe"] = Resp(503, raise_status=RuntimeError("503"))
    assert "TCS" in gw._get_all_nse_securities()
    http.responses["/bhavcopy/universe"] = Resp(200, json_raises=ValueError("bad json"))
    assert "TCS" in gw._get_all_nse_securities()


def test_securities_static_list_still_goes_through_the_delisting_filter(nse_api, http, aliases):
    http.raises["/bhavcopy/universe"] = RuntimeError("down")
    aliases.resolve_map["TATAMOTORS"] = "TMPV"
    aliases.resolve_map["ZOMATO"] = None
    out = gw._get_all_nse_securities()
    assert "TMPV" in out and "TATAMOTORS" not in out and "ZOMATO" not in out


# ── _get_nifty_indices ───────────────────────────────────────────────────────

def test_nifty_indices_queries_five_indices_and_merges_fallback(nse_api):
    ep = lambda idx: f"equity-stock-indices?index={idx}"
    nse_api.responses[ep("NIFTY%2050")] = {"data": [{"symbol": "reliance"}, {"symbol": "NIFTY 50"}]}
    nse_api.responses[ep("NIFTY%20NEXT%2050")] = {"data": [{"symbol": "PAYTM"}, {"symbol": "RELIANCE"}]}
    nse_api.responses[ep("NIFTY%20MIDCAP%20100")] = {"data": "not a list"}
    nse_api.responses[ep("NIFTY%20MIDCAP%20150")] = None
    nse_api.responses[ep("NIFTY%20SMALLCAP%20100")] = {"data": [{"symbol": "SMALLONE"}, "junk", {"x": 1}]}
    out = gw._get_nifty_indices()
    assert [c[0] for c in nse_api.calls] == [ep(i) for i in (
        "NIFTY%2050", "NIFTY%20NEXT%2050", "NIFTY%20MIDCAP%20100", "NIFTY%20MIDCAP%20150", "NIFTY%20SMALLCAP%20100")]
    assert [c[1] for c in nse_api.calls][0] == "nse:index_NIFTY%2050"
    assert out[:3] == ["RELIANCE", "PAYTM", "SMALLONE"]          # live rows first, in order
    assert "TCS" in out and "BAJAJ-AUTO" in out                 # static fallback appended
    assert len(out) == len(set(out))
    assert "NIFTY 50" not in out


def test_nifty_indices_static_fallback_survives_a_dead_nse(nse_api):
    out = gw._get_nifty_indices()
    assert len(out) == 50 and out[0] == "ADANIENT" and "WIPRO" in out


# ── _get_recent_ipos ─────────────────────────────────────────────────────────

class FixedDatetime(_RealDatetime):
    @classmethod
    def now(cls, tz=None):
        return _RealDatetime(2026, 9, 30, 12, 0, tzinfo=tz)


@pytest.fixture
def ipo_env(monkeypatch, nse_api):
    monkeypatch.setattr(gw, "datetime", FixedDatetime)
    env = types.SimpleNamespace(nse=nse_api, alerts=None, alerts_raises=None, alerts_calls=[])
    import ipo_scanner

    def calendar():
        env.alerts_calls.append(1)
        if env.alerts_raises is not None:
            raise env.alerts_raises
        return env.alerts

    monkeypatch.setattr(ipo_scanner, "fetch_ipoalerts_calendar", calendar)
    return env


PAST_EP = "public-past-issues?from_date=30-09-2025&to_date=30-09-2026"


def test_recent_ipos_uses_a_365_day_window_and_merges_current_issues(ipo_env):
    ipo_env.nse.responses[PAST_EP] = {"data": [
        {"symbol": "aaa"}, {"secCode": "bbb"}, {"htmSym": "ccc"}, {"nothing": 1}, "junk", {"symbol": ""},
    ]}
    ipo_env.nse.responses["ipo-current-issue"] = [{"symbol": "ddd"}, {"secCode": "eee"}, {"symbol": "AAA"}, "junk"]
    out = gw._get_recent_ipos()
    assert out == ["AAA", "BBB", "CCC", "DDD", "EEE"]            # upper-cased, de-duplicated, order kept
    assert ipo_env.nse.calls[0] == (PAST_EP, gw.IPO_CACHE_KEY, 86400)
    assert ipo_env.nse.calls[1] == ("ipo-current-issue", "nse:ipo_current", 3600)
    assert ipo_env.alerts_calls == []


def test_recent_ipos_accepts_a_bare_list_and_dict_current_issue(ipo_env):
    ipo_env.nse.responses[PAST_EP] = [{"symbol": "x1"}, {"symbol": "x2"}]
    ipo_env.nse.responses["ipo-current-issue"] = {"data": [{"symbol": "x3"}]}
    assert gw._get_recent_ipos() == ["X1", "X2", "X3"]


def test_recent_ipos_current_issue_with_odd_shapes_is_ignored(ipo_env):
    ipo_env.nse.responses[PAST_EP] = {"data": [{"symbol": "aaa"}]}
    for odd in (None, "text", {"data": "not a list"}, 5):
        ipo_env.nse.responses["ipo-current-issue"] = odd
        assert gw._get_recent_ipos() == ["AAA"]


def test_recent_ipos_current_issue_fetch_error_is_swallowed(ipo_env):
    ipo_env.nse.responses[PAST_EP] = {"data": [{"symbol": "aaa"}]}
    ipo_env.nse.raises["ipo-current-issue"] = RuntimeError("blocked")
    assert gw._get_recent_ipos() == ["AAA"]


def test_recent_ipos_past_rows_with_wrong_container_are_ignored(ipo_env):
    ipo_env.nse.responses[PAST_EP] = {"data": "nope"}
    ipo_env.alerts = [{"symbol": "ALT1"}]
    assert gw._get_recent_ipos() == ["ALT1"]


def test_recent_ipos_ipoalerts_fallback_when_nse_is_empty(ipo_env, log):
    ipo_env.alerts = [{"symbol": "ALT1"}, {"symbol": ""}, {"nosym": 1}, {"symbol": "ALT2"}]
    assert gw._get_recent_ipos() == ["ALT1", "ALT2"]
    assert ipo_env.alerts_calls == [1]
    assert log.any("info", "ipoalerts fallback returned 2 symbols")


def test_recent_ipos_ipoalerts_returning_nothing_falls_to_static_list(ipo_env, log):
    ipo_env.alerts = None
    out = gw._get_recent_ipos()
    assert "SWIGGY" in out and "JIOFIN" in out and len(out) == 19
    assert log.any("warning", "static fallback list")


def test_recent_ipos_ipoalerts_error_falls_to_static_list(ipo_env, log):
    ipo_env.alerts_raises = RuntimeError("ipoalerts down")
    out = gw._get_recent_ipos()
    assert "IREDA" in out
    assert log.any("warning", "ipoalerts fallback for recent-ipos failed")


def test_recent_ipos_symbols_are_run_through_the_equity_filter(ipo_env, monkeypatch):
    """FIXED: unlike every other universe source, `_get_recent_ipos` returned raw upper-cased symbols, so
    index / derivative / renamed names only died later in `_build_scan_universe`'s final clean pass, and
    callers using this list directly (e.g. `_get_all_known_symbols`) kept them."""
    monkeypatch.setattr(gw, "_clean_equity_symbol",
                        lambda x: None if " " in str(x) else ({"OLDNAME": "NEWNAME"}.get(str(x).upper(), str(x).upper())))
    ipo_env.nse.responses[PAST_EP] = {"data": [{"symbol": "nifty 50"}, {"symbol": "OLDNAME"},
                                               {"symbol": "newname"}, {"symbol": "KEEP"}]}
    assert gw._get_recent_ipos() == ["NEWNAME", "KEEP"]          # index dropped, rename mapped, de-duplicated


def test_recent_ipos_when_every_live_name_is_filtered_falls_through_to_the_fallbacks(ipo_env, monkeypatch):
    monkeypatch.setattr(gw, "_clean_equity_symbol", lambda x: None if str(x).startswith("X") else str(x).upper())
    ipo_env.nse.responses[PAST_EP] = {"data": [{"symbol": "XJUNK"}]}
    ipo_env.alerts = [{"symbol": "alt1"}, {"symbol": "XALT"}]       # the fallback source is filtered too
    assert gw._get_recent_ipos() == ["ALT1"]
    assert ipo_env.alerts_calls


# ── _next_general_pool_slice ─────────────────────────────────────────────────

@pytest.fixture
def cursor(monkeypatch):
    monkeypatch.setattr(gw, "_general_pool_order", [])
    monkeypatch.setattr(gw, "_general_pool_order_key", None)
    monkeypatch.setattr(gw, "_general_pool_pos", 0)
    calls = []
    monkeypatch.setattr(gw.random, "shuffle", lambda x: calls.append(list(x)))
    return calls


def test_slice_of_empty_pool_is_empty(cursor):
    assert gw._next_general_pool_slice([], 5) == []
    assert cursor == []                                          # nothing shuffled


def test_slice_walks_the_pool_in_fixed_windows_and_wraps(cursor):
    pool = S(10)
    assert gw._next_general_pool_slice(pool, 4) == pool[0:4]
    assert gw._next_general_pool_slice(pool, 4) == pool[4:8]
    assert gw._next_general_pool_slice(pool, 4) == pool[8:10] + pool[0:2]      # wraps around the end
    assert gw._next_general_pool_slice(pool, 4) == pool[2:6]
    assert len(cursor) == 1                                      # reshuffled once, not per call


def test_slice_covers_every_symbol_once_per_lap(cursor):
    pool = S(9)
    seen = []
    for _ in range(3):
        seen += gw._next_general_pool_slice(pool, 3)
    assert sorted(seen) == pool


def test_slice_bigger_than_pool_returns_whole_pool_each_time(cursor):
    pool = S(3)
    assert gw._next_general_pool_slice(pool, 150) == pool
    assert gw._next_general_pool_slice(pool, 150) == pool


def test_slice_exact_fit_wraps_position_to_zero(cursor):
    pool = S(6)
    assert gw._next_general_pool_slice(pool, 3) == pool[:3]
    assert gw._next_general_pool_slice(pool, 3) == pool[3:]
    assert gw._general_pool_pos == 0
    assert gw._next_general_pool_slice(pool, 3) == pool[:3]


def test_slice_reshuffles_and_restarts_when_pool_changes(cursor):
    a = S(10)
    gw._next_general_pool_slice(a, 4)
    b = S(10)[:-1] + ["ZZZ"]                                     # same length, different last symbol
    out = gw._next_general_pool_slice(b, 4)
    assert out == b[:4] and len(cursor) == 2
    c = S(11)                                                    # different length
    gw._next_general_pool_slice(c, 4)
    assert len(cursor) == 3


def test_slice_key_detects_swapped_interior_symbols(cursor):
    """FIXED: the pool-change key was (len, first, last), so a pool with the same size and endpoints but
    swapped interior symbols was not detected and the cursor kept serving the OLD interior symbols."""
    a = ["A0", "A1", "A2", "A3", "A4", "A5"]
    b = ["A0", "B1", "B2", "B3", "B4", "A5"]
    gw._next_general_pool_slice(a, 3)
    out = gw._next_general_pool_slice(b, 3)
    assert out == ["A0", "B1", "B2"]                                   # restarted on the NEW pool
    assert len(cursor) == 2 and cursor[1] == b


def test_slice_key_is_stable_for_an_unchanged_pool(cursor):
    a = ["A0", "A1", "A2", "A3", "A4", "A5"]
    gw._next_general_pool_slice(a, 3)
    assert gw._next_general_pool_slice(list(a), 3) == ["A3", "A4", "A5"]   # same content -> cursor continues
    assert len(cursor) == 1


# ── _get_momentum_movers ─────────────────────────────────────────────────────

GAINERS = "live-analysis-variations?index=gainers"
LOSERS = "live-analysis-variations?index=losers"
VOLG = "live-analysis-variations?index=volume-gainers"
N500 = "equity-stock-indices?index=NIFTY%20500"


class Getter:
    __name__ = "getter"                                          # the 52w error log formats fn.__name__

    def __init__(self, payload=None, raises=None):
        self.payload, self.raises, self.calls = payload, raises, 0

    def __call__(self):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.payload


@pytest.fixture
def mm(monkeypatch, nse_api, http, log, cursor):
    import data_feed
    env = types.SimpleNamespace(
        nse=nse_api, http=http, cache=None, sets=[], set_raises=None,
        gainers=Getter({"data": []}), losers=Getter({"data": []}), active=Getter({"data": []}),
        nifty50=[], nifty50_raises=None,
        indices=[], indices_calls=[0], securities=[], securities_calls=[0],
        bulk={}, bulk_raises=None, bulk_seeds=[],
    )
    monkeypatch.setattr(gw, "_redis_get", lambda k: env.cache)

    def rset(k, v, ttl=None):
        if env.set_raises is not None:
            raise env.set_raises
        env.sets.append((k, list(v), ttl))

    monkeypatch.setattr(gw, "_redis_set", rset)
    monkeypatch.setattr(gw, "market_top_gainers", env.gainers)
    monkeypatch.setattr(gw, "market_top_losers", env.losers)
    monkeypatch.setattr(gw, "market_most_active", env.active)

    def nifty50():
        if env.nifty50_raises is not None:
            raise env.nifty50_raises
        return env.nifty50

    monkeypatch.setattr(gw, "_get_nifty50_data", nifty50)
    monkeypatch.setattr(gw, "_get_nifty_indices", lambda: (env.indices_calls.__setitem__(0, env.indices_calls[0] + 1), list(env.indices))[1])
    monkeypatch.setattr(gw, "_get_all_nse_securities", lambda: (env.securities_calls.__setitem__(0, env.securities_calls[0] + 1), list(env.securities))[1])

    def bulk(seed):
        env.bulk_seeds.append(list(seed))
        if env.bulk_raises is not None:
            raise env.bulk_raises
        return env.bulk

    monkeypatch.setattr(data_feed, "bulk_yahoo_download_prices", bulk)
    http.responses["/angelone/movers"] = Resp(200, {"data": []})
    return env


def test_movers_returns_cached_list_without_recomputing(mm):
    mm.cache = ["CACHED1", "CACHED2"]
    assert gw._get_momentum_movers() == ["CACHED1", "CACHED2"]
    assert mm.nse.calls == [] and mm.http.calls == [] and mm.sets == []


@pytest.mark.parametrize("cached", [None, [], {"a": 1}, "text"])
def test_movers_ignores_empty_or_non_list_cache(mm, cached):
    mm.cache = cached
    gw._get_momentum_movers()
    assert len(mm.nse.calls) == 4


def _fill_boards(mm, n=45):
    mm.nse.responses[GAINERS] = {"data": [{"symbol": s, "pChange": 5} for s in S(n, "G")]}


def test_movers_nse_boards_are_queried_with_15_minute_ttl(mm):
    _fill_boards(mm)
    gw._get_momentum_movers()
    assert mm.nse.calls == [
        (GAINERS, "nse:gainers", 900), (LOSERS, "nse:losers", 900),
        (VOLG, "nse:vol_gainers", 900), (N500, "nse:nifty500_idx", 900)]


def test_movers_nse_move_threshold_is_two_percent_inclusive(mm):
    mm.nse.responses[GAINERS] = {"data": [
        {"symbol": "AT2", "pChange": 2.0}, {"symbol": "BELOW", "pChange": 1.99},
        {"symbol": "NEG", "pChange": -2.5}, {"symbol": "STR", "pChange": "3.5"},
        {"symbol": "ALT1", "perChange": 4}, {"symbol": "ALT2", "change_pct": -6},
        {"symbol": "NOCHG"}, {"symbol": "BADCHG", "pChange": "n/a"},
        {"symbolName": "byname", "pChange": 9},
    ]}
    out = set(gw._get_momentum_movers())
    assert {"AT2", "NEG", "STR", "ALT1", "ALT2", "NOCHG", "BADCHG", "BYNAME"} <= out
    assert "BELOW" not in out


def test_movers_zero_pchange_is_a_real_flat_value_and_excluded(mm):
    """FIXED: `item.get("pChange") or item.get("perChange") or ...` treated a numeric 0 / 0.0 like a missing
    field, so a flat stock was added as a "mover" while the string "0" was correctly excluded."""
    mm.nse.responses[GAINERS] = {"data": [
        {"symbol": "FLATNUM", "pChange": 0}, {"symbol": "FLATFLOAT", "pChange": 0.0},
        {"symbol": "FLATSTR", "pChange": "0"}, {"symbol": "FLATALT", "pChange": 0, "perChange": 9}]}
    out = set(gw._get_momentum_movers())
    assert not ({"FLATNUM", "FLATFLOAT", "FLATSTR", "FLATALT"} & out)


def test_movers_missing_or_blank_pchange_falls_through_to_the_next_field_or_is_included(mm):
    mm.nse.responses[GAINERS] = {"data": [
        {"symbol": "ALTFIELD", "pChange": None, "perChange": 5},
        {"symbol": "BLANKFIELD", "pChange": "", "change_pct": -4},
        {"symbol": "NOFIELD"}, {"symbol": "SMALL", "pChange": 0.5}]}
    out = set(gw._get_momentum_movers())
    assert {"ALTFIELD", "BLANKFIELD", "NOFIELD"} <= out and "SMALL" not in out


def test_movers_nse_row_shapes(mm, log):
    mm.nse.responses[GAINERS] = {"NIFTY": [{"symbol": "VIANIFTY", "pChange": 5}]}          # NIFTY key
    mm.nse.responses[LOSERS] = {"data": {"data": [{"symbol": "NESTED", "pChange": -5}]}}   # nested dict
    mm.nse.responses[VOLG] = {"data": {"other": 1}}                                         # dict, no rows
    mm.nse.responses[N500] = {"data": [
        "junk", {"symbol": "NIFTY 50", "pChange": 9}, {"symbol": "-", "pChange": 9},
        {"symbol": "", "pChange": 9}, {"symbol": "OK1", "pChange": 3}]}
    out = set(gw._get_momentum_movers())
    assert {"VIANIFTY", "NESTED", "OK1"} <= out
    assert not ({"NIFTY 50", "-", ""} & out)


def test_movers_nse_none_and_non_dict_boards_are_tolerated(mm, log):
    mm.nse.responses[GAINERS] = None
    mm.nse.responses[LOSERS] = ["a", "list"]
    mm.nse.responses[VOLG] = {"data": 5}
    gw._get_momentum_movers()
    assert log.any("warning", f"NSE movers {GAINERS}: no data")


def test_movers_nse_fetch_error_does_not_stop_other_boards(mm, log):
    mm.nse.raises[GAINERS] = RuntimeError("blocked")
    mm.nse.responses[LOSERS] = {"data": [{"symbol": "LOSER1", "pChange": -4}]}
    assert "LOSER1" in gw._get_momentum_movers()
    assert log.any("debug", f"NSE movers {GAINERS}")


def test_movers_zero_nse_contribution_is_flagged_as_likely_blocked(mm, log):
    gw._get_momentum_movers()
    assert log.any("warning", "contributed 0 symbols this cycle")
    assert log.any("info", "step1 (NSE live boards, full-market): 0 symbols")


def test_movers_healthy_nse_does_not_warn(mm, log):
    _fill_boards(mm)
    gw._get_momentum_movers()
    assert not log.any("warning", "contributed 0 symbols")
    assert log.any("info", "step1 (NSE live boards, full-market): 45 symbols")


def test_movers_angelone_sweep(mm, log):
    _fill_boards(mm)
    mm.http.responses["/angelone/movers"] = Resp(200, {
        "data": [{"symbol": "angel1"}, "junk", {"symbol": "NIFTY 50"}, {"symbol": ""}, {"nosym": 1}, {"symbol": "ANGEL2"}],
        "status": "ok", "universe_size": 2000, "quotes_fetched": 1900})
    out = set(gw._get_momentum_movers())
    assert {"ANGEL1", "ANGEL2"} <= out and "NIFTY 50" not in out
    assert mm.http.calls == [(f"{gw.MARKET_DATA_URL}/angelone/movers", 25.0)]
    assert log.any("info", "AngelOne movers: +2 symbols")


def test_movers_angelone_non_200_and_errors_are_tolerated(mm, log):
    _fill_boards(mm)
    mm.http.responses["/angelone/movers"] = Resp(503, {"data": [{"symbol": "NOPE"}]})
    assert "NOPE" not in gw._get_momentum_movers()
    assert log.any("debug", "AngelOne movers: HTTP 503")
    del mm.http.responses["/angelone/movers"]
    mm.http.raises["/angelone/movers"] = RuntimeError("down")
    gw._get_momentum_movers()
    assert log.any("debug", "AngelOne movers fetch failed")
    mm.http.raises.clear()
    mm.http.responses["/angelone/movers"] = Resp(200, {"data": None})
    gw._get_momentum_movers()


def test_movers_gateway_boards_are_cleaned_and_isolated(mm):
    _fill_boards(mm)
    mm.gainers.payload = {"data": [{"symbol": "gw1"}, {"ticker": "gw2"}, "junk", {"symbol": "NIFTY 50"}, {}]}
    mm.losers.raises = RuntimeError("route exploded")
    mm.active.payload = {"data": [{"symbol": "gw3"}]}
    out = set(gw._get_momentum_movers())
    assert {"GW1", "GW2", "GW3"} <= out and "NIFTY 50" not in out
    assert mm.gainers.calls == mm.losers.calls == mm.active.calls == 1


def test_movers_gateway_outer_guard_is_only_reachable_via_a_missing_route(mm, monkeypatch, log):
    """The outer `except` around the gateway-boards loop cannot fire with real data (the inner
    try/except swallows every per-board error, and building the tuple of route functions cannot
    fail). It is exercised here by deleting one route global so the tuple build raises NameError."""
    mm.nse.responses[GAINERS] = {"data": [{"symbol": "LIVE1", "pChange": 5}]}
    monkeypatch.delattr(gw, "market_top_losers")
    assert gw._get_momentum_movers() == ["LIVE1"]
    assert log.any("debug", "gateway movers")


def test_movers_gateway_board_payload_none_is_tolerated(mm):
    _fill_boards(mm)
    mm.gainers.payload = None
    mm.losers.payload = {"data": None}
    gw._get_momentum_movers()


def test_movers_nifty50_leg_uses_three_percent_threshold(mm):
    _fill_boards(mm)
    mm.nifty50 = [
        {"symbol": "n_hi", "change_pct": 3.0}, {"symbol": "n_lo", "change_pct": 2.99},
        {"symbol": "n_neg", "pChange": -4}, {"symbol": "n_none"}, {"symbol": "n_bad", "change_pct": "abc"},
        {"symbol": "NIFTY 50", "change_pct": 9}, "junk", {"change_pct": 9},
        {"symbol": "n_str", "change_pct": "5.5"},
    ]
    out = set(gw._get_momentum_movers())
    assert {"N_HI", "N_NEG", "N_BAD", "N_STR"} <= out
    assert "N_LO" not in out and "N_NONE" not in out and "NIFTY 50" not in out


def test_movers_nifty50_errors_and_none_are_tolerated(mm):
    _fill_boards(mm)
    mm.nifty50_raises = RuntimeError("yfinance down")
    gw._get_momentum_movers()
    mm.nifty50_raises = None
    mm.nifty50 = None
    gw._get_momentum_movers()


def test_movers_enough_live_symbols_skips_the_yfinance_seed_scan(mm):
    _fill_boards(mm, n=40)                                       # exactly 40 -> no fallback
    out = gw._get_momentum_movers()
    assert len(out) == 40
    assert mm.indices_calls == [0] and mm.securities_calls == [0] and mm.bulk_seeds == []


def test_movers_thin_live_data_runs_the_bulk_seed_scan(mm, log):
    _fill_boards(mm, n=39)                                       # 39 < 40 -> fallback
    mm.indices = S(100, "IDX")
    mm.securities = S(30, "GEN") + S(5, "IDX")                   # IDX names are excluded from the general pool
    mm.bulk = {
        "IDX000": {"day_change_pct": 5.0}, "IDX001": {"day_change_pct": -7.5},
        "IDX002": {"day_change_pct": 4.99}, "IDX003": {"day_change_pct": None},
        "IDX004": {"day_change_pct": "abc"}, "IDX005": "not-a-dict", "IDX006": {},
        "GEN000": {"day_change_pct": "6"},
    }
    out = set(gw._get_momentum_movers())
    (seed,) = mm.bulk_seeds
    assert seed[:20] == mm.indices[:20]                          # large-cap slice first
    assert set(seed[20:100]) == set(mm.indices[20:100])          # then up to 80 mid/small names
    assert set(seed[100:]) == set(S(30, "GEN"))                  # then the rotating general slice
    assert len(seed) == 130 and len(seed) == len(set(seed))
    assert {"IDX000", "IDX001", "GEN000"} <= out
    assert not ({"IDX002", "IDX003", "IDX004", "IDX005", "IDX006"} & out)
    assert log.any("info", "momentum_movers fallback seed: 20 largecap + 80 mid/smallcap-index + 30 general")


def test_movers_seed_is_capped_at_180_and_mid_slice_at_80(mm):
    mm.indices = S(300, "IDX")
    mm.securities = S(400, "GEN")
    gw._get_momentum_movers()
    (seed,) = mm.bulk_seeds
    assert len(seed) == 180
    assert seed[:20] == mm.indices[:20]
    assert len([s for s in seed if s.startswith("IDX")]) == 100
    assert len([s for s in seed if s.startswith("GEN")]) == 80    # truncated by the 180 cap


def test_movers_general_pool_slice_is_bounded_by_the_sample_size(mm):
    mm.indices = S(10, "IDX")
    mm.securities = S(500, "GEN")
    gw._get_momentum_movers()
    (seed,) = mm.bulk_seeds
    assert len(seed) == 160
    assert gw._GENERAL_POOL_SAMPLE_SIZE == 150


def test_movers_bulk_fetch_failure_still_returns_live_movers(mm, log):
    mm.nse.responses[GAINERS] = {"data": [{"symbol": "LIVE1", "pChange": 5}]}
    mm.indices = S(30, "IDX")
    mm.bulk_raises = RuntimeError("bulk quotes down")
    assert gw._get_momentum_movers() == ["LIVE1"]
    assert log.any("warning", "bulk quote fetch failed")


def test_movers_result_is_sorted_and_cached_for_90_seconds(mm, log):
    mm.nse.responses[GAINERS] = {"data": [{"symbol": "ZED", "pChange": 5}, {"symbol": "ABC", "pChange": 5}]}
    out = gw._get_momentum_movers()
    assert out == ["ABC", "ZED"]
    assert mm.sets == [(gw.MOMENTUM_MOVERS_CACHE_KEY, ["ABC", "ZED"], 90)]
    assert log.any("info", "Momentum movers collected: 2 symbols")


def test_movers_cache_write_failure_is_non_fatal(mm, log):
    mm.nse.responses[GAINERS] = {"data": [{"symbol": "ABC", "pChange": 5}]}
    mm.set_raises = RuntimeError("kv down")
    assert gw._get_momentum_movers() == ["ABC"]
    assert log.any("debug", "momentum movers cache set failed")


# ── _get_news_mentioned_symbols ──────────────────────────────────────────────

class Entry:
    def __init__(self, title="", summary=None):
        self.title = title
        if summary is not None:
            self.summary = summary


@pytest.fixture
def news(monkeypatch, log):
    env = types.SimpleNamespace(urls=[], feeds={}, feed_raises={}, securities=[], indices=[],
                                sec_raises=None, default_entries=[])

    def parse(url):
        env.urls.append(url)
        for frag, exc in env.feed_raises.items():
            if frag in url:
                raise exc
        for frag, entries in env.feeds.items():
            if frag in url:
                return types.SimpleNamespace(entries=entries)
        return types.SimpleNamespace(entries=env.default_entries)

    monkeypatch.setattr(gw.feedparser, "parse", parse)

    def secs():
        if env.sec_raises is not None:
            raise env.sec_raises
        return list(env.securities)

    monkeypatch.setattr(gw, "_get_all_nse_securities", secs)
    monkeypatch.setattr(gw, "_get_nifty_indices", lambda: list(env.indices))
    return env


def test_news_queries_five_google_news_feeds(news):
    gw._get_news_mentioned_symbols()
    assert len(news.urls) == 5
    assert all(u.startswith("https://news.google.com/rss/search?q=") and "hl=en-IN&gl=IN&ceid=IN:en" in u
               for u in news.urls)
    assert "NSE+bulk+deal+OR+block+deal" in news.urls[1]


def test_news_matches_whole_words_at_start_middle_and_end(news):
    news.securities = ["TCS", "INFY", "WIPRO", "HCLTECH"]
    news.default_entries = []
    news.feeds["results+earnings"] = [
        Entry("TCS beats estimates", "quarterly numbers"),               # at the start
        Entry("Analysts like", "the stock INFY"),                        # inside/end via summary join
    ]
    news.feeds["order+win"] = [Entry("Strong order book for wipro today")]
    out = gw._get_news_mentioned_symbols()
    assert set(out) == {"TCS", "INFY", "WIPRO"}


def test_news_symbols_of_every_length_need_a_word_boundary(news):
    news.securities = ["TCS", "ITC", "IDEA", "TITAN"]
    news.feeds["results+earnings"] = [Entry("ITCHY TCSX IDEAS TITANIUM stocks")]
    out = set(gw._get_news_mentioned_symbols())
    assert not ({"TCS", "ITC", "IDEA", "TITAN"} & out)        # substrings of longer words never match


def test_news_four_plus_char_symbols_no_longer_match_inside_ordinary_words(news):
    """FIXED: any symbol of 4+ characters matched as a bare substring, so "IDEA" (Vodafone Idea) matched the
    ordinary word "ideas" and "TITAN" matched "titanium"."""
    news.securities = ["IDEA", "TITAN"]
    news.feeds["results+earnings"] = [Entry("Five ideas for the week ahead, titanium prices rise")]
    assert gw._get_news_mentioned_symbols() == []


def test_news_whole_word_mentions_still_match_including_next_to_punctuation(news):
    news.securities = ["IDEA", "TITAN", "ITC", "M&M", "BAJAJ-AUTO"]
    news.feeds["results+earnings"] = [Entry("Vodafone IDEA, TITAN: up. (ITC) and M&M; BAJAJ-AUTO!")]
    assert set(gw._get_news_mentioned_symbols()) == {"IDEA", "TITAN", "ITC", "M&M", "BAJAJ-AUTO"}


def test_news_symbol_glued_to_ampersand_or_digits_is_not_a_match(news):
    news.securities = ["TCS", "SAIL"]
    news.feeds["results+earnings"] = [Entry("TCS2 AT&TCS SAIL9")]
    assert gw._get_news_mentioned_symbols() == []


def test_news_single_character_symbols_are_ignored(news):
    news.securities = ["A", "TCS"]
    news.feeds["results+earnings"] = [Entry("A TCS story")]
    assert gw._get_news_mentioned_symbols() == ["TCS"]


def test_news_uses_at_most_twenty_entries_per_feed(news):
    news.securities = ["EARLY", "LATE"]
    entries = [Entry("filler") for _ in range(20)] + [Entry("LATE mention")]
    entries[0] = Entry("EARLY mention")
    news.feeds["results+earnings"] = entries
    out = gw._get_news_mentioned_symbols()
    assert out == ["EARLY"]


def test_news_entries_without_title_or_summary_are_tolerated(news):
    news.securities = ["TCS"]
    news.feeds["results+earnings"] = [types.SimpleNamespace(), Entry(None, None), Entry("TCS up", None)]
    assert gw._get_news_mentioned_symbols() == ["TCS"]


def test_news_one_bad_feed_does_not_stop_the_others(news):
    news.securities = ["TCS"]
    news.feed_raises["results+earnings"] = RuntimeError("feed down")
    news.feeds["order+win"] = [Entry("TCS wins order")]
    assert gw._get_news_mentioned_symbols() == ["TCS"]


def test_news_feed_with_no_entries_attribute_value(news, monkeypatch):
    news.securities = ["TCS"]
    monkeypatch.setattr(gw.feedparser, "parse", lambda url: types.SimpleNamespace(entries=None))
    assert gw._get_news_mentioned_symbols() == []


def test_news_candidates_include_nifty_indices_and_are_deduped(news):
    news.securities = ["TCS"]
    news.indices = ["TCS", "INFY"]
    news.feeds["results+earnings"] = [Entry("TCS and INFY rally")]
    out = gw._get_news_mentioned_symbols()
    assert sorted(out) == ["INFY", "TCS"]


def test_news_candidate_slice_is_at_least_400_deep(news, monkeypatch):
    news.securities = S(450, "Q")
    news.feeds["results+earnings"] = [Entry("Q399 and Q400 and Q449 in the news")]
    monkeypatch.setattr(gw, "SCAN_UNIVERSE_TARGET", 100)          # floor of 400 applies
    out = set(gw._get_news_mentioned_symbols())
    assert "Q399" in out                                          # index 399 is inside the [:400] slice
    assert "Q400" not in out and "Q449" not in out
    monkeypatch.setattr(gw, "SCAN_UNIVERSE_TARGET", 430)
    out = set(gw._get_news_mentioned_symbols())
    assert "Q400" in out and "Q449" not in out                    # slice grows with the universe target


def test_news_result_is_capped_at_120(news):
    news.securities = S(200, "SYM")
    news.feeds["results+earnings"] = [Entry(" ".join(news.securities))]
    assert len(gw._get_news_mentioned_symbols()) == 120


def test_news_symbol_lookup_failure_is_swallowed(news, log):
    news.sec_raises = RuntimeError("nse down")
    assert gw._get_news_mentioned_symbols() == []
    assert log.any("warning", "Could not parse news for symbols")


# ── _get_event_symbols ───────────────────────────────────────────────────────

def test_event_symbols_from_dict_and_list_payloads(http):
    http.responses["/symbols_with_events"] = Resp(200, {"symbols": ["abc", "DEF", "abc", "NIFTY 50", None, ""]})
    assert gw._get_event_symbols() == ["ABC", "DEF"]
    assert http.calls == [(f"{gw.EVENT_URL}/symbols_with_events", 12)]
    http.responses["/symbols_with_events"] = Resp(200, ["xyz", "uvw"])
    assert gw._get_event_symbols() == ["XYZ", "UVW"]


@pytest.mark.parametrize("resp", [
    Resp(500, {"symbols": ["A1"]}), Resp(200, {"symbols": None}), Resp(200, {}), Resp(200, "text"), Resp(200, 5),
])
def test_event_symbols_unusable_responses_give_empty(http, resp):
    http.responses["/symbols_with_events"] = resp
    assert gw._get_event_symbols() == []


def test_event_symbols_http_and_json_errors_are_swallowed(http, log):
    http.raises["/symbols_with_events"] = RuntimeError("down")
    assert gw._get_event_symbols() == []
    assert log.any("warning", "Could not fetch event symbols")
    del http.raises["/symbols_with_events"]
    http.responses["/symbols_with_events"] = Resp(200, json_raises=ValueError("bad"))
    assert gw._get_event_symbols() == []


def test_event_symbols_capped_at_80(http):
    http.responses["/symbols_with_events"] = Resp(200, {"symbols": S(120, "EV")})
    out = gw._get_event_symbols()
    assert len(out) == 80 and out[0] == "EV000"


# ── _get_bulk_deal_symbols ───────────────────────────────────────────────────

FO_EP = "equity-stockIndices?index=SECURITIES%20IN%20F%26O"


def test_bulk_deals_queries_three_endpoints_with_30_minute_ttl(nse_api):
    gw._get_bulk_deal_symbols()
    assert nse_api.calls == [
        (FO_EP, "nse:fo_bulk_seed", 1800),
        ("historical/bulk-deals", "nse:bulk_deals", 1800),
        ("historical/block-deals", "nse:block_deals", 1800)]


def test_bulk_deals_row_shapes_and_keys(nse_api):
    nse_api.responses[FO_EP] = {"data": [{"symbol": "fo1"}, {"symbolName": "fo2"}, {"scm": "fo3"}, "fo4", 5, {}]}
    nse_api.responses["historical/bulk-deals"] = {"bulkDeals": [{"symbol": "bd1"}, {"symbol": "FO1"}]}
    nse_api.responses["historical/block-deals"] = [{"symbol": "bk1"}, {"symbol": "x"}, {"symbol": "NIFTY 50"}]
    assert gw._get_bulk_deal_symbols() == ["FO1", "FO2", "FO3", "FO4", "BD1", "BK1"]


def test_bulk_deals_blockdeals_key_and_odd_payloads(nse_api):
    nse_api.responses[FO_EP] = {"blockDeals": [{"symbol": "bl1"}]}
    nse_api.responses["historical/bulk-deals"] = {"data": "not a list"}
    nse_api.responses["historical/block-deals"] = 5
    assert gw._get_bulk_deal_symbols() == ["BL1"]


def test_bulk_deals_reads_at_most_80_rows_per_endpoint_and_returns_at_most_100(nse_api):
    nse_api.responses[FO_EP] = {"data": [{"symbol": s} for s in S(200, "F")]}
    nse_api.responses["historical/bulk-deals"] = {"data": [{"symbol": s} for s in S(200, "B")]}
    out = gw._get_bulk_deal_symbols()
    assert len(out) == 100
    assert out[:80] == S(80, "F") and out[80:] == S(20, "B")


def test_bulk_deals_endpoint_error_does_not_stop_the_rest(nse_api, log):
    nse_api.raises[FO_EP] = RuntimeError("blocked")
    nse_api.responses["historical/block-deals"] = [{"symbol": "ok1"}]
    assert gw._get_bulk_deal_symbols() == ["OK1"]
    assert log.any("debug", "bulk deals equity-stockIndices")


# ── _get_52w_extreme_symbols ─────────────────────────────────────────────────

@pytest.fixture
def w52(monkeypatch, nse_api, log):
    env = types.SimpleNamespace(nse=nse_api, gainers=Getter({"data": []}), active=Getter({"data": []}))
    monkeypatch.setattr(gw, "market_top_gainers", env.gainers)
    monkeypatch.setattr(gw, "market_most_active", env.active)
    return env


def test_52w_gateway_boards_use_three_percent_threshold(w52):
    w52.gainers.payload = {"data": [
        {"symbol": "g_hi", "change_pct": 3.0}, {"symbol": "g_lo", "change_pct": 2.9},
        {"Symbol": "g_cap", "pChange": -5}, {"ticker": "g_tick", "pctChange": "4"},
        {"symbol": "g_none"}, {"symbol": "g_bad", "change_pct": "abc"}, "junk", {"symbol": "NIFTY 50", "pChange": 9},
    ]}
    w52.active.payload = {"data": [{"symbol": "ma1", "pChange": 8}, {"symbol": "G_HI", "pChange": 8}]}
    out = gw._get_52w_extreme_symbols()
    assert out == ["G_HI", "G_CAP", "G_TICK", "G_NONE", "G_BAD", "MA1"]
    assert w52.gainers.calls == 1 and w52.active.calls == 1


def test_52w_gateway_board_error_is_isolated_and_none_payload_ok(w52, log):
    w52.gainers.raises = RuntimeError("route down")
    w52.active.payload = None
    assert gw._get_52w_extreme_symbols() == []
    assert log.any("debug", "52w/movers")


def test_52w_nse_boards_filter_and_cap_rows(w52):
    w52.nse.responses["live-analysis-variations?index=gainers"] = {"data": (
        [{"symbol": "n_hi", "pChange": 3}, {"symbol": "n_lo", "pChange": 2.99}, {"symbolName": "n_alt", "perChange": -4},
         {"symbol": "n_none"}, {"symbol": "n_bad", "pChange": "zzz"}, "junk"] + [{"symbol": s, "pChange": 5} for s in S(60, "P")])}
    w52.nse.responses["liveEquity-market?index=gainers"] = {"data": [{"symbol": "live1", "pChange": 6}]}
    out = gw._get_52w_extreme_symbols()
    assert "N_HI" in out and "N_ALT" in out and "N_NONE" in out and "N_BAD" in out and "LIVE1" in out
    assert "N_LO" not in out
    assert "P033" in out and "P034" not in out                    # only the first 40 rows are read
    assert w52.nse.calls == [
        ("live-analysis-variations?index=gainers", "nse:gainers52", 900),
        ("liveEquity-market?index=gainers", "nse:live_gainers", 900)]


def test_52w_nse_odd_payloads_and_errors_are_tolerated(w52):
    w52.nse.responses["live-analysis-variations?index=gainers"] = ["a", "list"]
    w52.nse.raises["liveEquity-market?index=gainers"] = RuntimeError("blocked")
    assert gw._get_52w_extreme_symbols() == []
    w52.nse.raises.clear()
    w52.nse.responses["liveEquity-market?index=gainers"] = {"data": None}
    assert gw._get_52w_extreme_symbols() == []


def test_52w_result_is_capped_at_80(w52):
    w52.gainers.payload = {"data": [{"symbol": s, "pChange": 9} for s in S(100, "H")]}
    assert len(gw._get_52w_extreme_symbols()) == 80


# ── _row_price_over_cap ──────────────────────────────────────────────────────

@pytest.fixture
def cap(monkeypatch):
    monkeypatch.setattr(gw, "MAX_UNIVERSE_PRICE", 5000.0)


def test_price_cap_disabled_when_max_is_zero(monkeypatch, aliases):
    monkeypatch.setattr(gw, "MAX_UNIVERSE_PRICE", 0.0)
    aliases.high_price.add("BOSCHLTD")
    assert gw._row_price_over_cap({"symbol": "BOSCHLTD", "price": 99999}) is False


def test_price_cap_known_expensive_symbol_is_blocked_even_without_a_price(cap, aliases):
    aliases.high_price.add("BOSCHLTD")
    assert gw._row_price_over_cap({"symbol": "BOSCHLTD"}) is True
    assert gw._row_price_over_cap({}, symbol="BOSCHLTD") is True
    assert gw._row_price_over_cap({"symbol": "OTHER"}, symbol="BOSCHLTD") is True     # explicit symbol wins


def test_price_cap_denylist_lookup_errors_fall_through_to_the_price(cap, aliases, monkeypatch):
    aliases.high_raises = RuntimeError("kv down")
    assert gw._row_price_over_cap({"symbol": "X", "price": 6000}) is True
    monkeypatch.setitem(sys.modules, "symbol_aliases", None)
    assert gw._row_price_over_cap({"symbol": "X", "price": 100}) is False


@pytest.mark.parametrize("row,expected", [
    ({"price": 5000.01}, True), ({"price": 5000}, False), ({"price": 4999.99}, False),
    ({"price": "6,000.50"}, True), ({"price": " 4 999 "}, False),
    ({"close": 7000}, True), ({"cmp": 7000}, True), ({"ltp": 7000}, True),
    ({"last_price": 7000}, True), ({"current_price": 7000}, True),
    ({"metrics": {"price": 8000}}, True), ({"metrics": {"ltp": 10}}, False),
    ({"price": ""}, False), ({"price": None}, False), ({}, False),
    ({"price": "-"}, False), ({"price": "N/A"}, False), ({"price": "none"}, False),
])
def test_price_cap_reads_the_first_usable_price(cap, row, expected):
    assert gw._row_price_over_cap(row) is expected


def test_price_cap_skips_non_positive_prices_and_uses_the_next_key(cap):
    assert gw._row_price_over_cap({"price": 0, "close": 9000}) is True
    assert gw._row_price_over_cap({"price": -5, "close": 9000}) is True
    assert gw._row_price_over_cap({"price": 0, "close": 0}) is False


def test_price_cap_first_positive_price_decides(cap):
    assert gw._row_price_over_cap({"price": 100, "close": 9000}) is False


def test_price_cap_row_metrics_must_be_a_dict(cap):
    assert gw._row_price_over_cap({"metrics": "text", "price": None}) is False


def test_price_cap_unparseable_value_skips_that_key_and_reads_the_next(cap):
    """FIXED: the try/except sat OUTSIDE the key loop, so one unparseable value ("abc") ended the scan and
    a valid, over-cap `close` listed after it was never consulted — the row was let through."""
    assert gw._row_price_over_cap({"price": "abc", "close": 9000}) is True
    assert gw._row_price_over_cap({"price": [1], "close": 9000}) is True
    assert gw._row_price_over_cap({"price": "abc", "close": 100}) is False
    assert gw._row_price_over_cap({"price": "abc", "close": "xyz"}) is False      # nothing parseable -> allowed


def test_price_cap_non_dict_row_is_allowed_through(cap):
    """FIXED: `isinstance(row, dict)` was only checked for the symbol lookup; the price loop then called
    `row.get` unguarded, so a non-dict row raised AttributeError."""
    assert gw._row_price_over_cap(None) is False
    assert gw._row_price_over_cap("junk") is False


# ── _filter_symbols_under_max_price ──────────────────────────────────────────

@pytest.fixture
def pf(monkeypatch):
    import data_feed
    import price_resolver
    env = types.SimpleNamespace(feeds={}, feed_raises=None, feed_calls=[], prices={}, resolver_raises=set())
    monkeypatch.setattr(gw, "MAX_UNIVERSE_PRICE", 5000.0)

    def feeds(symbols):
        env.feed_calls.append(list(symbols))
        if env.feed_raises is not None:
            raise env.feed_raises
        return env.feeds

    def resolve(sym, row, feed_item):
        if sym in env.resolver_raises:
            raise RuntimeError("resolver blew up")
        return env.prices.get(sym)

    monkeypatch.setattr(data_feed, "get_all_stock_feeds", feeds)
    monkeypatch.setattr(price_resolver, "resolve_display_price", resolve)
    return env


@pytest.mark.parametrize("empty", [None, [], ["", None, "  ", ".NS"]])
def test_price_filter_empty_inputs(pf, empty):
    assert gw._filter_symbols_under_max_price(empty) == []
    assert pf.feed_calls == []


def test_price_filter_normalises_and_dedupes_before_loading_feeds(pf):
    out = gw._filter_symbols_under_max_price(["tcs.ns", " TCS ", "infy.bo", None, "INFY"])
    assert out == ["TCS", "INFY"] and pf.feed_calls == [["TCS", "INFY"]]


def test_price_filter_drops_over_cap_keeps_at_cap_and_unknown(pf):
    pf.prices = {"CHEAP": 100.0, "ATCAP": 5000.0, "PRICEY": 5000.5, "ZERO": 0, "NEG": -3}
    out = gw._filter_symbols_under_max_price(["CHEAP", "ATCAP", "PRICEY", "ZERO", "NEG", "UNKNOWN"])
    assert out == ["CHEAP", "ATCAP", "ZERO", "NEG", "UNKNOWN"]


def test_price_filter_passes_the_feed_row_to_the_resolver(pf, monkeypatch):
    import price_resolver
    seen = []
    pf.feeds = {"AAA": {"price": 7}}
    monkeypatch.setattr(price_resolver, "resolve_display_price", lambda s, row, feed: seen.append((s, row, feed)) or 1)
    gw._filter_symbols_under_max_price(["AAA", "BBB"])
    assert seen == [("AAA", {}, {"price": 7}), ("BBB", {}, {})]


def test_price_filter_known_high_price_names_are_dropped_only_when_a_cap_is_set(pf, aliases, monkeypatch):
    aliases.high_price.add("BOSCHLTD")
    assert gw._filter_symbols_under_max_price(["BOSCHLTD", "TCS"]) == ["TCS"]
    monkeypatch.setattr(gw, "MAX_UNIVERSE_PRICE", 0.0)
    assert gw._filter_symbols_under_max_price(["BOSCHLTD", "TCS"]) == ["BOSCHLTD", "TCS"]


def test_price_filter_cap_zero_keeps_everything(pf, monkeypatch):
    monkeypatch.setattr(gw, "MAX_UNIVERSE_PRICE", 0.0)
    pf.prices = {"PRICEY": 90000.0}
    assert gw._filter_symbols_under_max_price(["PRICEY"]) == ["PRICEY"]


def test_price_filter_resolver_error_treats_price_as_unknown(pf):
    pf.resolver_raises = {"BAD"}
    pf.prices = {"BAD": 90000.0}
    assert gw._filter_symbols_under_max_price(["BAD"]) == ["BAD"]


def test_price_filter_feed_load_failure_keeps_symbols(pf, log):
    pf.feed_raises = RuntimeError("neon down")
    assert gw._filter_symbols_under_max_price(["PRICEY", "TCS"]) == ["PRICEY", "TCS"]   # price unknown -> kept
    assert log.any("debug", "universe price filter feed load")
    pf.prices = {"PRICEY": 9000.0}                               # the resolver can still price it without a feed row
    assert gw._filter_symbols_under_max_price(["PRICEY", "TCS"]) == ["TCS"]


def test_price_filter_feeds_none_is_treated_as_empty(pf):
    pf.feeds = None
    assert gw._filter_symbols_under_max_price(["TCS"]) == ["TCS"]


def test_price_filter_without_price_resolver_reads_feed_fields(pf, monkeypatch):
    monkeypatch.setitem(sys.modules, "price_resolver", None)
    pf.feeds = {
        "P1": {"price": 9000}, "P2": {"close": 9000}, "P3": {"cmp": 9000}, "P4": {"ltp": 9000},
        "P5": {"last_price": 9000}, "P6": {"prev_close": 9000},
        "OK1": {"price": 100}, "OK2": {"price": "n/a", "close": 50}, "OK3": {"price": None},
        "OK4": {"price": 0, "close": 0}, "Z1": {"price": "abc", "close": 9000}, "OK5": {},
    }
    syms = list(pf.feeds)
    out = gw._filter_symbols_under_max_price(syms)
    # Unlike `_row_price_over_cap`, the try/except here is per key: "abc" is skipped and the
    # over-cap `close` after it is still read, so Z1 is dropped.
    assert out == ["OK1", "OK2", "OK3", "OK4", "OK5"]


def test_price_filter_without_known_high_price_helper_keeps_going(pf, monkeypatch):
    fa = FakeAliases()
    mod = fa.module()
    del mod.is_known_high_price
    monkeypatch.setitem(sys.modules, "symbol_aliases", mod)
    pf.prices = {"PRICEY": 9000.0, "TCS": 100.0}
    assert gw._filter_symbols_under_max_price(["PRICEY", "TCS"]) == ["TCS"]


def test_price_filter_logs_the_drop_count_only_when_something_was_dropped(pf, log):
    pf.prices = {"PRICEY": 9000.0}
    gw._filter_symbols_under_max_price(["TCS"])
    assert not log.any("info", "Universe")
    gw._filter_symbols_under_max_price(["TCS", "PRICEY"])
    assert log.any("info", "kept=1 dropped=1")


# ── _build_scan_universe ─────────────────────────────────────────────────────

class FakeKv:
    def __init__(self):
        self.stale, self.raises = None, None
        self.keys = []

    def get_stale(self, key):
        self.keys.append(key)
        if self.raises is not None:
            raise self.raises
        return self.stale


class TimeFreezer:
    """Freeze `datetime.datetime.now` (the function re-imports datetime locally)."""

    def __init__(self, monkeypatch):
        self.mp = monkeypatch

    def at(self, year, month, day, hour, minute):
        class Fake(_RealDatetime):
            @classmethod
            def now(cls, tz=None):
                return _RealDatetime(year, month, day, hour, minute, tzinfo=tz)

        self.mp.setattr(_dtmod, "datetime", Fake)

    def broken(self):
        class Fake(_RealDatetime):
            @classmethod
            def now(cls, tz=None):
                raise RuntimeError("clock broke")

        self.mp.setattr(_dtmod, "datetime", Fake)


@pytest.fixture
def ub(monkeypatch, log):
    env = types.SimpleNamespace(
        cache={}, sets=[], set_raises={}, kv=FakeKv(),
        securities=[], indices=[], movers=[], bulk=[], w52=[], news=[], ipos=[], events=[],
        watchlist=[], searched=[], pruned=set(), raises={}, calls=[],
        price_filter_calls=[],
    )
    monkeypatch.setattr(gw, "_redis_get", lambda k: env.cache.get(k))

    def rset(k, v, ttl=None):
        if k in env.set_raises:
            raise env.set_raises[k]
        env.sets.append((k, list(v), ttl))

    monkeypatch.setattr(gw, "_redis_set", rset)
    monkeypatch.setattr(gw, "_kv_cache", env.kv)

    def src(name, attr):
        def fn():
            env.calls.append(name)
            if name in env.raises:
                raise env.raises[name]
            return list(getattr(env, attr))
        return fn

    for name, attr in (("_get_all_nse_securities", "securities"), ("_get_nifty_indices", "indices"),
                       ("_get_momentum_movers", "movers"), ("_get_bulk_deal_symbols", "bulk"),
                       ("_get_52w_extreme_symbols", "w52"), ("_get_news_mentioned_symbols", "news"),
                       ("_get_recent_ipos", "ipos"), ("_get_event_symbols", "events"),
                       ("_load_watchlist", "watchlist"), ("_load_searched", "searched")):
        monkeypatch.setattr(gw, name, src(name, attr))
    monkeypatch.setattr(gw, "_is_symbol_pruned", lambda s: s in env.pruned)

    def price_filter(symbols):
        env.price_filter_calls.append(list(symbols))
        return list(symbols)

    monkeypatch.setattr(gw, "_filter_symbols_under_max_price", price_filter)
    monkeypatch.setattr(gw.random, "shuffle", lambda x: None)
    monkeypatch.setattr(gw, "SCAN_UNIVERSE_TARGET", 500)
    monkeypatch.setattr(gw, "SCAN_UNIVERSE_HARD_CAP", 5000)
    monkeypatch.setattr(gw, "SYMBOL_ALIASES", {"OLDA": "NEWA", "OLDB": ["NEWB1", "NEWB2"]})
    env.time = TimeFreezer(monkeypatch)
    env.time.at(2026, 9, 30, 10, 0)                              # Wednesday, market open
    return env


def _sets(env, key):
    return [(v, t) for k, v, t in env.sets if k == key]


def test_build_returns_the_cached_universe_after_filtering(ub):
    ub.cache[gw.SCAN_UNIVERSE_KEY] = ["tcs", "NIFTY 50", "infy", "TCS"]
    assert gw._build_scan_universe() == ["TCS", "INFY"]
    assert ub.price_filter_calls == [["TCS", "INFY"]]
    assert ub.calls == [] and ub.sets == []


@pytest.mark.parametrize("cached", [None, [], "text", {"a": 1}])
def test_build_ignores_empty_or_non_list_cache(ub, cached):
    ub.cache[gw.SCAN_UNIVERSE_KEY] = cached
    ub.kv.stale = None
    ub.securities = ["AAA"]
    assert "AAA" in gw._build_scan_universe()


def test_build_serves_a_stale_copy_of_50_or_more_and_rewarms_the_live_key(ub, log):
    ub.kv.stale = ["nifty 50"] + S(60)
    out = gw._build_scan_universe()
    assert out == S(60)
    assert ub.kv.keys == [gw.SCAN_UNIVERSE_STALE_KEY]
    assert _sets(ub, gw.SCAN_UNIVERSE_KEY) == [(S(60), 300)]
    assert ub.calls == []                                        # no rebuild on the request path
    assert log.any("info", "serving 60-symbol stale copy")


@pytest.mark.parametrize("stale", [S(49), [], None, "text", {"a": 1}])
def test_build_rejects_a_stub_sized_or_invalid_stale_copy(ub, stale):
    ub.kv.stale = stale
    ub.securities = ["LIVE1"]
    out = gw._build_scan_universe()
    assert "LIVE1" in out
    assert ub.calls                                              # fell through to a full rebuild


def test_build_stale_read_failure_is_non_fatal(ub, log):
    ub.kv.raises = RuntimeError("neon cold")
    ub.securities = ["LIVE1"]
    assert "LIVE1" in gw._build_scan_universe()
    assert log.any("debug", "stale-read failed")


def test_build_without_kv_cache_skips_the_stale_lookup(ub, monkeypatch):
    monkeypatch.setattr(gw, "_kv_cache", None)
    ub.securities = ["LIVE1"]
    assert "LIVE1" in gw._build_scan_universe()
    assert ub.kv.keys == []


def test_build_stale_copy_is_filtered_by_price_before_the_size_check(ub, monkeypatch):
    ub.kv.stale = S(70)
    monkeypatch.setattr(gw, "_filter_symbols_under_max_price", lambda s: s[:40])     # price gate drops 30
    ub.securities = ["LIVE1"]
    out = gw._build_scan_universe()
    assert "LIVE1" in out                                        # 40 < 50 -> stale copy rejected


def test_build_samples_the_securities_list_up_to_the_target(ub, monkeypatch):
    monkeypatch.setattr(gw, "SCAN_UNIVERSE_TARGET", 250)
    ub.securities = S(300)
    ub.bulk = ["DYN1"]                                           # dynamic names sort to the front
    out = gw._build_scan_universe()
    assert len(out) == 250 and out[0] == "DYN1"
    # shuffle is a no-op here, so the base sample is exactly the first 250 securities
    assert set(out) - {"DYN1", "NEWA", "NEWB1", "NEWB2"} <= set(S(250))


def test_build_uses_indices_when_the_securities_list_is_empty(ub):
    ub.indices = ["IDX1", "IDX2"]
    assert {"IDX1", "IDX2"} <= set(gw._build_scan_universe())


def test_build_securities_failure_is_logged_and_indices_still_used(ub, monkeypatch, log):
    ub.indices = ["IDX1", "IDX2"]
    n = {"i": 0}

    def flaky():                                                 # fails on the base fetch only
        n["i"] += 1
        if n["i"] == 1:
            raise RuntimeError("nse + bhavcopy down")
        return []

    monkeypatch.setattr(gw, "_get_all_nse_securities", flaky)
    assert {"IDX1", "IDX2"} <= set(gw._build_scan_universe())
    assert log.any("warning", "Failed to fetch securities")


def test_build_pad_step_securities_lookup_is_guarded(ub):
    """FIXED: the padding step's securities lookup is now guarded, so a persistently failing source
    returns the thin universe instead of raising."""
    ub.indices = ["IDX1", "IDX2"]
    ub.raises["_get_all_nse_securities"] = RuntimeError("nse + bhavcopy down")
    out = gw._build_scan_universe()
    assert isinstance(out, list) and "IDX1" in out


def test_build_indices_failure_is_logged_not_raised(ub, log):
    ub.raises["_get_nifty_indices"] = RuntimeError("index feed down")
    ub.securities = ["AAA"]
    out = gw._build_scan_universe()
    assert "AAA" in out
    assert log.any("warning", "Failed to fetch indices")


@pytest.mark.parametrize("source,label", [
    ("_get_news_mentioned_symbols", "news symbols"),
    ("_get_recent_ipos", "IPOs"),
    ("_get_event_symbols", "event symbols"),
])
def test_build_survives_a_failing_source(ub, log, source, label):
    ub.securities = ["AAA"]
    ub.raises[source] = RuntimeError("source down")
    assert "AAA" in gw._build_scan_universe()
    assert log.any("warning", f"Failed to fetch {label}")


def test_build_movers_group_failure_skips_the_rest_of_that_group_only(ub, log):
    ub.securities = ["AAA"]
    ub.movers = ["MOV1"]
    ub.bulk = ["BULK1"]
    ub.raises["_get_momentum_movers"] = RuntimeError("movers down")
    ub.w52 = ["W52ONE"]
    out = set(gw._build_scan_universe())
    assert "AAA" in out and "MOV1" not in out
    assert "BULK1" not in out and "W52ONE" not in out          # same try-block: later calls never ran
    assert log.any("warning", "Failed to fetch momentum movers")


def test_build_merges_every_source_plus_watchlist_searched_and_alias_targets(ub):
    ub.securities, ub.indices = ["SEC1"], ["IDX1"]
    ub.movers, ub.bulk, ub.w52 = ["MOV1"], ["BULK1"], ["W52"]
    ub.news, ub.ipos, ub.events = ["NEWS1"], ["IPO1"], ["EVT1"]
    ub.watchlist, ub.searched = ["WATCH1"], ["SRCH1"]
    out = set(gw._build_scan_universe())
    assert {"SEC1", "IDX1", "MOV1", "BULK1", "W52", "NEWS1", "IPO1", "EVT1", "WATCH1", "SRCH1",
            "NEWA", "NEWB1", "NEWB2"} <= out


def test_build_final_clean_pass_drops_delisted_and_dedupes_renames(ub, aliases, log):
    aliases.resolve_map.update({"OLDNAME": "NEWNAME", "NEWNAME": "NEWNAME", "GONE": None})
    ub.securities = ["OLDNAME", "NEWNAME", "GONE", "NIFTY 50", "  ", "TCS"] + S(250, "F")   # >= pad floor: no padding
    out = gw._build_scan_universe()
    assert out.count("NEWNAME") == 1 and "OLDNAME" not in out
    assert "GONE" not in out and "NIFTY 50" not in out and "TCS" in out
    assert log.any("info", "dropped 2 delisted/merged/invalid symbols")     # GONE and NIFTY 50; blank not counted


def test_build_falls_back_to_ten_large_caps_when_everything_is_dropped(ub, monkeypatch):
    monkeypatch.setattr(gw, "_clean_equity_symbol", lambda s: None)
    out = gw._build_scan_universe()
    assert out[:10] == ["HDFCBANK", "ICICIBANK", "INFY", "HCLTECH", "ITC", "SBIN",
                        "BHARTIARTL", "KOTAKBANK", "AXISBANK", "WIPRO"]


def test_build_prunes_unproductive_symbols_except_watchlist_names(ub, log):
    ub.securities = ["KEEP", "PRUNED1", "WATCHED"] + S(250, "F")   # >= pad floor: no padding
    ub.watchlist = ["WATCHED"]
    ub.pruned = {"PRUNED1", "WATCHED"}
    out = set(gw._build_scan_universe())
    assert "KEEP" in out and "WATCHED" in out and "PRUNED1" not in out
    assert log.any("info", "Universe pruning: excluded")


def test_build_dynamic_sources_come_first_in_priority_order(ub):
    ub.securities = ["PLAIN1", "PLAIN2"]
    ub.bulk = ["BULK1", "shared.ns"]
    ub.w52 = ["W52", "SHARED"]
    ub.movers = ["MOV1", "MOV2"]
    ub.news = ["NEWS1"]
    ub.events = ["EVT1"]
    ub.ipos = ["IPO1", "BULK1"]
    out = gw._build_scan_universe()
    # bulk -> 52w -> movers -> news -> events -> ipos; duplicates keep their first position
    assert out[:8] == ["BULK1", "SHARED", "W52", "MOV1", "MOV2", "NEWS1", "EVT1", "IPO1"]


def test_build_dynamic_priority_ignores_symbols_pruned_out_of_the_universe(ub):
    ub.securities = ["PLAIN1"]
    ub.bulk = ["GONE1", "BULK1"]
    ub.pruned = {"GONE1"}
    out = gw._build_scan_universe()
    assert "GONE1" not in out and out[0] == "BULK1"


def test_build_dynamic_priority_failure_is_non_fatal(ub, log, monkeypatch):
    ub.securities = ["PLAIN1"]
    calls = {"n": 0}

    def flaky_bulk():
        calls["n"] += 1
        if calls["n"] == 2:                                     # first call is the merge step; second is priority
            raise RuntimeError("bulk down on 2nd call")
        return ["BULK1"]

    monkeypatch.setattr(gw, "_get_bulk_deal_symbols", flaky_bulk)
    out = gw._build_scan_universe()
    assert "PLAIN1" in out and "BULK1" in out
    assert log.any("warning", "dynamic priority merge failed")


def _securities_on_second_call(monkeypatch, symbols):
    """First `_get_all_nse_securities()` call (base fetch) is empty; later calls (the pad step) work."""
    n = {"i": 0}

    def fn():
        n["i"] += 1
        return [] if n["i"] == 1 else list(symbols)

    monkeypatch.setattr(gw, "_get_all_nse_securities", fn)
    return n


def test_build_pads_a_thin_universe_from_the_full_securities_list(ub, monkeypatch):
    _securities_on_second_call(monkeypatch, S(300, "SEC"))
    ub.indices = ["IDX1"]
    out = gw._build_scan_universe()
    assert len(out) == 220 and len(set(out)) == 220              # floor 200 + 20; IDX1 + 3 alias targets kept
    assert {"IDX1", "NEWA", "NEWB1", "NEWB2"} <= set(out)


def test_build_pad_floor_has_a_minimum_of_200_for_a_small_target(ub, monkeypatch):
    monkeypatch.setattr(gw, "SCAN_UNIVERSE_TARGET", 250)          # 0.4 * 250 = 100, so the 200 minimum wins
    _securities_on_second_call(monkeypatch, S(300, "SEC"))
    assert len(gw._build_scan_universe()) == 220


def test_build_does_not_pad_when_already_at_the_floor(ub, monkeypatch):
    monkeypatch.setattr(gw, "SCAN_UNIVERSE_TARGET", 600)
    ub.securities = S(400, "SEC")
    ub.indices = []
    out = gw._build_scan_universe()
    assert len(out) >= 400
    assert ub.calls.count("_get_all_nse_securities") == 1        # only the initial fetch


def test_build_pad_floor_scales_with_a_large_target(ub, monkeypatch):
    monkeypatch.setattr(gw, "SCAN_UNIVERSE_TARGET", 1000)
    _securities_on_second_call(monkeypatch, S(700, "SEC"))
    out = gw._build_scan_universe()
    assert len(out) == 420                                       # floor max(200, 0.4 * 1000) = 400, +20


def test_build_result_is_cut_to_the_target_and_the_hard_cap(ub, monkeypatch):
    monkeypatch.setattr(gw, "SCAN_UNIVERSE_TARGET", 10)
    ub.securities = S(300, "SEC")
    ub.bulk = ["BULK1"]
    out = gw._build_scan_universe()
    assert len(out) == 10 and out[0] == "BULK1"
    monkeypatch.setattr(gw, "SCAN_UNIVERSE_HARD_CAP", 5)
    out = gw._build_scan_universe()
    assert len(out) == 5


@pytest.mark.parametrize("when,ttl", [
    ((2026, 9, 30, 10, 0), 1800),        # Wednesday, open
    ((2026, 9, 30, 9, 15), 1800),        # opening minute (inclusive)
    ((2026, 9, 30, 15, 30), 1800),       # closing minute (inclusive)
    ((2026, 9, 30, 9, 14), 21600),       # a minute before the open
    ((2026, 9, 30, 15, 31), 21600),      # a minute after the close
    ((2026, 10, 3, 10, 0), 21600),       # Saturday
    ((2026, 10, 4, 10, 0), 21600),       # Sunday
    ((2026, 10, 2, 12, 0), 1800),        # Friday
])
def test_build_cache_ttl_follows_the_ist_market_window(ub, when, ttl):
    ub.time.at(*when)
    ub.securities = ["AAA"]
    gw._build_scan_universe()
    assert _sets(ub, gw.SCAN_UNIVERSE_KEY)[0][1] == ttl


def test_build_cache_ttl_falls_back_to_one_hour_when_the_clock_fails(ub):
    ub.time.broken()
    ub.securities = ["AAA"]
    gw._build_scan_universe()
    assert _sets(ub, gw.SCAN_UNIVERSE_KEY)[0][1] == 3600


def test_build_writes_live_and_stale_keys_with_the_price_filtered_result(ub, monkeypatch):
    monkeypatch.setattr(gw, "_filter_symbols_under_max_price", lambda s: [x for x in s if x != "PRICEY"])
    ub.securities = ["PRICEY", "CHEAP"]
    out = gw._build_scan_universe()
    assert "PRICEY" not in out and "CHEAP" in out
    (live_val, live_ttl), = _sets(ub, gw.SCAN_UNIVERSE_KEY)
    (stale_val, stale_ttl), = _sets(ub, gw.SCAN_UNIVERSE_STALE_KEY)
    assert live_val == out == stale_val
    assert live_ttl == 1800 and stale_ttl == 14400


def test_build_stale_key_write_failure_is_non_fatal(ub, log):
    ub.set_raises[gw.SCAN_UNIVERSE_STALE_KEY] = RuntimeError("neon down")
    ub.securities = ["AAA"]
    assert "AAA" in gw._build_scan_universe()
    assert log.any("debug", "stale-key write failed")
    assert _sets(ub, gw.SCAN_UNIVERSE_KEY)


def test_build_logs_the_summary(ub, log, monkeypatch):
    monkeypatch.setattr(gw, "MAX_UNIVERSE_PRICE", 5000.0)
    ub.securities = ["AAA"]
    ub.bulk = ["AAA"]
    gw._build_scan_universe()
    assert log.any("info", "Scan universe built:") and log.any("info", "dynamic=1")


# ── _get_all_known_symbols ───────────────────────────────────────────────────

@pytest.fixture
def known(monkeypatch, ub):
    return ub


def test_known_symbols_returns_the_cached_set(known):
    known.cache[gw.KNOWN_SYMBOLS_KEY] = ["AAA", "BBB", "AAA"]
    assert gw._get_all_known_symbols() == {"AAA", "BBB"}
    assert known.calls == [] and known.sets == []


@pytest.mark.parametrize("cached", [None, [], "text", {"a": 1}])
def test_known_symbols_rebuilds_on_empty_or_non_list_cache(known, cached):
    known.cache[gw.KNOWN_SYMBOLS_KEY] = cached
    known.indices = ["IDX1"]
    assert "IDX1" in gw._get_all_known_symbols()


def test_known_symbols_merges_all_sources_and_normalises(known):
    known.securities = ["SEC1", "sec2.ns"]
    known.indices = ["IDX1"]
    known.watchlist, known.searched = ["WATCH1"], ["srch1.bo"]
    known.ipos, known.movers = ["IPO1"], ["MOV1"]
    known.cache[gw.SCAN_UNIVERSE_KEY] = ["UNI1", "", "uni2.NS"]
    out = gw._get_all_known_symbols()
    assert out == {"SEC1", "SEC2", "IDX1", "WATCH1", "SRCH1", "IPO1", "MOV1", "UNI1", "UNI2",
                   "NEWA", "NEWB1", "NEWB2"}
    (val, ttl), = _sets(known, gw.KNOWN_SYMBOLS_KEY)
    assert set(val) == out and ttl == 21600


def test_known_symbols_scan_universe_must_be_a_list(known):
    known.cache[gw.SCAN_UNIVERSE_KEY] = "AAA BBB"
    out = gw._get_all_known_symbols()
    assert "AAA BBB" not in out


def test_known_symbols_securities_failure_is_tolerated(known, log):
    known.raises["_get_all_nse_securities"] = RuntimeError("nse down")
    known.indices = ["IDX1"]
    assert "IDX1" in gw._get_all_known_symbols()
    assert log.any("debug", "known-symbols: _get_all_nse_securities lookup failed")


def test_known_symbols_securities_slice_is_at_least_300_deep(known, monkeypatch):
    known.securities = S(400, "Q")
    monkeypatch.setattr(gw, "SCAN_UNIVERSE_TARGET", 100)
    out = gw._get_all_known_symbols()
    assert "Q299" in out and "Q300" not in out
    known.cache.clear()
    monkeypatch.setattr(gw, "SCAN_UNIVERSE_TARGET", 350)
    out = gw._get_all_known_symbols()
    assert "Q349" in out and "Q350" not in out


@pytest.mark.parametrize("source", ["_get_nifty_indices", "_load_watchlist", "_load_searched",
                                    "_get_recent_ipos", "_get_momentum_movers"])
def test_known_symbols_every_source_is_guarded(known, source):
    """FIXED: a failure in any source other than `_get_all_nse_securities` propagated out of
    `_get_all_known_symbols`, so `_resolve_symbol` (and every route that calls it) raised instead of
    degrading."""
    known.securities = ["SEC1"]
    known.raises[source] = RuntimeError("source down")
    out = gw._get_all_known_symbols()
    assert "SEC1" in out


# ── _resolve_symbol ──────────────────────────────────────────────────────────

@pytest.fixture
def resolve(monkeypatch):
    env = types.SimpleNamespace(known={"RELIANCE", "TCS", "INFY", "HDFCBANK"}, calls=0)

    def known():
        env.calls += 1
        return env.known

    monkeypatch.setattr(gw, "_get_all_known_symbols", known)
    monkeypatch.setattr(gw, "SYMBOL_ALIASES", {"TATAMOTORS": "TMPV", "MULTI": ["FIRST", "SECOND"]})
    return env


@pytest.mark.parametrize("empty", [None, ""])
def test_resolve_empty_input(resolve, empty):
    assert gw._resolve_symbol(empty) is None
    assert resolve.calls == 0


def test_resolve_alias_takes_precedence_without_loading_known_symbols(resolve):
    assert gw._resolve_symbol("tatamotors.ns") == "TMPV"
    assert gw._resolve_symbol("MULTI") == "FIRST"                 # list alias -> first entry
    assert resolve.calls == 0


def test_resolve_exact_known_symbol_is_returned_normalised(resolve):
    assert gw._resolve_symbol("reliance.bo") == "RELIANCE"


def test_resolve_fuzzy_match_uses_a_0_7_cutoff(resolve):
    assert gw._resolve_symbol("RELIANC") == "RELIANCE"
    assert gw._resolve_symbol("infi") == "INFY"
    assert gw._resolve_symbol("ZZZZZZ") is None


def test_resolve_returns_the_closest_of_several_candidates(resolve):
    resolve.known = {"HDFCBANK", "HDFCLIFE", "HDFC"}
    assert gw._resolve_symbol("HDFCBAN") == "HDFCBANK"


def test_resolve_with_no_known_symbols_returns_none(resolve):
    resolve.known = set()
    assert gw._resolve_symbol("ANYTHING") is None
