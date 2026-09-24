"""
tests/test_db.py

100%-coverage-plan round for db.py (was 7% — 482 of 520 statements never
executed). db.py is the engine/session factory plus every additive schema
migration the service runs on boot. create_all(checkfirst=True) only creates
MISSING TABLES, so any column added to models.py after the first deploy
exists ONLY if an `_ensure_*` function here adds it — and if one is missing,
every ORM query on that table dies with "no such column" / ORA-00904 / an
UndefinedColumn, which is exactly the "session-11 model-vs-migration" bug
class (position-stocks-service /ledger 500). Nothing in the suite ever ran
any of it.

Everything runs against REAL SQLite engines (real inspect(), real ALTER
TABLE, real UPDATE ... IN (SELECT ...), real CREATE INDEX). The only fakes:
  * a before_cursor_execute hook that RECORDS every statement (so the Oracle
    branch — whose SQL SQLite rejects — can still be captured and checked)
    and can be told to make matching statements fail with a chosen message;
  * _FakeOracleEngine for _ensure_oracle_autoincrement, whose SQL
    (user_tab_identity_cols, sequences, triggers) only exists on Oracle;
  * monkeypatched create_engine / build_oracle_engine to check pool settings.

What is covered:
  * _normalize_pg_url — postgres:// scheme, channel_binding stripped from the
    start / middle / end of the query, sslmode added or left alone.
  * dialect / get_engine / get_session_factory / get_db — oracle vs postgres
    detection, engine + factory caching, no-DB behaviour (None / RuntimeError,
    NOT cached), pool sizing (4+4), connect args, session always closed.
  * init_schema — every _ensure_* / fixup is wired in (a new function nobody
    calls is a silent no-op), the order constraint (broker_imported column
    must exist before its backfill runs), Oracle-only autoincrement step
    running first, and a full legacy -> current upgrade through init_schema.
  * The 17 column-migration functions, driven on a REAL "legacy" schema (every
    model column any migration adds is removed first):
      - each adds exactly its own columns, a second run issues zero ALTERs;
      - after all of them the schema equals models.py — same column set, same
        NOT NULL flags — and the ORM can SELECT from every mapped table;
      - pre-existing rows survive and read back with the SAME defaults a
        freshly-inserted row would get (legacy rows must behave like new rows);
      - Oracle branch (recorded SQL): same (table, column) set as Postgres per
        function, NUMBER(1) <-> Boolean, VARCHAR2(n)/String(n) lengths, NOT NULL
        columns all have a DEFAULT (else ADD COLUMN fails on a non-empty table);
      - failure handling: inspect() failure warns and skips; "already exists"
        / ORA-01430 swallowed silently; any other error warns and carries on.
  * DRIFT GUARD (the future-proofing): a frozen snapshot of every model column
    NOT covered by a migration. Adding a column to an existing model without
    an _ensure_* now fails a test that says exactly what to do.
  * The two index ensurers (real CREATE INDEX IF NOT EXISTS, names/columns must
    match the model's own Index objects, Oracle SQL has no IF NOT EXISTS,
    ORA-00955/ORA-01408 swallowed), _backfill_broker_imported_flag (real
    UPDATE semantics, both dialect variants, idempotent),
    _fix_stale_dhan_token_expiry (SQL per dialect, rowcount logging, failure).
  * _ensure_oracle_autoincrement — skip non-`id` PK tables, skip tables that
    already have IDENTITY, sequence start = MAX(id)+1 (or 1 on failure/empty),
    ORA-00955 swallowed, trigger DDL, identity-check and trigger failures warn.

Run from services/real-trade-service:
    python3 -m pytest tests/test_db.py -q --cov=db --cov-report=term-missing
"""
from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import Boolean, DateTime, Float, Integer, MetaData, String, Table, Text, create_engine, event, inspect
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

import config
import db
import models

LOGGER = "real-trade-db"

COLUMN_FUNCS = [
    "_ensure_manual_order_columns",
    "_ensure_account_columns",
    "_ensure_gate_state_columns",
    "_ensure_candidate_overnight_column",
    "_ensure_position_columns",
    "_ensure_exit_retry_columns",
    "_ensure_product_type_columns",
    "_ensure_overnight_hold_columns",
    "_ensure_cost_model_columns",
    "_ensure_watchlist_link_columns",
    "_ensure_source_tab_columns",
    "_ensure_catalyst_price_source_column",
    "_ensure_afterhours_gate_column",
    "_ensure_overnight_hold_toggle_column",
    "_ensure_afterhours_last_run_columns",
    "_ensure_regime_override_columns",
    "_ensure_edis_check_columns",
    "_ensure_fill_notional_column",
]
INDEX_FUNCS = ["_ensure_hot_path_indexes", "_ensure_nextday_watchlist_indexes"]
OTHER_FUNCS = ["_fix_stale_dhan_token_expiry", "_backfill_broker_imported_flag", "_ensure_oracle_autoincrement"]

# Every model column that is NOT added by an _ensure_* migration, i.e. what
# create_all() produced on the very first deploy of each table (snapshot taken
# 2026-09-24, session97). See TestDriftGuard for how this is used.
BASELINE_TEXT = """
market_regime_history: id score recorded_at
stockky_shared_order_budget: id trade_date orders_placed_today updated_at
stockky_shared_service_exposure: service_name open_positions_market_value updated_at
stockky_shared_symbol_lock: id symbol held_by_service held_by_mode claimed_at updated_at
trade_accounts: id mode starting_capital current_equity cash_available realized_pnl_today created_at updated_at
trade_adaptive_metric_history: id metric_name value recorded_at
trade_audit_log: id mode actor action detail occurred_at
trade_credentials: id dhan_client_id_masked dhan_client_id_encrypted access_token_encrypted token_issued_at token_expires_at updated_at
trade_gate_state: id mode admin_authenticated admin_authenticated_at admin_session_expires_at dhan_connected dhan_connected_at risk_config_confirmed risk_config_confirmed_at armed armed_at disarmed_reason updated_at
trade_intraday_restricted: symbol first_detected_at last_detected_at hit_count last_detail
trade_nextday_watchlist: id mode symbol catalyst_type catalyst_source headline priority_score market_date collected_at consumed consumed_at updated_at
trade_pnl: id mode trade_date starting_equity ending_equity realized_pnl trades_count win_count max_drawdown_pct
trade_reconciliation: id mode check_type matched discrepancy_detail triggered_safety_lock checked_at
trade_resilience_cache: key payload_json updated_at
trade_risk_config: id mode risk_per_trade_pct max_daily_loss_pct max_concurrent_positions max_portfolio_risk_pct stale_data_seconds max_tick_volatility_mult allow_pyramiding updated_at updated_by
trade_risk_events: id mode symbol check_name verdict detail occurred_at
trade_watchlist: id mode symbol catalyst_type catalyst_price catalyst_ts horizon_class decay_half_life_days entry_band_pct source_tier conviction_score status missed_reason expires_at created_at updated_at
trade_candidates: id mode symbol source_tab decision_label conviction_score signal_price raw_payload received_at consumed
trade_positions: id mode symbol status qty_open avg_entry_price current_stop current_target unrealized_pnl realized_pnl opened_at closed_at
trade_decisions: id mode candidate_id symbol decision_type action reasoning proposed_qty proposed_price proposed_stop proposed_target risk_verdict risk_verdict_reason created_at
trade_exit_decisions: id position_id action reasoning ltp_at_decision evaluated_at
trade_position_events: id position_id event_type detail occurred_at
trade_orders: id mode decision_id symbol side order_type qty limit_price valid_until status dhan_order_id created_at updated_at
trade_fills: id order_id qty price dhan_trade_id filled_at
trade_order_events: id order_id event_type detail occurred_at
"""
BASELINE = {
    line.split(":")[0]: set(line.split(":", 1)[1].split())
    for line in BASELINE_TEXT.strip().splitlines()
}


