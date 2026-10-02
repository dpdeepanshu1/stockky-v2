"""tests/test_ipo_scanner.py — coverage for api-gateway/ipo_scanner.py

Recent-IPO discovery (ipoalerts + NSE + manual entries), per-symbol scoring (`analyze_ipo`),
the background scan job, the repair batch, the feed audit / purge helpers and the cached list
reader.

No network, no real KV, no real database: `httpx`, `kv_cache`, `ipo_schema`, `sqlalchemy`,
`rate_limiter`, `surprise_scanner`, `symbol_aliases` and `yfinance` are replaced by small fakes
(the module imports most of them lazily, inside functions). Every test starts from clean
module globals and no test sleeps (`time.sleep` is stubbed).

Run from services/api-gateway:
    python3 -m pytest tests/test_ipo_scanner.py -v
"""
from __future__ import annotations

import copy
import sys
import types
from datetime import datetime, timedelta

import pytest

import ipo_scanner as ipo


# ── fakes ─────────────────────────────────────────────────────────────────────

class FakeKV:
    """In-memory stand-in for kv_cache. Returns deep copies, like a real round trip."""

    def __init__(self):
        self.store = {}
        self.sets = []              # (key, value, ttl)
        self.get_raises = None
        self.set_raises = None
        self.get_raises_for = {}    # key -> exception
        self.set_raises_for = {}    # key -> exception

    def module(self):
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

        m.kv_get, m.kv_set = kv_get, kv_set
        return m


class FakeResp:
    def __init__(self, status=200, payload=None, text="", json_raises=None):
        self.status_code = status
        self._payload = payload
        self.text = text
        self._json_raises = json_raises

    def json(self):
        if self._json_raises is not None:
            raise self._json_raises
        return self._payload


class FakeCookies:
    def __init__(self, names):
        self._names = list(names)

    def keys(self):
        return list(self._names)


class FakeClient:
    """Stand-in for httpx.Client used by _nse_session / fetch_nse_ipo_calendar."""

    instances = []

    def __init__(self, *a, **kw):
        self.init_kwargs = kw
        self.gets = []
        self.cookie_names = FakeClient.next_cookies
        self.cookies = FakeCookies(self.cookie_names)
        self.closed = False
        self.close_raises = False
        self.routes = {}       # url substring -> FakeResp | Exception | callable
        self.default_status = 200
        FakeClient.instances.append(self)

    next_cookies = ("nsit", "nseappid")
    bootstrap_raises = None

    def get(self, url, **kw):
        self.gets.append((url, kw))
        if FakeClient.bootstrap_raises is not None and kw.get("headers") is ipo.NSE_BOOTSTRAP_HEADERS:
            raise FakeClient.bootstrap_raises
        for frag, val in self.routes.items():
            if frag in url:
                if isinstance(val, Exception):
                    raise val
                if callable(val):
                    return val()
                return val
        return FakeResp(self.default_status, [])

    def close(self):
        self.closed = True
        if self.close_raises:
            raise RuntimeError("close boom")


class FakeHttpx:
    """Stand-in for the `httpx` module as seen by ipo_scanner."""

    def __init__(self):
        self.calls = []            # (url, params, kwargs)
        self.routes = []           # (url_substring, handler)
        self.Client = FakeClient

    def route(self, frag, handler):
        self.routes.append((frag, handler))

    def get(self, url, params=None, **kw):
        self.calls.append((url, params, kw))
        for frag, handler in self.routes:
            if frag in url:
                if isinstance(handler, Exception):
                    raise handler
                if callable(handler):
                    return handler(url, params, kw)
                return handler
        raise RuntimeError(f"unrouted httpx.get {url}")


@pytest.fixture
def kv(monkeypatch):
    fake = FakeKV()
    monkeypatch.setitem(sys.modules, "kv_cache", fake.module())
    return fake


@pytest.fixture
def hx(monkeypatch):
    fake = FakeHttpx()
    monkeypatch.setattr(ipo, "httpx", fake)
    FakeClient.instances.clear()
    FakeClient.next_cookies = ("nsit", "nseappid")
    FakeClient.bootstrap_raises = None
    return fake


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    ipo._LOCAL_JOB.clear()
    ipo._LOCAL_JOB.update({"status": "idle", "message": "Idle", "processed": 0, "total": 0})
    ipo._IPO_STOP_FLAG.clear()
    ipo._NSE_SESSION_CACHE["client"] = None
    ipo._NSE_SESSION_CACHE["ts"] = 0.0
    if ipo._IPO_SCAN_LOCK.locked():
        ipo._IPO_SCAN_LOCK.release()
    monkeypatch.setattr(ipo.time, "sleep", lambda s: None)
    # never let a test reach the real rate limiter / schema / db
    monkeypatch.setitem(sys.modules, "rate_limiter", types.SimpleNamespace(acquire=lambda *a, **k: None))
    yield
    ipo._IPO_STOP_FLAG.clear()
    if ipo._IPO_SCAN_LOCK.locked():
        ipo._IPO_SCAN_LOCK.release()


def _today():
    return datetime.now(ipo.IST)


def _iso(days_ago):
    return (_today() - timedelta(days=days_ago)).strftime("%Y-%m-%d")


# ── small pure helpers ────────────────────────────────────────────────────────

class TestNonEquity:
    def test_empty_is_false(self):
        assert ipo._is_ipo_non_equity("") is False
        assert ipo._is_ipo_non_equity(None) is False
        assert ipo._is_ipo_non_equity("   ") is False

    def test_leading_digit_is_bond(self):
        assert ipo._is_ipo_non_equity("1150VIES30") is True
        assert ipo._is_ipo_non_equity(" 925ecl28 ") is True

    def test_plain_equity_false(self):
        assert ipo._is_ipo_non_equity("TCS") is False

    def test_delegates_to_symbol_aliases(self):
        assert ipo._is_ipo_non_equity("ABCD-RE") is True
        assert ipo._is_ipo_non_equity("RELIANCE29SEP26FUT") is True

    def test_symbol_aliases_failure_is_false(self, monkeypatch):
        bad = types.ModuleType("symbol_aliases")   # no is_non_equity_instrument attribute -> ImportError
        monkeypatch.setitem(sys.modules, "symbol_aliases", bad)
        assert ipo._is_ipo_non_equity("TCS") is False


class TestStopFlag:
    def test_request_and_clear(self):
        assert ipo.ipo_stop_requested() is False
        ipo.request_ipo_stop()
        assert ipo.ipo_stop_requested() is True
        ipo.clear_ipo_stop()
        assert ipo.ipo_stop_requested() is False


class TestIsoListingDate:
    @pytest.mark.parametrize("val", [None, "", "   ", 0])
    def test_falsy_is_none(self, val):
        assert ipo._iso_listing_date(val) is None

    def test_whitespace_only_string_after_str(self):
        class Blank:
            def __bool__(self):
                return True

            def __str__(self):
                return "   "
        assert ipo._iso_listing_date(Blank()) is None

    def test_iso_fast_path_truncates(self):
        assert ipo._iso_listing_date("2026-08-06T10:00:00") == "2026-08-06"
        assert ipo._iso_listing_date(" 2026-08-06 ") == "2026-08-06"

    def test_nse_format_normalised(self):
        assert ipo._iso_listing_date("06-AUG-2026") == "2026-08-06"
        assert ipo._iso_listing_date("06-08-2026") == "2026-08-06"

    def test_unparseable_is_none(self):
        assert ipo._iso_listing_date("soon") is None


class TestParseDate:
    def test_empty(self):
        assert ipo._parse_date(None) is None
        assert ipo._parse_date("") is None

    @pytest.mark.parametrize("raw", ["2026-08-06", "06-Aug-2026", "06-08-2026", "06/08/2026"])
    def test_formats(self, raw):
        assert ipo._parse_date(raw) == datetime(2026, 8, 6)

    def test_iso_fallback_and_failure(self):
        # "%Y-%m-%d" cannot parse a 10-char slice of this, fromisoformat can't either -> None
        assert ipo._parse_date("not-a-date") is None

    def test_fromisoformat_fallback(self):
        # 20260806 is not in the strptime list but datetime.fromisoformat accepts it
        assert ipo._parse_date("20260806") == datetime(2026, 8, 6)


class TestClamp:
    def test_bounds(self):
        assert ipo._clamp(-5) == 0.0
        assert ipo._clamp(150) == 100.0
        assert ipo._clamp(42.5) == 42.5
        assert ipo._clamp(5, 10, 20) == 10


class TestPriceBand:
    @pytest.mark.parametrize("raw,expected", [
        (None, None),
        (97, 97.0),
        (97.5, 97.5),
        ("    97", 97.0),
        ("Rs.750 to Rs.788", 788.0),
        ("95-99", 99.0),
        ("Rs.1,050 to Rs.1,100", 1100.0),
        ("n/a", None),
        ("", None),
    ])
    def test_extract(self, raw, expected):
        assert ipo._extract_price_band_upper(raw) == expected

    def test_value_error_is_none(self, monkeypatch):
        monkeypatch.setattr(ipo.re, "findall", lambda *a, **k: ["1.2.3"])
        assert ipo._extract_price_band_upper("x") is None


class TestPlaceholder:
    @pytest.mark.parametrize("v", [None, "", "  ", "-", "--", "N/A", "NA", "null", "None"])
    def test_placeholders(self, v):
        assert ipo._nse_placeholder_none(v) is None

    def test_real_value_kept(self):
        assert ipo._nse_placeholder_none("06-Aug-2026") == "06-Aug-2026"
        assert ipo._nse_placeholder_none(5) == 5


class TestNameMatchScore:
    def test_empty(self):
        assert ipo._name_match_score("", "abc") == 0.0
        assert ipo._name_match_score("abc", "") == 0.0
        assert ipo._name_match_score(None, None) == 0.0
        assert ipo._name_match_score("!!!", "abc") == 0.0

    def test_exact_ignores_punctuation_and_case(self):
        assert ipo._name_match_score("Tempsens (India) Ltd.", "tempsens india ltd") == 1.0

    def test_substring_either_direction(self):
        assert ipo._name_match_score("Tempsens", "Tempsens Instruments India Limited") == 0.9
        assert ipo._name_match_score("Tempsens Instruments India Limited", "Tempsens") == 0.9

    def test_token_overlap(self):
        s = ipo._name_match_score("alpha beta gamma", "alpha beta delta")
        assert s == pytest.approx(2 / 3 * 0.8)

    def test_no_overlap(self):
        assert ipo._name_match_score("alpha", "omega") == 0.0

    def test_defensive_empty_token_guard(self, monkeypatch):
        """`q`/`c` are non-empty here, so a real string always yields tokens; the guard is purely
        defensive. Force it with strings whose split() returns nothing."""
        class NoTokens(str):
            def split(self, *a, **k):
                return []

        class Wrapper:
            def __init__(self, text):
                self.text = text

            def lower(self):
                return self

            def strip(self):
                return NoTokens(self.text)
        monkeypatch.setattr(ipo.re, "sub", lambda pat, repl, string, *a, **k: Wrapper(string))
        assert ipo._name_match_score("abc", "xyz") == 0.0



# ── kv-backed helpers: quota, job progress ────────────────────────────────────

class TestQuota:
    def test_key_uses_ist_date(self):
        assert ipo._ipoalerts_quota_key() == f"stockky:ipoalerts:quota:{_today().strftime('%Y-%m-%d')}"

    def test_used_default_zero(self, kv):
        assert ipo._ipoalerts_quota_used() == 0

    def test_used_reads_int(self, kv):
        kv.store[ipo._ipoalerts_quota_key()] = "7"
        assert ipo._ipoalerts_quota_used() == 7

    def test_used_swallows_errors(self, kv):
        kv.get_raises = RuntimeError("down")
        assert ipo._ipoalerts_quota_used() == 0
        kv.get_raises = None
        kv.store[ipo._ipoalerts_quota_key()] = "abc"
        assert ipo._ipoalerts_quota_used() == 0

    def test_spend_adds_and_sets_ttl(self, kv):
        kv.store[ipo._ipoalerts_quota_key()] = 3
        ipo._ipoalerts_quota_spend(2)
        assert kv.sets[-1] == (ipo._ipoalerts_quota_key(), 5, 90000)

    def test_spend_swallows_errors(self, kv):
        kv.set_raises = RuntimeError("down")
        ipo._ipoalerts_quota_spend(1)   # must not raise


class TestJobProgress:
    def test_set_job_merges_and_persists(self, kv):
        out = ipo._set_job(status="running", processed=1)
        assert out["status"] == "running" and out["processed"] == 1
        assert "updated_at" in out
        key, val, ttl = kv.sets[-1]
        assert key == ipo.IPO_JOB_KEY and ttl == 3600 and val["processed"] == 1

    def test_set_job_kv_failure_still_returns(self, kv):
        kv.set_raises = RuntimeError("down")
        out = ipo._set_job(status="done")
        assert out["status"] == "done"

    def test_progress_returns_durable_when_not_running(self, kv):
        kv.store[ipo.IPO_JOB_KEY] = {"status": "done", "processed": 9}
        assert ipo.get_ipo_scan_progress() == {"status": "done", "processed": 9}

    def test_progress_overlays_local_when_running(self, kv):
        kv.store[ipo.IPO_JOB_KEY] = {"status": "done", "processed": 9, "extra": 1}
        ipo._LOCAL_JOB.update(status="running", processed=2)
        got = ipo.get_ipo_scan_progress()
        assert got["status"] == "running" and got["processed"] == 2 and got["extra"] == 1

    def test_progress_falls_back_to_local_when_durable_missing(self, kv):
        assert ipo.get_ipo_scan_progress()["status"] == "idle"

    def test_progress_falls_back_on_kv_error(self, kv):
        kv.get_raises = RuntimeError("down")
        assert ipo.get_ipo_scan_progress()["status"] == "idle"


# ── ipoalerts discovery ───────────────────────────────────────────────────────

class TestNormalizeIpoalertsRow:
    def test_full_row(self):
        row = {"symbol": " abc ", "name": "ABC Ltd", "priceRange": "95-99", "listingDate": "2026-09-01",
               "gmp": 12, "subscriptionTimes": 30}
        out = ipo._normalize_ipoalerts_row(row, "listed")
        assert out == {
            "symbol": "ABC", "company_name": "ABC Ltd", "issue_price": 99.0, "listing_date": "2026-09-01",
            "status": "listed", "source": "ipoalerts", "subscription_times": 30, "gmp": 12.0,
        }

    def test_missing_symbol(self):
        assert ipo._normalize_ipoalerts_row({}, "open") is None
        assert ipo._normalize_ipoalerts_row({"symbol": "  "}, "open") is None

    def test_non_equity_rejected(self):
        assert ipo._normalize_ipoalerts_row({"symbol": "1150VIES30"}, "open") is None

    def test_price_range_with_commas_and_garbage(self):
        out = ipo._normalize_ipoalerts_row({"symbol": "A", "priceRange": "1,050-1,100"}, "open")
        assert out["issue_price"] == 1100.0
        out = ipo._normalize_ipoalerts_row({"symbol": "A", "priceRange": "abc"}, "open")
        assert out["issue_price"] is None
        out = ipo._normalize_ipoalerts_row({"symbol": "A"}, "open")
        assert out["issue_price"] is None and out["company_name"] == "A"

    def test_gmp_variants(self):
        f = lambda **kw: ipo._normalize_ipoalerts_row({"symbol": "A", **kw}, "open")["gmp"]
        assert f(greyMarketPremium="5.5") == 5.5
        assert f(gmp={"value": 3}) == 3.0
        assert f(gmp={"premium": 4}) == 4.0
        assert f(gmp="bad") is None
        assert f() is None

    def test_subscription_total_fallback(self):
        out = ipo._normalize_ipoalerts_row({"symbol": "A", "subscription": {"total": 12.5}}, "open")
        assert out["subscription_times"] == 12.5
        out = ipo._normalize_ipoalerts_row({"symbol": "A"}, "open")
        assert out["subscription_times"] is None

    def test_exception_returns_none(self):
        assert ipo._normalize_ipoalerts_row(None, "open") is None


