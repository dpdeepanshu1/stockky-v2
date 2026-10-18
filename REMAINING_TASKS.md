# Remaining tasks (rewritten in group 416, after group 415)

The earlier list in this file (market-structure engine, "after group 349") described work that now exists: the policy
(`structure_policy.py`), the real-trade gate, the frontend panel, observation logging, the backtest tooling and the retest
branch. It was deleted rather than patched. Current state of the engine: `docs/GROUP339_MARKET_STRUCTURE_ENGINE.md`.
Current defaults: `docs/GROUP415_EVIDENCE_GATED_STRUCTURE_DEFAULTS.md`.

## Evidence gates (decide whether the structure rules earn their place)
| Gate | Status |
|---|---|
| G1 correctness | Not verified on real data: corporate actions on >= 10 real splits/bonuses, truncation invariance, `/analyze` p95 latency |
| G2 benefit | Not met. A/B verdict `no_edge` (group 375: +0.54 pp, CI [-0.01, 1.29]; group 410: +0.35 pp, CI [-1.63, 2.44], 71 signals) |
| G3 stability | Not started |
| G4 live | Not enough observed days |

Until G2 passes: `STRUCTURE_PREPARE_UPGRADE`, `STRUCTURE_STOP_MODE` stay off and `STRUCTURE_EXIT_MODE` stays `log` (group 415).
Run `scripts/structure_gate_check.py` on >= 2 seeds once the calibration run finishes (re-read seed 13's `summary.txt`; the earlier
`no_edge` may not be from that seed).

## Group 422: policy record-only path (shipped, OFF by default)
To measure a stricter rule before it costs money: set `STRUCTURE_POLICY_SHADOW_RULES=P7,P8` (or `STRUCTURE_POLICY_MODE=record` for everything) on decision-prediction-service, let observations accumulate, then compare `features.would_be` with `final_decision`. Note that shadowing P7 / P8 LOOSENS live BUY NOW gating (those rules stop applying), so only do it deliberately. The real-trade `structure_gate` and the sniper gate are separate gates and are not covered. Group 423 shipped the strict independent-group count (see below).

## Group 423: strict independent groups (shipped, OFF by default)
Workflow: `STRUCTURE_POLICY_PARAMS_JSON={"groups_strict_min":2}` + `STRUCTURE_POLICY_SHADOW_RULES=P7X` on decision-prediction-service, let observations accumulate (the field only exists for scans made after the engine copy is deployed), then compare outcomes of days where `features.groups_strict` < 2 versus the rest before going live. Not wired into `ms_ab_report` / replay (`backtest.POLICY_RULES`) because old stored summaries lack `ms_groups_strict`; add that once enough new rows exist. No evidence yet that it helps (gate G2 is no_edge), so do not enable it live on theory.

## Open design question (from the 16 Oct hold-time check, group 421)
The daily-structure stop / target are multi-day levels, but real-trade squares off at ~15:00 unless the label is overnight-hold eligible, so structure R:R is never realised for BUY NOW / PREPARE entries. The card text now says so (group 421). Whether to add a swing-carry path for structure-confirmed entries is undecided and needs same-day vs next-day vs 2-5 day data: run `scripts/hold_bucket_check.py rt_*.json` on all exports.

## Not built
- Intraday (15m/5m) structure features and `independent_groups` counting in `scoring.py` (the policy has its own group count).
- Walk-forward calibration from real data; `groups_min=2` is a judgement call.
- WS-2: position-stocks still keeps legacy board bodies behind `BOARD_USE_MS_ENGINE`.
- Retest/CHOCH entry branch exists (group 414, `STRUCTURE_RETEST_ENTRY`, default OFF) but has no replay evidence yet. The decision card shows a retest badge since group 417 (Buy Sniper card does not).

## Checks that need your VM / live market
- `grep STRUCTURE_ .env` - old `STRUCTURE_EXIT_MODE=exit`, `STRUCTURE_STOP_MODE=on`, `STRUCTURE_PREPARE_UPGRADE=1`,
  `STRUCTURE_MAX_AGE_DAYS=7` override the group 415 defaults.
- `grep -c 16283` over 24h of logs. A hit on an entry, target or super-order leg means the 0.01 tick band below Rs 250 is wrong there too.
- `/ops/db-latency` (Oracle writes up to 8 s seen in the exit loop).
- REAL watchlist reject histogram; `legacy_gate_backtest.py` with a candidates `limit` above 500 (500 was returned, so counts were truncated).
- `monday_check` during market hours on Mon 19 Oct (depth20 and session verdict are meaningless after the close).
- Re-check `STRUCTURE_MAX_AGE_DAYS=5` in December when the 2027 NSE holiday list is out (worst 2026 case is 4.65 days on Mon 28 Dec).
- Set `HF_MODEL` in `.env` to a model your token can use (the default Mistral-7B is rejected; no default is guessed from here).

## Known data issues
- 28 of 150 history fetches failed (delisted, new IPOs, odd tickers): survivorship bias in the A/B.
- The A/B baseline is a technical-only proxy, so a full-decision backtest is not possible.
- `ms_loc_score` is not monotonic against returns (rule off since group 410).

## Not verified anywhere
Frontend `npm run build`, Python 3.11/3.12 with pinned dependencies, real pytest runs of every suite (use `bash scripts/docker_test.sh`),
anything needing live Dhan.

## Minor
- The broker-stop SL-M-rejected flag is in memory: the first stop after each restart fails once and retries. Harmless.
- Engine caveats: `compat_board` was rebuilt, not recovered; golden tests freeze behaviour, they do not prove thresholds.

## Group 427: order-flow record (shipped, record only)
Let it collect for about two weeks of real trading days, then read `GET /records` (real-trade) `by_imbalance_5`, `by_imbalance_20`, `by_spread`, and position-stocks' positions `order_flow`. Only if a bucket is clearly worse on 100+ trades is a rule worth building (as a log-first gate, like group 425). Not recorded: cumulative volume delta / aggressor volume / absorption (no trade-by-trade feed). Still to run on the VM: `entry_rules_backtest.py`, `legacy_gate_backtest.py`, `structure_backtest.py` on hist400 (after `fetch_history_for_backtest.py hist400 --benchmark-only`), then paste the reports.
