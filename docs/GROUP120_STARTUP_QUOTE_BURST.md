# group120 (2026-10-04) - startup quote burst

Cumulative on group119. Run the api-gateway suite on the VM.

## Cause
`main.py::_warm_surprise_scan_cache` called `surprise_engine.scan()` with no `cached` flag as soon as the process started, so every boot (and every quick restart) swept one `/quote` call per liquid static symbol (~1,000, 25 at a time) while market-data-service was still starting and the momentum/universe warms ran beside it.

## What changed
- New env `SURPRISE_BOOT_WARM_DELAY_SEC` (default 20; blank-safe; 0 restores the old immediate start).
- Market phase `closed`/`holiday` and a saved durable result exists: restore it and skip the sweep. (The cached fast path only honours results younger than `SURPRISE_CACHE_MAX_AGE_SEC`=220s, so an overnight warm could never serve the next morning's first cycle.)
- Otherwise `scan(cached=True)`: a restart shortly after a good scan reuses the saved result; an old or missing one still triggers the full warm sweep.
- `preopen`, `open` and `post` phases still warm, so a restart at 09:05 is ready for the 09:15 cycle.

## Not changed
The momentum-movers / scan-universe warm (it has its own cache) and market-data-service's own startup hooks.
