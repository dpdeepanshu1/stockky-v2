# Session 112, round 22 (2026-09-25): circuit-freeze RMS rejection wording missed by `is_circuit_limit_error()` (position-stocks-service + real-trade-service)

User-reported live incident, diagnosed from two screenshots rather than a
coverage sweep: PB FinTech (POLICYBZR position) sat in status `OPEN` on the
Stockky dashboard while its LTP — confirmed identical on both AngelOne's
feed (₹1,159.50, what the dashboard shows) and Dhan's own order book
(₹1,161.20, from the second screenshot) — was already ~5.4% past its stop
level (₹1,224.70). Ruling out a feed-lag/display explanation (the two
prices essentially agreed), the second screenshot supplied the real clue: a
live Dhan RMS rejection for a different position (Allied Digital Services)
read:

    RMS:351260925312407:Order rejected, Stock in circuit freeze.
    Place order within 78.95 to 113.60.

## Root cause

`execution/dhan_client.py::is_circuit_limit_error()` classifies a Dhan
rejection message as a permanent, session-long circuit-band rejection by
substring-matching against `_CIRCUIT_LIMIT_MARKERS` — but every existing
marker (`"rate not within ckt limit"`, `"not within circuit limit"`,
`"ckt limit"`, `"circuit limit"`, `"within ckt"`) assumes the "Ckt
Limit"/"circuit limit" phrasing from the original session41b incident
(eMudhra). Confirmed live against the exact message above:

```python
>>> dc.is_circuit_limit_error(
...     "RMS:351260925312407:Order rejected, Stock in circuit freeze. "
...     "Place order within 78.95 to 113.60."
... )
False
```

Dhan uses **"circuit FREEZE"**, not "circuit LIMIT", for at least this
rejection path — a distinct phrasing that shares no matching substring
with anything on the list. Practical effect: `orders/eod_squareoff.py`'s
`_fire_flat_sell()` retry loop (used by EOD squareoff, manual "Exit Now",
and stagnation exit) checks `is_circuit_limit_error(err_str)` specifically
so it can fail fast — record the placement failure and stop retrying —
instead of treating a rejection as "unclassified (possibly transient)" and
burning retry attempts against a rejection that can never succeed at any
price until the freeze lifts. A circuit-frozen stock's stop-loss leg (or a
manual/EOD flat-sell attempt) hitting this exact wording was being treated
as transient instead of permanent — matching the observed symptom of a
position stuck `OPEN` well past its stop with no automatic resolution and
no correctly-classified failure logged either.

This is a different code path from the `EXIT_LEGS_REJECTED` dead-leg
detection in `orders/reconcile.py` (which AASTHA correctly triggered) —
that mechanism reads the super-order leg's `orderStatus` enum field
(`REJECTED`/`CANCELLED`/`EXPIRED`), not free-text rejection reasons, so
it's unaffected by this particular gap. This fix is specifically about the
plain-order rejection-message classifier used for the flat-sell fallback
path, entry-time avoidance, and notification/cooldown routing.

## Fix

Added `"circuit freeze"` and `"in circuit freeze"` to
`_CIRCUIT_LIMIT_MARKERS` in **both** copies of this function —
`position-stocks-service/execution/dhan_client.py` (where the incident was
found) and `real-trade-service/execution/dhan_client.py` (the sibling copy
this codebase's own comments explicitly say to keep in sync, e.g.
`feed/scrip_master.py`'s docstring: "Keep the two copies in sync"). Every
existing marker is left as-is — "Rate Not Within Ckt Limit" is a real,
separately-observed phrasing too, not a wrong guess to replace.

Added regression coverage in both services' `tests/test_dhan_client.py`
using the exact live message text.

## Verification

  * `position-stocks-service/tests/test_dhan_client.py`: **110 passed**.
  * `position-stocks-service` full suite: **2247 passed, 2 skipped**, no
    regressions (main.py, feed/scrip_master.py, feed/ws_client.py all
    still 100%/98%/98% as of round 21 — this round only touched
    `execution/dhan_client.py`'s marker list and its own test file).
  * `real-trade-service/tests/test_dhan_client.py`: **39 passed**.
  * `real-trade-service` full suite: **2721 passed, 2 skipped, 4 failed**
    — the 4 failures are pre-existing and unrelated
    (`tests/test_oracle_compat.py::TestBuildOracleEngine::*`, failing on
    this sandbox's missing Oracle connectivity/`oracledb` setup, nothing
    to do with `dhan_client.py`).
  * `py_compile` clean on both edited `dhan_client.py` files and both
    edited test files.

## Open items from the live incident (not yet resolved — need the user's
own Dhan/service access, not just static code reading)

  * Whether PB FinTech's STOP_LOSS_LEG rejection actually WAS a circuit
    freeze (this fix makes the classifier correctly recognize it if so)
    still needs confirming against the real `get_super_order_list`
    `legDetails` for that specific order (`34326092514375`) — the user was
    asked to pull and share this; not yet received.
  * Whether AASTHA (already `EXIT_LEGS_REJECTED`) is still genuinely open
    at Dhan or was already closed out there (dashboard showing stale state)
    — it did not appear in the user's Dhan positions screenshot at all.
  * If PB FinTech's leg rejection reason text does turn out to be
    circuit-freeze wording, note this fix alone does not retroactively
    reclassify or unstick that already-open position — it only prevents
    the *next* occurrence's flat-sell retries from being misclassified.
    The existing position still needs a manual "Exit Now" (which will
    itself hit the same circuit-freeze rejection and correctly fail fast
    now, rather than retry pointlessly) or to wait out the freeze.
