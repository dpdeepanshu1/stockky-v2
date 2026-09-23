# Session 26 — cross-service contract audit (2026-09-12)

## Scope
Continuing the "do all the remaining" sweep past real-trade-service into the
cross-service data contracts it depends on: api-gateway's `/surprise/ipo/list`
and `/surprise/scan` payload shapes (consumed by
`candidate_engine/candidates.py` and `watchlist_engine/sources.py`), and
market-data-service's `/live-quote`, `/quote`, `/history` shapes (consumed by
`market_feed/feed.py`) and analysis-intelligence-service's fundamental/
technical `/analyze/{symbol}` shapes (consumed by `_fetch_fund_tech_score`).

## Found & fixed — two significant candidate-pipeline bugs

**1. `candidate_engine/candidates.py`'s `_rows_from_ipo()` and the matching
IPO-normalizer in `watchlist_engine/sources.py`'s `_normalize_tier1()` read
the wrong field names.** `api-gateway/ipo_scanner.py` writes each IPO row's
score as `ipo_score` and its price as `current_price` — verified directly
against `ipo_scanner.py` (`result["ipo_score"] = ...`, `result["current_price"]
= ...`; no top-level `score`, `cmp`, or bare `price` key exists anywhere in
that module). Both real-trade-service readers were doing
`item.get("score")` / `item.get("cmp") or item.get("price")`, which always
returned `None`/`0.0`. In `_rows_from_ipo()` that meant
`score < MIN_CONVICTION` (55) was always true — **every IPO candidate has
been silently rejected since this function's 2026-09-02 "results vs items"
fix**, regardless of its real `ipo_score`/`decision`. Fixed both call sites
to read `ipo_score`/`current_price` first, keeping `score`/`cmp`/`price` as
fallbacks for schema-drift safety.

**2. `candidate_engine/candidates.py`'s `_rows_from_surprise()` gated on a
`decision` field that `api-gateway/surprise_scanner.py` never sets.**
Verified by grepping the entire `surprise_scanner.py` module — `score_stock()`
returns `symbol`/`score`/`tier`/`price`/`cmp`/etc., but never a `decision`
key, and the string `"decision"` doesn't appear anywhere in that file. The
gate `decision not in _ACTIONABLE_DECISIONS` was therefore always true (empty
string never matches `{"BUY NOW", "PREPARE TO BUY"}`) — **every surprise-scan
candidate has been silently rejected at any score since this function was
written.** The very next line's own `decision_label` fallback to `"BUY NOW"`
already showed the intended behavior for this source. Fixed to only enforce
the decision match when the field is actually present, otherwise falling
through to the `score >= MIN_CONVICTION` floor alone (which is this source's
real quality gate — `surprise_scanner.py`'s own tier logic already requires
score≥65/breakout or score≥35+change%/building before a row even reaches
`results`).

**Combined impact:** two of the three standard-track candidate sources
(`ipo`, `surprise`) have likely been contributing **zero** candidates the
entire time these functions existed, with no error/log signal distinguishing
this from a genuinely quiet day — this is a materially larger finding than
any prior-session fix in this codebase, on par with (or bigger than) the
original N+1/hard-reset bugs from the very first audit rounds.

## Verified
- `python3 -m py_compile` clean on both touched files.
- Real import of `candidate_engine.candidates` and `watchlist_engine.sources`.
- Functional test of `_rows_from_ipo()`: an 82-score `BUY NOW` IPO row (keyed
  `ipo_score`/`current_price`) now correctly produces a candidate; a 10-score
  row is still correctly rejected.
- Functional test of `_rows_from_surprise()`: a score-78 `breakout`-tier row
  with no `decision` key now correctly produces a candidate (`decision_label`
  defaults to `"BUY NOW"`); a low-score row and a row with an explicit
  `DO NOT BUY` decision are both still correctly rejected.

## Also re-confirmed correct (no changes)
- api-gateway `/stockky-hot`'s three bucket items (`news_driven`/
  `results_driven`/`bulk_insider_driven`) DO carry real `score`/`decision`/
  `price`/`close` fields matching `_rows_from_hot_picks()` exactly — that
  source track was already correct.
- market-data-service `/live-quote/{symbol}`, `/quote/{symbol}`,
  `/history/{symbol}` response shapes (`ltp`/`price`/`cmp`/`source`/`volume`/
  `candles`) all match what `market_feed/feed.py` reads.
- analysis-intelligence-service fundamental/technical `/analyze/{symbol}`
  response shapes (`fundamental_score`/`sector`/`sector_normalized`/
  `market_cap`, `technical_score`/`adx`) match `_fetch_fund_tech_score()`
  exactly, including the adaptive-parameter query params technical/main.py
  actually accepts.
- Minor, non-functional observation (not fixed, not worth touching):
  `market_feed/feed.py`'s `_get_preview()` tries a fallback path
  `/last-close/{symbol}` that doesn't exist on market-data-service — harmless
  dead code since the loop just moves on after a 404, and the primary
  `/quote/{symbol}` path already covers the same price fallbacks
  (`price`/`cmp`/`ltp`/`prev_close`/`close`).

## Status / next steps
Still not audited this round: the bulk of api-gateway (ipo_scanner.py's
internals, data_feed.py, surprise_premarket.py, hotpicks_store.py,
instant_scanner.py, buy_sniper.py — ~20k+ lines), all of
decision-prediction-service, notification-scheduler-service, and the
frontend. The two fixes above were the highest-value findings reachable by
checking "what does real-trade-service actually consume vs. what do the
upstream services actually emit" — the same technique that surfaced them
should be applied to any other yet-unaudited consumer/producer pair if the
audit continues.
