"""
Hot Picks durable store — 24h table, instant-result load, freshness gate,
stop flag and feed audit. Runs identically on Neon/Postgres (Render) and Oracle
Autonomous DB (Oracle VM); every dialect difference is resolved inside
hotpicks_schema.py, never here.

This is the Hot Picks half of the four-part pattern already used by the IPO
tracker and the surprise premarket feed:

  1. hotpicks_schema.py      portable DDL + upsert/select SQL      (both backends)
  2. this module             writer / reader / freshness / audit
  3. stop flag               request_hotpicks_stop() honoured mid-scan
  4. /stockky-hot/stop|table|audit endpoints in main.py

Why a DB table when kv_cache already caches the result: kv_cache holds ONE blob
under one key with a TTL, so a stopped or partial scan leaves nothing behind and
a redeploy/TTL expiry means the tab paints empty until the user sits through a
fresh multi-minute scan. hotpicks_static_feed keeps per-(symbol, section) rows
for a rolling window, so the tab paints instantly from the last 24 hours,
partial scans still contribute, and "is this stale?" becomes a SQL question
instead of a guess.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("hotpicks-store")

# A scan within this many hours of the last one can be served from the table
# instead of re-walking the universe (same default/meaning as IPO_DB_FRESH_HOURS).
HOTPICKS_DB_FRESH_HOURS = float(os.getenv("HOTPICKS_DB_FRESH_HOURS", "24"))
# Rows older than this are pruned after each successful write.
HOTPICKS_RETENTION_HOURS = float(os.getenv("HOTPICKS_RETENTION_HOURS", "72"))
# Default display window for /stockky-hot/table.
HOTPICKS_TABLE_HOURS = float(os.getenv("HOTPICKS_TABLE_HOURS", "24"))

SECTIONS = ("news_driven", "results_driven", "bulk_insider_driven")

# Short-lived memo for the feed-health audit. The tab re-mounts on every switch
# and the panel refetches; the counts cannot change meaningfully inside this
# window, so serving a memoised copy removes the query round-trip from tab load.
HOTPICKS_AUDIT_TTL_SEC = float(os.getenv("HOTPICKS_AUDIT_TTL_SEC", "20"))
_AUDIT_CACHE: Dict[str, Any] = {}
_AUDIT_LOCK = threading.Lock()


# ── Stop flag ───────────────────────────────────────────────────────────────
# Process-local threading.Event, exactly like ipo_scanner's _IPO_STOP_FLAG. The
# scan loop lives in this same process (BackgroundTasks), so an Event is enough
# and needs no DB round-trip per symbol.
_HOTPICKS_STOP_FLAG = threading.Event()


def request_hotpicks_stop() -> None:
    """Ask the running Hot Picks scan to stop after the current symbol."""
    _HOTPICKS_STOP_FLAG.set()


def clear_hotpicks_stop() -> None:
    """Reset the flag — called when a new scan starts."""
    _HOTPICKS_STOP_FLAG.clear()


def hotpicks_stop_requested() -> bool:
    return _HOTPICKS_STOP_FLAG.is_set()


# ── Helpers ────────────────────────────────────────────────────────────────
def _schema():
    """Import lazily so a missing module degrades to 'no durable store' rather
    than breaking the whole gateway at import time."""
    import hotpicks_schema

    return hotpicks_schema


def _lob_to_str(value) -> Optional[str]:
    """Oracle CLOB -> str. oracle_compat sets oracledb.defaults.fetch_lobs=False
    so this is normally already a str; the .read() fallback covers an engine
    built before that default was applied."""
    if value is None or isinstance(value, str):
        return value
    reader = getattr(value, "read", None)
    if callable(reader):
        try:
            return reader()
        except Exception:
            return None
    return str(value)


def _num(value) -> Optional[float]:
    """Postgres NUMERIC comes back as Decimal, Oracle NUMBER as int/float.
    Normalising here means the JSON the frontend sees is byte-identical on both
    backends instead of '75' on one and '75.00' on the other."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _utc_hours_since(last_at) -> Optional[float]:
    """Hours between last_at and now, tolerating naive vs aware timestamps.

    Postgres TIMESTAMPTZ returns tz-aware; Oracle TIMESTAMP returns naive and is
    UTC on Autonomous DB (SYSTIMESTAMP). Same assumption ipo_scanner already
    makes for ipo_static_feed, kept identical on purpose.
    """
    if last_at is None:
        return None
    try:
        now = datetime.now(timezone.utc)
        la = last_at if getattr(last_at, "tzinfo", None) else last_at.replace(tzinfo=timezone.utc)
        return (now - la).total_seconds() / 3600.0
    except Exception:
        return None


