#!/usr/bin/env python3
"""
Neon(Render) vs Oracle(VM) mapping audit for the Stockky v2 feed schemas.

Loads each schema module twice — once with a Render-style env (DATABASE_URL to
Neon, no ORACLE_DSN) and once with an Oracle-VM-style env (ORACLE_DSN set) — and
asserts that EVERY piece of SQL it emits belongs to the right dialect, that the
backend selection resolves the way it must, and that the Oracle DDL obeys
Oracle's strictness rules. Exits non-zero on any finding.
"""
from __future__ import annotations

import importlib
import os
import re
import sys

GW = "/sessions/relaxed-laughing-bohr/mnt/outputs/_work/repo/services/api-gateway"
sys.path.insert(0, GW)

NEON_URL = "postgresql://u:p@ep-x.eu-central-1.aws.neon.tech/stockky?sslmode=require"

findings: list[str] = []
checks = 0


def check(cond: bool, label: str) -> None:
    global checks
    checks += 1
    if not cond:
        findings.append(label)


def set_env(mode: str) -> None:
    """Install a clean Render-like or Oracle-VM-like environment."""
    for k in (
        "ORACLE_DSN", "ORACLE_USER", "ORACLE_PASSWORD", "ORACLE_ADMIN_PASSWORD",
        "ORACLE_WALLET_DIR", "ORACLE_WALLET_PASSWORD", "TNS_ADMIN",
        "CACHE_DATABASE_URL", "DATABASE_URL", "TRAINING_DATABASE_URL",
    ):
        os.environ.pop(k, None)
    if mode == "neon":
        os.environ["DATABASE_URL"] = NEON_URL
    else:
        # Oracle VM: the wallet vars decide, and DATABASE_URL is often STILL a
        # Postgres URL left over in the shared .env — Oracle must win anyway.
        os.environ["ORACLE_DSN"] = "stockkydb_high"
        os.environ["ORACLE_USER"] = "ADMIN"
        os.environ["ORACLE_PASSWORD"] = "secret"
        os.environ["ORACLE_WALLET_DIR"] = "/opt/stockky/wallet"
        os.environ["ORACLE_WALLET_PASSWORD"] = "wsecret"
        os.environ["DATABASE_URL"] = NEON_URL


def load(name: str):
    if name in sys.modules:
        return importlib.reload(sys.modules[name])
    return importlib.import_module(name)


# Tokens that must never appear in the other dialect's SQL.
ORACLE_ONLY = ["SYSTIMESTAMP", "VARCHAR2", "NUMBER(", "CLOB", "MERGE INTO",
               "FROM dual", "NUMTODSINTERVAL", "user_tables", "TO_DATE", "TO_CLOB"]
PG_ONLY = ["TIMESTAMPTZ", "BOOLEAN", "ON CONFLICT", "EXCLUDED.", "SERIAL",
           "information_schema", "IF NOT EXISTS", "::date", "INTERVAL '1 hour'",
           " TEXT", "NOW()"]


def scan(sql: str, banned: list[str], where: str) -> None:
    up = sql.upper()
    for tok in banned:
        if tok.upper() in up:
            findings.append(f"{where}: forbidden token {tok!r}")


