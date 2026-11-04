#!/usr/bin/env python3
"""label_headlines.py - build and score a real-headline test set for event_lite (round 173).

  python3 scripts/label_headlines.py collect NEWS_URL SYM [SYM ...] [-o headlines.csv]
        reads the headlines the news service already returns for each symbol (GET NEWS_URL/news/analyze/<SYM>) and writes a CSV
        with the offline classifier's guess in `pred_type`, `pred_pol`, `pred_conf` and EMPTY `gold_type`, `gold_pol` for you to fill.
  python3 scripts/label_headlines.py from-text FILE.txt [-o headlines.csv]      # one headline per line instead of the service
  python3 scripts/label_headlines.py score headlines.csv                       # accuracy on the rows you labelled (gold_pol -1/0/1, gold_type optional)

Label BEFORE you read the prediction columns (hide them in your sheet) or the set is no longer blind. Standard library only.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "services" / "analysis-intelligence-service"))
import event_lite  # noqa: E402

FIELDS = ["headline", "gold_pol", "gold_type", "pred_pol", "pred_type", "pred_conf", "symbol"]


def _row(headline: str, symbol: str = "") -> dict:
    r = event_lite.classify(headline)
    return {"headline": headline, "gold_pol": "", "gold_type": "", "pred_pol": r["pol"], "pred_type": r["type"],
            "pred_conf": r["confidence"], "symbol": symbol}


def _write(rows, out: str) -> int:
    seen, uniq = set(), []
    for r in rows:
        k = r["headline"].strip().lower()
        if k and k not in seen:
            seen.add(k)
            uniq.append(r)
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(uniq)
    return len(uniq)


def collect(base: str, symbols, out: str, fetch=None) -> int:
    fetch = fetch or (lambda url: json.load(urllib.request.urlopen(url, timeout=30)))
    rows = []
    for s in symbols:
        try:
            data = fetch(f"{base.rstrip('/')}/news/analyze/{s}")
        except Exception as e:  # noqa: BLE001
            print(f"{s}: skipped ({e})", file=sys.stderr)
            continue
        for h in data.get("headlines") or []:
            rows.append(_row(str(h.get("title") or ""), s))
    return _write(rows, out)


def from_text(path: str, out: str) -> int:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return _write([_row(x.strip()) for x in lines if x.strip()], out)


def score(path: str) -> dict:
    n = pol_ok = typ_n = typ_ok = 0
    wrong = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            g = (r.get("gold_pol") or "").strip()
            if g not in ("-1", "0", "1"):
                continue
            res = event_lite.classify(r["headline"])
            n += 1
            if res["pol"] == int(g):
                pol_ok += 1
            else:
                wrong.append((r["headline"][:80], int(g), res["pol"]))
            gt = (r.get("gold_type") or "").strip()
            if gt:
                typ_n += 1
                typ_ok += res["type"] == gt
    return {"labelled": n, "pol_acc": round(pol_ok / n, 3) if n else None,
            "type_labelled": typ_n, "type_acc": round(typ_ok / typ_n, 3) if typ_n else None, "wrong_pol": wrong}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect"); c.add_argument("url"); c.add_argument("symbols", nargs="+"); c.add_argument("-o", "--out", default="headlines.csv")
    t = sub.add_parser("from-text"); t.add_argument("file"); t.add_argument("-o", "--out", default="headlines.csv")
    s = sub.add_parser("score"); s.add_argument("csv")
    a = ap.parse_args(argv)
    if a.cmd == "collect":
        print(f"wrote {collect(a.url, a.symbols, a.out)} headlines to {a.out} - fill gold_pol (-1/0/1) and gold_type, then run: score {a.out}")
    elif a.cmd == "from-text":
        print(f"wrote {from_text(a.file, a.out)} headlines to {a.out}")
    else:
        r = score(a.csv)
        print(json.dumps({k: v for k, v in r.items() if k != "wrong_pol"}))
        for h, g, p in r["wrong_pol"]:
            print(f"  gold {g:+d} pred {p:+d}  {h}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
