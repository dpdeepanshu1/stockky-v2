"""
tests/test_db_postgres_live.py

OPTIONAL companion to tests/test_db.py: runs db.py's Postgres-branch SQL
against a REAL PostgreSQL server (embedded, via the `pgserver` package), because
SQLite cannot execute two of the statements — `INTERVAL '24 hours'` in
_fix_stale_dhan_token_expiry — and is more forgiving than Postgres about the
rest (NOT NULL ADD COLUMN, DEFAULT TRUE/FALSE, CREATE INDEX IF NOT EXISTS,
UPDATE ... IN (SELECT ...)).

Skipped automatically when `pgserver` is not installed, so the normal suite
(and the VM's `pytest --cov` run) is unaffected. To run it:

    pip install pgserver
    python3 -m pytest tests/test_db_postgres_live.py -q

The Oracle branch cannot be executed anywhere but Oracle; it is covered
statically in tests/test_db.py (recorded SQL, per-function parity with this
branch, type/length/nullability/default checked against models.py).
"""
from __future__ import annotations

import logging
import os
import sys
import uuid
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

pgserver = pytest.importorskip("pgserver")

from sqlalchemy import MetaData, Table, create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

import config
import db
import models

LOGGER = "real-trade-db"
COLUMN_FUNCS = [n for n in dir(db) if n.startswith("_ensure_") and n.endswith(("_columns", "_column"))
                and n not in ("_ensure_oracle_autoincrement",)]


@pytest.fixture(scope="module")
def pg_server(tmp_path_factory):
    srv = pgserver.get_server(tmp_path_factory.mktemp("pgdata"))
    yield srv
    srv.cleanup()


@pytest.fixture()
def pg(pg_server, monkeypatch):
    """A brand-new empty database per test, wired into the db module as the
    'postgresql' dialect."""
    name = "t" + uuid.uuid4().hex[:10]
    pg_server.psql(f"CREATE DATABASE {name};")
    uri = pg_server.get_uri(name)
    eng = create_engine(uri)
    monkeypatch.setattr(db, "_engine", eng)
    monkeypatch.setattr(db, "_SessionLocal", None)
    monkeypatch.delenv("ORACLE_DSN", raising=False)
    monkeypatch.setattr(config, "DATABASE_URL", uri)
    assert db.dialect() == "postgresql"
    yield eng
    eng.dispose()


def _migrated_columns():
    """(table, column) pairs the Postgres branch adds — discovered by running
    every migration against a skeleton (PK-only) SQLite schema."""
    from sqlalchemy import event

    eng = create_engine("sqlite:///:memory:")
    md = MetaData()
    for t in models.Base.metadata.sorted_tables:
        Table(t.name, md, *[c._copy() for c in t.primary_key.columns])
    md.create_all(eng)
    seen = []

    @event.listens_for(eng, "before_cursor_execute")
    def _r(conn, cur, st, params, ctx, many):
        if st.startswith("ALTER TABLE"):
            seen.append(st)

    for fn in COLUMN_FUNCS:
        getattr(db, fn)(eng, "postgresql")
    import re

    return {(m.group(1), m.group(2)) for s in seen if (m := re.match(r"ALTER TABLE (\w+) ADD COLUMN (\w+)", s))}


def _make_legacy(eng):
    mig = _migrated_columns()
    md = MetaData()
    for t in models.Base.metadata.sorted_tables:
        Table(t.name, md, *[c._copy() for c in t.columns if (t.name, c.name) not in mig])
    md.create_all(eng)
    return md, mig


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING and r.name == LOGGER]


