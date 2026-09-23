# Stockky — this patch: what changed, why, and what's still on you

## 1. Root cause of the price-repair / IPO-scan bugs

Your log's `ERROR:yfinance:HTTP Error 401 {"error":{"code":"Unauthorized","description":"Invalid Crumb"}}`
is the single root cause behind **two** symptoms you reported:

- **Data Feed Health → Repair does nothing, price stays "0 (missing)" forever.**
  `market-data-service`'s price waterfall was Yahoo → TwelveData → AlphaVantage → Polygon.
  When Yahoo's crumb/cookie flow breaks, TwelveData 404s on NSE symbols (not on your plan) and
  Polygon has **no India coverage at all** — so every fallback also failed silently. Unlike
  RSI/PE/ROCE/sentiment (which already had baseline-seed fallbacks), **price had no fallback**,
  so it stayed at 0 no matter how many times you clicked Repair.
- **IPO scan shows "error" for newly-listed stocks.** `ipo_scanner.py`'s `_fetch_history()` called
  `yfinance` directly, bypassing every bit of hardening `market-data-service` has. One Yahoo
  hiccup = instant `"stage": "error"`.

**Important honesty check:** the *reason* Yahoo is returning "Invalid Crumb" is very likely that
Yahoo is fingerprinting/blocking your Render/Oracle datacenter IP, not a stale yfinance version
(you're already on `yfinance==1.5.2`, which is current). No amount of code can force Yahoo to
issue a valid crumb to a blocked IP — that's an anti-bot decision on Yahoo's end, and can flip
on/off over time on its own. So this patch doesn't "fix Yahoo" — it makes the rest of the app
**not depend on Yahoo being healthy**.

### What this patch actually does about it
- Added `_waterfall_nse_direct_price()` in `market-data-service/main.py` — hits NSE's own
  `nseindia.com/api/quote-equity` endpoint directly. Free, no API key, no quota, and on a
  completely different auth path than Yahoo's crumb system, so it keeps working through a
  Yahoo outage. Wired in as the 2nd rung of both `get_realtime_price()` and `/quote/{symbol}`.
- Added `_waterfall_bhavcopy_price()` / `eod_close_from_bhavcopy()` — absolute last resort,
  pulls yesterday's official NSE bhavcopy closing price. Not live, but a real EOD close is
  infinitely better than a record stuck at `0` forever. Cached with a short (120s) TTL so a
  live source can override it the moment one succeeds again.
- `ipo_scanner.py._fetch_history()` now calls `market-data-service`'s `/history/{symbol}`
  endpoint (shared caching, retries, index-fallback candidates) instead of raw `yfinance`,
  with a direct-yfinance call only as a last-ditch fallback if market-data-service itself is
  unreachable.

**NSE's site can also rate-limit/block scripted traffic**, especially from cloud IPs, so this
isn't a 100% guarantee either — but it's a second, independent path that doesn't share Yahoo's
failure mode, and the bhavcopy rung means price should now basically never stay at literal `0`.

### What I'd still recommend if 401s persist
TwelveData and Polygon free tiers genuinely don't cover NSE — that's a plan limitation, not a
bug. If you want a paid, reliable, India-covering source as a true primary (rather than a
scraped-endpoint fallback), consider a provider with explicit NSE/BSE support (e.g. Upstox/
Kite Connect if you already have a trading account, or a paid IndianAPI/Finnhub-style feed).
Happy to wire one in if you get an API key.

## 2. New: "Refill All" bulk repair (Data Feed Health page)

- Backend: `POST /api/feed/repair-all` starts a background job that walks **every** incomplete
  record (not just the next 15), in batches of 5 with pacing between batches so it stays inside
  upstream rate limits over a long run. `GET /api/feed/repair-all/status` for progress,
  `POST /api/feed/repair-all/stop` to cancel mid-run.
- Frontend: new "🚀 Refill All (N)" button next to "Auto-Repair Next 15" on the Data Feed Health
  card, with a live progress bar and a Stop button while it's running.

## 3. New: "Wake DB" (Neon / training / cache)

- Backend: `GET|POST /ops/wake-db-all` pings the gateway's own Neon connection **and** the
  training service's `/health` (which itself touches whichever DB `TRAINING_DATABASE_URL` /
  `DATABASE_URL` / `CACHE_DATABASE_URL` resolves to on that service).
- Frontend:
  - Fires automatically once when the app loads (`App.tsx`, alongside the existing session
    keep-alive).
  - Manual button in the mobile top bar (🔌DB / ⏳DB / ✅DB / ⚠️DB icon, top-right) and the
    desktop status strip ("WAKE DB" pill).
  - Same control duplicated in **Settings → Databases** section.

## 4. Oracle Cloud: "site reloads every ~5s of inactivity"

I went through the whole frontend and there is **no 5-second reload/refresh timer anywhere in
this codebase** — the only `window.location.reload()` calls are in the explicit "Power Off"
button and a Training-tab action, both user-triggered. So this is almost certainly happening
**outside this repo**, on the Oracle VM's reverse-proxy / process layer. Two most likely causes,
in order of likelihood:

