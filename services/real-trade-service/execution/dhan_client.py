"""
execution/dhan_client.py — THE ONLY module in this service allowed to hold
a decrypted Dhan credential or make a request to Dhan's API.

SDK version note (updated session72): requirements.txt now pins dhanhq>=2.2.0
(aligned with position-stocks-service). _get_sdk_client() probes for
DhanContext (≥2.1 style) and falls back to dhanhq(client_id, access_token)
(2.0.x style) so both SDK generations are supported. All SDK responses
follow the shape {status, remarks, data} — callers extract .get('data', {})
or .get('data', []).

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
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Optional

import httpx
from sqlalchemy.orm import Session

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
#
# SESSION41 FIX (STATUS.md/session41 open item #5): TICK_SIZE=0.05 was a
# flat constant, but NSE's tick size is actually price-linked, reviewed
# monthly off the security's own closing price (NSE circular effective
# 2024-06-10, revised further 2025-04-15 — confirmed via NSE circulars and
# broker notices, e.g. Zerodha/5paisa/HDFC Securities coverage). A candidate
# priced below ₹250 needs a ₹0.01 tick, not ₹0.05 — round_to_tick(price,
# tick_size=0.05) was silently over-rounding those (e.g. a genuine ₹87.13
# target gets forced to ₹87.15), and is_valid_tick_price(price, 0.05) could
# reject a perfectly valid ₹87.13 order outright. Same story in the other
# direction for high-priced stocks (>₹1,000), where the real tick is now
# coarser than 0.05. TICK_SIZE stays as the >₹250–₹1,000 band default (the
# single most common band for this service's candidates) for any caller
# that still passes it explicitly, but round_to_tick/is_valid_tick_price now
# resolve the correct band from the price itself unless a caller overrides.
TICK_SIZE = 0.05
_TICK_SIZE_DEC = Decimal("0.05")

# NSE price-linked tick bands (cash/equity segment), upper-bound exclusive.
# Source: NSE circular effective 2024-06-10 (introduced the <₹250 = ₹0.01
# band) and the 2025-04-15 revision (added the >₹1,000 bands). Reviewed
# monthly by NSE off the security's prior-month closing price — this is a
# best-effort approximation from the LIVE reference price this service
# already has on hand (current LTP / candidate price), not NSE's own
# monthly-review closing price, so it can occasionally be one band off for
# a security that crossed a band boundary since NSE's last monthly review.
# That's an acceptable trade-off: it is still far closer than the old flat
# ₹0.05 for every price, and Dhan/the exchange remain the final authority
# — a wrong guess here surfaces as a normal tick-multiple rejection, same
# as any other Dhan-side validation failure, never a silent bad fill.
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
    _TICK_SIZE_BANDS' comment above for the source and the monthly-review
    caveat. Falls back to the flat TICK_SIZE default for anything
    non-numeric or <= 0 (nothing meaningful to band)."""
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
    """Round `price` to the nearest valid exchange tick. Uses Decimal
    arithmetic (not binary float) end-to-end so the result is an EXACT
    multiple of tick_size, not just something that happens to display that
    way after a float round() — see the 2026-09-07 fix note above for why
    the float version could still fail Dhan's tick check on a price that
    looked perfectly clean. When `tick_size` is not given (the normal
    case), it is resolved from `price`'s own NSE price band (session41 fix
    — see tick_size_for_price above) instead of always using the flat
    ₹0.05 default. Returns the input unchanged if it's <= 0 (nothing to
    round) or tick_size is invalid."""
    try:
        price_f = float(price)
        if price_f <= 0:
            return price_f
        resolved_tick = tick_size if tick_size is not None else tick_size_for_price(price_f)
        if resolved_tick <= 0:
            return price_f
        # str(price_f) — not Decimal(price_f) — so we start from the exact
        # decimal digits a human/JSON would see, not price_f's underlying
        # binary approximation.
        price_dec = Decimal(str(price_f))
        tick_dec = Decimal(str(resolved_tick))
        ticks = (price_dec / tick_dec).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        result = (ticks * tick_dec).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return float(result)
    except (TypeError, ValueError, InvalidOperation):
        return price


