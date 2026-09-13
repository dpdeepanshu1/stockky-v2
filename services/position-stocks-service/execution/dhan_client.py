"""
execution/dhan_client.py — THE ONLY module in position-stocks-service
allowed to hold a decrypted Dhan credential or call Dhan's API.

DUPLICATED from real-trade-service/execution/dhan_client.py on purpose
(see config.py module docstring for the isolation rationale) — the
tick-rounding, security-id cache, and _clean_security_id logic below are
copied verbatim because they encode real, hard-won fixes (see the
2026-09-07 comments) that this service needs exactly as much as
real-trade-service does. Divergence from here on is additive only
(place_super_order/modify/cancel), never a "simplified" rewrite of the
proven parts.

Credentials: read via auth/dhan_credentials_ro.py — this service NEVER
saves or refreshes a Dhan token itself. See that module's docstring.

Two defense-in-depth layers, same convention as real-trade-service:
  1. Every mutating call re-checks `is_armed` itself.
  2. Read-only calls (funds, positions, super-order list) are NOT gated by
     arming.
"""
from __future__ import annotations

import logging
import re
import time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from auth import dhan_credentials_ro

logger = logging.getLogger("position-stocks-dhan-client")

# ── Security master cache (symbol -> Dhan security_id) ─────────────────────
_SECURITY_CACHE_TTL_SECONDS = 24 * 60 * 60
_security_cache: dict[str, str] = {}
_security_cache_loaded_at: float = 0.0
_security_collision_count = 0

NSE_EQ_SEGMENT = "NSE_EQ"
TICK_SIZE = 0.05
_TICK_SIZE_DEC = Decimal("0.05")


def round_to_tick(price: float, tick_size: float = TICK_SIZE) -> float:
    """Round `price` to the nearest valid exchange tick, in Decimal (not
    binary float) end-to-end — see real-trade-service's dhan_client.py
    2026-09-07 fix comment for why binary float rounding can still fail
    Dhan's tick-multiple check on a price that looks perfectly clean."""
    try:
        price_f = float(price)
        if price_f <= 0 or tick_size <= 0:
            return price_f
        price_dec = Decimal(str(price_f))
        tick_dec = Decimal(str(tick_size))
        ticks = (price_dec / tick_dec).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        result = (ticks * tick_dec).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return float(result)
    except (TypeError, ValueError, InvalidOperation):
        return price


def is_valid_tick_price(price: float, tick_size: float = TICK_SIZE) -> bool:
    try:
        price_dec = Decimal(str(float(price)))
        tick_dec = Decimal(str(tick_size))
        if price_dec <= 0 or tick_dec <= 0:
            return True
        remainder = (price_dec / tick_dec) % 1
        return remainder == 0
    except (TypeError, ValueError, InvalidOperation):
        return False


class SecurityNotResolvedError(Exception):
    pass


class DhanNotConnectedError(Exception):
    pass


class DhanNotArmedError(Exception):
    pass


def _get_sdk_client(db: Session):
    """Build a fresh dhanhq 2.0.2 SDK client from real-trade-service's
    stored (and owned) credentials, read via dhan_credentials_ro."""
    creds = dhan_credentials_ro.get_decrypted_credentials(db)
    if creds is None:
        raise DhanNotConnectedError(
            "No Dhan credentials found — connect Dhan in real-trade-service "
            "first (position-stocks-service shares the same account/credentials "
            "and never manages its own)."
        )
    client_id, access_token = creds
    try:
        from dhanhq import dhanhq  # noqa: PLC0415
    except ImportError as e:
        raise RuntimeError("dhanhq SDK not installed — check requirements.txt") from e
    return dhanhq(client_id, access_token)


def _extract_data(response: dict, key: str = "data"):
    if not isinstance(response, dict):
        return response
    if response.get("status") == "failure":
        remarks = response.get("remarks", "")
        if isinstance(remarks, dict):
            msg = remarks.get("error_message", str(remarks))
        else:
            msg = str(remarks)
        raise RuntimeError(f"Dhan API error: {msg}")
    return response.get(key)


_TRAILING_FLOAT_SUFFIX_RE = re.compile(r"^(\d+)\.0+$")


def _clean_security_id(raw: str) -> str:
    raw = (raw or "").strip()
    m = _TRAILING_FLOAT_SUFFIX_RE.match(raw)
    return m.group(1) if m else raw


