# Stockky v2 — What changed, and exactly how to deploy it

16 files. Every one goes to the **same path** on both branches (`main` → Render+Neon,
`backup-production` → Oracle VM + Oracle ADB). There is no separate Oracle copy of
anything — the code decides at runtime by reading `ORACLE_DSN`.

Two brand-new files, fourteen edited:

```
frontend/src/App.tsx                                        edited
frontend/src/api.ts                                         edited
frontend/src/components/IpoTracker.tsx                      NEW
frontend/src/components/HotStocks.tsx                       edited
frontend/src/components/SurpriseStocks.tsx                  edited
services/api-gateway/main.py                                edited
services/api-gateway/rate_limiter.py                        edited
services/api-gateway/symbol_aliases.py                      edited
services/api-gateway/ipo_scanner.py                         edited
services/api-gateway/ipo_schema.py                          edited
services/api-gateway/hotpicks_schema.py                     NEW
services/api-gateway/hotpicks_store.py                      NEW
services/api-gateway/data_feed.py                           edited
services/api-gateway/oracle_compat.py                       edited
services/market-data-service/main.py                        edited
services/decision-prediction-service/prediction/requirements.txt   edited
```

---

## Step 1 — Copy the files in

Unzip, then copy the whole tree over your repo root. The folder structure inside the
zip already matches the repo, so from the unzipped folder:

```bash
cp -r stockky-v2-changes/. /path/to/stockky-v2-main/
```

Do this **once on `main`** and **once on `backup-production`** (or copy on one branch,
commit, then cherry-pick / merge into the other). Same bytes both times.

## Step 2 — Set the new environment variables

Every one of these has a working default, so **if you set nothing at all, everything
still runs**. Set them only when you want to change the behaviour described.

### On Render (api-gateway service → Environment)

| Key | Value to use | What it does |
|---|---|---|
| `AUDIT_TTL_SEC` | `20` | How many seconds the DB-health numbers are reused before recounting. Raise to `60` if the DB Health tab still feels slow; set `0` to disable caching. |
| `HOTPICKS_AUDIT_TTL_SEC` | `20` | Same idea, for the Hot Picks feed-health panel. |
| `HOTPICKS_DB_FRESH_HOURS` | `24` | A stored Hot Picks scan newer than this counts as fresh, so the tab paints from the table instead of rescanning. |
| `HOTPICKS_RETENTION_HOURS` | `72` | Stored picks older than this get deleted after each scan. |
| `HOTPICKS_TABLE_HOURS` | `24` | Default window the 24h table shows. |
| `RL_MAX_WAIT_SEC` | `5` | Longest a call waits for rate-limit tokens. **This is the fix for the `max_wait exceeded, proceeding anyway` spam** — it used to be 20. |
| `RL_INTERACTIVE_RESERVE` | `0.34` | Fraction of tokens background scans may never touch, so a single-symbol lookup is never starved by a running scan. |
| `RL_RENAME_DISCOVERY` | `1` | Turns on automatic NSE-rename discovery. Set `0` to disable. |
| `RL_DISCOVERY_AT_STREAK` | `2` | After this many failures for one symbol, look up whether it was renamed. |
| `RL_SKIP_HIGH_PRICE` | *(leave unset)* | Only set to `1` if you want ₹5000+ names skipped everywhere, including manual search. Off by default on purpose. |
| `YF_TZ_CACHE_DIR` | `/tmp/yfinance_tz` | Kills the `Failed to create TzCache folder` warning. |
| `SCAN_UNIVERSE_TARGET` | `500` | You already have this. Two places that were hardcoded to 300/400 now follow it. |

### On the Oracle VM (`.env` next to docker-compose)

Add **exactly the same keys with the same values** as above, plus keep your existing
Oracle block unchanged:

```
ORACLE_DSN=stockkydb_high
ORACLE_USER=ADMIN
ORACLE_PASSWORD=<your password>
ORACLE_WALLET_DIR=/opt/stockky/wallet
ORACLE_WALLET_PASSWORD=<your wallet password>
```

Optional Oracle-only tuning, all with safe defaults:

