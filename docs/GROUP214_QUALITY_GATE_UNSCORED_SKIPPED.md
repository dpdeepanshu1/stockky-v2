# Group 214 - the volume-shock quality gate no longer passes a candidate it could not score (real-trade-service)

Cumulative on group 213. Source: audit item A6 of group 155 ("quality-gate fail-open"). Rebuild: `docker compose build real-trade-service && docker compose up -d`.
Files changed: `candidate_engine/candidates.py`, `config.py` (comment only) (+ tests).

## What was wrong
`_fetch_fund_tech_score` asks analysis-intelligence for a fundamental score and a technical score. A timeout, a non-200 or an exception left the
score `None`, exactly like a service that answered with no score. `_quality_gate_fund_tech` then returned "no fundamental/technical data
available - floor check skipped" (PASS). Three more routes ended in the same pass:
- a per-symbol scoring exception (`quality_gate: scoring failed for X`) - the symbol had no result and was inserted;
- the whole scoring pass failing (`scoring pass failed entirely`) - `quality_scores = {}`, every symbol inserted;
- (by design, not changed) candidates beyond `VOLUME_SHOCK_QUALITY_GATE_MAX_SYMBOLS`.

So when analysis-intelligence was slow (it timed out at the 2026-10-06 boot, group 186 item 5) the gate switched itself off exactly when it could not
see anything, and every volume-shock mover went to the watchlist unchecked.

## Fix
- `_fetch_fund_tech_score` also returns `fund_fetched` / `tech_fetched` (True only for HTTP 200 + parsed).
- `_quality_unscored(qr)`: True for no result at all, or both lookups failed with no score. A result without the flags is treated as scored (old behaviour).
- In `_refresh_volume_shock_candidates`, a symbol that was sent to the scorer and is unscored is skipped THIS cycle (log:
  `VOLUME_SHOCK CANDIDATE SKIPPED X ... could not score it`), counted as `quality_unscored=N` in the cycle summary line. It is not inserted;
  the next volume-shock cycle tries it again, so a short outage delays an entry instead of letting it through unchecked.
- `VOLUME_SHOCK_QUALITY_FAIL_CLOSED=0` (or false/no/off) restores the old behaviour. Blank / anything else = on.

## Not changed on purpose
- A service that ANSWERED (HTTP 200) with no score is still lenient: missing data is not a reject (documented design of the gate).
- One working lookup is enough: its floor still applies, the other pillar is skipped.
- Candidates beyond the cost cap (`VOLUME_SHOCK_QUALITY_GATE_MAX_SYMBOLS`, 40) are still not quality-checked. That is documented in config.py as
  a cost control; raise the cap if you want them checked.
- The standard (hot picks / IPO / surprise) track, the market-regime fetch (`_get_market_regime` defaults to a neutral score 50 when
  /market/indices fails) and Gate 6 are untouched.

## Trade-off
During an analysis-intelligence outage no new volume-shock candidates are added until it recovers. If you would rather keep the old behaviour, set
`VOLUME_SHOCK_QUALITY_FAIL_CLOSED=0` in the real-trade-service environment.

## Tests
- New `tests/test_group214_quality_gate_unscored.py` (28 cases): fetch flags (both ok / both raise / non-200 / 200 without score / one side), the unscored
  helper matrix, env parsing, and the orchestration (both failed -> skipped and logged, env off -> old pass, answered-without-score lenient, one working
  lookup applies its floor, mixed batch, beyond-cap ungated, gate disabled).
- `tests/test_candidates_orchestration.py`: the two tests that pinned "scoring failed -> inserted anyway" are parametrized: default skips, env `0` inserts.
- Sandbox: candidate suites 162 passed. Full real-trade-service run: 3412 passed; the same 4 failed + 1 error as on the uploaded zip
  (`test_group171_held_quote_calls.py` x4, `test_group172_volume_shock_history_reasons.py` x1) - they pass when run alone, so they look order-dependent; not touched.
