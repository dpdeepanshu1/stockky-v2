#!/usr/bin/env python3
"""Round 181 - build the extra company-name list for the event feed from the NSE equity list (all listed companies, ~2000+).
Run INSIDE the container (it has internet, writes to the persistent volume):
  docker compose exec -T analysis-intelligence-service python tools/build_company_names.py
Or from a CSV you downloaded yourself (columns SYMBOL, NAME OF COMPANY):
  docker compose exec -T analysis-intelligence-service python tools/build_company_names.py /app/EQUITY_L.csv
Output: /data/event/names_extra.json ({SYMBOL: ["Company Name"]}); the feed reads it through EVENT_FEED_NAMES_FILE. Restart the service afterwards.
Only SUBSCRIBED symbols are ever tagged, so a big list is safe. Generic or very short names are dropped by the feed itself."""
import csv, io, json, os, sys, urllib.request

OUT = os.getenv("EVENT_FEED_NAMES_FILE", "/data/event/names_extra.json")
URLS = ["https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv", "https://archives.nseindia.com/content/equities/EQUITY_L.csv"]
HDR = {"User-Agent": "Mozilla/5.0", "Accept": "text/csv,*/*", "Referer": "https://www.nseindia.com/"}


def _read():
    if len(sys.argv) > 1:
        return open(sys.argv[1], encoding="utf-8-sig").read()
    last = None
    for u in URLS:
        try:
            return urllib.request.urlopen(urllib.request.Request(u, headers=HDR), timeout=30).read().decode("utf-8-sig")
        except Exception as e:  # noqa: BLE001
            last = e
    raise SystemExit(f"could not download the NSE list ({last}). Download EQUITY_L.csv elsewhere and pass its path as the argument.")


def main():
    rows = list(csv.DictReader(io.StringIO(_read())))
    out = {}
    for r in rows:
        r = {(k or "").strip().upper(): (v or "").strip() for k, v in r.items()}
        sym, name = r.get("SYMBOL"), r.get("NAME OF COMPANY")
        if sym and name:
            out[sym] = [name]
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False)
    print("wrote", len(out), "companies to", OUT)


main()
