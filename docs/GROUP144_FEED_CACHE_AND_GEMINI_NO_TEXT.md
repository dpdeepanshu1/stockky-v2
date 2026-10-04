# group144 (2026-10-04) - log audit: shared news-feed cache, Gemini "no text" reply

Cumulative on group143. Rebuild: `docker compose up -d --build analysis-intelligence-service decision-prediction-service`
(use your compose names).

## What the logs showed (VM startup + a stockky-hot scan, Sun 2026-10-04 evening)
Startup is clean: fresh containers, Oracle connected, schema ready, ATR cache warmed, 5 REAL positions matched, all loops
started. The two things worth fixing, both from the scan section:

1. **Same three feeds downloaded per symbol.** For each of ~97 symbols `event/main.py` logged `Moneycontrol feed entries: 15`,
   `Economic Times feed entries: 51`, `CNBC TV18 feed entries: 0`: the same three site-wide documents, fetched again for every
   symbol (no cache, no timeout). CNBC TV18 returned 0 entries every single time (dead or blocked), so it cost a download
   per symbol for nothing.
2. **`Gemini call failed: KeyError('parts')`** (prediction). A 200 reply with no text part (finishReason MAX_TOKENS/SAFETY, or a
   blocked prompt) raised inside the success branch. The template note was used anyway, so output was fine, but the warning
   hid the real reason.

## Change
- `analysis-intelligence-service/event/main.py`: new `_parse_site_feed()` used by Moneycontrol, Economic Times and CNBC TV18.
  TTL 300 s (`EVENT_FEED_CACHE_SECONDS`), 600 s when the feed was empty or failed (`EVENT_FEED_EMPTY_CACHE_SECONDS`). Blank or
  bad env = default, 0 = cache off = old behaviour. One lock per feed URL, so concurrent symbols wait for one download.
  A cached failure is re-raised, so each source's own "fetch failed" handling is unchanged. Google News is per-symbol and is NOT cached.
- `decision-prediction-service/prediction/main.py` `_call_gemini`: a 200 without text returns None and logs one INFO line with
  finishReason / blockReason. Real errors (429, other status, exceptions) are unchanged.
- `tests/conftest.py` (analysis-intelligence): autouse fixture clears the feed cache around every test.

## Trade-off
News headlines can be up to 5 min old for these three sources (10 min after an empty fetch). Set `EVENT_FEED_CACHE_SECONDS=0` to undo.

## Tests
- `tests/test_event_main.py` +11 (`TestSiteFeedCache`): second symbol reuses the download, feeds cached separately, empty and
  failed feeds not refetched, expiry, longer empty TTL, 0 = off, blank/bad/custom env, Google not cached, 6 concurrent threads = 1 download.
- New `prediction/tests/test_gemini_no_text.py` (8).
- Run here under real pytest: analysis-intelligence 2224 passed (was 2213), prediction 62 passed, decision 42 passed,
  and group 143's real-trade-service tests (157 passed) which could not run last time.

## Seen in the logs and left alone
- `GET /positions/REAL` etc. 401: the dashboard was not logged in to real-trade-service. Expected.
- `GET /training/api/insights` 404: the route needs `training_report.joblib`, which is not in the container. It also returns
  hard-coded placeholder insights when the file exists - your call whether to remove them.
- `AngelOne quote(batch) HTTP 403 exceeding access rate` once at boot, NSE 403 / "static last-resort list with 69 symbols":
  known (NSE blocks the VM; Sunday evening).
- `repair <sym>: seeded baseline PE=22.5 / ROCE=15`: deliberate, and the row is tagged `pe_seed` / `roce_seed`.
- `ipoalerts status=listed -> 400 (free plan)`: known, logged once per refresh.
