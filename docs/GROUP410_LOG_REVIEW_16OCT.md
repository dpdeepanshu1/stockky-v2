# Group 410b - review of the 16 Oct 03:29-03:47 UTC boot log

## What the log shows
* Healthy boot: all 8 services up, Oracle connected, reconcile OK (0 open positions), TOTP token refreshed, no crash or restart loop.
* The A/B / calibration commands in the paste are the same runs as before the group410 P7 change (still 7 survivors, old defaults) - nothing new there.
* REAL: the volume-shock track re-fetched the same 3 symbols (BIGBLOC, JKIPL: quality gate `fundamental_score 13-14 < floor 34-38`; ORIENTELEC: `Delivery 12.8% < 30%`)
  every ~12 s: /history, /delivery, /fundamentals, /technical each time. Their rejections had no cache entry (the cache was filled only from the first analysis stage, and the
  delivery rejection carried no `reject_kind`). This feeds the ReadTimeouts (`market_cap fetch failed ... ReadTimeout`), AngelOne candle 403, yfinance bucket saturation and
  indianapi / alphavantage 429s seen in the same minutes.

## Change (real-trade-service `candidate_engine/candidates.py`)
* Delivery rejection now has `reject_kind = "vs_delivery"` -> cached for `REJECT_CACHE_VS_S` (300 s). Short, because delivery stops mattering once a symbol becomes high-conviction.
* Quality-gate rejection on an ABSOLUTE floor (fundamental / technical score, market cap) is cached as `vs_quality` for `REJECT_CACHE_PRICE_S` (900 s). The sector-relative rejection
  depends on that cycle's peers and is not cached. Floors are adaptive, hence the short TTL.
* Kill switch unchanged: `REJECT_CACHE_VS_S=0` / `REJECT_CACHE_PRICE_S=0` restore per-cycle evaluation.
* Test: `services/real-trade-service/tests/test_group410_vs_reject_cache_kinds.py`.

## Seen, NOT changed (no code defect shown by this log)
* `OPENING_GATE:NO_PREV_DAY_DATA` for ADVANCE / DEEPA / WELCORP / ARMEE / NEWGEN: the previous-day history was fetched seconds later and the same symbols then got `PREV_DAY_WEAK_CLOSE`.
  Transient at the open; if it persists after 09:30 IST, send the log.
* `STRUCTURE_BAD_LOCATION` / `RANGE_GATE` / `OPENING_GATE` skips in position-stocks: the gates doing their job on a flat-to-weak open. 0 scalp entries is not a fault.
* AAKASH / BMISL / HDFIRE: delisted or no price; already paused 30 min by the feed. Add BMISL / HDFIRE to `KNOWN_DELISTED_SYMBOLS` if they stay in the universe (they come from the dynamic universe).
* NSE `quote-equity` 403 and `Fetched 0 securities from NSE`: external; fallbacks (bhavcopy) worked.
