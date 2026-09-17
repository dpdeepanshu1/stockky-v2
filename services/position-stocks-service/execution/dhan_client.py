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

import notifier
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
_VALID_ORDER_TYPES = {"MARKET", "LIMIT", "STOP_LOSS", "STOP_LOSS_MARKET"}

# SESSION41 FIX (STATUS.md open item #5 — "TICK_SIZE is hardcoded to 0.05
# for all stocks"): confirmed against NSE's actual price-linked tick
# circular (effective 2024-06-10, revised 2025-04-15) that this WAS wrong
# for any candidate priced below ₹250 (real tick ₹0.01, not ₹0.05) and for
# anything above ₹1,000 (real tick coarser than ₹0.05). Duplicated
# verbatim from real-trade-service/execution/dhan_client.py's identical
# session41 fix, per this module's own duplication-on-purpose convention
# (see this file's top docstring) — see that file's comment for the full
# source citation and the "best-effort from live price, not NSE's monthly-
# review closing price" caveat.
_TICK_SIZE_BANDS: list[tuple[float, float]] = [
    (250.0, 0.01),
    (1000.0, 0.05),
    (5000.0, 0.10),
    (10000.0, 0.50),
    (20000.0, 1.00),
    (float("inf"), 5.00),
]


def tick_size_for_price(price: float) -> float:
    """Best-effort NSE price-band tick size for `price` — see
    _TICK_SIZE_BANDS' comment above. Falls back to the flat TICK_SIZE
    default for anything non-numeric or <= 0."""
    try:
        price_f = float(price)
    except (TypeError, ValueError):
        return TICK_SIZE
    if price_f <= 0:
        return TICK_SIZE
    for upper_bound, band_tick in _TICK_SIZE_BANDS:
        if price_f < upper_bound:
            return band_tick
    return TICK_SIZE


def round_to_tick(price: float, tick_size: Optional[float] = None) -> float:
    """Round `price` to the nearest valid exchange tick, in Decimal (not
    binary float) end-to-end — see real-trade-service's dhan_client.py
    2026-09-07 fix comment for why binary float rounding can still fail
    Dhan's tick-multiple check on a price that looks perfectly clean. When
    `tick_size` is not given, it is resolved from `price`'s own NSE price
    band (session41 fix) instead of always using the flat ₹0.05 default."""
    try:
        price_f = float(price)
        if price_f <= 0:
            return price_f
        resolved_tick = tick_size if tick_size is not None else tick_size_for_price(price_f)
        if resolved_tick <= 0:
            return price_f
        price_dec = Decimal(str(price_f))
        tick_dec = Decimal(str(resolved_tick))
        ticks = (price_dec / tick_dec).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        result = (ticks * tick_dec).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return float(result)
    except (TypeError, ValueError, InvalidOperation):
        return price


def is_valid_tick_price(price: float, tick_size: Optional[float] = None) -> bool:
    """`tick_size` resolves from `price`'s own NSE price band when not
    given (session41 fix), matching round_to_tick."""
    try:
        price_f = float(price)
        resolved_tick = tick_size if tick_size is not None else tick_size_for_price(price_f)
        price_dec = Decimal(str(price_f))
        tick_dec = Decimal(str(resolved_tick))
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
    """Build a fresh dhanhq SDK client from real-trade-service's stored (and
    owned) credentials, read via dhan_credentials_ro.

    SDK version compatibility (2026-09-13, session 8):
      dhanhq <2.1  — constructor is dhanhq(client_id, access_token)
      dhanhq ≥2.1  — constructor is dhanhq(DhanContext(client_id, access_token))
    We probe for DhanContext first (the new style); if it doesn't exist we fall
    back to the old two-arg form. This keeps the code forward-compatible while
    still working with pinned 2.0.2 in environments that haven't upgraded."""
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
    try:
        from dhanhq import DhanContext  # noqa: PLC0415 — dhanhq ≥2.1
        return dhanhq(DhanContext(client_id, access_token))
    except ImportError:
        # dhanhq <2.1 (e.g. pinned 2.0.2) — old two-arg positional form
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


