# Group 440 - 52-week rule runs log-only by default

## Evidence (18 Oct `legacy_gate_backtest.py hist400`, 357 symbols, T+10, net of costs)
| scope | gate | median edge | clipped edge | day-cluster 90% CI | verdict |
|---|---|---|---|---|---|
| all days | range_52w_legacy | -0.63 | -0.64 | [-0.91, -0.37] | hurts |
| weak (Nifty < own 50-day avg) | range_52w_legacy | -0.69 | -0.73 | [-1.26, -0.20] | hurts |
| strong | range_52w_legacy | -0.55 | -0.50 | [-0.77, -0.24] | hurts |
(the `with_exemption` rows read the same; weak-regime `exemption_test` is `insufficient_sample`.) Group 420 had already found `hurts` at T+5 / T+10 / T+20.
Per the rule written in `GROUP439_52W_REGIME_SPLIT.md`: weak row also `hurts` -> the rule has no regime in which it helps in this history.

## Change
- `docker-compose.yml` (real-trade-service): `CANDIDATE_52W_LOG_ONLY` default `1`. The rule is still evaluated; instead of rejecting it logs
  `52W_LOG_ONLY would reject <SYM>: ...`, counts, and lets the candidate through. Code default (no compose) is unchanged (`0` = rejects).
- `.env.oracle.recommended`: the group 420 hard-off line `CANDIDATE_52W_RULE_ENABLED=0` is replaced by `CANDIDATE_52W_LOG_ONLY=1`. Same live effect (nothing rejected), but the
  would-reject names are now kept. If your VM `.env` still has `CANDIDATE_52W_RULE_ENABLED=0` it stays hard-off and nothing is counted: delete that line to get the counts.
- Visibility (new): `GET /range52w/status` on real-trade (admin token on REAL): `log_only`, `rule_enabled`, `would_reject` since boot, the last 50 would-reject symbols with
  range position and market score, plus regime-gate settings and the current market score. A candidate that passes only because of log-only carries
  `market_note` = "52w log-only: rule would have rejected (at X% of 52w range, market score Y)" (also in the candidate's `_mtf` payload).
- `scripts/env_structure_check.py`: intended default for `CANDIDATE_52W_LOG_ONLY` is now `1` (`0` = TIGHTENS); `CANDIDATE_52W_RULE_ENABLED=0` is flagged OVERRIDE (off, no counting).

## This LOOSENS an entry rule
Near-52-week-high stocks that are not fresh volume-confirmed highs, in a weak market, now become candidates. The later gates (structure, entry, sizing, stops) are unchanged.
Caveats from the evidence: T+10 hold vs the bot's 1-2 day exits, survivorship in today's 400 stocks, Nifty-vs-50DMA is only a stand-in for the market score, daily bars.

## Rollback
`CANDIDATE_52W_LOG_ONLY=0` in the VM `.env` (a value there wins over the compose default), then `docker compose up -d real-trade-service`.

## How to judge it (about two weeks of trading days)
1. `GET /range52w/status` -> `recent` lists what the rule would have blocked.
2. Compare those symbols' outcomes (trades taken after the pass, records report) with the other candidates. If the would-reject names do clearly worse on 100+ trades, switch back (`=0`).
3. `python3 scripts/env_structure_check.py ~/stockky-v2/.env` shows what the VM actually has.

Tests: `services/real-trade-service/tests/test_group440_52w_log_only_visibility.py` (7); group434 / 379 52-week tests (63) still pass; `scripts/test_env_structure_check.py` (5). real-trade suite here: 4985 passed, 2 skipped, 4 failed (`test_oracle_compat`, the `oracledb` driver is not installed in this container; unrelated).
