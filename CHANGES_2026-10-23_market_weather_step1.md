# Market Weather, Part 1 steps 1-2 (23 Oct 2026)

Ships with `MARKET_STATE_MODE=shadow`: no trade, size or gate changes. Foundation only.

## Step 1 - overlay mechanism (`control/kit.py`, identical in all three services)
- `Flag.overlay` marks a switch the market-state layer may TIGHTEN for one day. Needs `live=True` and a known direction.
- `Controller.overlay_set / overlay_clear / reap / overlay_status`, `kit.ACTIVE`, `kit.current_profile()`; routes `POST|GET|DELETE /control/overlay` (same admin dependency).
- Rules enforced in code: judged against the SAVED value (TIGHTENS or SAME only); all-or-nothing; never written to overrides.json or os.environ; `until` needs a UTC offset and is at most 24 h ahead; accepted only while `MARKET_STATE_MODE=live` (a stamp-only call with `entries: {}` is also accepted in shadow); a person's `set()` / `clear()` cancels the overlay; switching the mode away from live clears every overlay; a daemon reaper (30 s) plus `describe()` restore expired values.
- Catalog: `MARKET_STATE_MODE` (off/shadow/live, ships shadow, going live needs confirm), `MARKET_STATE_RISK_MULT` (both trading services), `MARKET_STATE_CONVICTION_ADD` and `OPENING_ENTRY_NOT_BEFORE_IST` (real-trade). Overlay dials: those four, plus `RISK_MAX_ENTRIES_PER_DAY`, `ENTRY_LAST_TIME_IST` (real-trade), `MAX_CONCURRENT_SCALP_POSITIONS`, `SCALP_ENTRY_CUTOFF_IST` (position-stocks).
  NOT overlay dials: `STOPOUT_DAY_PAUSE_N` (your open decision), `ACCOUNT_MAX_DAILY_LOSS_PCT`, `DEFAULT_RISK_PER_TRADE_PCT`, every gate.

## Step 2 - hooks (all 1.0 / 0 by default) and stamps
- real-trade `risk_engine/engine.py`: `effective_risk_pct *= _market_state_risk_mult()` once, before the daily-loss cap. `effective_risk.py` gets `market_state_risk_mult()` and a display-only `effective_risk_pct_in_force()` (used by `/risk-config` and the portfolio summary). `effective_risk_pct()` itself is unchanged, so the multiplier is never applied twice.
- real-trade `candidate_engine/candidates.py`: `_min_conviction()` = base + `MARKET_STATE_CONVICTION_ADD` at the 3 `score < floor` comparisons. Score assignments (volume_shock `max(MIN_CONVICTION, 85/75/60)`, the nominal `conviction_score`) keep the base.
- position-stocks `capital/ledger.py`: risk_rupees x `MARKET_STATE_RISK_MULT`.
- Stamp columns `market_state` (16) and `profile_version` (24), nullable: real-trade `trade_orders`, `trade_positions`; position-stocks `scalp_positions`. Added at boot by the existing additive-migration idiom. Stamped at auto-order creation (copied to the position at fill) and at both ScalpPosition creations.

## Tests
`scripts/test_regime_overlay.py` (19), `services/real-trade-service/tests/test_regime_hooks.py` (10), `services/position-stocks-service/tests/test_regime_hooks.py` (5), `services/real-trade-service/tests/test_regime_overlay_routes.py` (needs fastapi).
Pre-existing failures in the original E88 zip, untouched: `test_real_catalogs_are_valid_and_pass_the_static_check` (expects every catalog switch live; `DHAN_ORDER_WS_ENABLED` is not) and the E37 `test_exempt_list_has_no_stale_entries` in both services (stale EXEMPT entries).