| Key | Value | What it does |
|---|---|---|
| `HOTPICKS_DB_POOL_SIZE` | `2` | Connections in the Hot Picks pool. |
| `HOTPICKS_JSON_MAX_BYTES` | `30000` | Must stay **below 32767**. See the ORA-01461 note below. |
| `IPO_JSON_MAX_BYTES` | `30000` | Same ceiling, for the IPO table. |

**Important:** leave `DATABASE_URL` alone on the Oracle VM even if it still points at
Neon. `ORACLE_DSN` wins. This is asserted by an automated check, not assumed.

## Step 3 — Rebuild

Render redeploys on push by itself. On the Oracle VM:

```bash
cd /opt/stockky
docker compose build --no-cache api-gateway market-data-service decision-prediction-service
docker compose up -d
```

`--no-cache` matters. A cached layer will silently keep the old
`prediction/requirements.txt` and the pickle-version warnings will come straight back.

## Step 4 — Check it worked

1. **IPO Tracker** — a new left-nav entry. Scan, Stop, the 30-day/365-day toggle and a
   data-health panel are all there. Open the Surprise tab: the IPO section is **gone**
   from it (moved, not copied).
2. **Hot Picks** — press Scan. The remaining-time now counts down against the real
   universe size instead of a fake 100. Reload the page mid-scan: it reconnects to the
   running job instead of showing an empty tab. Press Stop: partial results are kept.
3. **Surprise** — there is now a red **⏹ Stop** button next to Refresh Scan. It is
   greyed out when nothing is running.
4. **Logs** — `max_wait exceeded` should appear at most once per 30 seconds instead of
   ten times in a row, and the TzCache and pickle-version warnings should be gone.

---

## What each fix actually was

**Hot Picks remaining-time was wrong** — three separate bugs stacked. `/status`
replayed numbers frozen at the last write; `/run` invented progress (`total=100`,
processed jumping 0→10→30→90); and the frontend defaulted `total` to 100 and refused
to resume polling after a reload. All three are fixed, so the ETA is now derived from
the real symbol count and real position.

**Rate limiter got stuck** — the old logic was "wait 20 seconds, then proceed anyway",
which is the worst of both worlds: you pay the full stall *and* still make the call the
drained bucket said was unsafe, so Yahoo's own 429 backoff piles on top. Now the budget
is 5s, divided by how many callers are queued, and it genuinely gives up instead of
proceeding. Warnings are throttled to one per 30s per bucket.

**"Symbol not found" was slow** — the failure-streak plumbing in `symbol_aliases.py`
existed but **nothing ever called it**, so the counter never moved and every scan paid
full price for the same dead tickers. It is now wired into the yfinance monkeypatch,
which covers every call site at once (Market Scan, Surprise, IPO, Hot Picks, Data Feed,
every repair button) because they all funnel through `yf.download` / `Ticker.history` /
`Ticker.info`.

**Rename discovery was dead code** — `try_discover_rename()` and
`resolve_with_fallback()` were called from nowhere, so a real NSE rename could only be
fixed by hand-editing a table. It now fires automatically at the 2nd failure for a
symbol, in a background thread (so no scan ever waits on it), at most once per symbol
per process, with a one-at-a-time lock so a bad upstream can't open 200 connections to
NSE. A confirmed rename is persisted, so the next call needs no network at all.

**DB tabs were slow to load** — each request built a brand-new connection pool, which
on the Oracle VM means a full TCP + TLS + wallet handshake before the first query. Now
one warm pool per process, plus a 20-second memo on the audit endpoints, which were
walking every tracked symbol on every single tab mount.

**NSE 403 on the bootstrap call** — this one was worth opening. The client was built
with `Accept: application/json` and then used to fetch `https://www.nseindia.com`,
which is an HTML page. NSE's WAF treats "JSON Accept on a document URL with no
`Sec-Fetch-*` navigation hints" as a bot and answers 403. Because a 403 still carries a
`Set-Cookie`, nothing raised and nothing looked broken — but the cookies you got back
were the weak anonymous pair, not the real `nsit`/`nseappid` pair. NSE rate-limits that
anonymous session much harder, so this was plausibly feeding the intermittent empty
payloads and "not found" errors too. The two HTML hops now send browser-shaped
navigation headers, the `/api/` calls keep their JSON Accept, and the code logs a
warning if the handshake still yields no usable cookie.

