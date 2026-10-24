"""fo_download.py - E73 (D24): fetch the two F&O files the training features need. OFFLINE tool, run by hand on the VM; nothing in the live services imports it.

    python fo_download.py --from 2026-09-01 --to 2026-10-22 --out fo_data            # both files
    python fo_download.py --from 2026-09-01 --to 2026-10-22 --out fo_data --only ban

Writes (skips a file that already exists, so it can be re-run to fill gaps):
    fo_data/fo_secban_YYYYMMDD.csv   the exchange's "securities in F&O ban" list, saved as downloaded (an empty list is still a file = that day is COVERED)
    fo_data/fo_oi_YYYYMMDD.csv       symbol,open_interest - stock-futures open interest per symbol from that day's F&O bhavcopy (parse_fo_bhavcopy_oi)
which training/ml_wiring.py reads through `ml_extras.fo.ban_dir` / `oi_dir`.

STATUS: the parsing is unit-tested on synthetic files (tests/test_e73_ml_wiring.py); the URLs below are the exchange's public archive paths as last known and were NOT
exercised from the authoring sandbox (no route to the exchange). If a path has moved, the run prints it as "failed" and writes nothing for that day - fix the two URL
templates, nothing else. The exchange needs a browser-like User-Agent and a cookie from its home page; both are handled. Be polite: one request per second, no parallelism.
A 404 on a weekday is normally a market holiday, not an error.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import io
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import date, datetime, timedelta
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fo_features import parse_fo_bhavcopy_oi  # noqa: E402

BAN_URL = "https://nsearchives.nseindia.com/archives/fo/sec_ban/fo_secban_{ddmmyyyy}.csv"
BHAV_URL = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{yyyymmdd}_F_0000.csv.zip"
HOME = "https://www.nseindia.com/"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def _opener() -> urllib.request.OpenerDirector:
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.addheaders = [("User-Agent", UA), ("Accept", "*/*"), ("Accept-Language", "en-US,en;q=0.9"), ("Referer", HOME)]
    try:
        op.open(HOME, timeout=15).read(1)       # cookie warm-up; failure is fine, the archive host often does not need it
    except Exception:  # noqa: BLE001
        pass
    return op


def _get(op, url: str, retries: int = 2) -> Optional[bytes]:
    """bytes, or None for 404 (holiday / not published) and for any failure after the retries."""
    err = "?"
    for attempt in range(retries + 1):
        try:
            with op.open(url, timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            err = f"HTTP {e.code}"
        except Exception as e:  # noqa: BLE001
            err = type(e).__name__
        if attempt < retries:
            time.sleep(2.0 * (attempt + 1))
    print(f"  failed: {url} ({err})")
    return None


def bhavcopy_oi_csv(zip_bytes: bytes) -> Optional[str]:
    """`symbol,open_interest` CSV text from a downloaded F&O bhavcopy zip (or plain csv) - None when it holds no stock futures."""
    try:
        if zip_bytes[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
                name = next((n for n in z.namelist() if n.lower().endswith(".csv")), None)
                if not name:
                    return None
                body = z.read(name)
        else:
            body = zip_bytes
        df = parse_fo_bhavcopy_oi(body)
    except Exception:  # noqa: BLE001
        return None
    return None if df.empty else df.to_csv(index=False)


def weekdays(d0: date, d1: date):
    d = d0
    while d <= d1:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def run(d0: date, d1: date, out: str, only: Optional[str] = None, pause: float = 1.0) -> dict:
    os.makedirs(out, exist_ok=True)
    op = None
    res = {"ban": 0, "oi": 0, "skipped": 0, "missing": 0}
    for d in weekdays(d0, d1):
        jobs = []
        if only in (None, "ban"):
            jobs.append(("ban", os.path.join(out, f"fo_secban_{d:%Y%m%d}.csv")))
        if only in (None, "oi"):
            jobs.append(("oi", os.path.join(out, f"fo_oi_{d:%Y%m%d}.csv")))
        for kind, path in jobs:
            if os.path.exists(path):
                res["skipped"] += 1
                continue
            op = op or _opener()
            if kind == "ban":
                raw = _get(op, BAN_URL.format(ddmmyyyy=f"{d:%d%m%Y}"))
                text = None if raw is None else raw.decode("utf-8", errors="replace")
            else:
                raw = _get(op, BHAV_URL.format(yyyymmdd=f"{d:%Y%m%d}"))
                text = None if raw is None else bhavcopy_oi_csv(raw)
            time.sleep(pause)
            if text is None:
                res["missing"] += 1
                continue
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
            res[kind] += 1
            print(f"  wrote {path}")
    print(f"done: ban files {res['ban']}, oi files {res['oi']}, already there {res['skipped']}, no file (holiday / failed) {res['missing']}")
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Download F&O ban lists and stock-futures open interest for training.")
    ap.add_argument("--from", dest="d0", required=True, help="first day, YYYY-MM-DD")
    ap.add_argument("--to", dest="d1", required=True, help="last day, YYYY-MM-DD")
    ap.add_argument("--out", default="fo_data")
    ap.add_argument("--only", choices=("ban", "oi"))
    ap.add_argument("--pause", type=float, default=1.0, help="seconds between requests")
    a = ap.parse_args(argv)
    d0, d1 = datetime.strptime(a.d0, "%Y-%m-%d").date(), datetime.strptime(a.d1, "%Y-%m-%d").date()
    if d1 < d0:
        ap.error("--to is before --from")
    run(d0, d1, a.out, a.only, a.pause)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
