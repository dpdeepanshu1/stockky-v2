"""
main.py coverage, part 1 of 2 — startup/shutdown, seeding, gate helpers,
health/status/auth, Dhan connect + eDIS group, risk config, arm/disarm,
risk-engine dry run, feature/autopilot toggles, adaptive status,
reset-starting-capital, audit log, resilience routes.

(session90 step9.) Folds in and completes the previously-unrun draft
`test_main_routes_draft.py`. Part 2 (`test_main_routes_trading.py`) covers
the order/position/cycle/reconcile half of main.py.

Approach — same as tests/test_exit_remaining_coverage.py and
tests/test_portfolio_remaining_coverage.py: call the `async def` route
functions directly (bypassing FastAPI's HTTP layer / Depends resolution),
passing dependencies as ordinary keyword arguments, against a shared
in-memory sqlite engine reset per test via `_fresh_db()`.

SESSION_SECRET / ADMIN_USERNAME are monkeypatched on `config` by an
autouse fixture rather than relying on os.environ.setdefault — config is
imported once, by whichever test module pytest collects first, so an env
var set here is too late in a full-suite run.

Run from services/real-trade-service:
    python -m pytest tests/test_main_routes_core.py -q --cov=main --cov-report=term-missing
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest.mock as mock
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("SESSION_SECRET", "test-session-secret-not-for-prod")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi import HTTPException

import config
import models
import db as db_module
import main
from auth.admin_auth import issue_session_token, AdminAuthError

# ── shared DB engine (StaticPool so :memory: survives across sessions) ─────
_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_SessionFactory = sessionmaker(bind=_engine)


@pytest.fixture(autouse=True)
def _auth_config(monkeypatch):
    """Guarantee a signing secret + username regardless of which test module
    imported `config` first."""
    monkeypatch.setattr(config, "SESSION_SECRET", "test-session-secret-not-for-prod")
    monkeypatch.setattr(config, "ADMIN_USERNAME", "admin")


def _fresh_db():
    models.Base.metadata.drop_all(_engine)
    models.Base.metadata.create_all(_engine)
    return _SessionFactory()


def _seed(db, real_ready: bool = False):
    """Mirrors main._seed_defaults()'s shape for both modes. If
    real_ready=True, REAL's gate starts fully walked (admin_authenticated,
    dhan_connected, risk_config_confirmed) so /arm/REAL can succeed."""
    demo_gate = models.TradeGateState(
        mode="DEMO", admin_authenticated=True,
        admin_authenticated_at=datetime.now(timezone.utc),
        admin_session_expires_at=None,
        risk_config_confirmed=True,
        risk_config_confirmed_at=datetime.now(timezone.utc),
    )
    real_gate = models.TradeGateState(mode="REAL")
    if real_ready:
        real_gate.admin_authenticated = True
        real_gate.admin_authenticated_at = datetime.now(timezone.utc)
        real_gate.admin_session_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        real_gate.dhan_connected = True
        real_gate.dhan_connected_at = datetime.now(timezone.utc)
        real_gate.risk_config_confirmed = True
        real_gate.risk_config_confirmed_at = datetime.now(timezone.utc)
    db.add(demo_gate)
    db.add(real_gate)
    for m, cap in (("DEMO", 100_000.0), ("REAL", 0.0)):
        db.add(models.TradeRiskConfig(mode=m))
        db.add(models.TradeAccount(mode=m, starting_capital=cap, current_equity=cap, cash_available=cap))
    db.commit()


def _add_valid_credential(db):
    db.add(models.TradeCredential(
        dhan_client_id_masked="****1234",
        token_issued_at=datetime.now(timezone.utc),
        token_expires_at=datetime.now(timezone.utc) + timedelta(hours=12),
    ))
    db.commit()


def _run(coro):
    return asyncio.run(coro)


def _admin_bearer() -> str:
    token, _ = issue_session_token(config.ADMIN_USERNAME)
    return f"Bearer {token}"


def _expect_http_error(exc_info, status_code: int):
    assert exc_info.value.status_code == status_code


# ══════════════════════════════════════════════════════════════════════════
# startup / shutdown hooks
# ══════════════════════════════════════════════════════════════════════════
class TestBootForensicsHooks:
    def test_startup_records_boot_and_logs_auth_config(self):
        with mock.patch("boot_forensics.record_boot") as rec, \
             mock.patch("auth.admin_auth.log_auth_config") as log_cfg:
            _run(main._boot_forensics_startup())
        rec.assert_called_once_with("real-trade-service")
        log_cfg.assert_called_once_with("real-trade-service")

    def test_startup_swallows_any_failure(self):
        with mock.patch("boot_forensics.record_boot", side_effect=RuntimeError("no /proc")):
            _run(main._boot_forensics_startup())  # must not raise

    def test_shutdown_marks_clean(self):
        with mock.patch("boot_forensics.mark_clean_shutdown") as mark:
            _run(main._boot_forensics_shutdown())
        mark.assert_called_once()

    def test_shutdown_swallows_any_failure(self):
        with mock.patch("boot_forensics.mark_clean_shutdown", side_effect=RuntimeError("x")):
            _run(main._boot_forensics_shutdown())  # must not raise


class TestStartup:
    def test_config_errors_refuse_to_start(self):
        with mock.patch.object(config, "startup_config_errors", return_value=["ADMIN_PASSWORD_HASH is not set"]), \
             mock.patch.object(main, "init_schema") as init_schema:
            with pytest.raises(RuntimeError) as ei:
                _run(main.startup())
        assert "refusing to start" in str(ei.value)
        init_schema.assert_not_called()

    def _patches(self, **overrides):
        """Everything startup() touches after the config check, mocked."""
        fake_factory = mock.Mock(return_value=mock.Mock())
        base = {
            "init_schema": mock.patch.object(main, "init_schema"),
            "seed": mock.patch.object(main, "_seed_defaults"),
            "migrate": mock.patch.object(main, "_migrate_risk_defaults"),
            "factory": mock.patch.object(db_module, "get_session_factory", return_value=fake_factory),
            "reconcile": mock.patch("resilience.local_cache.reconcile_on_startup"),
            "atr": mock.patch("market_feed.feed.load_atr_cache_from_db"),
            "ap_start": mock.patch("execution.auto_pilot.start"),
            "stale": mock.patch("adaptive_thresholds.startup_staleness_warning"),
            "errors": mock.patch.object(config, "startup_config_errors", return_value=[]),
        }
        base.update(overrides)
        return base

    def test_happy_path_runs_every_step(self):
        p = self._patches()
        with p["init_schema"] as init_schema, p["seed"] as seed, p["migrate"] as migrate, \
             p["factory"], p["reconcile"] as reconcile, p["atr"] as atr, \
             p["ap_start"] as ap_start, p["stale"] as stale, p["errors"]:
            _run(main.startup())
        init_schema.assert_called_once()
        seed.assert_called_once()
        migrate.assert_called_once()
        reconcile.assert_called_once()
        atr.assert_called_once()
        ap_start.assert_called_once()
        stale.assert_called_once()

    def test_reconcile_atr_and_staleness_failures_are_non_fatal(self):
        p = self._patches(
            reconcile=mock.patch("resilience.local_cache.reconcile_on_startup", side_effect=RuntimeError("r")),
            atr=mock.patch("market_feed.feed.load_atr_cache_from_db", side_effect=RuntimeError("a")),
            stale=mock.patch("adaptive_thresholds.startup_staleness_warning", side_effect=RuntimeError("s")),
        )
        with p["init_schema"], p["seed"], p["migrate"], p["factory"], p["reconcile"], p["atr"], \
             p["ap_start"] as ap_start, p["stale"], p["errors"]:
            _run(main.startup())  # must not raise
        ap_start.assert_called_once()  # startup still reaches auto_pilot.start()


class TestShutdown:
    def test_flushes_atr_cache(self):
        fake_db = mock.Mock()
        factory = mock.Mock(return_value=mock.Mock(return_value=fake_db))
        with mock.patch.object(db_module, "get_session_factory", return_value=factory()), \
             mock.patch("market_feed.feed.flush_atr_cache_to_db") as flush:
            _run(main.shutdown())
        flush.assert_called_once_with(fake_db)
        fake_db.close.assert_called_once()

    def test_flush_failure_is_non_fatal(self):
        with mock.patch.object(db_module, "get_session_factory", side_effect=RuntimeError("no db")):
            _run(main.shutdown())  # must not raise


# ══════════════════════════════════════════════════════════════════════════
# _seed_defaults / _migrate_risk_defaults
# ══════════════════════════════════════════════════════════════════════════
class TestSeedDefaults:
    def test_seeds_both_modes_on_empty_db(self):
        db = _fresh_db()
        with mock.patch.object(db_module, "get_session_factory", return_value=_SessionFactory):
            main._seed_defaults()
        demo = db.query(models.TradeGateState).filter_by(mode="DEMO").first()
        real = db.query(models.TradeGateState).filter_by(mode="REAL").first()
        assert demo.admin_authenticated is True and demo.risk_config_confirmed is True
        assert demo.admin_session_expires_at is None
        assert real.admin_authenticated is False and real.risk_config_confirmed is False
        demo_acct = db.query(models.TradeAccount).filter_by(mode="DEMO").first()
        real_acct = db.query(models.TradeAccount).filter_by(mode="REAL").first()
        assert demo_acct.starting_capital == config.DEFAULT_DEMO_CAPITAL
        assert real_acct.starting_capital == 0.0
        assert db.query(models.TradeRiskConfig).count() == 2

    def test_second_call_never_overwrites_existing_rows(self):
        db = _fresh_db()
        with mock.patch.object(db_module, "get_session_factory", return_value=_SessionFactory):
            main._seed_defaults()
            risk = db.query(models.TradeRiskConfig).filter_by(mode="DEMO").first()
            risk.risk_per_trade_pct = 3.3
            db.commit()
            main._seed_defaults()
        db.expire_all()
        risk = db.query(models.TradeRiskConfig).filter_by(mode="DEMO").first()
        assert risk.risk_per_trade_pct == 3.3
        assert db.query(models.TradeGateState).count() == 2


class TestMigrateRiskDefaults:
    def test_stale_old_default_is_bumped(self):
        db = _fresh_db()
        _seed(db)
        risk = db.query(models.TradeRiskConfig).filter_by(mode="DEMO").first()
        risk.risk_per_trade_pct = 1.0
        db.commit()
        with mock.patch.object(db_module, "get_session_factory", return_value=_SessionFactory), \
             mock.patch.object(config, "DEFAULT_RISK_PER_TRADE_PCT", 5.0):
            main._migrate_risk_defaults()
        db.expire_all()
        risk = db.query(models.TradeRiskConfig).filter_by(mode="DEMO").first()
        assert risk.risk_per_trade_pct == 5.0
        assert risk.updated_by == "system-migration-session79"

    def test_admin_edited_value_is_left_alone(self):
        db = _fresh_db()
        _seed(db)
        for m in ("DEMO", "REAL"):
            r = db.query(models.TradeRiskConfig).filter_by(mode=m).first()
            r.risk_per_trade_pct = 2.0
        db.commit()
        with mock.patch.object(db_module, "get_session_factory", return_value=_SessionFactory), \
             mock.patch.object(config, "DEFAULT_RISK_PER_TRADE_PCT", 5.0):
            main._migrate_risk_defaults()
        db.expire_all()
        for m in ("DEMO", "REAL"):
            assert db.query(models.TradeRiskConfig).filter_by(mode=m).first().risk_per_trade_pct == 2.0

    def test_missing_risk_row_is_skipped(self):
        db = _fresh_db()  # nothing seeded at all
        with mock.patch.object(db_module, "get_session_factory", return_value=_SessionFactory):
            main._migrate_risk_defaults()  # must not raise
        assert db.query(models.TradeRiskConfig).count() == 0


# ══════════════════════════════════════════════════════════════════════════
# _gate / _disarm / _check_and_expire_gates
# ══════════════════════════════════════════════════════════════════════════
class TestCheckAndExpireGates:
    def test_expired_admin_session_is_cleared_but_stays_armed(self):
        db = _fresh_db()
        _seed(db, real_ready=True)
        gate = db.query(models.TradeGateState).filter_by(mode="REAL").first()
        gate.armed = True
        gate.admin_session_expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        _add_valid_credential(db)
        db.commit()
        result = main._check_and_expire_gates(db, "REAL")
        assert result.admin_authenticated is False
        assert result.armed is True  # admin expiry never drags `armed` down
        assert result.dhan_connected is True

    def test_real_with_no_credential_row_drops_dhan_connected_and_disarms(self):
        db = _fresh_db()
        _seed(db, real_ready=True)
        gate = db.query(models.TradeGateState).filter_by(mode="REAL").first()
        gate.armed = True
        db.commit()
        result = main._check_and_expire_gates(db, "REAL")
        assert result.dhan_connected is False
        assert result.armed is False
        assert result.disarmed_reason == "Dhan access token expired"
        audit = db.query(models.TradeAuditLog).filter_by(action="DISARMED").first()
        assert audit is not None and audit.mode == "REAL"

    def test_real_with_expired_token_but_not_armed_only_flips_flag(self):
        db = _fresh_db()
        _seed(db, real_ready=True)
        db.add(models.TradeCredential(
            token_issued_at=datetime.now(timezone.utc) - timedelta(days=3),
            token_expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        ))
        db.commit()
        result = main._check_and_expire_gates(db, "REAL")
        assert result.dhan_connected is False
        assert db.query(models.TradeAuditLog).filter_by(action="DISARMED").count() == 0

    def test_real_with_valid_token_untouched(self):
        db = _fresh_db()
        _seed(db, real_ready=True)
        _add_valid_credential(db)
        result = main._check_and_expire_gates(db, "REAL")
        assert result.dhan_connected is True


# ── health ───────────────────────────────────────────────────────────────
class TestHealth:
    def test_health_ok(self):
        result = _run(main.health())
        assert result["ok"] is True
        assert result["service"] == config.SERVICE_NAME


# ── /status/{mode} (gate_status) ────────────────────────────────────────────
class TestGateStatus:
    def test_demo_fresh_seed(self):
        db = _fresh_db()
        _seed(db)
        result = _run(main.gate_status("demo", db=db))
        assert result["mode"] == "DEMO"
        assert result["admin_authenticated"] is True
        assert result["dhan_connected"] is None  # only surfaced for REAL
        assert result["armed"] is False
        assert result["shared_symbol_lock"] is None  # DEMO never shows the lock
        assert result["risk_config"]["min_trade_value_default"] == config.MIN_TRADE_VALUE
        assert result["account"]["starting_capital"] == 100_000.0

    def test_real_fresh_seed(self):
        db = _fresh_db()
        _seed(db)
        result = _run(main.gate_status("real", db=db))
        assert result["mode"] == "REAL"
        assert result["admin_authenticated"] is False
        assert result["dhan_connected"] is False
        assert isinstance(result["shared_symbol_lock"], list)

    def test_scheduled_automation_block_reflects_gate_flags(self):
        db = _fresh_db()
        _seed(db)
        gate = db.query(models.TradeGateState).filter_by(mode="DEMO").first()
        gate.prepick_enabled = True
        gate.afterhours_news_scan_enabled = True
        gate.afterhours_scan_last_run_at = datetime(2026, 9, 22, 20, 0, tzinfo=timezone.utc)
        gate.auto_pilot_enabled = True
        db.commit()
        result = _run(main.gate_status("DEMO", db=db))
        sched = result["scheduled_automation"]
        assert sched["prepick"]["enabled"] is True
        assert sched["afterhours_news_scan"]["enabled"] is True
        assert sched["afterhours_news_scan"]["last_run_at"].startswith("2026-09-22")
        assert result["auto_pilot_enabled"] is True
        assert sched["afterhours_news_scan"]["window_ist"].count("–") == 1

    def test_afterhours_last_run_none_when_never_ran(self):
        db = _fresh_db()
        _seed(db)
        result = _run(main.gate_status("DEMO", db=db))
        assert result["scheduled_automation"]["afterhours_news_scan"]["last_run_at"] is None

    def test_missing_account_and_risk_rows_yield_nones(self):
        db = _fresh_db()
        db.add(models.TradeGateState(mode="DEMO"))
        db.commit()
        result = _run(main.gate_status("DEMO", db=db))
        assert result["risk_config"] is None
        assert result["account"]["starting_capital"] is None
        assert result["account"]["realized_pnl_total"] is None

    def test_invalid_mode_raises_400(self):
        db = _fresh_db()
        _seed(db)
        with pytest.raises(HTTPException) as ei:
            _run(main.gate_status("SWING", db=db))
        _expect_http_error(ei, 400)

    def test_missing_gate_row_raises_500(self):
        db = _fresh_db()
        with pytest.raises(HTTPException) as ei:
            _run(main.gate_status("DEMO", db=db))
        _expect_http_error(ei, 500)


# ── /auth/login, /auth/logout ───────────────────────────────────────────────
class TestAuthLoginLogout:
    def test_login_bad_credentials_401(self):
        db = _fresh_db()
        _seed(db)
        with mock.patch.object(main, "verify_admin_password", return_value=False):
            with pytest.raises(HTTPException) as ei:
                _run(main.login(main.LoginRequest(username="admin", password="wrong"), db=db))
            _expect_http_error(ei, 401)
        assert db.query(models.TradeAuditLog).filter_by(action="ADMIN_LOGIN_FAILED").count() == 1

    def test_login_not_configured_500(self):
        db = _fresh_db()
        _seed(db)
        with mock.patch.object(main, "verify_admin_password", side_effect=AdminAuthError("no hash configured")):
            with pytest.raises(HTTPException) as ei:
                _run(main.login(main.LoginRequest(username="admin", password="x"), db=db))
            _expect_http_error(ei, 500)

    def test_login_success_sets_real_gate(self):
        db = _fresh_db()
        _seed(db)
        with mock.patch.object(main, "verify_admin_password", return_value=True):
            result = _run(main.login(main.LoginRequest(username="admin", password="right"), db=db))
        assert "token" in result and "expires_at" in result
        real_gate = db.query(models.TradeGateState).filter_by(mode="REAL").first()
        assert real_gate.admin_authenticated is True
        demo_gate = db.query(models.TradeGateState).filter_by(mode="DEMO").first()
        assert demo_gate.admin_session_expires_at is None  # DEMO untouched
        assert db.query(models.TradeAuditLog).filter_by(action="ADMIN_LOGIN").count() == 1

    def test_logout_clears_real_admin_flag(self):
        db = _fresh_db()
        _seed(db, real_ready=True)
        result = _run(main.logout(admin="admin", db=db))
        assert result == {"ok": True}
        real_gate = db.query(models.TradeGateState).filter_by(mode="REAL").first()
        assert real_gate.admin_authenticated is False


# ── /auth/config-check ──────────────────────────────────────────────────────
class TestAuthConfigCheck:
    def test_shape(self):
        result = main.auth_config_check()
        assert result["session_secret_configured"] is True
        assert "admin_password_hash_configured" in result


# ── /dhan/connect, /dhan/regenerate-token, /dhan/status ─────────────────────
class TestDhanConnectGroup:
    def test_connect_dhan_sets_gate(self):
        db = _fresh_db()
        _seed(db)
        fake_row = mock.Mock(dhan_client_id_masked="****1234")
        with mock.patch.object(main.dhan_credentials, "save_credentials", return_value=fake_row) as save, \
             mock.patch.object(main.dhan_credentials, "connection_status", return_value={"connected": True}) as status:
            result = _run(main.connect_dhan(
                main.ConnectDhanRequest(client_id="C1", access_token="tok"),
                admin="admin", db=db,
            ))
        save.assert_called_once()
        status.assert_called_once()
        assert result == {"connected": True}
        real_gate = db.query(models.TradeGateState).filter_by(mode="REAL").first()
        assert real_gate.dhan_connected is True

    def test_regenerate_token_disabled_by_config_409(self):
        db = _fresh_db()
        _seed(db)
        with mock.patch.object(config, "DHAN_TOTP_ENABLED", False):
            with pytest.raises(HTTPException) as ei:
                _run(main.regenerate_dhan_token(admin="admin", db=db))
            _expect_http_error(ei, 409)

    def test_regenerate_token_totp_failure_502(self):
        db = _fresh_db()
        _seed(db)
        with mock.patch.object(config, "DHAN_TOTP_ENABLED", True), \
             mock.patch.object(main.dhan_credentials, "refresh_if_totp_enabled", return_value=False):
            with pytest.raises(HTTPException) as ei:
                _run(main.regenerate_dhan_token(admin="admin", db=db))
            _expect_http_error(ei, 502)

    def test_regenerate_token_success(self):
        db = _fresh_db()
        _seed(db)
        with mock.patch.object(config, "DHAN_TOTP_ENABLED", True), \
             mock.patch.object(main.dhan_credentials, "refresh_if_totp_enabled", return_value=True), \
             mock.patch.object(main.dhan_credentials, "connection_status", return_value={"connected": True}):
            result = _run(main.regenerate_dhan_token(admin="admin", db=db))
        assert result == {"connected": True}
        assert db.query(models.TradeAuditLog).filter_by(action="DHAN_TOKEN_REGENERATED").count() == 1

    def test_dhan_status_passthrough(self):
        db = _fresh_db()
        _seed(db)
        with mock.patch.object(main.dhan_credentials, "connection_status", return_value={"connected": False}):
            result = _run(main.dhan_status(admin="admin", db=db))
        assert result == {"connected": False}


# ── /dhan/edis/* ────────────────────────────────────────────────────────────
class TestDhanEdisRoutes:
    def test_request_tpin_success(self):
        db = _fresh_db()
        with mock.patch("execution.dhan_client.edis_request_tpin") as req:
            result = _run(main.dhan_edis_request_tpin(admin="admin", db=db))
        req.assert_called_once_with(db)
        assert result["ok"] is True
        assert "requested_at" in result

    def test_request_tpin_not_connected_503(self):
        db = _fresh_db()
        with mock.patch("execution.dhan_client.edis_request_tpin",
                        side_effect=main.dhan_client.DhanNotConnectedError("nope")):
            with pytest.raises(HTTPException) as ei:
                _run(main.dhan_edis_request_tpin(admin="admin", db=db))
            _expect_http_error(ei, 503)

    def test_request_tpin_generic_failure_502(self):
        db = _fresh_db()
        with mock.patch("execution.dhan_client.edis_request_tpin", side_effect=RuntimeError("cdsl down")):
            with pytest.raises(HTTPException) as ei:
                _run(main.dhan_edis_request_tpin(admin="admin", db=db))
            _expect_http_error(ei, 502)
            assert "cdsl down" in ei.value.detail

    def test_authorize_form_success_returns_html_response(self):
        db = _fresh_db()
        with mock.patch("execution.dhan_client.edis_get_form", return_value="<form/>") as get_form:
            resp = _run(main.dhan_edis_authorize_form(admin="admin", db=db, bulk=True, isin="", qty=0))
        get_form.assert_called_once_with(db, isin="", qty=0, bulk=True)
        assert resp.body == b"<form/>"
        assert resp.media_type == "text/html"

    def test_authorize_form_not_connected_503(self):
        db = _fresh_db()
        with mock.patch("execution.dhan_client.edis_get_form",
                        side_effect=main.dhan_client.DhanNotConnectedError("nope")):
            with pytest.raises(HTTPException) as ei:
                _run(main.dhan_edis_authorize_form(admin="admin", db=db, bulk=True, isin="", qty=0))
            _expect_http_error(ei, 503)

    def test_authorize_form_no_demat_holdings_is_409_not_502(self):
        db = _fresh_db()
        with mock.patch("execution.dhan_client.edis_get_form",
                        side_effect=RuntimeError("No demat holdings found to authorize")):
            with pytest.raises(HTTPException) as ei:
                _run(main.dhan_edis_authorize_form(admin="admin", db=db, bulk=True, isin="", qty=0))
            _expect_http_error(ei, 409)

    def test_authorize_form_other_runtime_error_502(self):
        db = _fresh_db()
        with mock.patch("execution.dhan_client.edis_get_form", side_effect=RuntimeError("bad payload")):
            with pytest.raises(HTTPException) as ei:
                _run(main.dhan_edis_authorize_form(admin="admin", db=db, bulk=True, isin="", qty=0))
            _expect_http_error(ei, 502)

    def test_authorize_form_unexpected_exception_502(self):
        db = _fresh_db()
        with mock.patch("execution.dhan_client.edis_get_form", side_effect=ValueError("weird")):
            with pytest.raises(HTTPException) as ei:
                _run(main.dhan_edis_authorize_form(admin="admin", db=db, bulk=True, isin="", qty=0))
            _expect_http_error(ei, 502)
            assert "Dhan eDIS form error" in ei.value.detail

    def test_edis_status_passthrough(self):
        db = _fresh_db()
        with mock.patch("execution.dhan_client.edis_inquire", return_value={"aprvdQty": 5}) as inq:
            result = _run(main.dhan_edis_status(admin="admin", db=db, isin="INE123"))
        inq.assert_called_once_with(db, isin="INE123")
        assert result == {"aprvdQty": 5}

    def test_edis_summary_passthrough(self):
        db = _fresh_db()
        with mock.patch("execution.dhan_client.edis_verification_summary",
                        return_value={"verified_today": True}):
            result = _run(main.dhan_edis_summary(admin="admin", db=db))
        assert result == {"verified_today": True}


# ── /dhan/network-check ─────────────────────────────────────────────────────
class TestDhanNetworkCheck:
    def test_with_ip(self):
        with mock.patch("execution.dhan_client.get_outbound_ip", return_value="1.2.3.4"):
            result = _run(main.dhan_network_check(admin="admin"))
        assert result["outbound_ip"] == "1.2.3.4"
        assert "Whitelist this exact IP" in result["note"]

    def test_without_ip(self):
        with mock.patch("execution.dhan_client.get_outbound_ip", return_value=None):
            result = _run(main.dhan_network_check(admin="admin"))
        assert result["outbound_ip"] is None
        assert "Could not determine" in result["note"]


# ── /dhan/funds, /dhan/account ──────────────────────────────────────────────
class TestDhanFundsAccount:
    def test_dhan_funds_not_connected_409(self):
        db = _fresh_db()
        _seed(db)
        with mock.patch("execution.dhan_client.get_funds",
                        side_effect=main.dhan_client.DhanNotConnectedError("not connected")):
            with pytest.raises(HTTPException) as ei:
                _run(main.dhan_funds(admin="admin", db=db))
            _expect_http_error(ei, 409)

    def test_dhan_funds_generic_error_502(self):
        db = _fresh_db()
        _seed(db)
        with mock.patch("execution.dhan_client.get_funds", side_effect=RuntimeError("boom")):
            with pytest.raises(HTTPException) as ei:
                _run(main.dhan_funds(admin="admin", db=db))
            _expect_http_error(ei, 502)

    def test_dhan_funds_success(self):
        db = _fresh_db()
        _seed(db)
        with mock.patch("execution.dhan_client.get_funds", return_value={"available": 1000}):
            result = _run(main.dhan_funds(admin="admin", db=db))
        assert result == {"available": 1000}

    def test_dhan_account_connected_with_funds_error(self):
        db = _fresh_db()
        _seed(db)
        with mock.patch.object(main.dhan_credentials, "connection_status",
                                return_value={"connected": True}), \
             mock.patch("execution.dhan_client.get_funds", side_effect=RuntimeError("timeout")):
            result = _run(main.dhan_account(admin="admin", db=db))
        assert result["connected"] is True
        assert result["funds"] is None
        assert "timeout" in result["funds_error"]

    def test_dhan_account_connected_with_funds_ok(self):
        db = _fresh_db()
        _seed(db)
        with mock.patch.object(main.dhan_credentials, "connection_status",
                                return_value={"connected": True}), \
             mock.patch("execution.dhan_client.get_funds", return_value={"available": 5}):
            result = _run(main.dhan_account(admin="admin", db=db))
        assert result["funds"] == {"available": 5}
        assert result["funds_error"] is None

    def test_dhan_account_not_connected_skips_funds_call(self):
        db = _fresh_db()
        _seed(db)
        with mock.patch.object(main.dhan_credentials, "connection_status",
                                return_value={"connected": False}), \
             mock.patch("execution.dhan_client.get_funds") as get_funds:
            result = _run(main.dhan_account(admin="admin", db=db))
        get_funds.assert_not_called()
        assert result["funds"] is None
        assert result["funds_error"] is None


# ── /risk-config/{mode} (GET), /risk-config (POST), /risk-config/{mode}/confirm
class TestRiskConfig:
    def test_get_risk_config_success(self):
        db = _fresh_db()
        _seed(db)
        result = _run(main.get_risk_config("demo", db=db))
        assert result["mode"] == "DEMO"
        assert result["min_trade_value_default"] == config.MIN_TRADE_VALUE
        assert result["max_trade_value_default"] == config.MAX_TRADE_VALUE
        # TradeRiskConfig.updated_at has default=_now (models.py), so a
        # freshly-seeded row already carries a timestamp, not None.
        assert result["updated_at"] is not None

    def test_get_risk_config_updated_at_serialised(self):
        db = _fresh_db()
        _seed(db)
        risk = db.query(models.TradeRiskConfig).filter_by(mode="DEMO").first()
        risk.updated_at = datetime(2026, 9, 1, 10, 0, 0)
        db.commit()
        result = _run(main.get_risk_config("DEMO", db=db))
        assert result["updated_at"].startswith("2026-09-01")

    def test_get_risk_config_invalid_mode_400(self):
        db = _fresh_db()
        _seed(db)
        with pytest.raises(HTTPException) as ei:
            _run(main.get_risk_config("BOGUS", db=db))
        _expect_http_error(ei, 400)

    def test_get_risk_config_missing_row_404(self):
        db = _fresh_db()
        db.add(models.TradeGateState(mode="DEMO"))
        db.commit()
        with pytest.raises(HTTPException) as ei:
            _run(main.get_risk_config("DEMO", db=db))
        _expect_http_error(ei, 404)

    def test_update_risk_config_demo_open_succeeds(self):
        db = _fresh_db()
        _seed(db)
        body = main.RiskConfigUpdate(mode="DEMO", risk_per_trade_pct=2.5)
        result = _run(main.update_risk_config(body, authorization="", db=db))
        assert result == {"ok": True}
        risk = db.query(models.TradeRiskConfig).filter_by(mode="DEMO").first()
        assert risk.risk_per_trade_pct == 2.5
        assert risk.updated_by == "demo-user"

    def test_update_risk_config_while_armed_409(self):
        db = _fresh_db()
        _seed(db)
        gate = db.query(models.TradeGateState).filter_by(mode="DEMO").first()
        gate.armed = True
        db.commit()
        body = main.RiskConfigUpdate(mode="DEMO", risk_per_trade_pct=2.5)
        with pytest.raises(HTTPException) as ei:
            _run(main.update_risk_config(body, authorization="", db=db))
        _expect_http_error(ei, 409)

    def test_update_risk_config_missing_row_404(self):
        db = _fresh_db()
        db.add(models.TradeGateState(mode="DEMO"))
        db.commit()
        body = main.RiskConfigUpdate(mode="DEMO", risk_per_trade_pct=2.5)
        with pytest.raises(HTTPException) as ei:
            _run(main.update_risk_config(body, authorization="", db=db))
        _expect_http_error(ei, 404)

    def test_update_risk_config_real_requires_admin_401(self):
        db = _fresh_db()
        _seed(db, real_ready=True)
        body = main.RiskConfigUpdate(mode="REAL", risk_per_trade_pct=2.0)
        with pytest.raises(HTTPException) as ei:
            _run(main.update_risk_config(body, authorization="", db=db))
        _expect_http_error(ei, 401)

    def test_update_risk_config_real_with_admin_succeeds(self):
        db = _fresh_db()
        _seed(db, real_ready=True)
        body = main.RiskConfigUpdate(mode="REAL", max_concurrent_positions=5)
        result = _run(main.update_risk_config(body, authorization=_admin_bearer(), db=db))
        assert result == {"ok": True}
        risk = db.query(models.TradeRiskConfig).filter_by(mode="REAL").first()
        assert risk.max_concurrent_positions == 5
        assert risk.updated_by == "admin"

    def test_update_risk_config_none_fields_keep_existing_value(self):
        db = _fresh_db()
        _seed(db)
        risk = db.query(models.TradeRiskConfig).filter_by(mode="DEMO").first()
        before = risk.max_daily_loss_pct
        body = main.RiskConfigUpdate(mode="DEMO", max_trade_value=50_000.0, allow_pyramiding=True)
        _run(main.update_risk_config(body, authorization="", db=db))
        db.expire_all()
        risk = db.query(models.TradeRiskConfig).filter_by(mode="DEMO").first()
        assert risk.max_trade_value == 50_000.0
        assert risk.allow_pyramiding is True
        assert risk.max_daily_loss_pct == before

    def test_confirm_risk_config(self):
        db = _fresh_db()
        _seed(db)
        result = _run(main.confirm_risk_config("demo", admin=None, db=db))
        assert result == {"ok": True}
        gate = db.query(models.TradeGateState).filter_by(mode="DEMO").first()
        assert gate.risk_config_confirmed is True


# ── /arm/{mode}, /disarm/{mode}, /emergency-pause ───────────────────────────
class TestArmDisarm:
    def test_arm_demo_succeeds_without_gates(self):
        db = _fresh_db()
        _seed(db)
        result = _run(main.arm("demo", admin=None, db=db))
        assert result == {"ok": True, "armed": True, "mode": "DEMO"}

    def test_arm_real_missing_gates_409(self):
        db = _fresh_db()
        _seed(db)  # REAL not ready
        with pytest.raises(HTTPException) as ei:
            _run(main.arm("real", admin="admin", db=db))
        _expect_http_error(ei, 409)
        assert "missing gates" in ei.value.detail
        for g in ("admin_authenticated", "dhan_connected", "risk_config_confirmed"):
            assert g in ei.value.detail

    def test_arm_real_ready_succeeds(self):
        db = _fresh_db()
        _seed(db, real_ready=True)
        _add_valid_credential(db)  # _check_and_expire_gates re-validates the token
        result = _run(main.arm("real", admin="admin", db=db))
        assert result == {"ok": True, "armed": True, "mode": "REAL"}

    def test_arm_invalid_mode_400(self):
        db = _fresh_db()
        _seed(db)
        with pytest.raises(HTTPException) as ei:
            _run(main.arm("FUTURES", admin=None, db=db))
        _expect_http_error(ei, 400)

    def test_disarm_when_armed_logs_action(self):
        db = _fresh_db()
        _seed(db)
        _run(main.arm("demo", admin=None, db=db))
        result = _run(main.disarm("demo", admin=None, db=db))
        assert result == {"ok": True, "armed": False, "mode": "DEMO"}
        gate = db.query(models.TradeGateState).filter_by(mode="DEMO").first()
        assert gate.armed is False
        assert gate.disarmed_reason == "Manual disarm by admin"
        assert db.query(models.TradeAuditLog).filter_by(action="DISARMED").count() == 1

    def test_disarm_when_not_armed_is_noop_safe(self):
        db = _fresh_db()
        _seed(db)
        result = _run(main.disarm("demo", admin=None, db=db))
        assert result == {"ok": True, "armed": False, "mode": "DEMO"}
        assert db.query(models.TradeAuditLog).filter_by(action="DISARMED").count() == 0

    def test_emergency_pause_disarms_both_modes(self):
        db = _fresh_db()
        _seed(db, real_ready=True)
        _add_valid_credential(db)
        _run(main.arm("demo", admin=None, db=db))
        _run(main.arm("real", admin="admin", db=db))
        result = _run(main.emergency_pause(db=db))
        assert result == {"ok": True, "paused": True}
        for m in ("DEMO", "REAL"):
            gate = db.query(models.TradeGateState).filter_by(mode=m).first()
            assert gate.armed is False
            assert gate.disarmed_reason == "Emergency pause triggered"


# ── /risk-engine/check ──────────────────────────────────────────────────────
class TestRiskEngineCheck:
    def _body(self, mode="DEMO", **kw):
        d = dict(mode=mode, symbol="reliance", side="buy", qty=10, entry_price=100.0, stop_price=98.0)
        d.update(kw)
        return main.RiskCheckRequest(**d)

    def test_demo_dry_run_records_risk_event(self):
        db = _fresh_db()
        _seed(db)
        with mock.patch.object(main, "is_market_open_ist", return_value=True):
            result = _run(main.risk_engine_check(self._body(), authorization="", db=db))
        assert set(result) == {"verdict", "check_name", "reason", "approved_qty"}
        ev = db.query(models.TradeRiskEvent).all()
        assert len(ev) == 1
        assert ev[0].symbol == "RELIANCE" and ev[0].mode == "DEMO"

    def test_counts_open_positions_and_ratcheted_stop(self):
        db = _fresh_db()
        _seed(db)
        # One with a stop below entry (real risk), one ratcheted ABOVE entry
        # (risk must clamp to 0, not go negative/abs()).
        db.add(models.TradePosition(mode="DEMO", symbol="AAA", status="OPEN", qty_open=10,
                                    avg_entry_price=100.0, current_stop=95.0))
        db.add(models.TradePosition(mode="DEMO", symbol="BBB", status="OPEN", qty_open=5,
                                    avg_entry_price=200.0, current_stop=210.0))
        db.add(models.TradePosition(mode="DEMO", symbol="CCC", status="OPEN", qty_open=1,
                                    avg_entry_price=50.0, current_stop=None))
        db.commit()
        captured = {}

        def _fake_eval(intent, state):
            captured["state"] = state
            captured["intent"] = intent
            return mock.Mock(check_name="ok", verdict=mock.Mock(value="APPROVED"), reason="fine", approved_qty=10)

        with mock.patch.object(main, "risk_evaluate", side_effect=_fake_eval), \
             mock.patch.object(main, "is_market_open_ist", return_value=True):
            result = _run(main.risk_engine_check(self._body(), authorization="", db=db))
        st = captured["state"]
        assert st.open_position_count == 3
        assert st.open_position_symbols == {"AAA", "BBB", "CCC"}
        assert st.open_positions_total_risk == pytest.approx(50.0)  # only AAA: (100-95)*10
        assert st.open_positions_market_value == pytest.approx(10 * 100 + 5 * 200 + 1 * 50)
        assert st.trading_globally_paused is True  # gate not armed
        assert captured["intent"].side == "BUY"
        assert result["verdict"] == "APPROVED"

    def test_max_trade_value_falls_back_to_config_then_row_override(self):
        db = _fresh_db()
        _seed(db)
        captured = []

        def _fake_eval(intent, state):
            captured.append(state.max_trade_value)
            return mock.Mock(check_name="ok", verdict=mock.Mock(value="APPROVED"), reason="", approved_qty=1)

        with mock.patch.object(main, "risk_evaluate", side_effect=_fake_eval):
            _run(main.risk_engine_check(self._body(), authorization="", db=db))
            risk = db.query(models.TradeRiskConfig).filter_by(mode="DEMO").first()
            risk.max_trade_value = 12345.0
            db.commit()
            _run(main.risk_engine_check(self._body(), authorization="", db=db))
        assert captured[0] == config.MAX_TRADE_VALUE
        assert captured[1] == 12345.0

    def test_real_mode_reads_other_service_exposure(self):
        db = _fresh_db()
        _seed(db, real_ready=True)
        _add_valid_credential(db)
        captured = {}

        def _fake_eval(intent, state):
            captured["other"] = state.other_service_open_positions_market_value
            return mock.Mock(check_name="ok", verdict=mock.Mock(value="APPROVED"), reason="", approved_qty=1)

        with mock.patch.object(main.shared_exposure, "get_other_service_exposure", return_value=777.0), \
             mock.patch.object(main, "risk_evaluate", side_effect=_fake_eval):
            _run(main.risk_engine_check(self._body(mode="REAL"), authorization=_admin_bearer(), db=db))
        assert captured["other"] == 777.0

    def test_real_without_admin_401(self):
        db = _fresh_db()
        _seed(db, real_ready=True)
        with pytest.raises(HTTPException) as ei:
            _run(main.risk_engine_check(self._body(mode="REAL"), authorization="", db=db))
        _expect_http_error(ei, 401)

    def test_missing_account_or_risk_row_404(self):
        db = _fresh_db()
        db.add(models.TradeGateState(mode="DEMO"))
        db.commit()
        with pytest.raises(HTTPException) as ei:
            _run(main.risk_engine_check(self._body(), authorization="", db=db))
        _expect_http_error(ei, 404)


# ── /features/{mode} ─────────────────────────────────────────────────────────
class TestFeatureToggle:
    def test_set_feature_enable(self):
        db = _fresh_db()
        _seed(db)
        body = main.FeatureToggleRequest(feature="prepick", enabled=True)
        result = _run(main.set_feature("demo", body, admin=None, db=db))
        assert result == {"ok": True, "mode": "DEMO", "feature": "prepick", "enabled": True}
        gate = db.query(models.TradeGateState).filter_by(mode="DEMO").first()
        assert gate.prepick_enabled is True
        assert gate.prepick_enabled_at is not None

    def test_set_feature_disable_clears_timestamp(self):
        db = _fresh_db()
        _seed(db)
        _run(main.set_feature("demo", main.FeatureToggleRequest(feature="prepick", enabled=True), admin=None, db=db))
        result = _run(main.set_feature("demo", main.FeatureToggleRequest(feature="prepick", enabled=False), admin=None, db=db))
        assert result["enabled"] is False
        gate = db.query(models.TradeGateState).filter_by(mode="DEMO").first()
        assert gate.prepick_enabled is False
        assert gate.prepick_enabled_at is None

    def test_every_known_feature_maps_to_real_gate_columns(self):
        db = _fresh_db()
        _seed(db)
        gate = db.query(models.TradeGateState).filter_by(mode="DEMO").first()
        for feature, (enabled_col, at_col) in main._FEATURE_COLUMNS.items():
            assert hasattr(gate, enabled_col), f"{feature}: {enabled_col} missing on TradeGateState"
            assert hasattr(gate, at_col), f"{feature}: {at_col} missing on TradeGateState"

    def test_feature_name_is_normalised(self):
        db = _fresh_db()
        _seed(db)
        body = main.FeatureToggleRequest(feature="  EOD_Squareoff ", enabled=True)
        result = _run(main.set_feature("demo", body, admin=None, db=db))
        assert result["feature"] == "eod_squareoff"

    def test_set_feature_unknown_feature_400(self):
        db = _fresh_db()
        _seed(db)
        body = main.FeatureToggleRequest(feature="not_a_real_feature", enabled=True)
        with pytest.raises(HTTPException) as ei:
            _run(main.set_feature("demo", body, admin=None, db=db))
        _expect_http_error(ei, 400)

    def test_set_feature_invalid_mode_400(self):
        db = _fresh_db()
        _seed(db)
        body = main.FeatureToggleRequest(feature="prepick", enabled=True)
        with pytest.raises(HTTPException) as ei:
            _run(main.set_feature("BOGUS", body, admin=None, db=db))
        _expect_http_error(ei, 400)


# ── /autopilot/{mode}/enable, /disable ──────────────────────────────────────
class TestAutopilotToggle:
    def test_enable(self):
        db = _fresh_db()
        _seed(db)
        result = _run(main.autopilot_enable("demo", admin=None, db=db))
        assert result == {"ok": True, "mode": "DEMO", "auto_pilot_enabled": True}
        gate = db.query(models.TradeGateState).filter_by(mode="DEMO").first()
        assert gate.auto_pilot_enabled_at is not None

    def test_disable(self):
        db = _fresh_db()
        _seed(db)
        _run(main.autopilot_enable("demo", admin=None, db=db))
        result = _run(main.autopilot_disable("demo", admin=None, db=db))
        assert result == {"ok": True, "mode": "DEMO", "auto_pilot_enabled": False}

    def test_enable_invalid_mode_400(self):
        db = _fresh_db()
        _seed(db)
        with pytest.raises(HTTPException) as ei:
            _run(main.autopilot_enable("BOGUS", admin=None, db=db))
        _expect_http_error(ei, 400)

    def test_disable_invalid_mode_400(self):
        db = _fresh_db()
        _seed(db)
        with pytest.raises(HTTPException) as ei:
            _run(main.autopilot_disable("BOGUS", admin=None, db=db))
        _expect_http_error(ei, 400)


# ── /adaptive/status, /adaptive/market-params/status ────────────────────────
class TestAdaptiveStatusRoutes:
    def test_adaptive_status_success_passthrough(self):
        db = _fresh_db()
        with mock.patch("adaptive_thresholds.adaptive_status", return_value={"adaptive_active": True}):
            result = _run(main.get_adaptive_status(db=db))
        assert result == {"adaptive_active": True}

    def test_adaptive_status_exception_falls_back(self):
        db = _fresh_db()
        with mock.patch("adaptive_thresholds.adaptive_status", side_effect=RuntimeError("db exploded")):
            result = _run(main.get_adaptive_status(db=db))
        assert result["adaptive_active"] is False
        assert "db exploded" in result["error"]
        assert result["static_fallback"] == config.ENTRY_REGIME_MIN_SCORE

    def test_adaptive_market_params_success_passthrough(self):
        db = _fresh_db()
        with mock.patch("adaptive_market_params.adaptive_params_status", return_value={"ok": True}):
            result = _run(main.get_adaptive_market_params_status(db=db))
        assert result == {"ok": True}

    def test_adaptive_market_params_exception_falls_back(self):
        db = _fresh_db()
        with mock.patch("adaptive_market_params.adaptive_params_status", side_effect=RuntimeError("boom")):
            result = _run(main.get_adaptive_market_params_status(db=db))
        assert "error" in result
        assert result["candidate_max_atr_pct"]["source"] == "static (error)"
        assert result["min_market_cap_cr"]["source"] == "static (error)"


# ── /account/reset-starting-capital ─────────────────────────────────────────
class TestResetStartingCapital:
    def test_success(self):
        db = _fresh_db()
        _seed(db)
        req = main.ResetStartingCapitalRequest(mode="DEMO", starting_capital=250000.0)
        result = _run(main.reset_starting_capital(req, admin="admin", db=db))
        assert result["ok"] is True
        assert result["new_starting_capital"] == 250000.0
        assert result["old_starting_capital"] == 100_000.0
        account = db.query(models.TradeAccount).filter_by(mode="DEMO").first()
        assert account.starting_capital == 250000.0
        # current_equity / cash_available deliberately untouched
        assert account.current_equity == 100_000.0

    def test_invalid_mode_400(self):
        db = _fresh_db()
        _seed(db)
        req = main.ResetStartingCapitalRequest(mode="BOGUS", starting_capital=1000.0)
        with pytest.raises(HTTPException) as ei:
            _run(main.reset_starting_capital(req, admin="admin", db=db))
        _expect_http_error(ei, 400)

    def test_non_positive_capital_400(self):
        db = _fresh_db()
        _seed(db)
        req = main.ResetStartingCapitalRequest(mode="DEMO", starting_capital=0.0)
        with pytest.raises(HTTPException) as ei:
            _run(main.reset_starting_capital(req, admin="admin", db=db))
        _expect_http_error(ei, 400)

    def test_no_account_row_404(self):
        db = _fresh_db()
        db.add(models.TradeGateState(mode="DEMO"))
        db.commit()
        req = main.ResetStartingCapitalRequest(mode="DEMO", starting_capital=1000.0)
        with pytest.raises(HTTPException) as ei:
            _run(main.reset_starting_capital(req, admin="admin", db=db))
        _expect_http_error(ei, 404)


# ── /audit-log ───────────────────────────────────────────────────────────────
class TestAuditLog:
    def test_demo_only_query_open_no_admin(self):
        db = _fresh_db()
        db.add(models.TradeAuditLog(actor="system", action="ARMED", mode="DEMO"))
        db.add(models.TradeAuditLog(actor="admin", action="ARMED", mode="REAL"))
        db.commit()
        rows = _run(main.audit_log(mode="DEMO", limit=50, authorization="", db=db))
        assert len(rows) == 1
        assert rows[0]["mode"] == "DEMO"

    def test_no_mode_treated_as_real_requires_admin_401(self):
        db = _fresh_db()
        with pytest.raises(HTTPException) as ei:
            _run(main.audit_log(mode=None, limit=50, authorization="", db=db))
        _expect_http_error(ei, 401)

    def test_no_mode_with_admin_returns_all_modes(self):
        db = _fresh_db()
        db.add(models.TradeAuditLog(actor="system", action="ARMED", mode="DEMO"))
        db.add(models.TradeAuditLog(actor="admin", action="ARMED", mode="REAL"))
        db.commit()
        rows = _run(main.audit_log(mode=None, limit=50, authorization=_admin_bearer(), db=db))
        assert len(rows) == 2

    def test_real_mode_with_admin_succeeds(self):
        db = _fresh_db()
        db.add(models.TradeAuditLog(actor="admin", action="ARMED", mode="REAL"))
        db.commit()
        rows = _run(main.audit_log(mode="REAL", limit=50, authorization=_admin_bearer(), db=db))
        assert len(rows) == 1

    def test_limit_is_clamped(self):
        db = _fresh_db()
        for _ in range(3):
            db.add(models.TradeAuditLog(actor="system", action="X", mode="DEMO"))
        db.commit()
        rows_low = _run(main.audit_log(mode="DEMO", limit=0, authorization="", db=db))
        rows_high = _run(main.audit_log(mode="DEMO", limit=99999, authorization="", db=db))
        assert len(rows_low) == 1   # 0 clamps UP to 1
        assert len(rows_high) == 3  # huge clamps DOWN to 200, still returns all 3


# ── /resilience/status, /resilience/reset ───────────────────────────────────
class TestResilienceRoutes:
    def test_status_shape(self):
        db = _fresh_db()
        fake_breaker = mock.Mock()
        fake_breaker.to_dict.return_value = {"state": "CLOSED"}
        with mock.patch("resilience.circuit_breaker.api_gateway_breaker", fake_breaker), \
             mock.patch("resilience.circuit_breaker.market_data_breaker", fake_breaker), \
             mock.patch("resilience.local_cache.load_snapshot", return_value=None):
            result = _run(main.resilience_status_route(admin="admin", db=db))
        assert result["breakers"]["api_gateway"] == {"state": "CLOSED"}
        assert result["breakers"]["market_data"] == {"state": "CLOSED"}
        assert result["dynamic_universe_last"] is None

    def test_reset_clears_all_three_breakers(self):
        fake = mock.Mock()
        fake.name = "fake"
        fake._failures = 3
        fake._opened_at = datetime.now(timezone.utc)
        with mock.patch("resilience.circuit_breaker.api_gateway_breaker", fake), \
             mock.patch("resilience.circuit_breaker.market_data_breaker", fake), \
             mock.patch("resilience.circuit_breaker.event_service_breaker", fake):
            result = _run(main.resilience_reset_route(admin="admin"))
        assert result["ok"] is True
        assert result["count"] == 3
        assert fake._failures == 0 and fake._opened_at is None


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(pytest.main([__file__, "-v"]))
