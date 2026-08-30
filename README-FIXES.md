# Stockky test-sweep fixes — 2026-08-30 run

From `test_20260830_112214.log`: **PASS 114 / WARN 8 / FAIL 2**.

The 8 WARNs are expected, not bugs (the test script says so itself):
- `training/api/report` + `training/api/insights` → legitimate 404 until a
  training run has produced a report ("No report found").
- `dhan/status`, `dhan/network-check`, `audit-log`, `dhan/positions`,
  `dhan/holdings`, `dhan/orders` → 401 because these require an
  authenticated admin session; the test script explicitly notes this is
  expected.

Both real FAILs are fixed here.

## Fix 1 — `scheduler/hydrate/weekend` timing out (FAIL, GET 000 @ 25s)

**Root cause:** the endpoint ran `hydrate_batch()` synchronously inside the
HTTP request handler. That function fetches the whole universe and, for
every symbol in its slice, makes up to three sequential upstream calls
(fundamental/technical/events) — by design this can take minutes to hours
(the GitHub Action already uses a 900s curl timeout for one slice, and
`--full` is documented as "slow, intentional"). Any client with a shorter
timeout (the test harness, a load balancer, an uptime check) sees the
connection die with no response — exactly the `000` in the log.

**Fix:** `weekend_hydrator.py` gets a small background-job wrapper
(`start_hydrate_background`, `get_hydrate_job`) mirroring the same
fire-and-forget + pollable `/status` pattern the api-gateway's
`refill_additional.py` already uses. `scheduler/main.py`'s
`/hydrate/weekend` now starts the job on a daemon thread and returns
immediately (add `?wait=true` to keep the old fully-blocking behavior for
manual runs with a very long client timeout). A new
`GET /hydrate/weekend/status` lets callers poll progress.

Also updated `.github/workflows/weekend-hydrator.yml`'s slice branch to
poll the new status endpoint instead of holding one long curl open, since
the trigger call now returns almost instantly.

Files touched:
- `services/notification-scheduler-service/scheduler/weekend_hydrator.py`
- `services/notification-scheduler-service/scheduler/main.py`
- `.github/workflows/weekend-hydrator.yml`

## Fix 2 — `notifications/test` failing (FAIL, POST 502 @ 15.02s)

**Root cause:** the gateway's `/notifications/test` proxied to the
notification service with a flat 15s timeout. But `/test` fans out to
every enabled channel, including CallMeBot, which tries a voice call then
a text fallback per recipient at **up to 35s each** — the same reason
`/notifications/call/me` right above it in the same file already uses a
70s timeout. 15s was simply too short for a real config with CallMeBot
enabled, so it reliably timed out and got reported as a misleading
"Notification service unreachable" 502.

**Fix:** bumped the gateway timeout to 75s (matching the pattern used by
`call/me`), added the same wake-first health ping used by
`/notifications/config`, and gave timeouts their own 504 with an accurate
message instead of lumping them into the generic 502.

**Bonus latent bug caught while fixing this:** the frontend's `request()`
helper auto-retries on timeout/502/503/504. With the old 15s timeout
reliably tripping, every click of "Test Notifications" was silently
re-sending the same *real* alert 2–3 times (a real voice call/Telegram
message you didn't ask for again). `testNotifications()` in `api.ts` now
uses **0 retries** (this call has a real external side effect, so it
should never be silently retried) and an 85s client-side timeout to match
the gateway's new 75s.

Files touched:
- `services/api-gateway/main.py`
- `frontend/src/api.ts`

## How to merge

Copy this folder's contents over your repo root — every path here matches
your existing structure exactly (`services/...`, `frontend/src/api.ts`,
`.github/workflows/...`). Only the 5 files listed above changed; nothing
else was touched.

After merging, re-run your test script — both prior FAILs should now
either return quickly (`hydrate/weekend` returns a "started" job
immediately) or succeed within the new timeout (`notifications/test`).
