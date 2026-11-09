"""dhan_data/options.py - NIFTY / SENSEX index-option contracts and option chain from Dhan (F&O round 1).

Three jobs, all read-only (nothing here places an order):
  1. parse_option_rows(csv_text)   OPTIDX rows of Dhan's scrip master -> OptionContract list (pure, tested offline).
                                   scrip_master.load() calls ingest() with the text it already downloaded, so there is no second 100 MB download.
  2. normalize_chain(body, ...)    Dhan POST /optionchain response -> one flat, stable JSON shape the fno-options-service reads (pure, tested offline).
  3. fetch_expiries / fetch_chain  the two Dhan calls (POST /optionchain/expirylist, POST /optionchain), 1 request per DHAN_OPTIONCHAIN_MIN_INTERVAL_S
                                   (Dhan allows one unique request per 3 s), with a short cache so several readers share one call.

UNVERIFIED against the live scrip master and live responses (no network where this was written). Column names are matched case-insensitively and both the
compact (SEM_*) and the detailed spellings are accepted; the first thing to run on the VM is  GET /dhan/options/contracts?underlying=NIFTY  and a look at
"contracts" > 0 and "lot_size" (Nifty 65, Sensex 20 as of Nov 2026 - read from the file, never hard-coded).
Never raises into a caller that uses the *_safe helpers; the fetch_* functions raise dhan_data.errors.DhanError subclasses like the rest of the package.
"""
from __future__ import annotations

import csv
import io
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import config

logger = logging.getLogger("dhan-data.options")

UNDERLYINGS: Dict[str, Dict[str, str]] = {
    # name -> yahoo-style index key used by scrip_master.INDEX_ALIASES, exchange, derivatives segment, trading-symbol prefix in the master
    "NIFTY": {"index": "^NSEI", "exchange": "NSE", "segment": "NSE_FNO", "prefix": "NIFTY"},
    "SENSEX": {"index": "^BSESN", "exchange": "BSE", "segment": "BSE_FNO", "prefix": "SENSEX"},
}

_TRADING_SYMBOL_RE = re.compile(r"^(?P<und>[A-Z0-9&]+)-(?P<mon>[A-Za-z]{3})(?P<yr>\d{4})-(?P<strike>[\d.]+)-(?P<ot>CE|PE)$")


@dataclass(frozen=True)
class OptionContract:
    underlying: str          # "NIFTY" | "SENSEX"
    exchange: str            # "NSE" | "BSE"
    segment: str             # "NSE_FNO" | "BSE_FNO" (what Dhan wants as exchangeSegment on orders)
    security_id: int
    symbol: str              # trading symbol as listed, e.g. NIFTY-Nov2026-24500-CE
    expiry: str              # ISO date YYYY-MM-DD
    strike: float
    opt_type: str            # "CE" | "PE"
    lot_size: int
    tick_size: float
    expiry_flag: str         # "W" weekly, "M" monthly, "" unknown

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)


# ── parsing ───────────────────────────────────────────────────────────────────────────────────────────────
def _pick(row: dict, *names: str) -> str:
    for n in names:
        v = row.get(n)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def _parse_expiry(raw: str) -> Optional[str]:
    """'2026-11-17 14:30:00', '2026-11-17', '17-11-2026', '17/11/2026' -> '2026-11-17' (None when unreadable)."""
    s = (raw or "").strip()
    if not s:
        return None
    s = s.split(" ")[0].split("T")[0]
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _to_float(raw: str) -> Optional[float]:
    try:
        v = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def _row_to_contract(row: dict) -> Optional[OptionContract]:
    inst = _pick(row, "SEM_INSTRUMENT_NAME", "INSTRUMENT").upper()
    if inst != "OPTIDX":
        return None
    exch = _pick(row, "SEM_EXM_EXCH_ID", "EXCH_ID").upper()
    sym = _pick(row, "SEM_TRADING_SYMBOL", "SYMBOL_NAME")          # kept as listed; matching is case-insensitive
    und = _pick(row, "UNDERLYING_SYMBOL").upper()
    m = _TRADING_SYMBOL_RE.match(sym.upper())
    if not und:
        und = m.group("und") if m else (sym.upper().split("-")[0] if sym else "")
    if und not in UNDERLYINGS:
        return None
    spec = UNDERLYINGS[und]
    if exch and exch != spec["exchange"]:
        return None
    try:
        sid = int(float(_pick(row, "SEM_SMST_SECURITY_ID", "SECURITY_ID")))
    except ValueError:
        return None
    opt = _pick(row, "SEM_OPTION_TYPE", "OPTION_TYPE").upper()
    if opt not in ("CE", "PE"):
        opt = m.group("ot") if m else ""
    if opt not in ("CE", "PE"):
        return None
    expiry = _parse_expiry(_pick(row, "SEM_EXPIRY_DATE", "SM_EXPIRY_DATE"))
    strike = _to_float(_pick(row, "SEM_STRIKE_PRICE", "STRIKE_PRICE"))
    if strike is None and m:
        strike = _to_float(m.group("strike"))
    lot = _to_float(_pick(row, "SEM_LOT_UNITS", "LOT_SIZE"))
    if not expiry or strike is None or strike <= 0 or not lot or lot <= 0:
        return None
    tick = _to_float(_pick(row, "SEM_TICK_SIZE", "TICK_SIZE")) or 0.05
    if tick >= 1.0:                    # some masters give the tick in paise (5.00 = Rs 0.05); no index-option tick is Rs 1 or more
        tick = tick / 100.0
    flag = _pick(row, "SEM_EXPIRY_FLAG", "EXPIRY_FLAG").upper()[:1]
    return OptionContract(underlying=und, exchange=spec["exchange"], segment=spec["segment"], security_id=sid, symbol=sym, expiry=expiry,
                          strike=float(strike), opt_type=opt, lot_size=int(lot), tick_size=float(tick), expiry_flag=flag if flag in ("W", "M") else "")


