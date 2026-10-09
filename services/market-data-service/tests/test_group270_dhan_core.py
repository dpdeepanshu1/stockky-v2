"""group270: dhan_data core - config, scrip master, credentials, HTTP client, quote batcher, candles.

All offline: httpx.MockTransport and an in-memory SQLite stand in for Dhan and the database.
Run from services/market-data-service:   python3 -m pytest tests/test_group270_dhan_core.py -v
"""
from __future__ import annotations

import os
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import pytest

from dhan_data import client, config, creds, history, quotes, scrip_master
from dhan_data.errors import (DhanApiError, DhanAuthError, DhanNoDataError, DhanNotConfigured,
                              DhanRateLimitError, DhanSubscriptionError)

TOKEN = "SECRET-TOKEN-VALUE-123"
CID = "1100999999"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in list(os.environ):
        if k.startswith("DHAN_") or k in ("QUOTE_PROVIDER_ORDER", "HISTORY_PROVIDER_ORDER"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DHAN_DATA_ENABLED", "1")
    monkeypatch.setenv("DHAN_QUOTE_MIN_INTERVAL_S", "0.2")      # the config floor; keeps the suite fast
    monkeypatch.setenv("DHAN_QUOTE_BATCH_WINDOW_MS", "30")
    quotes._reset_for_tests()
    client._reset_for_tests()
    creds.invalidate()
    scrip_master._set_for_tests({}, {})
    monkeypatch.setattr(client, "_breaker", lambda: None)
    yield
    quotes._reset_for_tests()
    client._reset_for_tests()


# ── config ─────────────────────────────────────────────────────────────────────────────────────────────────────
class TestConfig:
    def test_default_order_is_dhan_angelone_yfinance(self):
        assert config.quote_order() == ["dhan", "angelone", "yfinance"]
        assert config.quote_position() == "first"

    @pytest.mark.parametrize("order,pos", [
        ("dhan,angelone,yfinance", "first"),
        ("angelone,dhan,yfinance", "after_angelone"),
        ("angelone,yfinance,dhan", "after_yfinance"),
        ("angelone,yfinance", "off"),
        ("yfinance,dhan", "after_yfinance"),
        ("dhan", "first"),
    ])
    def test_positions(self, monkeypatch, order, pos):
        monkeypatch.setenv("QUOTE_PROVIDER_ORDER", order)
        assert config.quote_position() == pos

    def test_blank_and_garbage_values_use_defaults(self, monkeypatch):
        for k in ("QUOTE_PROVIDER_ORDER", "HISTORY_PROVIDER_ORDER"):
            monkeypatch.setenv(k, "")
        monkeypatch.setenv("DHAN_QUOTE_MIN_INTERVAL_S", "")
        monkeypatch.setenv("DHAN_QUOTE_MAX_BATCH", "abc")
        monkeypatch.setenv("DHAN_DATA_BASE_URL", "")
        monkeypatch.setenv("DHAN_LIVE_POLL_S", "  ")
        assert config.quote_order() == ["dhan", "angelone", "yfinance"]
        assert config.quote_min_interval_s() == 1.1
        assert config.quote_max_batch() == 1000
        assert config.base_url() == "https://api.dhan.co/v2"
        assert config.live_poll_interval_s() == 1.2

    def test_unknown_providers_dropped_and_dupes_removed(self, monkeypatch):
        monkeypatch.setenv("QUOTE_PROVIDER_ORDER", "dhan, nonsense ,dhan,yfinance")
        assert config.quote_order() == ["dhan", "yfinance"]

    def test_disabled_turns_everything_off(self, monkeypatch):
        monkeypatch.setenv("DHAN_DATA_ENABLED", "0")
        assert config.quote_position() == "off" and config.history_position() == "off"

    def test_floor_on_rate_interval(self, monkeypatch):
        monkeypatch.setenv("DHAN_QUOTE_MIN_INTERVAL_S", "0.01")
        assert config.quote_min_interval_s() == 0.2


# ── scrip master ────────────────────────────────────────────────────────────────────────────────────────────────
CSV = """SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_SMST_SECURITY_ID,SEM_INSTRUMENT_NAME,SEM_TRADING_SYMBOL,SEM_SERIES,SM_SYMBOL_NAME
NSE,E,2885,EQUITY,RELIANCE,EQ,RELIANCE INDUSTRIES LTD
NSE,E,11536,EQUITY,TCS,EQ,TATA CONSULTANCY
NSE,E,9999,EQUITY,RELIANCE,BE,SHOULD BE IGNORED
NSE,E,5555,EQUITY,RELIANCE,EQ,DUPLICATE KEEPS FIRST
BSE,E,500325,EQUITY,RELIANCE,A,BSE ROW IGNORED
NSE,I,13,INDEX,NIFTY,,Nifty 50
NSE,I,25,INDEX,BANKNIFTY,,Nifty Bank
BSE,I,51,INDEX,SENSEX,,SENSEX
NSE,D,777,FUTSTK,RELIANCE,,FUT ROW IGNORED
NSE,E,notanumber,EQUITY,BAD,EQ,BAD ID
"""


class TestScripMaster:
    def test_parse_keeps_nse_eq_only_first_row_wins(self):
        eq, ix = scrip_master.parse_csv(CSV)
        assert eq == {"RELIANCE": 2885, "TCS": 11536}

    def test_parse_indices(self):
        _, ix = scrip_master.parse_csv(CSV)
        assert ix["NIFTY"] == 13 and ix["NIFTY50"] == 13
        assert ix["BANKNIFTY"] == 25 and ix["SENSEX"] == 51

    def test_header_case_insensitive(self):
        eq, _ = scrip_master.parse_csv(CSV.replace("SEM_EXM_EXCH_ID", "sem_exm_exch_id"))
        assert "TCS" in eq

    def test_lookup_equity_strips_suffix(self):
        scrip_master._set_for_tests({"RELIANCE": 2885}, {})
        assert scrip_master.security_id("RELIANCE.NS") == ("NSE_EQ", 2885)
        assert scrip_master.security_id("reliance") == ("NSE_EQ", 2885)
        assert scrip_master.security_id("NOPE") is None

    def test_lookup_index_via_yahoo_ticker(self):
        eq, ix = scrip_master.parse_csv(CSV)
        scrip_master._set_for_tests(eq, ix)
        assert scrip_master.security_id("^NSEI") == ("IDX_I", 13)
        assert scrip_master.security_id("^NSEBANK") == ("IDX_I", 25)
        assert scrip_master.security_id("^BSESN") == ("IDX_I", 51)
        assert scrip_master.security_id("^INDIAVIX") is None      # not in this sample: no guessed id

    def test_split_known(self):
        scrip_master._set_for_tests({"TCS": 1}, {})
        assert scrip_master.split_known(["TCS", "ZZZ"]) == (["TCS"], ["ZZZ"])

    def test_load_rejects_tiny_map_and_keeps_previous(self, monkeypatch):
        scrip_master._set_for_tests({"KEEP": 1}, {})

        class R:
            text = CSV

            def raise_for_status(self):
                pass

        class C:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get(self, url): return R()

        monkeypatch.setattr(httpx, "Client", C)
        assert scrip_master.load(force=True) is True          # previous map still in place
        assert scrip_master.security_id("KEEP") == ("NSE_EQ", 1)
        assert "only 2 NSE equities" in (scrip_master.status()["last_error"] or "")


# ── credentials ─────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture()
def cred_db(monkeypatch):
    from cryptography.fernet import Fernet
    from sqlalchemy import create_engine, text
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("DHAN_CREDENTIAL_ENC_KEY", key)
    eng = create_engine("sqlite://")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE trade_credentials (id INTEGER PRIMARY KEY, dhan_client_id_encrypted TEXT, "
                       "access_token_encrypted TEXT, token_issued_at TIMESTAMP, token_expires_at TIMESTAMP)"))
    monkeypatch.setattr(creds, "_engine_provider", lambda: eng)
    f = Fernet(key.encode())

    def put(client_id=CID, token=TOKEN, issued=None, expires=None, enc=f):
        issued = issued or datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
        expires = expires or issued + timedelta(hours=24)
        with eng.begin() as c:
            c.execute(text("DELETE FROM trade_credentials"))
            c.execute(text("INSERT INTO trade_credentials VALUES (1, :a, :b, :c, :d)"),
                      {"a": enc.encrypt(client_id.encode()).decode(), "b": enc.encrypt(token.encode()).decode(),
                       "c": issued, "d": expires})
    yield put
    monkeypatch.setattr(creds, "_engine_provider", None)


