"""Read-only: show whether the durable last surprise/scan result exists and when it expires.
Run inside the api-gateway container (see diagnose_group132.sh). Writes nothing."""
import datetime as dt
import time

import kv_cache
import surprise_scanner as sc

KEY = sc.SURPRISE_LAST_RESULT_CACHE_KEY
eng = kv_cache._get_neon()
print("durable engine:", "yes" if eng else "NO (memory-only: nothing can survive a restart)")
print("key durable   :", kv_cache._is_durable(KEY))
print("save TTL (s)  :", sc._last_result_durable_ttl_sec(sc.SURPRISE_CACHE_MAX_AGE_SEC),
      "(group 132 expects 604800 = 7 days)")
if eng:
    from sqlalchemy import text
    with eng.connect() as c:
        row = c.execute(text("SELECT expires_at FROM stockky_kv WHERE k = :k"), {"k": KEY}).fetchone()
    if not row:
        print("row in DB     : ABSENT (purged or never saved: the next scan(cached) after a boot saves it)")
    else:
        exp = row[0]
        if exp is not None:
            if getattr(exp, "tzinfo", None) is None:
                exp = exp.replace(tzinfo=dt.timezone.utc)
            left = (exp - dt.datetime.now(dt.timezone.utc)).total_seconds()
            print(f"row in DB     : present, expires {exp.isoformat()} ({left/86400:.2f} days from now)")
        else:
            print("row in DB     : present, no expiry")
p = kv_cache.get_stale(KEY)
if isinstance(p, dict) and p.get("scan_ts"):
    age = time.time() - float(p["scan_ts"])
    n = (p.get("result") or {}).get("count")
    print(f"stale read    : OK, scan is {age/3600:.1f} h old, count={n} "
          f"(served as fresh only if <= {sc.SURPRISE_CACHE_MAX_AGE_SEC:.0f}s old)")
else:
    print("stale read    : nothing restorable")