# ── engine helpers ────────────────────────────────────────────────────────
def new_engine():
    """SQLite engine that records every statement and can be told to fail
    statements containing a substring:  eng._rules.append((substr, message))."""
    eng = create_engine("sqlite:///:memory:")
    eng._sql = []
    eng._rules = []

    @event.listens_for(eng, "before_cursor_execute")
    def _hook(conn, cursor, statement, params, context, executemany):
        eng._sql.append(statement)
        for needle, message in eng._rules:
            if needle in statement:
                raise OperationalError(statement, params, Exception(message))

    return eng


def alters(eng):
    return [s for s in eng._sql if s.lstrip().upper().startswith("ALTER TABLE")]


def skeleton_engine():
    """Every table with ONLY its primary key column(s)."""
    eng = new_engine()
    md = MetaData()
    for t in models.Base.metadata.sorted_tables:
        Table(t.name, md, *[c._copy() for c in t.primary_key.columns])
    md.create_all(eng)
    eng._sql.clear()
    return eng


def _parse_alters(statements):
    """-> {(table, column): tail-of-DDL} for both dialect shapes."""
    out = {}
    for s in statements:
        s = s.strip()
        m = re.match(r"ALTER TABLE (\w+) ADD COLUMN (\w+) (.+)$", s) or re.match(r"ALTER TABLE (\w+) ADD \((\w+) (.+)\)$", s)
        assert m, f"unparseable ALTER: {s}"
        out[(m.group(1), m.group(2))] = m.group(3)
    return out


@pytest.fixture(scope="module")
def discovered():
    """Runs every column migration against the skeleton schema on both
    dialects and records what each one tries to add:
        {dialect: {func_name: {(table, col): ddl_tail}}}"""
    out = {}
    for dname in ("postgresql", "oracle"):
        per_fn = {}
        for fn in COLUMN_FUNCS:
            eng = skeleton_engine()
            getattr(db, fn)(eng, dname)
            per_fn[fn] = _parse_alters(alters(eng))
        out[dname] = per_fn
    return out


def migrated_set(discovered):
    return {k for per in discovered["postgresql"].values() for k in per}


def legacy_engine(discovered):
    """Full models schema MINUS every column a migration adds, and minus all
    the model-level indexes — i.e. what a database created by an older deploy
    looks like. Returns (engine, legacy_metadata)."""
    mig = migrated_set(discovered)
    eng = new_engine()
    md = MetaData()
    for t in models.Base.metadata.sorted_tables:
        Table(t.name, md, *[c._copy() for c in t.columns if (t.name, c.name) not in mig])
    md.create_all(eng)
    eng._sql.clear()
    return eng, md


def _filler(col):
    t = col.type
    if isinstance(t, Boolean):
        return False
    if isinstance(t, Integer):
        return 1
    if isinstance(t, Float):
        return 1.5
    if isinstance(t, DateTime):
        return datetime(2026, 9, 1, 4, 0, 0)
    if isinstance(t, (String, Text)):
        return "REAL" if col.name == "mode" else "x"
    raise AssertionError(f"no filler for {col} ({t!r})")


def test_filler_covers_every_column_type_the_models_use_and_rejects_unknown_ones():
    from sqlalchemy import Column, LargeBinary

    assert _filler(Column("b", Boolean)) is False
    assert _filler(Column("i", Integer)) == 1
    assert _filler(Column("f", Float)) == 1.5
    assert _filler(Column("d", DateTime)) == datetime(2026, 9, 1, 4, 0, 0)
    assert _filler(Column("mode", String(8))) == "REAL" and _filler(Column("t", Text)) == "x"
    with pytest.raises(AssertionError, match="no filler"):
        _filler(Column("blob", LargeBinary))


def insert_row(eng, table, **overrides):
    """Insert one row filling every NOT NULL column that has no default."""
    vals = dict(overrides)
    for c in table.columns:
        if c.name in vals or c.primary_key:
            continue
        if not c.nullable and c.default is None and c.server_default is None:
            vals[c.name] = _filler(c)
    with eng.begin() as conn:
        res = conn.execute(table.insert().values(**vals))
    return res.inserted_primary_key[0]


@pytest.fixture(autouse=True)
def _isolate_db_module(monkeypatch):
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_SessionLocal", None)
    monkeypatch.delenv("ORACLE_DSN", raising=False)
    monkeypatch.setattr(config, "DATABASE_URL", "sqlite:///:memory:")


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING and r.name == LOGGER]


# ══════════════════════════════════════════════════════════════════════════
# _normalize_pg_url
# ══════════════════════════════════════════════════════════════════════════
class TestNormalizePgUrl:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("postgres://u:p@h/db", "postgresql://u:p@h/db?sslmode=require"),
            ("postgresql://u:p@h/db", "postgresql://u:p@h/db?sslmode=require"),
            ("postgresql://u:p@h/db?a=1", "postgresql://u:p@h/db?a=1&sslmode=require"),
            # existing sslmode (any case) is respected, never doubled
            ("postgresql://u:p@h/db?sslmode=disable", "postgresql://u:p@h/db?sslmode=disable"),
            ("postgresql://u:p@h/db?SSLMODE=verify-full", "postgresql://u:p@h/db?SSLMODE=verify-full"),
            # channel_binding (Neon's default string) is stripped wherever it sits
            ("postgresql://u:p@h/db?channel_binding=require", "postgresql://u:p@h/db?sslmode=require"),
            ("postgresql://u:p@h/db?sslmode=require&channel_binding=require", "postgresql://u:p@h/db?sslmode=require"),
            ("postgresql://u:p@h/db?channel_binding=require&sslmode=require", "postgresql://u:p@h/db?sslmode=require"),
            ("postgres://u:p@h/db?channel_binding=require", "postgresql://u:p@h/db?sslmode=require"),
        ],
    )
    def test_cases(self, raw, expected):
        assert db._normalize_pg_url(raw) == expected

    @pytest.mark.parametrize(
        "raw, expected",
        [
            # REGRESSION (session97): used to leave "a=1&&b=2", which libpq rejects
            # ("missing key/value separator '=' in URI query parameter").
            ("postgresql://u:p@h/db?a=1&channel_binding=require&b=2", "postgresql://u:p@h/db?a=1&b=2&sslmode=require"),
            ("postgresql://u:p@h/db?a=1&channel_binding=x&channel_binding=y&b=2", "postgresql://u:p@h/db?a=1&b=2&sslmode=require"),
        ],
    )
    def test_channel_binding_in_the_middle_leaves_no_empty_parameter(self, raw, expected):
        out = db._normalize_pg_url(raw)
        assert out == expected
        assert "&&" not in out

    def test_result_is_accepted_by_libpq(self):
        psycopg2_ext = pytest.importorskip("psycopg2.extensions")
        out = db._normalize_pg_url("postgres://u:p@h/db?application_name=x&channel_binding=require&connect_timeout=5")
        parsed = psycopg2_ext.parse_dsn(out)
        assert parsed["sslmode"] == "require" and parsed["connect_timeout"] == "5"
        assert "channel_binding" not in parsed