class TestFetchIpoalerts:
    @pytest.fixture(autouse=True)
    def _key(self, monkeypatch):
        monkeypatch.setattr(ipo, "IPOALERTS_API_KEY", "k")

    def _row(self, sym, listing=None, price="95-99"):
        return {"symbol": sym, "name": sym + " Ltd", "priceRange": price, "listingDate": listing}

    def test_no_key_returns_empty(self, monkeypatch, kv):
        monkeypatch.setattr(ipo, "IPOALERTS_API_KEY", "")
        assert ipo.fetch_ipoalerts_calendar() == []

    def test_cache_hit(self, kv, hx):
        kv.store[ipo.IPOALERTS_CACHE_KEY] = [{"symbol": "X"}]
        assert ipo.fetch_ipoalerts_calendar() == [{"symbol": "X"}]
        assert hx.calls == []

    def test_cache_read_error_falls_through(self, kv, hx):
        kv.get_raises_for[ipo.IPOALERTS_CACHE_KEY] = RuntimeError("down")
        hx.route("ipoalerts", FakeResp(200, {"ipos": []}))
        assert ipo.fetch_ipoalerts_calendar() == []
        assert len(hx.calls) == 2

    def test_bypass_cache(self, kv, hx):
        kv.store[ipo.IPOALERTS_CACHE_KEY] = [{"symbol": "X"}]
        hx.route("ipoalerts", FakeResp(200, {"ipos": []}))
        assert ipo.fetch_ipoalerts_calendar(_bypass_cache=True) == []
        assert len(hx.calls) == 2

    def test_quota_exhausted_reuses_cache_list(self, kv, hx, monkeypatch):
        monkeypatch.setattr(ipo, "IPOALERTS_DAILY_LIMIT", 1)
        # first kv_get (cache) returns non-list -> continue; quota check trips; second read returns list
        seq = iter([None, [{"symbol": "Z"}]])
        real = kv.module()
        m = types.ModuleType("kv_cache")
        m.kv_get = lambda key: (next(seq) if key == ipo.IPOALERTS_CACHE_KEY else None)
        m.kv_set = real.kv_set
        monkeypatch.setitem(sys.modules, "kv_cache", m)
        assert ipo.fetch_ipoalerts_calendar() == [{"symbol": "Z"}]
        assert hx.calls == []

    def test_quota_exhausted_no_cache(self, kv, hx, monkeypatch):
        monkeypatch.setattr(ipo, "IPOALERTS_DAILY_LIMIT", 1)
        assert ipo.fetch_ipoalerts_calendar() == []

    def test_quota_exhausted_cache_read_raises(self, kv, hx, monkeypatch):
        monkeypatch.setattr(ipo, "IPOALERTS_DAILY_LIMIT", 1)
        calls = {"n": 0}
        real = kv.module()
        m = types.ModuleType("kv_cache")

        def kv_get(key):
            if key == ipo.IPOALERTS_CACHE_KEY:
                calls["n"] += 1
                if calls["n"] == 2:
                    raise RuntimeError("down")
            return None
        m.kv_get, m.kv_set = kv_get, real.kv_set
        monkeypatch.setitem(sys.modules, "kv_cache", m)
        assert ipo.fetch_ipoalerts_calendar() == []

    def test_happy_path_filters_dedupes_and_caches(self, kv, hx):
        recent = _iso(2)
        old = _iso(30)
        listed = {"ipos": [self._row("REC", recent), self._row("OLD", old), self._row("NODATE"),
                           {"symbol": ""}]}
        opened = {"ipos": [self._row("OPEN"), self._row("REC", recent)]}   # REC duplicated across statuses

        def handler(url, params, kw):
            return FakeResp(200, opened if params["status"] == "open" else listed)
        hx.route("ipoalerts", handler)
        out = ipo.fetch_ipoalerts_calendar()
        assert [r["symbol"] for r in out] == ["OPEN", "REC"]
        assert kv.store[ipo.IPOALERTS_CACHE_KEY] == out
        assert kv.store[ipo._ipoalerts_quota_key()] == 2
        # headers carry the API key
        assert hx.calls[0][2]["headers"]["X-API-KEY"] == "k"

    def test_future_listed_row_dropped(self, kv, hx):
        future = (_today() + timedelta(days=5)).strftime("%Y-%m-%d")
        hx.route("ipoalerts", lambda u, p, k: FakeResp(200, {"ipos": [self._row("F", future)]}
                                                       if p["status"] == "listed" else {"ipos": []}))
        assert ipo.fetch_ipoalerts_calendar() == []

    def test_non_200_and_exception_count_as_spent(self, kv, hx):
        def handler(url, params, kw):
            if params["status"] == "open":
                return FakeResp(400, text="bad status")
            raise RuntimeError("timeout")
        hx.route("ipoalerts", handler)
        assert ipo.fetch_ipoalerts_calendar() == []
        assert kv.store[ipo._ipoalerts_quota_key()] == 2

    def test_data_without_ipos_key(self, kv, hx):
        hx.route("ipoalerts", FakeResp(200, {"ipos": None}))
        assert ipo.fetch_ipoalerts_calendar() == []

    def test_rate_limiter_failure_ignored(self, kv, hx, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("rl")
        monkeypatch.setitem(sys.modules, "rate_limiter", types.SimpleNamespace(acquire=boom))
        hx.route("ipoalerts", FakeResp(200, {"ipos": []}))
        assert ipo.fetch_ipoalerts_calendar() == []

    def test_cache_write_failure_ignored(self, kv, hx):
        hx.route("ipoalerts", FakeResp(200, {"ipos": [self._row("OPEN")]}))
        kv.set_raises_for[ipo.IPOALERTS_CACHE_KEY] = RuntimeError("down")
        out = ipo.fetch_ipoalerts_calendar()
        assert [r["symbol"] for r in out] == ["OPEN"]

    def test_listed_row_with_naive_and_aware_dates(self, kv, hx, monkeypatch):
        # a tz-aware parsed date must not be re-stamped
        aware = datetime.now(ipo.IST) - timedelta(days=1)
        monkeypatch.setattr(ipo, "_parse_date", lambda d: aware)
        hx.route("ipoalerts", lambda u, p, k: FakeResp(200, {"ipos": [self._row("A", "x")]}
                                                       if p["status"] == "listed" else {"ipos": []}))
        assert [r["symbol"] for r in ipo.fetch_ipoalerts_calendar()] == ["A"]


# ── NSE session / calendar ────────────────────────────────────────────────────

class TestNseSession:
    def test_bootstrap_builds_client_and_caches(self, hx):
        c = ipo._nse_session()
        assert isinstance(c, FakeClient)
        assert c.init_kwargs["headers"] is ipo.NSE_HEADERS and c.init_kwargs["follow_redirects"] is True
        # both hops use the navigation headers, not the JSON ones
        assert [g[1]["headers"] for g in c.gets] == [ipo.NSE_BOOTSTRAP_HEADERS] * 2
        assert ipo._NSE_SESSION_CACHE["client"] is c
        assert ipo._nse_session() is c          # cache hit
        assert len(FakeClient.instances) == 1

    def test_force_new_replaces_and_closes_old(self, hx):
        first = ipo._nse_session()
        second = ipo._nse_session(force_new=True)
        assert second is not first and first.closed is True
        assert ipo._NSE_SESSION_CACHE["client"] is second

    def test_expired_cache_rebuilds(self, hx):
        first = ipo._nse_session()
        ipo._NSE_SESSION_CACHE["ts"] = 0.0
        second = ipo._nse_session()
        assert second is not first

    def test_old_client_close_error_swallowed(self, hx):
        first = ipo._nse_session()
        first.close_raises = True
        second = ipo._nse_session(force_new=True)
        assert ipo._NSE_SESSION_CACHE["client"] is second

    def test_no_cookies_warns(self, hx, caplog):
        FakeClient.next_cookies = ()
        with caplog.at_level("WARNING", logger="ipo-scanner"):
            ipo._nse_session()
        assert "no cookies" in caplog.text

    def test_weak_cookies_info(self, hx, caplog):
        FakeClient.next_cookies = ("abck", "bm_sv")
        with caplog.at_level("INFO", logger="ipo-scanner"):
            ipo._nse_session()
        assert "weak" in caplog.text and "abck,bm_sv" in caplog.text

    def test_good_cookies_debug(self, hx, caplog):
        with caplog.at_level("DEBUG", logger="ipo-scanner"):
            ipo._nse_session()
        assert "nse bootstrap ok" in caplog.text

    def test_bootstrap_exception_still_caches_client(self, hx, caplog):
        FakeClient.bootstrap_raises = RuntimeError("blocked")
        with caplog.at_level("DEBUG", logger="ipo-scanner"):
            c = ipo._nse_session()
        assert ipo._NSE_SESSION_CACHE["client"] is c
        assert "nse session bootstrap" in caplog.text


class TestFetchNseCalendar:
    @pytest.fixture
    def client(self, monkeypatch, hx):
        c = FakeClient()
        ipo._NSE_SESSION_CACHE["client"] = c
        ipo._NSE_SESSION_CACHE["ts"] = 10.0 ** 12    # far future -> cache hit for the whole test
        monkeypatch.setattr(ipo, "_nse_session", lambda force_new=False: c)
        return c

    def test_list_and_dict_payloads(self, client):
        client.routes["public-past-issues"] = FakeResp(200, [
            {"symbol": "AAA", "issuePrice": "100", "listingDate": "06-Aug-2026"}])
        client.routes["all-upcoming-issues"] = FakeResp(200, {"data": [
            {"symbol": "BBB", "priceRange": "Rs.10 to Rs.12", "issueEndDate": "10-Sep-2026", "status": "Active"},
            {"symbol": ""}]})
        out = ipo.fetch_nse_ipo_calendar()
        assert [r["symbol"] for r in out] == ["AAA", "BBB"]
        assert out[0]["stage"] == "listed" and out[1]["stage"] == "upcoming"
        assert client.closed is False       # shared client is never closed here

    def test_dict_payload_without_data(self, client):
        client.routes["public-past-issues"] = FakeResp(200, {"data": None})
        client.routes["all-upcoming-issues"] = FakeResp(200, {})
        assert ipo.fetch_nse_ipo_calendar() == []

    def test_blocked_403_resets_cache_ts(self, client):
        client.routes["public-past-issues"] = FakeResp(403)
        client.routes["all-upcoming-issues"] = FakeResp(500)
        ipo._NSE_SESSION_CACHE["client"] = client
        ipo._NSE_SESSION_CACHE["ts"] = 123.0
        # 403 resets ts; the later 500 leaves it alone
        assert ipo.fetch_nse_ipo_calendar() == []
        assert ipo._NSE_SESSION_CACHE["ts"] == 0.0

    def test_403_for_different_cached_client_leaves_ts(self, client):
        client.routes["public-past-issues"] = FakeResp(401)
        client.routes["all-upcoming-issues"] = FakeResp(200, [])
        ipo._NSE_SESSION_CACHE["client"] = object()     # somebody else's client now
        ipo._NSE_SESSION_CACHE["ts"] = 55.0
        ipo.fetch_nse_ipo_calendar()
        assert ipo._NSE_SESSION_CACHE["ts"] == 55.0

    def test_get_exception_and_bad_json(self, client):
        client.routes["public-past-issues"] = RuntimeError("timeout")
        client.routes["all-upcoming-issues"] = FakeResp(200, json_raises=ValueError("not json"))
        assert ipo.fetch_nse_ipo_calendar() == []

    def test_rate_limiter_failure_ignored(self, client, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("rl")
        monkeypatch.setitem(sys.modules, "rate_limiter", types.SimpleNamespace(acquire=boom))
        assert ipo.fetch_nse_ipo_calendar() == []

    def test_outer_guard_when_error_handler_itself_fails(self, client, monkeypatch):
        """Defensive outer `except`: only reachable if the inner handler's own logging raises."""
        client.routes["public-past-issues"] = RuntimeError("timeout")
        warned = []

        class BadLogger:
            def info(self, msg, *a, **k):
                if "fetch failed" in msg:
                    raise RuntimeError("log boom")

            def warning(self, msg, *a, **k):
                warned.append(msg % a)

            debug = info
        monkeypatch.setattr(ipo, "logger", BadLogger())
        assert ipo.fetch_nse_ipo_calendar() == []
        assert warned and "session failed entirely" in warned[0]


class TestNormalizeNseRow:
    def test_listed_row(self):
        out = ipo._normalize_nse_row({
            "symbol": "aaa", "companyName": "AAA Ltd", "issuePrice": "    97", "listingDate": "06-AUG-2026",
            "issueStartDate": "01-Aug-2026", "issueEndDate": "03-Aug-2026", "status": "Closed"}, "past")
        assert out["symbol"] == "AAA" and out["issue_price"] == 97.0
        assert out["listing_date"] == "2026-08-06" and out["listing_date_estimated"] is False
        assert out["stage"] == "listed" and out["status"] == "past" and out["source"] == "nse_auto"
        assert out["company_name"] == "AAA Ltd" and out["nse_status"] == "Closed"
        assert out["issue_start_date"] == "01-Aug-2026" and out["issue_end_date"] == "03-Aug-2026"

    def test_symbol_fallbacks(self):
        assert ipo._normalize_nse_row({"scriptCode": "sc"}, "past")["symbol"] == "SC"
        assert ipo._normalize_nse_row({"companyName": "Foo"}, "past")["symbol"] == "FOO"
        assert ipo._normalize_nse_row({"company": "Bar", "ipoStartDate": "s", "ipoEndDate": "e"}, "past")["symbol"] == "BAR"

    def test_empty_symbol_and_non_equity(self):
        assert ipo._normalize_nse_row({}, "past") is None
        assert ipo._normalize_nse_row({"symbol": "1150VIES30"}, "past") is None

    def test_company_name_fallbacks(self):
        assert ipo._normalize_nse_row({"symbol": "A", "company": "Co"}, "past")["company_name"] == "Co"
        assert ipo._normalize_nse_row({"symbol": "A"}, "past")["company_name"] == "A"

    def test_price_fallback_chain_skips_placeholders(self):
        row = {"symbol": "A", "issuePrice": "-", "priceRange": "Rs.342 to Rs.360"}
        assert ipo._normalize_nse_row(row, "past")["issue_price"] == 360.0
        row = {"symbol": "A", "issuePrice": "-", "priceRange": "N/A", "cutOffPrice": "55"}
        assert ipo._normalize_nse_row(row, "past")["issue_price"] == 55.0
        row = {"symbol": "A", "finalIssuePrice": "70"}
        assert ipo._normalize_nse_row(row, "past")["issue_price"] == 70.0
        assert ipo._normalize_nse_row({"symbol": "A"}, "past")["issue_price"] is None

    def test_listing_date_fallback_key_and_unparseable(self):
        out = ipo._normalize_nse_row({"symbol": "A", "dateOfListing": "2026-08-06"}, "past")
        assert out["listing_date"] == "2026-08-06"
        out = ipo._normalize_nse_row({"symbol": "A", "listingDate": "soon"}, "past")
        assert out["listing_date"] is None and out["stage"] == "past"

    def test_estimated_listing_from_issue_end(self):
        out = ipo._normalize_nse_row({"symbol": "A", "issueEndDate": "10-Sep-2026", "listingDate": "-"}, "upcoming")
        assert out["listing_date"] == "2026-09-13"
        assert out["listing_date_estimated"] is True and out["stage"] == "upcoming"

    def test_stage_from_status_or_issue_end_without_estimate(self):
        out = ipo._normalize_nse_row({"symbol": "A", "status": "Forthcoming"}, "past")
        assert out["stage"] == "upcoming" and out["listing_date"] is None
        out = ipo._normalize_nse_row({"symbol": "A", "issueEndDate": "garbage"}, "past")
        assert out["stage"] == "upcoming" and out["listing_date_estimated"] is False
        out = ipo._normalize_nse_row({"symbol": "A", "status": "Closed"}, "past")
        assert out["stage"] == "past"

    def test_exception_returns_none(self):
        assert ipo._normalize_nse_row(None, "past") is None


# ── manual entries / name resolution ──────────────────────────────────────────

class TestAddManualIpo:
    def test_entry_shape_and_persist(self, kv):
        e = ipo.add_manual_ipo("abc.ns", "99", "2026-09-01", company_name="ABC Ltd", subscription_times=3, gmp=4)
        assert e["symbol"] == "ABC" and e["issue_price"] == 99.0 and e["company_name"] == "ABC Ltd"
        assert e["status"] == "manual" and e["source"] == "manual" and e["added_at"]
        key, val, ttl = kv.sets[-1]
        assert key == ipo.IPO_MANUAL_KEY and ttl == 90 * 86400 and val == [e]

    def test_replaces_same_symbol_and_defaults_name(self, kv):
        kv.store[ipo.IPO_MANUAL_KEY] = [{"symbol": "ABC", "issue_price": 1}, {"symbol": "XYZ"}]
        e = ipo.add_manual_ipo(" abc.bo ", 5, "2026-09-01")
        assert e["company_name"] == "ABC"
        assert [x["symbol"] for x in kv.store[ipo.IPO_MANUAL_KEY]] == ["XYZ", "ABC"]

    def test_kv_read_error_starts_fresh(self, kv):
        kv.get_raises = RuntimeError("down")
        kv.set_raises = RuntimeError("down")      # persist failure is logged, entry still returned
        assert ipo.add_manual_ipo("A", 1, "2026-09-01")["symbol"] == "A"

    def test_non_list_existing_ignored(self, kv):
        kv.store[ipo.IPO_MANUAL_KEY] = {"oops": 1}
        ipo.add_manual_ipo("A", 1, "2026-09-01")
        assert [x["symbol"] for x in kv.store[ipo.IPO_MANUAL_KEY]] == ["A"]


class TestManualIpos:
    def test_list(self, kv):
        kv.store[ipo.IPO_MANUAL_KEY] = [{"symbol": "A"}]
        assert ipo._manual_ipos() == [{"symbol": "A"}]

    def test_non_list_and_error(self, kv):
        kv.store[ipo.IPO_MANUAL_KEY] = "x"
        assert ipo._manual_ipos() == []
        kv.get_raises = RuntimeError("down")
        assert ipo._manual_ipos() == []


class TestResolveByName:
    def _cand(self, sym, name, price=10.0, date="2026-09-01"):
        return {"symbol": sym, "company_name": name, "issue_price": price, "listing_date": date}

    def test_blank_name(self):
        assert ipo.resolve_ipo_by_name("  ") == (None, [])
        assert ipo.resolve_ipo_by_name(None) == (None, [])

    def test_nse_confident_match_skips_ipoalerts(self, monkeypatch):
        cands = [self._cand("TEMP", "Tempsens Instruments India Limited"), self._cand("OTH", "Other Co")]
        monkeypatch.setattr(ipo, "fetch_nse_ipo_calendar", lambda: cands)
        monkeypatch.setattr(ipo, "IPOALERTS_API_KEY", "k")
        monkeypatch.setattr(ipo, "fetch_ipoalerts_calendar", lambda: pytest.fail("must not spend quota"))
        best, near = ipo.resolve_ipo_by_name("Tempsens")
        assert best["symbol"] == "TEMP" and near == []

    def test_match_by_symbol(self, monkeypatch):
        monkeypatch.setattr(ipo, "fetch_nse_ipo_calendar", lambda: [self._cand("TEMP", "Zzz")])
        monkeypatch.setattr(ipo, "IPOALERTS_API_KEY", "")
        best, _ = ipo.resolve_ipo_by_name("temp")
        assert best["symbol"] == "TEMP"

    def test_falls_back_to_ipoalerts(self, monkeypatch):
        monkeypatch.setattr(ipo, "fetch_nse_ipo_calendar", lambda: [self._cand("OTH", "Other Co")])
        monkeypatch.setattr(ipo, "IPOALERTS_API_KEY", "k")
        ia = [self._cand("LOW", "Unrelated Name"), self._cand("NEW", "Brand New Industries Limited")]
        monkeypatch.setattr(ipo, "fetch_ipoalerts_calendar", lambda: ia)
        best, near = ipo.resolve_ipo_by_name("Brand New Industries Limited")
        assert best["symbol"] == "NEW" and near == []

    def test_near_misses_sorted_filtered_and_capped(self, monkeypatch):
        # query has 4 tokens: 3 shared -> 0.6, 2 shared -> 0.4, 1 shared -> 0.2 (below the 0.35 floor)
        names = ["alpha beta q1 r1", "alpha beta gamma z1", "alpha beta q2 r2", "alpha beta q3 r3",
                 "alpha beta q4 r4", "alpha beta q5 r5", "alpha q6 r6 s6"]
        cands = [self._cand(f"S{i}", n) for i, n in enumerate(names)]
        monkeypatch.setattr(ipo, "fetch_nse_ipo_calendar", lambda: cands)
        monkeypatch.setattr(ipo, "IPOALERTS_API_KEY", "")
        best, near = ipo.resolve_ipo_by_name("alpha beta gamma delta")
        assert best is None
        assert len(near) == 5                       # 6 qualify, capped at 5
        assert near[0]["symbol"] == "S1"            # highest score (0.6) first
        assert "S6" not in [n["symbol"] for n in near]

    def test_no_candidates(self, monkeypatch):
        monkeypatch.setattr(ipo, "fetch_nse_ipo_calendar", lambda: [])
        monkeypatch.setattr(ipo, "IPOALERTS_API_KEY", "")
        assert ipo.resolve_ipo_by_name("Whatever Ltd") == (None, [])

    def test_candidate_without_names(self, monkeypatch):
        monkeypatch.setattr(ipo, "fetch_nse_ipo_calendar", lambda: [{"symbol": None, "company_name": None}])
        monkeypatch.setattr(ipo, "IPOALERTS_API_KEY", "")
        assert ipo.resolve_ipo_by_name("Whatever Ltd") == (None, [])


class TestAddManualByName:
    def test_blank(self):
        out = ipo.add_manual_ipo_by_name("  ")
        assert out == {"resolved": False, "message": "Company name is required.", "suggestions": []}

    def test_unresolved_with_suggestions_no_key(self, monkeypatch):
        monkeypatch.setattr(ipo, "resolve_ipo_by_name",
                            lambda n: (None, [{"company_name": "Near Co"}, {"company_name": None}]))
        monkeypatch.setattr(ipo, "IPOALERTS_API_KEY", "")
        out = ipo.add_manual_ipo_by_name("Nea")
        assert out["resolved"] is False and out["suggestions"] == ["Near Co"]
        assert "NSE's calendar." in out["message"] and "ipoalerts" not in out["message"]

    def test_unresolved_mentions_ipoalerts_when_configured(self, monkeypatch):
        monkeypatch.setattr(ipo, "resolve_ipo_by_name", lambda n: (None, []))
        monkeypatch.setattr(ipo, "IPOALERTS_API_KEY", "k")
        assert "or ipoalerts" in ipo.add_manual_ipo_by_name("Nea")["message"]

    def test_resolved_persists(self, monkeypatch, kv):
        match = {"symbol": "TEMP", "issue_price": "100", "listing_date": "2026-09-01",
                 "company_name": "Tempsens", "subscription_times": 5, "gmp": 2}
        monkeypatch.setattr(ipo, "resolve_ipo_by_name", lambda n: (match, []))
        out = ipo.add_manual_ipo_by_name(" Tempsens ")
        assert out["resolved"] is True and out["entry"]["symbol"] == "TEMP"
        assert out["entry"]["gmp"] == 2 and out["entry"]["subscription_times"] == 5

    def test_resolved_without_company_name(self, monkeypatch, kv):
        match = {"symbol": "TEMP", "issue_price": 100, "listing_date": "2026-09-01"}
        monkeypatch.setattr(ipo, "resolve_ipo_by_name", lambda n: (match, []))
        assert ipo.add_manual_ipo_by_name("x")["entry"]["company_name"] == "TEMP"


# ── merged universe ───────────────────────────────────────────────────────────

class TestMergedUniverse:
    def _stub(self, monkeypatch, nse=(), ia=(), manual=()):
        monkeypatch.setattr(ipo, "fetch_nse_ipo_calendar", lambda: list(nse))
        monkeypatch.setattr(ipo, "fetch_ipoalerts_calendar", lambda: list(ia))
        monkeypatch.setattr(ipo, "_manual_ipos", lambda: list(manual))

    def test_precedence_and_diag(self, monkeypatch):
        d = _iso(3)
        nse = [{"symbol": "A", "issue_price": 1, "listing_date": d, "source": "nse_auto"}]
        ia = [{"symbol": "A", "issue_price": 2, "listing_date": d, "source": "ipoalerts"},
              {"symbol": "B", "issue_price": 3, "listing_date": d}]
        manual = [{"symbol": "B", "issue_price": 9, "listing_date": "2000-01-01", "source": "manual"}]
        self._stub(monkeypatch, nse, ia, manual)
        monkeypatch.setattr(ipo, "IPOALERTS_API_KEY", "k")
        recent, diag = ipo._merged_ipo_universe()
        by = {r["symbol"]: r for r in recent}
        assert by["A"]["source"] == "ipoalerts" and by["B"]["source"] == "manual"   # manual bypasses the window
        assert diag == {"nse_candidates": 1, "ipoalerts_candidates": 2, "ipoalerts_configured": True,
                        "manual_candidates": 1, "merged_total": 2, "merged_dated": 2, "recent_after_window": 2}

    def test_non_equity_skipped_for_auto_sources_but_not_manual(self, monkeypatch):
        d = _iso(3)
        row = lambda s: {"symbol": s, "issue_price": 1, "listing_date": d}
        self._stub(monkeypatch, [row("1150VIES30")], [row("925ECL28")], [row("777MAN")])
        recent, diag = ipo._merged_ipo_universe()
        assert [r["symbol"] for r in recent] == ["777MAN"]
        assert diag["merged_total"] == 1

    def test_manual_without_symbol_ignored_in_symbol_set(self, monkeypatch):
        self._stub(monkeypatch, [], [], [{"symbol": "M", "issue_price": 1, "listing_date": _iso(1)}])
        recent, _ = ipo._merged_ipo_universe()
        assert [r["symbol"] for r in recent] == ["M"]

    def test_window_filtering(self, monkeypatch, caplog):
        row = lambda s, days, **kw: {"symbol": s, "issue_price": 1, "listing_date": _iso(days), **kw}
        nse = [
            row("FRESH", 10),
            row("TOOOLD", 400),
            row("SOON", -5),
            row("TOOFAR", -60),
            row("UPC", -200, stage="upcoming"),
            {"symbol": "BADDATE", "issue_price": 1, "listing_date": "garbage"},
            {"symbol": "NOPRICE", "issue_price": None, "listing_date": _iso(1)},
            {"symbol": "NODATE", "issue_price": 1, "listing_date": None},
        ]
        self._stub(monkeypatch, nse)
        with caplog.at_level("INFO", logger="ipo-scanner"):
            recent, diag = ipo._merged_ipo_universe()
        assert sorted(r["symbol"] for r in recent) == ["FRESH", "SOON", "UPC"]
        assert diag["merged_total"] == 8 and diag["merged_dated"] == 6 and diag["recent_after_window"] == 3
        assert "ipo universe: 6 candidates -> 3" in caplog.text

    def test_aware_datetime_kept(self, monkeypatch):
        aware = datetime.now(ipo.IST) - timedelta(days=2)
        monkeypatch.setattr(ipo, "_parse_date", lambda d: aware)
        self._stub(monkeypatch, [{"symbol": "A", "issue_price": 1, "listing_date": "x"}])
        recent, _ = ipo._merged_ipo_universe()
        assert [r["symbol"] for r in recent] == ["A"]

    def test_no_log_when_nothing_filtered(self, monkeypatch, caplog):
        self._stub(monkeypatch, [{"symbol": "A", "issue_price": 1, "listing_date": _iso(1)}])
        with caplog.at_level("INFO", logger="ipo-scanner"):
            ipo._merged_ipo_universe()
        assert "ipo universe" not in caplog.text


# ── database helpers (fake ipo_schema + sqlalchemy) ───────────────────────────

class FakeText:
    def __init__(self, sql):
        self.sql = sql

    def __str__(self):
        return self.sql


class FakeResult:
    def __init__(self, scalar=None, one=None, rows=None):
        self._scalar, self._one, self._rows = scalar, one, rows or []

    def scalar(self):
        return self._scalar

    def fetchone(self):
        return self._one

    def __iter__(self):
        return iter(self._rows)


class FakeMappingRow:
    def __init__(self, d):
        self._mapping = d


class FakeConn:
    def __init__(self, db):
        self.db = db

    def execute(self, stmt, params=None):
        sql = str(stmt)
        self.db.executed.append((sql, params))
        if self.db.execute_raises is not None:
            raise self.db.execute_raises
        if self.db.fail_symbols and isinstance(params, dict) and params.get("symbol") in self.db.fail_symbols:
            raise RuntimeError("bad row " + str(params.get("symbol")))
        for frag, res in self.db.results:
            if frag in sql:
                return res
        return FakeResult()


class FakeCtx:
    def __init__(self, db, kind):
        self.db, self.kind = db, kind

    def __enter__(self):
        self.db.opened.append(self.kind)
        if self.db.open_raises is not None:
            raise self.db.open_raises
        return FakeConn(self.db)

    def __exit__(self, *a):
        return False


class FakeEngine:
    def __init__(self, db):
        self.db = db

    def begin(self):
        return FakeCtx(self.db, "begin")

    def connect(self):
        return FakeCtx(self.db, "connect")

    def dispose(self):
        self.db.disposed += 1
        if self.db.dispose_raises:
            raise RuntimeError("dispose boom")


class FakeDB:
    """Collects everything the fake schema/engine is asked to do."""

    def __init__(self):
        self.url = "postgresql://x"
        self.dial = "postgres"
        self.engine_none = False
        self.make_engine_raises = None
        self.executed = []
        self.opened = []
        self.results = []          # (sql fragment, FakeResult)
        self.fail_symbols = set()
        self.execute_raises = None
        self.open_raises = None
        self.dispose_raises = False
        self.disposed = 0
        self.adapt_calls = []
        self.ensure_raises = None
        self.ensure_calls = 0
        self.engine_names = []

    def schema_module(self):
        db = self
        m = types.ModuleType("ipo_schema")
        m.TABLE_NAME = "ipo_static_feed"
        m.ROW_KEYS = ("symbol", "company_name", "issue_price", "listing_date", "stage", "buy_suggestion_json")
        m.database_url = lambda: db.url
        m.dialect = lambda: db.dial

        def make_engine(name="x"):
            db.engine_names.append(name)
            if db.make_engine_raises is not None:
                raise db.make_engine_raises
            return None if db.engine_none else FakeEngine(db)
        m.make_engine = make_engine

        def adapt_rows(rows, dial):
            db.adapt_calls.append((copy.deepcopy(rows), dial))
            return rows
        m.adapt_rows = adapt_rows
        m.upsert_sql = lambda dial: f"UPSERT[{dial}]"
        m.table_exists_sql = lambda dial: f"EXISTS[{dial}]"

        def ensure():
            db.ensure_calls += 1
            if db.ensure_raises is not None:
                raise db.ensure_raises
            return {}
        m.ensure_ipo_schema = ensure
        return m

    def sqlalchemy_module(self):
        m = types.ModuleType("sqlalchemy")
        m.text = FakeText
        return m


@pytest.fixture
def db(monkeypatch):
    d = FakeDB()
    monkeypatch.setitem(sys.modules, "ipo_schema", d.schema_module())
    monkeypatch.setitem(sys.modules, "sqlalchemy", d.sqlalchemy_module())
    return d


class TestDbUpsert:
    ROW = {"symbol": "A", "company_name": "A Ltd", "issue_price": 10, "listing_date": "06-AUG-2026",
           "stage": "listed", "extra": "ignored", "buy_suggestion": {"decision": "BUY NOW"}}

    def test_ipo_schema_import_failure(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "ipo_schema", None)
        assert ipo._ipo_db_upsert([self.ROW]) == 0

    def test_no_url_or_no_rows(self, db):
        assert ipo._ipo_db_upsert([]) == 0
        db.url = None
        assert ipo._ipo_db_upsert([self.ROW]) == 0
        assert db.engine_names == []

    def test_sqlalchemy_missing(self, db, monkeypatch):
        monkeypatch.setitem(sys.modules, "sqlalchemy", None)
        assert ipo._ipo_db_upsert([self.ROW]) == 0

    def test_engine_unavailable(self, db):
        db.engine_none = True
        assert ipo._ipo_db_upsert([self.ROW]) == 0

    def test_postgres_single_transaction_and_payload(self, db):
        rows = [self.ROW, {"symbol": "B", "listing_date": None}]
        assert ipo._ipo_db_upsert(rows) == 2
        assert db.opened == ["begin"]
        (sql, params), = db.executed
        assert sql == "UPSERT[postgres]"
        assert params[0]["listing_date"] == "2026-08-06" and params[0]["buy_suggestion_json"] == '{"decision": "BUY NOW"}'
        assert "extra" not in params[0] and "buy_suggestion" not in params[0]
        assert params[1]["listing_date"] == "" and params[1]["buy_suggestion_json"] is None
        assert db.adapt_calls[0][1] == "postgres"
        assert db.disposed == 1 and db.engine_names == ["stockky-ipo-writer"]

    def test_oracle_per_row_transactions_skip_bad_rows(self, db, caplog):
        db.dial = "oracle"
        db.fail_symbols = {"BAD"}
        rows = [{"symbol": "A"}, {"symbol": "BAD"}, {"symbol": "C"}]
        with caplog.at_level("WARNING", logger="ipo-scanner"):
            assert ipo._ipo_db_upsert(rows) == 2
        assert db.opened == ["begin"] * 3
        assert "row for BAD failed" in caplog.text

    def test_outer_failure_returns_zero_and_disposes(self, db, caplog):
        db.execute_raises = RuntimeError("db down")
        with caplog.at_level("WARNING", logger="ipo-scanner"):
            assert ipo._ipo_db_upsert([self.ROW]) == 0
        assert "ipo db upsert failed" in caplog.text and db.disposed == 1

    def test_dispose_error_swallowed(self, db):
        db.dispose_raises = True
        assert ipo._ipo_db_upsert([self.ROW]) == 1

    def test_make_engine_raises_no_dispose(self, db):
        db.make_engine_raises = RuntimeError("no driver")
        assert ipo._ipo_db_upsert([self.ROW]) == 0
        assert db.disposed == 0


class TestDbWipe:
    def test_import_failures(self, db, monkeypatch):
        monkeypatch.setitem(sys.modules, "sqlalchemy", None)
        assert ipo._ipo_db_wipe() is False
        monkeypatch.setitem(sys.modules, "ipo_schema", None)
        assert ipo._ipo_db_wipe() is False

    def test_no_url_and_no_engine(self, db):
        db.url = ""
        assert ipo._ipo_db_wipe() is False
        db.url, db.engine_none = "x", True
        assert ipo._ipo_db_wipe() is False

    def test_success(self, db):
        assert ipo._ipo_db_wipe() is True
        assert db.executed[0][0] == "DELETE FROM ipo_static_feed" and db.disposed == 1

    def test_failure_and_dispose_error(self, db):
        db.execute_raises = RuntimeError("x")
        db.dispose_raises = True
        assert ipo._ipo_db_wipe() is False
        assert db.disposed == 1


class TestDbFreshness:
    def _setup(self, db, exists=1, last_at=None, one=True, dial="postgres"):
        db.dial = dial
        db.results = [
            ("EXISTS", FakeResult(scalar=exists)),
            ("MAX(updated_at)", FakeResult(one=((last_at,) if one else None))),
        ]

    def test_engine_none(self, db):
        db.engine_none = True
        assert ipo._ipo_db_freshness_hours() is None

    def test_import_error_swallowed(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "ipo_schema", None)
        assert ipo._ipo_db_freshness_hours() is None

    def test_missing_table(self, db):
        self._setup(db, exists=0)
        assert ipo._ipo_db_freshness_hours() is None
        assert db.disposed == 1

    def test_no_row_or_null_timestamp(self, db):
        self._setup(db, one=False)
        assert ipo._ipo_db_freshness_hours() is None
        self._setup(db, last_at=None)
        assert ipo._ipo_db_freshness_hours() is None

    def test_naive_timestamp_treated_as_utc(self, db):
        from datetime import timezone
        self._setup(db, last_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=5))
        assert ipo._ipo_db_freshness_hours() == pytest.approx(5.0, abs=0.01)

    def test_aware_timestamp_oracle_dialect(self, db):
        from datetime import timezone
        self._setup(db, last_at=datetime.now(timezone.utc) - timedelta(hours=2), dial="oracle")
        assert ipo._ipo_db_freshness_hours() == pytest.approx(2.0, abs=0.01)
        assert db.executed[0][0] == "EXISTS[oracle]" and db.executed[0][1] == {"tbl": "ipo_static_feed"}

    def test_query_error_is_none(self, db):
        db.execute_raises = RuntimeError("down")
        assert ipo._ipo_db_freshness_hours() is None
        assert db.disposed == 1


class TestDbDeleteSymbols:
    def test_empty_short_circuits(self, db):
        assert ipo._ipo_db_delete_symbols([]) == 0
        assert ipo._ipo_db_delete_symbols(None) == 0
        assert ipo._ipo_db_delete_symbols(["", None]) == 0
        assert db.engine_names == []

    def test_import_failures(self, db, monkeypatch):
        monkeypatch.setitem(sys.modules, "sqlalchemy", None)
        assert ipo._ipo_db_delete_symbols(["A"]) == 0
        monkeypatch.setitem(sys.modules, "ipo_schema", None)
        assert ipo._ipo_db_delete_symbols(["A"]) == 0

    def test_no_url_and_no_engine(self, db):
        db.url = None
        assert ipo._ipo_db_delete_symbols(["A"]) == 0
        db.url, db.engine_none = "x", True
        assert ipo._ipo_db_delete_symbols(["A"]) == 0

    def test_deletes_each_symbol(self, db):
        assert ipo._ipo_db_delete_symbols(["A", "", "B"]) == 2
        assert [p for _, p in db.executed] == [{"symbol": "A"}, {"symbol": "B"}]
        assert "DELETE FROM ipo_static_feed WHERE symbol = :symbol" in db.executed[0][0]
        assert db.engine_names == ["stockky-ipo-purge"]

    def test_failure_and_dispose_error(self, db):
        db.execute_raises = RuntimeError("x")
        db.dispose_raises = True
        assert ipo._ipo_db_delete_symbols(["A"]) == 0
        assert db.disposed == 1


# ── feed audit ────────────────────────────────────────────────────────────────

def _audit_rows(*rows):
    return [FakeMappingRow(r) for r in rows]


def _full(sym, **kw):
    return {"symbol": sym, "company_name": sym + " Ltd", "issue_price": 10, "listing_date": "2026-09-01",
            "stage": "listed", "nse_status": None, "subscription_times": None, "gmp": None,
            "ipo_score": 60.0, "decision": "HOLD", "updated_at": None, **kw}


class TestFeedAudit:
    def test_schema_import_failure(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "ipo_schema", None)
        out = ipo.get_ipo_feed_audit()
        assert out["ok"] is False and out["rows"] == [] and "ipo_schema import failed" in out["error"]

    def test_no_database_configured(self, db):
        db.url = None
        out = ipo.get_ipo_feed_audit()
        assert out["ok"] is True and out["total_tracked"] == 0 and out["missing_ipos"] == []
        assert "cache-only" in out["message"]

    def test_engine_unavailable(self, db):
        db.engine_none = True
        assert ipo.get_ipo_feed_audit() == {"ok": False, "error": "engine unavailable", "rows": []}

    def test_query_failure(self, db):
        db.execute_raises = RuntimeError("boom" * 100)
        out = ipo.get_ipo_feed_audit()
        assert out["ok"] is False and len(out["error"]) == 200 and out["rows"] == []
        assert db.disposed == 1

    def test_make_engine_raises(self, db):
        db.make_engine_raises = RuntimeError("no driver")
        out = ipo.get_ipo_feed_audit()
        assert out == {"ok": False, "error": "no driver", "rows": []} and db.disposed == 0

    def test_ensure_failure_and_dispose_error(self, db):
        db.ensure_raises = RuntimeError("ddl")
        db.dispose_raises = True
        out = ipo.get_ipo_feed_audit()
        assert out["ok"] is False and out["error"] == "ddl"

    def test_empty_table(self, db):
        db.results = [("FROM ipo_static_feed", FakeResult(rows=[]))]
        out = ipo.get_ipo_feed_audit()
        assert out["ok"] is True and out["total_tracked"] == 0 and out["health_score"] == 0.0
        assert out["message"] == "No IPO rows tracked yet — run Scan IPOs first."
        assert db.ensure_calls == 1

    def test_bucketing_health_and_message(self, db):
        rows = _audit_rows(
            _full("OK1"),
            _full("OK2"),
            _full("MISS", stage="listed", ipo_score=None, updated_at="2026-09-01"),
            _full("NOPRICE", issue_price="", decision=None),
            _full("1150VIES30", ipo_score=None),
            _full("NODATA", stage="no_data_yet", ipo_score=None),
            _full("UPC", stage="upcoming", ipo_score=None),
            _full("PRE", stage="pre_listing", ipo_score=None),
            _full("NOSTAGE", stage=None, decision=None),
        )
        db.results = [("FROM ipo_static_feed", FakeResult(rows=rows))]
        out = ipo.get_ipo_feed_audit()
        assert out["total_tracked"] == 9 and out["fully_scored"] == 2
        assert [m["symbol"] for m in out["missing_ipos"]] == ["MISS", "NOPRICE", "NOSTAGE"]
        assert out["missing_ipos"][0]["missing_fields"] == ["ipo_score"]
        assert out["missing_ipos"][0]["updated_at"] == "2026-09-01"
        assert out["missing_ipos"][1]["missing_fields"] == ["issue_price", "decision"]
        assert out["missing_ipos"][2]["updated_at"] == ""
        assert out["missing_count"] == 3
        assert [r["symbol"] for r in out["non_equity_ipos"]] == ["1150VIES30"] and out["non_equity_count"] == 1
        assert out["no_data_yet_count"] == 1 and out["pending_listing_count"] == 1 and out["pre_listing_count"] == 1
        # actionable = 9 - 1 - 1 - 1 - 1 = 5 ; scored 2 -> 40.0
        assert out["health_score"] == 40.0
        msg = out["message"]
        assert msg.startswith("IPO feed health 40.0% · 2/5 scored")
        for frag in ("1 waiting on Yahoo data", "1 non-equity (NCD/bond) rows", "1 not yet listed", "1 pre-open"):
            assert frag in msg

    def test_all_rows_unactionable_health_zero(self, db):
        db.results = [("FROM ipo_static_feed", FakeResult(rows=_audit_rows(
            _full("UPC", stage="upcoming", ipo_score=None))))]
        out = ipo.get_ipo_feed_audit()
        assert out["health_score"] == 0.0 and out["message"] == "IPO feed health 0.0% · 0/0 scored · 1 not yet listed"

    def test_caps_list_sizes(self, db):
        rows = _audit_rows(*[_full(f"M{i}", ipo_score=None) for i in range(250)],
                           *[_full(f"N{i}", stage="no_data_yet", ipo_score=None) for i in range(60)])
        db.results = [("FROM ipo_static_feed", FakeResult(rows=rows))]
        out = ipo.get_ipo_feed_audit()
        assert len(out["missing_ipos"]) == 200 and out["missing_count"] == 250
        assert len(out["no_data_yet_ipos"]) == 50 and out["no_data_yet_count"] == 60


# ── upstream fetchers: history, quote, GMP, fundamentals ──────────────────────

import pandas as pd  # noqa: E402  (test-only; the module under test imports it lazily)


def _yf_module(hist=None, raises=None, record=None):
    m = types.ModuleType("yfinance")

    class Ticker:
        def __init__(self, t):
            if record is not None:
                record["ticker"] = t

        def history(self, **kw):
            if record is not None:
                record["kw"] = kw
            if raises is not None:
                raise raises
            return hist
    m.Ticker = Ticker
    return m


def _aliases_module(resolve=lambda s: s + ".NS"):
    m = types.ModuleType("symbol_aliases")
    m.resolve_ns_ticker = resolve
    m.is_non_equity_instrument = lambda s: False
    return m


class TestFetchHistory:
    CANDLES = [
        {"date": "2026-09-01", "open": 10, "high": 12, "low": 9, "close": 11, "volume": 1000},
        {"date": "2026-09-02", "open": 11, "high": 13, "low": 10, "close": 12, "volume": 2000},
    ]

    def test_market_data_service_success(self, hx):
        hx.route("/history/AAA", FakeResp(200, {"candles": self.CANDLES}))
        df = ipo._fetch_history("AAA", 7)
        assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
        assert df.index.name == "Date" and len(df) == 2 and float(df["Close"].iloc[-1]) == 12.0
        url, params, kw = hx.calls[0]
        assert url == f"{ipo.MARKET_DATA_URL}/history/AAA"
        assert params == {"days": 7, "interval": "1d"} and kw["timeout"] == 15

    def test_days_floor_is_one(self, hx):
        hx.route("/history/AAA", FakeResp(200, {"candles": self.CANDLES}))
        ipo._fetch_history("AAA", -3)
        assert hx.calls[0][1]["days"] == 1

    def test_data_key_and_no_date_column(self, hx):
        rows = [{"close": 1, "high": 2, "low": 0.5, "volume": 5}]
        hx.route("/history/AAA", FakeResp(200, {"data": rows}))
        df = ipo._fetch_history("AAA", 3)
        assert "Close" in df.columns and df.index.name is None

    def test_no_close_column_falls_to_yfinance(self, hx, monkeypatch):
        hx.route("/history/AAA", FakeResp(200, {"candles": [{"date": "2026-09-01", "high": 1}]}))
        hist = pd.DataFrame({"Close": [1.0]})
        monkeypatch.setitem(sys.modules, "yfinance", _yf_module(hist))
        monkeypatch.setitem(sys.modules, "symbol_aliases", _aliases_module())
        assert ipo._fetch_history("AAA", 3) is hist

    @pytest.mark.parametrize("resp", [
        FakeResp(200, {"candles": []}),
        FakeResp(200, None),
        FakeResp(503),
        RuntimeError("md down"),
    ])
    def test_upstream_miss_uses_yfinance_fallback(self, hx, monkeypatch, resp):
        hx.route("/history/AAA", resp)
        rec = {}
        hist = pd.DataFrame({"Close": [1.0, 2.0]})
        monkeypatch.setitem(sys.modules, "yfinance", _yf_module(hist, record=rec))
        monkeypatch.setitem(sys.modules, "symbol_aliases", _aliases_module())
        assert ipo._fetch_history("AAA", 2) is hist
        assert rec["ticker"] == "AAA.NS"
        assert rec["kw"] == {"period": "5d", "interval": "1d", "auto_adjust": True}   # max(5, days)

    def test_fallback_period_uses_days_when_large(self, hx, monkeypatch):
        hx.route("/history/AAA", FakeResp(503))
        rec = {}
        monkeypatch.setitem(sys.modules, "yfinance", _yf_module(pd.DataFrame({"Close": [1.0]}), record=rec))
        monkeypatch.setitem(sys.modules, "symbol_aliases", _aliases_module())
        ipo._fetch_history("AAA", 40)
        assert rec["kw"]["period"] == "40d"

    def test_fallback_unresolvable_symbol(self, hx, monkeypatch):
        hx.route("/history/AAA", FakeResp(503))
        monkeypatch.setitem(sys.modules, "yfinance", _yf_module(pd.DataFrame({"Close": [1.0]})))
        monkeypatch.setitem(sys.modules, "symbol_aliases", _aliases_module(lambda s: None))
        assert ipo._fetch_history("AAA", 3) is None

    @pytest.mark.parametrize("hist", [None, pd.DataFrame()])
    def test_fallback_empty(self, hx, monkeypatch, hist):
        hx.route("/history/AAA", FakeResp(503))
        monkeypatch.setitem(sys.modules, "yfinance", _yf_module(hist))
        monkeypatch.setitem(sys.modules, "symbol_aliases", _aliases_module())
        assert ipo._fetch_history("AAA", 3) is None

    def test_fallback_exception(self, hx, monkeypatch):
        hx.route("/history/AAA", FakeResp(503))
        monkeypatch.setitem(sys.modules, "yfinance", _yf_module(raises=RuntimeError("yahoo")))
        monkeypatch.setitem(sys.modules, "symbol_aliases", _aliases_module())
        assert ipo._fetch_history("AAA", 3) is None

    def test_yfinance_not_installed(self, hx, monkeypatch):
        hx.route("/history/AAA", FakeResp(503))
        monkeypatch.setitem(sys.modules, "yfinance", None)
        assert ipo._fetch_history("AAA", 3) is None


class TestQuoteNow:
    def test_price_key_precedence(self, hx):
        hx.route("/quote/A", FakeResp(200, {"price": 0, "regularMarketPrice": "12.5"}))
        assert ipo._quote_now("A") == 12.5
        assert hx.calls[0][2]["timeout"] == 8

    @pytest.mark.parametrize("payload,expected", [
        ({"close": 3}, 3.0), ({"last": 4}, 4.0), ({"price": 0}, None), ({}, None)])
    def test_other_keys(self, hx, payload, expected):
        hx.route("/quote/A", FakeResp(200, payload))
        assert ipo._quote_now("A") == expected

    def test_non_200_and_exception(self, hx):
        hx.route("/quote/A", FakeResp(500))
        assert ipo._quote_now("A") is None
        hx.routes.clear()
        hx.route("/quote/A", RuntimeError("down"))
        assert ipo._quote_now("A") is None


class TestFetchGmp:
    def _page(self, body):
        return FakeResp(200, text=f"<html><body>{body}</body></html>")

    def test_non_200(self, hx):
        hx.route("investorgain", FakeResp(500))
        assert ipo._fetch_gmp("ABCD", "ABCD Ltd", 100) is None

    def test_found_by_company_name(self, hx):
        hx.route("investorgain", self._page("<td>Foo Industries Limited</td><td>GMP: ₹12.5</td>"))
        assert ipo._fetch_gmp("FOO", "Foo Industries Limited", 100) == 12.5
        url, params, kw = hx.calls[0]
        assert params == {"company": "Foo Industries"}          # symbol too short -> first two name words
        assert kw["follow_redirects"] is True and kw["timeout"] == 8

    def test_symbol_search_term_and_suffix_stripping(self, hx):
        hx.route("investorgain", self._page("ABCD row GMP - -5"))
        assert ipo._fetch_gmp(" abcd.ns ", "", 100) == -5.0
        assert hx.calls[0][1] == {"company": "ABCD"}

    def test_symbol_used_as_fallback_anchor(self, hx):
        hx.route("investorgain", self._page("<td>ABCD</td> GMP +7"))
        assert ipo._fetch_gmp("ABCD", "Totally Different Name Pvt", 100) == 7.0

    def test_company_not_on_page(self, hx):
        hx.route("investorgain", self._page("<td>Other Co</td> GMP 9"))
        assert ipo._fetch_gmp("ABCD", "ABCD Ltd", 100) is None

    def test_no_gmp_in_window(self, hx):
        hx.route("investorgain", self._page("<td>ABCD Ltd</td> nothing useful here"))
        assert ipo._fetch_gmp("ABCD", "ABCD Ltd", 100) is None

    def test_gmp_far_outside_window_ignored(self, hx):
        filler = "x" * 700
        hx.route("investorgain", self._page(f"ABCD Ltd {filler} GMP: 5"))
        assert ipo._fetch_gmp("ABCD", "ABCD Ltd", 100) is None

    def test_sanity_gate(self, hx):
        hx.route("investorgain", self._page("ABCD Ltd GMP: 500"))
        assert ipo._fetch_gmp("ABCD", "ABCD Ltd", 100) is None          # > 3x issue price
        hx.calls.clear()
        assert ipo._fetch_gmp("ABCD", "ABCD Ltd", 0.0) == 500.0         # no issue price -> gate skipped
        assert ipo._fetch_gmp("ABCD", "ABCD Ltd", 200) == 500.0         # within 3x

    def test_search_term_truncated_to_40(self, hx):
        hx.route("investorgain", FakeResp(500))
        ipo._fetch_gmp("AB", "A" * 30 + " " + "B" * 30, 10)
        assert len(hx.calls[0][1]["company"]) <= 40

    def test_exception_returns_none(self, hx):
        hx.route("investorgain", RuntimeError("down"))
        assert ipo._fetch_gmp("ABCD", "x", 1) is None

    def test_none_symbol(self, hx):
        hx.route("investorgain", FakeResp(500))
        assert ipo._fetch_gmp(None) is None


class TestFetchFundamentals:
    def test_nested_metrics_filtered(self, hx):
        hx.route("/analyze/A", FakeResp(200, {"metrics": {"revenue": 10, "eps": 0, "roce": None, "junk": 1}}))
        assert ipo._fetch_ipo_fundamentals("A") == {"revenue": 10, "eps": 0}
        assert hx.calls[0][2]["timeout"] == 15

    def test_flat_payload(self, hx):
        hx.route("/analyze/A", FakeResp(200, {"pe_ratio": 12, "other": 1}))
        assert ipo._fetch_ipo_fundamentals("A") == {"pe_ratio": 12}

    def test_metrics_not_dict_uses_payload(self, hx):
        hx.route("/analyze/A", FakeResp(200, {"metrics": "n/a", "roce": 9}))
        assert ipo._fetch_ipo_fundamentals("A") == {"roce": 9}

    def test_empty_snapshot_is_none(self, hx):
        hx.route("/analyze/A", FakeResp(200, {"metrics": {}}))
        assert ipo._fetch_ipo_fundamentals("A") is None
        hx.routes.clear()
        hx.route("/analyze/A", FakeResp(200, None))
        assert ipo._fetch_ipo_fundamentals("A") is None

    def test_non_200_exception_and_bad_shape(self, hx):
        hx.route("/analyze/A", FakeResp(404))
        assert ipo._fetch_ipo_fundamentals("A") is None
        hx.routes.clear()
        hx.route("/analyze/A", RuntimeError("down"))
        assert ipo._fetch_ipo_fundamentals("A") is None
        hx.routes.clear()
        hx.route("/analyze/A", FakeResp(200, [1, 2]))     # list payload -> .get raises -> swallowed
        assert ipo._fetch_ipo_fundamentals("A") is None

    def test_non_dict_json_object_guard(self, hx):
        """Defensive `isinstance(metrics, dict)` guard. Real JSON can't reach it (a non-dict payload
        already blows up on `.get` and is swallowed above), so use an object that has `.get`."""
        class Weird:
            def get(self, k, d=None):
                return None

            def __bool__(self):
                return True
        hx.route("/analyze/A", FakeResp(200, Weird()))
        assert ipo._fetch_ipo_fundamentals("A") is None

    def test_rate_limiter_failure_ignored(self, hx, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("rl")
        monkeypatch.setitem(sys.modules, "rate_limiter", types.SimpleNamespace(acquire=boom))
        hx.route("/analyze/A", FakeResp(200, {"eps": 1}))
        assert ipo._fetch_ipo_fundamentals("A") == {"eps": 1}


# ── decisions and suggestions ─────────────────────────────────────────────────

@pytest.fixture
def bars(monkeypatch):
    """Pin the decision bars so the tests don't depend on IPO_*_BAR env overrides."""
    monkeypatch.setattr(ipo, "BUY_NOW_BAR", 70.0)
    monkeypatch.setattr(ipo, "PREPARE_BAR", 58.0)
    monkeypatch.setattr(ipo, "DO_NOT_BUY_BAR", 40.0)
    monkeypatch.setattr(ipo, "FRESH_WINDOW_DAYS", 30)


class TestDecision:
    @pytest.mark.parametrize("score,expected", [
        (100, "BUY NOW"), (70, "BUY NOW"), (69.9, "PREPARE TO BUY"), (58, "PREPARE TO BUY"),
        (57.9, "HOLD"), (40, "HOLD"), (39.9, "DO NOT BUY"), (0, "DO NOT BUY")])
    def test_bars(self, bars, score, expected):
        assert ipo._decision_for_score(score) == expected

    def test_sell_needs_deep_break_and_weak_score(self, bars):
        assert ipo._decision_for_score(30, current_vs_issue_pct=-16) == "SELL"
        assert ipo._decision_for_score(30, current_vs_issue_pct=-15) == "DO NOT BUY"     # not below -15
        assert ipo._decision_for_score(45, current_vs_issue_pct=-30) == "HOLD"           # score not weak enough
        assert ipo._decision_for_score(30, current_vs_issue_pct=None) == "DO NOT BUY"


class TestBuildSuggestion:
    def _s(self, **kw):
        args = dict(symbol="AAA", company_name="AAA Ltd", current_price=100.0, decision="BUY NOW",
                    score=80.0, atr_pct=0.05, rationale="why")
        args.update(kw)
        return ipo._build_ipo_suggestion(**args)

    @pytest.mark.parametrize("decision", ["HOLD", "DO NOT BUY", "SELL", ""])
    def test_non_actionable_is_none(self, bars, decision):
        assert self._s(decision=decision) is None

    def test_buy_now_shape_and_numbers(self, bars):
        s = self._s()
        # conviction_mult = 0.4 + (80-58)/42*1.1 = 0.97619 ; move = 5*2.2*0.97619 = 10.738%
        assert s["action"] == "BUY NOW" and s["entry_time"] == "Now" and s["decision"] == "BUY NOW"
        assert s["buy_price_low"] == 99.5 and s["buy_price_high"] == 101.0
        assert s["buy_price_range"] == "₹99.5 - ₹101.0"
        assert s["estimated_profit_pct"] == 10.7 and s["target_price"] == 110.74
        assert s["estimated_profit"] == "+10.7% (₹10.74/share)"
        assert s["stop_loss"] == 92.5           # max(4%, 1.5*5%) = 7.5% below
        assert s["conviction_score"] == 80 and s["price"] == 100.0 and s["sector"] is None
        assert s["holding_duration"] == s["holding_period"] == "2 to 7 Trading Days"
        assert s["rationale"] == "why" and s["symbol"] == "AAA"

    def test_prepare_action_and_entry_time(self, bars):
        s = self._s(decision="PREPARE TO BUY", score=60.0)
        assert s["action"] == "BUY ON 15M BREAKOUT"
        assert s["entry_time"] == "Next Trading Session (09:25 AM - 09:45 AM)"

    def test_expected_move_clamped(self, bars):
        assert self._s(atr_pct=0.001)["estimated_profit_pct"] == 3.0       # floor
        assert self._s(atr_pct=0.5, score=100.0)["estimated_profit_pct"] == 25.0   # ceiling

    def test_atr_stop_floor_is_four_percent(self, bars):
        assert self._s(atr_pct=0.01)["stop_loss"] == 96.0

    def test_structural_stop_used_when_tighter(self, bars):
        # atr stop 92.5 ; listing-day low 95 -> 94.05 is tighter (higher)
        assert self._s(listing_day_low=95.0)["stop_loss"] == 94.05

    def test_structural_stop_ignored_when_looser_or_invalid(self, bars):
        assert self._s(listing_day_low=80.0)["stop_loss"] == 92.5     # 79.2 < atr stop
        assert self._s(listing_day_low=100.0)["stop_loss"] == 92.5    # not below current price
        assert self._s(listing_day_low=120.0)["stop_loss"] == 92.5
        assert self._s(listing_day_low=None)["stop_loss"] == 92.5
        assert self._s(listing_day_low=0.0)["stop_loss"] == 92.5


# ── analyze_ipo ───────────────────────────────────────────────────────────────

NOW = datetime(2026, 9, 30, 11, 0, tzinfo=ipo.IST)


def _hist(closes, highs=None, lows=None, vols=None):
    n = len(closes)
    return pd.DataFrame({
        "Close": closes,
        "High": highs if highs is not None else [c * 1.02 for c in closes],
        "Low": lows if lows is not None else [c * 0.98 for c in closes],
        "Volume": vols if vols is not None else [1000.0] * n,
    }, index=pd.date_range("2026-09-01", periods=n))


@pytest.fixture
def stubs(monkeypatch, bars):
    """Stub every upstream call analyze_ipo makes; tests override attributes on the returned dict."""
    s = {"gmp": None, "quote": None, "hist": None, "fund": None, "gmp_calls": [], "hist_calls": []}

    def gmp(sym, name="", price=0.0):
        s["gmp_calls"].append((sym, name, price))
        return s["gmp"]

    def history(sym, days):
        s["hist_calls"].append((sym, days))
        return s["hist"]
    monkeypatch.setattr(ipo, "_fetch_gmp", gmp)
    monkeypatch.setattr(ipo, "_quote_now", lambda sym: s["quote"])
    monkeypatch.setattr(ipo, "_fetch_history", history)
    monkeypatch.setattr(ipo, "_fetch_ipo_fundamentals", lambda sym: s["fund"])
    return s


def _entry(listing="2026-09-20", **kw):
    return {"symbol": "AAA", "issue_price": 100, "listing_date": listing, "company_name": "AAA Ltd", **kw}


class TestAnalyzeIpoEarlyStages:
    def test_unparseable_listing_date(self, stubs):
        r = ipo.analyze_ipo(_entry(listing="soon"), NOW)
        assert r["stage"] == "unknown" and r["error"] == "Could not parse listing_date"
        assert "days_since_listing" not in r
        assert stubs["gmp_calls"] == []       # _days_approx = 999 -> GMP scrape not attempted

    def test_unparseable_date_still_derives_gmp_pct_from_entry(self, stubs):
        r = ipo.analyze_ipo(_entry(listing=None, gmp=10), NOW)
        assert r["gmp_pct_of_issue"] == 10.0 and r["gmp_implied_listing"] == 110.0

    def test_default_now_and_defaults(self, stubs):
        far = (datetime.now(ipo.IST) + timedelta(days=30)).strftime("%Y-%m-%d")
        e = {"symbol": "AAA", "issue_price": "100", "listing_date": far}
        r = ipo.analyze_ipo(e)
        assert r["stage"] == "upcoming" and r["company_name"] == "AAA" and r["source"] == "manual"
        assert r["issue_price"] == 100.0 and r["days_since_listing"] < 0

    def test_upcoming_scrapes_gmp(self, stubs):
        stubs["gmp"] = 12.5
        r = ipo.analyze_ipo(_entry(listing="2026-10-05"), NOW)
        assert r["stage"] == "upcoming" and r["message"] == "Lists on 2026-10-05 — not yet tradable."
        assert r["gmp"] == 12.5 and r["gmp_pct_of_issue"] == 12.5 and r["gmp_implied_listing"] == 112.5
        assert stubs["gmp_calls"] == [("AAA", "AAA Ltd", 100.0)]

    def test_upcoming_scrape_miss_leaves_gmp_none(self, stubs):
        r = ipo.analyze_ipo(_entry(listing="2026-10-05"), NOW)
        assert r["gmp"] is None and "gmp_pct_of_issue" not in r

    def test_provided_gmp_not_rescraped(self, stubs):
        ipo.analyze_ipo(_entry(listing="2026-10-05", gmp=3), NOW)
        assert stubs["gmp_calls"] == []

    def test_old_listing_never_scrapes_gmp(self, stubs):
        stubs["hist"] = None
        ipo.analyze_ipo(_entry(listing="2026-09-01"), NOW)     # 29 days ago
        assert stubs["gmp_calls"] == []

    def test_zero_issue_price_skips_gmp_pct(self, stubs):
        r = ipo.analyze_ipo(_entry(listing="2026-10-05", issue_price=0, gmp=5), NOW)
        assert "gmp_pct_of_issue" not in r


class TestAnalyzeListingDay:
    LISTING = "2026-09-30"

    def test_pre_listing_no_signals(self, stubs):
        r = ipo.analyze_ipo(_entry(self.LISTING), NOW)
        assert r["stage"] == "pre_listing" and r["days_since_listing"] == 0
        assert r["message"].startswith("Lists today")
        assert "pre_listing_advisory" not in r and "ipo_score" not in r

    def test_pre_listing_subscription_only_strong(self, stubs):
        r = ipo.analyze_ipo(_entry(self.LISTING, subscription_times=80), NOW)
        assert r["pre_listing_advisory_score"] == 100.0
        assert r["pre_listing_advisory"].startswith("Strong subscription/GMP")

    def test_pre_listing_subscription_only_weak(self, stubs):
        r = ipo.analyze_ipo(_entry(self.LISTING, subscription_times=2), NOW)
        assert r["pre_listing_advisory_score"] == 52.0
        assert r["pre_listing_advisory"].startswith("Moderate/weak")

    def test_pre_listing_uses_scraped_gmp_and_averages_with_subscription(self, stubs):
        stubs["gmp"] = 20.0                    # +20% -> gmp score 60
        r = ipo.analyze_ipo(_entry(self.LISTING, subscription_times=30), NOW)   # sub score 80
        assert r["gmp"] == 20.0 and r["pre_listing_advisory_score"] == 70.0
        assert r["pre_listing_advisory"].startswith("Strong")

    def test_pre_listing_gmp_extremes_are_capped(self, stubs):
        r = ipo.analyze_ipo(_entry(self.LISTING, gmp=-500), NOW)      # pct clamped to -50 -> 25
        assert r["pre_listing_advisory_score"] == 25.0
        r = ipo.analyze_ipo(_entry(self.LISTING, gmp=900), NOW)       # pct clamped to 100 -> 100
        assert r["pre_listing_advisory_score"] == 100.0

    def test_pre_listing_zero_gmp_is_neutral(self, stubs):
        r = ipo.analyze_ipo(_entry(self.LISTING, gmp=0), NOW)
        assert r["pre_listing_advisory_score"] == 50.0

    def test_pre_listing_zero_issue_price_ignores_gmp(self, stubs):
        r = ipo.analyze_ipo(_entry(self.LISTING, issue_price=0, gmp=5), NOW)
        assert "pre_listing_advisory_score" not in r

    def test_listing_day_live_buy_now(self, stubs):
        stubs["quote"] = 130.0                 # +30% pop -> strength 62
        r = ipo.analyze_ipo(_entry(self.LISTING, subscription_times=50), NOW)
        assert r["stage"] == "listing_day" and r["current_price"] == 130.0 and r["listing_pop_pct"] == 30.0
        # 0.6*62 + 0.4*100 = 77.2
        assert r["ipo_score"] == 77.2 and r["score_breakdown"] == {"listing_strength": 77.2}
        assert r["decision"] == "BUY NOW"
        bs = r["buy_suggestion"]
        assert bs["action"] == "BUY NOW" and bs["price"] == 130.0
        assert bs["rationale"] == "Listing day, pop +30.0% vs issue price ₹100.0."

    def test_listing_day_without_subscription_uses_pop_only(self, stubs):
        stubs["quote"] = 100.0
        r = ipo.analyze_ipo(_entry(self.LISTING), NOW)
        assert r["ipo_score"] == 50.0 and r["decision"] == "HOLD" and r["buy_suggestion"] is None

    def test_listing_day_pop_extremes_and_never_sells(self, stubs):
        stubs["quote"] = 40.0                  # -60% clamps to -50 -> 50 - 20 = 30
        r = ipo.analyze_ipo(_entry(self.LISTING), NOW)
        assert r["ipo_score"] == 30.0 and r["decision"] == "DO NOT BUY"     # SELL is never returned on day 1
        stubs["quote"] = 1000.0                # +900% clamps to +150 -> 100
        assert ipo.analyze_ipo(_entry(self.LISTING), NOW)["ipo_score"] == 100.0


class TestAnalyzePostListing:
    def test_no_history_is_no_data_yet(self, stubs):
        for h in (None, pd.DataFrame()):
            stubs["hist"] = h
            r = ipo.analyze_ipo(_entry("2026-09-25"), NOW)
            assert r["stage"] == "no_data_yet" and "No price history from Yahoo Finance yet" in r["message"]
            assert "ipo_score" not in r and "decision" not in r

    def test_history_window_is_days_plus_two_capped(self, stubs, monkeypatch):
        stubs["hist"] = None
        ipo.analyze_ipo(_entry("2026-09-25"), NOW)
        assert stubs["hist_calls"][-1] == ("AAA", 7)
        monkeypatch.setattr(ipo, "LOOKBACK_DAYS_MAX", 4)
        ipo.analyze_ipo(_entry("2026-09-25"), NOW)
        assert stubs["hist_calls"][-1] == ("AAA", 4)

    def test_golden_strong_setup(self, stubs):
        stubs["hist"] = _hist([120, 125, 130, 128, 135, 140, 138, 142],
                              vols=[1000, 1100, 1200, 1500, 1800, 2000, 2200, 2500])
        r = ipo.analyze_ipo(_entry("2026-09-20"), NOW)
        assert r["stage"] == "listed" and r["days_since_listing"] == 10
        assert r["current_price"] == 142.0 and r["listing_day_close"] == 120.0
        assert r["post_listing_high"] == 144.84
        assert r["listing_pop_pct"] == 20.0 and r["current_vs_issue_pct"] == 42.0
        assert r["current_vs_high_pct"] == -1.96 and r["momentum_5d_pct"] == 10.94
        assert r["volume_trend_ratio"] == 2.24 and r["atr_pct"] == 3.73
        assert r["score_breakdown"] == {"momentum": 100.0, "pullback_quality": 55.0, "listing_strength": 74.1,
                                        "volume_trend": 99.5, "recency": 91.7}
        assert r["ipo_score"] == 82.7 and r["decision"] == "BUY NOW"
        bs = r["buy_suggestion"]
        assert bs["target_price"] == 154.17 and bs["stop_loss"] == 134.06
        assert bs["buy_price_low"] == 141.29 and bs["buy_price_high"] == 143.42
        assert bs["conviction_score"] == 83
        assert "IPO score 83/100 — 10d since listing, +42.0% vs issue price" in bs["rationale"]
        assert "fundamentals_snapshot" not in r

    def test_rationale_carries_hardcoded_market_note(self, stubs):
        """Pins current behaviour: every rationale ends with a static 'Aug-2026 … Nifty -7% in 6m,
        FII net-short' sentence that never changes with real market data. NOT FIXED."""
        stubs["hist"] = _hist([120, 125, 130, 128, 135, 140, 138, 142])
        r = ipo.analyze_ipo(_entry("2026-09-20"), NOW)
        assert "Market context (Aug-2026): Nifty -7% in 6m, FII net-short" in r["buy_suggestion"]["rationale"]
        assert "BUY_NOW≥70, PREPARE≥58" in r["buy_suggestion"]["rationale"]

    def test_fundamentals_snapshot_attached(self, stubs):
        stubs["hist"] = _hist([120, 125])
        stubs["fund"] = {"eps": 1.5}
        assert ipo.analyze_ipo(_entry("2026-09-25"), NOW)["fundamentals_snapshot"] == {"eps": 1.5}

    def test_broken_listing_is_sell(self, stubs):
        stubs["hist"] = _hist([90, 88, 85, 84, 83], highs=[90, 88, 85, 84, 83], lows=[90, 88, 85, 84, 83])
        r = ipo.analyze_ipo(_entry("2026-09-25"), NOW)
        assert r["current_vs_issue_pct"] == -17.0
        assert r["score_breakdown"]["pullback_quality"] == 13.0      # 30 + (-17)
        assert r["score_breakdown"]["momentum"] == 11.1
        assert r["score_breakdown"]["listing_strength"] == 34.1      # negative Day-1 pop lowers the score
        assert r["score_breakdown"]["volume_trend"] == 50.0 and r["score_breakdown"]["recency"] == 100.0
        assert r["ipo_score"] == 30.9 and r["decision"] == "SELL" and r["buy_suggestion"] is None

    def test_pullback_zones(self, stubs):
        # right at the high (<=2% off) -> 55
        stubs["hist"] = _hist([110, 115, 120, 125, 130], highs=[110, 115, 120, 125, 130])
        assert ipo.analyze_ipo(_entry("2026-09-25"), NOW)["score_breakdown"]["pullback_quality"] == 55.0
        # 2-20% off the high, above issue -> 85
        stubs["hist"] = _hist([120, 130, 140, 150, 140], highs=[125, 135, 145, 155, 145])
        assert ipo.analyze_ipo(_entry("2026-09-25"), NOW)["score_breakdown"]["pullback_quality"] == 85.0
        # faded >20% off the high, still above issue -> 85 - (30-20)*1.5 = 70
        stubs["hist"] = _hist([200, 190, 170, 150, 140], highs=[200, 190, 170, 150, 140])
        assert ipo.analyze_ipo(_entry("2026-09-25"), NOW)["score_breakdown"]["pullback_quality"] == 70.0

    def test_single_candle(self, stubs):
        stubs["hist"] = _hist([120.0], highs=[125.0], lows=[118.0])
        r = ipo.analyze_ipo(_entry("2026-09-29"), NOW)
        assert r["stage"] == "listed" and r["momentum_5d_pct"] == 0.0 and r["volume_trend_ratio"] == 1.0
        assert r["atr_pct"] == round((125.0 - 118.0) / 120.0 * 100, 2)
        assert r["score_breakdown"]["momentum"] == 50.0

    def test_zero_early_volume_keeps_neutral_trend(self, stubs):
        stubs["hist"] = _hist([120, 121, 122, 123, 124, 125], vols=[0, 0, 0, 0, 500, 600])
        r = ipo.analyze_ipo(_entry("2026-09-25"), NOW)
        assert r["volume_trend_ratio"] == 1.0 and r["score_breakdown"]["volume_trend"] == 50.0

    def test_recency_decays_after_five_days(self, stubs):
        stubs["hist"] = _hist([120, 125])
        r = ipo.analyze_ipo(_entry("2026-09-10"), NOW)          # 20 days -> 100 - 15*(50/30)
        assert r["score_breakdown"]["recency"] == 75.0
        r = ipo.analyze_ipo(_entry("2025-06-01"), NOW)          # very old -> clamped to 0
        assert r["score_breakdown"]["recency"] == 0.0

    def test_gmp_scraped_for_fresh_listed_stock(self, stubs):
        stubs["hist"] = _hist([120, 125])
        stubs["gmp"] = 5.0
        r = ipo.analyze_ipo(_entry("2026-09-28"), NOW)
        assert r["stage"] == "listed" and r["gmp"] == 5.0 and r["gmp_pct_of_issue"] == 5.0

    @pytest.mark.parametrize("bad", [0, 0.0, -5, float("nan"), float("inf")])
    def test_non_positive_or_non_finite_issue_price_returns_error_row(self, stubs, bad):
        """issue_price=0 (reachable via add_manual_ipo + repair), negatives, NaN and inf return an
        error row (same shape as an unparseable listing_date) instead of raising ZeroDivisionError,
        and no GMP / history lookup is attempted."""
        stubs["hist"] = _hist([120, 125])
        r = ipo.analyze_ipo(_entry("2026-09-25", issue_price=bad), NOW)
        assert r["stage"] == "unknown" and r["error"] == "issue_price must be a positive number"
        assert "ipo_score" not in r and "decision" not in r and "buy_suggestion" not in r


# ── result cache merge ────────────────────────────────────────────────────────

class TestMergeResults:
    def test_no_cache(self, kv):
        assert ipo._merge_ipo_results([{"symbol": "A"}]) == [{"symbol": "A"}]

    def test_non_dict_cache_and_kv_error(self, kv):
        kv.store[ipo.IPO_LIST_KEY] = ["junk"]
        assert ipo._merge_ipo_results([{"symbol": "A"}]) == [{"symbol": "A"}]
        kv.get_raises = RuntimeError("down")
        assert ipo._merge_ipo_results([{"symbol": "A"}]) == [{"symbol": "A"}]

    def test_new_wins_in_place_and_new_symbols_appended(self, kv):
        kv.store[ipo.IPO_LIST_KEY] = {"results": [
            {"symbol": "A", "v": "old"}, {"symbol": "B", "v": "keep"}, {"v": "no-symbol"}]}
        out = ipo._merge_ipo_results([{"symbol": "C", "v": "new"}, {"symbol": "A", "v": "new"}, {"v": "dropped"}])
        assert out == [{"symbol": "A", "v": "new"}, {"symbol": "B", "v": "keep"}, {"v": "no-symbol"},
                       {"symbol": "C", "v": "new"}]

    def test_cached_dict_without_results(self, kv):
        kv.store[ipo.IPO_LIST_KEY] = {"results": None}
        assert ipo._merge_ipo_results([{"symbol": "A"}]) == [{"symbol": "A"}]

    def test_duplicate_symbol_in_cache_is_collapsed_to_the_fresh_row(self, kv):
        """A symbol held twice in the cached list is refreshed in place once and the stale second
        copy is dropped."""
        kv.store[ipo.IPO_LIST_KEY] = {"results": [{"symbol": "A", "v": 1}, {"symbol": "B", "v": "keep"},
                                                  {"symbol": "A", "v": 2}]}
        out = ipo._merge_ipo_results([{"symbol": "A", "v": 9}])
        assert out == [{"symbol": "A", "v": 9}, {"symbol": "B", "v": "keep"}]

    def test_duplicate_cached_symbol_without_fresh_row_is_left_alone(self, kv):
        kv.store[ipo.IPO_LIST_KEY] = {"results": [{"symbol": "A", "v": 1}, {"symbol": "A", "v": 2}]}
        out = ipo._merge_ipo_results([{"symbol": "B", "v": 9}])
        assert out == [{"symbol": "A", "v": 1}, {"symbol": "A", "v": 2}, {"symbol": "B", "v": 9}]


# ── scan orchestration ────────────────────────────────────────────────────────

class TestRunIpoScanLock:
    def test_second_trigger_is_skipped(self, kv, monkeypatch):
        assert ipo._IPO_SCAN_LOCK.acquire(blocking=False)
        try:
            out = ipo.run_ipo_scan()
        finally:
            ipo._IPO_SCAN_LOCK.release()
        assert out["skipped"] is True and out["reason"] == "scan already running"

    def test_delegates_and_releases(self, monkeypatch):
        seen = {}

        def locked(force=False, wipe=False):
            seen["args"] = (force, wipe)
            assert ipo._IPO_SCAN_LOCK.locked()
            return {"status": "done"}
        monkeypatch.setattr(ipo, "_run_ipo_scan_locked", locked)
        assert ipo.run_ipo_scan(force=True, wipe=True) == {"status": "done"}
        assert seen["args"] == (True, True) and not ipo._IPO_SCAN_LOCK.locked()

    def test_releases_when_scan_raises(self, monkeypatch):
        def locked(force=False, wipe=False):
            raise RuntimeError("boom")
        monkeypatch.setattr(ipo, "_run_ipo_scan_locked", locked)
        with pytest.raises(RuntimeError):
            ipo.run_ipo_scan()
        assert not ipo._IPO_SCAN_LOCK.locked()


class ScanHarness:
    """Stubs everything _run_ipo_scan_locked touches outside kv."""

    def __init__(self, monkeypatch):
        self.universe = []
        self.diag = {"nse_candidates": 1, "ipoalerts_candidates": 0, "ipoalerts_configured": False,
                     "manual_candidates": 0}
        self.age = None
        self.analyzed = []
        self.analyze_raises = {}
        self.upserts = []
        self.upsert_raises = None
        self.wipes = 0
        self.wipe_ok = True
        self.on_analyze = None
        monkeypatch.setattr(ipo, "_ipo_db_freshness_hours", lambda: self.age)
        monkeypatch.setattr(ipo, "_merged_ipo_universe", lambda: (list(self.universe), dict(self.diag)))
        monkeypatch.setattr(ipo, "analyze_ipo", self._analyze)
        monkeypatch.setattr(ipo, "_ipo_db_upsert", self._upsert)
        monkeypatch.setattr(ipo, "_ipo_db_wipe", self._wipe)

    def _analyze(self, entry):
        self.analyzed.append(entry["symbol"])
        if entry["symbol"] in self.analyze_raises:
            raise self.analyze_raises[entry["symbol"]]
        if self.on_analyze:
            self.on_analyze(entry)
        return {"symbol": entry["symbol"], "stage": entry.get("stage_out", "listed"),
                "ipo_score": entry.get("score"), "pre_listing_advisory_score": entry.get("adv")}

    def _upsert(self, rows):
        if self.upsert_raises is not None:
            raise self.upsert_raises
        self.upserts.append([r["symbol"] for r in rows])
        return len(rows)

    def _wipe(self):
        self.wipes += 1
        return self.wipe_ok


@pytest.fixture
def scan(monkeypatch, kv, db):
    return ScanHarness(monkeypatch)


class TestRunScanFreshness:
    def test_schema_failures_are_non_fatal(self, scan, db, monkeypatch):
        db.ensure_raises = RuntimeError("ddl")
        out = ipo._run_ipo_scan_locked()
        assert out["status"] == "done"          # 0 candidates path, but it got that far
        monkeypatch.setitem(sys.modules, "ipo_schema", None)
        assert ipo._run_ipo_scan_locked()["status"] == "done"

    def test_fresh_table_reuses_cache(self, scan, kv):
        scan.age = 1.5
        kv.store[ipo.IPO_LIST_KEY] = {"results": [{"symbol": "A"}, {"symbol": "B"}]}
        scan.universe = [{"symbol": "X"}]
        out = ipo._run_ipo_scan_locked()
        assert out["status"] == "skipped_fresh" and out["processed"] == 2 and out["total"] == 2
        assert "1.5h old (< 24h)" in out["message"] and scan.analyzed == []

    @pytest.mark.parametrize("age,cache", [
        (None, {"results": [{"symbol": "A"}]}),        # table empty/unavailable
        (30.0, {"results": [{"symbol": "A"}]}),        # older than the freshness window
        (1.0, {"results": []}),                        # fresh table but nothing cached to return
        (1.0, "junk"),
        (1.0, None),
    ])
    def test_falls_through_to_real_scan(self, scan, kv, age, cache):
        scan.age = age
        if cache is not None:
            kv.store[ipo.IPO_LIST_KEY] = cache
        scan.universe = [{"symbol": "X", "score": 50}]
        assert ipo._run_ipo_scan_locked()["status"] == "done" and scan.analyzed == ["X"]

    def test_cache_read_error_falls_through(self, scan, kv):
        scan.age = 1.0
        kv.get_raises_for[ipo.IPO_LIST_KEY] = RuntimeError("down")
        scan.universe = [{"symbol": "X"}]
        assert ipo._run_ipo_scan_locked()["status"] == "done" and scan.analyzed == ["X"]

    def test_force_and_wipe_bypass_freshness(self, scan, kv):
        scan.age = 1.0
        kv.store[ipo.IPO_LIST_KEY] = {"results": [{"symbol": "A"}]}
        scan.universe = [{"symbol": "X"}]
        ipo._run_ipo_scan_locked(force=True)
        assert scan.analyzed == ["X"]
        scan.analyzed.clear()
        ipo._run_ipo_scan_locked(wipe=True)
        assert scan.analyzed == ["X"]


class TestRunScanEmptyUniverse:
    def _run(self, scan, **diag):
        scan.diag.update(diag)
        return ipo._run_ipo_scan_locked()

    def test_all_reasons(self, scan):
        out = self._run(scan, nse_candidates=0, ipoalerts_configured=False, manual_candidates=0)
        assert out["status"] == "done" and out["total"] == 0 and out["results_count"] == 0
        assert out["message"] == ("0 IPOs found — NSE returned 0 (likely IP-blocked from this deployment); "
                                  "IPOALERTS_API_KEY not set; no manual entries. Use '+ Add IPO' to add one by name.")
        assert out["diagnostics"]["nse_candidates"] == 0

    def test_ipoalerts_configured_but_empty(self, scan):
        out = self._run(scan, ipoalerts_configured=True, ipoalerts_candidates=0, manual_candidates=2)
        assert "ipoalerts returned 0 (quota exhausted or no very-recent listings)" in out["message"]
        assert "no manual entries" not in out["message"] and "IPOALERTS_API_KEY not set" not in out["message"]

    def test_candidates_but_none_scoreable(self, scan):
        out = self._run(scan, ipoalerts_configured=True, ipoalerts_candidates=3, manual_candidates=1)
        assert "all sources returned candidates, but none had both a listing date and issue price" in out["message"]


class TestRunScanFull:
    def test_analyzes_sorts_persists(self, scan, kv):
        scan.universe = [
            {"symbol": "LISTED_LOW", "score": 40},
            {"symbol": "LISTED_HIGH", "score": 90},
            {"symbol": "UPC", "stage_out": "upcoming"},
            {"symbol": "PRE", "stage_out": "pre_listing", "adv": 60},
            {"symbol": "DAY1", "stage_out": "listing_day", "score": 75},
            {"symbol": "ODD", "stage_out": "weird", "score": 99},
            {"symbol": "ERR"},
        ]
        scan.analyze_raises["ERR"] = RuntimeError("bad candle")
        kv.store[ipo.IPO_LIST_KEY] = {"results": [{"symbol": "OLD", "stage": "listed"}, {"symbol": "UPC", "v": "stale"}]}
        out = ipo._run_ipo_scan_locked()
        assert out["status"] == "done" and out["results_count"] == 6 and out["processed"] == 7 and out["total"] == 7
        assert out["message"] == "Done: 6 analyzed, 1 errors"
        saved = kv.store[ipo.IPO_LIST_KEY]["results"]
        # merge (not overwrite): OLD survives, UPC refreshed in place, sorted new rows appended after
        assert [r["symbol"] for r in saved] == ["OLD", "UPC", "PRE", "DAY1", "LISTED_HIGH", "LISTED_LOW", "ODD"]
        assert saved[1].get("v") is None
        assert kv.store[ipo.IPO_LIST_KEY]["generated_at"]
        key, val, ttl = [s for s in kv.sets if s[0] == ipo.IPO_LIST_KEY][-1]
        assert ttl == ipo.IPO_LIST_CACHE_TTL_SEC
        assert scan.upserts == [["PRE", "DAY1", "UPC", "LISTED_HIGH", "LISTED_LOW", "ODD"]]

    def test_progress_updates_per_symbol(self, scan, kv):
        scan.universe = [{"symbol": "A"}, {"symbol": "B"}]
        ipo._run_ipo_scan_locked()
        msgs = [v["message"] for k, v, _ in kv.sets if k == ipo.IPO_JOB_KEY]
        assert "Scanning 2 IPO(s)…" in msgs and "1/2 · A" in msgs and "2/2 · B" in msgs
        assert ipo._LOCAL_JOB["status"] == "done"

    def test_persist_failures_are_non_fatal(self, scan, kv, caplog):
        scan.universe = [{"symbol": "A"}]
        kv.set_raises_for[ipo.IPO_LIST_KEY] = RuntimeError("kv down")
        scan.upsert_raises = RuntimeError("db down")
        with caplog.at_level("WARNING", logger="ipo-scanner"):
            out = ipo._run_ipo_scan_locked()
        assert out["status"] == "done" and out["results_count"] == 1
        assert "could not persist ipo list" in caplog.text and "ipo db persist failed" in caplog.text


class TestRunScanStop:
    def test_stop_persists_partial_results(self, scan, kv):
        scan.universe = [{"symbol": "A"}, {"symbol": "B"}, {"symbol": "C"}]
        scan.on_analyze = lambda entry: ipo.request_ipo_stop() if entry["symbol"] == "B" else None
        kv.store[ipo.IPO_LIST_KEY] = {"results": [{"symbol": "OLD"}]}
        out = ipo._run_ipo_scan_locked()
        assert out["status"] == "stopped" and out["results_count"] == 2 and out["processed"] == 2
        assert out["message"] == "Stopped at 2/3 — 0 errors before stop"
        assert scan.analyzed == ["A", "B"] and scan.upserts == []
        assert [r["symbol"] for r in kv.store[ipo.IPO_LIST_KEY]["results"]] == ["OLD", "A", "B"]
        assert ipo.ipo_stop_requested() is False        # flag cleared for the next run

    def test_stop_with_kv_failure(self, scan, kv, caplog):
        scan.universe = [{"symbol": "A"}, {"symbol": "B"}]
        scan.on_analyze = lambda entry: ipo.request_ipo_stop()
        kv.set_raises_for[ipo.IPO_LIST_KEY] = RuntimeError("down")
        with caplog.at_level("WARNING", logger="ipo-scanner"):
            out = ipo._run_ipo_scan_locked()
        assert out["status"] == "stopped" and "could not persist partial ipo list" in caplog.text

    def test_stale_stop_flag_is_cleared_at_start(self, scan):
        ipo.request_ipo_stop()
        scan.universe = [{"symbol": "A"}]
        assert ipo._run_ipo_scan_locked()["status"] == "done" and scan.analyzed == ["A"]


class TestRunScanWipe:
    def test_wipe_replaces_cache_and_wipes_db(self, scan, kv):
        scan.universe = [{"symbol": "NEW", "stage_out": "upcoming"}]
        kv.store[ipo.IPO_LIST_KEY] = {"results": [{"symbol": "OLD", "stage": "listed"}]}    # no old upcoming
        out = ipo._run_ipo_scan_locked(wipe=True)
        assert [r["symbol"] for r in kv.store[ipo.IPO_LIST_KEY]["results"]] == ["NEW"]     # replaced, not merged
        assert scan.wipes == 1 and scan.upserts == [["NEW"]]
        assert out["message"] == "Done: 1 analyzed, 0 errors — ipo_static_feed wiped and refed fresh"

    def test_wipe_failure_is_logged(self, scan, kv, caplog):
        scan.universe = [{"symbol": "NEW", "stage": "upcoming"}]
        scan.wipe_ok = False
        with caplog.at_level("WARNING", logger="ipo-scanner"):
            out = ipo._run_ipo_scan_locked(wipe=True)
        assert "DB wipe failed or unavailable" in caplog.text and out["status"] == "done"

    def test_wipe_downgraded_when_upcoming_vanishes(self, scan, kv):
        scan.universe = [{"symbol": "NEW", "stage": "listed"}]           # 0 upcoming in this fetch
        kv.store[ipo.IPO_LIST_KEY] = {"results": [{"symbol": "UPC1", "stage": "upcoming"},
                                                  {"symbol": "UPC2", "stage": "upcoming"}]}
        out = ipo._run_ipo_scan_locked(wipe=True)
        assert scan.wipes == 0
        assert "wipe skipped: this fetch found 0 upcoming-stage IPOs but 2 were already known" in out["message"]
        assert [r["symbol"] for r in kv.store[ipo.IPO_LIST_KEY]["results"]] == ["UPC1", "UPC2", "NEW"]  # merged

    def test_wipe_proceeds_when_cache_unreadable(self, scan, kv):
        scan.universe = [{"symbol": "NEW", "stage": "listed"}]
        kv.get_raises_for[ipo.IPO_LIST_KEY] = RuntimeError("down")
        ipo._run_ipo_scan_locked(wipe=True)
        assert scan.wipes == 1

    def test_wipe_proceeds_when_cache_not_a_dict(self, scan, kv):
        scan.universe = [{"symbol": "NEW", "stage": "listed"}]
        kv.store[ipo.IPO_LIST_KEY] = ["junk"]
        ipo._run_ipo_scan_locked(wipe=True)
        assert scan.wipes == 1

    def test_wipe_with_results_none_in_cache(self, scan, kv):
        scan.universe = [{"symbol": "NEW", "stage": "listed"}]
        kv.store[ipo.IPO_LIST_KEY] = {"results": None}
        ipo._run_ipo_scan_locked(wipe=True)
        assert scan.wipes == 1


# ── repair batch ──────────────────────────────────────────────────────────────

class RepairHarness:
    def __init__(self, monkeypatch):
        self.audit = {"ok": True, "missing_ipos": [], "pre_listing_ipos": []}
        self.market_open = False
        self.universe = []
        self.diag = {"nse_candidates": 1, "ipoalerts_candidates": 0}
        self.manual = []
        self.analysis = {}          # symbol -> dict result | Exception
        self.analyzed = []
        self.upserts = []
        self.upsert_raises = None
        self.sleeps = []
        monkeypatch.setattr(ipo, "get_ipo_feed_audit", lambda: self.audit)
        monkeypatch.setitem(sys.modules, "surprise_scanner",
                            types.SimpleNamespace(is_market_open_ist=lambda: self.market_open))
        monkeypatch.setattr(ipo, "_merged_ipo_universe", lambda: (list(self.universe), dict(self.diag)))
        monkeypatch.setattr(ipo, "_manual_ipos", lambda: list(self.manual))
        monkeypatch.setattr(ipo, "analyze_ipo", self._analyze)
        monkeypatch.setattr(ipo, "_ipo_db_upsert", self._upsert)
        monkeypatch.setattr(ipo.time, "sleep", lambda s: self.sleeps.append(s))

    def _analyze(self, entry):
        self.analyzed.append(entry)
        res = self.analysis.get(entry["symbol"], {"symbol": entry["symbol"], "ipo_score": 55.0, "decision": "HOLD"})
        if isinstance(res, Exception):
            raise res
        return res

    def _upsert(self, rows):
        if self.upsert_raises is not None:
            raise self.upsert_raises
        self.upserts.append([r["symbol"] for r in rows])
        return len(rows)

    def miss(self, *symbols, stage="listed"):
        self.audit["missing_ipos"] = [{"symbol": s, "stage": stage} for s in symbols]

    def known(self, *symbols):
        self.universe = [{"symbol": s, "issue_price": 10, "listing_date": "2026-09-01"} for s in symbols]


@pytest.fixture
def rep(monkeypatch, kv):
    return RepairHarness(monkeypatch)


class TestRepairEarlyExits:
    def test_audit_failure(self, rep):
        rep.audit = {"ok": False, "error": "engine unavailable"}
        assert ipo.ipo_repair_batch() == {"status": "error", "repaired": [], "attempted": 0,
                                          "error": "engine unavailable"}
        rep.audit = {"ok": False}
        assert ipo.ipo_repair_batch()["error"] == "audit failed"

    def test_nothing_missing(self, rep):
        out = ipo.ipo_repair_batch()
        assert out["status"] == "completed" and out["message"] == "Nothing missing — every tracked IPO is fully scored."
        assert out["attempted"] == 0

    def test_missing_ipos_none_treated_as_empty(self, rep):
        rep.audit = {"ok": True, "missing_ipos": None, "pre_listing_ipos": None}
        assert ipo.ipo_repair_batch()["status"] == "completed"

    def test_symbol_filter(self, rep):
        rep.miss("AAA", "BBB")
        rep.known("AAA", "BBB")
        out = ipo.ipo_repair_batch(symbol=" aaa ")
        assert out["attempted"] == 1 and out["repaired"] == ["AAA"]

    def test_symbol_filter_not_found(self, rep):
        rep.miss("AAA")
        out = ipo.ipo_repair_batch(symbol="ZZZ")
        assert out["status"] == "not_found" and out["message"] == "ZZZ is not currently missing any fields."

    def test_symbol_filter_tolerates_missing_symbol_field(self, rep):
        rep.audit["missing_ipos"] = [{"stage": "listed"}, {"symbol": "AAA", "stage": "listed"}]
        rep.known("AAA")
        assert ipo.ipo_repair_batch(symbol="AAA")["repaired"] == ["AAA"]

    def test_pre_listing_rows_only_folded_in_when_market_open(self, rep):
        rep.miss("AAA")
        rep.audit["pre_listing_ipos"] = [{"symbol": "PRE", "stage": "pre_listing"}]
        rep.known("AAA", "PRE")
        assert ipo.ipo_repair_batch()["attempted"] == 1
        rep.market_open = True
        assert ipo.ipo_repair_batch()["attempted"] == 2

    @pytest.mark.parametrize("limit,expected", [(None, 15), (0, 15), (3, 3), (500, 100), (-5, 1)])
    def test_limit_clamp(self, rep, limit, expected):
        syms = [f"S{i:03d}" for i in range(120)]
        rep.miss(*syms)
        rep.known(*syms)
        assert ipo.ipo_repair_batch(limit=limit)["attempted"] == expected


class TestRepairLookup:
    def test_no_data_yet_rows_are_skipped(self, rep):
        rep.audit["missing_ipos"] = [{"symbol": "YAH", "stage": "no_data_yet"}, {"symbol": "AAA", "stage": None}]
        rep.known("AAA")
        out = ipo.ipo_repair_batch()
        assert out["still_no_data"] == ["YAH"] and out["repaired"] == ["AAA"] and rep.sleeps == [0.3]

    def test_entry_priority_universe_then_manual_then_cache(self, rep, kv):
        rep.miss("U", "M", "C", "C_BAD")
        rep.universe = [{"symbol": "U", "issue_price": 10, "listing_date": "2026-09-01", "src": "universe"}]
        rep.manual = [{"symbol": "U", "src": "manual"}, {"symbol": "M", "src": "manual"}, {"src": "no-symbol"}]
        kv.store[ipo.IPO_LIST_KEY] = {"results": [
            {"symbol": "M", "src": "cache"},
            {"symbol": "C", "issue_price": 5, "listing_date": "2026-09-02", "src": "cache"},
            {"symbol": "C_BAD", "issue_price": None, "listing_date": "2026-09-02"},      # unusable cached row
            {"issue_price": 5, "listing_date": "x"},
        ]}
        out = ipo.ipo_repair_batch()
        assert [e["src"] for e in rep.analyzed] == ["universe", "manual", "cache"]
        assert out["failed"] == [{"symbol": "C_BAD", "reason": "not_found_upstream"}]

    def test_cache_read_failure_and_non_dict_cache(self, rep, kv):
        rep.miss("AAA")
        kv.get_raises_for[ipo.IPO_LIST_KEY] = RuntimeError("down")
        assert ipo.ipo_repair_batch()["failed"][0]["reason"] == "not_found_upstream"
        kv.get_raises_for.clear()
        kv.store[ipo.IPO_LIST_KEY] = ["junk"]
        assert ipo.ipo_repair_batch()["failed"][0]["reason"] == "not_found_upstream"

    def test_failure_reasons(self, rep):
        rep.miss("1150VIES30", "GONE")
        out = ipo.ipo_repair_batch()
        assert out["failed"][0]["symbol"] == "1150VIES30"
        assert out["failed"][0]["reason"].startswith("non_equity_instrument (NCD/bond series")
        assert out["failed"][1] == {"symbol": "GONE", "reason": "not_found_upstream"}
        assert rep.sleeps == []                               # nothing analyzed -> no pacing sleep

    def test_upstream_fetch_empty_reason(self, rep):
        rep.diag = {"nse_candidates": 0, "ipoalerts_candidates": 0}
        rep.miss("GONE")
        reason = ipo.ipo_repair_batch()["failed"][0]["reason"]
        assert reason.startswith("not_found_upstream (NSE+ipoalerts both returned 0 candidates")

    def test_diag_missing_keys_count_as_empty_fetch(self, rep):
        rep.diag = {}
        rep.miss("GONE")
        assert "both returned 0 candidates" in ipo.ipo_repair_batch()["failed"][0]["reason"]


class TestRepairAnalysis:
    def test_scored_vs_unscored_vs_exception(self, rep, caplog):
        rep.miss("OK", "PRE", "NODEC", "BOOM")
        rep.known("OK", "PRE", "NODEC", "BOOM")
        rep.analysis = {
            "PRE": {"symbol": "PRE", "stage": "pre_listing"},
            "NODEC": {"symbol": "NODEC", "ipo_score": 50.0, "decision": None},
            "BOOM": RuntimeError("x" * 300),
        }
        with caplog.at_level("INFO", logger="ipo-scanner"):
            out = ipo.ipo_repair_batch()
        assert out["repaired"] == ["OK"] and out["not_yet_tradeable"] == ["PRE", "NODEC"]
        assert out["failed"] == [{"symbol": "BOOM", "reason": "x" * 160}]
        assert out["attempted"] == 4 and out["status"] == "completed"
        assert "PRE analyzed but still unscored (stage=pre_listing)" in caplog.text
        assert rep.sleeps == [0.3] * 4                # paced after every analyzed symbol, even failures

    def test_error_row_is_reported_as_failed_not_pending(self, rep):
        rep.miss("BADPX", "PRE")
        rep.known("BADPX", "PRE")
        rep.analysis = {
            "BADPX": {"symbol": "BADPX", "stage": "unknown", "error": "issue_price must be a positive number"},
            "PRE": {"symbol": "PRE", "stage": "pre_listing"},
        }
        out = ipo.ipo_repair_batch()
        assert out["repaired"] == [] and out["not_yet_tradeable"] == ["PRE"]
        assert out["failed"] == [{"symbol": "BADPX", "reason": "issue_price must be a positive number"}]
        assert "BADPX (issue_price must be a positive number)" in out["message"]

    def test_zero_score_counts_as_scored(self, rep):
        rep.miss("Z")
        rep.known("Z")
        rep.analysis["Z"] = {"symbol": "Z", "ipo_score": 0.0, "decision": "DO NOT BUY"}
        assert ipo.ipo_repair_batch()["repaired"] == ["Z"]

    def test_persists_db_and_cache(self, rep, kv):
        rep.miss("A", "B")
        rep.known("A", "B")
        kv.store[ipo.IPO_LIST_KEY] = {"results": [{"symbol": "A", "v": "stale"}, {"symbol": "OTHER"}]}
        ipo.ipo_repair_batch()
        assert rep.upserts == [["A", "B"]]
        saved = kv.store[ipo.IPO_LIST_KEY]["results"]
        assert [r["symbol"] for r in saved] == ["A", "OTHER", "B"] and saved[0].get("v") is None
        assert kv.sets[-1][2] == ipo.IPO_LIST_CACHE_TTL_SEC

    def test_persist_failures_do_not_break_response(self, rep, kv, caplog):
        rep.miss("A")
        rep.known("A")
        rep.upsert_raises = RuntimeError("db down")
        kv.set_raises_for[ipo.IPO_LIST_KEY] = RuntimeError("kv down")
        with caplog.at_level("DEBUG", logger="ipo-scanner"):
            out = ipo.ipo_repair_batch()
        assert out["repaired"] == ["A"]
        assert "db persist failed" in caplog.text

    def test_cache_sync_failure_logged(self, rep, kv, caplog):
        rep.miss("A")
        rep.known("A")
        kv.set_raises_for[ipo.IPO_LIST_KEY] = RuntimeError("kv down")
        with caplog.at_level("DEBUG", logger="ipo-scanner"):
            ipo.ipo_repair_batch()
        assert "cache sync skipped" in caplog.text

    def test_no_results_means_no_persist(self, rep, kv):
        rep.miss("GONE")
        ipo.ipo_repair_batch()
        assert rep.upserts == [] and kv.sets == []


class TestRepairMessages:
    def test_all_repaired_has_no_message(self, rep):
        """By design: when everything is repaired and nothing is waiting on Yahoo, neither message
        branch fires and the response carries no 'message' key — the frontend (IpoFeedHealth) then
        builds its own "Repaired N symbol(s): ..." text from `repaired`."""
        rep.miss("A", "B")
        rep.known("A", "B")
        out = ipo.ipo_repair_batch()
        assert out["repaired"] == ["A", "B"] and "message" not in out

    def test_repaired_all_actionable_but_some_waiting_on_yahoo(self, rep):
        rep.audit["missing_ipos"] = [{"symbol": "A", "stage": None}] + [
            {"symbol": f"Y{i}", "stage": "no_data_yet"} for i in range(7)]
        rep.known("A")
        out = ipo.ipo_repair_batch()
        assert out["message"] == ("Repaired 1/1 actionable symbol(s). 7 symbol(s) still waiting on Yahoo price data "
                                  "(Y0, Y1, Y2, Y3, Y4…) — try again in a day or two.")

    def test_short_yahoo_list_has_no_ellipsis(self, rep):
        rep.audit["missing_ipos"] = [{"symbol": "A"}, {"symbol": "Y", "stage": "no_data_yet"}]
        rep.known("A")
        assert "(Y) — try again" in ipo.ipo_repair_batch()["message"]

    def test_only_no_data_rows(self, rep):
        rep.audit["missing_ipos"] = [{"symbol": "Y", "stage": "no_data_yet"}]
        out = ipo.ipo_repair_batch()
        assert out["message"].startswith("Repaired 0/0 actionable symbol(s). 1 symbol(s) still waiting")

    def test_failures_named_and_truncated(self, rep):
        syms = [f"G{i}" for i in range(7)]
        rep.miss("OK", *syms)
        rep.known("OK")
        out = ipo.ipo_repair_batch()
        assert out["message"].startswith("Repaired 1/8 actionable symbol(s) — 7 failed — G0 (not_found_upstream)")
        assert "G4 (not_found_upstream)…." in out["message"] or out["message"].count("…") == 1
        assert "G5" not in out["message"]

    def test_unscored_rows_are_reported_as_not_tradeable_not_lost(self, rep):
        # Every non-repaired, non-waiting symbol lands in exactly one of `failed` / `not_yet_tradeable`,
        # so the old "N no longer found upstream" fallback in the summary could never fire from a real
        # run — that dead branch has been removed from ipo_repair_batch (pass 81).
        rep.miss("A")
        rep.known("A")
        rep.analysis["A"] = {"symbol": "A", "ipo_score": None, "decision": None}
        out = ipo.ipo_repair_batch()
        assert out["not_yet_tradeable"] == ["A"] and out["failed"] == []
        assert out["message"].startswith("Repaired 0/1 actionable symbol(s) — 1 found but not tradeable yet (A)")
        assert "no longer found upstream" not in out["message"]

    def test_not_yet_tradeable_and_yahoo_parts(self, rep):
        rep.audit["missing_ipos"] = ([{"symbol": f"P{i}", "stage": "pre_listing"} for i in range(6)]
                                     + [{"symbol": "Y", "stage": "no_data_yet"}])
        rep.market_open = True
        rep.known(*[f"P{i}" for i in range(6)])
        rep.analysis = {f"P{i}": {"symbol": f"P{i}", "stage": "pre_listing"} for i in range(6)}
        out = ipo.ipo_repair_batch()
        assert out["message"] == (
            "Repaired 0/6 actionable symbol(s) — 6 found but not tradeable yet (P0, P1, P2, P3, P4…) "
            "— pre-open/listing-day rows score once the market opens — 1 waiting on Yahoo data.")


# ── purge non-equity rows ─────────────────────────────────────────────────────

class TestPurgeNonEquity:
    def test_audit_failure(self, monkeypatch):
        monkeypatch.setattr(ipo, "get_ipo_feed_audit", lambda: {"ok": False, "error": "engine unavailable"})
        assert ipo.purge_non_equity_ipos() == {"status": "error", "error": "engine unavailable", "purged": []}
        monkeypatch.setattr(ipo, "get_ipo_feed_audit", lambda: {"ok": False})
        assert ipo.purge_non_equity_ipos()["error"] == "audit failed"

    def test_nothing_to_purge(self, monkeypatch):
        for audit in ({"ok": True}, {"ok": True, "non_equity_ipos": [{"symbol": None}, {}]}):
            monkeypatch.setattr(ipo, "get_ipo_feed_audit", lambda a=audit: a)
            out = ipo.purge_non_equity_ipos()
            assert out == {"status": "completed", "purged": [], "message": "No non-equity (NCD/bond) rows found."}

    def test_purges_db_and_cache(self, monkeypatch, kv):
        monkeypatch.setattr(ipo, "get_ipo_feed_audit", lambda: {
            "ok": True, "non_equity_ipos": [{"symbol": "1150VIES30"}, {"symbol": "925ECL28"}, {"symbol": ""}]})
        deleted = []
        monkeypatch.setattr(ipo, "_ipo_db_delete_symbols", lambda syms: deleted.append(list(syms)) or len(syms))
        kv.store[ipo.IPO_LIST_KEY] = {"results": [{"symbol": "1150VIES30"}, {"symbol": "KEEP"}, {"symbol": "925ECL28"}],
                                     "generated_at": "t"}
        out = ipo.purge_non_equity_ipos()
        assert deleted == [["1150VIES30", "925ECL28"]]
        assert out["status"] == "completed" and out["purged"] == ["1150VIES30", "925ECL28"]
        assert out["message"] == "Deleted 2/2 non-equity row(s): 1150VIES30, 925ECL28"
        saved = kv.store[ipo.IPO_LIST_KEY]
        assert saved["results"] == [{"symbol": "KEEP"}] and saved["generated_at"] == "t"
        assert kv.sets[-1][2] == ipo.IPO_LIST_CACHE_TTL_SEC

    def test_long_symbol_list_truncated_in_message(self, monkeypatch, kv):
        syms = [f"{i}BOND" for i in range(10)]
        monkeypatch.setattr(ipo, "get_ipo_feed_audit", lambda: {"ok": True, "non_equity_ipos": [{"symbol": s} for s in syms]})
        monkeypatch.setattr(ipo, "_ipo_db_delete_symbols", lambda s: 7)
        out = ipo.purge_non_equity_ipos()
        assert out["message"].startswith("Deleted 7/10 non-equity row(s): 0BOND, 1BOND")
        assert out["message"].endswith("7BOND…") and "8BOND" not in out["message"]

    @pytest.mark.parametrize("cached", [None, "junk", {"results": []}, {"results": None}])
    def test_cache_without_results_left_alone(self, monkeypatch, kv, cached):
        monkeypatch.setattr(ipo, "get_ipo_feed_audit", lambda: {"ok": True, "non_equity_ipos": [{"symbol": "1X"}]})
        monkeypatch.setattr(ipo, "_ipo_db_delete_symbols", lambda s: 1)
        if cached is not None:
            kv.store[ipo.IPO_LIST_KEY] = cached
        before = len(kv.sets)
        assert ipo.purge_non_equity_ipos()["status"] == "completed"
        assert len(kv.sets) == before

    def test_cache_errors_swallowed(self, monkeypatch, kv, caplog):
        monkeypatch.setattr(ipo, "get_ipo_feed_audit", lambda: {"ok": True, "non_equity_ipos": [{"symbol": "1X"}]})
        monkeypatch.setattr(ipo, "_ipo_db_delete_symbols", lambda s: 1)
        kv.get_raises = RuntimeError("down")
        with caplog.at_level("DEBUG", logger="ipo-scanner"):
            out = ipo.purge_non_equity_ipos()
        assert out["purged"] == ["1X"] and "cache cleanup skipped" in caplog.text


# ── cached list reader ────────────────────────────────────────────────────────

class TestGetIpoList:
    def _seed(self, kv, rows):
        kv.store[ipo.IPO_LIST_KEY] = {"results": rows, "generated_at": "t"}

    def test_empty_cache_shapes(self, kv):
        assert ipo.get_ipo_list() == {"results": [], "generated_at": None, "display_days": None}
        assert ipo.get_ipo_list(7)["display_days"] == 7
        kv.store[ipo.IPO_LIST_KEY] = ["junk"]
        assert ipo.get_ipo_list()["results"] == []

    def test_kv_error(self, kv):
        kv.get_raises = RuntimeError("down")
        assert ipo.get_ipo_list(30) == {"results": [], "generated_at": None, "display_days": 30}

    def test_default_window_filters_by_age(self, kv, monkeypatch):
        monkeypatch.setattr(ipo, "IPO_CHECKER_DEFAULT_DISPLAY_DAYS", 30)
        self._seed(kv, [
            {"symbol": "NEW", "stage": "listed", "listing_date": _iso(5)},
            {"symbol": "EDGE", "stage": "listed", "listing_date": _iso(30)},
            {"symbol": "OLD", "stage": "listed", "listing_date": _iso(31)},
            {"symbol": "STUCK", "stage": "no_data_yet", "listing_date": _iso(240)},     # measured like any listed row
            {"symbol": "PRE", "stage": "pre_listing", "listing_date": _iso(400)},
            {"symbol": "DAY1", "stage": "listing_day", "listing_date": _iso(400)},
            {"symbol": "UPC", "stage": "upcoming", "listing_date": _iso(-9)},
            {"symbol": "NODATE", "stage": "listed", "listing_date": None},
            {"symbol": "BADDATE", "stage": "listed", "listing_date": "garbage"},
        ])
        out = ipo.get_ipo_list()
        assert [r["symbol"] for r in out["results"]] == ["NEW", "EDGE", "PRE", "DAY1", "UPC", "NODATE", "BADDATE"]
        assert out["display_days"] == 30 and out["total_scanned"] == 9 and out["generated_at"] == "t"

    def test_explicit_window(self, kv):
        self._seed(kv, [{"symbol": "A", "stage": "listed", "listing_date": _iso(5)},
                        {"symbol": "B", "stage": "listed", "listing_date": _iso(20)}])
        out = ipo.get_ipo_list(display_days=10)
        assert [r["symbol"] for r in out["results"]] == ["A"] and out["display_days"] == 10
        assert [r["symbol"] for r in ipo.get_ipo_list(display_days="15")["results"]] == ["A"]   # int() coercion

    @pytest.mark.parametrize("days", [0, -3, 365, 1000])
    def test_unfiltered_windows(self, kv, days):
        rows = [{"symbol": "OLD", "stage": "listed", "listing_date": _iso(300)}]
        self._seed(kv, rows)
        out = ipo.get_ipo_list(display_days=days)
        assert out["results"] == rows and out["display_days"] == days and "total_scanned" not in out

    def test_none_display_days_with_zero_default_is_unfiltered(self, kv, monkeypatch):
        monkeypatch.setattr(ipo, "IPO_CHECKER_DEFAULT_DISPLAY_DAYS", 0)
        rows = [{"symbol": "OLD", "stage": "listed", "listing_date": _iso(300)}]
        self._seed(kv, rows)
        assert ipo.get_ipo_list()["results"] == rows

    def test_results_none_and_aware_dates(self, kv, monkeypatch):
        kv.store[ipo.IPO_LIST_KEY] = {"results": None}
        assert ipo.get_ipo_list(10)["results"] == []
        aware = datetime.now(ipo.IST) - timedelta(days=1)
        monkeypatch.setattr(ipo, "_parse_date", lambda d: aware)
        self._seed(kv, [{"symbol": "A", "stage": "listed", "listing_date": "x"}])
        assert [r["symbol"] for r in ipo.get_ipo_list(10)["results"]] == ["A"]