class TestCreds:
    def test_reads_and_decrypts(self, cred_db):
        cred_db()
        assert creds.get_credentials(force=True) == (CID, TOKEN)
        assert creds.status()["ok"] is True

    def test_expired_token(self, cred_db):
        old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=30)
        cred_db(issued=old, expires=old + timedelta(hours=24))
        with pytest.raises(DhanNotConfigured, match="token_expired"):
            creds.get_credentials(force=True)

    def test_expiry_clamped_to_24h_hard_cap(self, cred_db):
        issued = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=26)
        cred_db(issued=issued, expires=issued + timedelta(days=30))      # claims 30 days, Dhan caps at 24 h
        with pytest.raises(DhanNotConfigured, match="token_expired"):
            creds.get_credentials(force=True)

    def test_wrong_key(self, cred_db, monkeypatch):
        from cryptography.fernet import Fernet
        cred_db(enc=Fernet(Fernet.generate_key()))
        with pytest.raises(DhanNotConfigured, match="key_mismatch"):
            creds.get_credentials(force=True)

    def test_missing_key_and_no_row(self, cred_db, monkeypatch):
        monkeypatch.setenv("DHAN_CREDENTIAL_ENC_KEY", "")
        with pytest.raises(DhanNotConfigured, match="enc_key_missing"):
            creds.get_credentials(force=True)
        from cryptography.fernet import Fernet
        monkeypatch.setenv("DHAN_CREDENTIAL_ENC_KEY", Fernet.generate_key().decode())
        with pytest.raises(DhanNotConfigured, match="no_row"):
            creds.get_credentials(force=True)

    def test_oracle_lob_values_are_read(self):
        class Lob:
            def __init__(self, v): self.v = v
            def read(self): return self.v
        assert creds._as_text(Lob("abc")) == "abc"
        assert creds._as_text(None) is None and creds._as_text("  ") is None

    def test_status_never_contains_secrets(self, cred_db):
        cred_db()
        s = str(creds.status())
        assert TOKEN not in s and CID not in s

    def test_failure_is_cached_briefly(self, cred_db):
        cred_db()
        creds.get_credentials(force=True)
        calls = {"n": 0}
        orig = creds._load
        creds._load = lambda: (calls.__setitem__("n", calls["n"] + 1) or orig())
        try:
            creds.get_credentials(); creds.get_credentials()
        finally:
            creds._load = orig
        assert calls["n"] == 0                                  # served from the 60 s cache


