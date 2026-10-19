# Group 453 - gateway login

**Problem:** the api-gateway had no login on its dangerous routes and nginx publishes it at `/api/`.

**Now protected (14 routes):** `/notifications/config` (POST), `/notifications/config/{channel}` (DELETE), `/notifications/call/me`, `/notifications/test`,
`/notifications/send-picks`, `/training/train`, `/ops/power-off`, `/ops/circuit-reset`, `/ops/overnight/config`, `/ops/overnight/run`, `/ops/resume-activity`,
`/data-feed/hard-reset`, `/api/data-feed/hard-reset`, `/api/feed/hard-reset`.
Everything else (reads, scans, watchlist, routine feed routes) is unchanged.

**How it works:** same admin session token real-trade issues at login (JWT HS256, `SESSION_SECRET`). The gateway checks `Authorization: Bearer <token>`.
The frontend adds the stored token to these paths automatically (`frontend/src/gatewayAuth.ts`).

**Before you deploy**
1. `SESSION_SECRET` in `.env` must be set (same value real-trade uses). If it is empty the protected routes answer **503** on purpose.
2. Rebuild/restart the api-gateway and rebuild the frontend.
3. Log in on the Real Trade tab; then Power off, Reset circuit, alert settings etc. work as before. Logged out they answer 401.

**Emergency switch:** `GATEWAY_ADMIN_GUARD=false` turns the guard off (default is on).

**Not covered here:** the other ~90 POST/DELETE routes (scan, watchlist, feed run/stop...) stay open as before - they are what the UI uses without a login.
CORS (`allow_origins=*`), login throttling and the Dockerfile root user are still open.