def _add_security(fresh: dict[str, str], sym: str, sec_id_raw: str) -> None:
    global _security_collision_count
    sec_id = _clean_security_id(sec_id_raw)
    if not sym or not sec_id:
        return
    if sym in fresh and fresh[sym] != sec_id:
        _security_collision_count += 1
        logger.warning(
            "Security master has 2+ distinct security_ids for symbol '%s' "
            "(keeping first seen: %s, ignoring: %s).", sym, fresh[sym], sec_id,
        )
        return
    fresh[sym] = sec_id


def _load_security_cache(db: Session) -> None:
    """Same load logic as real-trade-service's (SDK compact CSV, CSV-download
    fallback), duplicated so this service's cache lives in its own process
    memory and refresh cycle."""
    global _security_cache, _security_cache_loaded_at, _security_collision_count
    client = _get_sdk_client(db)
    _security_collision_count = 0

    fresh: dict[str, str] = {}
    try:
        df = client.fetch_security_list(mode="compact")
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                try:
                    exch = str(row.get("SEM_EXM_EXCH_ID", row.get("EXCH_ID", ""))).strip()
                    instrument = str(row.get("SEM_INSTRUMENT_NAME", row.get("INSTRUMENT_NAME", ""))).strip()
                    series = str(row.get("SEM_SERIES", row.get("SERIES", ""))).strip()
                    sym = str(row.get("SEM_TRADING_SYMBOL", row.get("TRADING_SYMBOL", ""))).strip().upper()
                    sec_id_raw = str(row.get("SEM_SMST_SECURITY_ID", row.get("SECURITY_ID", ""))).strip()
                    if exch == "NSE" and instrument == "EQUITY" and series in ("EQ", ""):
                        _add_security(fresh, sym, sec_id_raw)
                except Exception:
                    continue
    except Exception as e:
        logger.warning("fetch_security_list failed (%s) — falling back to direct CSV download", e)
        creds = dhan_credentials_ro.get_decrypted_credentials(db)
        if creds is None:
            raise DhanNotConnectedError("No Dhan credentials stored.")
        _client_id, access_token = creds
        try:
            resp = httpx.get(
                "https://images.dhan.co/api-data/api-scrip-master.csv",
                headers={"access-token": access_token},
                timeout=30.0,
            )
            resp.raise_for_status()
            import csv, io
            reader = csv.DictReader(io.StringIO(resp.text))
            for row in reader:
                try:
                    if row.get("SEM_EXM_EXCH_ID") != "NSE":
                        continue
                    if row.get("SEM_INSTRUMENT_NAME") != "EQUITY":
                        continue
                    if row.get("SEM_SERIES") not in (None, "", "EQ"):
                        continue
                    sym = (row.get("SEM_TRADING_SYMBOL") or "").strip().upper()
                    sec_id_raw = (row.get("SEM_SMST_SECURITY_ID") or "").strip()
                    _add_security(fresh, sym, sec_id_raw)
                except Exception:
                    continue
        except Exception as e2:
            logger.error("Security list download also failed: %s", e2)

    if not fresh:
        logger.error("Dhan instrument list fetch returned 0 usable rows — keeping existing cache.")
        return

    if _security_collision_count:
        logger.warning(
            "position-stocks: %d symbol(s) had 2+ distinct security_ids in this load.",
            _security_collision_count,
        )

    _security_cache = fresh
    _security_cache_loaded_at = time.time()
    logger.info("position-stocks: loaded %d NSE equity security IDs from Dhan", len(fresh))


def get_security_id(db: Session, symbol: str) -> str:
    now = time.time()
    if not _security_cache or (now - _security_cache_loaded_at) > _SECURITY_CACHE_TTL_SECONDS:
        _load_security_cache(db)
    sym = symbol.strip().upper().replace(".NS", "").replace(".BO", "")
    sec_id = _security_cache.get(sym)
    if sec_id is None:
        raise SecurityNotResolvedError(f"No Dhan NSE_EQ security_id found for '{sym}'.")
    return _clean_security_id(sec_id)


def get_funds(db: Session) -> dict:
    """Read-only — no arm check. Used by capital/ledger.py to sync the
    scalp pool's allocation against the account's real available balance."""
    client = _get_sdk_client(db)
    resp = client.get_fund_limits()
    data = _extract_data(resp)
    return data if isinstance(data, dict) else {}


def get_positions(db: Session) -> list:
    client = _get_sdk_client(db)
    resp = client.get_positions()
    try:
        data = _extract_data(resp)
    except RuntimeError as e:
        if "no positions" in str(e).lower():
            return []
        raise
    return data if isinstance(data, list) else []


