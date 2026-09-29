# Session 140 — api-gateway coverage pass 8: hotpicks_store (2026-09-30)

- New: `services/api-gateway/tests/test_hotpicks_store.py` (300 tests). No production code changed.
- Covered (523 statements, 170 branches, 100% / 100%): stop flag, `_schema` lazy import, `_lob_to_str`, `_num`, `_int`,
  `_utc_hours_since` (aware / naive / non-UTC offset / future / unusable), `_row_to_item` (JSON authoritative, columns only
  fill gaps, bad / non-dict / CLOB json, `stored_at` / `stored_generated_at`), `_payload_rows` (section order, blank
  symbols, 40 / 20 char clips, unserialisable item, every ROW_KEYS bind present), `hotpicks_db_upsert` (Postgres
  executemany vs Oracle row-by-row, separate prune txn, prune failure swallowed, engine always disposed),
  `hotpicks_db_payload`, `hotpicks_db_freshness_hours`, `hotpicks_audit` memo (TTL boundary exclusive, copies not shared,
  failed audits cached) + `_hotpicks_audit_uncached` (every early-return branch, counts, price-only `missing_data`,
  200-row cap, health score, stale / empty / all-scores-missing issues), `ensure_hotpicks_schema`, `_row_needs_scores`,
  `hotpicks_repair_batch` (target selection, limit clamp 1..100 default 15, price-key priority, MAX_STOCK_PRICE gate,
  per-symbol isolation, 15000-char cap, audit-cache invalidation) and `hotpicks_repair_scores` (DECISION_URL handling,
  72h window, field copy rules incl. "N/A" / None / score 0, reasons only replaced by a list, no-write when nothing useful).
- Hermetic: fresh module copy per test; `hotpicks_schema`, `sqlalchemy`, `httpx` are fakes in `sys.modules`; the module's
  `time` is a fake clock so the repair loops never sleep and the audit TTL is deterministic.
- Drift guards against the REAL `hotpicks_schema`: every `hp.<name>` the store calls exists there; `_payload_rows` emits
  exactly `ROW_KEYS`; every column `_row_to_item` reads is in `SELECT_COLUMNS`; a write -> read round trip on both
  dialects (payload -> rows -> real `adapt_rows` -> `_row_to_item`, incl. non-ASCII summary and Oracle 1/0 `from_scan`).
- Verification: REAL pytest + pytest-cov this time (the sandbox had pypi access). Full gateway suite:
  `bash run_tests.sh --single` -> 1648 passed (1348 + 300); `hotpicks_store.py` 100% with `--cov-branch`; gateway TOTAL
  18% -> 22%. 59 hand-made mutants: 58 caught, 1 equivalent (`isinstance(value, str)` short-circuit in `_lob_to_str`
  returns the same string either way). Two tests were added after the first mutant round (freshness boundary and
  `age_hours` rounding in `hotpicks_db_payload`).
- Observations, left unchanged (none pinned by a test, so a fix will not break the suite):
  * `hotpicks_repair_scores` never sets `out["attempted"]` (stays 0). main.py's repair endpoint sums `attempted` from the
    price and score passes, so score repairs are never counted in that total.
  * `hotpicks_repair_batch` parses `blob.get(...)` outside its per-key try: an `item_json` that is valid JSON but not an
    object (e.g. `[1]`) raises AttributeError and aborts the WHOLE run as status "error". Rows written by the upsert are
    always objects, so latent.
  * `_hotpicks_audit_uncached` wraps BOTH price keys in one try: a non-numeric `price` (e.g. "N/A") hides a valid `close`,
    so that row is counted as missing a price. `hotpicks_repair_batch` handles the same row correctly (per-key try).
  * Both repair functions write `json.dumps(blob)[:15000]`, but the upsert allows `HOTPICKS_JSON_MAX_BYTES` = 30000. A
    blob between 15000 and 30000 chars would be cut mid-JSON by a repair; `_row_to_item` then silently falls back to the
    typed columns. Latent (real blobs are far smaller).
  * `hotpicks_repair_scores` sets `blob["reasons"] = None` when the decision service returns a non-list truthy `reasons`
    and the stored blob had none. Cosmetic.
- Next (pass 9): Tier 4 — `refill_additional` (133 statements).
