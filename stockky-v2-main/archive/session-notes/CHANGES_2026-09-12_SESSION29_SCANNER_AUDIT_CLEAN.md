# Session 29 — 2026-09-12 — api-gateway scanner internals audit

Continuing the round-28 "still genuinely unaudited" list. This round covered:

- `instant_scanner.py` — full read (all 535 lines): feature resolution,
  technical/fundamental scoring, decision thresholds, `process_single_stock`.
  No bugs found.
- `buy_sniper.py` — full read (all 380 lines): conviction/sector adjustment,
  R:R enforcement, suggestion building, filtering/sorting. No bugs found.
- `notification/main.py` — full read (all 968 lines): config load/save,
  Discord/Slack/Telegram/CallMeBot senders, `_dispatch` urgent-vs-normal
  routing, outbox retry/backoff, Neon keepalive loop. No bugs found.
- `ipo_scanner.py` — full read of `analyze_ipo`, `_decision_for_score`,
  `_build_ipo_suggestion` (the score → BUY/SELL/HOLD decision path). No bugs
  found; this file already carries several prior-session fix comments
  (no_data_yet vs error distinction, lookback-window sizing, GMP enrichment
  ordering) that check out as correctly applied.
- `surprise_scanner.py` — read `dedupe_by_symbol`, `directional_filter`,
  `_session_progress_ist` (already fixed in a prior session per its own
  comment), and the static-cache load / KV-seed path in
  `SurpriseStockEngine`. No bugs found.
- `hotpicks_store.py` — spot-checked `_utc_hours_since` (naive/aware
  timestamp handling, matches ipo_scanner's documented convention
  intentionally) and `_row_to_item`. No bugs found.
- Repo-wide re-scan for the round-28 self-recursion bug shape, and for the
  round-28 `"x" in y and "z" in y` substring-check bug shape across all six
  api-gateway scanner files: no new instances of either.
- `data_feed.py` — spot-checked timezone handling (IST used consistently,
  no naive/UTC mixing found) but not yet read end-to-end line by line.

**Still genuinely unaudited** (unchanged from round 28, minus what's above):
`data_feed.py`'s full body beyond the timezone spot-check,
decision-prediction-service's `training/` and `prediction/` subtrees
(~5,000 lines across 24+35 files), and the frontend.
