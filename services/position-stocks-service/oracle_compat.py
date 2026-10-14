"""
oracle_compat.py — Neon/Postgres ↔ Oracle Autonomous DB portability shim.

This is the SINGLE place that decides which backend a given DATABASE_URL points
at and builds a python-oracledb (thin mode) SQLAlchemy engine against an Oracle
Autonomous Database using an mTLS Instance Wallet. It also emits the correct
dialect flavour of the small amount of hand-written SQL the KV cache uses
(CREATE TABLE / upsert), so the *same* application code runs unchanged on:

    * Render          -> Neon / Postgres   (DATABASE_URL = postgresql://...)
    * Oracle Cloud VM -> Oracle ADB         (ORACLE_DSN set, or
                                             DATABASE_URL = oracle+oracledb://...)

Env contract — MUST stay in sync with
services/decision-prediction-service/training/models.py:

    ORACLE_DSN              TNS alias, e.g. stockkydb_high  (presence => Oracle mode)
    ORACLE_USER             default ADMIN
    ORACLE_PASSWORD         (or ORACLE_ADMIN_PASSWORD)
    ORACLE_WALLET_DIR       wallet dir (or TNS_ADMIN) — tnsnames.ora/sqlnet.ora/ewallet
    ORACLE_WALLET_PASSWORD  wallet password (mTLS Instance Wallet)
    DB_POOL_SIZE / DB_MAX_OVERFLOW / DB_POOL_RECYCLE / DB_POOL_TIMEOUT

Blank handling (group 71): ORACLE_DSN / ORACLE_USER / ORACLE_WALLET_DIR / TNS_ADMIN are trimmed and a
whitespace-only value counts as unset (a padded ORACLE_DSN used to flip the process into Oracle mode with an
empty DSN). The two passwords are kept verbatim -- never stripped -- but a whitespace-only value counts as unset.

Nothing here imports oracledb unless an Oracle engine is actually built, so
services that only ever talk to Neon pay no import cost.
"""
from __future__ import annotations

import collections
import logging
import os
import sys
import threading
import time
import weakref

_log = logging.getLogger("oracle-compat")

_ORACLE_LOB_CONFIGURED = False


def _secret_env(*names: str) -> str:
    """First non-blank value among the named env vars, returned VERBATIM.

    Passwords are deliberately not .strip()'d (a password may legitimately start
    or end with whitespace), but a whitespace-only value is a paste/typo, not a
    password: it counts as unset, so an `ORACLE_PASSWORD="  "` no longer shadows
    the `ORACLE_ADMIN_PASSWORD` fallback or gets sent to Oracle as the password."""
    for name in names:
        value = os.environ.get(name) or ""
        if value.strip():
            return value
    return ""


def _wallet_dir() -> str:
    """Wallet directory: ORACLE_WALLET_DIR, else TNS_ADMIN (both trimmed; a
    whitespace-only value is unset, not a directory called ' ')."""
    return (os.environ.get("ORACLE_WALLET_DIR") or "").strip() or (os.environ.get("TNS_ADMIN") or "").strip()


def oracle_is_configured(url: str = "") -> bool:
    """True when we should talk to Oracle Autonomous DB instead of Postgres.

    Enabled if the URL scheme is oracle, or ORACLE_DSN is set in the env. This is
    exactly what lets the SAME code run on Render (Neon/Postgres) and on the
    Oracle VM (Oracle ADB) with only environment differences."""
    try:
        return (url or "").lower().startswith("oracle") or bool((os.environ.get("ORACLE_DSN") or "").strip())
    except Exception:
        return False


def dialect_name(engine) -> str:
    """Lower-case SQLAlchemy dialect name ('oracle' | 'postgresql' | 'sqlite')."""
    try:
        return (engine.dialect.name or "").lower()
    except Exception:
        return ""


def is_oracle_engine(engine) -> bool:
    return dialect_name(engine) == "oracle"


