# Pullback re-arm + intraday gate loosening (2026-10-19)

## What changed
- Price floor 20 -> 10 (position-stocks MIN_STOCK_PRICE; real-trade CANDIDATE_/RISK_MIN_STOCK_PRICE).
- position-stocks quality floors: fundamental 40 -> 25, technical 40 -> 30, market cap 500 -> 200 cr. Overnight carry floors unchanged (60 / 60 / 2000).
- real-trade quality floors: VOLUME_SHOCK_FUND/TECH_ABS_FLOOR 35 -> 25; market cap static 1500 -> 600 cr, absolute floor 500 -> 200 cr (weak-regime tilt still raises it).
- NOT loosened (the "genuine stock" guard): turnover / liquidity gate, depth/spread gate, circuit gate, news/earnings check, loss brake, market filter, kill switch.
- Pullback re-arm entries (orders/pullback_rearm.py, wired in orders/entry.py and main.py). GET /pullback-rearm shows what is armed.
- Earlier today: live-quote "today's return", VWAP re-anchor for the real-trade chase limit, ATR floor for capped stops, scripts/gate_scorecard.py.

## How the pullback re-arm works
1. ARM: attempt_entry() skips a candidate for RANGE_GATE:NEAR_DAY_HIGH, OPENING_GATE:NEAR_DAY_HIGH or OPENING_GATE:EXTENDED_ABOVE_OR
   (it had already passed the quality gate). If it is up >= REARM_MIN_DAY_PCT it is armed with peak = max(ltp, day high).
2. WATCH: every AUTO cycle, BEFORE the screener scan, each armed symbol is read from the live tick feed (the screener drops a stock once its
   window return fades, which is exactly what a pullback does). A new high restarts the leg (peak/trough reset).
3. TRIGGER only when ALL hold: pullback 0.8-3.5 % from the peak; bounce >= 0.3 % off the low; the low is >= 30 s old; price is >= 0.3 % under the
   peak (not chasing again); price not below VWAP. Deeper than 3.5 % = move failed, dropped. Expires after 90 min. Max 2 attempts/symbol/day.
4. ENTER through the normal attempt_entry(): quality gate, liquidity, depth, circuit, VWAP, loss brake, market filter, capital, price guard
   all still apply. Only the gates that armed it (near-high / extended-above-OR) are waived.
5. STOP sits 0.15 % under the pullback low (never wider than the adaptive stop, never tighter than 0.7 %). Target / trailing / breakeven are
   the usual ones. The ENTERED log row starts with PULLBACK_REARM:<why>.

Switch off: PULLBACK_REARM_ENABLED=0. Tunables: REARM_* in position-stocks config.py.

## real-trade-service re-arm (added later on 2026-10-19)
real-trade used to refuse a stock for chasing (drift gate "ran +X% above signal ... Chasing", or the structure gate's chase / stretched-above-EMA20),
consume the candidate and forget it. JUSTDIAL was missed that way. Now (entry_engine/pullback_rearm.py, wired in entry_engine/entry.py, GET /pullback-rearm on port 8005):
1. ARM: a chase refusal arms the symbol per mode (REAL and DEMO separately) when it is up >= REARM_MIN_DAY_PCT (1.0 %) on the day. Quality, liquidity, VWAP,
   regime, risk and bearish-structure refusals never arm (a pullback would not cure them).
2. WATCH: at the start of every evaluate_mode() pass the armed symbols are priced (one bulk quote call) and judged with the same rules as position-stocks
   (pullback 0.8-3.5 %, bounce >= 0.3 %, low held >= 30 s, >= 0.3 % under the peak, not below VWAP, expires after 90 min, 2 attempts/symbol/day).
3. TRIGGER: a fresh copy of the refused candidate is queued (same conviction, label, source) and the CHASE checks only (drift, structure chase / stretch)
   are waived for REARM_WAIVER_S (420 s, about two cycles). It then goes through every other gate, the risk engine, sizing, capital and the 50-point composite floor
   exactly like any candidate. Its drift is scored as 0 so the composite floor does not silently re-block it. The stop is the normal real-trade stop (2 % minimum).
4. Symbols already held, or that already have an unconsumed candidate, are not queued twice.
Switch off: PULLBACK_REARM_ENABLED=0. REAL only or DEMO only: PULLBACK_REARM_MODES=REAL or DEMO. Tunables: REARM_* in real-trade config.py (same names as position-stocks).
Compare modes after a couple of weeks: docker compose exec -T real-trade-service python - --days 14 < scripts/mode_compare.py