# ══════════════════════════════════════════════════════════════════════════
# dialect / get_engine / get_session_factory / get_db
# ══════════════════════════════════════════════════════════════════════════
class TestDialect:
    def test_postgresql_by_default(self, monkeypatch):
        monkeypatch.setattr(config, "DATABASE_URL", "postgresql://u:p@h/db")
        assert db.dialect() == "postgresql"

    def test_oracle_by_url_scheme(self, monkeypatch):
        monkeypatch.setattr(config, "DATABASE_URL", "oracle+oracledb://u:p@h/svc")
        assert db.dialect() == "oracle"

    def test_oracle_by_env_dsn(self, monkeypatch):
        monkeypatch.setenv("ORACLE_DSN", "mydb_high")
        assert db.dialect() == "oracle"


class TestGetEngine:
    def test_cached_engine_is_returned_as_is(self, monkeypatch):
        sentinel = object()
        monkeypatch.setattr(db, "_engine", sentinel)
        assert db.get_engine() is sentinel

    def test_oracle_branch_uses_4_plus_4_pool_and_caches(self, monkeypatch):
        calls = []

        def fake_build(url, db_pool_size, db_max_overflow):
            calls.append((url, db_pool_size, db_max_overflow))
            return "ORACLE-ENGINE", "wallet-info"

        monkeypatch.setattr(config, "DATABASE_URL", "oracle+oracledb://u:p@h/svc")
        monkeypatch.setattr(db._oc, "build_oracle_engine", fake_build)
        assert db.get_engine() == "ORACLE-ENGINE"
        assert db.get_engine() == "ORACLE-ENGINE"
        assert calls == [("oracle+oracledb://u:p@h/svc", 4, 4)]   # built once, sized for 2 background loops + dashboard

    def test_no_database_configured_returns_none_warns_and_is_not_cached(self, monkeypatch, caplog):
        monkeypatch.setattr(config, "DATABASE_URL", "")
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert db.get_engine() is None
        assert "no DB" in " ".join(_warnings(caplog)) or "has no DB" in caplog.text
        assert db._engine is None
        # configuring a DB afterwards must work — None was never cached
        monkeypatch.setattr(config, "DATABASE_URL", "postgresql://u:p@h/db")
        made = {}
        monkeypatch.setattr(db, "create_engine", lambda url, **kw: made.update(url=url, kw=kw) or "PG-ENGINE")
        assert db.get_engine() == "PG-ENGINE"

    def test_postgres_branch_normalizes_url_and_sets_pool_and_connect_args(self, monkeypatch):
        made = {}
        monkeypatch.setattr(config, "DATABASE_URL", "postgres://u:p@h/db?channel_binding=require")
        monkeypatch.setattr(db, "create_engine", lambda url, **kw: made.update(url=url, kw=kw) or "PG-ENGINE")
        assert db.get_engine() == "PG-ENGINE"
        assert made["url"] == "postgresql://u:p@h/db?sslmode=require"
        assert made["kw"] == {
            "pool_pre_ping": True,
            "pool_size": 4,
            "max_overflow": 4,
            "pool_timeout": 30,
            "connect_args": {"connect_timeout": 10, "application_name": "real-trade-service"},
        }
        assert db._engine == "PG-ENGINE"


class TestSessionFactoryAndGetDb:
    def test_factory_is_built_once_and_bound_to_the_engine(self, monkeypatch):
        eng = new_engine()
        monkeypatch.setattr(db, "_engine", eng)
        f1 = db.get_session_factory()
        assert db.get_session_factory() is f1
        s = f1()
        try:
            assert isinstance(s, Session)
            assert s.get_bind() is eng
            assert s.autoflush is False           # entry/exit engines flush explicitly
        finally:
            s.close()

    def test_no_database_raises_and_does_not_cache(self, monkeypatch):
        monkeypatch.setattr(db, "get_engine", lambda: None)
        with pytest.raises(RuntimeError, match="no database configured"):
            db.get_session_factory()
        assert db._SessionLocal is None

    def test_get_db_yields_a_session_and_closes_it(self, monkeypatch):
        closed = []
        fake = SimpleNamespace(close=lambda: closed.append(True))
        monkeypatch.setattr(db, "get_session_factory", lambda: (lambda: fake))
        gen = db.get_db()
        assert next(gen) is fake
        assert closed == []
        with pytest.raises(StopIteration):
            next(gen)
        assert closed == [True]

    def test_get_db_closes_the_session_when_the_request_raises(self, monkeypatch):
        closed = []
        fake = SimpleNamespace(close=lambda: closed.append(True))
        monkeypatch.setattr(db, "get_session_factory", lambda: (lambda: fake))
        gen = db.get_db()
        next(gen)
        with pytest.raises(ValueError):
            gen.throw(ValueError("handler blew up"))
        assert closed == [True]


# ══════════════════════════════════════════════════════════════════════════
# init_schema
# ══════════════════════════════════════════════════════════════════════════
def _spy_everything(monkeypatch):
    calls = []
    for name in COLUMN_FUNCS + INDEX_FUNCS + OTHER_FUNCS:
        monkeypatch.setattr(db, name, (lambda n: (lambda *a, **k: calls.append((n, a))))(name))
    return calls


class TestInitSchema:
    def test_no_engine_raises(self, monkeypatch):
        monkeypatch.setattr(db, "get_engine", lambda: None)
        with pytest.raises(RuntimeError, match="no database configured"):
            db.init_schema()

    def test_every_ensure_and_fixup_function_is_actually_called(self, monkeypatch):
        # A new _ensure_* function nobody wires into init_schema is a silent no-op.
        in_module = {n for n in dir(db) if n.startswith("_ensure_")} | {"_fix_stale_dhan_token_expiry", "_backfill_broker_imported_flag"}
        assert in_module == set(COLUMN_FUNCS + INDEX_FUNCS + OTHER_FUNCS), (
            "db.py gained/lost a migration function — add it to COLUMN_FUNCS / INDEX_FUNCS / OTHER_FUNCS in this test "
            "so it is covered, and make sure init_schema() calls it."
        )
        monkeypatch.setattr(db, "_engine", new_engine())
        calls = _spy_everything(monkeypatch)
        db.init_schema()
        called = {n for n, _ in calls}
        assert called == set(COLUMN_FUNCS + INDEX_FUNCS + ["_fix_stale_dhan_token_expiry", "_backfill_broker_imported_flag"])
        assert "_ensure_oracle_autoincrement" not in called       # Postgres/SQLite: real serial/identity already there

    def test_each_migration_runs_exactly_once_with_the_dialect(self, monkeypatch):
        monkeypatch.setattr(db, "_engine", new_engine())
        calls = _spy_everything(monkeypatch)
        db.init_schema()
        names = [n for n, _ in calls]
        assert len(names) == len(set(names))
        for n, args in calls:
            if n in COLUMN_FUNCS + INDEX_FUNCS:
                assert args[1] == "postgresql"

    def test_backfill_runs_after_the_column_it_reads_exists(self, monkeypatch):
        monkeypatch.setattr(db, "_engine", new_engine())
        calls = _spy_everything(monkeypatch)
        db.init_schema()
        names = [n for n, _ in calls]
        assert names.index("_ensure_position_columns") < names.index("_backfill_broker_imported_flag")

    def test_oracle_adds_the_autoincrement_step_and_runs_it_first(self, monkeypatch):
        eng = new_engine()
        monkeypatch.setattr(db, "_engine", eng)
        monkeypatch.setattr(db, "dialect", lambda: "oracle")
        calls = _spy_everything(monkeypatch)
        db.init_schema()
        assert calls[0][0] == "_ensure_oracle_autoincrement"
        assert calls[0][1] == (eng, models.Base)
        assert all(a[1] == "oracle" for n, a in calls if n in COLUMN_FUNCS + INDEX_FUNCS)

    def test_creates_every_table_on_a_fresh_database(self, monkeypatch):
        eng = new_engine()
        monkeypatch.setattr(db, "_engine", eng)
        db.init_schema()
        assert set(inspect(eng).get_table_names()) == {t.name for t in models.Base.metadata.sorted_tables}


