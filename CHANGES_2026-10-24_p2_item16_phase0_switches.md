# P2 item 16 (the page part): VIX, pre-open gap and ATR constants onto the Switches page - E106

Position Stocks. **No behaviour change at the shipped values**: the code already read every one of these at call time; they were only editable by `.env` and a restart. Now each is on the Real Money Calibration Switches page (live, with a range, a "looser" direction and the confirm step for a loosening change).

## Added switches (all `live=True`, shipped value = the real config default, checked by `check_control_catalog.py` and `test_group477_catalog_matches_config`)
| Switch | Shipped | Looser | What it does |
| --- | --- | --- | --- |
| `VIX_CAUTION_ABOVE` | 18.0 | higher (0 = off) | India VIX at or above it shrinks a new entry by the caution size |
| `VIX_SPIKE_PCT` | 10.0 | higher (0 = off) | VIX up this % on the day does the same |
| `VIX_CAUTION_SIZE_MULT` | 0.7 | higher (1.0 = no shrink) | the shrink |
| `PREOPEN_GAP_UP_BLOCK_PCT` | 6.0 | higher (0 = off) | no buy of a stock that indicates a gap-up this big, until the gate time |
| `PREOPEN_GAP_DOWN_BLOCK_PCT` | 3.0 | higher (0 = off) | same for a gap-down |
| `PREOPEN_GAP_GATE_UNTIL_IST` | 10:00 | earlier | when the two gap blocks stop |
| `OPENING_GATE_MIN_STOP_ATR_FRAC` | 0.3 | lower (0 = off) | in the opening window the stop may not be tighter than this x daily ATR % |
| `SCALP_TARGET_CAP_MIN_STOP_ATR_MULT` | 1.0 | lower (0 = off) | stop floor after a target cap, x bar ATR |

All eight are on the calibrator's PROTECTED list (a report can never move them; the reason is shown on the page).

## What was NOT done, and why
- **`WINDOW_AUTODISABLE_MIN_TRADES` 8 to 30** is still waiting for your word: it makes a losing window disable itself later, so it loosens a safety.
- **VIX caution as "above its own 12-month 75th percentile" and the pre-open gap as a multiple of each stock's own ATR** change what the live gates do. They need your go-ahead and, for the VIX rule, the cached VIX history; this change only puts the current fixed numbers on the page so they can be tuned without a restart in the meantime.
- Position Stocks has no entry "conviction floor" (that is a real-trade setting; here only `TOP_PICKS_MIN_CONVICTION` exists, and it filters preview picks, not orders), so nothing was added for it. The opening-wait time is already a calibrated dial (`OPENING_ENTRY_NOT_BEFORE_IST`).

## Proof
`tests/test_phase0_switches.py` (5 tests, stdlib only: live, config target, shipped equals the real default, loosening directions and `zero_off`, protected, unique; a changed shipped value is caught). `scripts/check_control_catalog.py` (every consumer reads at call time), the group-477 check run in a clean interpreter (103 switches, no drift, none invalid), `test_control_kit` (20), `test_regime_overlay` (19), `check_shared_copies`, `test_env_review_check`. Not run here: pytest and SQLAlchemy (pypi blocked), so `test_autocalib.py` (it checks every protected key is a real switch) and the frontend build are for the VM.
