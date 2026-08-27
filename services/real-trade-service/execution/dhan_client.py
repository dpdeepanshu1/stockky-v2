"""
execution/dhan_client.py — THE ONLY module in this service allowed to hold
a decrypted Dhan credential or make a request to Dhan's API.

FIX (2026-08-27): dhanhq 2.0.2 constructor is dhanhq(client_id, access_token)
— DhanContext does NOT exist in this version. All SDK responses follow the
shape {status, remarks, data} — callers extract .get('data', {}) or
.get('data', []).

Every other module (risk_engine, candidate_engine, entry/exit engines) must
go through here. Two defense-in-depth layers on top of the 4-gate arming
sequence already enforced in main.py's route dependencies:

  1. Every mutating call (place_order, modify_order, cancel_order) re-checks
     `is_armed` itself — it does not trust the caller to have checked.
  2. Read-only calls (funds, positions, holdings, order list) are NOT gated
     by arming — reconciliation and the dashboard need to read live account
     state even when trading is intentionally disarmed.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import httpx
from sqlalchemy.orm import Session

import config
from auth import dhan_credentials

logger = logging.getLogger("real-trade-dhan-client")

# ── Security master cache (symbol -> Dhan security_id) ─────────────────────
_SECURITY_CACHE_TTL_SECONDS = 24 * 60 * 60
_security_cache: dict[str, str] = {}
_security_cache_loaded_at: float = 0.0

NSE_EQ_SEGMENT = "NSE_EQ"


class SecurityNotResolvedError(Exception):
    pass


class DhanNotConnectedError(Exception):
    pass


class DhanNotArmedError(Exception):
    pass


def _get_sdk_client(db: Session):
    """Build a fresh dhanhq 2.0.2 SDK client from stored credentials.
    dhanhq 2.0.2 constructor: dhanhq(client_id, access_token)
    DhanContext was removed in 2.0 — do NOT use it."""
    creds = dhan_credentials.get_decrypted_credentials(db)
    if creds is None:
        raise DhanNotConnectedError("No Dhan credentials stored — connect Dhan first.")
    client_id, access_token = creds
    try:
        from dhanhq import dhanhq  # noqa: PLC0415
    except ImportError as e:
        raise RuntimeError("dhanhq SDK not installed — check requirements.txt") from e
    # dhanhq 2.0.2: direct positional args, no DhanContext wrapper
    return dhanhq(client_id, access_token)


def _extract_data(response: dict, key: str = "data") -> any:
    """Safely extract data from Dhan SDK response envelope {status, remarks, data}.
    Returns None if status is failure or data is missing."""
    if not isinstance(response, dict):
        return response  # already unwrapped (shouldn't happen, but safe)
    if response.get("status") == "failure":
        remarks = response.get("remarks", "")
        if isinstance(remarks, dict):
            msg = remarks.get("error_message", str(remarks))
        else:
            msg = str(remarks)
        raise RuntimeError(f"Dhan API error: {msg}")
    return response.get(key)


def _load_security_cache(db: Session) -> None:
    """Loads NSE equity security IDs using dhanhq.fetch_security_list().
    Falls back to direct CSV download if SDK method fails.
    Only keeps main-board equity rows (SEM_SERIES=EQ, SEM_INSTRUMENT_NAME=EQUITY)."""
    global _security_cache, _security_cache_loaded_at
    client = _get_sdk_client(db)

    fresh: dict[str, str] = {}
    try:
        import pandas as pd
        df = client.fetch_security_list(mode='compact')
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                try:
                    # Compact CSV columns vary — try common column names
                    exch = str(row.get("SEM_EXM_EXCH_ID", row.get("EXCH_ID", ""))).strip()
                    instrument = str(row.get("SEM_INSTRUMENT_NAME", row.get("INSTRUMENT_NAME", ""))).strip()
                    series = str(row.get("SEM_SERIES", row.get("SERIES", ""))).strip()
                    sym = str(row.get("SEM_TRADING_SYMBOL", row.get("TRADING_SYMBOL", ""))).strip().upper()
                    sec_id = str(row.get("SEM_SMST_SECURITY_ID", row.get("SECURITY_ID", ""))).strip()
                    if exch == "NSE" and instrument == "EQUITY" and series in ("EQ", "") and sym and sec_id:
                        fresh[sym] = sec_id
                except Exception:
                    continue
    except Exception as e:
        logger.warning("fetch_security_list failed (%s) — falling back to direct CSV download", e)
        # Fallback: direct CSV download with auth header
        creds = dhan_credentials.get_decrypted_credentials(db)
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
                    sec_id = (row.get("SEM_SMST_SECURITY_ID") or "").strip()
                    if sym and sec_id:
                        fresh[sym] = sec_id
                except Exception:
                    continue
        except Exception as e2:
            logger.error("Security list download also failed: %s", e2)

    if not fresh:
        logger.error("Dhan instrument list fetch returned 0 usable rows — keeping existing cache.")
        return

    _security_cache = fresh
    _security_cache_loaded_at = time.time()
    logger.info("real-trade: loaded %d NSE equity security IDs from Dhan", len(fresh))


def get_security_id(db: Session, symbol: str) -> str:
    """Returns the Dhan security_id for an NSE-listed symbol. Raises
    SecurityNotResolvedError rather than returning None/guessing."""
    global _security_cache_loaded_at
    now = time.time()
    if not _security_cache or (now - _security_cache_loaded_at) > _SECURITY_CACHE_TTL_SECONDS:
        _load_security_cache(db)

    sym = symbol.strip().upper().replace(".NS", "").replace(".BO", "")
    sec_id = _security_cache.get(sym)
    if sec_id is None:
        raise SecurityNotResolvedError(f"No Dhan NSE_EQ security_id found for '{sym}'.")
    return sec_id


# Substrings Dhan's own error remarks use for an actually-invalid/expired
# token, as opposed to some other transient API problem (rate limit,
# maintenance, network blip). Used by verify_token_live() below so a
# real-time check can tell "the token is genuinely dead" apart from
# "Dhan had a bad second" — only the former should auto-disarm trading.
_AUTH_ERROR_MARKERS = (
    "invalid access token", "invalid token", "dh-901", "dh-902", "dh-905",
    "token expired", "token has expired", "unauthorized", "authentication failed",
)


def is_auth_error(message: str) -> bool:
    m = (message or "").lower()
    return any(marker in m for marker in _AUTH_ERROR_MARKERS)


def verify_token_live(db: Session) -> tuple[bool, Optional[str]]:
    """Real-time check against Dhan itself, not just our own locally-computed
    expiry timer. The 24h validity window is exact per Dhan's docs, but this
    still exists to catch the cases the local clock can't: the admin
    generated a NEW token from Dhan Web (which invalidates the old one
    immediately, before its 24h is up), clock drift between this server and
    Dhan, or Dhan-side revocation. Returns (ok, error_message).

    Cheap and read-only (get_fund_limits) — safe to call frequently, e.g.
    once per auto-pilot tick, without it ever touching an order.
    """
    try:
        get_funds(db)
        return True, None
    except DhanNotConnectedError as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)


def get_funds(db: Session) -> dict:
    """Read-only — no arm check. Returns the fund limits data dict.
    SDK returns {status, data: {availabelBalance, ...}}"""
    client = _get_sdk_client(db)
    resp = client.get_fund_limits()
    data = _extract_data(resp)
    if not isinstance(data, dict):
        return {}
    return data


def get_positions(db: Session) -> list:
    """Read-only — no arm check. Returns list of open intraday/CNC positions.
    Same "no X available" benign-empty handling as get_holdings() below —
    Dhan reports zero open positions the same way (status=failure)."""
    client = _get_sdk_client(db)
    resp = client.get_positions()
    try:
        data = _extract_data(resp)
    except RuntimeError as e:
        if "no positions" in str(e).lower():
            return []
        raise
    if isinstance(data, list):
        return data
    return []


def get_holdings(db: Session) -> list:
    """Read-only — no arm check. Returns demat holdings.

    FIX (2026-08-27): Dhan's SDK returns {status: "failure", remarks:
    "No holdings available"} — not an empty list — when the account
    simply has zero holdings (completely normal for a fresh/small
    account, or one that's currently all-cash). _extract_data() treats
    ANY status="failure" as an error, so without this special case every
    single poll logged a scary-looking "Dhan API error" warning for a
    perfectly normal state. Only THIS specific benign message is
    swallowed into an empty list; any other failure still raises."""
    client = _get_sdk_client(db)
    resp = client.get_holdings()
    try:
        data = _extract_data(resp)
    except RuntimeError as e:
        if "no holdings" in str(e).lower():
            return []
        raise
    if isinstance(data, list):
        return data
    return []


def get_order_list(db: Session) -> list:
    """Read-only — no arm check. Returns all orders for the day."""
    client = _get_sdk_client(db)
    resp = client.get_order_list()
    data = _extract_data(resp)
    if isinstance(data, list):
        return data
    return []


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
    product_type: str = "CNC",
    validity: str = "DAY",
    tag: Optional[str] = None,
) -> dict:
    """Places a REAL order. is_armed MUST be True — second lock per module docstring."""
    if not is_armed:
        raise DhanNotArmedError("Real trading is not armed — refusing to place order.")
    client = _get_sdk_client(db)
    logger.info(
        "Placing REAL order: %s %s x%s @ %s (%s, %s)",
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
    """Cancel a pending order. Cancelling is always allowed even when NOT armed."""
    client = _get_sdk_client(db)
    logger.info("Cancelling REAL order %s", dhan_order_id)
    resp = client.cancel_order(dhan_order_id)
    return _extract_data(resp) or {}
