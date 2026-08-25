"""
NSE Bhavcopy / delivery % helpers — free tier only.

Sources (in order):
1. NSE quote-equity securityWiseDP (live intraday / last session)
2. NSE archives bhavcopy ZIP/CSV for a recent session date (multiple URL patterns)
3. NSE sec_bhavdata_full daily CSV (delivery columns)
4. Neutral fallback with explicit source tag (never invents precision)
"""
from __future__ import annotations

import gc

import csv
import io
import logging
import threading
import time
import zipfile
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger("market-data-bhavcopy")

# Universal ≤ ₹5000 gate — drop high-ticket equities at the bhavcopy root
MAX_STOCK_PRICE = 5000.0

# Remember which URL class worked last (process-local)
_BHAV_LAST_GOOD_PREFIX: str = ""
IST = ZoneInfo("Asia/Kolkata")

# Cache of a fully-downloaded-and-parsed bhavcopy CSV for a given session
# date, shared across every symbol lookup within this process.
#
# Why this exists: delivery_from_bhavcopy() used to download AND re-parse
# the entire market-wide bhavcopy CSV from scratch for every single symbol
# that called it — one full file (several MB) plus the whole multi-URL
# 404-cascade (NSE blocks/moves endpoints often enough that 3-5 URLs
# routinely fail before one succeeds), *per symbol*, even though the file
# content for a given date never changes. With dozens of symbols needing a
# delivery-%/EOD-close repair in the same run, this multiplied both the
# NSE request volume (worse 403-blocking risk) and the wall-clock time by
# however many symbols were being repaired. Logs showed the exact same
# cascade (404, 404, 404, 200) appearing twice back-to-back for the same
# date — that was two different symbols each redoing the full fetch.
# Caching the parsed dict once per date fixes both.
_BHAV_DAY_CACHE: Dict[str, Dict[str, dict]] = {}
_BHAV_DAY_CACHE_MAX_DATES = 3  # only ever need "today" + a couple of fallback sessions

NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/all-reports",
    "Connection": "keep-alive",
}

