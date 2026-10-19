# Group 454 - gateway CORS

**Problem:** the gateway allowed every origin, with credentials, and a second middleware stamped `Access-Control-Allow-Origin: *` on every response, so no setting could narrow it.

**Now:** `GATEWAY_CORS_ORIGINS` (comma separated, exact scheme + host + port, case-insensitive, trailing slash ignored).
- **Set** - only those origins get CORS headers. Error (500) responses echo only an allowed origin. The `/ws` websocket closes other origins with code 1008.
- **Unset / empty / `*`** - any origin, as before (default so a rebuild cannot break a working deploy). A warning is logged at boot.
- `allow_credentials` is now `False`; the frontend never sends cookies (Bearer header only).
- Not affected: requests with no `Origin` header (curl, server-to-server, same-origin through nginx `/api/`).

**To tighten (recommended):** in `.env` set e.g. `GATEWAY_CORS_ORIGINS=https://your-site.example.com` (add `http://localhost:5173` for local dev, and every other domain you open the app from, e.g. a Vercel URL),
then `docker compose up -d --build api-gateway`. Check with:
`curl -si -X OPTIONS https://your-site/api/health -H 'Origin: https://evil.example' -H 'Access-Control-Request-Method: GET' | grep -i access-control` - nothing should come back.
If the app stops loading from a domain you forgot, add that origin and restart the gateway (or clear the variable to go back to "any origin").

**Not changed:** the other services keep `allow_origins=["*"]`. The internal ones (market-data, analysis, decision, notification) are bound to 127.0.0.1 in docker-compose and are not browser-reachable.
real-trade, position-stocks and calibration-runner are reachable through nginx but their mutating routes already need the admin login; tighten them the same way later if you want (calibration-runner already has `CORS_ORIGINS`).
