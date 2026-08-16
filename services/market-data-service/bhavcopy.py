"""
NSE Bhavcopy / delivery % helpers — free tier only.

Sources (in order):
1. NSE quote-equity securityWiseDP (live intraday / last session)
2. NSE archives bhavcopy ZIP/CSV for a recent session date (multiple URL patterns)
3. NSE sec_bhavdata_full daily CSV (delivery columns)
4. Neutral fallback with explicit source tag (never invents precision)
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
    c = httpx.Client(timeout=25, headers=NSE_HEADERS, follow_redirects=True)
    try:
        c.get("https://www.nseindia.com")
        c.get("https://www.nseindia.com/market-data/securities-available-for-trading")
        c.get("https://www.nseindia.com/all-reports")
    except Exception:
        pass
    return c


def delivery_from_quote(symbol: str) -> Optional[Dict[str, Any]]:
    """Live delivery % from NSE quote-equity (securityWiseDP)."""
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


def _candidate_session_dates(n: int = 12) -> List:
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
    """Possible NSE archive URL patterns for a date (NSE changes paths periodically)."""
    dd = d.strftime("%d")
    mon = d.strftime("%b").upper()
    yyyy = d.strftime("%Y")
    ddmmyyyy = d.strftime("%d%m%Y")
    mm = d.strftime("%m")
    mon_cap = d.strftime("%b").capitalize()
    return [
        f"https://nsearchives.nseindia.com/content/historical/EQUITIES/{yyyy}/{mon}/cm{dd}{mon}{yyyy}bhav.csv.zip",
        f"https://archives.nseindia.com/content/historical/EQUITIES/{yyyy}/{mon}/cm{dd}{mon}{yyyy}bhav.csv.zip",
        f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv",
        f"https://archives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv",
        f"https://www.nseindia.com/content/historical/EQUITIES/{yyyy}/{mon}/cm{dd}{mon}{yyyy}bhav.csv.zip",
        f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{yyyy}{mm}{dd}_F_0000.csv.zip",
        f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{yyyy}{mm}{dd}_F_0000.csv",
        f"https://nsearchives.nseindia.com/content/Equities/sec_bhavdata_full_{ddmmyyyy}.csv",
        f"https://www1.nseindia.com/content/historical/EQUITIES/{yyyy}/{mon}/cm{dd}{mon}{yyyy}bhav.csv.zip",
        f"https://www.nseindia.com/api/reports?archives=%5B%7B%22name%22%3A%22CM%20-%20Bhavcopy(csv)%22%2C%22type%22%3A%22archives%22%2C%22category%22%3A%22capital-market%22%2C%22section%22%3A%22equities%22%7D%5D&date={dd}-{mon_cap}-{yyyy}&type=equities&mode=single",
    ]


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
    """Download recent official bhavcopy / sec_bhavdata and extract delivery %."""
    sym = symbol.upper().replace(".NS", "").replace(".BO", "")
    try:
        with _nse_client() as client:
            for d in _candidate_session_dates(12):
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
                                    name = next(
                                        (n for n in zf.namelist() if n.lower().endswith(".csv")),
                                        None,
                                    )
                                    if not name:
                                        continue
                                    text = zf.read(name).decode("utf-8", errors="ignore")
                            except zipfile.BadZipFile:
                                continue
                        else:
                            # JSON report wrappers sometimes return download links — skip non-CSV
                            if "application/json" in content_type:
                                continue
                            text = r.text
                        if not text:
                            continue
                        head = text[:800].upper()
                        if "SYMBOL" not in head and "TCKRSYMB" not in head and "SECURITY" not in head:
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


def delivery_from_nse_cm_series(symbol: str) -> Optional[Dict[str, Any]]:
    """Try NSE equity master / series-wise snapshot if available via public API."""
    sym = symbol.upper().replace(".NS", "").replace(".BO", "")
    try:
        with _nse_client() as client:
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
