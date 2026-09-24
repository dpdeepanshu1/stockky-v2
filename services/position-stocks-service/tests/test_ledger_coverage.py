"""
tests/test_ledger_coverage.py

Closes the coverage gaps in capital/ledger.py (53% → 100%; 81 missing lines:
71-79, 114-123, 133-250, 266-288, 301-302, 388, 447-448, 491, 496, 536,
556-563) and pins the *behaviour* of every public function in it so a wrong
branch fails a test rather than merely being executed.

Grouped by function:
  _pick_balance             — key scan order, None / non-numeric skip, all-miss
  _get_or_create            — first-use defaults, idempotence
  _maybe_lazy_reset_daily   — stale IST date triggers full reset (pnl, peer
                              pnl, kill switch), available_capital untouched
  sync_from_broker          — get_funds failure, no-balance / zero-balance,
                              first-key + key-shift warnings, no-open-positions
                              reset, own-committed-capital add-back (OPEN and
                              EXIT_LEGS_REJECTED count, CLOSED does not),
                              open-positions-with-zero-avail reset, normal
                              open-positions no-reset, peer-pnl sync +
                              shared-exposure publish tail calls
  sync_peer_pnl             — REAL function (the shared `env` fixture stubs it
                              out; these tests restore it): success, missing
                              keys, failure keeps last cached value
  reserve_capital           — kill-switch gate, zero-pool gate, sizing formula,
                              boundary (need == have), insufficient, no
                              decrement on refusal
  reserve_additional        — <= 0 early return, boundary, insufficient
  release_capital           — accounting, kill-switch trip (incl. exact
                              threshold), gate-row mirror, no gate row,
                              already-tripped no-op
  reconcile_position_cost   — delta==0 early return, +/- delta, negative
                              available warning
  reclaim_premature_release — <= 0 early return, normal deduct + warning
  reset_daily               — manual reset path
  get_state                 — full dict shape, iso_utc timestamps
  book_late_realized_pnl    — total-only booking, never trips kill switch

Run from services/position-stocks-service:
    python3 -m pytest tests/test_ledger_coverage.py -q \\
        --cov=capital.ledger --cov-report=term-missing
"""
from __future__ import annotations

import json as _json
import logging
import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import models
from capital import ledger, shared_exposure
from execution import dhan_client
from tz_utils import ist_today_str

# Captured at import, before any test's monkeypatch can replace it, so the
# sync_peer_pnl tests can put the real implementation back.
_REAL_SYNC_PEER_PNL = ledger.sync_peer_pnl

LOGGER = "position-stocks-ledger"


# ── Shared fixtures ────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_balance_key(monkeypatch):
    """ledger._last_balance_key is module-level state that leaks between
    tests; monkeypatch restores whatever it was afterwards."""
    monkeypatch.setattr(ledger, "_last_balance_key", None)


@pytest.fixture()
def env(monkeypatch):
    """In-memory DB with sync_peer_pnl and publish_own_exposure stubbed out.
    Returns (db, calls) where calls records what the stubs were given."""
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    calls = {"peer": 0, "exposure": []}

    def _peer(db_):
        calls["peer"] += 1

    monkeypatch.setattr(ledger, "sync_peer_pnl", _peer)
    monkeypatch.setattr(
        shared_exposure, "publish_own_exposure",
        lambda db_, v: calls["exposure"].append(v),
    )
    yield db, calls
    db.close()


@pytest.fixture()
def real_peer_env(monkeypatch):
    """In-memory DB with the REAL sync_peer_pnl (network stubbed per test)."""
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    monkeypatch.setattr(ledger, "sync_peer_pnl", _REAL_SYNC_PEER_PNL)
    yield db
    db.close()


def _seed_ledger(db, total=100_000.0, avail=90_000.0):
    row = ledger._get_or_create(db)
    row.total_allocated_capital = total
    row.available_capital = avail
    db.commit()
    return row


def _add_position(db, symbol="ABC", status="OPEN", capital_risked=5_000.0, sec_id="999"):
    p = models.ScalpPosition(
        symbol=symbol, window_source="5m",
        adaptive_target_pct=2.0, adaptive_stop_pct=1.0,
        status=status, quantity=10, entry_price=500.0,
        capital_risked=capital_risked,
        overnight_converted_to_cnc=False, dhan_security_id=sec_id,
        target_price=510.0, stop_price=490.0,
        opened_at=datetime.now(timezone.utc),
    )
    db.add(p)
    db.commit()
    return p


def _funds(monkeypatch, funds):
    monkeypatch.setattr(dhan_client, "get_funds", lambda db_: funds)


def _mock_urlopen_response(payload_bytes):
    resp = MagicMock()
    resp.read.return_value = payload_bytes
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


# ══════════════════════════════════════════════════════════════════════════════
# _pick_balance
# ══════════════════════════════════════════════════════════════════════════════

