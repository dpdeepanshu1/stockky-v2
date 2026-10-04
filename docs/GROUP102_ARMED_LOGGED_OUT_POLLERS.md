# group102 (2026-10-04) - item 29 remainder: REAL pollers ran while armed but logged out (guaranteed 401s)

Cumulative on group101. Frontend-only. Rebuild with `docker compose build frontend && docker compose up -d`.

## Cause
In `RealAutoTrade.tsx` the REAL-mode loaders and pollers were allowed to run when the gate was merely armed, even with no login session (`mode === "REAL" && !loggedIn && !status?.armed` was the skip rule; the one-shot positions/orders/candidates load used `loggedIn || status?.armed`). The comment said REAL data is readable when armed. It is not: `/positions/{mode}`, `/orders/{mode}`, `/candidates/{mode}` and `/pipeline/status/{mode}` all use `require_admin_if_real`, which for REAL requires a valid admin Bearer token whether or not the gate is armed (`auth/admin_auth.py`). With a gate left armed and the page logged out or expired, the Pipeline (2 s), Watchlist (5 s) and Positions (10 s) pollers and the one-shot load could only return 401.

## Fix
REAL loaders and pollers now require `loggedIn`; DEMO is unchanged (open). Four sites: the one-shot positions/orders/candidates load (its dependency list no longer includes `status?.armed`) and the three pollers. Auto-pilot itself is server-side and is not affected. The "Session expired - log in" banner for an armed gate is unchanged.

## Not changed / not verified
- The 401 handling in `rtRequest` is unchanged.
- No frontend test runner exists; `tsc --noEmit` is clean. Python suites are untouched by this change.
- The `/gate` status call stays unauthenticated by design, so the armed state still shows while logged out.
- I did not review calls outside `RealAutoTrade.tsx` for the same pattern, and I have not seen your VM's log, so I can't confirm this was the last source of 401s.