def parse_option_rows(text: str) -> List[OptionContract]:
    """All NIFTY / SENSEX OPTIDX contracts in the CSV text. Rows that do not parse are skipped, never raised."""
    out: List[OptionContract] = []
    seen: set = set()
    reader = csv.DictReader(io.StringIO(text or ""))
    for raw in reader:
        row = {(k or "").strip().upper(): v for k, v in raw.items()}
        c = _row_to_contract(row)
        if c is None:
            continue
        key = (c.underlying, c.expiry, c.strike, c.opt_type)
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


# ── in-memory contract store ───────────────────────────────────────────────────────────────────────────────
_lock = threading.Lock()
_contracts: Dict[Tuple[str, str, float, str], OptionContract] = {}
_loaded_at: float = 0.0


def ingest(text: str) -> int:
    """Replace the contract map from scrip-master CSV text. Returns the count; keeps the old map when nothing parsed. Never raises."""
    global _contracts, _loaded_at
    try:
        rows = parse_option_rows(text)
    except Exception as e:  # noqa: BLE001
        logger.warning("option contracts not parsed: %s", type(e).__name__)
        return 0
    if not rows:
        return 0
    fresh = {(c.underlying, c.expiry, c.strike, c.opt_type): c for c in rows}
    with _lock:
        _contracts, _loaded_at = fresh, time.time()
    logger.info("dhan option contracts loaded: %d (NIFTY %d, SENSEX %d)", len(fresh),
                sum(1 for c in rows if c.underlying == "NIFTY"), sum(1 for c in rows if c.underlying == "SENSEX"))
    return len(fresh)


def _set_for_tests(rows: Iterable[OptionContract]) -> None:
    global _contracts, _loaded_at
    with _lock:
        _contracts = {(c.underlying, c.expiry, c.strike, c.opt_type): c for c in rows}
        _loaded_at = time.time()


def list_expiries(underlying: str, today: Optional[date] = None) -> List[str]:
    """Sorted future-or-today expiries known from the scrip master for one underlying."""
    u = (underlying or "").upper()
    t = (today or date.today()).isoformat()
    with _lock:
        ex = {k[1] for k in _contracts if k[0] == u and k[1] >= t}
    return sorted(ex)


def contracts_for(underlying: str, expiry: Optional[str] = None) -> List[OptionContract]:
    u = (underlying or "").upper()
    with _lock:
        rows = [c for k, c in _contracts.items() if k[0] == u and (expiry is None or k[1] == expiry)]
    return sorted(rows, key=lambda c: (c.expiry, c.strike, c.opt_type))


def find_contract(underlying: str, expiry: str, strike: float, opt_type: str) -> Optional[OptionContract]:
    with _lock:
        return _contracts.get(((underlying or "").upper(), expiry, float(strike), (opt_type or "").upper()))


def status() -> Dict[str, Any]:
    with _lock:
        n = len(_contracts)
        by = {u: sum(1 for k in _contracts if k[0] == u) for u in UNDERLYINGS}
        lots = {u: sorted({c.lot_size for k, c in _contracts.items() if k[0] == u}) for u in UNDERLYINGS}
        age = round(time.time() - _loaded_at, 0) if _loaded_at else None
    return {"contracts": n, "by_underlying": by, "lot_sizes": lots, "age_s": age}