class TestPickBalance:
    """_pick_balance scans _BALANCE_KEYS in order, returns (float, key) on
    first numeric hit, skips None and non-numeric, returns (None, None) when
    nothing matches."""

    def test_returns_first_matching_key(self):
        val, key = ledger._pick_balance({"availabelBalance": 50000.0})
        assert val == pytest.approx(50000.0)
        assert key == "availabelBalance"

    def test_key_order_wins_when_several_present(self):
        funds = {"availableCash": 1.0, "availableBalance": 2.0, "availabelBalance": 3.0}
        val, key = ledger._pick_balance(funds)
        assert (val, key) == (3.0, "availabelBalance")

    def test_numeric_string_is_coerced_to_float(self):
        val, key = ledger._pick_balance({"availableBalance": "1234.5"})
        assert val == pytest.approx(1234.5)
        assert isinstance(val, float)
        assert key == "availableBalance"

    def test_zero_is_a_valid_hit_not_skipped(self):
        # 0 is falsy but not None — it must be returned, not fall through to
        # the next key (sync_from_broker handles <= 0 itself).
        val, key = ledger._pick_balance({"availabelBalance": 0, "availableBalance": 9.0})
        assert (val, key) == (0.0, "availabelBalance")

    def test_skips_none_values_and_finds_later_key(self):
        funds = {"availabelBalance": None, "availableBalance": None, "availableCash": 75000.0}
        val, key = ledger._pick_balance(funds)
        assert val == pytest.approx(75000.0)
        assert key == "availableCash"

    def test_skips_non_numeric_value_and_finds_next(self):
        funds = {"availabelBalance": "N/A", "availableBalance": 60000.0}
        val, key = ledger._pick_balance(funds)
        assert val == pytest.approx(60000.0)
        assert key == "availableBalance"

    def test_unconvertible_type_is_skipped(self):
        # float([]) raises TypeError (not ValueError) — both must be caught.
        funds = {"availabelBalance": [], "sodLimit": 7.0}
        val, key = ledger._pick_balance(funds)
        assert (val, key) == (7.0, "sodLimit")

    def test_all_keys_missing_returns_none_none(self):
        val, key = ledger._pick_balance({"someOtherField": 99999.0})
        assert val is None and key is None

    def test_all_keys_non_numeric_returns_none_none(self):
        funds = {k: "bad" for k in ledger._BALANCE_KEYS}
        val, key = ledger._pick_balance(funds)
        assert val is None and key is None


# ══════════════════════════════════════════════════════════════════════════════
# _get_or_create
# ══════════════════════════════════════════════════════════════════════════════

class TestGetOrCreate:
    def test_first_call_creates_zeroed_real_row(self, env):
        db, _ = env
        row = ledger._get_or_create(db)
        assert row.mode == "REAL"
        assert row.total_allocated_capital == 0.0
        assert row.available_capital == 0.0
        assert row.realized_pnl_today == 0.0
        assert row.realized_pnl_total == 0.0
        assert row.pnl_last_reset_date == ist_today_str()

    def test_second_call_returns_same_row(self, env):
        db, _ = env
        a = ledger._get_or_create(db)
        b = ledger._get_or_create(db)
        assert a.id == b.id
        assert db.query(models.ScalpCapitalLedger).count() == 1


# ══════════════════════════════════════════════════════════════════════════════
# _maybe_lazy_reset_daily
# ══════════════════════════════════════════════════════════════════════════════

class TestMaybeLazyResetDaily:
    """Triggered by _get_or_create when pnl_last_reset_date != today."""

    def test_stale_date_resets_pnl_and_kill_switch(self, env):
        db, _ = env
        row = ledger._get_or_create(db)
        row.pnl_last_reset_date = "2000-01-01"
        row.realized_pnl_today = -5000.0
        row.daily_loss_kill_switch_tripped = True
        row.daily_loss_kill_switch_tripped_date = "2000-01-01"
        db.commit()

        fresh = ledger._get_or_create(db)
        assert fresh.realized_pnl_today == pytest.approx(0.0)
        assert fresh.daily_loss_kill_switch_tripped is False
        assert fresh.daily_loss_kill_switch_tripped_date is None
        assert fresh.pnl_last_reset_date == ist_today_str()

    def test_reset_is_persisted_not_just_in_memory(self, env):
        db, _ = env
        row = ledger._get_or_create(db)
        row.pnl_last_reset_date = "2000-01-01"
        row.realized_pnl_today = -42.0
        db.commit()
        ledger._get_or_create(db)
        db.expire_all()
        stored = db.query(models.ScalpCapitalLedger).filter_by(mode="REAL").one()
        assert stored.realized_pnl_today == pytest.approx(0.0)
        assert stored.pnl_last_reset_date == ist_today_str()

    def test_current_date_does_not_reset(self, env):
        db, _ = env
        row = ledger._get_or_create(db)
        row.realized_pnl_today = -1234.0
        row.daily_loss_kill_switch_tripped = True
        db.commit()

        fresh = ledger._get_or_create(db)
        assert fresh.realized_pnl_today == pytest.approx(-1234.0)
        assert fresh.daily_loss_kill_switch_tripped is True

    def test_peer_pnl_also_reset_on_stale_date(self, env):
        db, _ = env
        row = ledger._get_or_create(db)
        row.pnl_last_reset_date = "2000-01-01"
        row.peer_realized_pnl_today = 9999.0
        db.commit()
        fresh = ledger._get_or_create(db)
        assert fresh.peer_realized_pnl_today == pytest.approx(0.0)

    def test_available_and_total_capital_carry_over(self, env):
        db, _ = env
        row = _seed_ledger(db, total=100_000.0, avail=37_000.0)
        row.pnl_last_reset_date = "2000-01-01"
        row.realized_pnl_total = 1_500.0
        db.commit()
        fresh = ledger._get_or_create(db)
        assert fresh.available_capital == pytest.approx(37_000.0)
        assert fresh.total_allocated_capital == pytest.approx(100_000.0)
        assert fresh.realized_pnl_total == pytest.approx(1_500.0)

    def test_reset_logs_info(self, env, caplog):
        db, _ = env
        row = ledger._get_or_create(db)
        row.pnl_last_reset_date = "2000-01-01"
        db.commit()
        with caplog.at_level(logging.INFO, logger=LOGGER):
            ledger._get_or_create(db)
        assert "lazy daily reset applied" in caplog.text


# ══════════════════════════════════════════════════════════════════════════════
# sync_from_broker
# ══════════════════════════════════════════════════════════════════════════════

