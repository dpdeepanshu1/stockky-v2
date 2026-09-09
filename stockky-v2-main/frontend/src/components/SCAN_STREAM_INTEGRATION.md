# Progressive scan via `/scan/stream` (NDJSON)

The gateway exposes **`GET /scan/stream`** which yields one JSON object per line
(`application/x-ndjson`). Use this when a full 300-symbol scan would exceed
Render’s ~100s HTTP timeout or freeze the UI while polling `/scan/status`.

## Client helper

`api.scanStream()` in `frontend/src/api.ts` is an async generator:

```ts
const ac = new AbortController();
const rows: any[] = [];
let processed = 0;
let total = 0;

try {
  for await (const row of api.scanStream({ lite: true, signal: ac.signal })) {
    if (row._meta) {
      if (row.event === "feed_bulk_loaded") {
        total = row.total ?? total;
        // optional: show "Neon feed X/Y loaded"
      }
      if (row.event === "done" || row.event === "cancelled") {
        processed = row.processed ?? processed;
        total = row.total ?? total;
        break;
      }
      continue;
    }
    // Normal symbol result
    rows.push(row);
    processed = row._progress?.processed ?? processed + 1;
    total = row._progress?.total ?? total;
    // Update React state incrementally, e.g. setProgress({ processed, total })
  }
} catch (e) {
  if ((e as any)?.name !== "AbortError") throw e;
} finally {
  // build ScanResult-shaped object for existing view
}

// Cancel:
// ac.abort();
```

## Wired into `App.tsx` (default)

Market Scan (**Run lite/full scan**) now prefers **`api.scanStream`**:

1. Opens NDJSON stream with `AbortController`.
2. Updates progress bar + **Live results** list on every symbol.
3. Stop Scan aborts the fetch and keeps partial rows as a `ScanResult`.
4. On stream failure (old gateway), **falls back** to `/scan/start` + poll.

Refresh-resume still uses `/scan/status` for background tasks started via the
fallback path.


## Neon keep-alive

Schedule every ~4 minutes (GitHub Action, cron-job.org, etc.):

```bash
curl -X POST "https://<your-api-gateway>/ops/neon-keepalive"
```

Or from the UI idle path (already pings Neon inside `/ops/idle-tick`).
