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
import re
import time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
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

# 2026-09-01 fix: NSE equity limit orders must be priced in multiples of this
# tick size (₹0.05, uniform across price bands for equities on NSE). Nothing
# in this service ever enforced that — entry_engine/entry.py computed
# entry_price as tick.price * 1.0025 rounded to 2dp, which lands on an
# invalid price the overwhelming majority of the time (spot-checked: ~80% of
# sampled prices). manual_engine.py's LIMIT ticket price had the same gap.
# A price like ₹847.41 (not a multiple of 0.05) is rejected outright by
# Dhan/the exchange, which surfaces here as "Risk-approved but Dhan
# placement failed" — a silent, systemic reason a risk-approved BUY never
# actually enters, indistinguishable in the logs from a genuine broker
# rejection. round_to_tick() is applied at the source (entry.py,
# manual_engine.py) so the stored decision/order price matches what's
# actually sent, AND again here in place_order() as a defense-in-depth
# safety net for any caller that forgets.
#
# 2026-09-07 fix: a real BUY (Wockhardt, ₹2,097.05, qty 1) was still
# rejected by the exchange with "EXCH:16283:The order price is not multiple
# of tick size" even though ₹2,097.05 LOOKS like a clean 0.05 multiple.
# Root cause: the old round_to_tick() did `price / tick_size` and
# `ticks * tick_size` in binary float. Both TICK_SIZE (0.05) and plenty of
# "clean" 2-decimal rupee prices have no exact binary float representation
# (0.05 in IEEE-754 double is actually 0.05000000000000000277...), so the
# division/multiplication can land a hair off the true tick — e.g. produce
# something that *prints* as 2097.05 via round(x, 2) but is actually
# 2097.0499999999997 or 2097.0500000000002 underneath, which the exchange's
# strict tick-multiple check (working in paise as integers) rejects even
# though Python's own display rounds it away. round(x / 0.05) inherits that
# same float error before it ever gets a chance to round to the nearest
# integer tick count. This is exactly the kind of drift the function's own
# docstring warned about but didn't fully close.
#
# Fix: do the tick math in Decimal, not float. Decimal(str(price)) parses
# the price from its exact decimal text (not the binary float that's already
# lost precision), so `price / tick_size` and `ticks * tick_size` are exact
# base-10 operations with no binary rounding error anywhere in the chain.
# The result is converted back to float only at the very end, once it is
# already guaranteed to be an exact multiple of TICK_SIZE.
TICK_SIZE = 0.05
_TICK_SIZE_DEC = Decimal("0.05")


def round_to_tick(price: float, tick_size: float = TICK_SIZE) -> float:
    """Round `price` to the nearest valid exchange tick (default ₹0.05).
    Uses Decimal arithmetic (not binary float) end-to-end so the result is
    an EXACT multiple of tick_size, not just something that happens to
    display that way after a float round() — see the 2026-09-07 fix note
    above for why the float version could still fail Dhan's tick check on
    a price that looked perfectly clean. Returns the input unchanged if
    it's <= 0 (nothing to round) or tick_size is invalid."""
    try:
        price_f = float(price)
        if price_f <= 0 or tick_size <= 0:
            return price_f
        # str(price_f) — not Decimal(price_f) — so we start from the exact
        # decimal digits a human/JSON would see, not price_f's underlying
        # binary approximation.
        price_dec = Decimal(str(price_f))
        tick_dec = Decimal(str(tick_size))
        ticks = (price_dec / tick_dec).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        result = (ticks * tick_dec).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return float(result)
    except (TypeError, ValueError, InvalidOperation):
        return price


def is_valid_tick_price(price: float, tick_size: float = TICK_SIZE) -> bool:
    """True if `price` is an exact multiple of tick_size, checked in Decimal
    to avoid the same binary-float drift round_to_tick() guards against.
    Used as a last-instant guard in place_order() so a bad price is caught
    and logged here — with a clear reason — instead of only surfacing later
    as an opaque exchange rejection."""
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
    global _security_cache, _security_cache_loaded_at, _security_collision_count
    client = _get_sdk_client(db)
    _security_collision_count = 0  # reset per-load so the summary below reflects only this load

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
                    sec_id_raw = str(row.get("SEM_SMST_SECURITY_ID", row.get("SECURITY_ID", ""))).strip()
                    if exch == "NSE" and instrument == "EQUITY" and series in ("EQ", ""):
                        _add_security(fresh, sym, sec_id_raw)
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
            "real-trade: %d symbol(s) had 2+ distinct security_ids in this load — "
            "kept the first seen for each, see prior warnings for which symbols.",
            _security_collision_count,
        )

    _security_cache = fresh
    _security_cache_loaded_at = time.time()
    logger.info("real-trade: loaded %d NSE equity security IDs from Dhan", len(fresh))


