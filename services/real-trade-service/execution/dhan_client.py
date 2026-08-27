"""
execution/dhan_client.py — THE ONLY module in this service allowed to hold
a decrypted Dhan credential or make a request to Dhan's API.

Every other module (risk_engine, candidate_engine, entry/exit engines once
built) must go through here. Two defense-in-depth layers on top of the
4-gate arming sequence already enforced in main.py's route dependencies:

  1. Every mutating call (place_order, modify_order, cancel_order) re-checks
     `is_armed` itself — it does not trust the caller to have checked. If
     main.py's gate check ever has a bug, this is the second lock.
  2. Read-only calls (funds, positions, holdings, order list) are NOT gated
     by arming — reconciliation and the dashboard need to read live account
     state even when trading is intentionally disarmed.

Uses the official `dhanhq` Python SDK. DEMO mode never reaches this file at
all — the paper-trading fill simulator (portfolio/ in Phase 2) is a
completely separate code path that only *reads* live prices, and even that
goes through market_feed/, not this client, since DEMO mode must work with
zero Dhan account linked.
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
# Dhan's order APIs take a numeric security_id, not the NSE trading symbol —
# there is no way to place an order from a symbol string alone. This caches
# the daily-published NSE equity instrument list so entry/exit never guess:
# an unresolved symbol means the order is refused, never sent with a
# fabricated ID. Cache is in-process and refreshed once per
# _SECURITY_CACHE_TTL_SECONDS — same one-process assumption already
# documented in hotpicks_store.py's stop flag; this service runs single
# worker (see docker-compose, no --workers flag), so that's safe here too.
_SECURITY_CACHE_TTL_SECONDS = 24 * 60 * 60
_security_cache: dict[str, str] = {}   # SYMBOL -> security_id (NSE_EQ only)
_security_cache_loaded_at: float = 0.0

NSE_EQ_SEGMENT = "NSE_EQ"


class SecurityNotResolvedError(Exception):
    pass


def _load_security_cache(db: Session) -> None:
    """Pulls Dhan's official NSE equity instrument list (v2 instrument API,
    requires the same access token already stored for trading) and keeps
    only main-board equity rows (SEM_EXM_EXCH_ID=NSE, SEM_INSTRUMENT_NAME=
    EQUITY, SEM_SERIES=EQ) — excludes SME/ETF/debt/etc, which share trading
    symbols with main-board names often enough to be a real mismatch risk
    if not filtered out."""
    global _security_cache, _security_cache_loaded_at
    creds = dhan_credentials.get_decrypted_credentials(db)
    if creds is None:
        raise DhanNotConnectedError("No Dhan credentials stored — connect Dhan first.")
    _client_id, access_token = creds

    resp = httpx.get(
        f"https://api.dhan.co/v2/instrument/{NSE_EQ_SEGMENT}",
        headers={"access-token": access_token},
        timeout=30.0,
    )
    resp.raise_for_status()

    import csv
    import io

    reader = csv.DictReader(io.StringIO(resp.text))
    fresh: dict[str, str] = {}
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
        except Exception:  # noqa: BLE001 — one malformed row must never abort the whole load
            continue

    if not fresh:
        # Never swap in an empty cache over a good one — a parsing/format
        # change upstream should surface as "still using yesterday's list",
        # not "every symbol is suddenly unresolved".
        logger.error("Dhan instrument list fetch returned 0 usable rows — keeping existing cache.")
        return

    _security_cache = fresh
    _security_cache_loaded_at = time.time()
    logger.info("real-trade: loaded %d NSE equity security IDs from Dhan", len(fresh))


def get_security_id(db: Session, symbol: str) -> str:
    """Returns the Dhan security_id for an NSE-listed symbol. Raises
    SecurityNotResolvedError rather than returning None/guessing — callers
    (entry_engine/exit_engine) must treat that as 'cannot act on this
    symbol right now', the same way they already treat a missing price
    tick, never as a reason to fall back to any placeholder ID."""
    global _security_cache_loaded_at
    now = time.time()
    if not _security_cache or (now - _security_cache_loaded_at) > _SECURITY_CACHE_TTL_SECONDS:
        _load_security_cache(db)

    sym = symbol.strip().upper().replace(".NS", "").replace(".BO", "")
    sec_id = _security_cache.get(sym)
    if sec_id is None:
        raise SecurityNotResolvedError(f"No Dhan NSE_EQ security_id found for '{sym}'.")
    return sec_id


class DhanNotConnectedError(Exception):
    pass


class DhanNotArmedError(Exception):
    pass


def _get_sdk_client(db: Session):
    """Build a fresh dhanhq SDK client from the currently-stored,
    decrypted credentials. Not cached across calls on purpose — a token
    refresh (manual re-paste, or future TOTP auto-refresh) must take
    effect on the very next call, not after some arbitrary cache TTL.

    NOTE: the DhanContext(client_id, access_token) constructor pattern
    below matches the dhanhq>=2.0 SDK's documented usage as of this
    writing (requirements.txt pins dhanhq==2.0.2). SDK APIs do change
    between versions — before Phase 2 wires this into a real order path,
    re-verify this constructor signature and the method names below
    (get_fund_limits/get_positions/get_holdings/get_order_list/
    place_order/cancel_order) against whatever dhanhq version actually
    installs, ideally against the sandbox environment first."""
    creds = dhan_credentials.get_decrypted_credentials(db)
    if creds is None:
        raise DhanNotConnectedError("No Dhan credentials stored — connect Dhan first.")
    client_id, access_token = creds
    try:
        from dhanhq import DhanContext, dhanhq
    except ImportError as e:  # pragma: no cover — dependency install issue
        raise RuntimeError("dhanhq SDK not installed — check requirements.txt") from e
    ctx = DhanContext(client_id, access_token)
    return dhanhq(ctx)


def get_funds(db: Session) -> dict:
    """Read-only — no arm check. Used by the dashboard and reconciliation."""
    client = _get_sdk_client(db)
    return client.get_fund_limits()


def get_positions(db: Session) -> list:
    """Read-only — no arm check."""
    client = _get_sdk_client(db)
    return client.get_positions()


def get_holdings(db: Session) -> list:
    """Read-only — no arm check."""
    client = _get_sdk_client(db)
    return client.get_holdings()


def get_order_list(db: Session) -> list:
    """Read-only — no arm check."""
    client = _get_sdk_client(db)
    return client.get_order_list()


def place_order(
    db: Session,
    *,
    is_armed: bool,
    security_id: str,
    exchange_segment: str,
    transaction_type: str,   # "BUY" | "SELL"
    quantity: int,
    order_type: str,         # "LIMIT" per decision 1 (config.ENTRY_ORDER_TYPE)
    price: float,
    product_type: str = "CNC",   # delivery, not intraday leverage, unless explicitly changed
    validity: str = "DAY",
) -> dict:
    """Places a REAL order on the connected Dhan account. `is_armed` MUST be
    passed explicitly by the caller (main.py resolves it from
    trade_gate_state right before calling this) — this function does not
    look it up itself, so a stale/cached armed-flag can never be the
    reason an order slips through. Second lock, per the module docstring."""
    if not is_armed:
        raise DhanNotArmedError("Real trading is not armed — refusing to place order.")
    client = _get_sdk_client(db)
    logger.info(
        "Placing REAL order: %s %s x%s @ %s (%s, %s)",
        transaction_type, security_id, quantity, price, order_type, product_type,
    )
    return client.place_order(
        security_id=security_id,
        exchange_segment=exchange_segment,
        transaction_type=transaction_type,
        quantity=quantity,
        order_type=order_type,
        product_type=product_type,
        price=price,
        validity=validity,
    )


def cancel_order(db: Session, *, is_armed: bool, dhan_order_id: str) -> dict:
    """Cancelling is always allowed even when NOT armed — arming only gates
    NEW risk-taking (placing orders), never the ability to back out of a
    pending one. This intentionally does NOT check is_armed the way
    place_order does."""
    client = _get_sdk_client(db)
    logger.info("Cancelling REAL order %s", dhan_order_id)
    return client.cancel_order(dhan_order_id)