class TestSyncFromBroker:
    def test_get_funds_failure_returns_zero(self, env, monkeypatch, caplog):
        db, calls = env

        def _boom(db_):
            raise RuntimeError("network down")

        monkeypatch.setattr(dhan_client, "get_funds", _boom)
        with caplog.at_level(logging.ERROR, logger=LOGGER):
            result = ledger.sync_from_broker(db)
        assert result == pytest.approx(0.0)
        assert "failed to get funds" in caplog.text
        # Bailed out before touching the ledger or the peer/exposure tail.
        assert calls["peer"] == 0 and calls["exposure"] == []
        assert ledger._get_or_create(db).last_synced_from_broker_at is None

    def test_no_balance_field_returns_zero(self, env, monkeypatch, caplog):
        db, calls = env
        _funds(monkeypatch, {"someOtherField": 99})
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            result = ledger.sync_from_broker(db)
        assert result == pytest.approx(0.0)
        assert "no usable available-balance field" in caplog.text
        assert calls["peer"] == 0 and calls["exposure"] == []

    def test_zero_balance_returns_zero(self, env, monkeypatch):
        db, calls = env
        _funds(monkeypatch, {"availabelBalance": 0})
        assert ledger.sync_from_broker(db) == pytest.approx(0.0)
        assert calls["exposure"] == []

    def test_negative_balance_returns_zero_and_leaves_ledger(self, env, monkeypatch):
        db, _ = env
        _seed_ledger(db, total=50_000.0, avail=40_000.0)
        _funds(monkeypatch, {"availabelBalance": -10.0})
        assert ledger.sync_from_broker(db) == pytest.approx(0.0)
        row = ledger._get_or_create(db)
        assert row.total_allocated_capital == pytest.approx(50_000.0)
        assert row.available_capital == pytest.approx(40_000.0)

    def test_first_sync_logs_key_warning_and_sets_total(self, env, monkeypatch, caplog):
        db, _ = env
        _funds(monkeypatch, {"availabelBalance": 200_000.0})
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            result = ledger.sync_from_broker(db)
        assert result == pytest.approx(200_000.0 * config.SCALP_POOL_CAPITAL_SHARE_PCT / 100.0)
        assert "using Dhan balance field" in caplog.text
        assert "shifted" not in caplog.text
        assert ledger._last_balance_key == "availabelBalance"

    def test_same_key_second_sync_does_not_warn_again(self, env, monkeypatch, caplog):
        db, _ = env
        _funds(monkeypatch, {"availabelBalance": 200_000.0})
        ledger.sync_from_broker(db)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            ledger.sync_from_broker(db)
        assert "using Dhan balance field" not in caplog.text
        assert "shifted" not in caplog.text

    def test_key_change_logs_shifted_warning(self, env, monkeypatch, caplog):
        db, _ = env
        monkeypatch.setattr(ledger, "_last_balance_key", "availabelBalance")
        _funds(monkeypatch, {"availableBalance": 200_000.0})
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            ledger.sync_from_broker(db)
        assert "funds response shape shifted" in caplog.text
        assert "using Dhan balance field" not in caplog.text
        assert ledger._last_balance_key == "availableBalance"

    def test_no_open_positions_resets_available_to_total(self, env, monkeypatch):
        db, _ = env
        _seed_ledger(db, total=1.0, avail=1.0)  # stale values to be overwritten
        _funds(monkeypatch, {"availabelBalance": 200_000.0})
        ledger.sync_from_broker(db)
        row = ledger._get_or_create(db)
        expected_alloc = 200_000.0 * config.SCALP_POOL_CAPITAL_SHARE_PCT / 100.0
        assert row.total_allocated_capital == pytest.approx(expected_alloc)
        assert row.available_capital == pytest.approx(expected_alloc)
        assert row.last_synced_from_broker_at is not None

    def test_closed_positions_do_not_count_as_committed(self, env, monkeypatch):
        db, calls = env
        _add_position(db, status="CLOSED", capital_risked=8_000.0)
        _funds(monkeypatch, {"availabelBalance": 200_000.0})
        ledger.sync_from_broker(db)
        row = ledger._get_or_create(db)
        expected_alloc = 200_000.0 * config.SCALP_POOL_CAPITAL_SHARE_PCT / 100.0
        assert row.total_allocated_capital == pytest.approx(expected_alloc)
        assert row.available_capital == pytest.approx(expected_alloc)
        assert calls["exposure"] == [pytest.approx(0.0)]

    def test_open_positions_capital_added_back_to_total(self, env, monkeypatch):
        # Capital-erosion fix: total_allocated_capital = scalp_alloc + capital
        # already committed to this pool's own OPEN / EXIT_LEGS_REJECTED rows.
        db, calls = env
        _add_position(db, symbol="A", status="OPEN", capital_risked=5_000.0, sec_id="1")
        _add_position(db, symbol="B", status="EXIT_LEGS_REJECTED", capital_risked=3_000.0, sec_id="2")
        _add_position(db, symbol="C", status="CLOSED", capital_risked=9_999.0, sec_id="3")
        row = ledger._get_or_create(db)
        row.available_capital = 12_345.0
        db.commit()
        _funds(monkeypatch, {"availabelBalance": 200_000.0})
        result = ledger.sync_from_broker(db)
        scalp_alloc = 200_000.0 * config.SCALP_POOL_CAPITAL_SHARE_PCT / 100.0
        assert result == pytest.approx(scalp_alloc)  # returns the free-cash slice only
        fresh = ledger._get_or_create(db)
        assert fresh.total_allocated_capital == pytest.approx(scalp_alloc + 8_000.0)
        # exposure published = committed capital (CLOSED excluded)
        assert calls["exposure"] == [pytest.approx(8_000.0)]

    def test_exit_legs_rejected_alone_blocks_available_hard_reset(self, env, monkeypatch):
        # A stuck EXIT_LEGS_REJECTED row still has capital reserved, so
        # open_count must be > 0 and available_capital must NOT be reset to
        # the full allocation (that would double-spend the reserved capital).
        db, _ = env
        _add_position(db, status="EXIT_LEGS_REJECTED", capital_risked=4_000.0)
        row = ledger._get_or_create(db)
        row.available_capital = 777.0
        db.commit()
        _funds(monkeypatch, {"availabelBalance": 200_000.0})
        ledger.sync_from_broker(db)
        assert ledger._get_or_create(db).available_capital == pytest.approx(777.0)

    def test_open_positions_with_zero_avail_resets_to_scalp_alloc(self, env, monkeypatch):
        db, _ = env
        _add_position(db, capital_risked=5_000.0)
        row = ledger._get_or_create(db)
        row.available_capital = 0.0
        db.commit()
        _funds(monkeypatch, {"availabelBalance": 200_000.0})
        ledger.sync_from_broker(db)
        fresh = ledger._get_or_create(db)
        expected_scalp_alloc = 200_000.0 * config.SCALP_POOL_CAPITAL_SHARE_PCT / 100.0
        # reset to the free-cash slice only — NOT scalp_alloc + committed
        assert fresh.available_capital == pytest.approx(expected_scalp_alloc)
        assert fresh.total_allocated_capital == pytest.approx(expected_scalp_alloc + 5_000.0)

    def test_open_positions_with_negative_avail_also_resets(self, env, monkeypatch):
        # reconcile_position_cost can legitimately push available negative.
        db, _ = env
        _add_position(db)
        row = ledger._get_or_create(db)
        row.available_capital = -250.0
        db.commit()
        _funds(monkeypatch, {"availabelBalance": 200_000.0})
        ledger.sync_from_broker(db)
        expected = 200_000.0 * config.SCALP_POOL_CAPITAL_SHARE_PCT / 100.0
        assert ledger._get_or_create(db).available_capital == pytest.approx(expected)

    def test_open_positions_with_positive_avail_leaves_avail_unchanged(self, env, monkeypatch):
        db, _ = env
        _add_position(db)
        row = ledger._get_or_create(db)
        row.available_capital = 12_345.0
        db.commit()
        _funds(monkeypatch, {"availabelBalance": 200_000.0})
        ledger.sync_from_broker(db)
        assert ledger._get_or_create(db).available_capital == pytest.approx(12_345.0)

    def test_success_syncs_peer_pnl_and_publishes_exposure_once(self, env, monkeypatch):
        db, calls = env
        _funds(monkeypatch, {"availabelBalance": 100_000.0})
        ledger.sync_from_broker(db)
        assert calls["peer"] == 1
        assert calls["exposure"] == [pytest.approx(0.0)]