# 2026-09-07 fix: Dhan's compact security list is read via pandas
# (client.fetch_security_list(mode='compact')). If ANY row anywhere in the
# whole file has a blank/NaN SEM_SMST_SECURITY_ID, pandas upcasts that
# WHOLE column to float64 for the load — so a perfectly normal id like
# "12345" comes back as the Python float 12345.0, and str(12345.0) ==
# "12345.0". Dhan's own order API then rejects that malformed id outright
# with "Invalid SecurityId" — this reproduces the exact PARADEEP SELL
# rejection: get_security_id() found *something* cached (so it didn't hit
# SecurityNotResolvedError), but the cached value had a spurious ".0"
# suffix that Dhan's live order engine doesn't accept. Whether this trips
# depends on whether that day's master file happened to have a blank id
# somewhere — so it can appear to "come and go" across different symbols
# on different days, not just PARADEEP specifically.
_TRAILING_FLOAT_SUFFIX_RE = re.compile(r"^(\d+)\.0+$")


def _clean_security_id(raw: str) -> str:
    """Strips a spurious trailing '.0' (or '.00', etc.) picked up from a
    pandas float-dtype cast — see module comment above. Safe no-op on an
    already-clean id."""
    raw = (raw or "").strip()
    m = _TRAILING_FLOAT_SUFFIX_RE.match(raw)
    return m.group(1) if m else raw


# 2026-09-07 fix: `fresh[sym] = sec_id` used to silently overwrite on any
# repeat symbol — if Dhan's master ever lists two rows matching our filter
# for the same SEM_TRADING_SYMBOL (a re-listed/re-issued instrument keeping
# its old row alongside a new one, for example), whichever row iteration
# happened to hit LAST silently won, with zero visibility into whether that
# was the right one. Now: first-seen wins deterministically, and every
# collision is logged so a human can tell which id Dhan's dashboard says is
# actually live if a symbol starts getting rejected.
_security_collision_count = 0


def _add_security(fresh: dict[str, str], sym: str, sec_id_raw: str) -> None:
    global _security_collision_count
    sec_id = _clean_security_id(sec_id_raw)
    if not sym or not sec_id:
        return
    if sym in fresh and fresh[sym] != sec_id:
        _security_collision_count += 1
        logger.warning(
            "Security master has 2+ distinct security_ids for symbol '%s' "
            "(keeping first seen: %s, ignoring: %s) — likely a re-listed/"
            "re-issued instrument. If orders for this symbol are being "
            "rejected, check which id Dhan's own dashboard shows as live.",
            sym, fresh[sym], sec_id,
        )
        return
    fresh[sym] = sec_id


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
    # Defense-in-depth (see _clean_security_id): cleans up an already-cached
    # malformed id immediately, without waiting for the 24h TTL or a
    # service restart to pick up the _load_security_cache fix above.
    return _clean_security_id(sec_id)


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


# Dhan's order-placement APIs (place/modify/cancel) enforce a SEPARATE
# security control from the access token itself: the request's source IP
# must be on an allowlist configured in the Dhan developer console. This is
# NOT the same failure as an expired/invalid token — read-only calls
# (funds, positions, holdings, order list) are NOT IP-gated, which is why
# verify_token_live()/get_funds() above can succeed while place_order still
# fails here. A cloud host's outbound IP (Render et al.) is drawn from a
# shared, non-static pool by default, so this fires whenever the platform
# happens to route the request through an IP that was never whitelisted —
# and every subsequent order will fail identically until either (a) the
# host is given a static outbound IP and that IP is whitelisted in Dhan, or
# (b) the request is routed through a static-IP proxy that is whitelisted.
# See get_outbound_ip() below and GET /dhan/network-check in main.py.
_INVALID_IP_MARKERS = (
    "invalid ip", "ip not whitelisted", "ip address not allowed",
    "unauthorized ip", "dh-907", "dh-908",
)


