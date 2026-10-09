# Group 271 - shadow rows carry the stop and target levels

Group 269's shadow mode logged only the entry price, so a `WOULD_ENTER` could not be judged against later prices without
guessing the stop and target. Both services now write them too. Nothing else changes.

- position-stocks-service: the candidate-log reason is now
  `OPENING_SHADOW:WOULD_ENTER ltp=.. gap=.. ... stop=<price>(<pct>%) target=<price>(<pct>%)`. The levels are the adaptive ones a real
  entry would have used (`compute_levels`), appended after the existing features, so `reason_prefix=OPENING_SHADOW` queries and
  the old prefix are unchanged.
- real-trade-service: the log line gains ` stop_pct=<x> target_pct=<y>`, from the same ATR rule a real entry uses
  (`_atr_stop_target_pct`). Omitted when the tick has no ATR. It is the base ATR stop/target, before the later range adjustment
  a real entry applies.

To judge a row: compare the stock's later high/low with the logged stop and target. Outcomes are still not simulated automatically.

Tests: 1 new test in each service. position-stocks 2981 passed + the known `group210` failure; real-trade 3848 passed,
1 skipped, 1 known `group172` error.
