# Session 49 — ORDER_FAILED root-cause fixes + tick hook registration

## Issues fixed

### 1. `on_tick_hook` never registered → tick activity count always 0

**Root cause**: `screening/engine.py` defines `on_tick_hook()` which updates
`_volume_accum` (the tick-activity counter used by `scan()` as a volume proxy),
but it was **never** passed to `ws_client.register_on_tick()`. So `_volume_accum`
stayed 0 for every symbol across the entire service lifetime.

The volume floor check in `scan()` is:
```
if tick_count < max(1, int(MIN_AVG_VOLUME / 5000)): continue
```
With `tick_count=0` this filters ALL symbols when `MIN_AVG_VOLUME > 0`.

**Fix** (`main.py`): register `on_tick_hook` at startup, right after `ws_client.start()`.

**Fix** (`screening/engine.py`): when `MIN_AVG_VOLUME=0`, skip the tick-floor filter
entirely (set it to 0, not 1, so operators can disable the activity gate via env).

### 2. `ORDER_FAILED: For BUY: targetPrice must be > price` + `Invalid Price for orderType`

**Root cause A** (targetPrice must be > price — SDK ValueError):
`dhanhq>=2.0.2` in requirements.txt could resolve any SDK version. On SDK builds
where `dhan_http` is not exposed (pre-2.2.0), the MARKET bypass in
`place_super_order()` raises RuntimeError ("upgrade dhanhq"). But the more common
failure mode: the SDK's `place_super_order()` local validation runs
`if not all([..., price])` — price=None/0 fails → ValueError before any HTTP call.
This fires when `tick_size_for_price` rounding collapses `target_price` to equal
or below `current_ltp` on very low-priced stocks.

**Root cause B** (Invalid Price for orderType — Dhan server rejection):
Even with the MARKET bypass via `dhan_http.post()`, Dhan's server rejects the
request if `targetPrice` or `stopLossPrice` are outside valid bounds.

**Fix** (`requirements.txt`): pin `dhanhq>=2.2.0` so `dhan_http` is always
available for the MARKET bypass.

**Fix** (`orders/adaptive.py`): add Step 6 sanity clamp — after tick-rounding,
ensure `target_price > current_ltp` and `stop_price < current_ltp` by bumping
exactly 1 tick if rounding collapsed them. This is safe for MARKET entries
(Dhan fills at market regardless; target/stop are the leg prices, not the entry).

**Fix** (`execution/dhan_client.py`): add the same guard inside `place_super_order()`
MARKET branch — if `target_price <= ref_price` after rounding, bump 1 tick before
posting to Dhan's HTTP endpoint. Drop the `"price": None` key entirely from the
payload rather than sending `null`, since some Dhan gateway versions reject explicit
null values for MARKET orders.

### 3. `LivePipelineStatus` showed "Idle" with nothing when not running

**Fix** (`PositionStocksTab.tsx`): when idle, show the last completed cycle's
summary + scan-stage candidate chips from `live.last_cycle`, so "Idle" always
shows what the most recent AUTO tick found (or didn't find), not just a blank card.

### 4. Added `scripts/healthcheck.sh`

Ubuntu health-check script covering:
- Docker container status
- `GET /health`, `/status`, `/ws-status`, `/pipeline/status`, `/candidates`
- Last 5 candidate log rows (to see SKIPPED reasons live)
- Recent docker logs for both services
- Usage: `bash scripts/healthcheck.sh [position-stocks|real-trade|all]`

## Data-source clarification (Angel One vs Dhan)

- **Angel One WS** (feed/ws_client.py + feed/angelone_session.py): ALL live price
  data — ticks go into `_tick_buffers`, screening engine reads them for rolling
  pct-change, volume proxy, ATR proxy, range position. Never used for orders.
- **Dhan SDK / HTTP** (execution/dhan_client.py): ONLY for order placement (BUY/SELL
  super orders, EOD flat-SELL plain orders), order status queries (super order list,
  plain order list), and funds. Never fetches prices.
  
This split was already correct in the code; this session adds no change to it.

## Files changed

- `services/position-stocks-service/requirements.txt` — dhanhq>=2.2.0
- `services/position-stocks-service/main.py` — register on_tick_hook at startup
- `services/position-stocks-service/screening/engine.py` — fix volume floor check
- `services/position-stocks-service/orders/adaptive.py` — Step 6 sanity clamp
- `services/position-stocks-service/execution/dhan_client.py` — MARKET payload hardening
- `frontend/src/components/PositionStocksTab.tsx` — LivePipelineStatus idle state
- `scripts/healthcheck.sh` — new Ubuntu health-check script