def _row_to_item(row_map: Dict[str, Any], hp) -> Dict[str, Any]:
    """Rebuild the exact dict shape /stockky-hot returns for one pick.

    item_json is the authoritative copy (so no field the UI reads is ever lost);
    the typed columns exist for querying/auditing and are used as the fallback
    when the JSON is missing or unparseable.
    """
    raw = _lob_to_str(row_map.get("item_json"))
    item: Dict[str, Any] = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                item = parsed
        except Exception:
            item = {}
    item.setdefault("symbol", row_map.get("symbol"))
    item.setdefault("section", row_map.get("section"))
    item.setdefault("decision", row_map.get("decision"))
    if item.get("score") is None:
        item["score"] = _num(row_map.get("score"))
    if item.get("news_score") is None and row_map.get("news_score") is not None:
        item["news_score"] = _num(row_map.get("news_score"))
    if item.get("headline_count") is None:
        item["headline_count"] = _int(row_map.get("headline_count"))
    item.setdefault("signal_strength", row_map.get("signal_strength"))
    item.setdefault("summary", _lob_to_str(row_map.get("summary")) or "")
    if row_map.get("next_earnings_date") and not item.get("next_earnings_date"):
        item["next_earnings_date"] = row_map.get("next_earnings_date")
    item["from_scan"] = hp.coerce_bool(row_map.get("from_scan"))
    # Provenance so the UI can label a row as "from the stored 24h table".
    item["stored_at"] = (
        row_map.get("updated_at").isoformat()
        if hasattr(row_map.get("updated_at"), "isoformat")
        else row_map.get("updated_at")
    )
    item["stored_generated_at"] = row_map.get("generated_at")
    return item