# ══════════════════════════════════════════════════════════════════════════
# Static invariants over what the migrations actually emit
# ══════════════════════════════════════════════════════════════════════════
def _model_col(table, col):
    return models.Base.metadata.tables[table].c[col]


_PG_TYPES = {"BOOLEAN": Boolean, "INTEGER": Integer, "FLOAT": Float, "DOUBLE": Float, "TIMESTAMP": DateTime, "TEXT": Text}
_ORA_TYPES = {"FLOAT": Float, "BINARY_DOUBLE": Float, "TIMESTAMP": DateTime, "CLOB": Text}


def _check_type(dialect, table, col, tail):
    m = _model_col(table, col)
    base = tail.split()[0]
    length = None
    if dialect == "postgresql":
        v = re.match(r"VARCHAR\((\d+)\)", base)
        expected = String if v else _PG_TYPES.get(base)
    else:
        v = re.match(r"VARCHAR2\((\d+)\)", base)
        n = re.match(r"NUMBER\((\d+)\)", base)
        if v:
            expected = String
        elif n:
            expected = Boolean if n.group(1) == "1" else Integer
        else:
            expected = _ORA_TYPES.get(base)
    assert expected is not None, f"{dialect}: {table}.{col} uses a type this test doesn't know: {base}"
    assert isinstance(m.type, expected), f"{dialect}: {table}.{col} DDL {base} vs model {m.type!r}"
    if v:
        length = int(v.group(1))
        assert m.type.length == length, f"{dialect}: {table}.{col} DDL length {length} vs model String({m.type.length})"


def _default_of(tail):
    m = re.search(r"DEFAULT (\S+)", tail)
    if not m:
        return "NO-DEFAULT"
    lit = m.group(1)
    if lit.startswith("'"):
        return lit.strip("'")
    if lit.upper() in ("TRUE", "FALSE"):
        return lit.upper() == "TRUE"
    return float(lit) if "." in lit else int(lit)


class TestMigrationDdlMatchesModels:
    def test_the_discovery_run_actually_finds_the_known_migrations(self, discovered):
        mig = migrated_set(discovered)
        assert len(mig) >= 59
        for key in [
            ("trade_orders", "execution_source"),
            ("trade_gate_state", "edis_morning_check_enabled"),
            ("trade_positions", "entry_product_type"),      # session38 DATAMATICS fix
            ("trade_positions", "consecutive_exit_failures"),
            ("trade_positions", "broker_imported"),
            ("trade_candidates", "overnight_priority"),
            ("trade_risk_config", "max_trade_value"),
            ("trade_watchlist", "catalyst_price_source"),
            ("trade_accounts", "broker_cash_available"),
        ]:
            assert key in mig

    def test_no_column_is_added_by_two_different_functions(self, discovered):
        seen = {}
        for fn, per in discovered["postgresql"].items():
            for k in per:
                assert k not in seen, f"{k} added by both {seen[k]} and {fn}"
                seen[k] = fn

    def test_each_function_only_adds_columns_the_models_actually_declare(self, discovered):
        for dname in ("postgresql", "oracle"):
            for fn, per in discovered[dname].items():
                assert per, f"{fn} ({dname}) added nothing on a skeleton schema"
                for (t, c) in per:
                    assert t in models.Base.metadata.tables, f"{fn}: no model table {t}"
                    assert c in models.Base.metadata.tables[t].c, f"{fn}: {t}.{c} is not a model column (typo / drift)"

    @pytest.mark.parametrize("fn", COLUMN_FUNCS)
    def test_postgres_and_oracle_branches_add_the_same_columns(self, discovered, fn):
        assert set(discovered["postgresql"][fn]) == set(discovered["oracle"][fn])

    @pytest.mark.parametrize("dialect", ["postgresql", "oracle"])
    def test_types_lengths_nullability_and_defaults_match_the_model(self, discovered, dialect):
        checked = 0
        for fn, per in discovered[dialect].items():
            for (t, c), tail in per.items():
                m = _model_col(t, c)
                _check_type(dialect, t, c, tail)
                not_null = "NOT NULL" in tail.upper()
                assert not_null == (not m.nullable), f"{dialect}: {t}.{c} NOT NULL={not_null} vs model nullable={m.nullable}"
                default = _default_of(tail)
                if not_null:
                    # ADD COLUMN ... NOT NULL with no DEFAULT fails on a non-empty table.
                    assert default != "NO-DEFAULT", f"{dialect}: {t}.{c} is NOT NULL with no DEFAULT — ALTER fails on existing rows"
                    assert m.default is not None, f"{t}.{c}: migration has a DEFAULT but the model has no default"
                    model_default = m.default.arg
                    if isinstance(model_default, bool) or isinstance(default, bool) or isinstance(model_default, (int, float)):
                        assert float(default) == float(model_default), f"{dialect}: {t}.{c} DEFAULT {default!r} vs model {model_default!r}"
                    else:
                        assert default == model_default
                checked += 1
        assert checked >= 59

    def test_oracle_and_postgres_defaults_agree_with_each_other(self, discovered):
        for fn in COLUMN_FUNCS:
            for key, tail in discovered["postgresql"][fn].items():
                a, b = _default_of(tail), _default_of(discovered["oracle"][fn][key])
                assert (a == b) or (float(a) == float(b)), f"{key}: postgres DEFAULT {a!r} vs oracle {b!r}"

    def test_oracle_ddl_uses_the_parenthesised_add_form(self, discovered):
        for fn in COLUMN_FUNCS:
            eng = skeleton_engine()
            getattr(db, fn)(eng, "oracle")
            assert alters(eng) and all(re.match(r"ALTER TABLE \w+ ADD \(\w+ .+\)$", s) for s in alters(eng))


