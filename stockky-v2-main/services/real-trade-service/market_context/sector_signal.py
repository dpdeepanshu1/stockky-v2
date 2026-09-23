"""
market_context/sector_signal.py — overnight US-sector signal (2026-09-11,
session23, user request): "we can take some idea from the US stock market
sector-wise, or on some point based on that, predict something early."

WHAT THIS IS: a small, capped ranking nudge applied at PREPICK (~09:00 IST),
never a gate. By 09:00 IST the US session has been closed for hours (NYSE/
NASDAQ close ~02:00-02:30 IST during EDT, ~03:00-03:30 IST during EST), so
"how did each US sector do overnight" is a clean, already-settled input by
the time Indian pre-market planning happens — not a live/racing feed.

WHAT THIS IS NOT: a forecast, a strategy, or a substitute for the existing
candidate/conviction pipeline. It only answers "did the closest US sector
proxy for this stock's NSE sector close up or down overnight, and by how
much" and turns that into a bonus/penalty of at most +/-config.
US_SECTOR_BONUS_CAP points, the same "nudge Gate 6's ranking, change
nothing about gates 1-5" posture as config.ENTRY_OVERNIGHT_PRIORITY_BONUS.

COVERAGE CAVEAT: NSE_SECTOR_MAP below only covers the major NSE sectoral-
index constituents (Nifty Bank/IT/Pharma/Auto/FMCG/Metal/Energy/Realty —
the most liquid, most frequently-picked names in this system's own
candidate universe). A symbol not in the map gets bonus 0.0 — never a
penalty for being unmapped. This is a coverage gap, not a bug: the
`real_data/stockky_data` static feed's own `sector` column is mostly NULL
today, so a full GICS-accurate mapping for every symbol in symbols.json
would need real sector reference data this codebase doesn't have yet.
Extending NSE_SECTOR_MAP over time (or wiring a real sector data source)
is the natural next step, not a rewrite of this module.

Entirely additive and off by default (config.US_SECTOR_SIGNAL_ENABLED).
Every failure mode here is best-effort / non-fatal, matching every other
resilience-cache-backed feature in this codebase — a fetch failure just
means bonus 0.0 for everyone that day, never a blocked prepick.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import config

logger = logging.getLogger("real-trade-sector-signal")

# ── NSE sector -> closest US sector ETF ─────────────────────────────────────
# SPDR Select Sector ETFs — liquid, single-ticker-per-sector, exactly what
# market-data-service already fetches via yfinance elsewhere in this repo.
SECTOR_ETF_MAP: dict[str, str] = {
    "IT": "XLK",
    "BANK": "XLF",
    "FINANCIAL_SERVICES": "XLF",
    "PHARMA": "XLV",
    "HEALTHCARE": "XLV",
    "AUTO": "XLY",
    "CONSUMER_DISCRETIONARY": "XLY",
    "FMCG": "XLP",
    "CONSUMER_STAPLES": "XLP",
    "METAL": "XLB",
    "MATERIALS": "XLB",
    "ENERGY": "XLE",
    "OIL_GAS": "XLE",
    "REALTY": "XLRE",
    "CAPITAL_GOODS": "XLI",
    "INDUSTRIALS": "XLI",
    "POWER": "XLU",
    "UTILITIES": "XLU",
    "TELECOM": "XLC",
    "MEDIA": "XLC",
}

# ── Static NSE symbol -> sector map ─────────────────────────────────────────
# Deliberately partial (see module docstring) — major sectoral-index names
# only. An unmapped symbol simply gets no bonus/penalty.
NSE_SECTOR_MAP: dict[str, str] = {
    # IT
    "TCS": "IT", "INFY": "IT", "WIPRO": "IT", "HCLTECH": "IT", "TECHM": "IT",
    "LTIM": "IT", "MPHASIS": "IT", "PERSISTENT": "IT", "COFORGE": "IT",
    "MASTEK": "IT", "ZENSARTECH": "IT", "CYIENT": "IT", "OFSS": "IT",
    "LATENTVIEW": "IT", "HAPPSTMNDS": "IT", "TANLA": "IT",
    # Bank / Financial services
    "HDFCBANK": "BANK", "ICICIBANK": "BANK", "SBIN": "BANK", "KOTAKBANK": "BANK",
    "AXISBANK": "BANK", "INDUSINDBK": "BANK", "BANDHANBNK": "BANK",
    "IDFCFIRSTB": "BANK", "PNB": "BANK", "CANBK": "BANK", "UNIONBANK": "BANK",
    "EQUITASBNK": "BANK", "BAJFINANCE": "FINANCIAL_SERVICES",
    "BAJAJFINSV": "FINANCIAL_SERVICES", "CHOLAFIN": "FINANCIAL_SERVICES",
    "SHRIRAMFIN": "FINANCIAL_SERVICES", "MUTHOOTFIN": "FINANCIAL_SERVICES",
    "MANAPPURAM": "FINANCIAL_SERVICES", "LICHSGFIN": "FINANCIAL_SERVICES",
    "SBILIFE": "FINANCIAL_SERVICES", "HDFCLIFE": "FINANCIAL_SERVICES",
    "ICICIPRU": "FINANCIAL_SERVICES", "ICICIGI": "FINANCIAL_SERVICES",
    "MOTILALOFS": "FINANCIAL_SERVICES", "CDSL": "FINANCIAL_SERVICES",
    "MCX": "FINANCIAL_SERVICES", "IIFL": "FINANCIAL_SERVICES",
    # Pharma / Healthcare
    "SUNPHARMA": "PHARMA", "DRREDDY": "PHARMA", "CIPLA": "PHARMA",
    "DIVISLAB": "PHARMA", "AUROPHARMA": "PHARMA", "LUPIN": "PHARMA",
    "TORNTPHARM": "PHARMA", "ALKEM": "PHARMA", "GLENMARK": "PHARMA",
    "BIOCON": "PHARMA", "IPCALAB": "PHARMA", "APOLLOHOSP": "HEALTHCARE",
    "METROPOLIS": "HEALTHCARE", "KIMS": "HEALTHCARE",
    # Auto
    "MARUTI": "AUTO", "TATAMOTORS": "AUTO", "M&M": "AUTO", "EICHERMOT": "AUTO",
    "BAJAJ-AUTO": "AUTO", "HEROMOTOCO": "AUTO", "TVSMOTOR": "AUTO",
    "MOTHERSON": "AUTO", "BOSCHLTD": "AUTO", "EXIDEIND": "AUTO",
    "APOLLOTYRE": "AUTO", "MRF": "AUTO", "ESCORTS": "AUTO",
    # FMCG / Consumer staples
    "HINDUNILVR": "FMCG", "ITC": "FMCG", "NESTLEIND": "FMCG", "BRITANNIA": "FMCG",
    "DABUR": "FMCG", "MARICO": "FMCG", "GODREJCP": "FMCG", "TATACONSUM": "FMCG",
    "COLPAL": "FMCG", "UBL": "FMCG", "VBL": "FMCG", "EMAMILTD": "FMCG",
    # Metal / Materials
    "TATASTEEL": "METAL", "JSWSTEEL": "METAL", "HINDALCO": "METAL",
    "VEDL": "METAL", "SAIL": "METAL", "NMDC": "METAL", "HINDZINC": "METAL",
    "JSWENERGY": "POWER", "GRASIM": "MATERIALS", "ULTRACEMCO": "MATERIALS",
    "SHREECEM": "MATERIALS", "AMBUJACEM": "MATERIALS", "DALBHARAT": "MATERIALS",
    # Energy
    "RELIANCE": "ENERGY", "ONGC": "ENERGY", "BPCL": "ENERGY", "IGL": "ENERGY",
    "GAIL": "ENERGY", "PETRONET": "ENERGY", "COALINDIA": "ENERGY",
    # Realty
    "DLF": "REALTY", "GODREJPROP": "REALTY", "OBEROIRLTY": "REALTY",
    "PHOENIXLTD": "REALTY", "SOBHA": "REALTY", "IBREALEST": "REALTY",
    # Capital goods / industrials
    "LT": "CAPITAL_GOODS", "SIEMENS": "CAPITAL_GOODS", "ABB": "CAPITAL_GOODS",
    "BHARTIARTL": "TELECOM", "INDUSTOWER": "TELECOM", "IDEA": "TELECOM",
    "NTPC": "POWER", "POWERGRID": "POWER", "TATAPOWER": "POWER",
    "TORNTPOWER": "POWER",
}

_SNAPSHOT_KEY = "us_sector_signal"


def _fetch_us_sector_returns() -> dict[str, float]:
    """Best-effort: pulls each ETF's latest daily % change via yfinance.
    Returns {} on any failure — never raises. This is a small, once-a-day
    batch (len(SECTOR_ETF_MAP) unique tickers), never called on the hot
    intraday path."""
    tickers = sorted(set(SECTOR_ETF_MAP.values()))
    try:
        import yfinance as yf
    except Exception:
        logger.warning("sector_signal: yfinance not available, skipping US sector fetch")
        return {}

    out: dict[str, float] = {}
    try:
        data = yf.download(
            tickers=" ".join(tickers), period="5d", interval="1d",
            group_by="ticker", progress=False, threads=True,
        )
    except Exception as e:
        logger.warning("sector_signal: yfinance bulk download failed: %s", e)
        return {}

    for t in tickers:
        try:
            closes = data[t]["Close"].dropna() if len(tickers) > 1 else data["Close"].dropna()
            if len(closes) < 2:
                continue
            prev, last = float(closes.iloc[-2]), float(closes.iloc[-1])
            if prev <= 0:
                continue
            out[t] = round((last - prev) / prev * 100.0, 3)
        except Exception:
            continue
    return out


def refresh_us_sector_snapshot(db) -> dict[str, float]:
    """Fetch-or-reuse today's US sector returns, cached once per IST trading
    date via the resilience cache (same idiom as overnight_priority). Safe
    to call from _prepick every morning — only re-fetches if today's
    snapshot isn't already there."""
    from resilience.local_cache import load_snapshot, save_snapshot
    from tz_utils import ist_today_str

    today = ist_today_str()
    try:
        snap = load_snapshot(db, _SNAPSHOT_KEY)
    except Exception:
        snap = None

    if snap and snap.get("trading_date") == today and snap.get("returns"):
        return snap["returns"]

    returns = _fetch_us_sector_returns()
    try:
        save_snapshot(db, _SNAPSHOT_KEY, {
            "trading_date": today,
            "returns": returns,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        logger.exception("sector_signal: failed to save today's snapshot (non-fatal)")
    return returns


def sector_bonus_for_symbol(symbol: str, sector_returns: dict[str, float]) -> float:
    """Maps a symbol -> its NSE sector -> the matching US ETF's overnight
    % return -> a capped +/-config.US_SECTOR_BONUS_CAP bonus, linearly
    scaled up to config.US_SECTOR_BONUS_FULL_SCALE_PCT. Returns 0.0 for any
    unmapped symbol, missing data, or if the feature is disabled — never
    raises."""
    if not config.US_SECTOR_SIGNAL_ENABLED or not sector_returns:
        return 0.0
    sector = NSE_SECTOR_MAP.get(symbol)
    if not sector:
        return 0.0
    etf = SECTOR_ETF_MAP.get(sector)
    if not etf:
        return 0.0
    pct = sector_returns.get(etf)
    if pct is None:
        return 0.0
    scale = max(config.US_SECTOR_BONUS_FULL_SCALE_PCT, 0.01)
    bonus = (pct / scale) * config.US_SECTOR_BONUS_CAP
    return round(max(-config.US_SECTOR_BONUS_CAP, min(config.US_SECTOR_BONUS_CAP, bonus)), 2)