def _payload_rows(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flatten a /stockky-hot payload into one DB row per (symbol, section)."""
    hp = _schema()
    generated_at = str(payload.get("generated_at") or "")[:40]
    rows: List[Dict[str, Any]] = []
    for section in SECTIONS:
        for item in payload.get(section) or []:
            if not isinstance(item, dict):
                continue
            sym = str(item.get("symbol") or "").strip().upper()
            if not sym:
                continue
            try:
                item_json = json.dumps(item, default=str)
            except Exception:
                item_json = None
            rows.append({
                "symbol": sym,
                "section": section,
                "decision": item.get("decision"),
                "score": _num(item.get("score")),
                "news_score": _num(item.get("news_score")),
                "headline_count": _int(item.get("headline_count")),
                "signal_strength": item.get("signal_strength"),
                "from_scan": bool(item.get("from_scan")),
                # Stored as a plain string on purpose — see the schema module's
                # "NO DATE BIND ANYWHERE" note.
                "next_earnings_date": (
                    str(item.get("next_earnings_date"))[:20]
                    if item.get("next_earnings_date")
                    else None
                ),
                "summary": item.get("summary") or "",
                "item_json": item_json,
                "generated_at": generated_at,
            })
    # Guarantee every declared bind key exists, so executemany on Postgres can
    # never fail with "a bound parameter is missing".
    for r in rows:
        for k in hp.ROW_KEYS:
            r.setdefault(k, None)
    return rows


# ── Writer ─────────────────────────────────────────────────────────────────
def hotpicks_db_upsert(payload: Dict[str, Any]) -> int:
    """Persist a Hot Picks payload to hotpicks_static_feed.

    Best-effort by design: a DB failure must never break the scan, because
    kv_cache is still the fast path the endpoints serve from. Returns the number
    of rows written (0 on any failure or when no backend is configured).
    """
    if not isinstance(payload, dict):
        return 0
    try:
        hp = _schema()
    except Exception:
        return 0
    if not hp.database_url():
        return 0
    rows = _payload_rows(payload)
    if not rows:
        return 0
    eng = None
    try:
        from sqlalchemy import text

        dial = hp.dialect()
        eng = hp.make_engine("stockky-hotpicks-writer")
        if eng is None:
            return 0
        adapted = hp.adapt_rows(rows, dial)
        stmt = text(hp.upsert_sql(dial))
        n = 0
        if dial == "oracle":
            # Row-by-row: python-oracledb sizes executemany binds from the FIRST
            # row, so one short summary followed by a long one would silently
            # truncate. Per-statement execution re-derives the bind types.
            with eng.begin() as conn:
                for row in adapted:
                    conn.execute(stmt, row)
                    n += 1
        else:
            with eng.begin() as conn:
                conn.execute(stmt, adapted)
                n = len(adapted)
        _prune(eng, hp, dial)
        return n
    except Exception as e:
        logger.warning("hotpicks db upsert failed (non-fatal, kv cache still served): %s", e)
        return 0
    finally:
        if eng is not None:
            try:
                eng.dispose()
            except Exception:
                pass


def _prune(eng, hp, dial: str) -> None:
    """Drop rows past the retention window. Separate transaction so a prune
    failure cannot roll back the rows we just wrote."""
    try:
        from sqlalchemy import text

        with eng.begin() as conn:
            conn.execute(
                text(hp.delete_older_than_sql(dial)),
                {"hours": int(HOTPICKS_RETENTION_HOURS)},
            )
    except Exception as e:
        logger.debug("hotpicks prune skipped: %s", e)


# ── Readers ────────────────────────────────────────────────────────────────
def hotpicks_db_payload(hours: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Rebuild a /stockky-hot-shaped payload from the stored rows.

    Returns None when nothing is configured/stored, so callers can fall through
    to kv_cache or a live scan. This is what makes reopening the tab instant.
    """
    hrs = int(hours if hours is not None else HOTPICKS_TABLE_HOURS)
    try:
        hp = _schema()
        from sqlalchemy import text
    except Exception:
        return None
    if not hp.database_url():
        return None
    eng = None
    try:
        dial = hp.dialect()
        # Shared pool — a tab open must not pay a fresh TLS/wallet handshake.
        eng = hp.shared_engine("stockky-hotpicks-reader")
        if eng is None:
            return None
        with eng.connect() as conn:
            exists = conn.execute(
                text(hp.table_exists_sql(dial)), {"tbl": hp.TABLE_NAME}
            ).scalar()
            if not exists:
                return None
            res = conn.execute(text(hp.select_recent_sql(dial)), {"hours": hrs})
            keys = list(res.keys())
            fetched = res.fetchall()
        sections: Dict[str, List[Dict[str, Any]]] = {s: [] for s in SECTIONS}
        newest: Optional[str] = None
        last_at = None
        for row in fetched:
            # Oracle returns UPPER-CASE column labels for unquoted identifiers.
            row_map = {str(k).lower(): v for k, v in zip(keys, row)}
            section = str(row_map.get("section") or "").lower()
            if section not in sections:
                continue
            sections[section].append(_row_to_item(row_map, hp))
            ga = row_map.get("generated_at")
            if ga and (newest is None or str(ga) > newest):
                newest = str(ga)
            ua = row_map.get("updated_at")
            if ua is not None and (last_at is None or ua > last_at):
                last_at = ua
        total = sum(len(v) for v in sections.values())
        if total == 0:
            return None
        age = _utc_hours_since(last_at)
        return {
            **sections,
            "generated_at": newest,
            "count": total,
            "hours": hrs,
            "age_hours": round(age, 2) if age is not None else None,
            "fresh": bool(age is not None and age <= HOTPICKS_DB_FRESH_HOURS),
            "source": "hotpicks_static_feed",
            "backend": dial,
            "cached": True,
        }
    except Exception as e:
        logger.warning("hotpicks db read failed: %s", e)
        return None


def hotpicks_db_freshness_hours() -> Optional[float]:
    """Hours since hotpicks_static_feed was last written, or None if empty."""
    try:
        hp = _schema()
        from sqlalchemy import text
    except Exception:
        return None
    if not hp.database_url():
        return None
    eng = None
    try:
        dial = hp.dialect()
        eng = hp.shared_engine("stockky-hotpicks-freshness")
        if eng is None:
            return None
        with eng.connect() as conn:
            exists = conn.execute(
                text(hp.table_exists_sql(dial)), {"tbl": hp.TABLE_NAME}
            ).scalar()
            if not exists:
                return None
            row = conn.execute(
                text(f"SELECT MAX(updated_at) FROM {hp.TABLE_NAME}")
            ).fetchone()
        return _utc_hours_since(row[0] if row else None)
    except Exception as e:
        logger.debug("hotpicks freshness check failed: %s", e)
        return None


def hotpicks_audit() -> Dict[str, Any]:
    """Feed-health snapshot for the Hot Picks tab (mirrors the premarket audit).

    Reports which backend is actually in use, whether the table exists, row
    counts per section, staleness, and how many rows are missing a decision or
    score — i.e. everything needed to tell "no picks today" apart from "the
    write path is broken".

    Result is memoised for HOTPICKS_AUDIT_TTL_SEC (default 20s): the panel is
    opened/closed and the tab switched away from and back constantly, and the
    underlying counts cannot meaningfully change in that window, so re-running
    the COUNT(*) + 24h scan on every mount was pure latency.
    """
    now = time.time()
    with _AUDIT_LOCK:
        cached = _AUDIT_CACHE.get("v")
        if cached and now - cached[0] < HOTPICKS_AUDIT_TTL_SEC:
            return dict(cached[1], cached=True)
    out = _hotpicks_audit_uncached()
    with _AUDIT_LOCK:
        _AUDIT_CACHE["v"] = (now, out)
    return dict(out, cached=False)


def _hotpicks_audit_uncached() -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "ok": False,
        "table": None,
        "backend": None,
        "configured": False,
        "table_exists": False,
        "rows_total": 0,
        "rows_24h": 0,
        "by_section": {},
        "age_hours": None,
        "fresh": False,
        "fresh_threshold_hours": HOTPICKS_DB_FRESH_HOURS,
        "retention_hours": HOTPICKS_RETENTION_HOURS,
        "missing_decision": 0,
        "missing_score": 0,
        "missing_price": 0,
        "issues": [],
        # Surprise-panel-shaped aliases — populated for real once the table
        # query below runs; kept present even in early-return branches
        # (schema unavailable, not configured, table missing) so the
        # frontend panel never has to special-case "field doesn't exist
        # yet" vs "field is legitimately zero".
        "health_score": 0.0,
        "total_tracked": 0,
        "fully_populated": 0,
        "missing_data": 0,
        "incomplete_stocks": [],
    }
    try:
        hp = _schema()
        from sqlalchemy import text
    except Exception as e:
        out["issues"].append(f"hotpicks_schema unavailable: {str(e)[:120]}")
        return out
    out["table"] = hp.TABLE_NAME
    out["backend"] = hp.dialect()
    if not hp.database_url():
        out["issues"].append(
            "No CACHE_DATABASE_URL / DATABASE_URL (and no ORACLE_DSN) configured — "
            "Hot Picks runs in memory only and will not survive a restart."
        )
        return out
    out["configured"] = True
    eng = None
    try:
        dial = hp.dialect()
        eng = hp.shared_engine("stockky-hotpicks-audit")
        if eng is None:
            out["issues"].append("Could not build a database engine")
            return out
        with eng.connect() as conn:
            exists = conn.execute(
                text(hp.table_exists_sql(dial)), {"tbl": hp.TABLE_NAME}
            ).scalar()
            out["table_exists"] = bool(exists)
            if not exists:
                out["issues"].append(
                    f"{hp.TABLE_NAME} does not exist yet — it is created on the next scan."
                )
                out["ok"] = True
                return out
            out["rows_total"] = _int(
                conn.execute(text(f"SELECT COUNT(*) FROM {hp.TABLE_NAME}")).scalar()
            )
            res = conn.execute(text(hp.select_recent_sql(dial)), {"hours": 24})
            keys = list(res.keys())
            rows = res.fetchall()
            by_section: Dict[str, int] = {s: 0 for s in SECTIONS}
            missing_decision = 0
            missing_score = 0
            missing_price = 0
            fully_populated = 0
            incomplete_stocks: list = []
            for row in rows:
                m = {str(k).lower(): v for k, v in zip(keys, row)}
                sec = str(m.get("section") or "").lower()
                if sec in by_section:
                    by_section[sec] += 1
                if not m.get("decision"):
                    missing_decision += 1
                if m.get("score") is None:
                    missing_score += 1
                # Price only lives inside item_json (no first-class price
                # column on hotpicks_static_feed — see SELECT_COLUMNS) so
                # it has to be parsed out per row, same way the frontend
                # already reads it off the persisted payload.
                px = 0.0
                try:
                    blob = json.loads(m.get("item_json") or "{}")
                    for k in ("price", "close"):
                        v = float(blob.get(k) or 0)
                        if v > 0:
                            px = v
                            break
                except Exception:
                    pass
                missing_fields = []
                if not m.get("decision"):
                    missing_fields.append("decision")
                if m.get("score") is None:
                    missing_fields.append("score")
                if px <= 0:
                    missing_fields.append("price")
                    missing_price += 1
                if missing_fields:
                    incomplete_stocks.append({"symbol": m.get("symbol"), "missing_fields": missing_fields})
                else:
                    fully_populated += 1
            out["rows_24h"] = len(rows)
            out["by_section"] = by_section
            out["missing_decision"] = missing_decision
            out["missing_score"] = missing_score
            out["missing_price"] = missing_price
            # Surprise-panel-shaped aliases (see audit_surprise_feed in
            # surprise_scanner.py) so the frontend can use ONE
            # <FeedHealthPanel> component for both tabs instead of two
            # near-identical ones reading two different response shapes.
            #
            # IMPORTANT: missing_data / incomplete_stocks here are PRICE-ONLY
            # (matching audit_surprise_feed exactly) even though
            # incomplete_stocks' missing_fields lists decision/score too for
            # display. hotpicks_repair_batch (below) can only ever fix
            # price — it deliberately does not fake a decision/score, those
            # need a real scoring pass. Previously this counted rows missing
            # ONLY decision/score as "missing_data" too, which enabled the
            # Auto-Repair button and inflated its count for something the
            # button could never actually fix — repeatedly reporting "0
            # repaired" and looking broken. missing_decision/missing_score
            # above still surface that gap; they just don't drive the
            # repair button's enabled state or count anymore.
            out["total_tracked"] = out["rows_24h"]
            out["fully_populated"] = fully_populated
            out["missing_data"] = missing_price
            out["health_score"] = (
                round((fully_populated / max(out["rows_24h"], 1)) * 100, 1) if out["rows_24h"] > 0 else 0.0
            )
            out["incomplete_stocks"] = incomplete_stocks[:200]
            last = conn.execute(
                text(f"SELECT MAX(updated_at) FROM {hp.TABLE_NAME}")
            ).fetchone()
        age = _utc_hours_since(last[0] if last else None)
        out["age_hours"] = round(age, 2) if age is not None else None
        out["fresh"] = bool(age is not None and age <= HOTPICKS_DB_FRESH_HOURS)
        if age is None:
            out["issues"].append("Table exists but is empty — run a Hot Picks scan.")
        elif not out["fresh"]:
            out["issues"].append(
                f"Stored picks are {out['age_hours']}h old (threshold "
                f"{HOTPICKS_DB_FRESH_HOURS}h) — next scan will refresh them."
            )
        if out["rows_24h"] and missing_score == out["rows_24h"]:
            out["issues"].append(
                "Every stored row is missing a score — the decision service was "
                "likely unreachable during the last scan."
            )
        out["ok"] = True
        return out
    except Exception as e:
        out["issues"].append(f"audit query failed: {str(e)[:160]}")
        return out


