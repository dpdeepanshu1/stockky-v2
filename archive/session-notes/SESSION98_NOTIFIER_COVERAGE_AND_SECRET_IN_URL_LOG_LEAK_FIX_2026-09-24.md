# Session 98 (2026-09-24): notifier.py — direct tests, and a secret-in-URL log leak found and fixed in four services

## Where this started

The VM run you pasted confirmed session97-era state: **1972 passed, 1 xfailed,
94%** (that run pre-dated the db.py work), and showed `notifier.py` at **23%**
after reading 52% → 48% earlier. I reproduced 23% in a clean sandbox with and
without any of my tests, i.e. **not a regression** — `notifier.py` had no direct
tests, so its coverage was whichever unmocked `notify_*` calls happened to run
inside other tests. That instability is why it is this round's item.

## The finding: secrets in URLs were being written to the logs

`notifier._direct_telegram()` calls `https://api.telegram.org/bot<TOKEN>/sendMessage`
— the **bot token is part of the URL**. `httpx` logs every request at INFO as

    INFO:httpx:HTTP Request: POST https://api.telegram.org/bot123456789:AAH-…/sendMessage "HTTP/1.1 200 OK"

and `main.py:44` runs `logging.basicConfig(level=logging.INFO)` with nothing
muting the `httpx` logger. Reproduced with a real `httpx.Client` on the pinned
`httpx==0.25.2` and on 0.28.1: the token is in the log on every direct send.

Following the multi-file rule I checked every other service. The same defect —
and worse — was in the sibling senders:

| Where | What leaked |
|---|---|
| `real-trade-service/notifier.py` | Telegram bot token (fallback path only) |
| `position-stocks-service/notifier.py` | Telegram bot token (fallback path only) |
| **`notification-scheduler-service/notification/main.py`** | **Telegram bot token on EVERY alert** — this is the *primary* sender for the whole platform (all real-trade / position-stocks alerts are routed through it). Also **Discord and Slack webhook URLs** and the **CallMeBot apikey**, on every send |
| `notification-scheduler-service/scheduler/governance_check.py` | Telegram bot token |

Two further leaks in `notification/main.py`, same root cause as session96's
PIN leak: `_send_discord` / `_send_slack` call `resp.raise_for_status()`, and
httpx's `HTTPStatusError` message embeds the full URL. That text was logged
**and returned to the API caller** as `"failed: <exc>"` — so a revoked webhook
(404) put the webhook URL into the `/notify` (and `/test`) JSON response.
Reproduced by the tests against the original code:
`failed: Client error '404 Not Found' for url 'https://discord.com/api/webhooks/<id>/<token>'`.

### Fix

- **`real-trade-service/notifier.py`**: a redacting `logging.Filter` on the
  `httpx` logger (`/bot<id>:<secret>/` → `/bot***/`), plus `_redact_token()`
  applied to the transport-error text it logs itself. Installed idempotently at
  import.
- **`position-stocks-service/notifier.py`**, **`governance_check.py`**: same
  filter (Telegram pattern).
- **`notification-scheduler-service/notification/main.py`**: a fuller
  `_redact_secrets()` covering Telegram, Discord (`/api/webhooks/<id>/<token>`),
  Slack (`hooks.slack.com/services/…`) and CallMeBot (`apikey=`), installed as
  the same `httpx`-logger filter **and** applied to the Discord/Slack/Telegram
  failure strings (log line *and* returned `failed: …`) and to CallMeBot's
  per-user error text.
- Verified: after the fix the httpx line reads
  `HTTP Request: POST https://api.telegram.org/bot***/sendMessage "HTTP/1.1 200 OK"`.

### Action for you (code can't do this part)

If any of those logs were ever shipped, pasted, or read by someone else, treat
the secrets as exposed. On the VM:

    docker compose logs notification-scheduler-service 2>&1 | grep -cE "api.telegram.org/bot|discord.com/api/webhooks|hooks.slack.com/services|apikey="
    docker compose logs real-trade-service 2>&1 | grep -c "api.telegram.org/bot"

Non-zero ⇒ rotate: Telegram bot token via @BotFather `/revoke`, regenerate the
Discord/Slack webhooks, and re-issue the CallMeBot apikey — then update them on
the Alert panel / `.env`. (The log filter only protects new lines.)

## What was added

**`real-trade-service/tests/test_notifier.py` — 64 tests, `notifier.py` 100%
(108/108), stable.** Everything goes through real httpx (`MockTransport`
behind `httpx.post` / `httpx.AsyncClient`), so requests, exceptions, timeouts
and the genuine INFO log line are real. Only the transport handler and a
monotonic clock are fake.
- Dedup: first/duplicate/exact-300s boundary, distinct texts independent, a
  suppressed duplicate does **not** extend the window (a drip every 100s can't
  suppress forever), 64-entry LRU + eviction order, refresh moves a key to the
  end, lone-surrogate text.