# ── chain normalisation ────────────────────────────────────────────────────────────────────────────────────
def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _side(d: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(d, dict):
        return None
    g = d.get("greeks") if isinstance(d.get("greeks"), dict) else {}
    return {
        "security_id": int(d["security_id"]) if _num(d.get("security_id")) is not None else None,
        "ltp": _num(d.get("last_price")),
        "iv": _num(d.get("implied_volatility")),
        "delta": _num(g.get("delta")), "gamma": _num(g.get("gamma")), "theta": _num(g.get("theta")), "vega": _num(g.get("vega")),
        "oi": _num(d.get("oi")), "prev_oi": _num(d.get("previous_oi")),
        "volume": _num(d.get("volume")), "prev_volume": _num(d.get("previous_volume")),
        "prev_close": _num(d.get("previous_close_price")), "avg_price": _num(d.get("average_price")),
        "bid": _num(d.get("top_bid_price")), "bid_qty": _num(d.get("top_bid_quantity")),
        "ask": _num(d.get("top_ask_price")), "ask_qty": _num(d.get("top_ask_quantity")),
    }


def normalize_chain(body: Any, underlying: str, expiry: str) -> Dict[str, Any]:
    """Dhan /optionchain body -> {"underlying","expiry","spot","strikes":[{"strike","ce","pe"}],"fetched_at"}. Strikes are sorted; an unreadable
    strike key is skipped. Returns "strikes": [] (never raises) when the body has no usable chain."""
    data = body.get("data") if isinstance(body, dict) else None
    data = data if isinstance(data, dict) else {}
    oc = data.get("oc") if isinstance(data.get("oc"), dict) else {}
    rows: List[Dict[str, Any]] = []
    for k, v in oc.items():
        strike = _num(k)
        if strike is None or not isinstance(v, dict):
            continue
        rows.append({"strike": strike, "ce": _side(v.get("ce")), "pe": _side(v.get("pe"))})
    rows.sort(key=lambda r: r["strike"])
    return {"underlying": (underlying or "").upper(), "expiry": expiry, "spot": _num(data.get("last_price")),
            "strikes": rows, "fetched_at": time.time()}


# ── the two Dhan calls ─────────────────────────────────────────────────────────────────────────────────────
_cache_lock = threading.Lock()
_cache: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}
_limiter = None


def _chain_limiter():
    global _limiter
    from . import client
    if _limiter is None:
        _limiter = client.Limiter(lambda: config.env_float("DHAN_OPTIONCHAIN_MIN_INTERVAL_S", 3.2, 1.0))
    return _limiter


def _underlying_ident(underlying: str) -> Tuple[str, int]:
    from . import scrip_master
    from .errors import DhanNoDataError
    spec = UNDERLYINGS.get((underlying or "").upper())
    if spec is None:
        raise DhanNoDataError(f"{underlying}: only NIFTY and SENSEX are supported")
    ident = scrip_master.security_id(spec["index"])
    if not ident:
        raise DhanNoDataError(f"{underlying}: index id not in the Dhan scrip master")
    return ident


def fetch_expiries(underlying: str) -> List[str]:
    """Active expiry dates (YYYY-MM-DD, sorted) from POST /optionchain/expirylist. Raises a DhanError subclass."""
    from . import client
    seg, sid = _underlying_ident(underlying)
    try:
        body = client.post("optionchain/expirylist", {"UnderlyingScrip": int(sid), "UnderlyingSeg": seg}, limiter=_chain_limiter())
        client.note_success()
    except Exception as e:  # noqa: BLE001
        client.note_failure(e)
        raise
    items = body.get("data") if isinstance(body, dict) else None
    out = sorted({d for d in (_parse_expiry(str(x)) for x in (items or [])) if d})
    return out


def fetch_chain(underlying: str, expiry: str, max_age_s: Optional[float] = None) -> Dict[str, Any]:
    """Normalised chain for one expiry. Cached for max_age_s (default DHAN_OPTIONCHAIN_CACHE_S, 3 s) so several readers share a call."""
    from . import client
    u = (underlying or "").upper()
    ttl = config.env_float("DHAN_OPTIONCHAIN_CACHE_S", 3.0, 0.0) if max_age_s is None else float(max_age_s)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get((u, expiry))
        if hit and now - hit[0] <= ttl:
            return hit[1]
    seg, sid = _underlying_ident(u)
    try:
        body = client.post("optionchain", {"UnderlyingScrip": int(sid), "UnderlyingSeg": seg, "Expiry": expiry}, limiter=_chain_limiter())
        client.note_success()
    except Exception as e:  # noqa: BLE001
        client.note_failure(e)
        raise
    norm = normalize_chain(body, u, expiry)
    with _cache_lock:
        _cache[(u, expiry)] = (time.monotonic(), norm)
    return norm