1. **You're serving the Vite dev server (`npm run dev` / `vite`) in production on the Oracle
   side instead of a built, static bundle.** Vite's dev server keeps an HMR (hot-module-reload)
   WebSocket open to the browser. If your reverse proxy (Nginx/Caddy on the Oracle VM) doesn't
   forward the `Upgrade`/`Connection` headers correctly, or has a short idle/read timeout, that
   WebSocket gets killed after a few seconds of inactivity. Vite's HMR client detects the drop
   and — depending on version/config — falls back to a full `location.reload()` on
   reconnect-failure. **Fix:** run `npm run build` and serve `frontend/dist` as static files
   (same as you'd do on Render) — never run the dev server in production.
2. **If you are already serving a static build**, check the Nginx/Caddy config on the Oracle VM
   for `proxy_read_timeout` / `keepalive_timeout` set very low (a few seconds). Combine that with
   a `Connection: close` header and some clients (or an aggressive CDN/edge cache) will treat any
   idle gap as "connection reset → reload page". Bumping these timeouts to 60s+ should stop it.

I don't have access to the Oracle VM's Nginx/systemd/Docker config from here (it isn't in this
zip), so I can't patch it directly — if you paste your Nginx site config or `docker-compose.yml`
for the Oracle side, I can point at the exact line.

## 5. Oracle Autonomous AI Database as a DB replacement — needs a real scoping pass, not a config flip

I looked at how this app talks to Postgres/Neon (`kv_cache.py`, `models.py` on both
`api-gateway` and the training service) before touching anything here, and I want to be upfront
rather than hand you something that looks done but silently breaks in production:

**This is not an env-var swap.** The data layer uses Postgres-specific SQL throughout —
`INSERT ... ON CONFLICT (k) DO UPDATE ...` (upsert), `NOW()`, and psycopg2-specific SSL param
handling — none of which exist in Oracle's SQL dialect. Oracle Autonomous DB also needs:
- the `oracledb` Python driver instead of `psycopg2`/`asyncpg`,
- the wallet you already downloaded (screenshot 3) wired into the connection (TNS name +
  `wallet_location`, not a plain `postgresql://` URL),
- every `ON CONFLICT` rewritten as Oracle `MERGE INTO ... WHEN MATCHED / WHEN NOT MATCHED`,
- and a re-test of every code path that touches the DB (kv_cache get/set, training models,
  trades, evaluate, feature store) since Oracle's type system and transaction behavior differ
  from Postgres in ways that can silently corrupt data if missed.

That's realistically its own focused task (roughly a day of careful work + testing), not
something to bolt on inside a bundle of five other fixes. I'd rather scope and do that properly
in a follow-up than ship a half-converted DB layer that looks like it works and then loses data.

**What I'd suggest instead, if the goal is "backup-production stays up if Render/Neon goes
down":** keep Postgres as the DB engine (Oracle has no Postgres-compatible mode — that's an
AWS/Google feature, not Oracle's), and instead run a **second free Postgres** (Neon has multiple
projects on the free tier, or Supabase) for the Oracle-side deployment, so both `main` and
`backup-production` speak the exact same SQL dialect your code already uses. That gets you real
redundancy today with zero code changes, and Oracle Autonomous DB can come later as a proper
migration project if you still want it long-term.

## 6. Same code on Render (main branch) + Oracle Cloud (backup-production) simultaneously

Both branches running the **same application code** is already how this is set up — nothing in
this patch is Render-specific or Oracle-specific; every fix above lives in `services/` and
`frontend/`, which both deployments already build from. What differs between the two deployments
is purely **environment variables** (`DATABASE_URL`, `MARKET_DATA_URL`, etc.), not code, so:
- Merge this patch into both `main` and `backup-production`.
- Keep each deployment's env vars pointed at its own DB (see §5 — for now, that should be two
  separate Postgres instances, not one Postgres + one Oracle, until the Oracle migration is
  actually done).
- I can't create the GitHub branch, push commits, or touch your Render/Oracle dashboards from
  here — I don't have credentials to your accounts. The zip below has the changed files with
  their exact repo-relative paths; drop them into both branches (or cherry-pick one commit onto
  both) and redeploy each independently.

"Optimize Render to handle all task at it done at qstash-side but no unnecessary workflow" — I
didn't touch the QStash/cron wiring in this pass (out of scope for the bugs you flagged, and I'd
rather not touch scheduling logic blind without knowing which of your current cron jobs you
actually rely on). If you want that trimmed, tell me which jobs in `/ops/qstash/*` and the
scheduler service you actually need and I'll cut the rest.

## Files changed in this patch
```
services/market-data-service/bhavcopy.py     (bhavcopy close-price parsing + eod_close_from_bhavcopy)
services/market-data-service/main.py         (NSE-direct + bhavcopy price waterfall stages)
services/api-gateway/ipo_scanner.py          (route history fetch through market-data-service)
services/api-gateway/main.py                 (Refill All job + Wake DB endpoint)
frontend/src/api.ts                          (repairFeedAll*, wakeAllDatabases)
frontend/src/App.tsx                         (auto DB-wake on load, Wake DB buttons x2)
frontend/src/components/DataHealthAudit.tsx  (Refill All button + progress UI)
frontend/src/index.css                       (.scan-action-danger style)
```