def _configure_oracle_lobs() -> None:
    """Return CLOB columns as plain str (not LOB handles) so json.loads() works.

    Safe/no-op on older oracledb builds that lack .defaults.fetch_lobs."""
    global _ORACLE_LOB_CONFIGURED
    if _ORACLE_LOB_CONFIGURED:
        return
    try:
        import oracledb  # noqa: WPS433 (imported lazily, Oracle path only)

        try:
            oracledb.defaults.fetch_lobs = False
        except Exception:
            pass
        _ORACLE_LOB_CONFIGURED = True
    except Exception:
        # oracledb not installed — caller will get a clear error when it builds
        # the engine; nothing to configure here.
        pass


def oracle_engine_kwargs(full_url_provided: bool, **pool_overrides) -> dict:
    """create_engine kwargs for Oracle Autonomous DB via python-oracledb thin mode
    + an mTLS Instance Wallet. Credentials/DSN come from discrete ORACLE_* env
    vars unless a full oracle+oracledb:// URL was supplied.

    pool_overrides lets a caller (e.g. the KV cache on a tiny box) request a
    smaller pool than the training service default."""
    ca: dict = {}
    if not full_url_provided:
        # Empty URL ("oracle+oracledb://") + connect_args — cleanest for wallet
        # auth and avoids URL-encoding the ADMIN password.
        ca["user"] = (os.environ.get("ORACLE_USER") or "").strip() or "ADMIN"
        pw = _secret_env("ORACLE_PASSWORD", "ORACLE_ADMIN_PASSWORD")
        if pw:
            ca["password"] = pw
        ca["dsn"] = (os.environ.get("ORACLE_DSN") or "").strip()  # TNS alias e.g. stockkydb_high
    # Wallet location applies to both URL and discrete-var forms.
    wallet_dir = _wallet_dir()
    wallet_pw = _secret_env("ORACLE_WALLET_PASSWORD")
    if wallet_dir:
        ca["config_dir"] = wallet_dir
        ca["wallet_location"] = wallet_dir
    if wallet_pw:
        ca["wallet_password"] = wallet_pw

    def _int(name, default):
        # group103: a BLANK value (e.g. `CACHE_DB_POOL_SIZE_ORACLE=` left empty in a compose/.env file, which
        # os.getenv returns as "" rather than the default) used to raise ValueError here and abort engine
        # creation. Blank now means "unset" and falls through: override -> env var -> default. A non-blank
        # value that is not a number (a real typo) still raises, so a misconfiguration is not hidden.
        for raw in (pool_overrides.get(name), os.environ.get(name.upper()), default):
            s = str(raw).strip() if raw is not None else ""
            if s:
                return int(s)
        return int(default)

    return {
        "echo": False,
        "pool_pre_ping": True,
        "pool_recycle": _int("db_pool_recycle", "300"),
        "pool_size": _int("db_pool_size", "3"),
        "max_overflow": _int("db_max_overflow", "2"),
        "pool_timeout": _int("db_pool_timeout", "30"),
        "connect_args": ca,
    }


def build_oracle_engine(url: str = "", **pool_overrides):
    """Return (engine, normalized_url) for Oracle Autonomous DB.

    A full 'oracle+oracledb://...' URL is used as-is; otherwise an empty
    'oracle+oracledb://' URL is used and the wallet/DSN/creds ride in
    connect_args (discrete ORACLE_* vars)."""
    from sqlalchemy import create_engine

    _configure_oracle_lobs()
    # "full" = a real oracle URL with an authority/DSN after the scheme. The
    # scheme-only sentinel "oracle+oracledb://" (discrete ORACLE_* vars, no URL)
    # is NOT full, so credentials/DSN ride in connect_args instead.
    body = url.split("://", 1)[1] if "://" in (url or "") else ""
    full = (url or "").lower().startswith("oracle") and bool(body.strip())
    kwargs = oracle_engine_kwargs(full, **pool_overrides)
    if not full:
        url = "oracle+oracledb://"
    _log.info(
        "Oracle Autonomous DB engine (dsn=%s, wallet=%s)",
        (os.environ.get("ORACLE_DSN") or "").strip() or "from-url",
        _wallet_dir() or "none",
    )
    eng = create_engine(url, **kwargs)
    _attach_call_timeout(eng)
    _attach_latency_probe(eng)   # group361: connect/query timing + keep-warm (never fatal)
    return eng, url


