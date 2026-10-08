# Group 237 - check the stored admin token once before the first protected REAL request

Cause (2026-10-07 evening log): on page load, before login, real-trade-service answered
`/pipeline/status/REAL`, `/watchlist-entries/REAL?status=entered` and `/resilience/status` with 401, each twice.
`loggedIn` starts as "a token exists in localStorage"; when that token had expired, the pollers that start together
all went out with it. (The earlier group 102 gating only helps when there is no token at all.)

## real-trade-service (main.py)
- New `GET /auth/session`: reads the Bearer token with the same `decode_session_token` the admin routes use and
  returns `{"valid": true|false}`. Always 200, booleans only, no auth dependency.

## frontend (realTradeApi.ts)
- `rtRequest`: before the first protected request that carries a stored token, ONE `GET /auth/session` decides; the
  other callers wait for that promise. If the token is not valid the token is cleared, `sessionExpiredHandler` fires
  once ("Session expired - log in again" banner) and the protected request is not sent, so no 401 reaches the server.
- Fails open: an older server without the route, a network error or a non-200 never blocks a request.
- `setSessionToken(token)` marks a freshly issued token as checked; clearing it resets the check for the next login.

## Not changed
position-stocks-service has its own client and still sends one `/dhan/account` before login (one 401 line, not a
burst). Frontend has no test runner; checked with `tsc --noEmit` only, not in a browser.

## Tests
real-trade tests/test_group237_auth_session.py (8).