# ── HTTP client ─────────────────────────────────────────────────────────────────────────────────────────────────
def _mock(monkeypatch, handler):
    monkeypatch.setattr(client, "_http", httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(creds, "get_credentials", lambda force=False: (CID, TOKEN))


class TestClassify:
    @pytest.mark.parametrize("status,body,cls", [
        (401, None, DhanAuthError),
        (403, {"errorCode": "DH-901"}, DhanAuthError),
        (200, {"status": "failure", "data": {"807": "Access token expired"}}, DhanAuthError),
        (200, {"status": "failure", "data": {"806": "Data APIs not subscribed"}}, DhanSubscriptionError),
        (429, None, DhanRateLimitError),
        (200, {"status": "failure", "data": {"805": "Too many requests"}}, DhanRateLimitError),
        (400, {"errorCode": "DH-905", "errorMessage": "bad input"}, DhanNoDataError),
        (200, {"status": "failure", "data": {"813": "Invalid SecurityId"}}, DhanNoDataError),
        (500, {"errorCode": "DH-908"}, DhanApiError),
    ])
    def test_mapping(self, status, body, cls):
        assert type(client.classify(status, body, "")) is cls


class TestPost:
    def test_sends_headers_and_body_and_returns_json(self, monkeypatch):
        seen = {}

        def handler(req: httpx.Request):
            seen["url"], seen["h"], seen["body"] = str(req.url), dict(req.headers), req.content
            return httpx.Response(200, json={"status": "success", "data": {"NSE_EQ": {}}})
        _mock(monkeypatch, handler)
        out = client.post("marketfeed/quote", {"NSE_EQ": [1]}, limiter=client.quote_limiter)
        assert out["status"] == "success"
        assert seen["url"] == "https://api.dhan.co/v2/marketfeed/quote"
        assert seen["h"]["access-token"] == TOKEN and seen["h"]["client-id"] == CID
        assert b'"NSE_EQ"' in seen["body"]

    def test_failure_raises_typed_error_without_secrets(self, monkeypatch):
        _mock(monkeypatch, lambda r: httpx.Response(401, json={"errorCode": "DH-901", "errorMessage": "bad"}))
        with pytest.raises(DhanAuthError) as ei:
            client.post("marketfeed/ltp", {"NSE_EQ": [1]}, limiter=client.quote_limiter)
        assert TOKEN not in str(ei.value) and CID not in str(ei.value)
        assert client.stats()["auth_errors"] == 1

    def test_network_error_text_has_no_headers(self, monkeypatch):
        def boom(req):
            raise httpx.ConnectError(f"cannot connect with {TOKEN}")
        _mock(monkeypatch, boom)
        with pytest.raises(DhanApiError) as ei:
            client.post("marketfeed/ltp", {}, limiter=client.quote_limiter)
        assert TOKEN not in str(ei.value) and "ConnectError" in str(ei.value)

    def test_non_json_body_is_an_error(self, monkeypatch):
        _mock(monkeypatch, lambda r: httpx.Response(200, text="<html>"))
        with pytest.raises(DhanApiError):
            client.post("x", {}, limiter=client.quote_limiter)

    def test_limiter_spaces_calls(self):
        lim = client.Limiter(lambda: 0.2)
        t0 = time.monotonic()
        for _ in range(3):
            lim.wait()
        assert time.monotonic() - t0 >= 0.38

    def test_limiter_slow_down_doubles_interval(self):
        lim = client.Limiter(lambda: 0.2)
        lim.slow_down(2.0, 5.0)
        assert lim.interval() == pytest.approx(0.4)


class TestPauseAndFailureHandling:
    def test_auth_error_pauses_and_invalidates(self, monkeypatch):
        called = {"inv": 0}
        monkeypatch.setattr(creds, "invalidate", lambda: called.__setitem__("inv", called["inv"] + 1))
        assert client.available() is True
        client.note_failure(DhanAuthError("x"))
        assert client.available() is False and called["inv"] == 1
        assert "auth" in (client.paused() or "")

    def test_subscription_error_pauses(self):
        client.note_failure(DhanSubscriptionError("x"))
        assert client.available() is False

    def test_not_configured_pauses_five_minutes(self):
        client.note_failure(DhanNotConfigured("no_row"))
        assert client.paused() is not None

    def test_rate_limit_slows_but_does_not_pause(self):
        client.note_failure(DhanRateLimitError("x"))
        assert client.available() is True
        assert client.quote_limiter.interval() == pytest.approx(config.quote_min_interval_s() * 2)

    def test_no_data_is_not_a_failure(self):
        client.note_failure(DhanNoDataError("x"))
        assert client.available() is True

    def test_disabled_is_unavailable(self, monkeypatch):
        monkeypatch.setenv("DHAN_DATA_ENABLED", "0")
        assert client.available() is False


# ── quotes ──────────────────────────────────────────────────────────────────────────────────────────────────────
def _item(price, prev=None, vol=1000, **extra):
    d = {"last_price": price, "ohlc": {"open": price, "high": price + 1, "low": price - 1, "close": prev or price - 2},
         "volume": vol, "upper_circuit_limit": price * 1.2, "lower_circuit_limit": price * 0.8,
         "net_change": price - (prev or price - 2), "average_price": price}
    d.update(extra)
    return d


class TestMapping:
    def test_map_item(self):
        r = quotes.map_item("TCS", _item(100.0, prev=98.0))
        assert r["price"] == 100.0 and r["previous_close"] == 98.0 and r["day_change_pct"] == round(2 / 98 * 100, 2)
        assert r["day_high"] == 101.0 and r["day_low"] == 99.0 and r["volume"] == 1000
        assert r["upper_circuit"] == pytest.approx(120.0) and r["source"] == "dhan"

    @pytest.mark.parametrize("bad", [None, {}, {"last_price": 0}, {"last_price": -5}, {"last_price": "x"},
                                     {"last_price": float("nan")}, "str"])
    def test_unusable_prices_give_none(self, bad):
        assert quotes.map_item("X", bad) is None

    def test_prev_close_disagreement_is_counted_not_changed(self):
        before = quotes.status()["prev_close_disagreements"]
        r = quotes.map_item("X", {"last_price": 100.0, "ohlc": {"close": 90.0}, "net_change": 1.0})
        assert r["previous_close"] == 90.0                      # never silently replaced
        assert quotes.status()["prev_close_disagreements"] == before + 1

    def test_ltt_formats(self):
        d = quotes.parse_ltt("09/10/2026 14:31:05")
        assert d == datetime(2026, 10, 9, 9, 1, 5, tzinfo=timezone.utc)      # 14:31 IST
        assert quotes.parse_ltt("garbage") is None and quotes.parse_ltt(None) is None


def _quote_server(monkeypatch, prices, delay=0.0, fail=None):
    """Mock Dhan /marketfeed/quote. prices: {securityId: price}. Counts calls and records requested ids."""
    calls = []

    def handler(req: httpx.Request):
        import json
        body = json.loads(req.content)
        calls.append(body)
        if delay:
            time.sleep(delay)
        if fail:
            return fail()
        data = {}
        for seg, ids in body.items():
            data[seg] = {str(i): _item(prices[i]) for i in ids if i in prices}
        return httpx.Response(200, json={"status": "success", "data": data})
    _mock(monkeypatch, handler)
    return calls


class TestBatcher:
    def _universe(self, n):
        m = {f"S{i}": 1000 + i for i in range(n)}
        scrip_master._set_for_tests(m, {})
        return m

    def test_many_concurrent_single_symbol_callers_share_one_upstream_call(self, monkeypatch):
        m = self._universe(60)
        calls = _quote_server(monkeypatch, {v: 100.0 + i for i, v in enumerate(m.values())})
        out, errs = {}, []

        def worker(sym):
            try:
                out[sym] = quotes.get_quote(sym)
            except Exception as e:      # noqa: BLE001
                errs.append(e)
        ts = [threading.Thread(target=worker, args=(s,)) for s in m]
        [t.start() for t in ts]; [t.join(10) for t in ts]
        assert not errs and all(out[s] and out[s]["price"] > 0 for s in m)
        assert len(calls) == 1                                  # 60 callers, ONE request
        assert sorted(calls[0]["NSE_EQ"]) == sorted(m.values())

    def test_fresh_row_is_served_with_zero_network(self, monkeypatch):
        m = self._universe(3)
        calls = _quote_server(monkeypatch, {1000: 50.0, 1001: 51.0, 1002: 52.0})
        quotes.get_quotes(list(m))
        n = len(calls)
        again = quotes.get_quotes(list(m))
        assert len(calls) == n and len(again) == 3

    def test_stale_row_triggers_a_new_call(self, monkeypatch):
        self._universe(1)
        calls = _quote_server(monkeypatch, {1000: 50.0})
        quotes.get_quote("S0")
        quotes._rows["S0"]["_mono"] -= 10.0
        quotes.get_quote("S0")
        assert len(calls) == 2

    def test_unknown_symbols_never_queued_and_never_wait(self, monkeypatch):
        self._universe(1)
        calls = _quote_server(monkeypatch, {1000: 50.0})
        t0 = time.monotonic()
        assert quotes.get_quotes(["NOPE", "ALSO_NOPE"]) == {}
        assert time.monotonic() - t0 < 0.5 and calls == []

    def test_failure_releases_waiters_quickly(self, monkeypatch):
        self._universe(2)
        _quote_server(monkeypatch, {}, fail=lambda: httpx.Response(500, json={"errorCode": "DH-908"}))
        t0 = time.monotonic()
        assert quotes.get_quotes(["S0", "S1"]) == {}
        assert time.monotonic() - t0 < 2.0                      # not the full 3.5 s wait

    def test_auth_failure_pauses_then_stage_is_skipped_instantly(self, monkeypatch):
        self._universe(2)
        calls = _quote_server(monkeypatch, {}, fail=lambda: httpx.Response(401, json={"errorCode": "DH-901"}))
        quotes.get_quotes(["S0"])
        assert client.available() is False
        n = len(calls)
        t0 = time.monotonic()
        assert quotes.get_quotes(["S1"]) == {}
        assert time.monotonic() - t0 < 0.2 and len(calls) == n

    def test_symbol_missing_from_response_is_released_not_hung(self, monkeypatch):
        self._universe(2)
        _quote_server(monkeypatch, {1000: 50.0})                # S1 (1001) not answered by Dhan
        t0 = time.monotonic()
        out = quotes.get_quotes(["S0", "S1"])
        assert "S0" in out and "S1" not in out and time.monotonic() - t0 < 2.0

    def test_batch_is_capped_and_split(self, monkeypatch):
        monkeypatch.setenv("DHAN_QUOTE_MAX_BATCH", "10")
        m = self._universe(25)
        calls = _quote_server(monkeypatch, {v: 10.0 for v in m.values()})
        out = quotes.get_quotes(list(m), wait_s=15)
        assert len(out) == 25 and len(calls) == 3 and all(len(c["NSE_EQ"]) <= 10 for c in calls)

    def test_index_symbols_go_in_their_own_segment(self, monkeypatch):
        scrip_master._set_for_tests({"TCS": 1}, {"NIFTY": 13})
        calls = _quote_server(monkeypatch, {1: 100.0, 13: 24000.0})
        out = quotes.get_quotes(["TCS", "^NSEI"])
        assert set(out) == {"TCS", "^NSEI"}
        assert calls[0] == {"NSE_EQ": [1], "IDX_I": [13]}

    def test_background_rows_skip_symbols_already_fresh(self, monkeypatch):
        self._universe(2)
        calls = _quote_server(monkeypatch, {1000: 50.0, 1001: 51.0})
        quotes.get_quote("S0")
        n = len(calls)
        assert quotes.request(["S0"], background=True) == 0       # fresh: nothing queued
        assert quotes.request(["S1"], background=True) == 1
        deadline = time.time() + 5
        while len(calls) == n and time.time() < deadline:
            time.sleep(0.05)
        assert len(calls) == n + 1 and calls[-1]["NSE_EQ"] == [1001]

    def test_listener_receives_rows(self, monkeypatch):
        self._universe(2)
        _quote_server(monkeypatch, {1000: 50.0, 1001: 51.0})
        got = []
        quotes.add_listener(lambda rows: got.extend(rows))
        quotes.get_quotes(["S0", "S1"])
        assert {r["symbol"] for r in got} == {"S0", "S1"}

    def test_disabled_returns_nothing_and_makes_no_call(self, monkeypatch):
        self._universe(1)
        calls = _quote_server(monkeypatch, {1000: 50.0})
        monkeypatch.setenv("DHAN_DATA_ENABLED", "0")
        assert quotes.get_quotes(["S0"]) == {} and calls == []

    def test_inject_rows_are_served_fresh(self, monkeypatch):
        calls = _quote_server(monkeypatch, {})
        quotes.inject_rows([{"symbol": "ABC", "price": 12.5, "_mono": time.monotonic()}])
        assert quotes.peek("ABC")["price"] == 12.5
        assert quotes.get_quote("ABC")["price"] == 12.5 and calls == []


# ── candles ─────────────────────────────────────────────────────────────────────────────────────────────────────
def _ts(d: date, hh=0, mm=0):
    from zoneinfo import ZoneInfo
    return int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=ZoneInfo("Asia/Kolkata")).timestamp())