# ── Order placement (plain — fallback if USE_SUPER_ORDER=false) ──────────
def place_order(
    db: Session,
    *,
    is_armed: bool,
    security_id: str,
    exchange_segment: str,
    transaction_type: str,
    quantity: int,
    order_type: str,
    price: float,
    product_type: str = "INTRA",
    validity: str = "DAY",
    tag: Optional[str] = None,
) -> dict:
    if not is_armed:
        raise DhanNotArmedError("position-stocks-service is not armed — refusing to place order.")
    client = _get_sdk_client(db)

    if order_type == "LIMIT" and price:
        tick_safe_price = round_to_tick(price)
        if tick_safe_price != price:
            logger.info(
                "place_order: rounded LIMIT price ₹%.4f -> ₹%.2f to a valid %.2f tick",
                price, tick_safe_price, TICK_SIZE,
            )
        price = tick_safe_price
        if not is_valid_tick_price(price):
            raise ValueError(
                f"Refusing to place LIMIT order at ₹{price} — not a valid ₹{TICK_SIZE} "
                f"tick multiple after rounding."
            )

    logger.info(
        "position-stocks: Placing REAL order: %s %s x%s @ %s (%s, %s)",
        transaction_type, security_id, quantity, price, order_type, product_type,
    )
    resp = client.place_order(
        security_id=security_id,
        exchange_segment=exchange_segment,
        transaction_type=transaction_type,
        quantity=quantity,
        order_type=order_type,
        product_type=product_type,
        price=price,
        validity=validity,
        tag=tag,
    )
    return _extract_data(resp) or {}


def cancel_order(db: Session, *, is_armed: bool, dhan_order_id: str) -> dict:
    """Cancelling is always allowed even when NOT armed."""
    client = _get_sdk_client(db)
    logger.info("position-stocks: Cancelling REAL order %s", dhan_order_id)
    resp = client.cancel_order(dhan_order_id)
    return _extract_data(resp) or {}


# ── Super Order (bracket: entry + target + stoploss in one call) ──────────
# See tracking doc §3.4. price is the ENTRY reference Dhan validates
# targetPrice/stopLossPrice against — it does NOT force a LIMIT fill when
# order_type="MARKET"; the fill executes at market. Pass the current live LTP.
def place_super_order(
    db: Session,
    *,
    is_armed: bool,
    security_id: str,
    exchange_segment: str,
    transaction_type: str,
    quantity: int,
    order_type: str,
    price: float,
    target_price: float,
    stop_loss_price: float,
    trailing_jump: float = 0.0,
    product_type: str = "INTRA",
    tag: Optional[str] = None,
) -> dict:
    if not is_armed:
        raise DhanNotArmedError("position-stocks-service is not armed — refusing to place super order.")
    client = _get_sdk_client(db)

    ref_price = price
    if order_type == "LIMIT" and ref_price:
        ref_price = round_to_tick(ref_price)

    logger.info(
        "position-stocks: Placing REAL Super Order: %s %s x%s ref=%s target=%s stop=%s (%s, %s)",
        transaction_type, security_id, quantity, ref_price, target_price, stop_loss_price,
        order_type, product_type,
    )
    resp = client.place_super_order(
        security_id=security_id,
        exchange_segment=exchange_segment,
        transaction_type=transaction_type,
        quantity=quantity,
        order_type=order_type,
        product_type=product_type,
        price=ref_price,
        targetPrice=target_price,
        stopLossPrice=stop_loss_price,
        trailingJump=trailing_jump,
        tag=tag,
    )
    return _extract_data(resp) or {}


def get_super_order_list(db: Session) -> list:
    """Read-only — no arm check. Used by the exit monitor to poll leg status."""
    client = _get_sdk_client(db)
    resp = client.get_super_order_list()
    data = _extract_data(resp)
    return data if isinstance(data, list) else []


def cancel_super_order(db: Session, *, order_id: str, order_leg: str) -> dict:
    """Cancelling is always allowed even when NOT armed. order_leg must be
    one of ENTRY_LEG, TARGET_LEG, STOP_LOSS_LEG."""
    client = _get_sdk_client(db)
    logger.info("position-stocks: Cancelling REAL super order %s leg=%s", order_id, order_leg)
    resp = client.cancel_super_order(order_id, order_leg)
    return _extract_data(resp) or {}
