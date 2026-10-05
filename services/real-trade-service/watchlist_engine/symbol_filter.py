"""
watchlist_engine/symbol_filter.py — group164 (item 5, first part)

Two small rules for the watchlist that both ingest (watchlist.py) and the trigger pass (entry.py) use:

1. Index names are not stocks. A hot-pick/news item for "NIFTY", "BANKNIFTY", "SENSEX" ... carries the INDEX
   level (e.g. 24,000) as its price, so the row's catalyst_price is meaningless for any tradable instrument and
   the band check compares a share price against an index level. Such symbols are not inserted, and rows already
   in the table are retired by the trigger pass.
2. One decision per symbol per cycle. A symbol can have several active rows (different catalysts: bulk_block,
   results, board, volume_shock ...), each with its own catalyst price. Evaluated independently, one row could
   be QUEUED while another was SKIPPED for the same stock in the same cycle (PACEDIGITK flapping), and every row
   cost a pass. `split_primary_rows` picks ONE row per symbol (best source tier, then the freshest catalyst, then
   the newest row); the others are left untouched this cycle and take over automatically if the primary becomes
   missed / expired / entered.

Switches (blank/invalid = on; 0/false/no/off = off):
    WATCHLIST_INDEX_FILTER        rule 1
    WATCHLIST_ONE_ROW_PER_SYMBOL  rule 2
    WATCHLIST_INDEX_SYMBOLS       extra comma-separated names to treat as indices
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Iterable

_OFF = ("0", "false", "no", "off")

# Exact names (after normalisation: upper-case, only A-Z/0-9, no .NS/.BO suffix).
_INDEX_NAMES = frozenset({
    "NIFTY", "NIFTY50", "NIFTY100", "NIFTY200", "NIFTY500", "BANKNIFTY", "NIFTYBANK", "FINNIFTY",
    "MIDCPNIFTY", "NIFTYIT", "NIFTYNEXT50", "NIFTYMIDCAP", "NIFTYSMALLCAP", "SENSEX", "SENSEX50",
    "BANKEX", "INDIAVIX", "VIX", "NSEI", "NSEBANK", "BSESN", "CNXIT", "CNXBANK", "CNXFIN",
    "CNXMIDCAP", "CNXSMALLCAP", "CNX100", "CNX200", "CNX500",
})


def _flag(name: str) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    return raw not in _OFF


def index_filter_on() -> bool:
    return _flag("WATCHLIST_INDEX_FILTER")


def one_row_per_symbol_on() -> bool:
    return _flag("WATCHLIST_ONE_ROW_PER_SYMBOL")


def normalize(symbol: str) -> str:
    sym = (symbol or "").strip().upper()
    for suf in (".NS", ".BO"):
        if sym.endswith(suf):
            sym = sym[: -len(suf)]
    return re.sub(r"[^A-Z0-9]", "", sym)


def is_index_symbol(symbol: str) -> bool:
    """True for index names (NIFTY 50, BANKNIFTY, SENSEX, ^NSEI ...). ETFs on an index (NIFTYBEES) are NOT
    handled here — entry.py's ETF guard already covers them. Never raises."""
    try:
        sym = normalize(symbol)
        if not sym:
            return False
        if sym in _INDEX_NAMES:
            return True
        extra = {normalize(x) for x in (os.getenv("WATCHLIST_INDEX_SYMBOLS") or "").split(",") if x.strip()}
        return sym in extra
    except Exception:
        return False


def _ts_key(row) -> float:
    ts = getattr(row, "catalyst_ts", None)
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.timestamp()
    return 0.0


def split_primary_rows(rows: Iterable) -> tuple[list, list]:
    """(primary_rows, duplicate_rows): ONE primary per symbol (suffix spellings count as one stock), in the
    original order of the primaries. Best = lowest source_tier, then newest catalyst_ts, then highest id."""
    rows = list(rows)
    best: dict[str, object] = {}
    for r in rows:
        key = normalize(getattr(r, "symbol", "")) or str(getattr(r, "symbol", ""))
        cur = best.get(key)
        if cur is None or _rank(r) < _rank(cur):
            best[key] = r
    primary_ids = {id(r) for r in best.values()}
    primaries = [r for r in rows if id(r) in primary_ids]
    dups = [r for r in rows if id(r) not in primary_ids]
    return primaries, dups


def _rank(row):
    return (
        int(getattr(row, "source_tier", 3) or 3),
        -_ts_key(row),
        -int(getattr(row, "id", 0) or 0),
    )
