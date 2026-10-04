# group127 (2026-10-04) - AngelOne movers sweep: flag rate-limited (partial) results

Cumulative on group126. Run `bash run_tests.sh` in market-data-service on the VM.

## What the post-deploy boot log showed
- `AngelOne quote(batch) returned HTTP 403 - body: Access denied because of exceeding access rate` during boot, then `AngelOne movers: +99 symbols (status=ok, universe=2710, quotes_fetched=2610)`.
- The sweep walks the whole NSE-EQ token map in batches of 50. When AngelOne's rate limit trips, the client enters a cooldown and the remaining batches return nothing; the route just skipped them and still answered `status: "ok"`, and cached that answer for the normal TTL. Here 100 of 2710 symbols (two batches) were missing.

## Change (`market-data-service/main.py`)
- `_movers_sweep_coverage(fetched, universe)` returns `(is_partial, missing)`. Partial = under 98 % of the universe came back (`_MOVERS_MIN_COVERAGE`).
- A partial result still returns `status: "ok"` with the rows it has (so api-gateway keeps working unchanged) plus `partial: true` and `missing_quotes: N`, logs one WARNING, and is cached for 120 s (`_MOVERS_PARTIAL_TTL_S`) instead of the full TTL, so the next movers poll retries the sweep.
- Complete sweeps behave exactly as before.

## Tests
`tests/test_angelone_movers_partial_sweep.py` (5 tests). Sandbox has no fastapi/pytest: the helper and constants were run standalone from the real source (5/5 pass); the test file compiles, not run under pytest.

## Findings from the same log (no code changed)
- **Fixed things confirmed:** group 121 line shows `0 new/updated row(s), 10 already stored ... 0 failed`; group 124 names unresolved symbols; no TzCache lines; no "Neon" wording; `/quote` calls are scattered, not a ~1,000-call wall (some are stockky-hot's 104 symbols).
- **AngelOne unresolved symbols are real stocks:** `HFCL, MTARTECH` (boot list) and `HFCL, HINDCON, SHREETNB, STLTECH, WARDINMOBI` (live universe). `angelone_scrip_master.py` keeps only rows whose trading symbol ends in `-EQ`, so a name listed under another series (e.g. `-BE`) is dropped. Quotes for these fall through to Yahoo, so nothing breaks, but I could not check the scrip master from the sandbox. To see what AngelOne lists for them, grep the scrip-master JSON on the VM for `"HFCL"` and look at the `symbol` field.
- **Hugging Face sentiment is dead:** `HF API call failed: [Errno -5] No address associated with hostname`. `news/main.py` still posts to `api-inference.huggingface.co`, which Hugging Face has retired in favour of `router.huggingface.co` (different request format). Until it is migrated every headline gets the neutral 0.0 fallback, so news sentiment from this call contributes nothing. Not changed: the new endpoint's payload, the model, and the token's permissions can't be checked from here.
- **NSE cookie 403 / "Fetched 0 securities from NSE"** unchanged (datacenter IP; bhavcopy fallback covers it).
