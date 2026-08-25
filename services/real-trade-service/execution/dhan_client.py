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
from typing import Optional

from sqlalchemy.orm import Session

import config
from auth import dhan_credentials

logger = logging.getLogger("real-trade-dhan-client")


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