# ══════════════════════════════════════════════════════════════════════════════
# sync_peer_pnl — the REAL function
# ══════════════════════════════════════════════════════════════════════════════

class TestSyncPeerPnl:
    def test_success_path_caches_peer_pnl(self, real_peer_env):
        db = real_peer_env
        payload = _json.dumps({"account": {"realized_pnl_today": -2500.0}}).encode()
        with patch("urllib.request.urlopen", return_value=_mock_urlopen_response(payload)) as uo:
            result = ledger.sync_peer_pnl(db)

        assert result == pytest.approx(-2500.0)
        row = ledger._get_or_create(db)
        assert row.peer_realized_pnl_today == pytest.approx(-2500.0)
        assert row.peer_pnl_last_synced_at is not None
        # right endpoint, short timeout (must never stall the sync cadence)
        assert uo.call_args.args[0] == f"{config.REAL_TRADE_SERVICE_URL}/status/REAL"
        assert uo.call_args.kwargs["timeout"] == 3

    def test_missing_account_key_caches_zero(self, real_peer_env):
        db = real_peer_env
        row = ledger._get_or_create(db)
        row.peer_realized_pnl_today = 500.0
        db.commit()
        payload = _json.dumps({"something": "else"}).encode()
        with patch("urllib.request.urlopen", return_value=_mock_urlopen_response(payload)):
            result = ledger.sync_peer_pnl(db)
        assert result == pytest.approx(0.0)
        assert ledger._get_or_create(db).peer_realized_pnl_today == pytest.approx(0.0)

    def test_missing_pnl_key_caches_zero(self, real_peer_env):
        db = real_peer_env
        payload = _json.dumps({"account": {}}).encode()
        with patch("urllib.request.urlopen", return_value=_mock_urlopen_response(payload)):
            assert ledger.sync_peer_pnl(db) == pytest.approx(0.0)

    def test_failure_returns_none_and_warns(self, real_peer_env, caplog):
        db = real_peer_env
        with patch("urllib.request.urlopen", side_effect=OSError("peer down")):
            with caplog.at_level(logging.WARNING, logger=LOGGER):
                result = ledger.sync_peer_pnl(db)
        assert result is None
        assert "could not fetch real-trade-service pnl" in caplog.text

    def test_failure_keeps_last_cached_value(self, real_peer_env):
        db = real_peer_env
        row = ledger._get_or_create(db)
        row.peer_realized_pnl_today = -321.0
        db.commit()
        with patch("urllib.request.urlopen", side_effect=OSError("peer down")):
            assert ledger.sync_peer_pnl(db) is None
        assert ledger._get_or_create(db).peer_realized_pnl_today == pytest.approx(-321.0)

    def test_malformed_json_returns_none(self, real_peer_env):
        db = real_peer_env
        with patch("urllib.request.urlopen", return_value=_mock_urlopen_response(b"<html>502</html>")):
            assert ledger.sync_peer_pnl(db) is None

    def test_null_pnl_value_returns_none(self, real_peer_env):
        # key present but null → float(None) raises → swallowed, cache kept
        db = real_peer_env
        row = ledger._get_or_create(db)
        row.peer_realized_pnl_today = 11.0
        db.commit()
        payload = _json.dumps({"account": {"realized_pnl_today": None}}).encode()
        with patch("urllib.request.urlopen", return_value=_mock_urlopen_response(payload)):
            assert ledger.sync_peer_pnl(db) is None
        assert ledger._get_or_create(db).peer_realized_pnl_today == pytest.approx(11.0)


