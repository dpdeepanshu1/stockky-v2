# group90 (2026-10-04) - item 30: AngelOne feed-token prefix in the position-stocks log

Cumulative on group89. In `position-stocks-service` run `python -m pytest tests -q`, then `docker compose build position-stocks-service && docker compose up -d`.

## Cause
`feed/angelone_session.py::_login` logged `position-stocks: AngelOne session refreshed (feed_token=XXXXXXXX...)` with the first 8 characters of the feed token on every login. The feed token is the credential that goes into the AngelOne WebSocket URL (the ws_client.py redaction filter exists for exactly that reason), so a prefix in the log is part of a live secret.

## Fix
The line now says `feed_token=received` or `feed_token=MISSING`, never any part of the value. `MISSING` is new information: AngelOne answered status OK without a feedToken, which would break the WebSocket feed.

## Sweep
Searched all services for other `token/secret/key/password/jwt[:N]` slices in non-test code. Only other hit: `notification-scheduler-service/notification/main.py::_mask` (first 4 + last 4 characters). That one is returned to the settings UI so the user can recognise which webhook/token is saved; it is not written to a log. Left as is.

## Second half of item 30: shared SESSION_SECRET - NOT changed
real-trade-service and position-stocks-service share one `SESSION_SECRET` on purpose: one admin login, one signed session JWT accepted by both (see the header of `position-stocks-service/auth/admin_auth.py` and the `/ops/auth-config` fingerprints that exist to confirm both services hold the same value). Splitting the secret would log you out of one service whenever you used the other, so I did not. The risk it carries (a leak of the secret from either service lets someone forge a session for both) is real but is a design trade-off, not a bug. If you want separate secrets, that is a larger change: a login flow that issues one token per service.

## Tests
`tests/test_angelone_session.py::TestLogin::test_login_log_never_contains_any_part_of_the_feed_token` (3 cases: token present, None, empty). Confirmed it fails (3 cases) against the old code and passes against the new. Real pytest in a clean venv: test_angelone_session.py 26 passed; full position-stocks-service suite 2406 passed.
