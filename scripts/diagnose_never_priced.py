"""Read-only probe, run INSIDE the market-data-service container (see diagnose_never_priced.sh).

For each symbol given as argv (default: STEAMHOUSE SGRL KENNAMET ELEVATE ROSSTECH) it asks this service, over localhost,
for GET /quote/<SYM> and GET /last-close/<SYM> and prints HTTP status, elapsed seconds and the fields that say WHY there
is no price (source, price, negative-cache marker, error text). A /quote that takes many seconds is the waterfall
(Yahoo, NSE, AngelOne REST, paid APIs ...) walking a symbol that has no price anywhere; `negative_cache` means group 161's
\"no price\" memory is answering. Nothing is written and no secrets are printed.
Env: MD_URL (default http://127.0.0.1:8001), PROBE_TIMEOUT_S (default 25).
"""
import os
import sys
import time

import httpx

BASE = (os.environ.get("MD_URL") or "").strip() or "http://127.0.0.1:8001"
try:
    TIMEOUT = float((os.environ.get("PROBE_TIMEOUT_S") or "").strip() or "25")
except ValueError:
    TIMEOUT = 25.0
DEFAULT_NAMES = ["STEAMHOUSE", "SGRL", "KENNAMET", "ELEVATE", "ROSSTECH"]
SHOW_KEYS = ("price", "source", "currency", "fetched_at", "stale", "stale_served", "error", "detail", "message", "reason")


def probe(client, path):
    t = time.time()
    try:
        r = client.get(BASE + path)
    except httpx.TimeoutException:
        return None, time.time() - t, "TIMEOUT after %.0fs" % TIMEOUT
    except Exception as e:  # connection refused etc.
        return None, time.time() - t, "%s: %s" % (type(e).__name__, str(e)[:120])
    try:
        body = r.json()
    except Exception:
        body = None
    return r.status_code, time.time() - t, body if body is not None else r.text[:160]


def summarise(body):
    if isinstance(body, dict):
        parts = ["%s=%r" % (k, body[k]) for k in SHOW_KEYS if k in body and body[k] not in (None, "")]
        if "price" in body and body["price"] is None:
            parts.insert(0, "price=null")
        return ", ".join(parts)[:300] or "(empty object)"
    return str(body)[:300]


def classify(status, elapsed, body):
    """One-line reading of a /quote answer."""
    if status is None:
        return "NO ANSWER (%s)" % body
    if status == 404:
        return "404 - the service says this symbol is delisted / unknown"
    if isinstance(body, dict):
        if body.get("source") == "negative_cache":
            return "NO PRICE, answered by the group 161 negative cache (fast, no waterfall)"
        if body.get("price") in (None, 0):
            return "NO PRICE after a full waterfall (%.1fs)" % elapsed if elapsed > 3 else "NO PRICE (fast)"
        return "PRICED from %s" % body.get("source")
    return "unrecognised answer"


def main():
    names = [a.strip().upper() for a in sys.argv[1:] if a.strip()] or DEFAULT_NAMES
    print("market-data base:", BASE, "timeout:", TIMEOUT, "s")
    with httpx.Client(timeout=TIMEOUT) as client:
        for n in names:
            print("\n== %s" % n)
            for label, path in (("quote #1", "/quote/%s" % n), ("quote #2", "/quote/%s" % n), ("last-close", "/last-close/%s" % n)):
                status, elapsed, body = probe(client, path)
                print("  %-10s HTTP %-4s %5.1fs  %s" % (label, status if status is not None else "-", elapsed, summarise(body)))
                if label.startswith("quote"):
                    print("             -> %s" % classify(status, elapsed, body))
    print("\n(quote #2 follows #1 at once: after 2 full failures group 161 answers from its negative cache.)")


if __name__ == "__main__":
    main()
