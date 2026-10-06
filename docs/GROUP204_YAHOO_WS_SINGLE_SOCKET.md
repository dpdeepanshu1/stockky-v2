# Group 204 - Yahoo live WebSocket: one socket, one subscribe, and no lost universe (market-data-service)

Cumulative on group 203. Item 5 of the open list ("possible duplicate Yahoo WebSocket"). Rebuild market-data-service.

## What the boot log shows
`yahoo_ws_feed: subscribed to 500 symbols` appeared twice, about a minute apart, for the same symbols. Nothing else in the repo opens a Yahoo
socket (only `market-data-service/yahoo_ws_feed.py` does) and the container runs one uvicorn process, so the second line came from inside that module.
The boot log is not in the zip, so the exact trigger on your VM is not proven; these are the code paths that can produce it, all closed:

1. `start_feed_background()` checked "is a thread alive" and started the thread as two separate steps. It is called from a worker thread (the
   group 173 boot fallback) and from the event loop (the universe refresh), so both could pass the check and start two feed threads / two sockets.
2. When `listen()` **returned** (connection closed cleanly) instead of raising, the loop fell straight back to the top: a new `AsyncWebSocket`, a full
   re-subscribe, no delay, no log line, and the first socket was never closed. A raised error logged "crashed, restarting"; a clean return logged nothing.
3. `ensure_subscribed()` could send the whole universe again onto a socket that was still doing its first subscribe (its "already subscribed" list is
   empty until that finishes).

## A second bug found on the way
The connection loop subscribed only the list the thread was first started with. A universe pushed in later by the 20 s refresh was dropped when the
feed was idle (outside market hours, or between connections) and every reconnect shrank back to the boot list.

## Change (`market-data-service/yahoo_ws_feed.py`; `main.py` unchanged)
- New `_DESIRED` set (Yahoo ids). `start_feed_background()` and `ensure_subscribed()` only add to it; every connect subscribes all of it.
- `start_feed_background()`: the alive-check and thread start are one step under a lock. A repeat call adds its symbols and returns.
- Connection loop: closes any previous client before opening a new one; the client is published for `ensure_subscribed()` only after the first subscribe
  finishes, and symbols added during that window are sent once afterwards (never the full list again); a clean `listen()` return logs a WARNING
  `listen() returned (connection closed) - reconnecting in 5s` and waits `MIN_RECONNECT_GAP_S` (5 s) first.
- Every subscribe line now says why: `subscribed to 500 symbols (connection #N, initial connect | market window opened | after listen() returned | after a crash)`.
  Next boot log: one `connection #1` line is normal; a `#2` line now names its cause.
- A feed thread that dies clears the stale client. `feed_status()` (the market-data Yahoo status route) adds `connects` and `desired_count`.

## Not changed
Market-hours idling, the tick handler, `get_live_quote`, the AngelOne feed, the refresh loop in `main.py`. Note: `ensure_subscribed()` still waits up to 10 s
for Yahoo's acknowledgement while the refresh loop awaits it (unchanged; it only matters when the universe changes while connected).

## Tests
`market-data-service/tests/test_group204_yahoo_ws_single_socket.py` (14 cases, fake `yfinance`, no sockets or real sleeping): 8 concurrent starts open one
thread; a repeat start adds symbols; a normal run opens one socket and subscribes once; a clean `listen()` return closes the old socket, logs it and reconnects;
a crash and a failed first subscribe close the half-open socket; a reconnect carries symbols added by the refresh; symbols added while idle are subscribed when
the window opens; market close closes the socket; a symbol added mid-subscribe is sent alone; status fields; thread death. pytest is not in the sandbox, so
all 14 passed under a small stand-in runner (supports `monkeypatch`); the real pytest run and the existing market-data suites were NOT run. On the VM:
`cd services/market-data-service && python -m pytest tests/test_group204_yahoo_ws_single_socket.py tests/test_group173_boot_feed_defer.py tests/test_main_routes.py -q`.