def ensure_hotpicks_schema() -> Dict[str, Any]:
    """Convenience re-export so callers need only import this module."""
    try:
        return _schema().ensure_hotpicks_schema()
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def hotpicks_repair_batch(limit: int = 15, symbol: Optional[str] = None, market_data_url: str = "") -> Dict[str, Any]:
    """Waterfall price fill for hotpicks_static_feed rows missing a price —
    mirrors repair_surprise_batch's exact pattern (surprise_scanner.py),
    adapted for the fact that price here lives inside item_json (see
    _hotpicks_audit_uncached's note) rather than a first-class column, so
    the repair is a targeted UPDATE of item_json + updated_at, not a full
    row upsert. Does NOT attempt to repair missing decision/score — those
    only come from a fresh scoring pass (a real scan), not a price lookup,
    so they're reported by the audit but intentionally left for the next
    'Search Hot Picks Stocks' run rather than faked here.
    """
    import httpx

    out: Dict[str, Any] = {"status": "no_data", "repaired": [], "attempted": 0}
    try:
        hp = _schema()
        from sqlalchemy import text
    except Exception as e:
        out["status"] = "error"
        out["error"] = f"hotpicks_schema unavailable: {str(e)[:160]}"
        return out
    if not hp.database_url():
        out["status"] = "not_configured"
        return out

    force_sym = (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip() or None
    eng = None
    try:
        dial = hp.dialect()
        eng = hp.shared_engine("stockky-hotpicks-repair")
        if eng is None:
            out["status"] = "error"
            out["error"] = "Could not build a database engine"
            return out

        targets: list = []  # list of (symbol, section, item_json_dict)
        with eng.connect() as conn:
            res = conn.execute(
                text(f"SELECT symbol, section, item_json FROM {hp.TABLE_NAME} "
                     "WHERE updated_at >= " + ("SYSTIMESTAMP - NUMTODSINTERVAL(24, 'HOUR')" if dial == "oracle"
                                                else "NOW() - INTERVAL '24 hours'"))
            )
            for row in res.fetchall():
                sym, section, item_json_raw = row[0], row[1], row[2]
                if force_sym and sym != force_sym:
                    continue
                try:
                    blob = json.loads(item_json_raw or "{}")
                except Exception:
                    blob = {}
                px = 0.0
                for k in ("price", "close"):
                    try:
                        v = float(blob.get(k) or 0)
                        if v > 0:
                            px = v
                            break
                    except (TypeError, ValueError):
                        pass
                if px <= 0:
                    targets.append((sym, section, blob))

        if force_sym and not targets:
            out["status"] = "not_found"
            out["message"] = f"{force_sym} not in the last 24h of stored Hot Picks."
            return out
        targets = targets[: max(1, min(int(limit or 15), 30))]
        out["attempted"] = len(targets)
        if not targets:
            out["status"] = "completed"
            out["message"] = "Nothing missing a price."
            return out

        md = (market_data_url or os.getenv("MARKET_DATA_URL") or "").rstrip("/")
        repaired = []
        with httpx.Client(timeout=8.0, follow_redirects=True) as client, eng.begin() as conn:
            for sym, section, blob in targets:
                try:
                    r = client.get(f"{md}/quote/{sym}")
                    if r.status_code != 200:
                        continue
                    body = r.json() if isinstance(r.json(), dict) else {}
                    px = None
                    for k in ("price", "cmp", "ltp", "close", "last_price"):
                        try:
                            v = float(body.get(k) or 0)
                            if v > 0:
                                px = v
                                break
                        except (TypeError, ValueError):
                            pass
                    # Same universe price gate every other repair path in this
                    # codebase enforces — OFF by default (MAX_STOCK_PRICE unset
                    # or 0 means no cap; a symbol only stays "missing price"
                    # here if a cap is explicitly configured and it's over it).
                    _max_px = float(os.getenv("MAX_STOCK_PRICE", "0") or 0)
                    if px is None or (_max_px > 0 and px > _max_px):
                        time.sleep(0.5)
                        continue
                    blob["price"] = px
                    blob["close"] = px
                    conn.execute(
                        text(f"UPDATE {hp.TABLE_NAME} SET item_json = :item_json, "
                             f"updated_at = {hp.now_func(dial)} WHERE symbol = :symbol AND section = :section"),
                        {"item_json": json.dumps(blob)[:15000], "symbol": sym, "section": section},
                    )
                    repaired.append(sym)
                except Exception as e:
                    logger.debug("hotpicks repair %s failed: %s", sym, e)
                time.sleep(0.5)

        with _AUDIT_LOCK:
            _AUDIT_CACHE.pop("v", None)  # force a fresh audit read next call
        out["status"] = "completed"
        out["repaired"] = repaired
        return out
    except Exception as e:
        logger.warning("hotpicks_repair_batch failed: %s", e)
        out["status"] = "error"
        out["error"] = str(e)[:200]
        return out