# ─────────────────────────────────────────────────────────────────────────────
# BUG FIX (2026-09-12): every Oracle round trip — including pool_pre_ping's own
# health-check SELECT — was unbounded. If the network path to Autonomous DB
# goes silently dead (no RST, just a black hole), a blocking call made
# synchronously inside an `async def` FastAPI route (e.g. api-gateway's
# /surprise/ipo/list -> kv_get -> _neon_get) freezes the ENTIRE single-worker
# event loop for as long as the hang lasts — every other route on that
# process queues up and only drains once the hang clears, which looks exactly
# like a flapping circuit breaker on the calling service ("call failed ()",
# an empty message because it's an httpx ReadTimeout on the client side,
# repeating every ~cooldown_s while the underlying cause is never a fast
# failure). python-oracledb's Connection.call_timeout bounds every single
# round trip (network + DB) in milliseconds; once it fires, oracledb raises
# promptly instead of hanging, which the existing broad except/return-None
# fallbacks in kv_cache.py already handle gracefully.
# ORACLE_CALL_TIMEOUT_MS default 8000 (8s) is intentionally well under the
# 25s Tier-1 HTTP timeout real-trade-service uses, so a stalled DB round trip
# fails fast enough that the caller gets a clean fallback response instead of
# tripping its own client-side timeout.
_DEFAULT_CALL_TIMEOUT_MS = 8000


def _call_timeout_ms() -> int:
    """Parse ORACLE_CALL_TIMEOUT_MS. A mistyped value (e.g. "8s") or a negative
    one used to abort the whole setup and silently leave every Oracle round trip
    UNBOUNDED -- the very hang this setting exists to prevent. It now logs a
    warning and keeps the protective default instead. An explicit 0 is still
    honoured (oracledb treats 0 as "no timeout")."""
    raw = os.environ.get("ORACLE_CALL_TIMEOUT_MS")
    if raw is None or not str(raw).strip():
        return _DEFAULT_CALL_TIMEOUT_MS
    try:
        value = int(str(raw).strip())
        if value < 0:
            raise ValueError("must not be negative")
    except ValueError as e:
        _log.warning(
            "invalid ORACLE_CALL_TIMEOUT_MS=%r (%s); using default %d ms",
            raw, e, _DEFAULT_CALL_TIMEOUT_MS,
        )
        return _DEFAULT_CALL_TIMEOUT_MS
    return value