class TestCandles:
    def test_parse_daily(self):
        d0 = date(2026, 10, 5)
        body = {"open": [10, 11], "high": [12, 13], "low": [9, 10], "close": [11, 12], "volume": [100, 200],
                "timestamp": [_ts(d0), _ts(d0 + timedelta(days=1))]}
        c = history.parse_candles(body, intraday=False)
        assert [x["date"] for x in c] == ["2026-10-05 00:00", "2026-10-06 00:00"]
        assert c[1] == {"date": "2026-10-06 00:00", "open": 11.0, "high": 13.0, "low": 10.0, "close": 12.0, "volume": 200}

    def test_parse_intraday_keeps_time(self):
        d0 = date(2026, 10, 5)
        body = {"open": [1], "high": [2], "low": [1], "close": [1.5], "volume": [5], "timestamp": [_ts(d0, 9, 15)]}
        assert history.parse_candles(body, intraday=True)[0]["date"] == "2026-10-05 09:15"

    def test_bad_rows_dropped_and_duplicates_collapse(self):
        d0 = date(2026, 10, 5)
        body = {"open": [1, 1, 1, 1], "high": [2, 2, 2, 2], "low": [1, 1, 1, 1], "close": [5, 0, None, 7],
                "volume": [1, 1, 1, 1], "timestamp": [_ts(d0), _ts(d0 + timedelta(days=1)), _ts(d0 + timedelta(days=2)), _ts(d0)]}
        c = history.parse_candles(body, intraday=False)
        assert len(c) == 1 and c[0]["close"] == 7.0              # duplicate date: last wins; zero/None dropped

    def test_old_1980_epoch_is_rejected_not_misread(self):
        body = {"open": [1], "high": [1], "low": [1], "close": [1], "volume": [1], "timestamp": [1234567]}
        assert history.parse_candles(body, intraday=False) == []

    def test_unsorted_input_is_sorted(self):
        d0 = date(2026, 10, 5)
        body = {"open": [1, 1], "high": [1, 1], "low": [1, 1], "close": [2, 1], "volume": [1, 1],
                "timestamp": [_ts(d0 + timedelta(days=3)), _ts(d0)]}
        assert [c["close"] for c in history.parse_candles(body, intraday=False)] == [1.0, 2.0]

    def test_weekly_aggregation(self):
        days = [date(2026, 10, 5) + timedelta(days=i) for i in range(7)]   # Mon..Sun
        days = [d for d in days if d.weekday() < 5] + [date(2026, 10, 12)]
        candles = [{"date": f"{d} 00:00", "open": 10 + i, "high": 20 + i, "low": 5 + i, "close": 15 + i, "volume": 100}
                   for i, d in enumerate(days)]
        w = history.to_weekly(candles)
        assert len(w) == 2 and w[0]["date"] == "2026-10-05 00:00"
        assert w[0]["open"] == 10 and w[0]["close"] == 19 and w[0]["high"] == 24 and w[0]["low"] == 5 and w[0]["volume"] == 500
        assert w[1]["date"] == "2026-10-12 00:00"

    def test_fetch_daily_payload(self, monkeypatch):
        scrip_master._set_for_tests({"TCS": 11536}, {"NIFTY": 13})
        seen = []

        def handler(req):
            import json
            seen.append((str(req.url), json.loads(req.content)))
            return httpx.Response(200, json={"open": [1], "high": [2], "low": [1], "close": [1.5], "volume": [9],
                                             "timestamp": [_ts(date(2026, 10, 5))]})
        _mock(monkeypatch, handler)
        monkeypatch.setenv("DHAN_HIST_MAX_PER_SEC", "50")
        out = history.fetch_daily("TCS", date(2026, 1, 1), date(2026, 10, 10))
        assert len(out) == 1
        url, body = seen[0]
        assert url.endswith("/charts/historical")
        assert body == {"securityId": "11536", "exchangeSegment": "NSE_EQ", "instrument": "EQUITY", "expiryCode": 0,
                        "oi": False, "fromDate": "2026-01-01", "toDate": "2026-10-10"}
        history.fetch_daily("^NSEI", date(2026, 1, 1), date(2026, 2, 1))
        assert seen[1][1]["instrument"] == "INDEX" and seen[1][1]["exchangeSegment"] == "IDX_I"

    def test_fetch_unknown_symbol_is_no_data(self):
        with pytest.raises(DhanNoDataError):
            history.fetch_daily("NOPE", date(2026, 1, 1), date(2026, 2, 1))

    def test_intraday_is_chunked_and_interval_mapped(self, monkeypatch):
        scrip_master._set_for_tests({"TCS": 11536}, {})
        monkeypatch.setenv("DHAN_INTRADAY_CHUNK_DAYS", "10")
        monkeypatch.setenv("DHAN_HIST_MAX_PER_SEC", "50")
        seen = []

        def handler(req):
            import json
            b = json.loads(req.content)
            seen.append(b)
            return httpx.Response(200, json={"open": [1], "high": [2], "low": [1], "close": [1.5], "volume": [9],
                                             "timestamp": [_ts(date(2026, 9, 1), 9, 15 + len(seen))]})
        _mock(monkeypatch, handler)
        out = history.fetch_candles("TCS", "1h", date(2026, 9, 1), date(2026, 9, 26))
        assert len(seen) == 3 and all(b["interval"] == "60" for b in seen)
        assert len(out) == 3 and out == sorted(out, key=lambda c: c["date"])

    def test_supports(self):
        assert history.supports("1d") and history.supports("1wk") and history.supports("1h")
        assert not history.supports("1mo") and not history.supports("3d")