class TestDriftGuard:
    """The session-11 bug class, guarded for the future.

    If you add a column to an EXISTING table in models.py you must also add an
    `_ensure_*` migration in db.py — create_all() will not add it to a database
    that already has the table, and every ORM query on that table then fails.
    BASELINE (top of this file) is the frozen list of columns that came from
    the original create_all(). A model column that is in neither BASELINE nor a
    migration is exactly that mistake."""

    @staticmethod
    def _unmigrated(mig, baseline):
        problems = []
        for t in models.Base.metadata.sorted_tables:
            if t.name not in baseline:
                continue   # brand-new table: create_all() creates it whole, no migration needed
            for c in t.columns:
                if c.name not in baseline[t.name] and (t.name, c.name) not in mig:
                    problems.append(f"{t.name}.{c.name}")
        return problems

    def test_the_guard_has_teeth(self, discovered):
        # Simulate "someone added trade_orders.status / a whole new table to models.py":
        mig = migrated_set(discovered)
        without_status = {k: (v - {"status"} if k == "trade_orders" else v) for k, v in BASELINE.items()}
        assert self._unmigrated(mig, without_status) == ["trade_orders.status"]
        without_table = {k: v for k, v in BASELINE.items() if k != "trade_fills"}
        assert self._unmigrated(mig, without_table) == []          # new tables need no migration

    def test_every_model_column_is_original_or_migrated(self, discovered):
        problems = self._unmigrated(migrated_set(discovered), BASELINE)
        assert not problems, (
            "Model column(s) with no migration in db.py: " + ", ".join(problems) + ". "
            "Add an _ensure_*_column(s) function (and call it from init_schema), "
            "then list it in COLUMN_FUNCS in tests/test_db.py."
        )

    def test_baseline_columns_still_exist_and_are_not_double_migrated(self, discovered):
        mig = migrated_set(discovered)
        for table, cols in BASELINE.items():
            model_cols = {c.name for c in models.Base.metadata.tables[table].columns}
            assert cols <= model_cols, f"{table}: baseline column(s) removed from models: {cols - model_cols}"
            assert not {(table, c) for c in cols} & mig, f"{table}: baseline column also migrated"

    def test_every_model_table_is_known_or_created_by_create_all(self):
        # new tables are fine; this just makes a rename/removal of a baseline table visible
        names = {t.name for t in models.Base.metadata.sorted_tables}
        assert set(BASELINE) <= names


# ══════════════════════════════════════════════════════════════════════════
# The 17 column migrations, executed for real on a legacy schema
# ══════════════════════════════════════════════════════════════════════════
def _cols(eng):
    insp = inspect(eng)
    return {t: {c["name"]: c for c in insp.get_columns(t)} for t in insp.get_table_names()}


class TestColumnMigrationsOnALegacySchema:
    @pytest.mark.parametrize("fn", COLUMN_FUNCS)
    def test_adds_exactly_its_own_columns_and_is_idempotent(self, discovered, fn, caplog):
        eng, _ = legacy_engine(discovered)
        expected = set(discovered["postgresql"][fn])
        before = _cols(eng)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            getattr(db, fn)(eng, "postgresql")
        after = _cols(eng)
        added = {(t, c) for t in after for c in after[t] if c not in before[t]}
        assert added == expected
        assert len(alters(eng)) == len(expected)
        assert _warnings(caplog) == []
        for t, c in expected:
            assert f"added {t}.{c}" in caplog.text
        eng._sql.clear()
        getattr(db, fn)(eng, "postgresql")             # second boot
        assert alters(eng) == []

    def test_full_upgrade_reproduces_the_model_schema_exactly(self, discovered):
        eng, _ = legacy_engine(discovered)
        for fn in COLUMN_FUNCS:
            getattr(db, fn)(eng, "postgresql")
        actual = _cols(eng)
        for t in models.Base.metadata.sorted_tables:
            assert set(actual[t.name]) == {c.name for c in t.columns}, t.name
            for c in t.columns:
                if c.primary_key:
                    continue
                assert actual[t.name][c.name]["nullable"] == c.nullable, f"{t.name}.{c.name} nullability differs after upgrade"

    def test_orm_can_select_every_mapped_table_after_upgrade(self, discovered):
        eng, _ = legacy_engine(discovered)
        for fn in COLUMN_FUNCS:
            getattr(db, fn)(eng, "postgresql")
        s = sessionmaker(bind=eng)()
        try:
            for mapper in models.Base.registry.mappers:
                s.query(mapper.class_).all()   # SELECTs every mapped column — the session-11 failure mode
        finally:
            s.close()

    def test_without_the_migrations_the_orm_really_does_break(self, discovered):
        # Proves the check above has teeth: on the un-upgraded legacy schema the
        # ORM cannot read a table whose model grew columns.
        eng, _ = legacy_engine(discovered)
        s = sessionmaker(bind=eng)()
        try:
            with pytest.raises(OperationalError, match="no such column"):
                s.query(models.TradeGateState).all()
        finally:
            s.close()

    def test_existing_rows_survive_and_read_back_like_freshly_inserted_ones(self, discovered):
        eng, md = legacy_engine(discovered)
        mig = migrated_set(discovered)
        seeded = {}
        for tname in sorted({t for t, _ in mig}):
            seeded[tname] = insert_row(eng, md.tables[tname])
        gate_id = seeded["trade_gate_state"]
        for fn in COLUMN_FUNCS:
            getattr(db, fn)(eng, "postgresql")
        s = sessionmaker(bind=eng)()
        try:
            classes = {m.class_.__tablename__: m.class_ for m in models.Base.registry.mappers}
            checked = 0
            for (tname, cname) in sorted(mig):
                col = _model_col(tname, cname)
                row = s.get(classes[tname], seeded[tname])
                assert row is not None, f"legacy {tname} row lost in the upgrade"
                value = getattr(row, cname)
                if col.nullable:
                    assert value is None, f"{tname}.{cname}: nullable column on a legacy row should read NULL, got {value!r}"
                else:
                    expected = col.default.arg
                    assert value == expected, f"{tname}.{cname}: legacy row reads {value!r}, a new row would get {expected!r}"
                checked += 1
            assert checked == len(mig)
            gate = s.get(models.TradeGateState, gate_id)
            # spot-check the semantics that matter operationally
            assert gate.auto_pilot_enabled is False
            assert gate.overnight_hold_enabled is True       # DEFAULT TRUE — a legacy REAL gate keeps holding overnight
            assert gate.edis_morning_check_enabled is True
            assert gate.eod_signal_scan_enabled is False
        finally:
            s.close()

    # ── oracle branch (SQLite rejects the syntax; the statements are recorded) ──
    @pytest.mark.parametrize("fn", COLUMN_FUNCS)
    def test_oracle_branch_emits_the_same_columns_and_warns_instead_of_raising(self, discovered, fn, caplog):
        eng, _ = legacy_engine(discovered)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            getattr(db, fn)(eng, "oracle")
        assert set(_parse_alters(alters(eng))) == set(discovered["postgresql"][fn])
        # SQLite can't run "ADD (...)": every attempt is reported, none raised
        assert len(_warnings(caplog)) == len(alters(eng))
        assert all("could not add" in w for w in _warnings(caplog))

    # ── failure handling ──
    @pytest.mark.parametrize("fn", COLUMN_FUNCS)
    def test_inspect_failure_warns_and_skips(self, fn, caplog):
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            getattr(db, fn)(object(), "postgresql")     # inspect(object()) raises
        assert _warnings(caplog), f"{fn}: silent on inspect failure"
        assert all("could not inspect" in w for w in _warnings(caplog))

    @pytest.mark.parametrize("message", [
        'column "x" of relation "t" already exists',
        "ORA-01430: column being added already exists in table",
        "ORA-01430: 表中已存在要添加的列",      # non-English NLS: only the ORA code identifies it
    ])
    @pytest.mark.parametrize("fn", COLUMN_FUNCS)
    def test_already_exists_races_are_swallowed_silently(self, discovered, fn, message, caplog):
        eng, _ = legacy_engine(discovered)
        eng._rules.append(("ALTER TABLE", message))
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            getattr(db, fn)(eng, "postgresql")
        assert _warnings(caplog) == []
        assert len(alters(eng)) == len(discovered["postgresql"][fn])     # every column was still attempted

    @pytest.mark.parametrize("fn", COLUMN_FUNCS)
    def test_any_other_alter_error_warns_and_the_remaining_columns_are_still_tried(self, discovered, fn, caplog):
        eng, _ = legacy_engine(discovered)
        eng._rules.append(("ALTER TABLE", "permission denied for table"))
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            getattr(db, fn)(eng, "postgresql")
        expected = discovered["postgresql"][fn]
        assert len(alters(eng)) == len(expected)
        assert len(_warnings(caplog)) == len(expected)
        assert all("could not add" in w and "permission denied" in w for w in _warnings(caplog))

    def test_one_failing_column_does_not_stop_the_next_one(self, discovered):
        eng, _ = legacy_engine(discovered)
        eng._rules.append(("min_trade_value", "boom"))
        db._ensure_cost_model_columns(eng, "postgresql")
        cols = {c["name"] for c in inspect(eng).get_columns("trade_risk_config")}
        assert "min_trade_value" not in cols
        assert {"min_edge_to_cost_ratio", "max_trade_value"} <= cols