def is_valid_tick_price(price: float, tick_size: Optional[float] = None) -> bool:
    """True if `price` is an exact multiple of tick_size, checked in Decimal
    to avoid the same binary-float drift round_to_tick() guards against.
    Used as a last-instant guard in place_order() so a bad price is caught
    and logged here — with a clear reason — instead of only surfacing later
    as an opaque exchange rejection. `tick_size` resolves from `price`'s own
    NSE price band when not given (session41 fix), matching round_to_tick."""
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
    """Build a fresh dhanhq SDK client from stored credentials.

    SDK version compatibility (session72 — aligned with position-stocks-service):
      dhanhq <2.1  — constructor is dhanhq(client_id, access_token)
      dhanhq ≥2.1  — constructor is dhanhq(DhanContext(client_id, access_token))
    We probe for DhanContext first (≥2.1 style); if absent fall back to the
    old two-arg form. Mirrors position-stocks-service's identical probe so
    both services behave the same across any dhanhq build ≥2.0.
    """
    creds = dhan_credentials.get_decrypted_credentials(db)
    if creds is None:
        raise DhanNotConnectedError("No Dhan credentials stored — connect Dhan first.")
    client_id, access_token = creds
    try:
        from dhanhq import dhanhq  # noqa: PLC0415
    except ImportError as e:
        raise RuntimeError("dhanhq SDK not installed — check requirements.txt") from e
    try:
        from dhanhq import DhanContext  # noqa: PLC0415 — dhanhq ≥2.1
        return dhanhq(DhanContext(client_id, access_token))
    except ImportError:
        # dhanhq <2.1 (old two-arg positional form — fallback only)
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
# 2026-09-09 fix — same underlying blocker, different wording: after fixing
# the "insufficient funds" bug (broker_imported → CNC, see CHANGES_
# 2026-09-09_INSUFFICIENT_FUNDS_SELL_FIX.md), CNC SELLs for the newly-
# import-fixed positions started failing with "Insufficient Holding
# Quantity" / "Scrip limit insufficient" instead. Confirmed against Dhan's
# own support article for that exact message (dhan.co/support/orders-and-
# positions/order-rejections/why-was-my-order-rejected-with-scrip-limit-
# insufficient-or-insufficient-holding-quantity-what-does-it-mean/): "This
# error occurs when the scrip ... is not freely available in your holding"
# — Dhan's wording for a holding that hasn't cleared today's CDSL eDIS/TPIN
# authorization, the exact same SEBI-mandated step described above, just
# surfaced through the scrip-limit RMS check instead of the CDSL-specific
# qty-validation check that produces "Validate Qty from CDSL". Both checks
# guard the same thing (is this holding authorized-for-sale today?) and
# both need the identical human fix (open Dhan app → Verify Holdings →
# enter T-PIN) — there is no way to tell them apart in a way that changes
# what the user needs to do, so both route through the same detector/alert
# rather than one falling into the generic "check Dhan directly" escalation
# with no actionable guidance.
_CDSL_EDIS_MARKERS = (
    "validate qty from cdsl", "cdsl", "edis", "tpin",
    "insufficient holding quantity", "scrip limit insufficient",
)


def is_cdsl_edis_error(message: str) -> bool:
    m = (message or "").lower()
    return any(marker in m for marker in _CDSL_EDIS_MARKERS)


# 2026-09-09 fix — session "insufficient funds on SELL" investigation:
# Dhan's RMS margin-shortfall rejection ("RMS:<id>:You have insufficient
# funds. Please add Rs.<amount> to trade.") was previously unrecognized by
# this module and fell into exit.py's generic catch-all branch — logged and
# retried forever with no explanation of WHY a SELL of an owned holding
# would ever need funds. The #1 cause (now fixed — see models.py
# TradePosition.broker_imported and exit_engine.exit._send_real_sell's
# docstring) was a broker-imported holding being sold product_type=
# "INTRADAY": with no matching MIS position to net against, Dhan prices
# that SELL like a fresh naked short and demands margin for it, which is
# what this specific message reports. Kept as its own detector (rather than
# folded into the generic branch) so exit.py can give a message that
# explains the actual mechanism instead of a bare "Dhan rejected this"
# — and so a genuine, still-possible funds shortfall (e.g. margin used
# elsewhere in the account) gets the same clear framing.
_INSUFFICIENT_FUNDS_MARKERS = (
    "insufficient funds", "insufficient fund", "add rs.", "add funds",
)


def is_insufficient_funds_error(message: str) -> bool:
    m = (message or "").lower()
    return any(marker in m for marker in _INSUFFICIENT_FUNDS_MARKERS)