def test_fresh_database_init_schema_is_clean_and_idempotent(pg, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        db.init_schema()
        db.init_schema()
    assert _warnings(caplog) == []
    assert set(inspect(pg).get_table_names()) == {t.name for t in models.Base.metadata.sorted_tables}


def test_legacy_database_is_upgraded_to_the_model_schema_with_zero_warnings(pg, caplog):
    md, mig = _make_legacy(pg)
    assert len(mig) >= 59
    with pg.begin() as conn:
        conn.execute(md.tables["trade_gate_state"].insert().values(mode="REAL", admin_authenticated=False, dhan_connected=True,
                                                                   risk_config_confirmed=False, armed=False))
    with caplog.at_level(logging.INFO, logger=LOGGER):
        db.init_schema()
    # Unlike SQLite this includes _fix_stale_dhan_token_expiry: Postgres accepts INTERVAL '24 hours'.
    assert _warnings(caplog) == []
    insp = inspect(pg)
    for t in models.Base.metadata.sorted_tables:
        cols = {c["name"]: c for c in insp.get_columns(t.name)}
        assert set(cols) == {c.name for c in t.columns}, t.name
        for c in t.columns:
            if not c.primary_key:
                assert cols[c.name]["nullable"] == c.nullable, f"{t.name}.{c.name}"
    s = sessionmaker(bind=pg)()
    try:
        for mapper in models.Base.registry.mappers:
            s.query(mapper.class_).all()
        gate = s.query(models.TradeGateState).one()
        assert gate.auto_pilot_enabled is False
        assert gate.overnight_hold_enabled is True      # DEFAULT TRUE applied to the pre-existing row
        assert gate.edis_morning_check_enabled is True
    finally:
        s.close()
    # second boot: nothing left to do, still silent
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        db.init_schema()
    assert _warnings(caplog) == []
    assert "added " not in caplog.text


def test_stale_token_expiry_is_capped_to_24h_and_only_when_needed(pg, caplog):
    db.init_schema()
    issued = datetime(2026, 9, 1, 3, 0, 0)
    rows = {
        "stale_30d": (issued, issued + timedelta(days=30)),
        "ok_5h": (issued, issued + timedelta(hours=5)),
        "exactly_24h": (issued, issued + timedelta(hours=24)),
        "no_expiry": (issued, None),
        "no_issue": (None, issued + timedelta(days=30)),
    }
    ids = {}
    with pg.begin() as conn:
        for key, (i, e) in rows.items():
            ids[key] = conn.execute(text(
                "INSERT INTO trade_credentials (dhan_client_id_masked, dhan_client_id_encrypted, access_token_encrypted, "
                "token_issued_at, token_expires_at, updated_at) VALUES ('m','c','t', :i, :e, now()) RETURNING id"
            ), {"i": i, "e": e}).scalar()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        db._fix_stale_dhan_token_expiry(pg)
    assert "capped 1 stale trade_credentials.token_expires_at row(s) to 24h" in caplog.text
    with pg.connect() as conn:
        got = {k: conn.execute(text("SELECT token_expires_at FROM trade_credentials WHERE id = :i"), {"i": v}).scalar()
               for k, v in ids.items()}
    assert got["stale_30d"] == issued + timedelta(hours=24)
    assert got["ok_5h"] == issued + timedelta(hours=5)
    assert got["exactly_24h"] == issued + timedelta(hours=24)
    assert got["no_expiry"] is None
    assert got["no_issue"] == issued + timedelta(days=30)      # can't cap without an issue time
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        db._fix_stale_dhan_token_expiry(pg)                     # idempotent
    assert "capped" not in caplog.text


def test_broker_imported_backfill_on_real_postgres(pg, caplog):
    db.init_schema()
    T = models.Base.metadata.tables
    imported = "Imported from Dhan demat holdings (qty 10)"
    ids = {}
    with pg.begin() as conn:
        for key, sym, flag in [("imported", "AAA", False), ("plain", "BBB", False), ("wrong_type", "CCC", False), ("already", "DDD", True)]:
            ids[key] = conn.execute(T["trade_positions"].insert().values(
                mode="REAL", symbol=sym, status="OPEN", qty_open=1, avg_entry_price=1.0, current_stop=1.0, current_target=2.0,
                unrealized_pnl=0.0, realized_pnl=0.0, opened_at=datetime(2026, 9, 1), broker_imported=flag,
            )).inserted_primary_key[0]
        for key, etype, detail in [("imported", "OPENED", imported), ("wrong_type", "CLOSED", imported), ("already", "OPENED", imported)]:
            conn.execute(T["trade_position_events"].insert().values(
                position_id=ids[key], event_type=etype, detail=detail, occurred_at=datetime(2026, 9, 1)))
    with caplog.at_level(logging.INFO, logger=LOGGER):
        db._backfill_broker_imported_flag(pg)
    assert "backfilled broker_imported=True on 1 pre-existing" in caplog.text
    with pg.connect() as conn:
        flags = dict(conn.execute(text("SELECT symbol, broker_imported FROM trade_positions")).all())
    assert flags == {"AAA": True, "BBB": False, "CCC": False, "DDD": True}
    assert _warnings(caplog) == []


def test_index_creation_uses_if_not_exists_for_real(pg):
    md = MetaData()
    for t in models.Base.metadata.sorted_tables:       # legacy: tables WITHOUT their model-level indexes
        Table(t.name, md, *[c._copy() for c in t.columns])
    md.create_all(pg)
    db._ensure_hot_path_indexes(pg, "postgresql")
    db._ensure_nextday_watchlist_indexes(pg, "postgresql")
    db._ensure_hot_path_indexes(pg, "postgresql")      # re-run: IF NOT EXISTS
    insp = inspect(pg)
    assert {i["name"] for i in insp.get_indexes("trade_orders")} >= {"ix_trade_orders_mode_created"}
    assert {i["name"] for i in insp.get_indexes("trade_candidates")} >= {"ix_trade_candidates_mode_consumed_recv"}
    assert {i["name"] for i in insp.get_indexes("trade_nextday_watchlist")} >= {
        "ix_nextday_watchlist_mode_date_consumed", "ix_nextday_watchlist_mode_sym_date"}