def audit_module(mod_name: str, table: str, has_select_recent: bool) -> None:
    # ── Render / Neon ────────────────────────────────────────────────────────
    set_env("neon")
    m = load(mod_name)
    check(m.is_oracle() is False, f"{mod_name}: is_oracle() must be False on Render")
    check(m.dialect() == "postgresql", f"{mod_name}: dialect() must be postgresql on Render")
    url = m.database_url() or ""
    check(url.startswith("postgresql://"), f"{mod_name}: Render URL must be postgresql:// (got {url[:24]!r})")
    check("neon.tech" in url, f"{mod_name}: Render URL must point at Neon")
    check("sslmode=require" in url, f"{mod_name}: Neon URL must force sslmode=require")
    check("oracle" not in url.lower(), f"{mod_name}: Render URL must not mention oracle")
    check(not hasattr(m, "now_func") or m.now_func("postgresql") == "NOW()",
          f"{mod_name}: now_func(postgresql)")

    pg_ddl = "\n".join(m.ddl_statements("postgresql"))
    scan(pg_ddl, ORACLE_ONLY, f"{mod_name} postgres DDL")
    check("IF NOT EXISTS" in pg_ddl, f"{mod_name}: postgres DDL should keep IF NOT EXISTS")
    pg_up = m.upsert_sql("postgresql")
    scan(pg_up, ORACLE_ONLY, f"{mod_name} postgres upsert")
    check("ON CONFLICT" in pg_up, f"{mod_name}: postgres upsert needs ON CONFLICT")
    scan(m.table_exists_sql("postgresql"), ORACLE_ONLY, f"{mod_name} postgres table_exists")
    check("information_schema" in m.table_exists_sql("postgresql"),
          f"{mod_name}: postgres table_exists must use information_schema")

    # ── Oracle VM / ADB ──────────────────────────────────────────────────────
    set_env("oracle")
    m = load(mod_name)
    check(m.is_oracle() is True, f"{mod_name}: is_oracle() must be True on the Oracle VM")
    check(m.dialect() == "oracle", f"{mod_name}: dialect() must be oracle on the Oracle VM")
    ourl = m.database_url() or ""
    check(ourl.startswith("oracle"), f"{mod_name}: Oracle URL must start with oracle (got {ourl[:24]!r})")
    check("neon.tech" not in ourl,
          f"{mod_name}: LEAK — Oracle side resolved to the Neon URL {ourl[:40]!r}")
    check("postgres" not in ourl.lower(), f"{mod_name}: Oracle URL must not mention postgres")
    check(not hasattr(m, "now_func") or m.now_func("oracle") == "SYSTIMESTAMP",
          f"{mod_name}: now_func(oracle)")

    or_ddl = "\n".join(m.ddl_statements("oracle"))
    scan(or_ddl, PG_ONLY, f"{mod_name} oracle DDL")
    # Oracle strictness rules
    check("IF NOT EXISTS" not in or_ddl.upper(), f"{mod_name}: Oracle DDL must not use IF NOT EXISTS")
    check(not re.search(r"\bBOOLEAN\b", or_ddl, re.I), f"{mod_name}: Oracle DDL must not use BOOLEAN")
    check(not re.search(r"\bTIMESTAMPTZ\b", or_ddl, re.I), f"{mod_name}: Oracle DDL must not use TIMESTAMPTZ")
    check(not re.search(r"\bSERIAL\b", or_ddl, re.I), f"{mod_name}: Oracle DDL must not use SERIAL")
    check(not re.search(r"\bNOT\s+NULL\s+DEFAULT\b", or_ddl, re.I),
          f"{mod_name}: Oracle DDL has NOT NULL before DEFAULT (must be DEFAULT first)")
    for mm in re.finditer(r"VARCHAR2\((\d+)\)", or_ddl):
        check(int(mm.group(1)) <= 4000, f"{mod_name}: VARCHAR2({mm.group(1)}) exceeds 4000 bytes")

    or_up = m.upsert_sql("oracle")
    scan(or_up, PG_ONLY, f"{mod_name} oracle upsert")
    check("MERGE INTO" in or_up, f"{mod_name}: oracle upsert needs MERGE INTO")
    check("FROM dual" in or_up, f"{mod_name}: oracle upsert needs FROM dual")
    scan(m.table_exists_sql("oracle"), PG_ONLY, f"{mod_name} oracle table_exists")
    check("user_tables" in m.table_exists_sql("oracle"),
          f"{mod_name}: oracle table_exists must use user_tables")
    check("UPPER(:tbl)" in m.table_exists_sql("oracle"),
          f"{mod_name}: oracle table_exists must UPPER() the bound name")

    # ORA-38104: MERGE join keys must not appear in the UPDATE SET list.
    on_clause = re.search(r"\)\s*s\s+ON\s*\((.*?)\)\s*WHEN", or_up, re.S | re.I)
    set_block = re.search(r"WHEN MATCHED THEN UPDATE SET(.*?)WHEN NOT MATCHED", or_up, re.S | re.I)
    if on_clause and set_block:
        keys = set(re.findall(r"d\.(\w+)\s*=", on_clause.group(1)))
        updated = set(re.findall(r"d\.(\w+)\s*=", set_block.group(1)))
        bad = keys & updated
        check(not bad, f"{mod_name}: ORA-38104 — join key(s) {sorted(bad)} in the MERGE UPDATE SET list")

    # Every CLOB column must be bound through TO_CLOB() in the MERGE select list
    # (ORA-01461 if the driver escalates a long str to DB_TYPE_LONG).
    clobs = set(re.findall(r"^\s*(\w+)\s+CLOB", or_ddl, re.M | re.I))
    for col in clobs:
        check(f"TO_CLOB(:{col})" in or_up,
              f"{mod_name}: CLOB column {col!r} bound without TO_CLOB() in the Oracle MERGE")

    if has_select_recent:
        for dial, banned in (("oracle", PG_ONLY), ("postgresql", ORACLE_ONLY)):
            scan(m.select_recent_sql(dial), banned, f"{mod_name} {dial} select_recent")
            scan(m.delete_older_than_sql(dial), banned, f"{mod_name} {dial} delete_older")
        check("NUMTODSINTERVAL" in m.select_recent_sql("oracle"),
              f"{mod_name}: oracle select_recent must use NUMTODSINTERVAL")
        check("INTERVAL '1 hour'" in m.select_recent_sql("postgresql"),
              f"{mod_name}: postgres select_recent must use INTERVAL")

    # Never SELECT * — column order must be stable across both backends.
    check("SELECT *" not in (m.select_recent_sql("oracle") if has_select_recent else ""),
          f"{mod_name}: oracle select_recent must not use SELECT *")
    check(table in or_ddl and table in pg_ddl, f"{mod_name}: table name {table} missing from a DDL")


