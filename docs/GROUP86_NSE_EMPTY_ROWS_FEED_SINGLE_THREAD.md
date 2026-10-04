# group86 (2026-10-04) - items 22 and 26

Cumulative on group85. Run `bash run_tests.sh` in api-gateway and market-data-service on the VM.

## Item 22 - misleading "NSE live API unreachable"
`api-gateway/main.py::_get_all_nse_securities`: when the securities list is empty the bhavcopy universe fallback still runs, but the warning is now
- `NSE live API answered but returned 0 securities rows - using bhavcopy universe fallback (N symbols)` when NSE returned a JSON body (HTTP 200) with no rows;
- `NSE live API unreachable - using bhavcopy universe fallback (N symbols)` only when there was no usable response.

## Item 26 - AngelOne feed thread "did not stop within 10.0s"
`market-data-service/angelone_ws_feed.py`:
- off-hours idle wait and the poll-interval wait are sliced into 1 s steps (stop took the full 10 s join timeout before, because the idle wait was one 60 s sleep);
- each `start_feed_background()` bumps `_generation`; a superseded thread exits at its next check, drops in-flight ticks, and cannot clear `_running` for its successor;
- the start now logs when the previous thread is still winding down.
`market-data-service/main.py`: the 15-minute universe refresh calls `stop_feed_background` through `asyncio.to_thread` (the join no longer blocks the event loop for up to 10 s).

No new env vars.

## Not changed
Yahoo news (0 for every symbol), Moneycontrol in the real-trade after-hours scan feed list, wake pings in other containers.
