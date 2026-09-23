#!/usr/bin/env python3
"""
test_angelone_movers.py — verify the AngelOne gainersLosers wiring
(session19h) end-to-end, in three layers:

  1. market-data-service: GET /angelone/movers directly
  2. api-gateway: GET /market/momentum-movers (calls _get_momentum_movers(),
     which now includes the AngelOne step)
  3. Sanity: does /angelone/movers actually change the size of the combined
     movers set, i.e. is it contributing symbols the other sources don't
     already have?

Usage:
    python3 test_angelone_movers.py \
        --market-data-url https://market-data-service-r6d7.onrender.com \
        --api-gateway-url https://<your-api-gateway-host>

Run this DURING NSE market hours (09:15-15:30 IST, Mon-Fri) for a
meaningful result — outside market hours AngelOne's gainersLosers board
can legitimately be thin/stale, which is not a bug.
"""
import argparse
import json
import sys
import time

import httpx


def check_angelone_movers(market_data_url: str) -> dict:
    print(f"\n[1/3] GET {market_data_url}/angelone/movers")
    try:
        r = httpx.get(f"{market_data_url}/angelone/movers", timeout=20.0)
    except Exception as e:
        print(f"  FAIL — request error: {e}")
        return {}
    print(f"  HTTP {r.status_code}")
    try:
        body = r.json()
    except Exception:
        print(f"  FAIL — non-JSON response: {r.text[:300]}")
        return {}

    status = body.get("status")
    if status == "not_configured":
        print("  STATUS: not_configured — ANGELONE_* env vars are not set "
              "on market-data-service. This is the #1 thing to check if "
              "you expected live data: the container needs "
              "ANGELONE_CLIENT_ID / ANGELONE_MPIN / ANGELONE_API_KEY / "
              "ANGELONE_TOTP_SECRET.")
        return body
    if status == "error":
        print(f"  STATUS: error — {body.get('error')}")
        print("  Common causes: TOTP secret wrong/expired, session login "
              "failing, or a genuine AngelOne-side rate-limit cooldown "
              "(angelone_gainers bucket — check logs for 'AngelOne "
              "gainersLosers' warnings).")
        return body

    data = body.get("data") or []
    print(f"  STATUS: ok — gainers={body.get('gainers_count')} "
          f"losers={body.get('losers_count')} total_rows={len(data)}")
    if data:
        sample = data[:5]
        print(f"  sample rows: {json.dumps(sample, indent=2)}")
    else:
        print("  WARNING: status=ok but 0 rows — either a genuinely flat "
              "market or a silent parsing mismatch (AngelOne response "
              "schema drift). Worth a second run a few minutes later "
              "before treating this as a bug.")
    return body


def check_momentum_movers_route(api_gateway_url: str) -> list:
    print(f"\n[2/3] GET {api_gateway_url}/market/momentum-movers")
    try:
        r = httpx.get(f"{api_gateway_url}/market/momentum-movers", timeout=60.0)
    except Exception as e:
        print(f"  FAIL — request error: {e}")
        return []
    print(f"  HTTP {r.status_code}")
    try:
        body = r.json()
    except Exception:
        print(f"  FAIL — non-JSON response: {r.text[:300]}")
        return []
    symbols = body if isinstance(body, list) else body.get("symbols") or body.get("data") or []
    print(f"  total momentum movers (all sources combined): {len(symbols)}")
    if symbols:
        print(f"  sample: {symbols[:15]}")
    return symbols if isinstance(symbols, list) else []


def cross_check(angelone_body: dict, combined_movers: list) -> None:
    print("\n[3/3] Cross-check: is AngelOne actually contributing symbols?")
    angelone_syms = {
        (row.get("symbol") or "").upper()
        for row in (angelone_body.get("data") or [])
        if isinstance(row, dict)
    }
    combined_set = {s.upper() for s in combined_movers}
    if not angelone_syms:
        print("  SKIP — /angelone/movers returned 0 symbols this run "
              "(see [1/3] above for why).")
        return
    overlap = angelone_syms & combined_set
    print(f"  AngelOne returned {len(angelone_syms)} symbols; "
          f"{len(overlap)} of them are present in the combined "
          f"/market/momentum-movers output.")
    if len(overlap) < len(angelone_syms):
        missing = list(angelone_syms - combined_set)[:10]
        print(f"  NOTE: {len(angelone_syms) - len(overlap)} AngelOne "
              f"symbols did NOT make it into the combined set — expected "
              f"if they failed _clean_equity_symbol() gating (e.g. "
              f"delisted/renamed/index names) or moved <2% by the time "
              f"the two calls ran a few seconds apart. Sample missing: "
              f"{missing}")
    if overlap:
        print("  RESULT: AngelOne wiring is live and contributing.")
    else:
        print("  RESULT: AngelOne returned data but NONE of it reached "
              "the combined output — likely a symbol-cleaning/format "
              "mismatch worth investigating (see _clean_equity_symbol).")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market-data-url", required=True, help="e.g. https://market-data-service-r6d7.onrender.com")
    ap.add_argument("--api-gateway-url", required=True, help="e.g. https://your-api-gateway-host")
    args = ap.parse_args()

    mdu = args.market_data_url.rstrip("/")
    agu = args.api_gateway_url.rstrip("/")

    t0 = time.time()
    angelone_body = check_angelone_movers(mdu)
    combined = check_momentum_movers_route(agu)
    cross_check(angelone_body, combined)
    print(f"\nDone in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