# 2026-09-08 fix — Bug B from the WELSPLSOL/session investigation: 3 same-day
# exits (Coffee Day Enterprises, Hyundai Motor India, Bandhan Bank), all at
# 3:27 PM IST, rejected with Dhan/NSE's own end-of-day cutoff message for
# fresh intraday orders — well-known to sit around 15:20-15:25 IST, ahead of
# the exchange's own auto square-off. This had ZERO special handling and fell
# into exit.py's generic catch-all branch, which just retried identically
# every cycle and only ever said "N consecutive rejections" — no indication
# that the real cause is a hard exchange-side cutoff that retrying the exact
# same INTRADAY order can never get past today, however many more cycles run.
# NOT the same failure as eod_squareoff being disabled — confirmed live via
# GET /status/REAL that eod_squareoff is enabled and already ran today
# (last_run present), so these three simply opened/re-evaluated after 15:15
# and hit the cutoff before EOD squareoff's own next pass could catch them.
# Kept as its own detector (same idiom as CDSL/insufficient-funds above) so
# exit.py can say what's actually going on instead of a bare "check Dhan
# directly", and so it doesn't need a human step at all — same-day product
# type only applies for the rest of today; tomorrow this position is no
# longer same-day and _send_real_sell will naturally send it as CNC instead
# (which then needs CDSL eDIS clearance, same as any other holding — see
# is_cdsl_edis_error above).
_INTRADAY_CUTOFF_MARKERS = (
    "cannot be placed at this time", "intraday orders cannot be placed",
    "square off time", "square-off time", "market is closed for intraday",
)


def is_intraday_cutoff_error(message: str) -> bool:
    m = (message or "").lower()
    return any(marker in m for marker in _INTRADAY_CUTOFF_MARKERS)


# BUG FIX (2026-09-10, session21e real-trade-service audit — live-evidence-
# driven, found via the actual Dhan order book rather than code reading):
# "RMS:<id>:Order rejected as this stock is not allowed to be traded in
# Intraday." — a rejection distinct from both is_intraday_cutoff_error
# above (that's a TIME-of-day restriction, same message regardless of
# symbol) and is_cdsl_edis_error (that's a SETTLEMENT-timing restriction on
# CNC same-day sells). This one is a permanent, PER-SECURITY restriction:
# some NSE securities (trade-to-trade / ASM / GSM surveillance stocks) can
# never be traded with product_type="INTRADAY" at all, any time of day, any
# day — only CNC (delivery) is allowed for them. This had NO detector before
# this fix, so it fell into exit.py's generic catch-all branch: retried
# every cycle, streak-escalated as if it might eventually succeed, with no
# indication of the real (permanent-for-today) cause.
#
# This is a serious gap for _send_real_sell's own same-day logic
# specifically: a SAME-DAY stop-loss/target hit on one of these securities
# was completely unexitable that day — CNC fails (CDSL hasn't settled the
# same-day buy yet) AND INTRADAY fails (this restriction) — with no
# fallback, meaning the position's stop-loss protection silently does
# nothing until the position ages into "not same-day" and a CNC sell
# becomes viable (which does work once CDSL settles, since T2T only
# blocks intraday trading, not delivery trading). Handled exactly like
# is_intraday_cutoff_error above: not recoverable today, so it doesn't
# count toward the generic reject-streak escalation, and repeatedly
# resending it is exactly as wasteful as the time-cutoff case — exit.py
# reuses the same per-position "stop hammering Dhan for the rest of today"
# suppression for both.
_SECURITY_INTRADAY_RESTRICTED_MARKERS = (
    "not allowed to be traded in intraday", "not allowed to trade in intraday",
)


def is_security_intraday_restricted_error(message: str) -> bool:
    m = (message or "").lower()
    return any(marker in m for marker in _SECURITY_INTRADAY_RESTRICTED_MARKERS)


# 2026-09-15 fix (session41b): circuit-limit (NSE price-band) rejection.
# "Rate Not Within Ckt Limit X To Y" — the order price is outside the
# exchange-enforced circuit-breaker band for this symbol.  This is a
# PERMANENT rejection for the current session: no retry at the same price
# can succeed.  On BUY side: skip entry, the stock has no intraday upside.
# On SELL side: nothing to do except wait; log once, do not retry.
_CIRCUIT_LIMIT_MARKERS = (
    "rate not within ckt limit", "not within circuit limit",
    "ckt limit", "circuit limit", "within ckt",
)