# ══════════════════════════════════════════════════════════════════════════════
# reserve_capital
# ══════════════════════════════════════════════════════════════════════════════

def _expected_position_value(total, stop_pct):
    risk = total * (config.RISK_PER_TRADE_PCT / 100.0) / config.MAX_CONCURRENT_SCALP_POSITIONS
    return risk / (stop_pct / 100.0)


class TestReserveCapital:
    def test_kill_switch_tripped_refuses_entry(self, env, caplog):
        db, _ = env
        _seed_ledger(db)
        row = ledger._get_or_create(db)
        row.daily_loss_kill_switch_tripped = True
        db.commit()
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            result = ledger.reserve_capital(db, adaptive_stop_pct=2.0)
        assert result is None
        assert "kill switch tripped" in caplog.text
        assert ledger._get_or_create(db).available_capital == pytest.approx(90_000.0)

    def test_zero_total_allocated_capital_refuses_entry(self, env, caplog):
        db, _ = env
        row = ledger._get_or_create(db)
        row.total_allocated_capital = 0.0
        row.available_capital = 0.0
        db.commit()
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            result = ledger.reserve_capital(db, adaptive_stop_pct=2.0)
        assert result is None
        assert "run sync_from_broker first" in caplog.text

    def test_insufficient_capital_returns_none_and_does_not_decrement(self, env):
        db, _ = env
        _seed_ledger(db, total=10_000.0, avail=1.0)
        assert ledger.reserve_capital(db, adaptive_stop_pct=2.0) is None
        assert ledger._get_or_create(db).available_capital == pytest.approx(1.0)

    def test_success_returns_position_value_and_decrements(self, env):
        db, _ = env
        _seed_ledger(db, total=100_000.0, avail=90_000.0)
        pv = ledger.reserve_capital(db, adaptive_stop_pct=2.0)
        expected = _expected_position_value(100_000.0, 2.0)
        assert pv == pytest.approx(expected)
        assert ledger._get_or_create(db).available_capital == pytest.approx(90_000.0 - expected)

    def test_wider_stop_means_smaller_position(self, env):
        db, _ = env
        _seed_ledger(db, total=100_000.0, avail=90_000.0)
        narrow = ledger.reserve_capital(db, adaptive_stop_pct=1.0)
        wide = ledger.reserve_capital(db, adaptive_stop_pct=4.0)
        assert narrow == pytest.approx(4 * wide)

    def test_need_equal_to_available_is_allowed(self, env):
        # boundary: refusal is strictly `position_value > available`
        db, _ = env
        need = _expected_position_value(100_000.0, 2.0)
        _seed_ledger(db, total=100_000.0, avail=need)
        assert ledger.reserve_capital(db, adaptive_stop_pct=2.0) == pytest.approx(need)
        assert ledger._get_or_create(db).available_capital == pytest.approx(0.0)

    def test_peer_losses_do_not_block_entry(self, env):
        # Cross-service kill-switch coupling was removed: peer pnl is display-only.
        db, _ = env
        row = _seed_ledger(db, total=100_000.0, avail=90_000.0)
        row.peer_realized_pnl_today = -1_000_000.0
        db.commit()
        assert ledger.reserve_capital(db, adaptive_stop_pct=2.0) is not None


# ══════════════════════════════════════════════════════════════════════════════
# reserve_additional
# ══════════════════════════════════════════════════════════════════════════════

class TestReserveAdditional:
    def test_zero_additional_returns_true_without_touching_ledger(self, env):
        db, _ = env
        _seed_ledger(db, avail=50_000.0)
        assert ledger.reserve_additional(db, 0.0) is True
        assert ledger._get_or_create(db).available_capital == pytest.approx(50_000.0)

    def test_negative_additional_returns_true_without_touching_ledger(self, env):
        db, _ = env
        _seed_ledger(db, avail=50_000.0)
        assert ledger.reserve_additional(db, -100.0) is True
        assert ledger._get_or_create(db).available_capital == pytest.approx(50_000.0)

    def test_zero_additional_is_true_even_when_available_is_negative(self, env):
        # reconcile_position_cost can push available below zero; a zero top-up
        # must still short-circuit to True rather than fall into the
        # `additional > available` refusal (0 > -50).
        db, _ = env
        _seed_ledger(db, avail=-50.0)
        assert ledger.reserve_additional(db, 0.0) is True
        assert ledger._get_or_create(db).available_capital == pytest.approx(-50.0)

    def test_insufficient_available_returns_false_and_leaves_ledger(self, env):
        db, _ = env
        _seed_ledger(db, avail=100.0)
        assert ledger.reserve_additional(db, 500.0) is False
        assert ledger._get_or_create(db).available_capital == pytest.approx(100.0)

    def test_sufficient_available_deducts_and_returns_true(self, env):
        db, _ = env
        _seed_ledger(db, avail=50_000.0)
        assert ledger.reserve_additional(db, 1_000.0) is True
        assert ledger._get_or_create(db).available_capital == pytest.approx(49_000.0)

    def test_exactly_available_is_allowed(self, env):
        db, _ = env
        _seed_ledger(db, avail=500.0)
        assert ledger.reserve_additional(db, 500.0) is True
        assert ledger._get_or_create(db).available_capital == pytest.approx(0.0)


# ══════════════════════════════════════════════════════════════════════════════
# release_capital
# ══════════════════════════════════════════════════════════════════════════════

