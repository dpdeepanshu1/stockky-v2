# Session 5 (2026-09-12) — position-stocks-service: missing nginx route found + fixed

User's first live deploy of position-stocks-service showed a broken Position
Stocks tab: buttons looked disabled, a banner showed nginx's raw "405 Not
Allowed" page, and the whole status strip read all-off (DISARMED/CLOSED/DOWN/
PAUSED) plus a stale risk-confirmation warning.

## Root cause
`deploy/nginx-stockky.conf` — the VM-level nginx reverse proxy — was never
updated when `position-stocks-service` (port 8006) was built in earlier
sessions. It only had `location` blocks for `/` (frontend), `/api/`
(api-gateway), and `/realtrade/` (real-trade-service). With no
`/positionstocks/` block:
- Every POST (`/arm`, `/disarm`, `/service/enable`, `/service/disable`,
  `/kill`, `/ledger/sync`, `/reconcile`) fell through to the frontend's
  static-file location, whose default nginx handler only allows GET/HEAD —
  hence the verbatim 405 page.
- Every GET (`/status`, `/positions`, `/candidates`, `/ledger`) fell through
  to the SPA's `index.html` fallback (200 OK, but HTML not JSON), so
  `/status` never returned real data — explaining why every status-derived
  UI element (armed, module, risk-confirmed banner) looked broken at once.

One root cause, five visible symptoms — not five separate bugs.

## Fix
`deploy/nginx-stockky.conf`: added `upstream stockky_position_stocks`
(`127.0.0.1:8006`) and a `location /positionstocks/` block, mirroring
`/realtrade/`'s exact proxy_pass/header/timeout pattern.

## Action required on the user's VM (not doable from this sandbox)
1. Replace `/etc/nginx/sites-available/stockky` with the updated file from
   this zip.
2. `sudo nginx -t && sudo systemctl reload nginx`
3. Set the Position Stocks Settings URL to
   `https://stockky.duckdns.org/positionstocks` (no trailing slash).
4. Reload the tab and confirm real state now shows.

Not yet re-verified live — this is the top item in STATUS.md's Next Steps.
