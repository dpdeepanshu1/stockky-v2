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
