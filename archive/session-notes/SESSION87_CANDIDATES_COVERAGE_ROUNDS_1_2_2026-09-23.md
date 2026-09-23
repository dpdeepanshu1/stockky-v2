# Session 87 (2026-09-23): candidate_engine/candidates.py — coverage rounds 1+2, 0% → 65%

Continuation of session86, which drafted (but never landed) round 1 of the
`candidate_engine/candidates.py` coverage plan before running out of turn.
This session had working `pytest` **and** package-registry network access
(`pip install pytest pytest-cov sqlalchemy httpx fastapi pydantic` all
succeeded), so — unlike sessions 76/77/82c/86 — everything below was
actually executed against the real code, not hand-traced or only
structurally verified.

## Round 1 — `tests/test_candidates_helpers.py` (107 tests)

Landed session86's draft, covering the self-contained pieces:

- **Sector-peer-history cache**: `_record_sector_peer_score` /
  `_get_cross_cycle_peer_scores` — no-op on missing sector/score,
  record-and-retrieve roundtrip, staleness pruning on both record and
  retrieve, max-samples cap evicting the oldest entries.
- **`_refresh_cycle_adaptive_params`**: success path updates all seven
  module globals; an exception from any one `adaptive_market_params` call
  leaves every global at its prior value (not a partial update).
- **HTTP fetch wrappers**: `_fetch`, `_fetch_history`, `_fetch_quote`,
  `_fetch_delivery` — 200 / non-200 / exception for each.
- **`_fetch_fund_tech_score`** / **`_fetch_market_cap_cr`**: market_cap
  raw-dict fallback, either call's exception being non-fatal to the
  other, a non-numeric market_cap being swallowed rather than raising.
- **`_prefetch_quotes_bulk`**: empty/all-falsy symbol list no-ops, dedup,
  chunking (`BULK_QUOTE_CHUNK_SIZE`), a chunk's HTTP error or exception
  being logged, never raised.
- **`_quality_gate_fund_tech`**: absolute fund/tech/market-cap floors,
  no-data skip note, thin-sector-sample bypass, sector-percentile
  reject/pass, cross-cycle peer scores merging into the sample.
- **Pure analysis helpers**: `_compute_atr_from_candles`, `_pct_return`,
  `_weighted_bullish_score`/`_is_bullish`, `_volume_is_healthy`,
  `_near_resistance`.
- **Source row-normalizers**: `_rows_from_hot_picks`, `_rows_from_ipo`,
  `_rows_from_volume_shock`, `_rows_from_surprise` — actionable-decision +
  min-conviction filtering, per-source field-fallback chains.
- **`_fetch_volume_shock_universe`**, **`_recently_candidated_symbols`**
  (cooldown window, other-mode exclusion, custom-hours override, the
  Gate-6-skip requeue-window shrink vs. a non-Gate-6 WAIT reason keeping
  the full cooldown, and the snapshot-read-exception fallback).

### Bug caught by actually running it

`test_low_sector_percentile_rejects` fed `fundamental_score`/
`technical_score` of 20 without first lowering `_adaptive_fund_floor`/
`_adaptive_tech_floor` from their real ~35 default. The candidate was
being rejected by the earlier **absolute-floor** check (line 613), never
reaching the **sector-relative** check the test claimed to exercise — the
assertion on the note text (`"sector-relative pctl" in note`) failed
immediately on the real run. Fixed by monkeypatching both floors to `0.0`
in that one test. This is exactly the class of bug a structural-only
check (import + `py_compile` + AST-verifying every dotted target exists)
cannot catch — it verifies names and signatures line up, not that a
test's fixture data actually reaches the branch it claims to cover.

Round 1 alone: **107 passed**, `candidate_engine/candidates.py` line
coverage 0% → 50%.

## Round 2 — `tests/test_candidates_analysis.py` (26 tests, new)

The two multi-call analysis functions round 1 deliberately left out:

