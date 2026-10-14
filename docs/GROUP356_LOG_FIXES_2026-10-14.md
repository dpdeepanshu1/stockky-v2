# group356 - 14 Oct 10:38 IST restart log fixes

1. position-stocks quality gate: event call is ONE shared in-flight request per symbol (waits QUALITY_GATE_TIMEOUT_S, then
   QUALITY_GATE_EVENT_RETRY_TIMEOUT_S more, default 8 s); a late answer is kept QUALITY_GATE_EVENT_LATE_TTL_S (300 s) for the next
   cycle. Off: QUALITY_GATE_EVENT_SHARED_FETCH=0.
2. real-trade dynamic universe: /check warm-up held for DYNAMIC_UNIVERSE_CHECK_STARTUP_HOLD_S (300) after a restart and
   DYNAMIC_UNIVERSE_CHECK_OPEN_HOLD_MIN (10) after 09:15 IST; subscriptions still sync at once. 0 = off.
3. market-data AngelOne live_quotes writer: skips unchanged rows, shares the Dhan writer's Oracle back-off after a timeout.
   Off: ANGELONE_FEED_DB_CHANGE_ONLY=0.
4. api-gateway: hotpicks_db_upsert, surprise load_static_cache / ensure_surprise_schema, clear_hotpicks_stop / ensure_hotpicks_schema
   moved off the event loop (asyncio.to_thread).
5. api-gateway: startup surprise pre-warm now joins the group 175 single-flight (_start_surprise_scan).

6. (group356b) quality gate fundamental / technical calls: same one-in-flight-request + keep-the-late-answer treatment as the event
   call. A symbol with NO cached scores waits the retry window before passing unfiltered; one with cached scores stays quick and the
   late live answer refreshes the cache next cycle. Still fail-open (a field that never arrives is "unknown"). Same switch as item 1.

Tests: new test_group356_* files in position-stocks, real-trade and market-data services; position-stocks tests/conftest.py pins older
tests to the legacy gate path. Verified only with an offline stand-in runner - run the full suite with real pytest before deploying.