def is_circuit_limit_error(message: str) -> bool:
    """True when Dhan rejects because the order price fell outside the
    NSE circuit-breaker price band for this symbol.
    Example: 'RMS:...:Rate Not Within Ckt Limit 395.25 To 592.85'
    Permanent for the session — callers must NOT retry at the same price."""
    m = (message or "").lower()
    return any(marker in m for marker in _CIRCUIT_LIMIT_MARKERS)


# 2026-09-09 fix — "RMS:<id>:You are trying to sell more than the quantity
# you currently hold." — fired when Stockky's qty_open is out of sync with
# what Dhan's CDSL demat actually shows. Root cause: a partial exit was
# acknowledged by Dhan but the fill-reconcile loop didn't reduce qty_open
# in the DB, so the next cycle tries to sell the original full quantity
# again. The correct response is NOT to retry with the same qty — that will
# always reject. Instead: fetch the actual available qty from Dhan holdings,
# cap the SELL to what's there, and if nothing is left, force-close the
# position as ghost (broker already exited it). If the holdings fetch itself
# fails, leave the position open and retry next cycle (same as CDSL branch).
_OVERSELL_MARKERS = (
    "sell more than the quantity",
    "sell more than quantity",
    "trying to sell more",
    "cannot sell more",
)


def is_oversell_error(message: str) -> bool:
    m = (message or "").lower()
    return any(marker in m for marker in _OVERSELL_MARKERS)


# 2026-09-09 fix — "EXCH:16387:Security is not allowed to trade in this
# market." — fired for HYUNDAI, BANDHANBNK and similar stocks bought today
# when exit.py tries to sell them as CNC from holdings. These stocks were
# bought intraday (T+0) and haven't settled to demat yet (T+1), AND the
# position opened after the INTRADAY cutoff window so the INTRADAY product
# type is no longer valid either. Dhan rejects the CNC SELL because the
# security isn't in CDSL holdings yet, and the INTRADAY SELL because the
# exchange cutoff has passed. Neither branch was recognised before — both
# fell into the generic catch-all and retried forever.
# Correct handling: these are "stuck until tomorrow" just like the intraday
# cutoff case — leave them open overnight; tomorrow they're T+1 settled,
# CNC will work, and CDSL eDIS/TPIN covers the demat validation.
_EXCHANGE_NOT_ALLOWED_MARKERS = (
    "security is not allowed to trade",
    "not allowed to trade in this market",
    "exch:16387",
    "scrip is not tradeable",
    "scrip not tradeable",
)


def is_exchange_not_allowed_error(message: str) -> bool:
    m = (message or "").lower()
    return any(marker in m for marker in _EXCHANGE_NOT_ALLOWED_MARKERS)


# ── CDSL eDIS / TPIN flow (DhanHQ v2, verified against
# https://dhanhq.co/docs/v2/edis/ on 2026-09-07 — this IS the current v2
# path, unlike the deprecated v1 assumption an earlier pass here made) ──────
#
# IMPORTANT — read before wiring this into anything automatic: the T-PIN is
# a one-time SMS OTP, not a credential. It CANNOT be generated once and
# stored in .env like ANGELONE_API_KEY etc. — three separate reasons:
#   1. GET /edis/tpin doesn't return a TPIN at all. It tells CDSL to SMS one
#      to the account holder's registered mobile. The API response is just
#      "202 Accepted" — no body, no PIN value for this backend to capture.
#   2. Even once you have that SMS'd TPIN, Dhan's API never accepts it as a
#      JSON field anywhere. POST /edis/form returns `edisFormHtml` — an
#      HTML <form> whose own onload JS immediately auto-submits itself to
#      CDSL's site (https://edis.cdslindia.com/eDIS/VerifyDIS/), carrying an
#      encrypted CDSL transaction blob (TransDtls) in a hidden field. The
#      TPIN is typed on CDSL'S page after that redirect — this backend
#      never sees it and never could, by design (that's the whole point of
#      the control: authorization happens directly with the depository, not
#      through the broker's API).
#   3. Even if it COULD be captured, it's single-use and short-lived —
#      storing it anywhere for reuse would do nothing on the next call.
#
# What CAN be automated: requesting the OTP (edis_request_tpin) and
# generating the CDSL redirect form (edis_get_form) are both plain backend
# calls. What CANNOT: the actual TPIN entry, which needs a human in a real
# browser on CDSL's page. See main.py's /dhan/edis/* endpoints — they cut
# the manual step down to "open one URL, type the code you were just
# texted", once per trading day (or again for any newly-settled holding).
_DHAN_REST_BASE = "https://api.dhan.co/v2"