def _add_gate(db, tripped=False, tripped_date=None):
    gate = models.ScalpGateState(
        mode="REAL", is_armed=True, service_enabled=True, auto_pilot_enabled=True,
        daily_loss_kill_switch_tripped=tripped,
        daily_loss_kill_switch_tripped_date=tripped_date,
    )
    db.add(gate)
    db.commit()
    return gate


def _threshold_loss(total, extra=1.0):
    return -(total * config.MAX_DAILY_LOSS_PCT_OF_POOL / 100.0 + extra)


class TestReleaseCapital:
    def test_profit_returns_capital_plus_pnl_and_accumulates(self, env):
        db, _ = env
        _seed_ledger(db, total=100_000.0, avail=80_000.0)
        ledger.release_capital(db, position_value=5_000.0, realized_pnl=250.0)
        row = ledger._get_or_create(db)
        assert row.available_capital == pytest.approx(85_250.0)
        assert row.realized_pnl_today == pytest.approx(250.0)
        assert row.realized_pnl_total == pytest.approx(250.0)
        assert row.daily_loss_kill_switch_tripped is False

    def test_small_loss_reduces_available_but_does_not_trip(self, env):
        db, _ = env
        _seed_ledger(db, total=100_000.0, avail=95_000.0)
        ledger.release_capital(db, position_value=5_000.0, realized_pnl=-1.0)
        row = ledger._get_or_create(db)
        assert row.available_capital == pytest.approx(99_999.0)
        assert row.realized_pnl_today == pytest.approx(-1.0)
        assert row.daily_loss_kill_switch_tripped is False

    def test_loss_trips_kill_switch_and_mirrors_to_gate(self, env):
        db, _ = env
        row = _seed_ledger(db, total=100_000.0, avail=95_000.0)
        _add_gate(db)

        ledger.release_capital(
            db, position_value=5_000.0,
            realized_pnl=_threshold_loss(row.total_allocated_capital),
        )

        fresh_row = ledger._get_or_create(db)
        assert fresh_row.daily_loss_kill_switch_tripped is True
        assert fresh_row.daily_loss_kill_switch_tripped_date == ist_today_str()

        db.expire_all()
        gate = db.query(models.ScalpGateState).filter_by(mode="REAL").first()
        assert gate.daily_loss_kill_switch_tripped is True
        assert gate.daily_loss_kill_switch_tripped_date == ist_today_str()

    def test_loss_exactly_at_threshold_trips(self, env, monkeypatch):
        # boundary: `loss_pct >= MAX` (not `>`). Numbers chosen to be exact in
        # binary float: 5000 / 100000 * 100 == 5.0.
        monkeypatch.setattr(config, "MAX_DAILY_LOSS_PCT_OF_POOL", 5.0)
        db, _ = env
        _seed_ledger(db, total=100_000.0, avail=95_000.0)
        ledger.release_capital(db, position_value=0.0, realized_pnl=-5_000.0)
        assert ledger._get_or_create(db).daily_loss_kill_switch_tripped is True

    def test_loss_just_below_threshold_does_not_trip(self, env, monkeypatch):
        monkeypatch.setattr(config, "MAX_DAILY_LOSS_PCT_OF_POOL", 5.0)
        db, _ = env
        _seed_ledger(db, total=100_000.0, avail=95_000.0)
        ledger.release_capital(db, position_value=0.0, realized_pnl=-4_999.0)
        assert ledger._get_or_create(db).daily_loss_kill_switch_tripped is False

    def test_threshold_uses_cumulative_realized_today(self, env, monkeypatch):
        monkeypatch.setattr(config, "MAX_DAILY_LOSS_PCT_OF_POOL", 5.0)
        db, _ = env
        _seed_ledger(db, total=100_000.0, avail=95_000.0)
        ledger.release_capital(db, position_value=0.0, realized_pnl=-3_000.0)
        assert ledger._get_or_create(db).daily_loss_kill_switch_tripped is False
        ledger.release_capital(db, position_value=0.0, realized_pnl=-2_000.0)
        assert ledger._get_or_create(db).daily_loss_kill_switch_tripped is True

    def test_profit_offsets_earlier_loss_for_the_threshold(self, env, monkeypatch):
        monkeypatch.setattr(config, "MAX_DAILY_LOSS_PCT_OF_POOL", 5.0)
        db, _ = env
        _seed_ledger(db, total=100_000.0, avail=95_000.0)
        ledger.release_capital(db, position_value=0.0, realized_pnl=-4_000.0)
        ledger.release_capital(db, position_value=0.0, realized_pnl=+2_000.0)
        ledger.release_capital(db, position_value=0.0, realized_pnl=-2_500.0)  # net -4500
        assert ledger._get_or_create(db).daily_loss_kill_switch_tripped is False

    def test_zero_total_allocated_never_trips_and_does_not_divide_by_zero(self, env):
        db, _ = env
        row = ledger._get_or_create(db)
        row.total_allocated_capital = 0.0
        row.available_capital = 0.0
        db.commit()
        ledger.release_capital(db, position_value=100.0, realized_pnl=-1_000_000.0)
        assert ledger._get_or_create(db).daily_loss_kill_switch_tripped is False

    def test_trip_without_gate_row_does_not_crash(self, env):
        db, _ = env
        row = _seed_ledger(db, total=100_000.0, avail=95_000.0)
        assert db.query(models.ScalpGateState).count() == 0
        ledger.release_capital(
            db, position_value=5_000.0,
            realized_pnl=_threshold_loss(row.total_allocated_capital),
        )
        assert ledger._get_or_create(db).daily_loss_kill_switch_tripped is True

    def test_gate_already_tripped_keeps_its_own_date(self, env):
        db, _ = env
        row = _seed_ledger(db, total=100_000.0, avail=95_000.0)
        _add_gate(db, tripped=True, tripped_date="1999-12-31")
        ledger.release_capital(
            db, position_value=5_000.0,
            realized_pnl=_threshold_loss(row.total_allocated_capital),
        )
        db.expire_all()
        gate = db.query(models.ScalpGateState).filter_by(mode="REAL").first()
        assert gate.daily_loss_kill_switch_tripped is True
        assert gate.daily_loss_kill_switch_tripped_date == "1999-12-31"

    def test_already_tripped_ledger_does_not_retrip_or_rewrite_date(self, env):
        db, _ = env
        row = _seed_ledger(db, total=100_000.0, avail=95_000.0)
        row.daily_loss_kill_switch_tripped = True
        row.daily_loss_kill_switch_tripped_date = "1999-12-31"
        db.commit()
        _add_gate(db, tripped=False)  # gate NOT mirrored yet — must stay untouched

        ledger.release_capital(
            db, position_value=5_000.0,
            realized_pnl=_threshold_loss(row.total_allocated_capital),
        )

        fresh = ledger._get_or_create(db)
        assert fresh.daily_loss_kill_switch_tripped is True
        assert fresh.daily_loss_kill_switch_tripped_date == "1999-12-31"
        db.expire_all()
        gate = db.query(models.ScalpGateState).filter_by(mode="REAL").first()
        assert gate.daily_loss_kill_switch_tripped is False

    def test_trip_logs_warning(self, env, caplog):
        db, _ = env
        row = _seed_ledger(db, total=100_000.0, avail=95_000.0)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            ledger.release_capital(
                db, position_value=5_000.0,
                realized_pnl=_threshold_loss(row.total_allocated_capital),
            )
        assert "DAILY LOSS KILL SWITCH TRIPPED" in caplog.text


