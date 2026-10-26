# E204 - DEMO simulated fills use the same quote-age gate as real entries

**Where:** real-trade-service/entry_engine/entry.py, `check_pending_fills` (DEMO only).

**Bug:** evaluate_mode refuses to build an entry on an old or unknown-age price (group290 gate 2b, `_entry_quote_age_reason`, ENTRY_MAX_QUOTE_AGE_S default 10 s; group388 made unknown age "refuse"). The DEMO fill simulation took whatever tick `get_quotes` returned with no age check at all. When the feed stalled or market-data served a cached value, a pending DEMO limit "filled" on a price the live market was not offering, at `min(limit, stale_price)`. That flatters DEMO results and makes DEMO diverge from REAL, whose fills only come from Dhan.

**Fix:** `check_pending_fills` calls `_entry_quote_age_reason(tick)` and skips the order for this pass when it returns a reason (logged at info). The order stays PLACED, is re-checked next pass, and otherwise expires with its window (E199b). Same switches as entries: `ENTRY_MAX_QUOTE_AGE_S=0` turns the gate off, `ENTRY_QUOTE_AGE_UNKNOWN=allow` lets unknown-age ticks through. One old quote does not block other symbols' fresh ones.

**Behaviour change to be aware of:** with the default 10 s limit, DEMO needs a fresh tick to fill. If the DEMO cycle runs far less often than ticks refresh this is a non-event (get_quotes is re-read each pass); if DEMO quotes are served from a slower cache, raise ENTRY_MAX_QUOTE_AGE_S rather than switching it off.

**Tests:** tests/test_e204_demo_fill_refuses_old_quote.py (6). NOT RUN (no pytest/SQLAlchemy in the sandbox); py_compile only. The existing TestCheckPendingFills ticks are stamped "now", so they are unaffected. No replay / Time Machine path calls check_pending_fills (checked).