# ══════════════════════════════════════════════════════════════════════════
# Index ensurers
# ══════════════════════════════════════════════════════════════════════════
INDEX_CASES = [
    ("_ensure_hot_path_indexes", [
        ("ix_trade_orders_mode_created", "trade_orders", ["mode", "created_at"]),
        ("ix_trade_candidates_mode_consumed_recv", "trade_candidates", ["mode", "consumed", "received_at"]),
    ]),
    ("_ensure_nextday_watchlist_indexes", [
        ("ix_nextday_watchlist_mode_date_consumed", "trade_nextday_watchlist", ["mode", "market_date", "consumed"]),
        ("ix_nextday_watchlist_mode_sym_date", "trade_nextday_watchlist", ["mode", "symbol", "market_date"]),
    ]),
]


def _index_map(eng, table):
    return {i["name"]: i["column_names"] for i in inspect(eng).get_indexes(table)}


class TestIndexEnsurers:
    @pytest.mark.parametrize("fn, indexes", INDEX_CASES)
    def test_creates_the_indexes_for_real_and_is_idempotent(self, discovered, fn, indexes, caplog):
        eng, _ = legacy_engine(discovered)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            getattr(db, fn)(eng, "postgresql")
            getattr(db, fn)(eng, "postgresql")        # second boot: IF NOT EXISTS
        for name, table, cols in indexes:
            assert _index_map(eng, table)[name] == cols
            assert f"ensured index {name} on {table}" in caplog.text
        assert _warnings(caplog) == []

    @pytest.mark.parametrize("fn, indexes", INDEX_CASES)
    def test_index_names_and_column_order_match_the_models_own_indexes(self, fn, indexes):
        for name, table, cols in indexes:
            model_idx = {i.name: [c.name for c in i.columns] for i in models.Base.metadata.tables[table].indexes}
            assert model_idx.get(name) == cols, f"{name}: db.py and models.py disagree"
            assert len(name) <= 128                    # Oracle 12.2+ identifier limit

    @pytest.mark.parametrize("fn, indexes", INDEX_CASES)
    def test_postgres_uses_if_not_exists_oracle_does_not(self, discovered, fn, indexes):
        for dname, expect_if_not_exists in (("postgresql", True), ("oracle", False)):
            eng, _ = legacy_engine(discovered)
            getattr(db, fn)(eng, dname)
            creates = [s for s in eng._sql if s.startswith("CREATE INDEX")]
            assert len(creates) == len(indexes)
            for (name, table, cols), sql in zip(indexes, creates):
                assert sql == (f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({', '.join(cols)})" if expect_if_not_exists
                               else f"CREATE INDEX {name} ON {table} ({', '.join(cols)})")

    @pytest.mark.parametrize("fn, indexes", INDEX_CASES)
    @pytest.mark.parametrize("message", ["ORA-00955: name is already used by an existing object", "ORA-01408: such column list already indexed"])
    def test_oracle_already_indexed_errors_are_swallowed(self, discovered, fn, indexes, message, caplog):
        eng, _ = legacy_engine(discovered)
        eng._rules.append(("CREATE INDEX", message))
        with caplog.at_level(logging.INFO, logger=LOGGER):
            getattr(db, fn)(eng, "oracle")          # must not raise
        assert _warnings(caplog) == []

    def test_a_real_ddl_error_is_swallowed_by_exec_ddl_safe_but_still_reported_as_ensured(self, discovered, caplog):
        # Pinned CURRENT behaviour (see session note observation): exec_ddl_safe
        # logs anything but "already exists" at DEBUG only, and the caller then
        # logs "ensured index" regardless.
        eng, _ = legacy_engine(discovered)
        eng._rules.append(("CREATE INDEX", "syntax error near INDEX"))
        with caplog.at_level(logging.INFO, logger=LOGGER):
            db._ensure_hot_path_indexes(eng, "postgresql")
        assert "ensured index ix_trade_orders_mode_created" in caplog.text
        assert "ix_trade_orders_mode_created" not in _index_map(eng, "trade_orders")


# ══════════════════════════════════════════════════════════════════════════
# _backfill_broker_imported_flag
# ══════════════════════════════════════════════════════════════════════════
def _seed_positions(eng):
    T = models.Base.metadata.tables
    pos = {}
    imported = "Imported from Dhan demat holdings (qty 10)"
    pos["imported"] = insert_row(eng, T["trade_positions"], symbol="AAA")
    pos["plain"] = insert_row(eng, T["trade_positions"], symbol="BBB")
    pos["wrong_type"] = insert_row(eng, T["trade_positions"], symbol="CCC")
    pos["other_detail"] = insert_row(eng, T["trade_positions"], symbol="DDD")
    pos["already_true"] = insert_row(eng, T["trade_positions"], symbol="EEE", broker_imported=True)
    for key, etype, detail in [
        ("imported", "OPENED", imported),
        ("wrong_type", "CLOSED", imported),
        ("other_detail", "OPENED", "Opened by entry engine"),
        ("already_true", "OPENED", imported),
    ]:
        insert_row(eng, T["trade_position_events"], position_id=pos[key], event_type=etype, detail=detail)
    return pos


def _flags(eng):
    with eng.connect() as conn:
        return dict(conn.exec_driver_sql("SELECT symbol, broker_imported FROM trade_positions").all())