print("=" * 74)
print("NEON (Render)  vs  ORACLE ADB (Oracle VM) — mapping audit")
print("=" * 74)

audit_module("hotpicks_schema", "hotpicks_static_feed", True)
audit_module("ipo_schema", "ipo_static_feed", False)
audit_module("surprise_schema", "surprise_static_feed", False)

# ── adapt_rows: type coercion must match each backend's column types ─────────
set_env("oracle")
hp = load("hotpicks_schema")
row = {k: None for k in hp.ROW_KEYS}
row.update({"symbol": "RELIANCE", "section": "news_driven", "from_scan": True,
            "summary": "x" * 9000, "item_json": "y" * 90000, "decision": "BUY",
            "generated_at": "2026-08-23T10:00:00+05:30"})
o = hp.adapt_rows([row], "oracle")[0]
check(o["from_scan"] == 1, "hotpicks adapt_rows(oracle): from_scan must be 1/0, not a bool")
check(not isinstance(o["from_scan"], bool), "hotpicks adapt_rows(oracle): from_scan is still a Python bool")
check(len(o["item_json"].encode()) <= hp.JSON_MAX_BYTES, "hotpicks adapt_rows(oracle): item_json over cap")
check(hp.JSON_MAX_BYTES <= 32767, f"hotpicks JSON_MAX_BYTES={hp.JSON_MAX_BYTES} exceeds Oracle's 32767-byte bind ceiling")
check(len(o["summary"].encode()) <= hp.SUMMARY_MAX_BYTES, "hotpicks adapt_rows(oracle): summary over cap")
check(len(o["generated_at"].encode()) <= 40, "hotpicks adapt_rows(oracle): generated_at over VARCHAR2(40)")
p = hp.adapt_rows([row], "postgresql")[0]
check(p["from_scan"] is True, "hotpicks adapt_rows(postgres): from_scan must stay a real bool")
check(hp.coerce_bool(1) is True and hp.coerce_bool(0) is False, "hotpicks coerce_bool(1/0)")
check(hp.coerce_bool(True) is True and hp.coerce_bool(None) is False, "hotpicks coerce_bool(bool/None)")

