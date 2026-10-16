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