def get_order_list(db: Session) -> list:
    """Read-only — no arm check. Returns all PLAIN (non-super) orders for
    the day, i.e. the same order book place_order()'s own post-placement
    verification reads inline. Added session40 alongside real-trade-
    service's identically-named function so orders/reconcile.py can look
    up a plain EOD_SQUAREOFF MARKET SELL's real fill by dhan_exit_order_id
    — get_super_order_list() (used for the normal TARGET_LEG/STOP_LOSS_LEG
    exit path) never contains plain orders, so that lookup was previously
    impossible from this module."""
    client = _get_sdk_client(db)
    resp = client.get_order_list()
    data = _extract_data(resp)
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
    product_type: str = "INTRADAY",
    validity: str = "DAY",
    tag: Optional[str] = None,
) -> dict:
    """2026-09-15 fix (session40 — same hardening as real-trade-service's
    dhan_client.place_order(), applied here for consistency; see that
    function's docstring for the full DATAMATICS-incident writeup this
    closes). This is the lower-traffic fallback path (USE_SUPER_ORDER=false
    — the primary path is place_super_order() below), but it carries the
    exact same risk: a caller passing order_type="MARKET"/price=0 has no
    way to verify the SDK actually placed a MARKET order rather than
    something else."""
    if not is_armed:
        raise DhanNotArmedError("position-stocks-service is not armed — refusing to place order.")

    order_type = (order_type or "").upper()
    if order_type not in _VALID_ORDER_TYPES:
        raise ValueError(
            f"place_order: refusing to send unrecognized order_type={order_type!r} "
            f"to Dhan — must be one of {sorted(_VALID_ORDER_TYPES)}."
        )

    client = _get_sdk_client(db)

    if order_type == "MARKET":
        if price:
            logger.warning(
                "position-stocks: place_order: order_type=MARKET but caller passed "
                "nonzero price=%s — forcing price to 0.", price,
            )
        price = 0.0
    elif order_type == "LIMIT" and price:
        _band_tick = tick_size_for_price(price)
        tick_safe_price = round_to_tick(price)
        if tick_safe_price != price:
            logger.info(
                "place_order: rounded LIMIT price ₹%.4f -> ₹%.2f to a valid %.2f tick",
                price, tick_safe_price, _band_tick,
            )
        price = tick_safe_price
        if not is_valid_tick_price(price):
            raise ValueError(
                f"Refusing to place LIMIT order at ₹{price} — not a valid ₹{_band_tick} "
                f"tick multiple after rounding."
            )

    outbound = {
        "security_id": security_id, "exchange_segment": exchange_segment,
        "transaction_type": transaction_type, "quantity": quantity,
        "order_type": order_type, "product_type": product_type,
        "price": price, "validity": validity, "tag": tag,
    }
    logger.info("position-stocks: place_order: outbound payload -> %s", outbound)

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
    result = _extract_data(resp) or {}

    # Best-effort, non-blocking post-placement verification — same
    # rationale as real-trade-service's dhan_client.place_order(). Never
    # raises; the order is already live at the broker regardless.
    placed_order_id = str(result.get("orderId") or result.get("order_id") or "")
    if placed_order_id:
        try:
            list_resp = client.get_order_list()
            broker_rows = _extract_data(list_resp)
            broker_rows = broker_rows if isinstance(broker_rows, list) else []
            broker_row = next(
                (r for r in broker_rows
                 if str(r.get("orderId") or r.get("order_id") or "") == placed_order_id),
                None,
            )
            if broker_row is not None:
                broker_order_type = str(
                    broker_row.get("orderType") or broker_row.get("order_type") or ""
                ).upper()
                broker_price = broker_row.get("price")
                # 2026-09-17 fix: Dhan always echoes a MARKET order back in
                # the order book as a "LIMIT" order with a computed
                # protection price — this is normal NSE-equity broker
                # behavior (a market order is implemented internally as a
                # protected limit order), not evidence the order was placed
                # wrong. It fired on every single market order, so both
                # checks below were permanent false alarms. Only alert when
                # the broker's order type/price diverges in a way that this
                # expected MARKET->LIMIT echo doesn't already explain.
                _is_expected_market_to_limit_echo = (
                    order_type == "MARKET" and broker_order_type == "LIMIT"
                )
                if (
                    broker_order_type
                    and broker_order_type != order_type
                    and not _is_expected_market_to_limit_echo
                ):
                    msg = (
                        f"place_order: BROKER ORDER TYPE MISMATCH for order "
                        f"{placed_order_id} ({transaction_type} {security_id} "
                        f"x{quantity}) — sent order_type={order_type} but Dhan "
                        f"reports orderType={broker_order_type} (price={broker_price}) "
                        f"— investigate immediately."
                    )
                    logger.critical("position-stocks: %s", msg)
                    notifier.notify_critical(msg)
        except Exception as e:  # noqa: BLE001 — verification must never break a real placement
            logger.warning(
                "position-stocks: place_order: post-placement verification failed for "
                "order %s (non-fatal, order is already live): %s", placed_order_id, e,
            )

    return result