def edis_request_tpin(db: Session) -> None:
    """GET /v2/edis/tpin — tells CDSL to SMS a fresh T-PIN to the
    account's registered mobile number. No response body (API returns
    202 Accepted); nothing here to capture or store — see module note
    above. Raises on any non-2xx via raise_for_status()."""
    creds = dhan_credentials.get_decrypted_credentials(db)
    if creds is None:
        raise DhanNotConnectedError("No Dhan credentials stored — connect Dhan first.")
    _client_id, access_token = creds
    resp = httpx.get(
        f"{_DHAN_REST_BASE}/edis/tpin",
        headers={"Content-Type": "application/json", "access-token": access_token},
        timeout=15.0,
    )
    resp.raise_for_status()


def edis_get_form(
    db: Session,
    isin: str = "",
    qty: int = 0,
    exchange: str = "NSE",
    segment: str = "EQ",
    bulk: bool = True,
) -> str:
    """POST /v2/edis/form — returns CDSL's self-submitting HTML redirect
    form as a raw string. bulk=True (the default) covers every holding in
    the portfolio with one form/one TPIN entry, which is what you want for
    the daily "authorize everything I might need to exit today" case;
    pass bulk=False with a specific isin/qty to authorize just one
    position. This function only fetches the form — it must be rendered
    in an actual browser (see /dhan/edis/authorize-form in main.py) for
    the redirect-to-CDSL + TPIN entry to happen; there is nothing to
    "complete" purely server-side.

    2026-09-12 fix (round 1): the previous version sent {"clientId": ...,
    "bulk": True} for bulk mode and omitted isin/qty/exchange/segment
    entirely, believing Dhan rejected those fields when present. That was
    backwards — /edis/form has NO clientId field at all (the access-token
    header alone identifies the account) — but round 1 then left
    isin="" / qty=0 as bulk-mode placeholders, which is itself invalid:
    Dhan's DH-905 is a combined "missing required fields, bad values for
    parameters" error, and an empty-string ISIN / zero qty trips the
    "bad value" half even though every field is technically present.

    2026-09-12 fix (round 2): verified against a working reference
    implementation (github.com/prashantpiyush/dhan-edis, which completes
    the full CDSL round-trip) — bulk=True does NOT mean isin/qty can be
    empty. It still needs a real isin + qty from an actual holding as an
    "anchor" for the request; bulk=True is what then extends the
    authorization to the whole portfolio rather than just that one
    holding. So when the caller asks for bulk mode without supplying its
    own isin/qty (the normal dashboard "Authorize on CDSL" case), pull the
    first current holding via get_holdings() and use its isin/qty as the
    anchor. bulk=False keeps using whatever isin/qty the caller passed in,
    unchanged from before."""
    creds = dhan_credentials.get_decrypted_credentials(db)
    if creds is None:
        raise DhanNotConnectedError("No Dhan credentials stored — connect Dhan first.")
    _client_id, access_token = creds  # clientId is not part of this request; the
    # access-token header alone identifies the account (Dhan echoes dhanClientId
    # back in the response instead).

    if bulk and not isin:
        holdings = get_holdings(db)
        if not holdings:
            raise RuntimeError(
                "No demat holdings found — there is nothing for CDSL eDIS to "
                "authorize yet (bulk authorization needs at least one holding "
                "to anchor the request)."
            )
        anchor = holdings[0]
        isin = anchor.get("isin") or ""
        qty = (
            anchor.get("availableQty")
            or anchor.get("totalQty")
            or anchor.get("dpQty")
            or 1
        )
        if not isin:
            raise RuntimeError(
                f"Dhan holding is missing an isin field, cannot anchor bulk "
                f"eDIS request: {anchor}"
            )

    body = {
        "isin": isin,
        "qty": qty,
        "exchange": exchange,
        "segment": segment,
        "bulk": bulk,
    }

    resp = httpx.post(
        f"{_DHAN_REST_BASE}/edis/form",
        headers={"Content-Type": "application/json", "access-token": access_token},
        json=body,
        timeout=15.0,
    )
    if not resp.is_success:
        # Surface the actual Dhan error body in logs/UI instead of a generic 400
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text[:300]
        raise RuntimeError(
            f"Dhan /edis/form returned HTTP {resp.status_code}: {detail}"
        )
    data = resp.json() or {}
    html = data.get("edisFormHtml")
    if not html:
        raise RuntimeError(f"Dhan /edis/form returned no edisFormHtml: {data}")
    return html