# Browser-navigation headers for the 3 bootstrap HTML hops only (matches the
# fix already applied in api-gateway/ipo_scanner.py and api-gateway/main.py
# _get_nse_client — see those for the full "Accept: application/json on an
# HTML doc triggers Akamai 403" explanation).
_NSE_BOOTSTRAP_HEADERS = {
    "User-Agent": NSE_HEADERS["User-Agent"],
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

# 2026-08-24 fix: _nse_client() used to build a BRAND NEW httpx.Client and
# redo the full 3-hop cookie bootstrap on every single call — every
# delivery-%/EOD-close/quote lookup for every symbol during a repair batch,
# instead of once. That multiplied NSE request volume several-fold and made
# the "403 Forbidden" blocking worse, not better (this is the same class of
# bug ipo_scanner.py's module docstring already documents and fixed for
# itself with a cached, short-TTL session). Cache and reuse one session for
# a few minutes instead.
_NSE_SESSION_CACHE: Dict[str, Any] = {"client": None, "ts": 0.0}
_NSE_SESSION_TTL_SECONDS = 300  # 5 minutes
_NSE_SESSION_LOCK = threading.Lock()


def _nse_client(force_new: bool = False) -> httpx.Client:
    with _NSE_SESSION_LOCK:
        cached = _NSE_SESSION_CACHE.get("client")
        age = time.time() - _NSE_SESSION_CACHE.get("ts", 0.0)
        if not force_new and cached is not None and age < _NSE_SESSION_TTL_SECONDS:
            return cached

        c = httpx.Client(timeout=25, headers=NSE_HEADERS, follow_redirects=True)
        try:
            c.get("https://www.nseindia.com", headers=_NSE_BOOTSTRAP_HEADERS)
            c.get("https://www.nseindia.com/market-data/securities-available-for-trading", headers=_NSE_BOOTSTRAP_HEADERS)
            c.get("https://www.nseindia.com/all-reports", headers=_NSE_BOOTSTRAP_HEADERS)
        except Exception:
            pass

        old = _NSE_SESSION_CACHE.get("client")
        if old is not None and old is not c:
            try:
                old.close()
            except Exception:
                pass
        _NSE_SESSION_CACHE["client"] = c
        _NSE_SESSION_CACHE["ts"] = time.time()
        return c


def delivery_from_quote(symbol: str) -> Optional[Dict[str, Any]]:
    """Live delivery % from NSE quote-equity (securityWiseDP)."""
    sym = symbol.upper().replace(".NS", "").replace(".BO", "")
    try:
        client = _nse_client()
        r = client.get(f"https://www.nseindia.com/api/quote-equity?symbol={sym}")
        if r.status_code != 200:
                return None
        data = r.json()
        sw = data.get("securityWiseDP") or {}
        pct = sw.get("deliveryToTradedQuantity")
        if pct is not None:
                return {
                    "symbol": sym,
                    "delivery_pct": round(float(pct), 2),
                    "traded_qty": sw.get("quantityTraded"),
                    "delivery_qty": sw.get("deliveryQuantity"),
                    "source": "nse_quote_equity",
                    "session_date": None,
                }
        tq, dq = sw.get("quantityTraded"), sw.get("deliveryQuantity")
        if tq and dq:
                return {
                    "symbol": sym,
                    "delivery_pct": round(float(dq) / float(tq) * 100.0, 2),
                    "traded_qty": tq,
                    "delivery_qty": dq,
                    "source": "nse_quote_equity",
                    "session_date": None,
                }
    except Exception as e:
        logger.warning("quote delivery failed %s: %s", sym, e)
    return None


def _candidate_session_dates(n: int = 6) -> List:
    """Recent business-looking dates (skip pure weekends; holidays still tried)."""
    out = []
    d = datetime.now(IST).date()
    # After ~18:30 IST, same-day bhav may exist; before that prefer prior sessions
    now = datetime.now(IST)
    if now.hour < 18:
        d = d - timedelta(days=1)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out


def _bhav_urls_for_date(d) -> List[str]:
    """
    NSE archive URL patterns for a session date (newest patterns first).

    Prefer sec_bhavdata_full CSV (currently reliable). Historical cm{DD}{MON}{YYYY}bhav.zip
    often 404s until NSE publishes / renames paths — try those last.
    Month folder is UPPER (AUG); day is zero-padded.
    """
    dd = d.strftime("%d")
    mon = d.strftime("%b").upper()          # AUG
    mon_title = d.strftime("%b").title()    # Aug
    yyyy = d.strftime("%Y")
    ddmmyyyy = d.strftime("%d%m%Y")         # 18082026
    mm = d.strftime("%m")
    yyyymmdd = d.strftime("%Y%m%d")
    return [
        # 1) Working daily full bhav (delivery cols) — try first
        f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv",
        f"https://archives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv",
        f"https://nsearchives.nseindia.com/content/Equities/sec_bhavdata_full_{ddmmyyyy}.csv",
        # 2) Newer CM UDiFF-style names
        f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{yyyymmdd}_F_0000.csv.gz",
        f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{yyyymmdd}_F_0000.csv.zip",
        f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{yyyymmdd}_F_0000.csv",
        # 3) Legacy monthly folder zips (often 404 mid-day / path changes)
        f"https://nsearchives.nseindia.com/content/historical/EQUITIES/{yyyy}/{mon}/cm{dd}{mon}{yyyy}bhav.csv.zip",
        f"https://archives.nseindia.com/content/historical/EQUITIES/{yyyy}/{mon}/cm{dd}{mon}{yyyy}bhav.csv.zip",
        f"https://www.nseindia.com/content/historical/EQUITIES/{yyyy}/{mon}/cm{dd}{mon}{yyyy}bhav.csv.zip",
        # 4) Reports API (needs cookies sometimes)
        f"https://www.nseindia.com/api/reports?archives=%5B%7B%22name%22%3A%22CM%20-%20Bhavcopy(csv)%22%2C%22type%22%3A%22archives%22%2C%22category%22%3A%22capital-market%22%2C%22section%22%3A%22equities%22%7D%5D&date={dd}-{mon_title}-{yyyy}&type=equities&mode=single",
    ]


def process_bhavcopy_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Universal ≤ ₹5000 + EQ-only filter for any bulk bhavcopy row list.
    Drop high-ticket names before they reach Neon / scanners / training.
    """
    out: List[Dict[str, Any]] = []
    dropped_price = 0
    dropped_series = 0
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        # SERIES / series filter when present
        series = str(row.get("SERIES") or row.get("series") or row.get("SctySrs") or "EQ").strip().upper()
        if series and series not in ("EQ", "BE", "BZ", ""):
            dropped_series += 1
            continue
        close = None
        for k in ("CLOSE", "close", "LAST", "last", "ClsPric", "LastPric", "price", "ltp", "CLOSE_PRICE"):
            raw = row.get(k)
            if raw is None or raw == "":
                continue
            try:
                # Strip NSE comma formats: "5,123.45" / "12,34,567.00" / NBSP
                s = (
                    str(raw)
                    .replace(",", "")
                    .replace("\u00a0", "")
                    .replace(" ", "")
                    .strip()
                )
                if not s or s.upper() in ("-", "NA", "N/A", "NONE", "NULL"):
                    continue
                close = float(s)
                if close > 0:
                    break
            except (TypeError, ValueError):
                close = None
        # Strict: known close > ₹5000 → drop.
        if close is not None and close > MAX_STOCK_PRICE:
            dropped_price += 1
            continue
        out.append(row)
    if dropped_price or dropped_series:
        logger.info(
            "process_bhavcopy_rows: kept=%s dropped_price=%s dropped_series=%s (max=%.0f)",
            len(out), dropped_price, dropped_series, MAX_STOCK_PRICE,
        )
    return out



# Columns required for EQ filter + ₹5000 gate — ignore the rest to cut RAM ~80%
_BHAV_USECOLS = frozenset({
    "SYMBOL", "symbol", "TckrSymb", "SECURITY",
    "SERIES", "series", "SctySrs",
    "CLOSE", "close", "LAST", "last", "ClsPric", "LastPric", "CLOSE_PRICE",
    "price", "ltp",
})


def load_bhavcopy_slim(file_path_or_buffer) -> "pd.DataFrame":
    """
    RAM-safe bhavcopy reader: only SYMBOL / SERIES / CLOSE|LAST columns.
    Accepts a path, file handle, or in-memory buffer (StringIO / BytesIO text).
    """
    import pandas as pd

    def _usecols(c):
        try:
            return str(c).strip() in _BHAV_USECOLS
        except Exception:
            return False

    try:
        df = pd.read_csv(
            file_path_or_buffer,
            usecols=_usecols,
            dtype=str,  # keep as str until we coerce prices — avoids mixed-type RAM bloat
            low_memory=True,
        )
    except ValueError:
        # usecols mismatch (header variants) — fall back to full read then drop
        df = pd.read_csv(file_path_or_buffer, dtype=str, low_memory=True)
        keep = [c for c in df.columns if str(c).strip() in _BHAV_USECOLS]
        if keep:
            df = df[keep]

    # Free any intermediate frames
    gc.collect()
    return df


def process_bhavcopy_dataframe(df):  # type: ignore[no-untyped-def]
    """
    Root ₹5000 filter for pandas DataFrames (NSE bhavcopy / sec_bhavdata).
    Drop expensive stocks immediately before they enter the database during
    a fresh feed. Accepts a pandas DataFrame *or* a list of dict rows.
    Returns a filtered DataFrame when input was a DataFrame, otherwise a list.
    Falls back to pure-Python if pandas is not installed.
    """
    # Path / buffer → slim CSV load (only SYMBOL/SERIES/CLOSE columns)
    if isinstance(df, (str, bytes)) or hasattr(df, "read"):
        try:
            df = load_bhavcopy_slim(df)
        except Exception as e:
            logger.warning("load_bhavcopy_slim failed: %s", e)
            return df if not isinstance(df, (str, bytes)) else None

    # List / records path — always use process_bhavcopy_rows
    if isinstance(df, list):
        kept = process_bhavcopy_rows(df)
        gc.collect()
        return kept

    if df is None:
        return df

    try:
        import pandas as pd  # optional dependency
    except ImportError:
        # Pure-Python fallback for objects that look like DataFrames
        if hasattr(df, "to_dict"):
            try:
                return process_bhavcopy_rows(df.to_dict(orient="records"))
            except Exception:
                return df
        return df

    if not isinstance(df, pd.DataFrame):
        # Unknown type — try records conversion, else pass through
        if hasattr(df, "to_dict"):
            try:
                return process_bhavcopy_rows(df.to_dict(orient="records"))
            except Exception:
                pass
        return df

    if df.empty:
        return df

    work = df.copy()
    work.columns = work.columns.str.strip()

    # EQ-only (allow BE/BZ which are still cash equity)
    series_col = None
    for c in ("SERIES", "series", "SctySrs"):
        if c in work.columns:
            series_col = c
            break
    if series_col is not None:
        s = work[series_col].astype(str).str.strip().str.upper()
        work = work[s.isin(["EQ", "BE", "BZ", ""]) | s.isna()]

    # Force conversion to float on ALL known price columns (NSE comma formats)
    for col in ("CLOSE", "close", "LAST", "last", "ClsPric", "LastPric", "CLOSE_PRICE", "price", "ltp"):
        if col in work.columns:
            work[col] = pd.to_numeric(
                work[col]
                .astype(str)
                .str.replace(",", "", regex=False)
                .str.replace("\u00a0", "", regex=False)
                .str.strip(),
                errors="coerce",
            )

    # Strict <= 5000 filter (NaN after coerce is dropped — prevents string-trap survivors)
    if "CLOSE" in work.columns:
        work = work[work["CLOSE"].notna() & (work["CLOSE"] > 0) & (work["CLOSE"] <= MAX_STOCK_PRICE)]
    elif "close" in work.columns:
        work = work[work["close"].notna() & (work["close"] > 0) & (work["close"] <= MAX_STOCK_PRICE)]
    elif "LAST" in work.columns:
        work = work[work["LAST"].notna() & (work["LAST"] > 0) & (work["LAST"] <= MAX_STOCK_PRICE)]
    elif "last" in work.columns:
        work = work[work["last"].notna() & (work["last"] > 0) & (work["last"] <= MAX_STOCK_PRICE)]

    logger.info(
        "process_bhavcopy_dataframe: %s → %s rows (strict max_price=%.0f, commas stripped)",
        len(df), len(work), MAX_STOCK_PRICE,
    )
    out = work.reset_index(drop=True)
    # Drop reference to full work frame and reclaim RAM on free-tier (512MB)
    try:
        del work
    except Exception:
        pass
    gc.collect()
    return out


def _parse_bhav_csv_all(text: str) -> Dict[str, Dict[str, Any]]:
    """
    Same column-detection logic as _parse_bhav_csv, but parses every EQ/BE/BZ
    row into a {symbol: row} dict in one pass instead of scanning for a
    single symbol. This is what makes the per-date cache possible — one
    parse serves every symbol that needs this date's data.
    """
    out: Dict[str, Dict[str, Any]] = {}
    if text.startswith("\ufeff"):
        text = text[1:]
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return out
    fields = {f.lower().strip().replace(" ", "_"): f for f in reader.fieldnames}

    def col(*names):
        for n in names:
            key = n.lower().strip().replace(" ", "_")
            if key in fields:
                return fields[key]
        return None

    sym_col = col("SYMBOL", "symbol", "TckrSymb", "SECURITY")
    series_col = col("SERIES", "series", "SctySrs")
    close_col = col("CLOSE", "close", "LAST", "last", "ClsPric", "LastPric", "ClosePrice")
    deliv_pct_col = col(
        "DELIV_PER", "DELIVERY_PER", "DELIV_PERCENTAGE", "DELIV_PERC",
        "DELIVERY_%", "DELIVERY_PERCENT", "DelivPer",
    )
    deliv_qty_col = col(
        "DELIV_QTY", "DELIVERY_QTY", "DELIV_QUANTITY", "DELIVERY_QUANTITY", "DelivQty",
    )
    tq_col = col(
        "TTL_TRD_QNTY", "TOTAL_TRADES", "TOTTRDQTY", "NO_OF_SHRS", "VOLUME",
        "TtlTradgVol", "TOT_TRADED_QTY", "TRADED_QTY",
    )
    if not sym_col:
        return out

    for row in reader:
        row_sym = (row.get(sym_col) or "").strip().upper()
        if not row_sym:
            continue
        if series_col:
            series = str(row.get(series_col) or "").strip().upper()
            if series and series not in ("EQ", "BE", "BZ"):
                continue
        close_px = None
        if close_col:
            try:
                close_raw = str(row.get(close_col) or "").replace(",", "").strip()
                if close_raw and close_raw.upper() not in ("-", "NA", "N/A"):
                    close_px = float(close_raw)
                    if close_px > MAX_STOCK_PRICE:
                        continue  # high-ticket — do not cache this row
            except (TypeError, ValueError):
                close_px = None
        pct = None
        if deliv_pct_col and row.get(deliv_pct_col):
            try:
                raw = str(row[deliv_pct_col]).replace(",", "").replace("%", "").strip()
                if raw and raw.upper() not in ("-", "NA", "N/A"):
                    pct = float(raw)
            except ValueError:
                pct = None
        if pct is None and deliv_qty_col and tq_col:
            try:
                dq = float(str(row[deliv_qty_col]).replace(",", "").strip())
                tq = float(str(row[tq_col]).replace(",", "").strip()) or 1.0
                pct = dq / tq * 100.0
            except ValueError:
                pct = None
        if pct is None and close_px is None:
            continue
        # Don't overwrite an EQ row already captured with a later BE/BZ dup
        if row_sym in out and out[row_sym].get("close") is not None:
            continue
        out[row_sym] = {
            "symbol": row_sym,
            "delivery_pct": round(pct, 2) if pct is not None else None,
            "close": close_px,
            "traded_qty": row.get(tq_col) if tq_col else None,
            "delivery_qty": row.get(deliv_qty_col) if deliv_qty_col else None,
            "source": "nse_bhavcopy",
        }
    return out


def _fetch_bhav_day_parsed(client: httpx.Client, d) -> Optional[Dict[str, Dict[str, Any]]]:
    """
    Download + parse one session date's bhavcopy once, cached for the rest
    of this process's lifetime under _BHAV_DAY_CACHE. Every symbol lookup
    for the same date after the first one is a plain dict lookup — no
    network call at all.
    """
    key = d.isoformat()
    if key in _BHAV_DAY_CACHE:
        return _BHAV_DAY_CACHE[key]

    for url in _bhav_urls_for_date(d):
        try:
            r = client.get(url)
            if r.status_code != 200 or not r.content:
                continue
            content_type = (r.headers.get("content-type") or "").lower()
            text = None
            body = r.content
            if url.endswith(".zip") or "zip" in content_type or body[:2] == b"PK":
                try:
                    with zipfile.ZipFile(io.BytesIO(body)) as zf:
                        name = next((n for n in zf.namelist() if n.lower().endswith(".csv")), None)
                        if not name:
                            continue
                        text = zf.read(name).decode("utf-8", errors="ignore")
                except zipfile.BadZipFile:
                    continue
            else:
                if "application/json" in content_type:
                    continue
                text = r.text
            if not text:
                continue
            head = text[:800].upper()
            if "SYMBOL" not in head and "TCKRSYMB" not in head and "SECURITY" not in head:
                continue
            parsed = _parse_bhav_csv_all(text)
            if parsed:
                # Keep the cache small — only the most recent few dates are
                # ever asked for in practice (repair loops walk forward from
                # "today"), so an unbounded cache would just be a slow leak.
                if len(_BHAV_DAY_CACHE) >= _BHAV_DAY_CACHE_MAX_DATES:
                    oldest = min(_BHAV_DAY_CACHE.keys())
                    _BHAV_DAY_CACHE.pop(oldest, None)
                _BHAV_DAY_CACHE[key] = parsed
                return parsed
        except Exception as e:
            logger.debug("bhav day fetch failed %s %s: %s", d, url, e)
            continue
    return None


def _parse_bhav_csv(text: str, symbol: str) -> Optional[Dict[str, Any]]:
    sym = symbol.upper().replace(".NS", "").replace(".BO", "")
    # Normalise possible BOM / whitespace
    if text.startswith("\ufeff"):
        text = text[1:]
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return None
    fields = {f.lower().strip().replace(" ", "_"): f for f in reader.fieldnames}

    def col(*names):
        for n in names:
            key = n.lower().strip().replace(" ", "_")
            if key in fields:
                return fields[key]
        return None

    sym_col = col("SYMBOL", "symbol", "TckrSymb", "SECURITY")
    series_col = col("SERIES", "series", "SctySrs")
    close_col = col("CLOSE", "close", "LAST", "last", "ClsPric", "LastPric", "ClosePrice")
    deliv_pct_col = col(
        "DELIV_PER", "DELIVERY_PER", "DELIV_PERCENTAGE", "DELIV_PERC",
        "DELIVERY_%", "DELIVERY_PERCENT", "DelivPer",
    )
    deliv_qty_col = col(
        "DELIV_QTY", "DELIVERY_QTY", "DELIV_QUANTITY", "DELIVERY_QUANTITY", "DelivQty",
    )
    tq_col = col(
        "TTL_TRD_QNTY", "TOTAL_TRADES", "TOTTRDQTY", "NO_OF_SHRS", "VOLUME",
        "TtlTradgVol", "TOT_TRADED_QTY", "TRADED_QTY",
    )

    if not sym_col:
        return None
    for row in reader:
        row_sym = (row.get(sym_col) or "").strip().upper()
        if row_sym != sym:
            continue
        # EQ-prefer when series column present
        if series_col:
            series = str(row.get(series_col) or "").strip().upper()
            if series and series not in ("EQ", "BE", "BZ"):
                continue
        # Universal ≤ ₹5000 gate when close/last is present
        close_px = None
        if close_col:
            try:
                close_raw = str(row.get(close_col) or "").replace(",", "").strip()
                if close_raw and close_raw.upper() not in ("-", "NA", "N/A"):
                    close_px = float(close_raw)
                    if close_px > MAX_STOCK_PRICE:
                        return None  # high-ticket — do not use this row
            except (TypeError, ValueError):
                close_px = None
        pct = None
        if deliv_pct_col and row.get(deliv_pct_col):
            try:
                raw = str(row[deliv_pct_col]).replace(",", "").replace("%", "").strip()
                if raw and raw.upper() not in ("-", "NA", "N/A"):
                    pct = float(raw)
            except ValueError:
                pct = None
        if pct is None and deliv_qty_col and tq_col:
            try:
                dq = float(str(row[deliv_qty_col]).replace(",", "").strip())
                tq = float(str(row[tq_col]).replace(",", "").strip()) or 1.0
                pct = dq / tq * 100.0
            except ValueError:
                pct = None
        # Previously this bailed out whenever delivery % couldn't be derived,
        # which also threw away a perfectly good closing price. Keep the row
        # if we have EITHER a delivery % OR a close price — callers that only
        # want EOD price (see eod_close_from_bhavcopy) rely on this.
        if pct is None and close_px is None:
            continue
        return {
            "symbol": sym,
            "delivery_pct": round(pct, 2) if pct is not None else None,
            "close": close_px,
            "traded_qty": row.get(tq_col) if tq_col else None,
            "delivery_qty": row.get(deliv_qty_col) if deliv_qty_col else None,
            "source": "nse_bhavcopy",
            "raw_row": {k: row.get(k) for k in (sym_col, deliv_pct_col, deliv_qty_col, tq_col, close_col) if k},
        }
    return None


def delivery_from_bhavcopy(symbol: str) -> Optional[Dict[str, Any]]:
    """Download recent official bhavcopy / sec_bhavdata and extract delivery %.

    Routed through _fetch_bhav_day_parsed's per-date cache — the first
    symbol looked up for a given session date pays the download+parse
    cost, every symbol after that for the same date is a dict lookup.
    """
    sym = symbol.upper().replace(".NS", "").replace(".BO", "")
    try:
        client = _nse_client()
        for d in _candidate_session_dates(12):
            day = _fetch_bhav_day_parsed(client, d)
            if not day:
                continue
            row = day.get(sym)
            if row:
                out = dict(row)
                out["session_date"] = d.isoformat()
                return out
    except Exception as e:
        logger.warning("bhavcopy pipeline failed for %s: %s", sym, e)
    return None


def eod_close_from_bhavcopy(symbol: str) -> Optional[float]:
    """Last-resort EOD close price straight from the official NSE bhavcopy.

    Used as the final rung of the live-price waterfall (see
    ``_waterfall_bhavcopy_price`` in main.py) when Yahoo/TwelveData/
    AlphaVantage/Polygon all fail — e.g. during a yfinance "Invalid Crumb"
    outage. It's end-of-day, not live, but a real EOD close is far better
    for the Data Feed Health repair flow than leaving a record at price=0
    forever.

    Routed through _fetch_bhav_day_parsed's per-date cache (same as
    delivery_from_bhavcopy) — this was previously a second, independent
    per-symbol full-file download+parse, so any repair run needing both
    delivery% and EOD close for the same symbol (or EOD close for several
    symbols) downloaded the identical CSV redundantly, once per call.
    """
    sym = symbol.upper().replace(".NS", "").replace(".BO", "")
    try:
        client = _nse_client()
        for d in _candidate_session_dates(12):
            day = _fetch_bhav_day_parsed(client, d)
            if not day:
                continue
            row = day.get(sym)
            if row and row.get("close"):
                return float(row["close"])
    except Exception as e:
        logger.warning("eod_close_from_bhavcopy failed for %s: %s", sym, e)
    return None


def delivery_from_nse_cm_series(symbol: str) -> Optional[Dict[str, Any]]:
    """Try NSE equity master / series-wise snapshot if available via public API."""
    sym = symbol.upper().replace(".NS", "").replace(".BO", "")
    try:
        client = _nse_client()
            # Historical data API used by NSE charts — may include delivery on some builds
        r = client.get(
                f"https://www.nseindia.com/api/historical/cm/equity"
                f"?symbol={sym}&series=[%22EQ%22]&from="
                f"{(datetime.now(IST).date() - timedelta(days=10)).strftime('%d-%m-%Y')}"
                f"&to={datetime.now(IST).date().strftime('%d-%m-%Y')}"
            )
        if r.status_code != 200:
                return None
        data = r.json()
        rows = data.get("data") or data.get("historicalData") or []
        if not rows:
            return None
            # Prefer newest row with delivery fields
            for row in reversed(rows):
                pct = row.get("CH_DELIVERY_PERC") or row.get("DELIV_PER") or row.get("deliveryToTradedQuantity")
                if pct is None:
                    continue
                return {
                    "symbol": sym,
                    "delivery_pct": round(float(pct), 2),
                    "traded_qty": row.get("CH_TOT_TRADED_QTY") or row.get("TTL_TRD_QNTY"),
                    "delivery_qty": row.get("CH_DELIV_QTY"),
                    "source": "nse_cm_historical",
                    "session_date": row.get("CH_TIMESTAMP") or row.get("mTIMESTAMP"),
                }
    except Exception as e:
        logger.debug("cm series delivery failed %s: %s", sym, e)
    return None


def get_delivery(symbol: str) -> Dict[str, Any]:
    """Public resolver: quote → bhavcopy → cm historical → neutral fallback."""
    sym = symbol.upper().replace(".NS", "").replace(".BO", "")
    for fn in (delivery_from_quote, delivery_from_bhavcopy, delivery_from_nse_cm_series):
        try:
            hit = fn(sym)
            if hit and hit.get("delivery_pct") is not None:
                hit["fetched_at"] = datetime.utcnow().isoformat()
                return hit
        except Exception as e:
            logger.warning("%s failed: %s", fn.__name__, e)
    return {
        "symbol": sym,
        "delivery_pct": 50.0,
        "source": "fallback_neutral",
        "session_date": None,
        "fetched_at": datetime.utcnow().isoformat(),
        "note": "Official delivery unavailable; neutral placeholder used so scoring never crashes.",
    }
