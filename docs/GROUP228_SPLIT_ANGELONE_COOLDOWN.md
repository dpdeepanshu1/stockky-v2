# Group 228 - AngelOne cooldown split into candle and quote families

Cumulative on group 227. Item 1 of the 2026-10-07 10:33 IST log review (items 3 and 4 are still open).
Rebuild: `docker compose build market-data-service && docker compose up -d`.
Files changed: `market-data-service/angelone_budget.py`, `angelone_client.py`, `angelone_ws_feed.py`, `.env.example`,
tests `test_group227_split_cooldown.py` (new, 34), `test_group211_angelone_budget.py` and `test_group225_plain_text_403.py` (updated).
Note: the new test file is `test_group227_split_cooldown.py` (numbered before the group was renumbered 228); the group is 228.

## What the log showed (13:39 IST boot)
1. A `/history?period=1mo` burst (BDL, GRASIM, SWIGGY ...) got `getCandleData` HTTP 403 "exceeding access rate": trip #1 (30 s), then trip #2 (60 s).
2. Group 211 treated that as one global cooldown. Every quote caller then logged `AngelOne-first did not price X (global AngelOne cooldown (403) is running)`.
   Yet quotes had just answered normally (`quotes/bulk: AngelOne REST resolved 89/89 symbols`): AngelOne limits each endpoint on its own.
3. The feed poll stopped (`52 of 499 tokens polled`), `/quote` fell to yfinance (`yf.download bulk failed: 18s hard timeout`, "rate-limit bucket saturated"),
   real-trade priced 306 of 653 symbols and about 50 `/quote` calls ReadTimed out.

## Change
`angelone_budget.py` keeps one cooldown state per family:
- `candle`: getCandleData (and any provider name containing "candle").
- `quote`: ltpData, quote batches, the feed poll, gainers/losers.
A 403/429 trips only its own family. Each family has its own 30 s -> 60 s escalation, its own "late answer, ignore" rule and its own trip count.
- `trip(endpoint)` picks the family; `skip(lane, family)`, `in_global_cooldown(family)`, `cooldown_remaining(family)` take one. With no family they
  mean "any running cooldown" (the old behaviour; used by status and by callers that name none).
- `admit()` checks the cooldown of the provider it is admitting for (a candle admit ignores a quote cooldown and the reverse).
- `angelone_client.get_candles` checks the candle family; quote, batch and gainers check the quote family. `angelone_ws_feed` checks the quote family
  at the start of a cycle, between batches and in its HTTP-denied safety net, so a candle cooldown no longer stops the poll.
- `GET /angelone/budget`: new `split_cooldown` and `cooldowns` {quote, candle} each with `active`, `remaining_s`, `trips`. The old keys
  (`global_cooldown_active`, `global_cooldown_remaining_s`, `trips`) now mean "any family" and "sum".
- `ANGELONE_SPLIT_COOLDOWN=0` puts every endpoint in one family again (exactly the group 211 behaviour).

## Effect to expect
- A candle 403 now costs only candle work: `/history` falls to yfinance for 30-60 s as before, but quotes, `/quotes/bulk`, the feed poll and the
  held-symbol lane keep using AngelOne, so yfinance is no longer flooded and real-trade stops timing out.
- The log line becomes `AngelOne budget: rate-limit answer from getCandleData - AngelOne candle callers skip AngelOne for 30s`.
- The miss text `global AngelOne cooldown (403) is running` now only appears for a quote-family cooldown.

## Not changed / limits
- This does not reduce candle calls. Group 227's rejection cache does that; candle bursts will still trip the candle limit sometimes.
- During a candle cooldown `/history` still goes to yfinance, which is slow under load. Item 3 (empty 200 vs error/stale, persist last-good candles)
  and item 4 (exit-cycle deadline) are separate.
- If AngelOne ever blocks the whole account/IP, quote calls get their own 403 and trip their own cooldown, so nothing is lost versus before.
- A real-trade/api-gateway change was not needed.

| Env (market-data-service) | Default | Meaning |
|---|---|---|
| `ANGELONE_SPLIT_COOLDOWN` | 1 | per-family cooldowns; `0` = one shared cooldown |

## Tests
`cd market-data-service && python -m pytest tests -q`: 1079 passed. `angelone_budget.py` 100% covered.
Updated because they asserted the old rule "a candle 403 stops quotes": group 211 (5 cooldown-state tests + the candle/quote test) and group 225 (the candles case).