def edis_inquire(db: Session, isin: str = "ALL") -> dict:
    """GET /v2/edis/inquire/{isin} — check whether holdings are currently
    eDIS-approved for sale. Pass "ALL" (default) for a whole-portfolio
    check. Useful for a morning dashboard check ("is today's authorization
    already done?") before the exit engine ever needs to find out the hard
    way via a rejected SELL."""
    creds = dhan_credentials.get_decrypted_credentials(db)
    if creds is None:
        raise DhanNotConnectedError("No Dhan credentials stored — connect Dhan first.")
    _client_id, access_token = creds
    resp = httpx.get(
        f"{_DHAN_REST_BASE}/edis/inquire/{isin}",
        headers={"Content-Type": "application/json", "access-token": access_token},
        timeout=15.0,
    )
    resp.raise_for_status()
    return resp.json() or {}


# Candidate field-name spellings seen across Dhan API versions/docs for the
# per-holding approved-vs-total quantity pair. Kept as a list rather than
# hardcoding one pair because this endpoint's exact response shape hasn't
# been captured from a live call yet (no network access while writing this)
# — if the real payload uses a name not listed here, _edis_row_status below
# falls through to "unknown" instead of guessing, so the dashboard badge
# never shows a confident green/red on a misread.
_EDIS_APPROVED_KEYS = ("aprvdQty", "approvedQty", "approved_qty", "aprvd_qty")
_EDIS_TOTAL_KEYS = ("totalQty", "total_qty", "dpQty", "dp_qty")


def _first_present(row: dict, keys: tuple) -> Optional[float]:
    for k in keys:
        if k in row and row[k] is not None:
            try:
                return float(row[k])
            except (TypeError, ValueError):
                continue
    return None


def edis_verification_summary(db: Session) -> dict:
    """Summarize edis_inquire("ALL") into the single question the Settings/
    Real Trade dashboard actually wants answered: "is today's CDSL
    authorization already done for every current holding?" Three possible
    outcomes, deliberately kept distinct rather than collapsed into a
    boolean — showing a confident green/red on a response this function
    can't actually parse would be worse than saying so:

      verified_today = True   — every holding has approved >= total qty.
      verified_today = False  — at least one holding still needs auth;
                                 pending_symbols lists which.
      verified_today = None   — inquire call failed, returned no holdings
                                 to check (nothing to verify either way),
                                 or came back in a shape none of the known
                                 field-name variants match. `detail`
                                 explains which.

    NOTE: the exact field names Dhan's v2 edis/inquire response uses for
    per-holding approved/total qty haven't been confirmed against a live
    response yet (see _EDIS_APPROVED_KEYS/_EDIS_TOTAL_KEYS above) — if this
    keeps returning verified_today=None with detail="unrecognized shape",
    capture one real response body and add its actual key names to those
    tuples.
    """
    checked_at = datetime.now(timezone.utc).isoformat()

    # session73 fix: Dhan's /edis/inquire/ALL returns a plain 500 (not an
    # empty list) when the demat account currently holds nothing — the
    # dashboard was showing that as a scary "⚠ Unknown / Server error 500"
    # instead of the calm, accurate answer "nothing held, so nothing needs
    # authorizing". Check our own holdings first (the same call
    # edis_get_form's bulk-anchor path already uses) and short-circuit
    # before ever hitting the flaky inquire endpoint when there's genuinely
    # nothing to check. Any error checking holdings just falls through to
    # the original inquire-based logic below, unchanged.
    try:
        holdings = get_holdings(db)
    except Exception:
        holdings = None
    if holdings is not None and len(holdings) == 0:
        return {"verified_today": True, "checked_at": checked_at,
                "detail": "No demat holdings currently — nothing to authorize.",
                "holdings_total": 0, "holdings_pending": 0, "pending_symbols": []}

    try:
        raw = edis_inquire(db, isin="ALL")
    except DhanNotConnectedError as e:
        return {"verified_today": None, "checked_at": checked_at, "detail": str(e),
                "holdings_total": 0, "holdings_pending": 0, "pending_symbols": []}
    except Exception as e:
        return {"verified_today": None, "checked_at": checked_at,
                "detail": f"eDIS inquire call failed: {e}",
                "holdings_total": 0, "holdings_pending": 0, "pending_symbols": []}

    # Response may be a bare list, or wrapped under a common container key —
    # handle both without assuming which this account/version returns.
    rows = raw if isinstance(raw, list) else (
        raw.get("data") or raw.get("holdings") or raw.get("result") or []
        if isinstance(raw, dict) else []
    )
    if not rows:
        return {"verified_today": None, "checked_at": checked_at,
                "detail": "No holdings returned by eDIS inquire (nothing to authorize, or unrecognized response shape).",
                "holdings_total": 0, "holdings_pending": 0, "pending_symbols": []}

    pending = []
    unrecognized = 0
    for row in rows:
        if not isinstance(row, dict):
            unrecognized += 1
            continue
        approved = _first_present(row, _EDIS_APPROVED_KEYS)
        total = _first_present(row, _EDIS_TOTAL_KEYS)
        if approved is None or total is None:
            unrecognized += 1
            continue
        if approved < total:
            label = row.get("isin") or row.get("tradingSymbol") or row.get("symbol") or "unknown"
            pending.append(label)

    if unrecognized == len(rows):
        return {"verified_today": None, "checked_at": checked_at,
                "detail": "eDIS inquire returned holdings but in an unrecognized shape — "
                          "field names didn't match any known variant. Check manually via "
                          "GET /dhan/edis/status?isin=ALL and update _EDIS_APPROVED_KEYS/"
                          "_EDIS_TOTAL_KEYS in dhan_client.py once you see the real keys.",
                "holdings_total": len(rows), "holdings_pending": 0, "pending_symbols": []}

    return {
        "verified_today": len(pending) == 0,
        "checked_at": checked_at,
        "detail": "All holdings authorized for sale today." if not pending
                  else f"{len(pending)} holding(s) still need today's T-PIN authorization.",
        "holdings_total": len(rows),
        "holdings_pending": len(pending),
        "pending_symbols": pending,
    }


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


