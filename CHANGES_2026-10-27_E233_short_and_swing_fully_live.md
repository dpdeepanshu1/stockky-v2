# E233 - short selling and the swing lane ship FULLY live (2026-10-27)

You asked for both lanes fully live. E224 had made them "live" but kept three rails that kept real money out. Two are lifted here; the third is evidence, not a setting.

## Changed (defaults only; `.env` and the Switches page still win)
- **Swing lane, REAL money:** `SWING_LIVE_REAL_ENABLED` ships `true` (was `false`), so `SWING_MODE=live` buys CNC swing picks in DEMO **and REAL**. `false` returns to DEMO only.
- **Short selling, size:** `SHORT_LIVE_QTY_CAP` ships `1000` (was `1`), so the normal short sizing (risk budget, cost gate, depth caps, `SHORT_MAX_POSITIONS`) decides the quantity instead of a 1-share cap. `1` returns to single-share shorts. A junk value still reads as 1.
- Catalog entries, `.env.example`, `docs/ENV_REFERENCE.md` (regenerated), compose comment and the swing headline text updated.

## NOT changed (on purpose)
- **`SHORT_LIVE_REQUIRES_PROBE` stays true.** A live short is still refused until the Dhan SELL probe (runner job `dhan_sell_probe`, one real 1-share round trip you tick) has a PASS on record. I cannot pass it from here, and it is the check that Dhan accepts the SELL Super Order and the cover. Until it passes, shorts log `SHORT_PROBE_NOT_PASSED`. If you would rather skip it, set `SHORT_LIVE_REQUIRES_PROBE=false` (a refused SELL Super Order opens no short, and the plain-order fallback always places a broker stop).
- All brakes unchanged: armed + module on + auto-pilot on (E222), broker stop on every short, 14:55 cover, kill switch, loss brakes, swing wiring checks (`GET /swing/status` -> `wiring.problems`: gate armed, EOD square-off ON, eDIS morning check, fresh evening scan), `SWING_MAX_TRADE_VALUE` 20000, `SWING_MAX_EXPOSURE_PCT` 40, 2 positions, 1 per sector. Both settings stay on the auto-calibrator's PROTECTED list.
- `SWING_BROKER_STOP_ENABLED` stays false: `BROKER_STOP_ENABLED` (true) already puts a resting stop on every carried position.

## Things to know before Monday
- With about 18k capital a swing entry is capped by the 40% exposure rule (about 7k per position), not by the 20000 value cap.
- REAL swing needs the REAL gate armed and its EOD square-off switch ON; otherwise `wiring.problems` lists it and entries are refused. That is the likeliest reason "nothing happened".
- The swing lane has no 30-trade net-positive shadow evidence in this zip; I did not check live results.

## Tests
Run here with pytest and the real service requirements: real-trade `test_swing_lane.py` + `test_swing_e138_wiring.py` 49 passed (one test updated because REAL now ships on, one new: an unarmed REAL gate is flagged); position-stocks `test_short_live.py` + `test_short_foundation.py` 20 passed; `scripts/test_short_live.py` 93 OK; `swing.test.mjs` and `positionSide.test.mjs` pass. NOT run here: the full service suites, `tsc`, `vite build`.

## Run on your VM
    bash scripts/docker_test.sh position-stocks-service
    bash scripts/docker_test.sh real-trade-service
Rebuild/restart both services, check `.env` has no old `SWING_LIVE_REAL_ENABLED=false` / `SHORT_LIVE_QTY_CAP=1` line (it would win), open Calibration -> Swing lane and Short selling, then start the `dhan_sell_probe` job.
