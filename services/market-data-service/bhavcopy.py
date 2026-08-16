"""
NSE Bhavcopy / delivery % helpers — free tier only.
Sources (in order):
1. NSE quote-equity securityWiseDP (live)
2. NSE archives bhavcopy ZIP/CSV for a recent session date
3. Neutral fallback with explicit source tag (never invents precision)
"""
from __future__ import annotations

import csv
import io
import logging
import zipfile
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger("market-data-bhavcopy")
IST = ZoneInfo("Asia/Kolkata")

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


def _nse_client() -> httpx.Client:
    c = httpx.Client(timeout=20, headers=NSE_HEADERS, follow_redirects=True)
    try:
        c.get("https://www.nseindia.com")
        c.get("https://www.nseindia.com/all-reports")
    except Exception:
        pass
    return c


def delivery_from_quote(symbol: str) -> Optional[Dict[str, Any]]:
    """Live delivery % from NSE quote-equity."""
    sym = symbol.upper().replace(".NS", "").replace(".BO", "")
    try:
        with _nse_client() as client:
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


def _candidate_session_dates(n: int = 8) -> List[datetime]:
    """Recent business-looking dates (skip pure weekends; holidays still tried)."""
    out = []
    d = datetime.now(IST).date()
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out


def _bhav_urls_for_date(d) -> List[str]:
    """Possible NSE archive URL patterns for a date (NSE changes paths periodically)."""
    dd = d.strftime("%d")
    mon = d.strftime("%b").upper()
    yyyy = d.strftime("%Y")
    ddmmyyyy = d.strftime("%d%m%Y")
    # Common historical patterns used by NSE / mirrors
    return [
        f"https://nsearchives.nseindia.com/content/historical/EQUITIES/{yyyy}/{mon}/cm{dd}{mon}{yyyy}bhav.csv.zip",
        f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv",
        f"https://archives.nseindia.com/content/historical/EQUITIES/{yyyy}/{mon}/cm{dd}{mon}{yyyy}bhav.csv.zip",
    ]


def _parse_bhav_csv(text: str, symbol: str) -> Optional[Dict[str, Any]]:
    sym = symbol.upper().replace(".NS", "").replace(".BO", "")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return None
    fields = {f.lower().strip(): f for f in reader.fieldnames}

    def col(*names):
        for n in names:
            if n.lower() in fields:
                return fields[n.lower()]
        return None

    sym_col = col("SYMBOL", "symbol")
    deliv_pct_col = col("DELIV_PER", "DELIVERY_PER", "DELIV_PERCENTAGE", "DELIV_PERC")
    deliv_qty_col = col("DELIV_QTY", "DELIVERY_QTY", "DELIV_QUANTITY")
    tq_col = col("TTL_TRD_QNTY", "TOTAL_TRADES", "TOTTRDQTY", "NO_OF_SHRS", "VOLUME")

    if not sym_col:
        return None
    for row in reader:
        if (row.get(sym_col) or "").strip().upper() != sym:
            continue
        pct = None
        if deliv_pct_col and row.get(deliv_pct_col):
            try:
                pct = float(str(row[deliv_pct_col]).replace(",", "").strip())
            except ValueError:
                pct = None
        if pct is None and deliv_qty_col and tq_col:
            try:
                dq = float(str(row[deliv_qty_col]).replace(",", "").strip())
                tq = float(str(row[tq_col]).replace(",", "").strip()) or 1.0
                pct = dq / tq * 100.0
            except ValueError:
                pct = None
        if pct is None:
            continue
        return {
            "symbol": sym,
            "delivery_pct": round(pct, 2),
            "traded_qty": row.get(tq_col) if tq_col else None,
            "delivery_qty": row.get(deliv_qty_col) if deliv_qty_col else None,
            "source": "nse_bhavcopy",
            "raw_row": {k: row.get(k) for k in (sym_col, deliv_pct_col, deliv_qty_col, tq_col) if k},
        }
    return None


def delivery_from_bhavcopy(symbol: str) -> Optional[Dict[str, Any]]:
    """Download recent bhavcopy and extract delivery % for symbol."""
    sym = symbol.upper().replace(".NS", "").replace(".BO", "")
    try:
        with _nse_client() as client:
            for d in _candidate_session_dates(10):
                for url in _bhav_urls_for_date(d):
                    try:
                        r = client.get(url)
                        if r.status_code != 200 or not r.content:
                            continue
                        content_type = (r.headers.get("content-type") or "").lower()
                        text = None
                        if url.endswith(".zip") or "zip" in content_type or r.content[:2] == b"PK":
                            with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
                                name = next((n for n in zf.namelist() if n.lower().endswith(".csv")), None)
                                if not name:
                                    continue
                                text = zf.read(name).decode("utf-8", errors="ignore")
                        else:
                            text = r.text
                        if not text or "SYMBOL" not in text.upper()[:500]:
                            continue
                        parsed = _parse_bhav_csv(text, sym)
                        if parsed:
                            parsed["session_date"] = d.isoformat()
                            parsed["source_url"] = url
                            return parsed
                    except Exception as e:
                        logger.debug("bhav try failed %s %s: %s", d, url, e)
                        continue
    except Exception as e:
        logger.warning("bhavcopy pipeline failed for %s: %s", sym, e)
    return None


def get_delivery(symbol: str) -> Dict[str, Any]:
    """Public resolver: quote → bhavcopy → neutral fallback."""
    sym = symbol.upper().replace(".NS", "").replace(".BO", "")
    for fn in (delivery_from_quote, delivery_from_bhavcopy):
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