class TestBackfillBrokerImported:
    @pytest.mark.parametrize("dialect_name", ["postgresql", "oracle"])
    def test_flags_only_positions_that_import_holdings_created(self, dialect_name, monkeypatch, caplog):
        eng = new_engine()
        models.Base.metadata.create_all(eng)
        pos = _seed_positions(eng)
        monkeypatch.setattr(db, "dialect", lambda: dialect_name)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            db._backfill_broker_imported_flag(eng)
        assert _flags(eng) == {"AAA": 1, "BBB": 0, "CCC": 0, "DDD": 0, "EEE": 1}
        assert "backfilled broker_imported=True on 1 pre-existing" in caplog.text
        sql = [s for s in eng._sql if s.startswith("UPDATE trade_positions")][0]
        assert ("= TRUE" in sql and "= FALSE" in sql) if dialect_name == "postgresql" else ("= 1" in sql and "= 0" in sql)
        assert pos  # seeded

    def test_second_run_touches_nothing_and_logs_nothing(self, monkeypatch, caplog):
        eng = new_engine()
        models.Base.metadata.create_all(eng)
        _seed_positions(eng)
        db._backfill_broker_imported_flag(eng)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            db._backfill_broker_imported_flag(eng)
        assert "backfilled" not in caplog.text
        assert _flags(eng)["AAA"] == 1

    def test_empty_tables_are_fine(self, caplog):
        eng = new_engine()
        models.Base.metadata.create_all(eng)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            db._backfill_broker_imported_flag(eng)
        assert caplog.text == ""

    def test_failure_warns_instead_of_raising(self, caplog):
        eng = new_engine()          # no tables at all
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            db._backfill_broker_imported_flag(eng)
        assert "could not backfill broker_imported flag" in " ".join(_warnings(caplog))


# ══════════════════════════════════════════════════════════════════════════
# _fix_stale_dhan_token_expiry
# ══════════════════════════════════════════════════════════════════════════
class _FakeBeginEngine:
    def __init__(self, rowcount=0, error=None):
        self.rowcount, self.error, self.statements = rowcount, error, []

    def begin(self):
        outer = self

        class _Ctx:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def execute(self_inner, stmt):
                outer.statements.append(stmt.text)
                if outer.error:
                    raise outer.error
                return SimpleNamespace(rowcount=outer.rowcount)

        return _Ctx()


class TestFixStaleDhanTokenExpiry:
    def test_postgres_sql(self, monkeypatch):
        eng = _FakeBeginEngine()
        monkeypatch.setattr(db, "dialect", lambda: "postgresql")
        db._fix_stale_dhan_token_expiry(eng)
        sql = eng.statements[0]
        assert sql.startswith("UPDATE trade_credentials SET token_expires_at = token_issued_at + INTERVAL '24 hours'")
        assert "token_expires_at > token_issued_at + INTERVAL '24 hours'" in sql
        assert "token_issued_at IS NOT NULL" in sql and "token_expires_at IS NOT NULL" in sql

    def test_oracle_sql_uses_oracle_interval_syntax(self, monkeypatch):
        eng = _FakeBeginEngine()
        monkeypatch.setattr(db, "dialect", lambda: "oracle")
        db._fix_stale_dhan_token_expiry(eng)
        sql = eng.statements[0]
        assert "INTERVAL '24' HOUR" in sql and "'24 hours'" not in sql
        assert "token_expires_at > token_issued_at + INTERVAL '24' HOUR" in sql

    def test_only_shortens_never_extends(self, monkeypatch):
        # the WHERE clause is what guarantees this
        eng = _FakeBeginEngine()
        monkeypatch.setattr(db, "dialect", lambda: "postgresql")
        db._fix_stale_dhan_token_expiry(eng)
        assert re.search(r"WHERE .*token_expires_at > token_issued_at", eng.statements[0])

    def test_logs_when_rows_were_capped(self, monkeypatch, caplog):
        monkeypatch.setattr(db, "dialect", lambda: "postgresql")
        with caplog.at_level(logging.INFO, logger=LOGGER):
            db._fix_stale_dhan_token_expiry(_FakeBeginEngine(rowcount=3))
        assert "capped 3 stale trade_credentials.token_expires_at row(s) to 24h" in caplog.text

    def test_silent_when_nothing_to_fix(self, monkeypatch, caplog):
        monkeypatch.setattr(db, "dialect", lambda: "postgresql")
        with caplog.at_level(logging.INFO, logger=LOGGER):
            db._fix_stale_dhan_token_expiry(_FakeBeginEngine(rowcount=0))
        assert caplog.text == ""

    def test_failure_warns_instead_of_raising(self, monkeypatch, caplog):
        monkeypatch.setattr(db, "dialect", lambda: "postgresql")
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            db._fix_stale_dhan_token_expiry(_FakeBeginEngine(error=RuntimeError("relation does not exist")))
        assert "could not check/fix stale token_expires_at: relation does not exist" in " ".join(_warnings(caplog))


# ══════════════════════════════════════════════════════════════════════════
# _ensure_oracle_autoincrement (fake Oracle engine — the SQL is Oracle-only)
# ══════════════════════════════════════════════════════════════════════════
class _Scalar:
    def __init__(self, v):
        self._v = v

    def scalar(self):
        return self._v


class _FakeOracleEngine:
    def __init__(self, *, has_identity=(), next_id=None, identity_check_fails=(), max_id_fails=(), seq_error=None, trigger_error=None):
        self.has_identity = set(has_identity)
        self.next_id = next_id or {}
        self.identity_check_fails = set(identity_check_fails)
        self.max_id_fails = set(max_id_fails)
        self.seq_error, self.trigger_error = seq_error, trigger_error
        self.log = []          # (kind, sql)

    def connect(self):
        return _Conn(self)

    def begin(self):
        return _Conn(self)


class _Conn:
    def __init__(self, eng):
        self.e = eng

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, stmt, params=None):
        sql = stmt.text
        self.e.log.append(("execute", sql))
        if "user_tab_identity_cols" in sql:
            t = params["t"]
            if t in self.e.identity_check_fails:
                raise RuntimeError("ORA-00942: table or view does not exist")
            return _Scalar(1 if t in self.e.has_identity else 0)
        if sql.startswith("SELECT NVL(MAX(id)"):
            table = re.search(r"FROM (\w+)", sql).group(1)
            if table in self.e.max_id_fails:
                raise RuntimeError("ORA-00942")
            return _Scalar(self.e.next_id.get(table, 1))
        if sql.startswith("CREATE SEQUENCE") and self.e.seq_error:
            raise RuntimeError(self.e.seq_error)
        return _Scalar(None)

    def exec_driver_sql(self, sql):
        self.e.log.append(("driver", sql))
        if self.e.trigger_error:
            raise RuntimeError(self.e.trigger_error)


ID_TABLES = sorted(t.name for t in models.Base.metadata.sorted_tables if "id" in {c.name for c in t.primary_key.columns})
NON_ID_TABLES = sorted(t.name for t in models.Base.metadata.sorted_tables if "id" not in {c.name for c in t.primary_key.columns})


def _ops(eng, kind, prefix):
    return [s for k, s in eng.log if k == kind and s.startswith(prefix)]


