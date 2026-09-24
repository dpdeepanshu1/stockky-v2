# Session 99 (2026-09-24): secret-in-URL log leak, round 2 — the two services session98 left open, plus one more found along the way

## Where this started

Session98 fixed the Telegram-bot-token / Discord-Slack-webhook / CallMeBot-apikey
leak in four files across three services, and flagged the same leak class as
still open in two more:

- `market-data-service/main.py` — TwelveData, Polygon and AlphaVantage API keys
  in query strings
- `analysis-intelligence-service/news/main.py` — NewsAPI key in a query string

Both run `logging.basicConfig(level=logging.INFO)` and call `httpx`, so the same
mechanism applied: `httpx` logs every request URL at INFO, the key is part of
the URL, nothing mutes the `httpx` logger.

## Confirmed and fixed

**`market-data-service/main.py`** — all three waterfall fallbacks build the
request URL with the key inline:

    https://api.twelvedata.com/price?symbol=...&apikey=<key>
    https://api.polygon.io/v2/aggs/ticker/.../prev?...&apiKey=<key>
    https://www.alphavantage.co/query?...&apikey=<key>

Reproduced with a real `httpx.Client` (`MockTransport`): the key was in the log
on every call to any of the three. Fixed the same way as session98's
Telegram-token fix — a redacting `logging.Filter` on the `httpx` logger
(`?apikey=<key>` / `?apiKey=<key>` → `...=***`), installed idempotently at
import, plus the same scrubbing applied to the three `except Exception as e:
logger.debug(...)` fallback lines (belt-and-suspenders; those are below the
default INFO threshold today but shouldn't leak if someone raises verbosity).

**`analysis-intelligence-service/news/main.py`** — `_fetch_newsapi()` builds:

    https://newsapi.org/v2/everything?...&apiKey=<key>

Same reproduction, same fix shape (this service had no `apikey=` variant to
worry about, just NewsAPI's `apiKey=`). Also scrubbed the `"NewsAPI fetch
failed: %s"` warning-level exception log. This service had no test directory
at all before this session.

## Found while checking the rest of the platform: a third, lower-severity leak

Following the same multi-file check session98 used, I looked at every other
`httpx`/websocket caller for the same pattern. One more turned up:

**`position-stocks-service/feed/ws_client.py`** — the AngelOne feed URL carries
`clientCode`, `feedToken` and `apiKey` as query params (required — AngelOne's
feed protocol has no header-auth alternative). Nothing in this module logs
`ws_url` directly. But the `websockets` library itself logs the full request
line — path *and* query string — via its own `"websockets.client"` logger, at
DEBUG:

    websockets/client.py:
        self.logger.debug("> GET %s HTTP/1.1", request.path)

This service's `LOG_LEVEL` defaults to `INFO` (`config.py`), so today the line
is suppressed and the token/key are *not* leaking in the default configuration
— this is meaningfully less severe than session98's always-on-at-INFO leak.
But `LOG_LEVEL=DEBUG` is a real, supported env var, and turning it on to
debug an unrelated feed issue would silently put the feed token and API key
into the log with no code change required to trigger it. I reproduced this
with a real local `websockets.serve()`/`websockets.connect()` round-trip (both
the un-fixed leak and the fixed, redacted version).

**Fix:** the same redacting-filter pattern, installed at import on
`"websockets.client"` (and this module's own logger, for symmetry) — rewrites
`feedToken=...` / `apiKey=...` to `***` regardless of `LOG_LEVEL`.

I'm including this fix rather than only noting it, since it's the same secret
class as the two requested fixes and the closure is a small, self-contained
addition — but flagging the lower severity explicitly since it doesn't fit the
"leaking today, on every request" shape the other two do.

## What was added

- **`market-data-service/tests/test_provider_key_redaction.py`** — 10 tests.
  End-to-end (real httpx `MockTransport`, one per provider: TwelveData, Polygon,
  AlphaVantage — each proves the *request* carried the real key while the *log*
  didn't), filter idempotency, other `httpx` log lines untouched, a failed-call
  exception-text scrub, and a small parametrized table for `_redact_secrets`
  incl. `None`/no-secret inputs.
- **`analysis-intelligence-service/tests/test_newsapi_key_redaction.py`** — 8
  tests, same shape (first test directory for this service). Includes the
  no-key-configured early-return case and a failed-call exception-text scrub.
- **`position-stocks-service/tests/test_ws_client_secret_redaction.py`** — 7
  tests. The end-to-end case is a *real* local `websockets.serve()` /
  `websockets.connect()` round-trip with the feed token and API key in the
  query string, asserting the genuine DEBUG request-line log from
  `"websockets.client"` is redacted; filter idempotency (checked on both
  `"websockets.client"` and the module's own logger); other DEBUG lines from
  that logger (e.g. `"= connection is OPEN"`) left untouched; a
  `_redact_secrets` table.

**All three regression suites fail on the pre-fix files** — verified by
temporarily swapping back the original `main.py` / `ws_client.py` and
re-running: exactly the redaction-dependent tests fail (9/10, 6/8, 6/7
respectively), the rest pass unchanged, confirming these are true regressions
and not new tests that would pass regardless.

## Verification

- `market-data-service`: 24 passed (14 baseline + 10 new).
- `analysis-intelligence-service`: 8 passed (0 baseline — first tests; no
  regressions possible here since there was nothing to regress, but the
  reproduction-before-fix step above stands in for it).
- `position-stocks-service`: 1232 passed (1225 baseline + 7 new).
- Live reproduction (not just unit tests) for all three: real `httpx`
  `MockTransport` requests for the two API-key cases, and a real local
  `websockets` server/client round-trip for the feed-token case — each shown
  leaking pre-fix and clean post-fix.

## Post-delivery fix (VM run)

Running the full suites on the actual VM surfaced one real issue in this
session's own work: `tests/test_ws_client_secret_redaction.py` used
`@pytest.mark.asyncio`, but `pytest-asyncio` isn't in
`position-stocks-service/requirements.txt` and isn't installed on the VM, so
the marker was unrecognized and the test errored (`async def functions are
not natively supported`) instead of running. Every other async test in this
codebase avoids that dependency by wrapping `asyncio.run()` in a plain sync
test (see `tests/test_screening_support.py`); this test now does the same —
no new dependency needed. Verified passing with `pytest-asyncio` explicitly
uninstalled. Full suite: 1232 passed (unchanged count, now green on the VM
too).

Separately, the VM run showed one failure in
`real-trade-service/tests/test_feed_fanout_controls.py::
test_preview_quotes_use_same_bounded_path` (`assert 7 <= 6` on a real-socket
concurrency-bound check). Confirmed unrelated to this session: `feed.py` and
that test file are byte-identical to the pre-session99 zip, the
`asyncio.Semaphore(FEED_QUOTE_CONCURRENCY)` bound in `feed.py` is correctly
implemented, and the test passed 5/5 in isolated reruns — consistent with a
timing-sensitive real-thread/socket test flaking under load during a
127-second, 2200+ test run rather than a real regression. Left as-is.

## Left open

1. **`market_feed/feed.py`** (67% in real-trade-service) and
   **`entry_engine/entry.py`** (84%) — still the next coverage candidates per
   session98.
2. The small low-coverage modules session98 listed (`symbol_master.py` 29%,
   `shared_adaptive.py` 27%, `boot_forensics.py` 18%, `admin_auth.py` 61%,
   `shared_exposure.py` 76%, `event_depth_local.py` 40%, `tz_utils.py` 80%,
   `pipeline_status.py` 91%, `config.py` 90%) — unchanged this round.
3. `market-data-service` and `analysis-intelligence-service` otherwise have
   very thin test coverage (market-data-service: 3 pre-existing files plus
   this session's 1; analysis-intelligence-service: only this session's 1) —
   worth a coverage pass similar to what real-trade-service has had, if
   that's useful next.
4. Not investigated this round: whether any *other* logger in the codebase
   (beyond `httpx` and `websockets.client`) could echo a secret-bearing URL —
   this and session98 covered every `httpx`/websocket caller checked so far,
   but a fresh pass after future changes is worth doing rather than assuming
   this is now exhaustive.