- **`_multi_tf_analysis`**: data-starved (quote AND every timeframe
  empty) vs. plain no-quote (some history resolved, quote didn't),
  zero-price quote, sub-₹20 price floor, 6-month downtrend block,
  weighted-bullish-score threshold, 52-week overextension, adaptive ATR
  cap, unhealthy volume, near-resistance, and the full-pass happy path.
- **`_volume_shock_analysis`**: no quote, insufficient daily history,
  unusable price, sub-₹20 floor, unresolvable return (prior_close ≤ 0),
  below-threshold return, thin volume history, below-threshold volume
  multiple, adaptive ATR cap, base-tier delivery-quality gate
  reject/pass, missing-vs-`fallback_neutral` delivery data being treated
  as *unknown* rather than a failing value, high-conviction
  classification skipping the delivery fetch entirely (asserted via
  `client.calls`), and upper-circuit classification.

Needed a routing fake `httpx.AsyncClient` (`_RoutedAsyncClient`,
dispatching on `(url, params)`) rather than round 1's plain
URL-substring router, because `_multi_tf_analysis` fires 7 concurrent
GETs at the exact same `/history/{symbol}` URL, distinguished only by the
`period` query param (`1d`/`5d`/`1mo`/`3mo`/`6mo`/`1y`/`2y`).

### Two more bugs caught by actually running it

Both in this session's own first-draft fixtures/assertions, not the
production code — same pattern as round 1's fix, caught immediately by
the failing assertion rather than by inspection:

1. The happy-path fixture's `1y` candle set was meant to be flat/
   non-bullish (to isolate the 52-week range check from the
   weighted-bullish-score check), but its first candle's `open` value
   actually produced an 11%+ return, so it silently counted as a fifth
   bullish timeframe. `bullish_count` came back `5.0`, not the asserted
   `4.0`. Fixed by correcting the assertion to match what the fixture
   actually produces — the production check was right, the test's
   expectation was wrong.
2. `test_overextended_52w_rejects`'s 52-week high/low pair gave a range
   position of 50%, nowhere near the >88% (top-12%) rejection threshold,
   so the function correctly did *not* reject, and the test's own
   `"52w range" in result["reject_reason"]` assertion then blew up with
   `TypeError: argument of type 'NoneType' is not iterable` (since
   `reject_reason` was `None`). Fixed the fixture's low/high values so
   the price genuinely sits in the top 12% of the range.

## Combined result

Round 1 + round 2 together: **133 passed**, `candidate_engine/
candidates.py` line coverage **0% → 65%** (243/689 statements still
missing). Full `real-trade-service` suite re-run after landing both
files:

```
1126 passed, 1 xfailed, 6 warnings in ~35s
```

No regressions from session86's 1100/1 baseline (the extra 26 passing
tests are this round's own `test_candidates_analysis.py`). No changes to
the production module itself — tests only.

## Still open

Largest first — the three top-level cycle-orchestrators this file builds
on top of the now-tested pieces:

- `_refresh_standard_candidates` (lines 1383-1605)
- `_refresh_volume_shock_candidates` (lines 1628-1878)
- `refresh_candidates` (lines 2012-2077)

None of these are self-contained the way rounds 1-2's targets were —
each wraps DB writes, the `intraday_eligibility` restricted-symbol
lookup, bulk-quote prefetch, and sector-peer-aware quality gating across
a whole candidate batch, so they'll need fixture-level mocking of several
chained calls plus a real in-memory-SQLite `db` fixture to exercise
properly. Same shape as session86's own deferred-then-closed
orchestration round for `execution/auto_pilot.py` — expect a similar
two-part split (helper-adjacent pieces first, full-cycle wiring second)
if this doesn't fit in one round.

A handful of small in-function branches are also still open (single
lines 806, 819, 861, 1063, 1193) — likely narrow edge conditions inside
the two functions just closed this session; worth a final short pass
once the three orchestrators above are done.

After that: re-running the suite with the corrected
`--cov=intraday_eligibility` flag (flagged since session84 as a wrong
`--cov` module path, not a real coverage gap — the module lives at repo
root as `intraday_eligibility.py`, not `execution.intraday_eligibility`).

## Commands

```bash
cd services/real-trade-service

# just this session's two files
python3 -m pytest tests/test_candidates_helpers.py tests/test_candidates_analysis.py \
  -q --cov=candidate_engine.candidates --cov-report=term-missing

# whole service, full suite
python3 -m pytest -q --cov=. --cov-report=term-missing
```