# ══════════════════════════════════════════════════════════════════════════════
# reconcile_position_cost
# ══════════════════════════════════════════════════════════════════════════════

class TestReconcilePositionCost:
    def test_delta_zero_returns_immediately(self, env):
        db, _ = env
        _seed_ledger(db, avail=50_000.0)
        ledger.reconcile_position_cost(db, delta=0)
        assert ledger._get_or_create(db).available_capital == pytest.approx(50_000.0)

    def test_delta_zero_is_silent_even_when_available_already_negative(self, env, caplog):
        # the early return must precede the "went negative" warning check
        db, _ = env
        _seed_ledger(db, avail=-100.0)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            ledger.reconcile_position_cost(db, delta=0)
        assert ledger._get_or_create(db).available_capital == pytest.approx(-100.0)
        assert "went negative" not in caplog.text
        assert "adjusted" not in caplog.text

    def test_positive_delta_deducts_from_available(self, env, caplog):
        db, _ = env
        _seed_ledger(db, avail=50_000.0)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            ledger.reconcile_position_cost(db, delta=500.0)
        assert ledger._get_or_create(db).available_capital == pytest.approx(49_500.0)
        assert "went negative" not in caplog.text
        assert "adjusted" in caplog.text

    def test_positive_delta_exceeding_available_goes_negative_with_warning(self, env, caplog):
        db, _ = env
        _seed_ledger(db, avail=100.0)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            ledger.reconcile_position_cost(db, delta=500.0)
        assert ledger._get_or_create(db).available_capital == pytest.approx(-400.0)
        assert "went negative" in caplog.text

    def test_delta_to_exactly_zero_available_is_not_a_warning(self, env, caplog):
        db, _ = env
        _seed_ledger(db, avail=500.0)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            ledger.reconcile_position_cost(db, delta=500.0)
        assert ledger._get_or_create(db).available_capital == pytest.approx(0.0)
        assert "went negative" not in caplog.text

    def test_negative_delta_returns_freed_capital(self, env):
        db, _ = env
        _seed_ledger(db, avail=50_000.0)
        ledger.reconcile_position_cost(db, delta=-200.0)
        assert ledger._get_or_create(db).available_capital == pytest.approx(50_200.0)

    def test_does_not_touch_pnl_or_kill_switch(self, env):
        db, _ = env
        _seed_ledger(db, avail=50_000.0)
        ledger.reconcile_position_cost(db, delta=100_000.0)
        row = ledger._get_or_create(db)
        assert row.realized_pnl_today == 0.0 and row.realized_pnl_total == 0.0
        assert row.daily_loss_kill_switch_tripped is False


# ══════════════════════════════════════════════════════════════════════════════
# reclaim_premature_release
# ══════════════════════════════════════════════════════════════════════════════

class TestReclaimPrematureRelease:
    def test_zero_capital_risked_is_a_no_op(self, env, caplog):
        db, _ = env
        _seed_ledger(db, avail=50_000.0)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            ledger.reclaim_premature_release(db, capital_risked=0.0)
        assert ledger._get_or_create(db).available_capital == pytest.approx(50_000.0)
        assert "reclaiming" not in caplog.text  # returned before the warning

    def test_negative_capital_risked_is_a_no_op(self, env):
        db, _ = env
        _seed_ledger(db, avail=50_000.0)
        ledger.reclaim_premature_release(db, capital_risked=-100.0)
        assert ledger._get_or_create(db).available_capital == pytest.approx(50_000.0)

    def test_positive_capital_risked_deducts_and_warns(self, env, caplog):
        db, _ = env
        _seed_ledger(db, avail=50_000.0)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            ledger.reclaim_premature_release(db, capital_risked=5_000.0)
        assert ledger._get_or_create(db).available_capital == pytest.approx(45_000.0)
        assert "reclaiming" in caplog.text

    def test_does_not_book_pnl_or_trip_kill_switch(self, env):
        db, _ = env
        _seed_ledger(db, total=100.0, avail=50.0)
        ledger.reclaim_premature_release(db, capital_risked=1_000_000.0)
        row = ledger._get_or_create(db)
        assert row.realized_pnl_today == 0.0 and row.realized_pnl_total == 0.0
        assert row.daily_loss_kill_switch_tripped is False


