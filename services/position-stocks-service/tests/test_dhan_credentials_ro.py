"""
tests/test_dhan_credentials_ro.py

Covers auth/dhan_credentials_ro.py (session112 round 12) — previously 46%,
32 missing lines: 62-67 (_fernet's missing-key RuntimeError), 74-87
(get_decrypted_credentials), 92-93 (is_connected), 101-106
(_effective_expiry), 118-138 (connection_status). Every caller in this
service (main.py's Dhan-status routes, startup logging) monkeypatches
these functions away instead of exercising the real ones, so this module
had zero direct coverage despite running on every "Dhan Account" card
render.

This is the READ-ONLY mirror of real-trade-service's owning
auth/dhan_credentials.py (see this module's own docstring: it must never
write to trade_credentials — there is no save_credentials() here). Test
shape mirrors the overlapping sections of real-trade-service's
tests/test_dhan_credentials.py (_effective_expiry, get_decrypted_credentials,
connection_status) but is written fresh rather than ported, since this
module's surface (no save/refresh/gate logic, plus an is_connected() this
service adds for its own cheap /health check) doesn't match one-to-one.

Uses a REAL in-memory SQLite table (this module's own _ROBase metadata —
never models.Base, per the module's own "never create_all() the owning
service's table" rule) and REAL Fernet encryption; only `datetime.now` is
frozen, via the same monkeypatch-the-module's-`datetime`-name technique
used elsewhere in this repo (e.g. real-trade-service's dhan_credentials
tests), since connection_status computes its own remaining-time math off
wall-clock `datetime.now(timezone.utc)`.

Run from services/position-stocks-service:
    python3 -m pytest tests/test_dhan_credentials_ro.py -q \\
        --cov=auth.dhan_credentials_ro --cov-report=term-missing
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
from auth import dhan_credentials_ro as dc

LOGGER = "position-stocks-dhan-auth-ro"

KEY = Fernet.generate_key().decode()
OTHER_KEY = Fernet.generate_key().decode()

CLIENT_ID = "1100123456"
TOKEN = "eyJ0b2tlbi1mcm9tLWRoYW4ifQ.SECRET-ACCESS-TOKEN"

UTC = timezone.utc
NOW = datetime(2026, 9, 3, 6, 0, 0, tzinfo=UTC)


# ── fixtures / helpers ────────────────────────────────────────────────────

@pytest.fixture()
def db():
    """Fresh in-memory DB per test, using this module's OWN read-only
    metadata (_ROBase) — never models.Base, matching the module's own
    contract that it never owns or creates this table in production."""
    engine = create_engine("sqlite:///:memory:")
    dc._ROBase.metadata.create_all(engine)
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


def _row(db, *, issued=None, expires=None, token="x", masked="****3456", client_id_enc="x"):
    r = dc.TradeCredentialRO(
        dhan_client_id_masked=masked,
        dhan_client_id_encrypted=client_id_enc,
        access_token_encrypted=token,
        token_issued_at=issued,
        token_expires_at=expires,
        updated_at=NOW,
    )
    db.add(r)
    db.commit()
    return r


def _seed_encrypted(db, *, issued=None, expires=None, client_id=CLIENT_ID, token=TOKEN, key=KEY):
    f = Fernet(key.encode())
    return _row(
        db,
        issued=issued,
        expires=expires,
        token=f.encrypt(token.encode()).decode(),
        client_id_enc=f.encrypt(client_id.encode()).decode(),
        masked="******" + client_id[-4:],
    )


# ══════════════════════════════════════════════════════════════════════════
# _fernet
# ══════════════════════════════════════════════════════════════════════════

class TestFernet:
    def test_unset_key_raises_runtime_error(self, monkeypatch):
        monkeypatch.setattr(config, "DHAN_CREDENTIAL_ENC_KEY", "")
        with pytest.raises(RuntimeError, match="DHAN_CREDENTIAL_ENC_KEY"):
            dc._fernet()

    def test_none_key_also_raises(self, monkeypatch):
        monkeypatch.setattr(config, "DHAN_CREDENTIAL_ENC_KEY", None)
        with pytest.raises(RuntimeError):
            dc._fernet()

    def test_configured_key_returns_a_working_fernet(self, enc_key):
        f = dc._fernet()
        assert f.decrypt(f.encrypt(b"round-trip")) == b"round-trip"


# ══════════════════════════════════════════════════════════════════════════
# _effective_expiry
# ══════════════════════════════════════════════════════════════════════════

class TestEffectiveExpiry:
    def test_no_expiry_returns_none(self):
        row = dc.TradeCredentialRO(token_expires_at=None, token_issued_at=NOW)
        assert dc._effective_expiry(row) is None

    def test_missing_issued_at_falls_back_to_stored_expiry_as_utc(self):
        row = dc.TradeCredentialRO(
            token_expires_at=datetime(2026, 9, 4, 1, 2, 3), token_issued_at=None,
        )
        assert dc._effective_expiry(row) == datetime(2026, 9, 4, 1, 2, 3, tzinfo=UTC)

    def test_stale_30_day_row_is_clamped_to_24h_hard_cap(self):
        row = dc.TradeCredentialRO(token_issued_at=NOW, token_expires_at=NOW + timedelta(days=30))
        assert dc._effective_expiry(row) == NOW + timedelta(hours=24)

    def test_shorter_real_expiry_is_kept(self):
        row = dc.TradeCredentialRO(token_issued_at=NOW, token_expires_at=NOW + timedelta(hours=7))
        assert dc._effective_expiry(row) == NOW + timedelta(hours=7)

    def test_naive_db_datetimes_are_treated_as_utc(self):
        row = dc.TradeCredentialRO(
            token_issued_at=datetime(2026, 9, 3, 6, 0, 0),
            token_expires_at=datetime(2026, 9, 3, 9, 0, 0),
        )
        eff = dc._effective_expiry(row)
        assert eff == datetime(2026, 9, 3, 9, 0, 0, tzinfo=UTC)
        assert eff.tzinfo is not None


# ══════════════════════════════════════════════════════════════════════════
# get_decrypted_credentials
# ══════════════════════════════════════════════════════════════════════════

class TestGetDecryptedCredentials:
    def test_no_row_returns_none(self, db, enc_key):
        assert dc.get_decrypted_credentials(db) is None

    def test_row_without_token_returns_none(self, db, enc_key):
        _row(db, token="")
        assert dc.get_decrypted_credentials(db) is None

    def test_round_trip(self, db, enc_key):
        _seed_encrypted(db)
        assert dc.get_decrypted_credentials(db) == (CLIENT_ID, TOKEN)

    def test_rotated_key_fails_closed_with_error_log(self, db, monkeypatch, caplog):
        monkeypatch.setattr(config, "DHAN_CREDENTIAL_ENC_KEY", KEY)
        _seed_encrypted(db, key=KEY)
        monkeypatch.setattr(config, "DHAN_CREDENTIAL_ENC_KEY", OTHER_KEY)
        with caplog.at_level(logging.ERROR, logger=LOGGER):
            assert dc.get_decrypted_credentials(db) is None
        assert "decrypt failed" in caplog.text
        assert TOKEN not in caplog.text

    def test_corrupted_blob_fails_closed(self, db, enc_key):
        _seed_encrypted(db)
        r = db.query(dc.TradeCredentialRO).one()
        r.access_token_encrypted = "this-is-not-a-fernet-token"
        db.commit()
        assert dc.get_decrypted_credentials(db) is None

    def test_unset_key_raises_rather_than_returning_none(self, db, monkeypatch):
        # Same pinned contract as the owning service's module: only an
        # InvalidToken (wrong/rotated key) is turned into None; a missing
        # key is a loud RuntimeError.
        monkeypatch.setattr(config, "DHAN_CREDENTIAL_ENC_KEY", KEY)
        _seed_encrypted(db, key=KEY)
        monkeypatch.setattr(config, "DHAN_CREDENTIAL_ENC_KEY", "")
        with pytest.raises(RuntimeError):
            dc.get_decrypted_credentials(db)


# ══════════════════════════════════════════════════════════════════════════
# is_connected
# ══════════════════════════════════════════════════════════════════════════

class TestIsConnected:
    def test_no_row_is_false(self, db):
        assert dc.is_connected(db) is False

    def test_row_without_token_is_false(self, db):
        _row(db, token=None)
        assert dc.is_connected(db) is False

    def test_row_with_empty_token_is_false(self, db):
        _row(db, token="")
        assert dc.is_connected(db) is False

    def test_row_with_token_is_true(self, db):
        _row(db, token="anything-nonempty")
        assert dc.is_connected(db) is True

    def test_does_not_attempt_decryption(self, db, monkeypatch):
        # Cheap check contract: must work even with NO key configured and a
        # blob that wouldn't decrypt, since it never calls _fernet().
        monkeypatch.setattr(config, "DHAN_CREDENTIAL_ENC_KEY", "")
        _row(db, token="not-a-real-fernet-token")
        assert dc.is_connected(db) is True


# ══════════════════════════════════════════════════════════════════════════
# connection_status
# ══════════════════════════════════════════════════════════════════════════

NOT_CONNECTED = {
    "connected": False,
    "client_id_masked": None,
    "token_issued_at": None,
    "token_expires_at": None,
    "token_valid": False,
    "token_hard_cap_hours": 24.0,
    "days_remaining": None,
    "hours_remaining": None,
    "seconds_remaining": None,
}


class TestConnectionStatus:
    def test_no_row_exact_shape(self, db):
        assert dc.connection_status(db) == NOT_CONNECTED

    def test_row_without_token_counts_as_not_connected(self, db):
        _row(db, token=None)
        assert dc.connection_status(db) == NOT_CONNECTED

    def test_connected_shape_and_countdown(self, db, frozen):
        _row(db, issued=NOW, expires=NOW + timedelta(hours=24), token="tok", masked="******3456")
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

    def test_countdown_moves_with_the_clock(self, db, monkeypatch):
        _freeze(monkeypatch, NOW)
        _row(db, issued=NOW, expires=NOW + timedelta(hours=24), token="tok")
        _freeze(monkeypatch, NOW + timedelta(hours=6))
        s = dc.connection_status(db)
        assert s["seconds_remaining"] == 18 * 3600
        assert s["hours_remaining"] == 18.0
        assert s["days_remaining"] == 0.8  # round(0.75, 1)

    def test_stale_30_day_row_shows_the_24h_cap_not_30_days(self, db, frozen):
        _row(db, issued=NOW, expires=NOW + timedelta(days=30), token="tok")
        s = dc.connection_status(db)
        assert s["seconds_remaining"] == 86400
        assert s["token_expires_at"] == "2026-09-04T06:00:00+00:00"

    def test_expired_token_reports_invalid_and_zero_seconds(self, db, frozen):
        _row(db, issued=NOW - timedelta(hours=27), expires=NOW - timedelta(hours=3), token="tok")
        s = dc.connection_status(db)
        assert s["connected"] is True
        assert s["token_valid"] is False
        assert s["seconds_remaining"] == 0

    def test_row_with_no_expiry_is_connected_but_invalid(self, db, frozen):
        _row(db, issued=NOW, expires=None, token="tok")
        s = dc.connection_status(db)
        assert s["connected"] is True
        assert s["token_valid"] is False
        assert s["token_expires_at"] is None
        assert s["seconds_remaining"] is None
        assert s["days_remaining"] is None
        assert s["hours_remaining"] is None

    def test_row_without_issued_at_omits_issued_timestamp(self, db, frozen):
        _row(db, issued=None, expires=NOW + timedelta(hours=2), token="tok")
        s = dc.connection_status(db)
        assert s["token_issued_at"] is None
        assert s["seconds_remaining"] == 7200

    def test_payload_never_contains_a_secret(self, db, enc_key):
        _seed_encrypted(db, issued=NOW, expires=NOW + timedelta(hours=24))
        blob = json.dumps(dc.connection_status(db))
        assert TOKEN not in blob
        assert CLIENT_ID not in blob
        assert "encrypted" not in blob
