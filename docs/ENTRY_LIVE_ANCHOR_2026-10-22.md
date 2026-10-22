# Entry stop / target anchored to the live price (2026-10-22)

**Problem (IONEXCHANG, 22 Oct):** signal 354.60, MARKET fill 352.30 (-0.65 %). The stop (351.75) was built from the scan price (354.60 x 0.992), so it sat only
0.15 % under the real fill instead of the planned 0.8 %. Stopped out 21 s after entry (-0.80). The same gap upward (EMAMILTD, +0.43 %) leaves a too-wide stop and a short
target. Today's ledger: 6 of 42 position-stocks trades had |signal-to-fill| >= 0.2 %. reconcile re-arms legs from the fill, but only on a later pass, and the price guard
only rejects a rise above the signal.

**Fix (`orders/live_anchor.py`, wired in `orders/entry.py`):** after every gate (incl. the RR floor) and just before the order, read the freshest tick (same age limit as the
price guard, `ENTRY_MAX_TICK_AGE_S`) and move BOTH legs to it, keeping the planned stop % / target %. Reward:risk is unchanged. The order's reference price and the stored
entry_price use the live tick; `signal_price` still holds the scan price so slippage stays measurable; reconcile still corrects entry_price to the real fill.
Fails open (no fresh tick, drift < 0.02 %, bad data -> unchanged). Manual entries are untouched.

**Switch:** `ENTRY_ANCHOR_TO_LIVE=0` restores the old behaviour. On the Real Money Calibration page (Entry guards); protected from auto-calibration.
**Tests:** `tests/test_live_anchor_20261022.py` (7, passing in the sandbox, stdlib only). Catalog drift check and ENV_REFERENCE check clean.
**Not run:** the DB-backed entry path and the wider pytest suites (no sqlalchemy / pytest in the sandbox): run `bash scripts/docker_test.sh position-stocks-service`.