def cancel_order(db: Session, *, is_armed: bool, dhan_order_id: str) -> dict:
    """Cancelling is always allowed even when NOT armed."""
    client = _get_sdk_client(db)
    logger.info("position-stocks: Cancelling REAL order %s", dhan_order_id)
    resp = client.cancel_order(dhan_order_id)
    return _extract_data(resp) or {}


# ── Super Order (bracket: entry + target + stoploss in one call) ──────────
# See tracking doc §3.4.
#
# 2026-09-15 fix (session41 — LIVE EVIDENCE: this session's candidate log
# showed a 100% BUY failure rate — every quality-gate-passing candidate was
# SKIPPED with "ORDER_FAILED:Dhan API error: Invalid Price for orderType"
# or "ORDER_FAILED:For BUY: targetPrice must be > price." This is why no
# scalp positions were ever opening). This module's own prior comment here
# asserted "price is the ENTRY reference Dhan validates targetPrice/
# stopLossPrice against — it does NOT force a LIMIT fill when
# order_type=MARKET; the fill executes at market. Pass the current live
# LTP." — that assumption was WRONG, confirmed against both the live
# rejections above AND Dhan's own dhanhq-py SDK issue tracker (dhan-oss/
# DhanHQ-py#87): "MARKET orders should not have a price ... the Dhan API
# server correctly rejects such orders if a price is specified", with the
# API's own accepted example sending "price": None for a MARKET entry leg
# alongside a real targetPrice.
#
# It gets worse than a parameter tweak, though — confirmed by pulling the
# ACTUAL pinned SDK source (pip download dhanhq; requirements.txt pins
# `dhanhq>=2.0.2`, and dhanhq==2.0.2 itself has NO Super Order support at
# all — no `place_super_order` exists in that release — so a live
# deployment with USE_SUPER_ORDER enabled must be running whatever newer
# release pip resolved, e.g. 2.2.0, the version actually inspected here).
# `SuperOrder.place_super_order()` in that SDK does its OWN client-side
# validation before ever making an HTTP call:
#     if not all([..., price]): raise ValueError("Missing required
#         parameters...")          # price=None or 0 -> always raises here
#     if price <= 0: raise ValueError("Price must be > 0.")
#     if transaction_type == "BUY":
#         if targetPrice > 0 and not (targetPrice > price):
#             raise ValueError("For BUY: targetPrice must be > price.")
# i.e. the SDK method itself REFUSES to ever send a MARKET entry leg with
# no price (price=None/0 always raises "Missing required parameters" or
# "Price must be > 0." before reaching Dhan at all) — so this service's old
# price=round_to_tick(candidate.current_ltp) was the ONLY way the code
# could get past the SDK's own gate, but that nonzero price is exactly
# what Dhan's SERVER then rejects for a real MARKET order ("Invalid Price
# for orderType"). This is precisely the bug dhan-oss/DhanHQ-py#87
# describes: "place_super_order() forces incorrect argument validations
# ... making it unusable for placing market super orders." There is no
# parameter combination that satisfies both the SDK's client-side check
# and Dhan's server-side check simultaneously for a MARKET entry.
#
# ("For BUY: targetPrice must be > price." specifically is the SDK's OWN
# ValueError text above, verbatim — it was firing client-side, before any
# network call, whenever round_to_tick's tick-granularity rounding of a
# low-priced candidate's target/ref prices happened to collapse
# targetPrice to <= price; "Invalid Price for orderType" is Dhan's real
# server response for every other case, where targetPrice legitimately
# cleared price and the (broken) SDK let the nonzero-price MARKET request
# through to the actual API.)
#
# Fix: for order_type="MARKET", bypass the SDK's broken convenience
# wrapper entirely and post directly through its own underlying HTTP
# client (`client.dhan_http.post(...)`, the exact same plumbing
# place_super_order() itself uses internally — see dhanhq/dhan_http.py),
# building the identical payload shape but with "price": None, exactly
# matching the accepted working example in dhan-oss/DhanHQ-py#87. LIMIT
# entries (not currently used by this service's only caller,
# orders/entry.py, but kept correct for completeness) are unaffected by
# this SDK bug — price > 0 is exactly what the SDK's own validation wants
# there — and still go through place_super_order() normally.
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
    product_type: str = "INTRADAY",
    tag: Optional[str] = None,
) -> dict:
    if not is_armed:
        raise DhanNotArmedError("position-stocks-service is not armed — refusing to place super order.")
    client = _get_sdk_client(db)

    order_type_upper = (order_type or "").upper()

    if order_type_upper == "MARKET":
        if price:
            logger.info(
                "position-stocks: place_super_order: order_type=MARKET — "
                "dropping reference price=%s (see 2026-09-15 session41 fix "
                "note above: the SDK's place_super_order() cannot send a "
                "MARKET entry leg with no price at all, and Dhan's server "
                "rejects one WITH a price — routing around the SDK's "
                "broken wrapper via client.dhan_http.post() directly).",
                price,
            )
        dhan_http = getattr(client, "dhan_http", None)
        if dhan_http is None:
            # Only reachable on a pre-2.1-style SDK build with no
            # dhan_http attribute exposed — and per the module note above,
            # such a build (dhanhq==2.0.2) has no Super Order support at
            # all anyway, so USE_SUPER_ORDER should never be true against
            # one. Fail loudly rather than silently falling through to the
            # SDK's own broken place_super_order() (which would just
            # reproduce the exact bug this fix exists to close).
            raise RuntimeError(
                "place_super_order: MARKET entry requires client.dhan_http "
                "(direct HTTP access) to work around a known SDK validation "
                "bug (dhan-oss/DhanHQ-py#87) — this SDK build doesn't "
                "expose it. Upgrade dhanhq (this service needs >=2.2.0) or "
                "set USE_SUPER_ORDER=false to use the plain-order fallback."
            )
        # BUG FIX (session48 followup): Dhan's server validates targetPrice > price even
        # for MARKET orders when price is None. We don't send price at all (None/null) to
        # tell Dhan it's market, but targetPrice and stopLossPrice must still be sane.
        # Also guard against round_to_tick collapsing target to == or < reference price
        # on very low-priced stocks — add at least 1 tick to ensure targetPrice > price
        # (Dhan compares against the actual fill price, not our reference, so this is safe).
        t_price = float(target_price)
        s_price = float(stop_loss_price)
        ref     = float(price) if price else 0.0
        if ref > 0 and t_price <= ref:
            # targetPrice collapsed to <= reference price after tick-rounding.
            # Bump by 1 tick so Dhan server won't reject it.
            band_tick = tick_size_for_price(ref)
            t_price = round_to_tick(ref + band_tick)
            logger.warning(
                "position-stocks: place_super_order MARKET: target=%.2f <= ref=%.2f "
                "after rounding — bumped to %.2f (1 tick above ref)",
                target_price, ref, t_price,
            )
        if ref > 0 and transaction_type.upper() == "BUY" and s_price >= ref:
            band_tick = tick_size_for_price(ref)
            s_price = round_to_tick(ref - band_tick)
            logger.warning(
                "position-stocks: place_super_order MARKET: stop=%.2f >= ref=%.2f "
                "after rounding — clamped to %.2f (1 tick below ref)",
                stop_loss_price, ref, s_price,
            )
        payload = {
            "transactionType": transaction_type.upper(),
            "exchangeSegment": exchange_segment.upper(),
            "productType": product_type.upper(),
            "orderType": "MARKET",
            "securityId": security_id,
            "quantity": int(quantity),
            # price=None tells Dhan this is a true MARKET entry (no price peg).
            # Do NOT send price key at all — some Dhan gateway versions reject
            # a null price even for MARKET; omitting is always safe.
            "targetPrice": t_price,
            "stopLossPrice": s_price,
            "trailingJump": float(trailing_jump),
        }
        if tag:
            payload["correlationId"] = tag
        logger.info(
            "position-stocks: Placing REAL Super Order (direct HTTP, MARKET "
            "entry, SDK bypass): %s %s x%s target=%s stop=%s (%s)",
            transaction_type, security_id, quantity, t_price, s_price,
            product_type,
        )
        resp = dhan_http.post("/super/orders", payload)
        return _extract_data(resp) or {}

    # LIMIT (or any other) entry — the SDK's own client-side validation
    # (price > 0, targetPrice > price for BUY, etc.) is exactly what we
    # want here, so go through place_super_order() normally, same as
    # before this fix.
    #
    # AUDIT FIX (prior session): ref_price was only rounded to a valid
    # tick when order_type=="LIMIT" originally, but the caller's price can
    # still carry binary-float WS-feed artifacts (see round_to_tick's own
    # docstring) — rounding here closes that gap.
    #
    # SESSION41 FIX (STATUS.md open item #6): this path is not currently
    # exercised by any caller (orders/entry.py only ever sends MARKET), but
    # was flagged as "re-check if you ever switch to LIMIT entries" — so
    # add the same fail-loudly guard place_order() already has, instead of
    # relying solely on the SDK's own ValueError text (which fires deep
    # inside a third-party call and gives no symbol/side context in the
    # traceback). This is additive safety, not a behaviour change for
    # MARKET, which returns above before reaching this branch.
    if order_type_upper == "LIMIT" and not price:
        raise ValueError(
            f"place_super_order: order_type=LIMIT requires a positive price "
            f"({transaction_type} {security_id} x{quantity}) — refusing to "
            f"call the SDK with price={price!r}, which would only surface "
            f"as an opaque 'Missing required parameters' ValueError from "
            f"inside the SDK with no symbol context."
        )
    ref_price = round_to_tick(price) if price else price
    if order_type_upper == "LIMIT" and not is_valid_tick_price(ref_price):
        raise ValueError(
            f"place_super_order: refusing to place LIMIT entry for "
            f"{security_id} at ₹{ref_price} — not a valid "
            f"₹{tick_size_for_price(ref_price)} tick multiple after rounding."
        )

    logger.info(
        "position-stocks: Placing REAL Super Order: %s %s x%s ref=%s target=%s stop=%s (%s, %s)",
        transaction_type, security_id, quantity, ref_price, target_price, stop_loss_price,
        order_type, product_type,
    )
    # AUDIT FIX: dhanhq 2.0.x's place_super_order() does not accept a `tag`
    # kwarg — the SDK simply doesn't forward it and raises TypeError on some
    # versions. Attempt with `tag` first (future-compatible); fall back to
    # without if the SDK rejects it. The tag is informational (used by
    # /dhan/live-orders to distinguish SCALP from real-trade-service orders)
    # — losing it is not a trading-safety failure, just a visibility gap.
    try:
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
    except TypeError:
        logger.info(
            "position-stocks: place_super_order: SDK rejected `tag` kwarg — "
            "retrying without it (dhanhq <2.1 compatibility)"
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


# ── Rejection classifiers (mirrors real-trade-service/execution/dhan_client.py) ─
# Added this session after screenshots confirmed all three rejection types fire
# on SELL attempts from this service too — the classifiers already exist in
# real-trade-service but were never ported here, so eod_squareoff.py and future
# exit paths had no way to distinguish them from generic failures.

_INTRADAY_CUTOFF_MARKERS = (
    "cannot be placed at this time", "intraday orders cannot be placed",
    "square off time", "square-off time", "market is closed for intraday",
)

_SECURITY_INTRADAY_RESTRICTED_MARKERS = (
    "not allowed to be traded in intraday", "not allowed to trade in intraday",
)

_INSUFFICIENT_FUNDS_MARKERS = (
    "insufficient funds", "insufficient fund", "add rs.", "add funds",
)

# 2026-09-15 fix (session41b): eMudhra and similar stocks hit NSE circuit-
# limit price bands — "Rate Not Within Ckt Limit X To Y" from Dhan's RMS.
# A circuit-limit rejection means the LIMIT price we sent fell outside the
# exchange-enforced price band for that symbol. For a BUY this happens when
# the stock has already hit its upper circuit and our order price is above
# the allowed upper band.  Retrying at the same price can NEVER succeed (the
# band only widens at the start of the next session), so we must NOT hammer
# Dhan with repeated identical orders.  The screener should also avoid
# picking circuit-hit stocks because there is no room to exit intraday.
_CIRCUIT_LIMIT_MARKERS = (
    "rate not within ckt limit", "not within circuit limit",
    "ckt limit", "circuit limit", "within ckt",
)


def is_intraday_cutoff_error(message: str) -> bool:
    """True when Dhan rejects because the exchange intraday window has
    closed for today (~15:20-15:25 IST). Retrying the same INTRADAY order
    cannot succeed for the rest of today — caller should note the reason
    and stop hammering Dhan until the next trading day."""
    m = (message or "").lower()
    return any(marker in m for marker in _INTRADAY_CUTOFF_MARKERS)


def is_security_intraday_restricted_error(message: str) -> bool:
    """True when Dhan rejects because the symbol is on T2T/ASM/GSM
    surveillance — can NEVER be traded INTRADAY regardless of time of day.
    Caller should record the symbol in ScalpIntradayRestrictedSecurity so
    future cycles skip it before even trying to buy."""
    m = (message or "").lower()
    return any(marker in m for marker in _SECURITY_INTRADAY_RESTRICTED_MARKERS)


def is_insufficient_funds_error(message: str) -> bool:
    """True when Dhan's RMS margin engine rejects for a funds shortfall.
    For INTRA SELL orders, this usually means Dhan is treating the order
    as a new short (no matching MIS position to net against), not a
    position close — the position_value capital is tied up, but the
    account's free margin is insufficient for the gross exposure."""
    m = (message or "").lower()
    return any(marker in m for marker in _INSUFFICIENT_FUNDS_MARKERS)


def is_circuit_limit_error(message: str) -> bool:
    """True when Dhan rejects because the order price fell outside the
    exchange-enforced circuit-breaker band for this symbol.
    Example: 'RMS:221260915797007:Rate Not Within Ckt Limit 395.25 To 592.85'
    This is a PERMANENT rejection for the current session — retrying the
    same price can never succeed.  Callers should:
      - BUY side: skip the entry; record the symbol so it is avoided for
        the rest of today (it's already at/near circuit; no intraday upside).
      - SELL side: there is nothing to do except wait — if the stock has
        hit its LOWER circuit, the stock may resume later in the session.
        Log once and do NOT hammer Dhan with retries."""
    m = (message or "").lower()
    return any(marker in m for marker in _CIRCUIT_LIMIT_MARKERS)
