# group87 (2026-10-04) - item 14: browser /ws loop

Cumulative on group86. Rebuild the frontend: `docker compose build frontend && docker compose up -d`.

## Cause
`useRealtime.ts::toWsUrl` set `url.pathname = "/ws"`, dropping any path in `VITE_API_URL`. With `VITE_API_URL=https://stockky.duckdns.org/api` the socket opened `wss://stockky.duckdns.org/ws`, which `deploy/nginx-stockky.conf` does not proxy (WebSocket upgrade is only configured under `location /api/`). It hit the frontend container and returned `200 483` (index.html), so the handshake failed and the hook retried.

## Fix
- `toWsUrl` keeps the base path and appends `/ws`: `https://host/api` -> `wss://host/api/ws`, `https://host/api/` -> same, `https://host` -> `wss://host/ws`, `http://localhost:8000` -> `ws://localhost:8000/ws`. Blank/invalid base -> `null` (unchanged).
- Reconnect delay cap: 15 s, then 2 min after 6 failed attempts in a row without opening (resets on a successful open).

## Check after deploy
- The nginx/frontend log should stop showing `GET /ws ... 200 483`.
- Browser devtools -> Network -> WS: one `wss://.../api/ws` connection with status 101; live quotes update without polling.
- If `VITE_API_URL` is set WITHOUT `/api` (gateway on its own host/port), nothing changes for you.

## Not changed
Backend `/ws` hub, nginx config.
