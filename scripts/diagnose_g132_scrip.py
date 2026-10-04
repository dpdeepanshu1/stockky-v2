"""Read-only: list every AngelOne scrip-master row whose symbol starts with each given name.
Run inside the market-data-service container (see diagnose_group132.sh). Names come in as argv;
none given -> HFCL MTARTECH STLTECH. Prints exch_seg / symbol / name / token / instrumenttype so the
series suffix (-EQ, -BE, -SM ...) is visible. Nothing is written."""
import sys
import time

import httpx

import angelone_scrip_master as m

names = [a.strip().upper() for a in sys.argv[1:] if a.strip()] or ["HFCL", "MTARTECH", "STLTECH"]
want = {n: [] for n in names}
total = nse = eq = 0
t = time.time()
timeout = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
with httpx.stream("GET", m.SCRIP_MASTER_URL, timeout=timeout) as resp:
    print("scrip master HTTP", resp.status_code, "from", m.SCRIP_MASTER_URL)
    resp.raise_for_status()
    for row in m._iter_json_array(resp.iter_text()):
        if not isinstance(row, dict):
            continue
        total += 1
        seg = row.get("exch_seg")
        sym = str(row.get("symbol", ""))
        if seg == "NSE":
            nse += 1
            if sym.endswith("-EQ"):
                eq += 1
        u = sym.upper()
        for n in names:
            if u == n or u.startswith(n + "-") or str(row.get("name", "")).upper() == n:
                want[n].append(row)
print(f"rows={total} NSE={nse} NSE-EQ={eq} ({time.time()-t:.0f}s)")
for n in names:
    rows = want[n]
    print(f"\n== {n}: {len(rows)} row(s)")
    for r in rows[:12]:
        print("  exch_seg=%-4s symbol=%-18s name=%-18s token=%-8s type=%s" % (
            r.get("exch_seg"), r.get("symbol"), r.get("name"), r.get("token"), r.get("instrumenttype") or "-"))
    if not rows:
        print("  (no row at all: not in AngelOne's file; a name/ticker problem, not a series problem)")
    else:
        in_map = any(str(r.get("exch_seg")) == "NSE" and str(r.get("symbol", "")).endswith("-EQ") for r in rows)
        print("  resolves today (NSE + -EQ):", "YES" if in_map else "NO -> only other series/segments exist")