def is_invalid_ip_error(message: str) -> bool:
    m = (message or "").lower()
    return any(marker in m for marker in _INVALID_IP_MARKERS)


# 2026-09-07 fix — session21 investigation: "Dhan API error: Validate Qty
# from CDSL" kept recurring for IONEXCHANG/PARADEEP even AFTER confirming
# (via direct DB query) that the exit engine was correctly computing
# product_type="CNC" for these (both opened 3 days earlier, well past
# same-day). That rules out the same-day/product-type theory entirely —
# the real cause is CDSL's mandatory eDIS/TPIN authorization step, which
# EVERY broker (not just Dhan) requires before it will let a CNC SELL
# debit existing demat holdings: "To sell holding stocks, one needs to
# complete the CDSL eDIS flow, generate T-PIN & mark stock to complete the
# sell action" (Dhan's own API docs, https://dhanhq.co/docs/v1/edis/).
# That authorization is a same-day-only, OTP-based step (valid for one
# trading day, and required again for any newly-added holding — see Dhan's
# support article "If I buy new stocks, will I have to verify my holding
# again to sell them?") — there is NO way for a fully unattended service to
# complete it, because the OTP goes to the account holder's registered
# mobile number, and the actual TPIN entry happens on CDSL's own page, not
# via a plain backend API call Dhan exposes. This is a SEBI-mandated
# anti-PoA-misuse control (see the Oct-2021 CDSL outage coverage), not a
# bug in this codebase, so there is no code fix that makes it go away —
# only clear, non-spammy visibility so a human knows to open the Dhan app
# and complete "Verify Holdings" before the market session, which is a
# 30-second manual step. See exit_engine/exit.py's use of this detector for
# the alert-throttling that keeps this from paging the same failure every
# single retry cycle for hours (as it did in the raw logs this was found
# from — 6h+ of the identical error, once per cycle, no dedup).
_CDSL_EDIS_MARKERS = (
    "validate qty from cdsl", "cdsl", "edis", "tpin",
)


def is_cdsl_edis_error(message: str) -> bool:
    m = (message or "").lower()
    return any(marker in m for marker in _CDSL_EDIS_MARKERS)


def get_outbound_ip() -> Optional[str]:
    """Best-effort: what IP is this service ACTUALLY sending Dhan requests
    from right now? Answers the question an Invalid IP error can't on its
    own ("ok, but which IP do I even put in Dhan's console?"). Uses a
    public IP-echo service since Dhan's own error message never includes
    the rejected IP. Read-only, no credentials involved — safe to call
    anonymously and often."""
    try:
        resp = httpx.get("https://api.ipify.org?format=json", timeout=6.0)
        resp.raise_for_status()
        return resp.json().get("ip")
    except Exception as e:
        logger.warning("get_outbound_ip: lookup failed: %s", e)
        return None


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

    # Defense-in-depth: see TICK_SIZE/round_to_tick module comment above.
    # MARKET orders correctly send price=0 (no limit) — never touch that.
    if order_type == "LIMIT" and price:
        tick_safe_price = round_to_tick(price)
        if tick_safe_price != price:
            logger.info(
                "place_order: rounded LIMIT price ₹%.4f -> ₹%.2f to a valid %.2f tick "
                "(caller did not pre-round)", price, tick_safe_price, TICK_SIZE,
            )
        price = tick_safe_price
        # 2026-09-07 fix: last-instant guard, checked in Decimal (see
        # is_valid_tick_price above) so a price that survives rounding but
        # is still off-tick due to *upstream* float drift (e.g. a caller
        # did its own arithmetic on an already-rounded price before handing
        # it here) is caught with a clear, attributable reason instead of
        # silently reaching Dhan and coming back as an opaque
        # "EXCH:16283:The order price is not multiple of tick size"
        # rejection that looks identical to a genuine broker-side issue.
        if not is_valid_tick_price(price):
            raise ValueError(
                f"Refusing to place LIMIT order at ₹{price} — not a valid "
                f"₹{TICK_SIZE} tick multiple after rounding. This should be "
                f"unreachable; report as a bug in the caller's price math."
            )

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
