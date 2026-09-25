# Session 112, round 16 (2026-09-25): `execution/dhan_client.py` coverage 21% → target ~95%+ (position-stocks-service)

Next item by priority off round 13/14's list, now that `db.py` is closed
out (round 14/15). `execution/dhan_client.py` was the largest remaining
gap: 419 statements, 332 missing, no test file at all. This is the ONLY
module in this service allowed to hold a decrypted Dhan credential or
call Dhan's API, so a regression here is a silent trading-safety bug
(wrong tick rounding, a swallowed order-type mismatch, a Super Order
MARKET payload regressing back to the exact session41 incident this
file's own comments describe), not a cosmetic one.

## What was added

`tests/test_dhan_client.py` — written fresh. real-trade-service has its
own `tests/test_dhan_client.py` + `tests/test_dhan_client_remaining_
coverage.py`, but this service's copy has diverged enough (different
credential source — `dhan_credentials_ro`, not `dhan_credentials` — plus
this service's own additive functions: `place_super_order`/`modify_
super_order`/`cancel_super_order`, `convert_position`, `get_trade_
history`, `place_cnc_stop_loss_market`/`cancel_cnc_stop_loss_order`) that
only the tick-rounding/classifier logic ports directly; everything else
needed writing against this file's actual functions.

Two testing techniques, both matching this repo's existing conventions:
  * Pure functions (`tick_size_for_price`, `round_to_tick`,
    `is_valid_tick_price`, `_extract_data`, `_clean_security_id`,
    `_add_security`, `_first_present`, the 4 rejection classifiers) are
    exercised directly — no mocking.
  * Every SDK-facing function is tested by monkeypatching
    `_get_sdk_client` to return a `SimpleNamespace`-based fake client
    (mirrors `tests/test_notifier_core.py`'s `SimpleNamespace`-fixture
    style already in this repo), so the REAL function body runs end to
    end — `get_funds`, `get_order_list`, `get_trade_history` (multi-page,
    the "no trade" break, the repeated-page-key break), `place_order`
    (invalid order_type, MARKET price-forced-to-0, LIMIT tick rounding +
    invalid-tick raise, the MARKET→LIMIT echo false-alarm fix vs. a
    genuine mismatch, non-blocking verification-failure swallow),
    `cancel_order`, `place_super_order` (MARKET direct-HTTP path incl. the
    target/stop-loss tick-collapse bump/clamp, `dhan_http` missing raise,
    LIMIT path incl. missing-price raise and the `tag`-kwarg `TypeError`
    retry), `get_super_order_list`/`cancel_super_order`/
    `modify_super_order` (unsupported leg, missing price per leg),
    `convert_position`, `edis_inquire`/`edis_verification_summary` (all 3
    `verified_today` outcomes plus the not-connected/generic-failure/
    unrecognized-shape paths), and `place_cnc_stop_loss_market`/
    `cancel_cnc_stop_loss_order` (every one of its 4 raise conditions plus
    the confirmed-live success path).
  * `_get_sdk_client` itself and `_load_security_cache`'s CSV-fallback
    branch additionally monkeypatch `auth.dhan_credentials_ro.get_
    decrypted_credentials` and real `httpx.get` (via `httpx.Response(...,
    request=...)`, same pattern `test_notifier_core.py` uses for outbound
    calls) rather than a fake client.

No production bug found this round (unlike rounds 7/8/14, which each
found one) — the file's own extensive session-history comments already
describe several real incidents (session40/41/48) and their fixes in
detail; this round's read-through didn't turn up anything those fixes
missed.

## Verification

Neither `sqlalchemy` nor `httpx` nor `dhanhq` is installed in this sandbox
(no network access to install them — same limitation noted in round 14/15),
so the actual pytest file wasn't run here. What WAS verified directly,
with no dependencies, by extracting the exact function bodies into a
standalone script and asserting every numeric/string case the test file
exercises: `tick_size_for_price`/`round_to_tick`/`is_valid_tick_price`
(all band boundaries, the Wockhardt ₹2097.05 regression, the
`place_order`/`place_super_order` tick-collapse bump/clamp math), all 4
rejection classifiers against the real Dhan rejection strings quoted in
the module's own comments, and `_clean_security_id`'s regex. All passed.
The SDK-facing tests (the bulk of the file) were traced by hand against
`dhan_client.py`'s actual source line by line (payload shapes, kwarg
names, branch conditions) rather than executed — flagging that the VM's
real pytest run is what confirms those; if anything doesn't collect
cleanly, it's most likely a fake-client method name/signature mismatch
in the test file itself (easy to spot from the failure), not a change
needed in `dhan_client.py`.

`_get_sdk_client`'s own SDK-probe test tolerates `dhanhq` not being
importable (catches `ImportError`/`RuntimeError`), same pattern
real-trade-service's own dhan_client tests already use for the identical
probe — the no-creds guard and the import/fallback branch are still
exercised either way.

## Next by priority (unchanged from round 13/14's list, minus this item)

`feed/*` (`scrip_master.py` 22%, `ws_client.py` 30%, `angelone_session.py`
41%), then `main.py` (22%, the largest remaining file by statement count).