- `notify_sync` / `notify_async`: service delivered (exact URL, JSON body, 12s
  timeout); not-delivered / 400 / 404 / 500 / 503 / unreachable / read-timeout /
  invalid JSON each fall back to direct Telegram and return its result; async
  fallback runs in a worker thread (blocking `httpx.post` kept off the loop).
- `_direct_telegram`: missing token / chat id, exact request, `*bold*`→`<b>`,
  HTML-mode 400 → plain-text retry with the original text and no `parse_mode`
  (the fix for `<`/`&` in Dhan error text), retry rejected / raising,
  transport error, 200-char body truncation.
- Token hygiene: end-to-end (real httpx line), through the `notify_sync`
  fallback, transport-error text, the filter and `_redact_token` directly
  (URL segment, lookalikes `/robots.txt` and `/bots/list` untouched, configured
  token replaced only if ≥8 chars), never raises, idempotent install.
- Module wiring in a fresh interpreter: default `NOTIFICATION_SERVICE_URL` and
  trailing-slash strip.

**`position-stocks-service/tests/test_notifier_token_redaction.py` — 4 tests.**
**`notification-scheduler-service/tests/test_telegram_token_redaction.py` — 21
tests** (that service's *first* test directory, with `__init__.py`): Telegram,
Discord, Slack, CallMeBot end-to-end, the revoked-webhook return-string case,
governance sender, regex table incl. lookalikes, filter behaviour, idempotency.

## Verification

- `real-trade-service` (VM-equivalent, pgserver hidden): **2253 passed,
  1 skipped, 1 xfailed**, overall **96%**, `notifier.py` **100%**,
  `tests/test_notifier.py` 100%.
- `position-stocks-service`: **1225 passed** (1221 + 4). `notification-scheduler-service`: **21 passed** (new).
- **The regression tests fail on the pre-fix code**: run against the original
  `notifier.py`, exactly the 15 token-hygiene tests fail and the other 49 pass
  (i.e. delivery behaviour is unchanged); against the original sibling files the
  end-to-end tests fail with `leaked 'discordSECRET'` / `'slackSECRET'` etc.
- **Mutation-checked** (`real-trade-service/notifier.py`): 52 deliberate
  regressions — dedup window / cache size / boundary `<`→`<=` / eviction order
  / a duplicate refreshing the window, every timeout, URL, payload field,
  `parse_mode`, retry payload, retry status, the filter not installed / not
  rewriting / keeping args / returning False / raising, regex too broad or
  missing `-` / `:`, literal replacement off or min-length 1, default URL,
  `rstrip` removed, … **0 survivors.** (One first-pass "survivor" was a bad
  mutation pattern matching two places, not a weak test.)

## Observations (not changed)

1. **Dedup is consumed before delivery.** `_should_send` records the message
   *before* any channel is tried, so if every channel is down the retry of the
   same text returns `True` ("sent") without being attempted for 5 minutes.
   Deliberately left: it also stops a dead channel turning into repeated 42-second
   stalls (next point). Pinned by a test.
2. **`notify_sync` can block its caller ~42s** (service 12s + Telegram 15s +
   plain-text retry 15s) and is called inline from `exit_engine/exit.py`
   (dozens of call sites). The docstring says a notification failure must never
   block an order path. Pinned (`[12.0, 15.0, 15.0]`) so any change is conscious;
   the real fix is fire-and-forget (thread / `notify_async`) and is a design
   change, not a test round.
3. Any alert containing `<` or `&` (common in Dhan error text) always fails
   Telegram's HTML mode first, logs a WARNING, and succeeds only on the
   plain-text retry — correct, but costs a second request every time.
4. Carried over: session97 notes (exec_ddl_safe hides DDL errors; upgraded DBs
   lack the `watchlist_entry_id` FK); session96 notes (missing encryption key
   doesn't fail closed; no-token TOTP response isn't Telegrammed; `DHAN_PIN`
   unvalidated; past `expiryTime` stored as-is).

## Still open, in priority order

1. **Same secret-in-URL class, other services — not fixed here.** API keys are in
   the query string of URLs fetched with httpx in `market-data-service/main.py`
   (Twelve Data `apikey=`, Polygon `apiKey=`, Alpha Vantage `apikey=`) and
   `analysis-intelligence-service/news/main.py` (NewsAPI `apiKey=`), and those
   services also run `basicConfig(level=INFO)`. If they call httpx (they import
   it) the keys are in *their* logs the same way. The simplest systemic fix is
   one line in each entrypoint, `logging.getLogger("httpx").setLevel(logging.WARNING)`,
   or the filter used here. Worth doing before more coverage work.
2. `market_feed/feed.py` (66%), `entry_engine/entry.py` (84%)
3. small modules: `symbol_master.py` 29%, `shared_adaptive.py` 27%,
   `boot_forensics.py` 18%, `admin_auth.py` 61%, `shared_exposure.py` 76%,
   `event_depth_local.py` 40%, `tz_utils.py` 80%, `pipeline_status.py` 91%,
   `config.py` 90%
4. `offline_test_harness.py` (288 stmts, 0%) is a dev harness — consider
   excluding it from coverage.