**Universe 300 → 500 leftovers** — `SCAN_UNIVERSE_TARGET` was raised to 500 but
`_get_all_known_symbols()` still capped at `[:300]` and the news-mention scanner at
`[:400]`. Symbols in slots 301–500 were being *scanned* but then rejected as "unknown"
by symbol lookup. Both now follow `SCAN_UNIVERSE_TARGET`. `universe_ingest.py` and the
GitHub Actions workflows were checked and have no hardcoded size — nothing to change
there.

**The two symbol maps disagreed** — api-gateway's `SYMBOL_RENAMES` and
market-data-service's `SMART_SYMBOL_MAP` were never reconciled. No key *conflicted*,
but market-data-service was missing five real renames (`PVR→PVRINOX`,
`IBULHSGFIN→SAMMAANCAP`, `L&TFH→LTF`, `ADANITRANS→ADANIENSOL`, `NSPIRA→NSIL`), so a
quote asked for `PVR` directly hit the dead ticker. api-gateway was missing three
compact company forms market-data already knew (`KFINTECHNOLOGIES`,
`KPITTECHNOLOGIES`, `ONE97`). Both sides now carry the union, with a comment in each
file saying they must be edited together.

**XGBoost / scikit-learn pickle warnings** — not cosmetic. `training/` pins
scikit-learn 1.5.1 + xgboost 2.1.1 and writes `model.pkl`; `prediction/` pinned 1.5.0 +
2.0.3 and read it. An unpickled estimator whose internals changed between versions is
not guaranteed to score identically, so this was a silent-wrong-predictions risk, not
just log noise. `prediction/requirements.txt` is now pinned to match training exactly.

**The `1900-01-01` date hack is gone** — empty `listing_date` now binds a real `NULL`
instead of a sentinel date, and there is an assertion specifically checking the sentinel
never comes back.

---

## Oracle-strictness items verified

Checked by an automated audit (140 assertions, all passing), not by eye:

- No `IF NOT EXISTS` in Oracle DDL — `exec_ddl_safe()` swallows ORA-00955 instead.
- `DEFAULT` always before `NOT NULL`.
- No native `BOOLEAN` anywhere — `NUMBER(1)`, with conversion on write and read.
- No `VARCHAR2` over 4000 bytes; long text is `CLOB`.
- Clipping is by **UTF-8 byte length**, not character count, so one non-ASCII character
  can't trigger ORA-12899 and silently drop a row.
- MERGE join keys never appear in the `UPDATE SET` list (ORA-38104).
- **ORA-01461**, found during this audit: python-oracledb escalates a string past
  Oracle's 32767-byte bind ceiling to `DB_TYPE_LONG`, and a LONG bind is only legal as a
  direct INSERT/UPDATE value — *not* as a projected expression in
  `MERGE ... USING (SELECT :bind ... FROM dual)`, which is exactly where ours sits. Both
  CLOB columns are now wrapped in `TO_CLOB()` and capped below the ceiling, and the
  audit asserts every CLOB column is bound that way so it can't regress.
- No Python `date`/`datetime` object is ever bound. Dates are stored as ISO strings and
  `updated_at` is set by SQL (`NOW()` / `SYSTIMESTAMP`), which removes the whole
  ORA-01830/01858 `TO_DATE` format class of bug.
- The Render/Postgres SQL is diffed against the pristine baseline and asserted
  **byte-for-byte unchanged**, so none of the Oracle work can affect your working
  Neon deployment.

## The one thing still needing your hardware

The Oracle DDL and MERGE statements have been verified for *strictness* — every rule
above is machine-checked — but they have never been executed against a live Oracle
Autonomous DB from here, because there is no Oracle connection available in this
environment. First deploy to `backup-production`, then open the Hot Picks and IPO
Tracker tabs once and check the data-health panel says the table exists. If a DDL
statement is going to complain, that is where it will show up, and it will be
non-fatal: every DB write path is best-effort and falls back to the kv cache.
