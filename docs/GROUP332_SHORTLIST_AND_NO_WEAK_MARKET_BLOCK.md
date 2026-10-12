# group 332 - best-3-to-5 shortlist, weak-market restriction removed, log fixes

## Shortlist
- position-stocks: `screening/shortlist.py`. Up to SHORTLIST_MAX names are quality-gated; passers are scored
  scan 0.40 / Market Board conviction 0.30 / quality 0.20 / zone 0.10 (weights renormalised over what exists),
  bearish -20, extended -10. Best 5 are tried best-first (capital fall-through unchanged). `GET /shortlist` shows the last one.
- real-trade: `entry_engine/shortlist.py`. Board nudge (max +/-6) is added to each candidate's composite; Gate 6 ranks, the
  shortlist (<= 5) is logged, entries are its first ENTRY_MAX_NEW_PER_CYCLE. Floor and UC bypass unchanged.
- Not a gate: nothing is added or lowered; no board = no nudge.

## Weak market no longer blocks
Off by default: real-trade regime gate, VIX block, VIX carry block, Nifty-weak carry block; position-stocks market gate,
prev-close gate, VIX block. Opening gate's Nifty checks follow MARKET_GATE_ENABLED (so they are off too).
Still on (stock-level, not market-level): R:R, drift, VWAP, risk engine, cash, quality gate, VIX *size* shrink (VIX_CAUTION_*).
Static "raised bar" constants in api-gateway buy_sniper / ipo_scanner and decision-prediction's regime-based news weighting were left.

## Log fixes
market-data live_store retry in halves; real-trade fast-exit DPY-4024 quiet until 3 in a row; position-stocks WS day-stats 2%.