class TestOracleAutoincrement:
    def test_tables_without_an_id_primary_key_are_skipped(self, caplog):
        assert {"trade_resilience_cache", "trade_intraday_restricted", "stockky_shared_service_exposure"} <= set(NON_ID_TABLES)
        eng = _FakeOracleEngine()
        with caplog.at_level(logging.INFO, logger=LOGGER):
            db._ensure_oracle_autoincrement(eng, models.Base)
        for t in NON_ID_TABLES:
            assert f"oracle autoincrement: skipping {t} — primary key is not `id`" in caplog.text
            assert not any(t in s for _, s in eng.log if s.startswith(("CREATE SEQUENCE", "CREATE OR REPLACE TRIGGER")))

    def test_tables_that_already_have_identity_are_left_alone(self):
        eng = _FakeOracleEngine(has_identity=ID_TABLES)
        db._ensure_oracle_autoincrement(eng, models.Base)
        assert _ops(eng, "execute", "CREATE SEQUENCE") == []
        assert _ops(eng, "driver", "CREATE OR REPLACE TRIGGER") == []
        assert len(_ops(eng, "execute", "SELECT COUNT(*) FROM user_tab_identity_cols")) == len(ID_TABLES)

    def test_a_table_without_identity_gets_sequence_and_trigger(self, caplog):
        eng = _FakeOracleEngine(has_identity=[t for t in ID_TABLES if t != "trade_accounts"], next_id={"trade_accounts": 42})
        with caplog.at_level(logging.INFO, logger=LOGGER):
            db._ensure_oracle_autoincrement(eng, models.Base)
        assert _ops(eng, "execute", "CREATE SEQUENCE") == ["CREATE SEQUENCE trade_accounts_id_seq START WITH 42 INCREMENT BY 1 NOCACHE NOCYCLE"]
        trg = _ops(eng, "driver", "CREATE OR REPLACE TRIGGER")
        assert trg == [
            "CREATE OR REPLACE TRIGGER trg_trade_accounts_bi BEFORE INSERT ON trade_accounts FOR EACH ROW "
            "WHEN (NEW.id IS NULL) BEGIN SELECT trade_accounts_id_seq.NEXTVAL INTO :NEW.id FROM dual; END;"
        ]
        assert "attached trade_accounts_id_seq / trg_trade_accounts_bi to trade_accounts" in caplog.text
        # the sequence must start after existing rows (else the first insert collides with id 1)
        assert any(s.startswith("SELECT NVL(MAX(id), 0) + 1 FROM trade_accounts") for _, s in eng.log)

    @pytest.mark.parametrize("value", [0, None])
    def test_empty_table_starts_the_sequence_at_1(self, value):
        eng = _FakeOracleEngine(has_identity=[t for t in ID_TABLES if t != "trade_fills"], next_id={"trade_fills": value})
        db._ensure_oracle_autoincrement(eng, models.Base)
        assert _ops(eng, "execute", "CREATE SEQUENCE") == ["CREATE SEQUENCE trade_fills_id_seq START WITH 1 INCREMENT BY 1 NOCACHE NOCYCLE"]

    def test_max_id_query_failure_falls_back_to_1(self):
        eng = _FakeOracleEngine(has_identity=[t for t in ID_TABLES if t != "trade_fills"], max_id_fails=["trade_fills"])
        db._ensure_oracle_autoincrement(eng, models.Base)
        assert _ops(eng, "execute", "CREATE SEQUENCE") == ["CREATE SEQUENCE trade_fills_id_seq START WITH 1 INCREMENT BY 1 NOCACHE NOCYCLE"]

    def test_identity_check_failure_warns_and_moves_on_to_the_next_table(self, caplog):
        eng = _FakeOracleEngine(has_identity=[t for t in ID_TABLES if t != "trade_fills"], identity_check_fails=["trade_accounts"])
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            db._ensure_oracle_autoincrement(eng, models.Base)
        assert "oracle identity check failed for trade_accounts" in " ".join(_warnings(caplog))
        assert not any("trade_accounts_id_seq" in s for _, s in eng.log)            # skipped, not half-done
        assert _ops(eng, "execute", "CREATE SEQUENCE") == ["CREATE SEQUENCE trade_fills_id_seq START WITH 1 INCREMENT BY 1 NOCACHE NOCYCLE"]

    @pytest.mark.parametrize("err", ["ORA-00955: name is already used by an existing object", "sequence already exists"])
    def test_sequence_that_already_exists_is_swallowed_and_the_trigger_still_attached(self, err, caplog):
        eng = _FakeOracleEngine(has_identity=[t for t in ID_TABLES if t != "trade_fills"], seq_error=err)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            db._ensure_oracle_autoincrement(eng, models.Base)
        assert _warnings(caplog) == []
        assert len(_ops(eng, "driver", "CREATE OR REPLACE TRIGGER")) == 1

    def test_trigger_failure_warns_and_does_not_stop_other_tables(self, caplog):
        eng = _FakeOracleEngine(has_identity=[t for t in ID_TABLES if t not in ("trade_fills", "trade_pnl")], trigger_error="ORA-04098: trigger is invalid")
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            db._ensure_oracle_autoincrement(eng, models.Base)
        w = " ".join(_warnings(caplog))
        assert "could not attach autoincrement trigger to trade_fills" in w
        assert "could not attach autoincrement trigger to trade_pnl" in w     # kept going

    def test_generated_object_names_fit_oracle_identifier_limits(self):
        # Oracle 12.2+ (Autonomous) allows 128; older releases only 30.
        longest = max(len(f"{t}_id_seq") for t in ID_TABLES)
        assert longest <= 128


# ══════════════════════════════════════════════════════════════════════════
# End to end: a legacy database upgraded through init_schema itself
# ══════════════════════════════════════════════════════════════════════════
class TestInitSchemaUpgradesALegacyDatabase:
    def test_legacy_to_current(self, discovered, monkeypatch, caplog):
        eng, md = legacy_engine(discovered)
        T = md.tables
        insert_row(eng, T["trade_gate_state"], mode="REAL")
        acct = insert_row(eng, T["trade_accounts"], mode="REAL")
        pos = insert_row(eng, T["trade_positions"], symbol="AAA")
        insert_row(eng, T["trade_position_events"], position_id=pos, event_type="OPENED", detail="Imported from Dhan demat holdings (qty 5)")
        monkeypatch.setattr(db, "_engine", eng)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            db.init_schema()
        actual = _cols(eng)
        for t in models.Base.metadata.sorted_tables:
            assert set(actual[t.name]) == {c.name for c in t.columns}, t.name
        # the only warning SQLite can produce: Postgres-only INTERVAL syntax in the token fixup
        assert [w for w in _warnings(caplog) if "stale token_expires_at" not in w] == []
        assert "schema ready (dialect=postgresql)" in caplog.text
        # indexes added on the pre-existing tables
        assert "ix_trade_orders_mode_created" in _index_map(eng, "trade_orders")
        assert "ix_nextday_watchlist_mode_sym_date" in _index_map(eng, "trade_nextday_watchlist")
        # backfill saw the freshly-added broker_imported column
        s = sessionmaker(bind=eng)()
        try:
            assert s.get(models.TradePosition, pos).broker_imported is True
            assert s.get(models.TradeAccount, acct) is not None
        finally:
            s.close()

    def test_running_init_schema_twice_changes_nothing(self, discovered, monkeypatch):
        eng, _ = legacy_engine(discovered)
        monkeypatch.setattr(db, "_engine", eng)
        db.init_schema()
        eng._sql.clear()
        db.init_schema()
        assert alters(eng) == []
