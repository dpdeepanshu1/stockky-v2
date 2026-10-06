"""group 188 (item 9): recognise ETFs / index funds / liquid and gilt funds by NAME.

NSE lists them in the cash segment as plain EQ series, so AngelOne's scrip master (the source of the whole-market
movers sweep and of position-stocks' WebSocket subscription) contains them. They are not single-stock picks and the
illiquid ones only add feed load and "possibly delisted" Yahoo misses (BANKBETA, MONQ50, LIQUIDPLUS, SILVERADD,
GSEC10YEAR, AONESILVER, GROWWMETAL ...).

There is no instrument-type field to read, so this is a conservative name test, never a hand-maintained "all ETFs"
list. Extend it with ETF_FUND_EXTRA_SYMBOLS=SYM1,SYM2 when one slips through; ETF_FUND_FILTER=0 turns it off.
NOTE: an identical copy lives in position-stocks-service/feed/instrument_filter.py and the api-gateway has the same
patterns in main.py (_ETF_INDEX_FUND_SYMBOL_RE); each service's test pins the same symbol table.
"""
from __future__ import annotations

import os
import re

_PATTERN = re.compile(
    r"(?:ETF|BEES)$"                    # *ETF, *BEES (NIFTYBEES, GOLDBEES, ALPL30IETF, SBILIQETF)
    r"|^SETF[A-Z0-9]"                   # SBI's SETF* series
    r"|^(?:NIFTY|BANKNIFTY)\d*$"        # index-style tickers
    r"|LIQUID"                          # LIQUIDBEES / LIQUIDSBI / LIQUIDPLUS / HDFCLIQUID
    r"|GSEC"                            # GSEC10YEAR and other gilt funds
    r"|^(?:SILVER|GOLD)(?:ADD|CASE)$"   # SILVERADD, GOLDCASE
)

# Names that carry no ETF-looking word. Seen in logs or already treated as ETFs by real-trade-service.
_KNOWN = frozenset({
    "BANKBETA", "MONQ50", "AONESILVER", "GROWWMETAL",
    "MASPTOP50", "MAFANG", "MON100", "MAHKTECH", "MOM100", "MOM50", "MIDSMALL", "N100",
})


def _clean(symbol) -> str:
    return str(symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()


def is_etf_or_fund(symbol) -> bool:
    """True when `symbol` looks like an ETF / index fund / liquid or gilt fund. Never raises."""
    try:
        if (os.getenv("ETF_FUND_FILTER") or "").strip() in ("0", "false", "False"):
            return False
        s = _clean(symbol)
        if not s:
            return False
        if s in _KNOWN or _PATTERN.search(s):
            return True
        extra = {x.strip().upper() for x in (os.getenv("ETF_FUND_EXTRA_SYMBOLS") or "").split(",") if x.strip()}
        return s in extra
    except Exception:  # noqa: BLE001
        return False


def drop_etfs(symbol_map: dict) -> tuple:
    """(map without ETF/fund names, number dropped). Input is not modified."""
    kept = {s: t for s, t in symbol_map.items() if not is_etf_or_fund(s)}
    return kept, len(symbol_map) - len(kept)
