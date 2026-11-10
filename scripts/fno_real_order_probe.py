#!/usr/bin/env python3
"""fno_real_order_probe.py - ONE real BUY + SELL of 1 lot of a Nifty (or Sensex) option, every step explained, with the profit / loss and the charges worked out. Thin wrapper: the code
lives in services/fno-options-service/fno/realprobe.py (read its header for the safety rules).

  DRY RUN (default, sends nothing, needs no token):
    MARKET_DATA_URL=http://127.0.0.1:8001 python3 scripts/fno_real_order_probe.py
  LIVE (real money; asks you to type a phrase first; run it at 09:30-14:45 IST on a trading day):
    read -p "Dhan client id: " C; read -s -p "Dhan access token: " T; echo
    DHAN_CLIENT_ID="$C" DHAN_ACCESS_TOKEN="$T" MARKET_DATA_URL=http://127.0.0.1:8001 python3 scripts/fno_real_order_probe.py --live ; unset C T

Writes data/fno_real_probe/probe_<time>.json and .txt (the token is never written; the client id is masked)."""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "services", "fno-options-service"))
from fno import execution, provider, realprobe  # noqa: E402

try:                                                    # round 318: the same holiday list the engine uses (FNO_HOLIDAYS or the built-in 2026 list)
    from config import HOLIDAYS  # noqa: E402
except Exception:  # noqa: BLE001 - the probe still runs without it; the quote-age guard remains
    HOLIDAYS = ()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--und", default="NIFTY", choices=["NIFTY", "SENSEX"])
    ap.add_argument("--side", default="CE", choices=["CE", "PE"])
    ap.add_argument("--offset", type=int, default=0, help="strikes away from ATM (0 = ATM)")
    ap.add_argument("--live", action="store_true", help="really send the orders (asks for a typed confirmation)")
    ap.add_argument("--hold-seconds", type=int, default=20)
    ap.add_argument("--fill-timeout", type=int, default=25, help="seconds to wait for each order to fill before cancelling it")
    ap.add_argument("--max-outlay", type=float, default=15000.0, help="refuse when the premium of 1 lot is above this many rupees")
    ap.add_argument("--expiry", default="", help="YYYY-MM-DD of the expiry to use (default: the nearest one at least 1 day away; the expiry day itself is never used)")
    ap.add_argument("--market-data", default=os.getenv("MARKET_DATA_URL", "http://127.0.0.1:8001"))
    ap.add_argument("--base", default=os.getenv("DHAN_BASE_URL", "https://api.dhan.co/v2"))
    ap.add_argument("--out", default="data/fno_real_probe")
    a = ap.parse_args(argv)
    cid, tok = os.getenv("DHAN_CLIENT_ID", ""), os.getenv("DHAN_ACCESS_TOKEN", "")
    if a.live and not (cid and tok):
        print("--live needs DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in the environment")
        return 2
    if a.live and not sys.stdin.isatty():
        print("--live must be run from an interactive terminal (it asks you to type a confirmation phrase)")
        return 2
    prov = provider.HttpProvider(a.market_data)
    brk = execution.DhanBroker(a.base, cid, tok, 10.0) if (cid and tok) else None
    res = realprobe.run(prov, brk, cid, a.und, a.side, a.offset, a.live, a.hold_seconds, a.fill_timeout, a.max_outlay, a.expiry or None, holidays=tuple(HOLIDAYS))
    if a.live:
        os.makedirs(a.out, exist_ok=True)
        stamp = realprobe.file_stamp()
        body = {k: v for k, v in res.items() if k != "lines"}
        with open(os.path.join(a.out, f"probe_{stamp}.json"), "w", encoding="utf-8") as fh:
            json.dump(body, fh, default=str, indent=1)
        with open(os.path.join(a.out, f"probe_{stamp}.txt"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(res["lines"]) + "\n")
        print(f"\nsaved: {a.out}/probe_{stamp}.json and .txt")
    return int(res.get("exit_code", 2))


if __name__ == "__main__":
    sys.exit(main())