def _attach_call_timeout(eng) -> None:
    try:
        from sqlalchemy import event
        timeout_ms = _call_timeout_ms()

        @event.listens_for(eng, "connect")
        def _set_call_timeout(dbapi_connection, connection_record):  # noqa: ANN001
            try:
                dbapi_connection.call_timeout = timeout_ms
            except Exception as e:  # pragma: no cover - defensive, never fatal
                _log.debug("could not set oracledb call_timeout: %s", e)
    except Exception as e:  # pragma: no cover - defensive, never fatal
        _log.warning("_attach_call_timeout setup failed (non-fatal): %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# group361: Oracle latency probe + keep-warm.
#
# Why: slow Oracle round trips (a 3.2 s rollback over SSL was logged on 2026-10-14) were
# visible only as event-loop stalls; nothing said WHICH statement, how often, or whether the
# time went into connecting (TLS handshake + auth, paid again whenever pool_recycle replaces a
# connection or the network path dropped an idle one) or into the query itself.
#
#  * Probe: times every cursor execute and every NEW physical connection per engine, keeps
#    count / slow count / max / p50 / p95, and logs a throttled WARNING for a slow one
#    (ORACLE_SLOW_QUERY_MS, default 1500; 0 = no slow log). latency_snapshot() returns it.
#  * Keep-warm: one daemon thread touches idle pooled connections every ORACLE_KEEPWARM_S
#    seconds (default 60; 0 = off). A touch is a normal checkout, so pool_pre_ping and
#    pool_recycle happen HERE (a reconnect or a dead-connection replace) instead of inside the
#    next request. It only uses a connection that is already idle in the pool: it never opens a
#    new session, so the Autonomous DB session count does not grow.
# Both are best effort: any failure is logged at debug and ignored. They use event.listen
# (not event.listens_for), and weakrefs, so a stub sqlalchemy or an un-weakref-able engine
# simply skips them.
# ─────────────────────────────────────────────────────────────────────────────
_LAT_LOCK = threading.Lock()
_LAT: dict = {}
_LAT_SEQ = [0]
_WARM = weakref.WeakSet()
_WARM_STATE = {"thread": None, "ticks": 0, "pings": 0, "failures": 0, "last_failure": None}
_WARM_STOP = threading.Event()
_LOCAL = threading.local()
_SLOW_LOG_GAP_S = 5.0


def _env_num(name: str, default: float, lo: float = 0.0) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return float(default)
    try:
        v = float(str(raw).strip())
    except ValueError:
        _log.warning("invalid %s=%r; using default %s", name, raw, default)
        return float(default)
    return max(lo, v)


def _slow_ms() -> float:
    return _env_num("ORACLE_SLOW_QUERY_MS", 1500.0)


def _keepwarm_s() -> float:
    return _env_num("ORACLE_KEEPWARM_S", 60.0)


def _probe_enabled() -> bool:
    return (os.environ.get("ORACLE_LATENCY_PROBE") or "1").strip() != "0"


def _caller_label() -> str:
    """Module that built the engine (first frame outside oracle_compat), e.g. 'kv_cache.py'."""
    try:
        f = sys._getframe(2)
        here = os.path.basename(__file__)
        while f is not None and os.path.basename(f.f_code.co_filename) == here:
            f = f.f_back
        return os.path.basename(f.f_code.co_filename) if f is not None else "unknown"
    except Exception:  # pragma: no cover - defensive
        return "unknown"


def _pct(sorted_vals, q: float) -> float:
    if not sorted_vals:
        return 0.0
    i = min(len(sorted_vals) - 1, int(round(q * (len(sorted_vals) - 1))))
    return round(sorted_vals[i], 1)


def _record(stat: dict, kind: str, ms: float, what: str) -> None:
    slow = _slow_ms()
    with _LAT_LOCK:
        stat[kind + "_n"] += 1
        stat[kind + "_total_ms"] += ms
        if ms > stat[kind + "_max_ms"]:
            stat[kind + "_max_ms"] = ms
        stat[kind + "_ring"].append(ms)
        is_slow = slow > 0 and ms >= slow
        log_it = False
        if is_slow:
            stat[kind + "_slow_n"] += 1
            stat["last_slow"] = {"kind": kind, "ms": round(ms, 1), "what": what, "at": time.time()}
            now = time.monotonic()
            if now - stat["_last_log"][kind] >= _SLOW_LOG_GAP_S:   # throttled per kind
                stat["_last_log"][kind] = now
                log_it = True
    if log_it:
        _log.warning("oracle slow %s %.0f ms (engine=%s): %s", kind, ms, stat["label"], what)


def _attach_latency_probe(eng) -> None:
    try:
        if not _probe_enabled():
            return
        from sqlalchemy import event
        with _LAT_LOCK:
            _LAT_SEQ[0] += 1
            label = "%s#%d" % (_caller_label(), _LAT_SEQ[0])
            stat = {"label": label, "_last_log": {"exec": 0.0, "connect": 0.0}, "last_slow": None}
            for k in ("exec", "connect"):
                stat[k + "_n"] = 0
                stat[k + "_slow_n"] = 0
                stat[k + "_total_ms"] = 0.0
                stat[k + "_max_ms"] = 0.0
                stat[k + "_ring"] = collections.deque(maxlen=256)
            stat["engine_ref"] = weakref.ref(eng)
            _LAT[label] = stat

        def _before(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
            try:
                context._stockky_t0 = time.perf_counter()  # per-execution, so a raising query cannot skew the next
            except Exception:  # pragma: no cover - defensive
                pass

        def _after(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
            t0 = getattr(context, "_stockky_t0", None)
            if t0 is None:  # pragma: no cover - defensive (probe attached mid-statement)
                return
            _record(stat, "exec", (time.perf_counter() - t0) * 1000.0, " ".join(str(statement).split())[:100])

        def _do_connect(dialect, conn_rec, cargs, cparams):  # noqa: ANN001
            _LOCAL.connect_t0 = time.perf_counter()
            return None  # let SQLAlchemy open the connection as usual

        def _connected(dbapi_connection, connection_record):  # noqa: ANN001
            t0 = _LOCAL.__dict__.pop("connect_t0", None)
            if t0 is not None:
                _record(stat, "connect", (time.perf_counter() - t0) * 1000.0, "new physical connection")

        event.listen(eng, "before_cursor_execute", _before)
        event.listen(eng, "after_cursor_execute", _after)
        event.listen(eng, "do_connect", _do_connect)
        event.listen(eng, "connect", _connected)
        _register_keepwarm(eng)
    except Exception as e:
        _log.debug("_attach_latency_probe skipped: %s", e)


def latency_snapshot() -> dict:
    """Per-engine connect/execute latency + pool state + keep-warm counters (safe to call anywhere)."""
    out = {"probe_enabled": _probe_enabled(), "slow_ms": _slow_ms(), "keepwarm_s": _keepwarm_s(), "engines": {}}
    with _LAT_LOCK:
        items = list(_LAT.items())
    for label, st in items:
        with _LAT_LOCK:
            row = {}
            for k in ("exec", "connect"):
                vals = sorted(st[k + "_ring"])
                n = st[k + "_n"]
                row[k] = {
                    "n": n, "slow_n": st[k + "_slow_n"],
                    "avg_ms": round(st[k + "_total_ms"] / n, 1) if n else 0.0,
                    "p50_ms": _pct(vals, 0.5), "p95_ms": _pct(vals, 0.95),
                    "max_ms": round(st[k + "_max_ms"], 1),
                }
            row["last_slow"] = dict(st["last_slow"]) if st["last_slow"] else None
            ref = st["engine_ref"]
        eng = ref()
        pool = getattr(eng, "pool", None) if eng is not None else None
        row["pool"] = None
        if pool is not None:
            try:
                row["pool"] = {"size": pool.size(), "checked_in": pool.checkedin(),
                               "checked_out": pool.checkedout(), "overflow": pool.overflow()}
            except Exception:  # pragma: no cover - defensive
                row["pool"] = None
        out["engines"][label] = row
    out["keepwarm"] = {k: _WARM_STATE[k] for k in ("ticks", "pings", "failures", "last_failure")}
    out["keepwarm"]["running"] = bool(_WARM_STATE["thread"] and _WARM_STATE["thread"].is_alive())
    out["keepwarm"]["engines"] = len(_WARM)
    return out


def _ping(eng) -> bool:
    """One normal checkout (pool_pre_ping / pool_recycle fire here) + a trivial statement."""
    from sqlalchemy import text
    sql = "SELECT 1 FROM DUAL" if getattr(getattr(eng, "dialect", None), "name", "") == "oracle" else "SELECT 1"
    with eng.connect() as conn:
        conn.execute(text(sql))
    return True


def _keepwarm_tick() -> int:
    """Touch up to 5 idle pooled connections per engine, one at a time. Returns pings made."""
    made = 0
    _WARM_STATE["ticks"] += 1
    for eng in list(_WARM):
        try:
            pool = getattr(eng, "pool", None)
            idle = pool.checkedin() if pool is not None and hasattr(pool, "checkedin") else 0
        except Exception:  # pragma: no cover - defensive
            idle = 0
        for _ in range(min(int(idle), 5)):
            if _WARM_STOP.is_set():
                return made
            try:
                _ping(eng)
                made += 1
                _WARM_STATE["pings"] += 1
            except Exception as e:
                _WARM_STATE["failures"] += 1
                _WARM_STATE["last_failure"] = str(e)[:160]
                _log.debug("oracle keep-warm ping failed: %s", e)
                break
    return made


def _keepwarm_loop() -> None:
    while not _WARM_STOP.wait(max(5.0, _keepwarm_s() or 60.0)):
        if _keepwarm_s() <= 0:
            continue
        try:
            _keepwarm_tick()
        except Exception as e:  # pragma: no cover - defensive
            _log.debug("oracle keep-warm tick failed: %s", e)


def _register_keepwarm(eng) -> None:
    if _keepwarm_s() <= 0:
        return
    _WARM.add(eng)  # TypeError for a non-weakref-able stub engine -> caller swallows it
    th = _WARM_STATE["thread"]
    if th is None or not th.is_alive():
        _WARM_STOP.clear()
        th = threading.Thread(target=_keepwarm_loop, name="oracle-keepwarm", daemon=True)
        _WARM_STATE["thread"] = th
        th.start()


# ─────────────────────────────────────────────────────────────────────────────
# Portable SQL for the hand-written KV / settings tables.
# Postgres keeps its original syntax (TEXT / TIMESTAMPTZ / NOW() / ON CONFLICT).
# Oracle gets VARCHAR2/CLOB/TIMESTAMP/SYSTIMESTAMP/MERGE. sqlite is never used by
# the durable layer (no DB URL => memory-only), so only these two branches exist.
# ─────────────────────────────────────────────────────────────────────────────

def now_func(dialect: str) -> str:
    return "SYSTIMESTAMP" if dialect == "oracle" else "NOW()"


def create_table_sql(dialect: str, table: str, with_expires: bool) -> str:
    """DDL for a KV-shaped table. Oracle omits IF NOT EXISTS (caller swallows
    ORA-00955); Postgres keeps it. Oracle CLOB is left NULLable on purpose —
    Oracle coerces '' to NULL, and a NOT NULL CLOB would then reject empty
    values; our JSON payloads are never empty so this is invisible in practice."""
    if dialect == "oracle":
        cols = "k VARCHAR2(1000) PRIMARY KEY, v CLOB"
        if with_expires:
            cols += ", expires_at TIMESTAMP"
        cols += ", updated_at TIMESTAMP DEFAULT SYSTIMESTAMP"
        return f"CREATE TABLE {table} ({cols})"
    cols = "k TEXT PRIMARY KEY, v TEXT NOT NULL"
    if with_expires:
        cols += ", expires_at TIMESTAMPTZ NULL"
    cols += ", updated_at TIMESTAMPTZ DEFAULT NOW()"
    return f"CREATE TABLE IF NOT EXISTS {table} ({cols})"


# 2026-10-04 (ORA-00936 on every boot): SQLAlchemy quotes reserved-word columns when it creates the
# table (a column named `mode` is stored as lowercase "mode"), but this raw DDL passed `mode` bare, and
# MODE is an Oracle reserved word -> "missing expression" and the index was never built. Quote exactly
# those tokens, the same way SQLAlchemy does.
_ORACLE_QUOTE_COLS = frozenset({"mode", "level", "comment", "user", "size", "start", "uid"})


def _oracle_quote_cols(col: str) -> str:
    parts = []
    for tok in str(col).split(","):
        t = tok.strip()
        bare = t.split()[0] if t else ""
        if bare.lower() in _ORACLE_QUOTE_COLS and not bare.startswith('"'):
            t = '"' + bare.lower() + '"' + t[len(bare):]
        parts.append(t)
    return ", ".join(parts)


def create_index_sql(dialect: str, index: str, table: str, col: str) -> str:
    if dialect == "oracle":
        # No IF NOT EXISTS before 23c; caller swallows ORA-00955/ORA-01408.
        return f"CREATE INDEX {index} ON {table} ({_oracle_quote_cols(col)})"
    return f"CREATE INDEX IF NOT EXISTS {index} ON {table} ({col})"


def upsert_sql(dialect: str, table: str, with_expires: bool) -> str:
    """Single-row upsert keyed on k. Binds: :k, :v, (:e when with_expires).

    Postgres -> INSERT ... ON CONFLICT (k) DO UPDATE.
    Oracle   -> MERGE ... USING (SELECT ... FROM dual) ... WHEN MATCHED/NOT."""
    if dialect == "oracle":
        if with_expires:
            using = "SELECT :k AS k, :v AS v, :e AS e FROM dual"
            matched = "UPDATE SET d.v = s.v, d.expires_at = s.e, d.updated_at = SYSTIMESTAMP"
            insert = ("INSERT (k, v, expires_at, updated_at) "
                      "VALUES (s.k, s.v, s.e, SYSTIMESTAMP)")
        else:
            using = "SELECT :k AS k, :v AS v FROM dual"
            matched = "UPDATE SET d.v = s.v, d.updated_at = SYSTIMESTAMP"
            insert = "INSERT (k, v, updated_at) VALUES (s.k, s.v, SYSTIMESTAMP)"
        return (
            f"MERGE INTO {table} d USING ({using}) s ON (d.k = s.k) "
            f"WHEN MATCHED THEN {matched} "
            f"WHEN NOT MATCHED THEN {insert}"
        )
    if with_expires:
        return (
            f"INSERT INTO {table} (k, v, expires_at, updated_at) "
            f"VALUES (:k, :v, :e, NOW()) "
            f"ON CONFLICT (k) DO UPDATE "
            f"SET v = EXCLUDED.v, expires_at = EXCLUDED.expires_at, updated_at = NOW()"
        )
    return (
        f"INSERT INTO {table} (k, v, updated_at) "
        f"VALUES (:k, :v, NOW()) "
        f"ON CONFLICT (k) DO UPDATE "
        f"SET v = EXCLUDED.v, updated_at = NOW()"
    )


def exec_ddl_safe(engine, sql: str, dialect: str) -> bool:
    """Run one DDL statement in its own transaction, swallowing 'already exists'.

    Returns True when the statement ran or the object was already there (benign),
    False when it genuinely FAILED. A real failure never raises (startup must not
    die on one index) but is logged at WARNING, not DEBUG, so a missing index or
    column is visible. Callers that log "ensured ..." should check the result.

    Each DDL gets its own begin() so a benign ORA-00955 on one statement cannot
    poison the others. Oracle auto-commits DDL anyway.

    Swallowed Oracle codes:
      ORA-00955  name is already used by an existing object (table/index)
      ORA-00957  duplicate column name
      ORA-01408  such column list already indexed
      ORA-02260  table can have only one primary key
      ORA-02264  name already used by an existing constraint
    The last two matter for tables that declare a NAMED table-level constraint
    (e.g. hotpicks_static_feed's composite PRIMARY KEY), where a re-run can trip
    on the constraint name rather than the table name.
    """
    from sqlalchemy import text

    try:
        with engine.begin() as conn:
            conn.execute(text(sql))
        return True
    except Exception as e:  # noqa: BLE001
        m = str(e)
        if dialect == "oracle" and any(
            code in m
            for code in ("ORA-00955", "ORA-01408", "ORA-00957", "ORA-02260", "ORA-02264")
        ):
            return True
        low = m.lower()
        if "already exists" in low or "duplicate column name" in low:
            return True
        _log.warning(
            "exec_ddl_safe FAILED (%s): %s [sql: %s]", dialect, m[:160], " ".join(str(sql).split())[:100]
        )
        return False