# ══════════════════════════════════════════════════════════════════════════════
# reset_daily
# ══════════════════════════════════════════════════════════════════════════════

class TestResetDaily:
    def test_manual_reset_clears_pnl_and_kill_switch(self, env):
        db, _ = env
        _seed_ledger(db)
        row = ledger._get_or_create(db)
        row.realized_pnl_today = -3000.0
        row.peer_realized_pnl_today = -1000.0
        row.daily_loss_kill_switch_tripped = True
        row.daily_loss_kill_switch_tripped_date = "2000-01-01"
        db.commit()

        ledger.reset_daily(db)

        fresh = ledger._get_or_create(db)
        assert fresh.realized_pnl_today == pytest.approx(0.0)
        assert fresh.peer_realized_pnl_today == pytest.approx(0.0)
        assert fresh.daily_loss_kill_switch_tripped is False
        assert fresh.daily_loss_kill_switch_tripped_date is None
        assert fresh.pnl_last_reset_date == ist_today_str()

    def test_reset_daily_leaves_available_and_totals_untouched(self, env):
        db, _ = env
        row = _seed_ledger(db, total=77_000.0, avail=42_000.0)
        row.realized_pnl_total = 1_234.0
        db.commit()
        ledger.reset_daily(db)
        fresh = ledger._get_or_create(db)
        assert fresh.available_capital == pytest.approx(42_000.0)
        assert fresh.total_allocated_capital == pytest.approx(77_000.0)
        assert fresh.realized_pnl_total == pytest.approx(1_234.0)

    def test_reset_then_reserve_works_again(self, env):
        db, _ = env
        _seed_ledger(db, total=100_000.0, avail=90_000.0)
        row = ledger._get_or_create(db)
        row.daily_loss_kill_switch_tripped = True
        db.commit()
        assert ledger.reserve_capital(db, adaptive_stop_pct=2.0) is None
        ledger.reset_daily(db)
        assert ledger.reserve_capital(db, adaptive_stop_pct=2.0) is not None


# ══════════════════════════════════════════════════════════════════════════════
# get_state
# ══════════════════════════════════════════════════════════════════════════════

class TestGetState:
    def test_fresh_ledger_state(self, env):
        db, _ = env
        state = ledger.get_state(db)
        assert state == {
            "total_allocated_capital": 0.0,
            "available_capital": 0.0,
            "realized_pnl_today": 0.0,
            "realized_pnl_total": 0.0,
            "peer_realized_pnl_today": 0.0,
            "peer_pnl_last_synced_at": None,
            "last_synced_from_broker_at": None,
            "daily_loss_kill_switch_tripped": False,
        }

    def test_populated_state_reports_values_and_utc_timestamps(self, env):
        db, _ = env
        row = _seed_ledger(db, total=100_000.0, avail=60_000.0)
        row.realized_pnl_today = -10.0
        row.realized_pnl_total = 99.0
        row.peer_realized_pnl_today = -5.0
        row.daily_loss_kill_switch_tripped = True
        row.last_synced_from_broker_at = datetime(2026, 9, 25, 4, 7, 0)  # naive, as SQLite returns
        row.peer_pnl_last_synced_at = datetime(2026, 9, 25, 4, 8, 0)
        db.commit()
        db.expire_all()

        state = ledger.get_state(db)
        assert state["total_allocated_capital"] == pytest.approx(100_000.0)
        assert state["available_capital"] == pytest.approx(60_000.0)
        assert state["realized_pnl_today"] == pytest.approx(-10.0)
        assert state["realized_pnl_total"] == pytest.approx(99.0)
        assert state["peer_realized_pnl_today"] == pytest.approx(-5.0)
        assert state["daily_loss_kill_switch_tripped"] is True
        # iso_utc must stamp an explicit UTC offset on naive DB datetimes
        assert state["last_synced_from_broker_at"] == "2026-09-25T04:07:00+00:00"
        assert state["peer_pnl_last_synced_at"] == "2026-09-25T04:08:00+00:00"


# ══════════════════════════════════════════════════════════════════════════════
# book_late_realized_pnl
# ══════════════════════════════════════════════════════════════════════════════

class TestBookLateRealizedPnl:
    def test_books_total_and_available_but_not_today(self, env):
        db, _ = env
        row = _seed_ledger(db, total=100_000.0, avail=50_000.0)
        row.realized_pnl_today = -100.0
        row.realized_pnl_total = 1_000.0
        db.commit()

        ledger.book_late_realized_pnl(db, 300.0)

        fresh = ledger._get_or_create(db)
        assert fresh.available_capital == pytest.approx(50_300.0)
        assert fresh.realized_pnl_total == pytest.approx(1_300.0)
        assert fresh.realized_pnl_today == pytest.approx(-100.0)  # untouched

    def test_late_loss_never_trips_todays_kill_switch(self, env):
        db, _ = env
        _seed_ledger(db, total=100_000.0, avail=50_000.0)
        _add_gate(db)
        ledger.book_late_realized_pnl(db, -50_000.0)  # far beyond the daily limit
        fresh = ledger._get_or_create(db)
        assert fresh.available_capital == pytest.approx(0.0)
        assert fresh.realized_pnl_total == pytest.approx(-50_000.0)
        assert fresh.realized_pnl_today == pytest.approx(0.0)
        assert fresh.daily_loss_kill_switch_tripped is False
        db.expire_all()
        gate = db.query(models.ScalpGateState).filter_by(mode="REAL").first()
        assert gate.daily_loss_kill_switch_tripped is False
