"""group 395: stop basis tag (bars / opening_plan / legacy) on AdaptiveLevels, persisted on the position, migrated onto an existing table,
and exported (with the stop / target / breakeven / signal fields) by GET /trades/history so scripts/opening_replay.py can judge each trade against its own stop."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine, inspect, text

import config
import db as dbmod
import models
from orders import adaptive, entry
from screening import opening_plan

# fixtures / helpers of the entry and main suites
from tests.test_entry import env, cand  # noqa: F401
from tests.test_main import client, db, _position  # noqa: F401


class TestAdaptiveBasis:
    @pytest.fixture(autouse=True)
    def bar_mode(self, monkeypatch):
        monkeypatch.setattr(config, "ADAPTIVE_BAR_ATR_ENABLED", True)
        monkeypatch.setattr(config, "SCALP_TARGET_CAP_PCT", 0.0)
        monkeypatch.setattr(adaptive, "_intraday_range_position", lambda s, p: None)
        monkeypatch.setattr(adaptive, "_atr_proxy_pct", lambda s, p: None)

    def test_live_bars_are_tagged_bars(self, monkeypatch):
        monkeypatch.setattr(adaptive, "_bar_atr_pct", lambda s, p: 1.0)
        assert adaptive.compute(0.0, 100.0, symbol="AAA").basis == "bars"

    def test_the_opening_plan_is_tagged_opening_plan(self, monkeypatch):
        monkeypatch.setattr(adaptive, "_bar_atr_pct", lambda s, p: None)
        monkeypatch.setattr(opening_plan, "bar_atr_pct", lambda s, now=None: 0.75)
        lv = adaptive.compute(0.0, 100.0, symbol="AAA")
        assert lv.basis == "opening_plan" and lv.stop_pct == pytest.approx(0.975, abs=0.01)

    def test_no_bars_and_no_plan_is_legacy(self, monkeypatch):
        monkeypatch.setattr(adaptive, "_bar_atr_pct", lambda s, p: None)
        monkeypatch.setattr(opening_plan, "bar_atr_pct", lambda s, now=None: None)
        assert adaptive.compute(0.0, 100.0, symbol="AAA").basis == "legacy"

    def test_a_plan_lookup_error_is_legacy_not_opening_plan(self, monkeypatch):
        monkeypatch.setattr(adaptive, "_bar_atr_pct", lambda s, p: None)

        def boom(symbol, now=None):
            raise RuntimeError("x")
        monkeypatch.setattr(opening_plan, "bar_atr_pct", boom)
        assert adaptive.compute(0.0, 100.0, symbol="AAA").basis == "legacy"

    def test_bar_mode_off_is_legacy(self, monkeypatch):
        monkeypatch.setattr(config, "ADAPTIVE_BAR_ATR_ENABLED", False)
        assert adaptive.compute(1.0, 100.0, symbol="AAA").basis == "legacy"

    def test_the_target_cap_rebuild_keeps_the_tag(self, monkeypatch):
        monkeypatch.setattr(adaptive, "_bar_atr_pct", lambda s, p: None)
        monkeypatch.setattr(opening_plan, "bar_atr_pct", lambda s, now=None: 4.0)
        monkeypatch.setattr(config, "SCALP_TARGET_CAP_PCT", 1.0)
        monkeypatch.setattr(config, "SCALP_TARGET_CAP_MIN_RR", 1.2)
        monkeypatch.setattr(config, "SCALP_TARGET_CAP_MIN_STOP_PCT", 0.5)
        lv = adaptive.compute(0.0, 100.0, symbol="AAA")
        assert lv.target_pct == pytest.approx(1.0) and lv.basis == "opening_plan"

    def test_default_is_legacy(self):
        assert adaptive.AdaptiveLevels(target_pct=1.0, stop_pct=0.5, target_price=101.0, stop_price=99.5).basis == "legacy"


class TestStopBasisHelper:
    @pytest.mark.parametrize("levels_basis,expected", [("bars", "bars"), ("opening_plan", "opening_plan"), ("", None), (None, None), (3, None)])
    def test_only_a_non_empty_string_is_stored(self, levels_basis, expected):
        class L:
            basis = levels_basis
        assert entry._stop_basis(L()) == expected

    def test_levels_without_the_attribute_give_none(self):
        assert entry._stop_basis(object()) is None


class TestPersisted:
    def _levels_with(self, monkeypatch, basis):
        def _levels(pct_change, ltp, symbol=None):
            return adaptive.AdaptiveLevels(target_pct=1.0, stop_pct=1.0, target_price=round(ltp * 1.01, 2),
                                           stop_price=round(ltp * 0.99, 2), breakeven_trigger_pct=0.5, basis=basis)
        monkeypatch.setattr(entry, "compute_levels", _levels)

    def test_auto_entry_stores_the_basis(self, env, monkeypatch):
        db, _, _ = env
        self._levels_with(monkeypatch, "opening_plan")
        assert entry.attempt_entry(db, cand()) is not None
        assert db.query(models.ScalpPosition).one().stop_basis == "opening_plan"

    def test_manual_entry_stores_the_basis(self, env, monkeypatch):
        db, _, _ = env
        self._levels_with(monkeypatch, "bars")
        assert entry.attempt_manual_entry(db, "ABC", 500.0).stop_basis == "bars"

    def test_the_default_levels_of_the_entry_suite_store_legacy(self, env):
        db, _, _ = env
        entry.attempt_entry(db, cand())
        assert db.query(models.ScalpPosition).one().stop_basis == "legacy"


class TestMigration:
    def test_migration_tuple_is_registered_and_nullable(self):
        rows = [m for m in dbmod._COLUMN_MIGRATIONS if m[0] == "scalp_positions" and m[1] == "stop_basis"]
        assert rows == [("scalp_positions", "stop_basis", "VARCHAR2(16)", "VARCHAR(16)", None, None)]

    def test_column_is_added_to_a_table_that_predates_it(self, monkeypatch):
        eng = create_engine("sqlite:///:memory:")
        with eng.begin() as c:
            c.execute(text("CREATE TABLE scalp_positions (id INTEGER PRIMARY KEY, symbol VARCHAR(32))"))
        monkeypatch.setattr(dbmod, "dialect", lambda: "postgres")
        monkeypatch.setattr(dbmod, "_COLUMN_MIGRATIONS", [m for m in dbmod._COLUMN_MIGRATIONS if m[1] == "stop_basis"])
        dbmod._ensure_columns(eng)
        assert "stop_basis" in {c["name"] for c in inspect(eng).get_columns("scalp_positions")}
        dbmod._ensure_columns(eng)                      # idempotent


class TestTradesHistoryExport:
    def test_new_fields_are_exported(self, client, db):
        _position(db, symbol="NEW", status="STOP_HIT", realized_pnl=-40.0, exit_price=98.0, stop_basis="opening_plan",
                  breakeven_trigger_pct=0.8, stop_moved_to_breakeven=True, signal_price=99.9,
                  opened_at=datetime.now(timezone.utc), closed_at=datetime.now(timezone.utc))
        t = client.get("/trades/history").json()["trades"][0]
        assert (t["stop_price"], t["target_price"]) == (98.0, 103.0)
        assert (t["adaptive_stop_pct"], t["adaptive_target_pct"]) == (2.0, 3.0)
        assert t["breakeven_trigger_pct"] == 0.8 and t["stop_moved_to_breakeven"] is True
        assert t["signal_price"] == 99.9 and t["stop_basis"] == "opening_plan"

    def test_rows_from_before_the_build_export_nulls(self, client, db):
        _position(db, symbol="OLD", status="TARGET_HIT", realized_pnl=10.0, exit_price=101.0,
                  opened_at=datetime.now(timezone.utc), closed_at=datetime.now(timezone.utc))
        t = client.get("/trades/history").json()["trades"][0]
        assert t["stop_basis"] is None and t["signal_price"] is None and t["breakeven_trigger_pct"] is None

    def test_export_feeds_the_replay_tool(self, client, db, tmp_path):
        import json
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), "scripts"))
        import opening_replay as orp
        ist = timezone(timedelta(hours=5, minutes=30))
        opened = datetime(2026, 10, 14, 9, 20, tzinfo=ist).astimezone(timezone.utc)
        _position(db, symbol="RPL", status="STOP_HIT", realized_pnl=-20.0, exit_price=98.0, quantity=10, stop_basis="opening_plan",
                  max_price_seen=101.2, min_price_seen=98.0, opened_at=opened, closed_at=opened + timedelta(minutes=4))
        p = tmp_path / "h.json"
        p.write_text(json.dumps(client.get("/trades/history").json()))
        g = orp.replay(orp.load([str(p)]))["stops"]["groups"]
        assert g["opening_plan"]["trades"] == 1 and g["opening_plan"]["stop_outs"] == 1
        assert g["opening_plan"]["stopped_after_running_half_r"] == 1      # ran +1.2% on a 2% stop (0.6R) before the stop
        assert g["opening_plan"]["median_stop_pct"] == 2.0
