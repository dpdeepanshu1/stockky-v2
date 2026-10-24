# Market Weather, Part 1 step 3: the runner `regime/` package (23 Oct 2026)

Pure computation. Nothing calls `/control/overlay`, no trade, size or gate changes, `MARKET_STATE_MODE` stays `shadow`. The morning / intraday jobs are step 4.

## What was added (calibration-runner-service)
- `regime/data.py`: candle cleaning (bad rows dropped, never repaired; last row of a date wins; ISO / epoch dates), `upto()` = bars strictly before a day, one `fetch_history()` helper for scripts.
- `regime/features.py`: daily features (returns, ATR, 10-day vol, SMA20 gap, 20-day drawdown, VIX level / 1-day change / 250-day percentile), opening features (gap, gap in ATRs), intraday features (day return, fall from high, range in ATRs). A feature that cannot be computed is `None`.
- `regime/classifier.py` (`CLASSIFIER_VERSION=c1`): weighted stress score 0-100 over the features that exist, cut-offs 22 / 42 / 60 / 78 -> calm / normal / choppy / stressed / panic, plus hard floors (VIX >= 30 / 38, gap down 2% / 3%, day down 2% / 3%, VIX +25%). Too little data (< 35% of the daily weight) -> `normal`, `data_ok=False`: never tighten on a guess. `ratchet=True` lets the state only go up during the session.
- `regime/profiles.py` (`VERSION=c1-p1`): state -> overlay entries per service. Calm / normal = stamp only (`entries: {}`). Choppy / stressed / panic tighten: risk multiplier 0.8 / 0.6 / 0.4, conviction add 3 / 6 / 10, earliest entry 09:30 / 09:45 / 10:15, max entries 4 / 3 / 2, last entry 13:30 / 13:00 / 12:00 (position-stocks: max positions 4 / 3 / 2, scalp cut-off 13:30 / 13:00 / 12:00). `build_overlay(service, state, saved, now)` clamps against the SAVED values (a dial already as tight or tighter is skipped; an unknown or unreadable saved value is skipped, never guessed) and sets `until` = 15:30 IST (None after that time). These numbers are a first draft, to be judged by the replay and the shadow report.
- `scripts/regime_history_replay.py` + job `regime_history_replay` (group "weekly"): state for each past day from data up to the day before, compared with that day's range. Verdict USEFUL / USEFUL_NOT_BEYOND_ATR / NO_EDGE / NOT_ENOUGH_DATA (exit 0 only for USEFUL), against a plain "yesterday's ATR" baseline. The order check is noise-aware (a worse state is out of order only if lower by more than 2 standard errors).
- `Dockerfile`: `COPY .../regime /app/regime`.

## Tests
`services/calibration-runner-service/tests/test_regime_package.py` (28): data cleaning, no look-ahead in the replay (rewriting day D and later leaves D's morning state unchanged), classifier order / floors / missing data / ratchet, every profile entry TIGHTENS against the shipped value and is canonical for the real `kit.classify` / `kit.canonical` in both services, dials match both catalogs (key, kind, direction), expiry at 15:30 IST, Dockerfile copy, job registration, end-to-end script run.

## Not done / limits
- The replay has NOT been run on real history: this environment has no network. It was run only on synthetic data, which says the machinery works, not that the classifier is any good. Run the `regime_history_replay` job on the VM and read the verdict before step 4.
- The replay judges the index (range, move below the open), not your trades' P&L.
- Weights, cut-offs and profile values are first-draft constants; change them by a new `CLASSIFIER_VERSION` / `PROFILE_VERSION`, not silently.
- `pytest` is not installed here: the existing runner tests (except `test_main_routes.py`, which needs pytest at import) were run through a plain harness and pass; please run the full suite in CI.
