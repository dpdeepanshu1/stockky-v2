# Session 112, Round 26 — position-stocks-service `feed/ws_client.py` coverage closeout

Closes out the 4 lines missing per the round-24 coverage run (510-511, 542,
549) — all inside `_ws_loop`'s message-handling body.

## What was covered

- **Heartbeat send failure (510-511):** `except Exception: pass` around
  `await ws.send("ping")`. The existing heartbeat test only ever lets the
  send succeed. New test makes `ws.send` raise specifically for `"ping"`
  (while still recording other sent messages normally) and asserts the
  loop doesn't propagate the error and keeps processing the tick that
  follows.
- **Stale-tick buffer pruning (542):** `buf.popleft()` inside the
  time-bounded prune loop. No existing test ever produced two ticks far
  enough apart in real wall-clock time to trigger an eviction. Rather than
  mock `time.time()` across multiple call sites in the loop (heartbeat
  check + `_parse_frame`'s own `ts = time.time()`), pre-seeded the buffer
  directly with an epoch-timestamped (`ts=0.0`) entry before running the
  loop — any real tick's `ts` is unconditionally more than
  `_MAX_BUFFER_AGE_S` (65 min) past that, so the prune fires deterministically.
- **`_last_quote` write (549):** every existing happy-path test builds a
  bare (non-depth) mode-3 frame, so `_parse_frame` always returned
  `best_bid=best_ask=None` and this line was structurally unreachable from
  those tests. Ported `test_ws_client.py`'s existing depth-frame/
  depth-packet builders into this file (local copies, no cross-file
  import dependency, matching this file's existing convention for its own
  `_build_mode3_frame`) to build a full ≥347-byte SnapQuote frame with a
  real bid/ask packet, then asserted `get_best_bid_ask` reflects it.

## Not done this round

- `config.py` 471-475 — still open, same `importlib.reload` risk noted
  since round 23.
- `orders/eod_squareoff.py` (4 missing) and the four 1-line gaps in
  `orders/adaptive.py` / `entry.py` / `reconcile.py` / `screening/
  engine.py` are still open per the round-24 coverage run.
- No `sqlalchemy`/`pytest`/`websockets` in this sandbox — new tests are
  statically verified with `py_compile` only, not executed. Re-run pytest
  on the real box to confirm, same as every round since 23.
- PB FinTech `legDetails` root-cause thread still untouched.
