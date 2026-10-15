# Group 379 - Groups E, F and G of the group378 report

## E1 - the 52-week "top 12%" rule (`real-trade-service/candidate_engine/candidates.py`)
It caused 8 of 13 rejections in the 13-Oct log, said "choppy/weak market" without looking at the market, and rejected stocks that had just made a new high.
Now:
- **Regime gate.** The rule applies only while the market score is below `CANDIDATE_52W_WEAK_REGIME_SCORE` (default 50). The score is the newest
  `market_regime_history` row (what entry_engine already records from `/market/indices`; no extra call), read once per cycle. A row older than
  `CANDIDATE_52W_REGIME_MAX_AGE_MIN` (30) or no row = score unknown = the rule applies as before (missing data never relaxes a rule).
- **Fresh-high exemption.** Price within `CANDIDATE_52W_FRESH_HIGH_PCT` (1.0) % of the 52-week high, that high set within `CANDIDATE_52W_FRESH_HIGH_DAYS` (10)
  calendar days of the last bar, and the 5-day average volume at least `CANDIDATE_52W_FRESH_HIGH_VOL_RATIO` (1.2) x the 20-day average. Such a stock is not
  rejected by check 4 **or by check 7 (near 20-day high)**, which would otherwise reject every fresh high. Missing dates or volume = no exemption. The
  candidate's `market_note` says "52w fresh-high breakout ...".
- The reject reason quotes the real market score ("market score 32 is below 50 (weak market)" or "market score unavailable ...").
- Switches: `CANDIDATE_52W_RULE_ENABLED=0` (rule off), `CANDIDATE_52W_REGIME_GATE=0` (every regime, old behaviour), `CANDIDATE_52W_FRESH_HIGH_EXEMPT=0` (no exemption).
- **None of these numbers is backtested.** They are conservative starting points. The exemption is ON by default and loosens an entry rule in REAL mode: run
  DEMO first, or set `CANDIDATE_52W_FRESH_HIGH_EXEMPT=0` until you have seen it work.

## E2 - ETFs in the stock candidate engine
`ITBEES` / `ITIETF` (and others) were entering the standard track and failing the market-cap fetch. One shared name test now lives in
`watchlist_engine/symbol_filter.is_etf_symbol` (the watchlist's `_wl_is_etf_symbol` delegates to it; its list gained ITIETF, BANKBETA, MONQ50, AONESILVER,
GROWWMETAL and a few fund-name patterns: SETF*, *LIQUID*, *GSEC*, SILVERADD/GOLDCASE). Both candidate tracks drop ETFs by name before any quote, history or
market-cap call (the symbol still counts as "handled" so the volume-shock track does not re-propose it). `CANDIDATE_ETF_FILTER=0` turns it off; extend with
`CANDIDATE_ETF_SYMBOLS=SYM1,SYM2` (or the old `WATCHLIST_ETF_SYMBOLS`). Name test only: an ETF with an ordinary-looking name still gets through.

## E3 - legacy candidate gates are not backtested (documentation only)
Never backtested, all untuned live defaults: 6-month downtrend (-10%), weighted bullish score (>= 4.0), 52-week range (E1 above), ATR cap (7% / adaptive),
volume health (5-day >= 0.80 x 20-day), near-resistance (within 2% of the 20-day high), price floor (Rs 20), market-cap floor. Suggested test order, **my judgment
from reading the code, not from rejection counts**: (1) near-resistance (same "choppy market" claim as the 52-week rule, never checks the regime), (2) the 52-week
rule with the E1 changes, (3) weighted bullish score, (4) volume ratio. Needs your VM history; `scripts/ms_ab_report.py` replays structure rules, not these gates,
so a gate replay would be new tooling.

## E3 tooling (added in group 380) - `scripts/legacy_gate_backtest.py`
Replays the daily-bar form of the legacy gates (6-month downtrend, 52-week top-12% before and after the fresh-high exemption, near 20-day high before and after
the exemption, volume health) on the same history folder the A/B report uses, with no lookahead, next-open entry, slippage and cost, and reports per gate:
days rejected, mean net return of passed vs rejected days, edge (pp), cluster-bootstrap 90% CI and a verdict (`helps` / `hurts` / `no_edge` /
`insufficient_sample`). `exemption_test` compares the exempted fresh highs with the ones the legacy rule still rejects: that is the direct test of E1's
exemption. `structure-backtest.yml` runs it after the A/B report (same sample, same seed) and puts the table in the run summary.
Limits, stated in the script and in the report: every day of every sampled stock is replayed, not only the days a live candidate source would have proposed, and
the market-score part of the 52-week rule cannot be replayed from daily candles. Treat a `helps` / `hurts` as a reason to look, not a tuning instruction.

## F1 - seeded random sample
`scripts/fetch_history_for_backtest.py`: `--sample N` now draws from the sorted, de-duplicated universe (same seed = same symbols whatever order the universe
endpoint returns; the old draw depended on that order), and `--manifest FILE` records seed, universe size and exact symbols. `scripts/ms_ab_report.py` gained
`--sample N --seed S` (benchmark never drawn; sample recorded in the report). `structure-backtest.yml`: default 400, new `seed` input, manifest in the artifact.
The re-run itself is yours to start (market-data host is rate limited; lower `limit` if needed). Try 2-3 seeds before reading anything into a verdict.

## F2 - PREPARE -> BUY upgrade report
`training/structure_obs.py` `summarize()` / `GET /api/structure/report` now has `prepare_upgrade`: `upgraded` (legacy PREPARE, final BUY NOW), `held_at_prepare`
(control) and `kept_buy_now` (context), with hit-rate and 20-day return differences. A difference is `null` until both groups have `min_n` fully evaluated rows.
There is no confidence interval, so it is a reason to look, not a verdict. Kill switch unchanged: `STRUCTURE_PREPARE_UPGRADE=0`.

## G2 - `MARKET_DATA_URL` default
16 modules defaulted to `https://market-data-service-r6d7.onrender.com`; now `http://market-data-service:8001` (the compose service name, already set in compose).
Two api-gateway tests that pinned the old default were updated. New `scripts/check_market_data_url_default.py` (wired into `service-tests.yml`) fails if an
onrender.com default comes back or a compose service has a different value. A service run outside compose without the variable now fails to connect instead of
quietly using a public host. `.env.example` keeps the Render URL for a split Render deployment.

## G3 - 2027 holidays
NSE has not published them (expected December 2026). All 7 lists carry a "2027 PENDING" comment and no guessed dates; `check_holiday_lists_sync.py` still passes
and still prints its 2027 reminder.

## G4 - deferred, unchanged
ML features, weekly / intraday structure, Wyckoff, volume profile.

## Not verified here
The sandbox had no network, pytest, httpx or sqlalchemy. Run for real: `scripts/test_ms_ab_report.py` (18), the structure-observation tests (7 + 21), the
guard scripts, `check_shared_copies.py`, `check_holiday_lists_sync.py`, `gen_env_reference.py --check`. Run through stubs only: E1/E2 (57 of 60 in
`tests/test_group379_52w_rule_and_etf_filter.py`; the 3 that need real sqlalchemy / the real entry module did not run). Not run: the full real-trade, api-gateway
and decision-prediction suites, Python 3.11/3.12 CI matrix, frontend build, coverage gate. Run `bash run_tests.sh` per service on the VM.
