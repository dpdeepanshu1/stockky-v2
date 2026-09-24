"""
tests/test_dhan_credentials.py

100%-coverage-plan round for auth/dhan_credentials.py (was 18% — 159 of 194
statements never executed; every other test mocks save_credentials /
get_decrypted_credentials / enforce_live_token / refresh_if_totp_enabled at
its own boundary, so the module itself had almost no direct coverage).

This is the module that decides whether REAL trading is allowed to touch the
Dhan account at all: it encrypts the client id + access token at rest, does
the local-clock expiry math the gate state machine polls, disarms REAL the
moment Dhan rejects the token or the host IP, and runs the opt-in TOTP
auto-refresh that talks to auth.dhan.co with the account PIN.

Everything runs against a REAL in-memory SQLite database (real
TradeCredential / TradeGateState tables) and REAL Fernet encryption and
REAL pyotp — the code under test is never mocked. The only fakes are:
  * httpx.post — replaced by a recorder that returns REAL httpx.Response
    objects bound to a REAL httpx.Request, so raise_for_status() produces the
    genuine HTTPStatusError text (including the full URL + query string,
    which is exactly what session96's PIN-leak regression needs);
  * notifier.notify_sync — captured, so the Telegram text can be asserted;
  * execution.dhan_client.verify_token_live — the live Dhan call
    enforce_live_token() makes (is_auth_error() stays the REAL classifier);
  * dc.datetime.now — frozen where exact expiry arithmetic matters;
  * an SQLAlchemy before_cursor_execute hook that makes chosen writes fail,
    to reproduce a genuine PendingRollbackError.

Two PRODUCTION FIXES ship with this round (both found while writing these
tests, both regression-pinned below and shown to FAIL on the pre-fix code):
  1. PIN / TOTP / client-id leak — httpx's HTTPStatusError message embeds the
     full request URL, and Dhan's generateAccessToken takes dhanClientId, pin
     and totp as QUERY PARAMETERS, so any 4xx/5xx from it put the account PIN
     into the service log and (first 300 chars) into the Telegram alert.
     Now routed through _redact_secrets() before logging/notifying.
  2. Poisoned caller Session — a DB failure while saving the refreshed token
     (or restoring gate.dhan_connected) was swallowed as "non-fatal" but left
     the caller's Session in the partial-rollback state, so the very next
     query in cycle_runner (enforce_live_token) raised PendingRollbackError.
     Now healed with _heal_session() (rolls back only if Session.is_active is
     False, so a healthy caller's pending state is never discarded).

What is covered:
  * _effective_expiry — None, missing issued_at, naive-as-UTC, the 24h hard
    cap clamping a stale 30-day row, and a shorter real expiry left alone.
  * _fernet / _mask — unset key, malformed key, round-trip, masking edges.
  * save_credentials — insert, single-row overwrite, blobs never contain the
    plaintext, guess-vs-real expiry, naive real expiry stamped UTC, no key.
  * get_decrypted_credentials — no row, empty token, round-trip, rotated key
    and corrupted blob (fail closed -> None), unset key (raises).
  * connection_status — exact not-connected shape, connected shape and
    countdown numbers, stale-row clamp, no-expiry row, no secret ever in the
    payload.
  * is_token_valid / token_needs_refresh — including the exact refresh-margin
    boundary and the hard-cap clamp being what actually enforces expiry.
  * enforce_live_token — ok, transient error (never disarms on a guess),
    every auth marker disarms, no-gate row, per-mode isolation, warning only
    when it was actually armed, reason truncation.
  * disarm_on_invalid_ip — no gate, armed/not-armed return value, reason
    text + truncation, per-mode isolation.
  * _parse_dhan_expiry — epoch s/ms, the CONFIRMED-2026-09-03 bare-ISO-is-IST
    format, Z/offset forms, space-separated, garbage, empty, overflow/NaN.
  * refresh_if_totp_enabled — disabled, missing env, happy path (exact HTTP
    request incl. a real TOTP code), every token-field shape, expiry from
    top-level and nested, gate healing (only REAL, only when it was False,
    never re-arms), HTTP 401/500, network error, bad JSON, non-dict JSON,
    pyotp missing, DB failure on save, DB failure on gate heal, notifier
    failure on both paths, 300-char cap, and the PIN-leak regression.
  * _redact_secrets / _heal_session helpers directly.

Run from services/real-trade-service:
    python3 -m pytest tests/test_dhan_credentials.py -q \\
        --cov=auth.dhan_credentials --cov-report=term-missing
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import httpx
import pyotp
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, event
from sqlalchemy.exc import OperationalError, PendingRollbackError
from sqlalchemy.orm import sessionmaker

import config
import models
import notifier
from auth import dhan_credentials as dc
from execution import dhan_client
from tz_utils import as_aware

LOGGER = "real-trade-dhan-auth"

KEY = Fernet.generate_key().decode()
OTHER_KEY = Fernet.generate_key().decode()

CLIENT_ID = "1100123456"
PIN = "987654"
TOTP_SECRET = "JBSWY3DPEHPK3PXP"  # valid base32, the pyotp docs' example secret
TOKEN = "eyJ0b2tlbi1mcm9tLWRoYW4ifQ.SECRET-ACCESS-TOKEN"

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 9, 3, 6, 0, 0, tzinfo=UTC)


# ── fixtures / helpers ────────────────────────────────────────────────────
@pytest.fixture()
def engine():
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    eng._fail_on = set()  # substrings of statements that must raise

    @event.listens_for(eng, "before_cursor_execute")
    def _maybe_fail(conn, cursor, statement, params, context, executemany):
        head = statement.lstrip().upper()
        if head.startswith(("INSERT", "UPDATE")) and any(t in statement for t in eng._fail_on):
            raise OperationalError(statement, params, Exception("simulated database outage"))

    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine):
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


@pytest.fixture()
def enc_key(monkeypatch):
    monkeypatch.setattr(config, "DHAN_CREDENTIAL_ENC_KEY", KEY)
    return KEY


@pytest.fixture()
def frozen(monkeypatch):
    return _freeze(monkeypatch, NOW)


def _freeze(monkeypatch, fixed):
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz is None else fixed.astimezone(tz)

    monkeypatch.setattr(dc, "datetime", _Frozen)
    return fixed


def _row(db, *, issued, expires, token="x", masked="****3456"):
    r = models.TradeCredential(
        dhan_client_id_masked=masked,
        dhan_client_id_encrypted="x",
        access_token_encrypted=token,
        token_issued_at=issued,
        token_expires_at=expires,
    )
    db.add(r)
    db.commit()
    return r


def _gate(db, mode="REAL", *, armed=False, connected=False):
    g = models.TradeGateState(mode=mode, armed=armed, dhan_connected=connected)
    db.add(g)
    db.commit()
    return g


def _fresh_gate(db, mode="REAL"):
    db.expire_all()
    return db.query(models.TradeGateState).filter_by(mode=mode).first()


@pytest.fixture()
def sent(monkeypatch):
    """Captures every Telegram text refresh_if_totp_enabled tries to send."""
    box: list[str] = []
    monkeypatch.setattr(notifier, "notify_sync", lambda text: box.append(text) or True)
    return box


class _PostRecorder:
    """Stand-in for httpx.post. Builds a REAL httpx.Request/Response so
    raise_for_status() produces the genuine error text."""

    def __init__(self, status=200, body=None, exc=None, content=None):
        self.status, self.body, self.exc, self.content = status, body, exc, content
        self.calls: list[dict] = []

    def __call__(self, url, params=None, timeout=None, **kw):
        self.calls.append({"url": url, "params": params, "timeout": timeout, **kw})
        if self.exc is not None:
            raise self.exc
        req = httpx.Request("POST", url, params=params)
        if self.content is not None:
            return httpx.Response(self.status, content=self.content, request=req)
        return httpx.Response(self.status, json=self.body, request=req)


@pytest.fixture()
def totp_env(monkeypatch, enc_key):
    monkeypatch.setattr(config, "DHAN_TOTP_ENABLED", True)
    monkeypatch.setenv("DHAN_TOTP_SECRET", TOTP_SECRET)
    monkeypatch.setenv("DHAN_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("DHAN_PIN", PIN)


def _post(monkeypatch, **kw) -> _PostRecorder:
    rec = _PostRecorder(**kw)
    monkeypatch.setattr(httpx, "post", rec)
    return rec


# ══════════════════════════════════════════════════════════════════════════
# _effective_expiry
# ══════════════════════════════════════════════════════════════════════════
class TestEffectiveExpiry:
    def test_no_expiry_returns_none(self):
        assert dc._effective_expiry(SimpleNamespace(token_expires_at=None, token_issued_at=NOW)) is None

    def test_missing_issued_at_falls_back_to_stored_expiry_as_utc(self):
        row = SimpleNamespace(token_expires_at=datetime(2026, 9, 4, 1, 2, 3), token_issued_at=None)
        assert dc._effective_expiry(row) == datetime(2026, 9, 4, 1, 2, 3, tzinfo=UTC)

    def test_stale_30_day_row_is_clamped_to_24h_hard_cap(self):
        # A row saved back when DHAN_TOKEN_LIFETIME_DAYS=30 was misconfigured.
        row = SimpleNamespace(token_issued_at=NOW, token_expires_at=NOW + timedelta(days=30))
        assert dc._effective_expiry(row) == NOW + timedelta(hours=24)

    def test_shorter_real_expiry_is_kept(self):
        row = SimpleNamespace(token_issued_at=NOW, token_expires_at=NOW + timedelta(hours=7))
        assert dc._effective_expiry(row) == NOW + timedelta(hours=7)

    def test_naive_db_datetimes_are_treated_as_utc(self):
        row = SimpleNamespace(
            token_issued_at=datetime(2026, 9, 3, 6, 0, 0),
            token_expires_at=datetime(2026, 9, 3, 9, 0, 0),
        )
        eff = dc._effective_expiry(row)
        assert eff == datetime(2026, 9, 3, 9, 0, 0, tzinfo=UTC)
        assert eff.tzinfo is not None


# ══════════════════════════════════════════════════════════════════════════
# _fernet / _mask
# ══════════════════════════════════════════════════════════════════════════
class TestFernetAndMask:
    def test_unset_key_raises_clear_runtime_error(self, monkeypatch):
        monkeypatch.setattr(config, "DHAN_CREDENTIAL_ENC_KEY", "")
        with pytest.raises(RuntimeError, match="DHAN_CREDENTIAL_ENC_KEY not configured"):
            dc._fernet()

    def test_malformed_key_is_rejected_by_fernet(self, monkeypatch):
        monkeypatch.setattr(config, "DHAN_CREDENTIAL_ENC_KEY", "not-a-real-fernet-key")
        with pytest.raises(ValueError):
            dc._fernet()

    def test_valid_key_round_trips(self, enc_key):
        f = dc._fernet()
        assert f.decrypt(f.encrypt(b"hello")) == b"hello"

    @pytest.mark.parametrize(
        "raw, expected",
        [
            (None, ""),
            ("", ""),
            ("7", "*"),
            ("1234", "****"),          # <=4 chars: fully starred, never reveals a short id
            ("12345", "*2345"),
            ("1100123456", "******3456"),
            ("  1100123456  ", "******3456"),   # surrounding whitespace stripped first
        ],
    )
    def test_mask(self, raw, expected):
        assert dc._mask(raw) == expected


# ══════════════════════════════════════════════════════════════════════════
# save_credentials
# ══════════════════════════════════════════════════════════════════════════
class TestSaveCredentials:
    def test_inserts_encrypted_row(self, db, enc_key, frozen):
        row = dc.save_credentials(db, CLIENT_ID, TOKEN)
        assert row.id is not None
        assert row.dhan_client_id_masked == "******3456"
        assert as_aware(row.token_issued_at) == NOW
        assert row.updated_at is not None
        f = Fernet(KEY.encode())
        assert f.decrypt(row.access_token_encrypted.encode()).decode() == TOKEN
        assert f.decrypt(row.dhan_client_id_encrypted.encode()).decode() == CLIENT_ID

    def test_plaintext_never_lands_in_any_column(self, db, enc_key):
        dc.save_credentials(db, CLIENT_ID, TOKEN)
        db.expire_all()
        r = db.query(models.TradeCredential).one()
        for col in (r.dhan_client_id_masked, r.dhan_client_id_encrypted, r.access_token_encrypted):
            assert TOKEN not in col
            assert CLIENT_ID not in col

    def test_without_real_expiry_uses_the_lifetime_guess(self, db, enc_key, frozen):
        row = dc.save_credentials(db, CLIENT_ID, TOKEN)
        assert as_aware(row.token_expires_at) == NOW + timedelta(hours=dc.DHAN_TOKEN_LIFETIME_HOURS)

    def test_real_expiry_from_dhan_is_stored(self, db, enc_key, frozen):
        real = NOW + timedelta(hours=5)
        row = dc.save_credentials(db, CLIENT_ID, TOKEN, real_expires_at=real)
        assert as_aware(row.token_expires_at) == real

    def test_naive_real_expiry_is_stamped_utc(self, db, enc_key, frozen):
        row = dc.save_credentials(db, CLIENT_ID, TOKEN, real_expires_at=datetime(2026, 9, 3, 11, 0, 0))
        assert as_aware(row.token_expires_at) == datetime(2026, 9, 3, 11, 0, 0, tzinfo=UTC)

    def test_second_save_overwrites_the_single_row(self, db, enc_key):
        dc.save_credentials(db, "1100000001", "old-token")
        dc.save_credentials(db, CLIENT_ID, "new-token")
        db.expire_all()
        assert db.query(models.TradeCredential).count() == 1
        assert dc.get_decrypted_credentials(db) == (CLIENT_ID, "new-token")

    def test_no_key_raises_and_persists_nothing(self, db, monkeypatch):
        monkeypatch.setattr(config, "DHAN_CREDENTIAL_ENC_KEY", "")
        with pytest.raises(RuntimeError):
            dc.save_credentials(db, CLIENT_ID, TOKEN)
        assert db.query(models.TradeCredential).count() == 0


# ══════════════════════════════════════════════════════════════════════════
# get_decrypted_credentials
# ══════════════════════════════════════════════════════════════════════════
class TestGetDecryptedCredentials:
    def test_no_row_returns_none(self, db, enc_key):
        assert dc.get_decrypted_credentials(db) is None

    def test_row_without_token_returns_none(self, db, enc_key):
        db.add(models.TradeCredential(access_token_encrypted=""))
        db.commit()
        assert dc.get_decrypted_credentials(db) is None

    def test_round_trip(self, db, enc_key):
        dc.save_credentials(db, CLIENT_ID, TOKEN)
        assert dc.get_decrypted_credentials(db) == (CLIENT_ID, TOKEN)

    def test_rotated_key_fails_closed_with_error_log(self, db, monkeypatch, caplog):
        monkeypatch.setattr(config, "DHAN_CREDENTIAL_ENC_KEY", KEY)
        dc.save_credentials(db, CLIENT_ID, TOKEN)
        monkeypatch.setattr(config, "DHAN_CREDENTIAL_ENC_KEY", OTHER_KEY)
        with caplog.at_level(logging.ERROR, logger=LOGGER):
            assert dc.get_decrypted_credentials(db) is None
        assert "decrypt failed" in caplog.text
        assert TOKEN not in caplog.text

    def test_corrupted_blob_fails_closed(self, db, enc_key):
        dc.save_credentials(db, CLIENT_ID, TOKEN)
        r = db.query(models.TradeCredential).one()
        r.access_token_encrypted = "this-is-not-a-fernet-token"
        db.commit()
        assert dc.get_decrypted_credentials(db) is None

    def test_unset_key_raises_rather_than_returning_none(self, db, monkeypatch):
        # Pinned as CURRENT behaviour (see session note observation): only an
        # InvalidToken is turned into None; a missing key is a loud
        # RuntimeError. config.validate() flags the missing key at boot.
        monkeypatch.setattr(config, "DHAN_CREDENTIAL_ENC_KEY", KEY)
        dc.save_credentials(db, CLIENT_ID, TOKEN)
        monkeypatch.setattr(config, "DHAN_CREDENTIAL_ENC_KEY", "")
        with pytest.raises(RuntimeError):
            dc.get_decrypted_credentials(db)


# ══════════════════════════════════════════════════════════════════════════
# connection_status
# ══════════════════════════════════════════════════════════════════════════
NOT_CONNECTED = {
    "connected": False,
    "client_id_masked": None,
    "token_expires_at": None,
    "token_valid": False,
    "days_remaining": None,
    "hours_remaining": None,
    "seconds_remaining": None,
}


class TestConnectionStatus:
    def test_no_row_exact_shape(self, db):
        assert dc.connection_status(db) == NOT_CONNECTED

    def test_row_without_token_counts_as_not_connected(self, db):
        db.add(models.TradeCredential(access_token_encrypted=None))
        db.commit()
        assert dc.connection_status(db) == NOT_CONNECTED

    def test_connected_shape_and_countdown(self, db, enc_key, frozen):
        dc.save_credentials(db, CLIENT_ID, TOKEN)
        s = dc.connection_status(db)
        assert s["connected"] is True
        assert s["client_id_masked"] == "******3456"
        assert s["token_valid"] is True
        assert s["token_hard_cap_hours"] == 24.0
        assert s["seconds_remaining"] == 86400
        assert s["hours_remaining"] == 24.0
        assert s["days_remaining"] == 1.0
        assert s["token_issued_at"] == "2026-09-03T06:00:00+00:00"
        assert s["token_expires_at"] == "2026-09-04T06:00:00+00:00"

    def test_countdown_moves_with_the_clock(self, db, enc_key, monkeypatch):
        _freeze(monkeypatch, NOW)
        dc.save_credentials(db, CLIENT_ID, TOKEN)
        _freeze(monkeypatch, NOW + timedelta(hours=6))
        s = dc.connection_status(db)
        assert s["seconds_remaining"] == 18 * 3600
        assert s["hours_remaining"] == 18.0
        assert s["days_remaining"] == 0.8  # round(0.75, 1)

    def test_stale_30_day_row_shows_the_24h_cap_not_30_days(self, db, frozen):
        _row(db, issued=NOW, expires=NOW + timedelta(days=30))
        s = dc.connection_status(db)
        assert s["seconds_remaining"] == 86400
        assert s["token_expires_at"] == "2026-09-04T06:00:00+00:00"

    def test_expired_token_reports_invalid_and_zero_seconds(self, db, frozen):
        _row(db, issued=NOW - timedelta(hours=27), expires=NOW - timedelta(hours=3))
        s = dc.connection_status(db)
        assert s["connected"] is True
        assert s["token_valid"] is False
        assert s["seconds_remaining"] == 0

    def test_row_with_no_expiry_is_connected_but_invalid(self, db, frozen):
        _row(db, issued=NOW, expires=None)
        s = dc.connection_status(db)
        assert s["connected"] is True
        assert s["token_valid"] is False
        assert s["token_expires_at"] is None
        assert s["seconds_remaining"] is None
        assert s["days_remaining"] is None
        assert s["hours_remaining"] is None

    def test_row_without_issued_at_omits_issued_timestamp(self, db, frozen):
        _row(db, issued=None, expires=NOW + timedelta(hours=2))
        s = dc.connection_status(db)
        assert s["token_issued_at"] is None
        assert s["seconds_remaining"] == 7200

    def test_payload_never_contains_a_secret(self, db, enc_key):
        dc.save_credentials(db, CLIENT_ID, TOKEN)
        blob = json.dumps(dc.connection_status(db))
        assert TOKEN not in blob
        assert CLIENT_ID not in blob
        assert "encrypted" not in blob


# ══════════════════════════════════════════════════════════════════════════
# is_token_valid / token_needs_refresh
# ══════════════════════════════════════════════════════════════════════════
class TestIsTokenValid:
    def test_none_row(self):
        assert dc.is_token_valid(None) is False

    def test_no_expiry(self):
        assert dc.is_token_valid(SimpleNamespace(token_expires_at=None, token_issued_at=NOW)) is False

    def test_valid_and_expired(self, frozen):
        ok = SimpleNamespace(token_issued_at=NOW, token_expires_at=NOW + timedelta(hours=1))
        old = SimpleNamespace(token_issued_at=NOW - timedelta(hours=5), token_expires_at=NOW - timedelta(seconds=1))
        assert dc.is_token_valid(ok) is True
        assert dc.is_token_valid(old) is False

    def test_exact_expiry_instant_is_already_invalid(self, frozen):
        row = SimpleNamespace(token_issued_at=NOW - timedelta(hours=2), token_expires_at=NOW)
        assert dc.is_token_valid(row) is False

    def test_hard_cap_not_the_stored_expiry_is_what_enforces_24h(self, frozen):
        # Issued 25h ago, stored expiry still 29 days out (stale misconfigured
        # row). Trusting token_expires_at alone would call this valid; Dhan
        # itself would already have killed it.
        row = SimpleNamespace(token_issued_at=NOW - timedelta(hours=25), token_expires_at=NOW + timedelta(days=29))
        assert dc.is_token_valid(row) is False

    def test_missing_issued_at_uses_stored_expiry(self, frozen):
        row = SimpleNamespace(token_issued_at=None, token_expires_at=NOW + timedelta(hours=1))
        assert dc.is_token_valid(row) is True


class TestTokenNeedsRefresh:
    @pytest.fixture(autouse=True)
    def _margin(self, monkeypatch):
        monkeypatch.setattr(config, "DHAN_TOTP_REFRESH_MARGIN_HOURS", 2.0)

    def test_no_row_needs_refresh(self, db):
        assert dc.token_needs_refresh(db) is True

    def test_row_without_token_needs_refresh(self, db):
        db.add(models.TradeCredential(access_token_encrypted=""))
        db.commit()
        assert dc.token_needs_refresh(db) is True

    def test_row_without_expiry_needs_refresh(self, db):
        _row(db, issued=NOW, expires=None)
        assert dc.token_needs_refresh(db) is True

    def test_plenty_of_life_left(self, db, frozen):
        _row(db, issued=NOW, expires=NOW + timedelta(hours=24))
        assert dc.token_needs_refresh(db) is False

    def test_inside_margin(self, db, frozen):
        _row(db, issued=NOW - timedelta(hours=22, minutes=30), expires=NOW + timedelta(hours=1, minutes=30))
        assert dc.token_needs_refresh(db) is True

    def test_exact_margin_boundary(self, db, monkeypatch):
        _row(db, issued=NOW - timedelta(hours=22), expires=NOW + timedelta(hours=2))
        _freeze(monkeypatch, NOW)  # expiry - margin == now  -> due (>=)
        assert dc.token_needs_refresh(db) is True
        _freeze(monkeypatch, NOW - timedelta(microseconds=1))  # one tick earlier -> not yet
        assert dc.token_needs_refresh(db) is False

    def test_stale_30_day_row_is_judged_on_the_hard_cap(self, db, frozen):
        # Stored expiry says 29 days left; issued 23h ago means the real
        # (capped) expiry is 1h away — inside the 2h margin.
        _row(db, issued=NOW - timedelta(hours=23), expires=NOW + timedelta(days=29))
        assert dc.token_needs_refresh(db) is True


# ══════════════════════════════════════════════════════════════════════════
# enforce_live_token
# ══════════════════════════════════════════════════════════════════════════
def _live(monkeypatch, result):
    monkeypatch.setattr(dhan_client, "verify_token_live", lambda db: result)


class TestEnforceLiveToken:
    def test_token_ok_touches_nothing(self, db, monkeypatch):
        _live(monkeypatch, (True, None))
        _gate(db, armed=True, connected=True)
        assert dc.enforce_live_token(db) == (True, None)
        g = _fresh_gate(db)
        assert g.armed is True and g.dhan_connected is True

    @pytest.mark.parametrize(
        "err",
        ["429 Too Many Requests", "Connection reset by peer", "DH-904: rate limit exceeded", "timeout", ""],
    )
    def test_transient_error_never_disarms_on_a_guess(self, db, monkeypatch, err):
        _live(monkeypatch, (False, err))
        _gate(db, armed=True, connected=True)
        assert dc.enforce_live_token(db) == (True, None)
        g = _fresh_gate(db)
        assert g.armed is True and g.dhan_connected is True

    def test_failure_with_no_message_is_treated_as_transient(self, db, monkeypatch):
        _live(monkeypatch, (False, None))
        _gate(db, armed=True, connected=True)
        assert dc.enforce_live_token(db) == (True, None)
        assert _fresh_gate(db).armed is True

    @pytest.mark.parametrize(
        "err",
        [
            "Invalid Access Token",
            "invalid token",
            "DH-901: Invalid_Authentication",
            "DH-902 no API access",
            "dh-905 input exception",
            "Token expired",
            "token has expired",
            "401 Unauthorized",
            "Authentication Failed",
        ],
    )
    def test_every_auth_marker_disarms_real(self, db, monkeypatch, caplog, err):
        _live(monkeypatch, (False, err))
        _gate(db, armed=True, connected=True)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert dc.enforce_live_token(db) == (False, err)
        g = _fresh_gate(db)
        assert g.armed is False
        assert g.dhan_connected is False
        assert g.disarmed_reason == f"Dhan rejected the token on a live check: {err}"
        assert g.updated_at is not None
        assert "auto-disarmed" in caplog.text

    def test_disarmed_reason_is_truncated_to_200_chars_of_error(self, db, monkeypatch):
        err = "Invalid Access Token " + "x" * 500
        _live(monkeypatch, (False, err))
        _gate(db, armed=True, connected=True)
        assert dc.enforce_live_token(db) == (False, err)   # full text still returned to the caller
        reason = _fresh_gate(db).disarmed_reason
        assert reason == "Dhan rejected the token on a live check: " + err[:200]
        assert len(reason) <= 255  # fits trade_gate_state.disarmed_reason String(255)

    def test_already_disarmed_still_clears_dhan_connected_but_logs_no_warning(self, db, monkeypatch, caplog):
        _live(monkeypatch, (False, "Invalid Access Token"))
        _gate(db, armed=False, connected=True)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert dc.enforce_live_token(db) == (False, "Invalid Access Token")
        g = _fresh_gate(db)
        assert g.dhan_connected is False and g.armed is False
        assert "auto-disarmed" not in caplog.text

    def test_auth_error_without_a_gate_row_still_reports_failure(self, db, monkeypatch):
        _live(monkeypatch, (False, "Invalid Access Token"))
        assert dc.enforce_live_token(db) == (False, "Invalid Access Token")
        assert db.query(models.TradeGateState).count() == 0

    def test_only_the_requested_mode_is_disarmed(self, db, monkeypatch):
        _live(monkeypatch, (False, "Invalid Access Token"))
        _gate(db, "REAL", armed=True, connected=True)
        _gate(db, "DEMO", armed=True, connected=True)
        dc.enforce_live_token(db)  # default mode is REAL
        assert _fresh_gate(db, "REAL").armed is False
        demo = _fresh_gate(db, "DEMO")
        assert demo.armed is True and demo.dhan_connected is True

    def test_explicit_mode_argument_targets_that_gate(self, db, monkeypatch):
        _live(monkeypatch, (False, "Invalid Access Token"))
        _gate(db, "REAL", armed=True, connected=True)
        _gate(db, "DEMO", armed=True, connected=True)
        dc.enforce_live_token(db, "DEMO")
        assert _fresh_gate(db, "DEMO").armed is False
        assert _fresh_gate(db, "REAL").armed is True


# ══════════════════════════════════════════════════════════════════════════
# disarm_on_invalid_ip
# ══════════════════════════════════════════════════════════════════════════
class TestDisarmOnInvalidIp:
    def test_no_gate_returns_false(self, db):
        assert dc.disarm_on_invalid_ip(db, "REAL", "DH-907 invalid ip") is False

    def test_armed_gate_is_disarmed_and_reports_true(self, db, caplog):
        _gate(db, armed=True, connected=True)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert dc.disarm_on_invalid_ip(db, "REAL", "DH-907 invalid ip") is True
        g = _fresh_gate(db)
        assert g.armed is False
        assert g.dhan_connected is True   # IP problem is not a dead token — connection flag untouched
        assert "outbound IP not whitelisted: DH-907 invalid ip" in g.disarmed_reason
        assert "GET /dhan/network-check" in g.disarmed_reason
        assert "IP Whitelisting" in g.disarmed_reason
        assert g.updated_at is not None
        assert "unwhitelisted IP" in caplog.text

    def test_already_disarmed_returns_false_but_refreshes_reason(self, db, caplog):
        _gate(db, armed=False, connected=True)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert dc.disarm_on_invalid_ip(db, "REAL", "DH-908") is False
        assert "DH-908" in _fresh_gate(db).disarmed_reason
        assert "unwhitelisted IP" not in caplog.text

    def test_long_error_is_truncated_and_none_is_safe(self, db):
        _gate(db, armed=True)
        dc.disarm_on_invalid_ip(db, "REAL", "E" * 400)
        reason = _fresh_gate(db).disarmed_reason
        assert "E" * 200 in reason and "E" * 201 not in reason
        _gate(db, "DEMO", armed=True)
        assert dc.disarm_on_invalid_ip(db, "DEMO", None) is True

    def test_other_mode_is_left_alone(self, db):
        _gate(db, "REAL", armed=True)
        _gate(db, "DEMO", armed=True)
        dc.disarm_on_invalid_ip(db, "REAL", "DH-907")
        assert _fresh_gate(db, "DEMO").armed is True


# ══════════════════════════════════════════════════════════════════════════
# _parse_dhan_expiry
# ══════════════════════════════════════════════════════════════════════════
class TestParseDhanExpiry:
    def test_none_returns_none_without_logging(self, caplog):
        with caplog.at_level(logging.INFO, logger=LOGGER):
            assert dc._parse_dhan_expiry(None) is None
        assert caplog.text == ""

    def test_raw_value_is_logged_once_so_the_format_can_be_confirmed(self, caplog):
        with caplog.at_level(logging.INFO, logger=LOGGER):
            dc._parse_dhan_expiry("2026-09-04T11:11:11.614")
        assert "Dhan expiryTime raw value: '2026-09-04T11:11:11.614' (type str)" in caplog.text

    @pytest.mark.parametrize("raw", [1788498671, 1788498671.0])
    def test_epoch_seconds(self, raw):
        assert dc._parse_dhan_expiry(raw) == datetime.fromtimestamp(1788498671, tz=UTC)

    def test_epoch_milliseconds_give_the_same_instant(self):
        assert dc._parse_dhan_expiry(1788498671000) == dc._parse_dhan_expiry(1788498671)

    def test_confirmed_real_format_bare_iso_is_IST_not_UTC(self):
        # CONFIRMED 2026-09-03 against a real Dhan response. Stamping this as
        # UTC (the original bug) is wrong by +5:30.
        got = dc._parse_dhan_expiry("2026-09-04T11:11:11.614")
        assert got == datetime(2026, 9, 4, 5, 41, 11, 614000, tzinfo=UTC)
        assert got.utcoffset() == timedelta(0)

    def test_iso_with_z_is_utc_as_is(self):
        assert dc._parse_dhan_expiry("2026-09-04T11:11:11Z") == datetime(2026, 9, 4, 11, 11, 11, tzinfo=UTC)

    def test_iso_with_explicit_offset_is_converted(self):
        assert dc._parse_dhan_expiry("2026-09-04T11:11:11+05:30") == datetime(2026, 9, 4, 5, 41, 11, tzinfo=UTC)

    def test_space_separated_form_is_treated_as_IST(self):
        assert dc._parse_dhan_expiry("2026-09-04 11:11:11") == datetime(2026, 9, 4, 5, 41, 11, tzinfo=UTC)

    def test_non_zero_padded_space_form_only_the_strptime_fallback_can_read(self):
        # fromisoformat() insists on zero-padded fields, strptime() does not —
        # this is the one input shape that reaches the second parser.
        with pytest.raises(ValueError):
            datetime.fromisoformat("2026-9-4 1:2:3")
        assert dc._parse_dhan_expiry("2026-9-4 11:1:1") == datetime(2026, 9, 4, 5, 31, 1, tzinfo=UTC)

    def test_surrounding_whitespace_is_tolerated(self):
        assert dc._parse_dhan_expiry("  2026-09-04T11:11:11Z  ") == datetime(2026, 9, 4, 11, 11, 11, tzinfo=UTC)

    @pytest.mark.parametrize("raw", ["", "   "])
    def test_blank_string_returns_none_quietly(self, raw, caplog):
        with caplog.at_level(logging.ERROR, logger=LOGGER):
            assert dc._parse_dhan_expiry(raw) is None
        assert "Could not parse" not in caplog.text   # blank is "no data", not a parse failure

    @pytest.mark.parametrize("raw", ["tomorrow-ish", "04/09/2026 11:11", "[1, 2]", "2026-13-45T99:99:99"])
    def test_unparseable_returns_none_and_logs_error(self, raw, caplog):
        with caplog.at_level(logging.ERROR, logger=LOGGER):
            assert dc._parse_dhan_expiry(raw) is None
        assert "Could not parse Dhan expiryTime" in caplog.text

    @pytest.mark.parametrize("raw", [float("nan"), 1e30, float("inf")])
    def test_absurd_numbers_never_raise(self, raw, caplog):
        with caplog.at_level(logging.ERROR, logger=LOGGER):
            assert dc._parse_dhan_expiry(raw) is None
        assert "Unexpected error parsing Dhan expiryTime" in caplog.text


# ══════════════════════════════════════════════════════════════════════════
# _redact_secrets / _heal_session (session96 helpers)
# ══════════════════════════════════════════════════════════════════════════
class TestRedactSecrets:
    def test_real_httpx_status_error_text_is_scrubbed(self):
        req = httpx.Request("POST", "https://auth.dhan.co/app/generateAccessToken",
                            params={"dhanClientId": CLIENT_ID, "pin": PIN, "totp": "123456"})
        with pytest.raises(httpx.HTTPStatusError) as ei:
            httpx.Response(401, request=req).raise_for_status()
        # Precondition: this IS the leak — the raw text carries every secret.
        raw = str(ei.value)
        assert PIN in raw and CLIENT_ID in raw and "123456" in raw
        safe = dc._redact_secrets(ei.value)
        assert PIN not in safe and CLIENT_ID not in safe and "123456" not in safe
        assert "dhanClientId=***&pin=***&totp=***" in safe
        assert "401 Unauthorized" in safe                      # the useful part survives
        assert "auth.dhan.co/app/generateAccessToken" in safe

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("PIN=1234&x=1", "PIN=***&x=1"),                        # case-insensitive
            ("access_token=abc.def&y=2", "access_token=***&y=2"),
            ("access-token=abc.def", "access-token=***"),
            ("accesstoken=abc", "accesstoken=***"),
            ("url?totp=555555'\nmore", "url?totp=***'\nmore"),      # stops at the quote
            ('url?pin=777777"', 'url?pin=***"'),
        ],
    )
    def test_query_parameter_values(self, text, expected):
        assert dc._redact_secrets(text) == expected

    @pytest.mark.parametrize("text", ["spin=12", "shipping=99", "plain error text", "", "pins=3"])
    def test_lookalike_words_and_plain_text_are_untouched(self, text):
        assert dc._redact_secrets(text) == text

    def test_literal_secrets_are_replaced_everywhere(self):
        assert dc._redact_secrets("bad 987654 and again 987654", "987654") == "bad *** and again ***"

    def test_short_literals_are_ignored_so_ordinary_numbers_survive(self):
        assert dc._redact_secrets("HTTP 401 for 123", "123", "40") == "HTTP 401 for 123"

    def test_empty_and_none_secrets_are_ignored(self):
        assert dc._redact_secrets("nothing to hide", "", None) == "nothing to hide"

    def test_accepts_exceptions_and_never_raises_on_hostile_objects(self):
        assert dc._redact_secrets(ValueError("pin=1234 bad")) == "pin=*** bad"

        class Hostile:
            def __str__(self):
                raise RuntimeError("no str for you")

        assert "withheld" in dc._redact_secrets(Hostile())


class TestHealSession:
    def test_healthy_session_is_left_untouched(self, db, enc_key):
        # Pending, uncommitted caller state must NOT be discarded.
        pending = models.TradeCredential(dhan_client_id_masked="pending")
        db.add(pending)
        assert db.is_active
        dc._heal_session(db)
        assert pending in db.new

    def test_failed_flush_is_rolled_back_so_the_session_is_usable_again(self, db, engine, enc_key):
        engine._fail_on = {"trade_credentials"}
        with pytest.raises(OperationalError):
            dc.save_credentials(db, CLIENT_ID, TOKEN)
        assert not db.is_active
        with pytest.raises(PendingRollbackError):
            db.query(models.TradeCredential).first()      # the poisoned state
        dc._heal_session(db)
        assert db.is_active
        engine._fail_on = set()
        assert db.query(models.TradeCredential).first() is None

    def test_a_failing_rollback_is_swallowed_and_logged(self, caplog):
        class Broken:
            is_active = False

            def rollback(self):
                raise RuntimeError("connection gone")

        with caplog.at_level(logging.ERROR, logger=LOGGER):
            dc._heal_session(Broken())     # must not raise
        assert "rollback after failed Dhan credential write also failed" in caplog.text


# ══════════════════════════════════════════════════════════════════════════
# refresh_if_totp_enabled
# ══════════════════════════════════════════════════════════════════════════
GOOD_BODY = {"accessToken": TOKEN, "expiryTime": "2026-09-04T11:11:11.614"}


class TestRefreshGuards:
    def test_disabled_is_a_silent_noop(self, db, monkeypatch, totp_env, sent):
        # totp_env supplies a valid secret/client id/PIN, so the ONLY thing
        # standing between this call and a live auth.dhan.co request is the flag.
        monkeypatch.setattr(config, "DHAN_TOTP_ENABLED", False)
        rec = _post(monkeypatch, body=GOOD_BODY)
        assert dc.refresh_if_totp_enabled(db) is False
        assert rec.calls == [] and sent == []
        assert db.query(models.TradeCredential).count() == 0

    @pytest.mark.parametrize("missing", ["DHAN_TOTP_SECRET", "DHAN_CLIENT_ID"])
    def test_missing_secret_or_client_id_is_rejected_before_any_http(self, db, monkeypatch, totp_env, sent, caplog, missing):
        monkeypatch.delenv(missing)
        rec = _post(monkeypatch, body=GOOD_BODY)
        with caplog.at_level(logging.ERROR, logger=LOGGER):
            assert dc.refresh_if_totp_enabled(db) is False
        assert rec.calls == []
        assert "DHAN_TOTP_SECRET or DHAN_CLIENT_ID not set" in caplog.text

    def test_missing_pin_is_not_validated_and_is_sent_empty(self, db, monkeypatch, totp_env, sent, frozen):
        # Pinned CURRENT behaviour (session note observation): only the secret
        # and client id are guarded, so an unset DHAN_PIN goes out as pin="".
        monkeypatch.delenv("DHAN_PIN")
        rec = _post(monkeypatch, body=GOOD_BODY)
        assert dc.refresh_if_totp_enabled(db) is True
        assert rec.calls[0]["params"]["pin"] == ""


class TestRefreshHappyPath:
    def test_sends_the_exact_request_with_a_real_totp_code(self, db, monkeypatch, totp_env, sent, frozen):
        rec = _post(monkeypatch, body=GOOD_BODY)
        before = pyotp.TOTP(TOTP_SECRET).now()
        assert dc.refresh_if_totp_enabled(db) is True
        after = pyotp.TOTP(TOTP_SECRET).now()      # tolerate a 30s window rollover mid-test
        assert len(rec.calls) == 1
        call = rec.calls[0]
        assert call["url"] == "https://auth.dhan.co/app/generateAccessToken"
        assert call["timeout"] == 15.0
        assert call["params"]["dhanClientId"] == CLIENT_ID
        assert call["params"]["pin"] == PIN
        assert call["params"]["totp"] in {before, after}
        assert set(call["params"]) == {"dhanClientId", "pin", "totp"}

    def test_saves_encrypted_credentials_with_dhans_real_expiry(self, db, monkeypatch, totp_env, sent, frozen):
        _post(monkeypatch, body=GOOD_BODY)
        assert dc.refresh_if_totp_enabled(db) is True
        db.expire_all()
        assert dc.get_decrypted_credentials(db) == (CLIENT_ID, TOKEN)
        row = db.query(models.TradeCredential).one()
        assert as_aware(row.token_expires_at) == datetime(2026, 9, 4, 5, 41, 11, 614000, tzinfo=UTC)
        assert as_aware(row.token_issued_at) == NOW
        assert TOKEN not in row.access_token_encrypted

    def test_success_notification_never_contains_the_token(self, db, monkeypatch, totp_env, sent, frozen):
        _post(monkeypatch, body=GOOD_BODY)
        dc.refresh_if_totp_enabled(db)
        assert sent == ["🔑 *Dhan TOTP token refreshed* — new token saved."]

    def test_logs_response_keys_and_the_parsed_expiry(self, db, monkeypatch, totp_env, sent, frozen, caplog):
        _post(monkeypatch, body=GOOD_BODY)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            dc.refresh_if_totp_enabled(db)
        assert "Dhan TOTP refresh response keys: ['accessToken', 'expiryTime']" in caplog.text
        assert "Real expiry from Dhan: 2026-09-04T05:41:11.614000+00:00" in caplog.text
        assert TOKEN not in caplog.text

    @pytest.mark.parametrize(
        "body",
        [
            {"accessToken": TOKEN},
            {"access_token": TOKEN},
            {"data": {"accessToken": TOKEN}},
            {"data": {"access_token": TOKEN}},
            {"accessToken": None, "access_token": "", "data": {"accessToken": TOKEN}},
        ],
    )
    def test_every_token_field_shape_is_accepted(self, db, monkeypatch, totp_env, sent, frozen, body):
        _post(monkeypatch, body=body)
        assert dc.refresh_if_totp_enabled(db) is True
        assert dc.get_decrypted_credentials(db) == (CLIENT_ID, TOKEN)

    def test_nested_expiry_time_is_honoured(self, db, monkeypatch, totp_env, sent, frozen):
        _post(monkeypatch, body={"data": {"accessToken": TOKEN, "expiryTime": "2026-09-03T20:00:00Z"}})
        assert dc.refresh_if_totp_enabled(db) is True
        db.expire_all()
        row = db.query(models.TradeCredential).one()
        assert as_aware(row.token_expires_at) == datetime(2026, 9, 3, 20, 0, 0, tzinfo=UTC)

    def test_unparseable_expiry_falls_back_to_the_24h_guess(self, db, monkeypatch, totp_env, sent, frozen, caplog):
        _post(monkeypatch, body={"accessToken": TOKEN, "expiryTime": "sometime soon"})
        with caplog.at_level(logging.INFO, logger=LOGGER):
            assert dc.refresh_if_totp_enabled(db) is True
        row = db.query(models.TradeCredential).one()
        assert as_aware(row.token_expires_at) == NOW + timedelta(hours=dc.DHAN_TOKEN_LIFETIME_HOURS)
        assert "unparsed — used 24h lifetime guess" in caplog.text

    def test_missing_expiry_also_uses_the_guess(self, db, monkeypatch, totp_env, sent, frozen):
        _post(monkeypatch, body={"accessToken": TOKEN})
        dc.refresh_if_totp_enabled(db)
        row = db.query(models.TradeCredential).one()
        assert as_aware(row.token_expires_at) == NOW + timedelta(hours=dc.DHAN_TOKEN_LIFETIME_HOURS)

    def test_a_notifier_failure_does_not_undo_a_successful_refresh(self, db, monkeypatch, totp_env, frozen):
        def boom(text):
            raise RuntimeError("telegram down")

        monkeypatch.setattr(notifier, "notify_sync", boom)
        _post(monkeypatch, body=GOOD_BODY)
        assert dc.refresh_if_totp_enabled(db) is True
        assert dc.get_decrypted_credentials(db) == (CLIENT_ID, TOKEN)


class TestRefreshGateHealing:
    """2026-09-01 fix: a fresh token must clear a stale gate.dhan_connected=False
    (else /arm 409s forever) — but must NEVER re-arm anything itself."""

    def test_restores_dhan_connected_on_real_and_leaves_it_disarmed(self, db, monkeypatch, totp_env, sent, frozen):
        _gate(db, "REAL", armed=False, connected=False)
        _post(monkeypatch, body=GOOD_BODY)
        assert dc.refresh_if_totp_enabled(db) is True
        g = _fresh_gate(db)
        assert g.dhan_connected is True
        assert as_aware(g.dhan_connected_at) == NOW
        assert g.armed is False      # the admin still re-arms explicitly

    def test_already_connected_gate_is_not_rewritten(self, db, monkeypatch, totp_env, sent, frozen):
        g = _gate(db, "REAL", armed=True, connected=True)
        g.dhan_connected_at = datetime(2026, 9, 1, 3, 0, 0)
        db.commit()
        _post(monkeypatch, body=GOOD_BODY)
        dc.refresh_if_totp_enabled(db)
        after = _fresh_gate(db)
        assert after.dhan_connected_at == datetime(2026, 9, 1, 3, 0, 0)
        assert after.armed is True

    def test_demo_gate_is_never_touched(self, db, monkeypatch, totp_env, sent, frozen):
        _gate(db, "DEMO", armed=False, connected=False)
        _post(monkeypatch, body=GOOD_BODY)
        dc.refresh_if_totp_enabled(db)
        assert _fresh_gate(db, "DEMO").dhan_connected is False

    def test_no_gate_row_is_fine(self, db, monkeypatch, totp_env, sent, frozen):
        _post(monkeypatch, body=GOOD_BODY)
        assert dc.refresh_if_totp_enabled(db) is True

    def test_gate_heal_db_failure_is_non_fatal_and_leaves_the_session_usable(
        self, db, engine, monkeypatch, totp_env, sent, frozen, caplog
    ):
        # REGRESSION (session96): the swallowed gate-commit failure used to
        # leave the caller's Session poisoned -> PendingRollbackError on the
        # very next query in cycle_runner (enforce_live_token).
        _gate(db, "REAL", armed=False, connected=False)
        engine._fail_on = {"trade_gate_state"}
        _post(monkeypatch, body=GOOD_BODY)
        with caplog.at_level(logging.ERROR, logger=LOGGER):
            assert dc.refresh_if_totp_enabled(db) is True
        assert "Failed to restore gate.dhan_connected" in caplog.text
        assert db.is_active
        engine._fail_on = set()
        assert dc.get_decrypted_credentials(db) == (CLIENT_ID, TOKEN)   # new token WAS committed first
        assert _fresh_gate(db).dhan_connected is False
        assert sent == ["🔑 *Dhan TOTP token refreshed* — new token saved."]


class TestRefreshFailures:
    def test_response_without_a_token_field_returns_false_and_saves_nothing(
        self, db, monkeypatch, totp_env, sent, caplog
    ):
        _post(monkeypatch, body={"status": "error", "message": "no token here", "data": None})
        with caplog.at_level(logging.ERROR, logger=LOGGER):
            assert dc.refresh_if_totp_enabled(db) is False
        assert "no token field in response" in caplog.text
        assert db.query(models.TradeCredential).count() == 0
        assert sent == []   # pinned CURRENT behaviour: this path logs but does not Telegram

    def test_http_401_returns_false_and_alerts(self, db, monkeypatch, totp_env, sent):
        _post(monkeypatch, status=401, body={"error": "bad totp"})
        assert dc.refresh_if_totp_enabled(db) is False
        assert len(sent) == 1
        assert sent[0].startswith("🚨 *Dhan TOTP refresh FAILED*\n")
        assert "401 Unauthorized" in sent[0]
        assert db.query(models.TradeCredential).count() == 0

    def test_http_500_returns_false(self, db, monkeypatch, totp_env, sent):
        _post(monkeypatch, status=500, body={})
        assert dc.refresh_if_totp_enabled(db) is False
        assert "500 Internal Server Error" in sent[0]

    def test_pin_totp_and_client_id_never_reach_the_log_or_telegram(
        self, db, monkeypatch, totp_env, sent, caplog
    ):
        # REGRESSION (session96): httpx puts the full URL — including
        # dhanClientId, pin and the one-time totp — into HTTPStatusError's
        # message; the module used to log it and Telegram its first 300 chars.
        rec = _post(monkeypatch, status=401, body={"error": "bad totp"})
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            assert dc.refresh_if_totp_enabled(db) is False
        sent_totp = rec.calls[0]["params"]["totp"]
        everything = caplog.text + "\n".join(sent)
        for secret in (PIN, CLIENT_ID, sent_totp, TOTP_SECRET):
            assert secret not in everything, f"secret {secret!r} leaked"
        assert "dhanClientId=***&pin=***&totp=***" in sent[0]
        assert "dhanClientId=***&pin=***&totp=***" in caplog.text

    def test_a_literal_secret_echoed_without_a_key_prefix_is_also_redacted(
        self, db, monkeypatch, totp_env, sent, caplog
    ):
        _post(monkeypatch, exc=RuntimeError(f"gateway rejected pin {PIN} for client {CLIENT_ID}"))
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            assert dc.refresh_if_totp_enabled(db) is False
        assert PIN not in sent[0] and CLIENT_ID not in sent[0]
        assert PIN not in caplog.text and CLIENT_ID not in caplog.text
        assert "gateway rejected pin *** for client ***" in sent[0]

    def test_redaction_happens_before_the_300_char_cut(self, db, monkeypatch, totp_env, sent):
        # Cutting first could slice "pin=987654" into "pin=98" and dodge the
        # pattern; with redaction first the secret is gone whatever the length.
        long_prefix = "x" * 285
        _post(monkeypatch, exc=RuntimeError(f"{long_prefix}?pin={PIN}&totp=123456"))
        dc.refresh_if_totp_enabled(db)
        body = sent[0].split("\n", 1)[1]
        assert len(body) <= 300
        assert "pin=98" not in body and PIN not in body

    def test_alert_body_is_capped_at_300_chars(self, db, monkeypatch, totp_env, sent):
        _post(monkeypatch, exc=RuntimeError("E" * 1000))
        dc.refresh_if_totp_enabled(db)
        assert sent[0] == "🚨 *Dhan TOTP refresh FAILED*\n" + "E" * 300

    def test_network_error(self, db, monkeypatch, totp_env, sent):
        _post(monkeypatch, exc=httpx.ConnectError("name resolution failed"))
        assert dc.refresh_if_totp_enabled(db) is False
        assert "name resolution failed" in sent[0]

    def test_invalid_json_body(self, db, monkeypatch, totp_env, sent):
        _post(monkeypatch, status=200, content=b"<html>gateway error</html>")
        assert dc.refresh_if_totp_enabled(db) is False
        assert sent and sent[0].startswith("🚨")

    @pytest.mark.parametrize("payload", [[], "just a string", 42, {"data": "oops-a-string"}])
    def test_json_of_the_wrong_shape_is_a_clean_failure_not_a_crash(self, db, monkeypatch, totp_env, sent, payload):
        _post(monkeypatch, status=200, body=payload)
        assert dc.refresh_if_totp_enabled(db) is False
        assert db.query(models.TradeCredential).count() == 0

    def test_missing_pyotp_is_a_clean_failure(self, db, monkeypatch, totp_env, sent):
        monkeypatch.setitem(sys.modules, "pyotp", None)   # makes `import pyotp` raise ImportError
        rec = _post(monkeypatch, body=GOOD_BODY)
        assert dc.refresh_if_totp_enabled(db) is False
        assert rec.calls == []
        assert sent and sent[0].startswith("🚨")

    def test_invalid_totp_secret_is_a_clean_failure(self, db, monkeypatch, totp_env, sent):
        monkeypatch.setenv("DHAN_TOTP_SECRET", "!!! not base32 !!!")
        rec = _post(monkeypatch, body=GOOD_BODY)
        assert dc.refresh_if_totp_enabled(db) is False
        assert rec.calls == []
        assert "!!! not base32 !!!" not in sent[0]

    def test_missing_encryption_key_after_a_good_response_is_a_clean_failure(
        self, db, monkeypatch, totp_env, sent
    ):
        monkeypatch.setattr(config, "DHAN_CREDENTIAL_ENC_KEY", "")
        _post(monkeypatch, body=GOOD_BODY)
        assert dc.refresh_if_totp_enabled(db) is False
        assert "DHAN_CREDENTIAL_ENC_KEY not configured" in sent[0]
        assert TOKEN not in sent[0]

    def test_db_failure_saving_the_token_returns_false_and_heals_the_callers_session(
        self, db, engine, monkeypatch, totp_env, sent
    ):
        # REGRESSION (session96): used to return False but leave the caller's
        # Session in PendingRollbackError.
        engine._fail_on = {"trade_credentials"}
        _post(monkeypatch, body=GOOD_BODY)
        assert dc.refresh_if_totp_enabled(db) is False
        assert db.is_active
        engine._fail_on = set()
        assert db.query(models.TradeCredential).count() == 0    # usable AND nothing half-saved
        assert sent and "simulated database outage" in sent[0]

    def test_http_failure_does_not_discard_the_callers_pending_state(self, db, monkeypatch, totp_env, sent):
        # _heal_session must only act on a POISONED session.
        pending = models.TradeGateState(mode="DEMO")
        db.add(pending)
        _post(monkeypatch, exc=httpx.ConnectError("down"))
        assert dc.refresh_if_totp_enabled(db) is False
        assert pending in db.new

    def test_notifier_failure_on_the_failure_path_is_swallowed(self, db, monkeypatch, totp_env):
        def boom(text):
            raise RuntimeError("telegram down")

        monkeypatch.setattr(notifier, "notify_sync", boom)
        _post(monkeypatch, status=401, body={})
        assert dc.refresh_if_totp_enabled(db) is False