# No Python date/datetime may ever cross the driver boundary for hotpicks.
import datetime as _dt
for k, v in o.items():
    check(not isinstance(v, (_dt.date, _dt.datetime)),
          f"hotpicks adapt_rows(oracle): {k} binds a Python date object (TO_DATE/ORA-01830 risk)")

ip = load("ipo_schema")
irow = {k: None for k in ip.ROW_KEYS}
irow.update({"symbol": "NEWIPO", "company_name": "Ā" * 200, "listing_date": "",
             "listing_date_estimated": True, "buy_suggestion_json": "z" * 90000})
io = ip.adapt_rows([irow], "oracle")[0]
check(io["listing_date"] is None, "ipo adapt_rows(oracle): empty listing_date must become None, not '1900-01-01'")
check(io["listing_date"] != "1900-01-01", "ipo adapt_rows(oracle): the 1900 sentinel is still being written")
check(io["listing_date_estimated"] == 1, "ipo adapt_rows(oracle): listing_date_estimated must be 1/0")
check(len(io["company_name"].encode()) <= 160, "ipo adapt_rows(oracle): company_name over VARCHAR2(160) bytes")
check(len(io["buy_suggestion_json"].encode()) <= ip.CLOB_BIND_MAX_BYTES,
      "ipo adapt_rows(oracle): buy_suggestion_json not clipped below the LONG-bind ceiling")
check(ip.CLOB_BIND_MAX_BYTES <= 32767,
      f"ipo CLOB_BIND_MAX_BYTES={ip.CLOB_BIND_MAX_BYTES} exceeds Oracle's 32767-byte bind ceiling")
ipg = ip.adapt_rows([irow], "postgresql")[0]
check(ipg["buy_suggestion_json"] == "z" * 90000,
      "ipo adapt_rows(postgres): Render path must NOT clip buy_suggestion_json (byte-for-byte behaviour)")
check(ipg["listing_date"] == "", "ipo adapt_rows(postgres): Render must keep '' for the ::date CASE")

# ── Render path must be byte-for-byte identical to the pristine baseline ────
ORIG = "/sessions/relaxed-laughing-bohr/mnt/outputs/_work/repo_orig/services/api-gateway"
sys.path.insert(0, ORIG)
set_env("neon")
for name, has_sr in (("ipo_schema", False), ("surprise_schema", False)):
    mod_new = load(name)
    new_pg = (mod_new.upsert_sql("postgresql"), "\n".join(mod_new.ddl_statements("postgresql")),
              mod_new.table_exists_sql("postgresql"))
    src = open(os.path.join(ORIG, f"{name}.py")).read()
    ns: dict = {}
    exec(compile(src, f"orig_{name}", "exec"), ns)
    old_pg = (ns["upsert_sql"]("postgresql"), "\n".join(ns["ddl_statements"]("postgresql")),
              ns["table_exists_sql"]("postgresql"))
    for lbl, a, b in zip(("upsert", "ddl", "table_exists"), new_pg, old_pg):
        check(a == b, f"{name}: Render/Postgres {lbl} SQL CHANGED vs baseline — regression risk")

print(f"\n{checks} assertions run.")
if findings:
    print(f"\n*** {len(findings)} FINDING(S) ***")
    for f in findings:
        print(f"  ✗ {f}")
    sys.exit(1)
print("\nRESULT: PASS — Render maps 100% to Neon, Oracle VM maps 100% to Oracle ADB,")
print("        no dialect cross-contamination, Render SQL unchanged from baseline.")