_VALID_ORDER_TYPES = {"MARKET", "LIMIT", "STOP_LOSS", "STOP_LOSS_MARKET"}


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
    """Places a REAL order. is_armed MUST be True — second lock per module docstring.

    2026-09-15 fix (session40 — DATAMATICS position 81 exit-SELL rejection
    storm, 89 consecutive REJECTED zero-fill attempts over ~4.5h): every
    caller in this codebase (exit.py, entry.py, manual_engine.py) already
    calls this function with order_type="MARKET", price=0 for a market
    exit/entry — that part was always correct. What this fix closes is
    that place_order() used to trust the dhanhq SDK's own place_order()
    to correctly translate `order_type="MARKET"` + `price=0` into an
    actual MARKET order at Dhan, with no way for this service to see or
    verify what request body the SDK actually built and sent. If the SDK
    (any version, including a future one this service upgrades to) ever
    mis-maps that order_type string — or a caller's price argument is
    stale/nonzero despite order_type="MARKET" — the exchange-side symptom
    is indistinguishable from a normal broker rejection: same
    "REJECTED, zero fill" shape logged here, silently retried forever by
    exit_engine with no way to tell "genuinely unfillable" apart from
    "we're sending the wrong order every single time".

    Three independent hardening layers, defense-in-depth (none of them
    require trusting the SDK's internal order_type handling):
      1. `order_type` is validated against Dhan's own documented enum
         (_VALID_ORDER_TYPES) BEFORE ever reaching the SDK — an
         unrecognized value now fails loudly here instead of silently
         reaching Dhan and getting mapped however the SDK's internal
         (unauditable, closed-source-to-us) dispatch logic decides.
      2. order_type="MARKET" now FORCES price to exactly 0.0 here,
         unconditionally — previously this was only a code-comment
         convention ("MARKET orders correctly send price=0 — never touch
         that") that every caller happened to honor; a stale/nonzero
         price argument reaching this function for a MARKET order can no
         longer leak through as a real limit price.
      3. The exact outbound payload (every field this function is about
         to hand the SDK) is logged BEFORE the call, and the broker's own
         order-type/price for the just-placed order is checked immediately
         after via get_order_list() and logged at CRITICAL if it doesn't
         match what was requested — instead of only finding out from a
         human re-reading Dhan's order book after the fact. This is
         best-effort and NEVER blocks or fails the placement itself (the
         order is already live at the broker by the time this check runs)
         — it only makes a mismatch immediately visible in the logs/alerts
         instead of requiring someone to notice it independently.

    See execution/reconcile.py's per-cycle orderType verification for the
    second, durable half of this fix (covers orders reconcile sees later,
    not just the one just placed), and exit_engine/exit.py's cooldown gate
    (TradePosition.consecutive_exit_failures) for why a real position can
    no longer be re-sold every single cycle forever after repeated
    zero-fill rejections without an escalating backoff and an operator
    alert.
    """
    if not is_armed:
        raise DhanNotArmedError("Real trading is not armed — refusing to place order.")

    order_type = (order_type or "").upper()
    if order_type not in _VALID_ORDER_TYPES:
        raise ValueError(
            f"place_order: refusing to send unrecognized order_type={order_type!r} "
            f"to Dhan — must be one of {sorted(_VALID_ORDER_TYPES)}. This should be "
            f"unreachable; report as a bug in the caller."
        )

    client = _get_sdk_client(db)

    # Defense-in-depth: see TICK_SIZE/round_to_tick module comment above.
    if order_type == "MARKET":
        # Hard override, not just a convention every caller has to honor —
        # see the 2026-09-15 fix note above. A MARKET order has no limit
        # price by definition; any nonzero value the caller passed is
        # discarded (and logged) rather than trusted.
        if price:
            logger.warning(
                "place_order: order_type=MARKET but caller passed nonzero price=%s "
                "— forcing price to 0 (MARKET orders never carry a limit price).",
                price,
            )
        price = 0.0
    elif order_type == "LIMIT" and price:
        _band_tick = tick_size_for_price(price)
        tick_safe_price = round_to_tick(price)
        if tick_safe_price != price:
            logger.info(
                "place_order: rounded LIMIT price ₹%.4f -> ₹%.2f to a valid %.2f tick "
                "(caller did not pre-round)", price, tick_safe_price, _band_tick,
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
                f"₹{_band_tick} tick multiple after rounding. This should be "
                f"unreachable; report as a bug in the caller's price math."
            )

    outbound = {
        "security_id": security_id,
        "exchange_segment": exchange_segment,
        "transaction_type": transaction_type,
        "quantity": quantity,
        "order_type": order_type,
        "product_type": product_type,
        "price": price,
        "validity": validity,
        "tag": tag,
    }
    # Full outbound payload, logged BEFORE the SDK call — this is the
    # exact set of fields this function is asking the SDK to place, so a
    # later "what did we actually ask Dhan for" question never has to be
    # reconstructed from behavior/guesswork again.
    logger.info("place_order: outbound payload -> %s", outbound)

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

    # Best-effort, non-blocking verification: ask Dhan's own order list for
    # what it recorded for the order we just placed, and compare its
    # orderType/price against what we intended. Never raises — the order
    # is already live at the broker regardless of what this finds, so a
    # verification failure must never look like a placement failure to the
    # caller. Logged at CRITICAL (not just warning) because a mismatch
    # here means this service is placing a materially different order
    # than it believes it is, on every occurrence, with real money.
    placed_order_id = str(result.get("orderId") or result.get("order_id") or "")
    if placed_order_id:
        try:
            broker_rows = get_order_list(db)
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
                # its own order list as a "LIMIT" order carrying a computed
                # protection price — this is normal NSE-equity broker
                # behavior (a market order is implemented internally as a
                # protected limit order), not evidence of a wrong order.
                # Both checks below fired on every single market order
                # before this fix, which is exactly the false alarm this
                # service kept raising while the order filled fine.
                _is_expected_market_to_limit_echo = (
                    order_type == "MARKET" and broker_order_type == "LIMIT"
                )
                if (
                    broker_order_type
                    and broker_order_type != order_type
                    and not _is_expected_market_to_limit_echo
                ):
                    logger.critical(
                        "place_order: BROKER ORDER TYPE MISMATCH for order %s (%s %s x%s) — "
                        "sent order_type=%s but Dhan's own order list reports orderType=%s "
                        "(broker price=%s). This service is placing a different order than "
                        "it believes it is — investigate immediately, do not assume this is "
                        "a one-off.",
                        placed_order_id, transaction_type, security_id, quantity,
                        order_type, broker_order_type, broker_price,
                    )
        except Exception as e:  # noqa: BLE001 — verification must never break a real placement
            logger.warning(
                "place_order: post-placement broker verification failed for order %s "
                "(non-fatal, order is already live): %s", placed_order_id, e,
            )

    return result


def cancel_order(db: Session, *, is_armed: bool, dhan_order_id: str) -> dict:
    """Cancel a pending order. Cancelling is always allowed even when NOT armed."""
    client = _get_sdk_client(db)
    logger.info("Cancelling REAL order %s", dhan_order_id)
    resp = client.cancel_order(dhan_order_id)
    return _extract_data(resp) or {}
