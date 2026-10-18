# Group 434e / 434f - TimesFM parameter record, offline probe, calibration guard, G3, live latency check

(Designed in the group 434 build logs, first packaged in group 436; the `434e` / `434f` names are kept so the logs still match. The group 434e line in `CHANGELOG_INDEX.md` about the origin filter is a different, earlier change.)

## What this is (and is not)
A TimesFM-style forecast is used for **parameters** - how wide the next 5 days will be, whether volatility is rising against the trailing month, whether
volume will be thin, how likely a close at or below the stop is - **never as a buy / sell direction**. Everything is OFF or log-only. Nothing in this group
changes an order, a size, a stop or a target.

## 434e - TimesFM
| Piece | What it does |
|---|---|
| `services/timesfm-forecast-service/` | FastAPI worker (port 8011, compose profile `timesfm`, NOT started by a plain `docker compose up`). `POST /batch` fills a nightly cache, `GET /features/{sym}?cached_only=true` reads it (the entry path never runs the model), `POST /forecast` is stateless (history given in the request) for the probe. |
| `tsfm_features.py` | Pure maths: quantile fan -> `fan_range_pct`, `forecast_vol_pct`, `vol_ratio`, `p_stop_close_max`, `p_target_close_max`, `volume_pace`. Stop / target touch is a LOWER bound (closes only; intraday lows are not modelled). |
| `backends.py` | `timesfm25` (local, Apache weights - the production-safe route), `bigquery` (AI.FORECAST), `research-3.0` (licence bars production: **refuses to build when `TSFM_TRADE_MODE=REAL`**, and unset means REAL), `fake` (wiring checks, no skill). An unknown `TSFM_BACKEND` is an error, never a silent fallback to another licence. |
| `entry_engine/timesfm_gate.py` + Gate 4g in `entry.py` | `TIMESFM_GATE_MODE` / `TIMESFM_GATE_DEMO_MODE` = `off` (default) or `log`. **There is no enforce mode**: any other value, `enforce` included, is treated as off (`TIMESFM_GATE_REQUESTED` shows what was asked). In `log` it reads the cache, records a hypothetical verdict and writes one `TIMESFM_GATE {json}` line per candidate. Service down / slow / stale = no opinion. `GET /timesfm/status` on real-trade shows counters. |
| `scripts/tsfm_probe.py` | **Gate A**: does the model beat ATR / EWMA / HAR (range) and trailing / EWMA volume, out of sample, pinball loss on q10/q50/q90, bootstrap over decision DATES, both history halves positive, last 6 months frozen as holdout. `--combine` needs a pass on every seed and at least 2 seeds. |
| `scripts/tsfm_nightly.py` | After the end-of-day bars: fills the service cache; exit 1 when the service is unreachable or > 40 % failed. |

### Choosing the route (your decision, not guessed here)
`TSFM_BACKEND=timesfm25` is the default and the only local route whose licence allows production. `research-3.0` is for the offline probe only.
`bigquery` sends price history to Google Cloud.

### Order of use
1. `docker compose --profile timesfm up -d --build timesfm-forecast-service`, then `docker compose --profile timesfm exec timesfm-forecast-service python selfcheck.py`
   (**first real test of the model adapter**: it has not run against real weights anywhere yet).
2. `python3 scripts/tsfm_probe.py calibration_out/hist_7 --out probe_7 --seed 7` (and 11, 13), then `--combine probe_*/probe_report.json`.
   `--no-model` first: it shows whether HAR beats ATR at all, which is useful on its own.
3. Only if Gate A passes: `scripts/tsfm_nightly.py` + `TIMESFM_GATE_DEMO_MODE=log`, collect `TIMESFM_GATE` lines, then a replay (Gate B) before anyone writes a blocking rule.

## 434f - calibration tooling
* `scripts/calibration_status.py` - a seed counts only when its history holds enough symbols (60 % of the manifest, else 50) and its A/B report has a real verdict and >= 30 signals. Prints the seeds to re-run.
* `scripts/vm_calibrate.sh` - the guard runs between fetch and A/B (a mostly failed download no longer feeds a meaningless A/B), `RESUME=1` keeps complete seeds, a whole-run status table is printed at the end, and the gate readout only sees complete seeds.
* `scripts/structure_gate_check.py` - **G3 (stability)** added next to G2 / G4: every seed must say `pass`, edges positive, both history halves positive, no regime flipping sign between seeds, and a per-rule table (`earns` / `hurts` / `unstable` / `neutral`). This is the check that would have caught the 424b mistake of keeping `chase` on two seeds.
* `scripts/analyze_latency_check.py` - the live half of G1: `--save` a baseline with `STRUCTURE_ENABLED=0`, then `--compare` with it on; FAIL above +15 % p95.

## What was NOT done / not verified
* No real model weights were available in the build environment: the `timesfm25` and `bigquery` adapters follow the vendors' documentation and are unproven until `selfcheck.py` runs on the VM. The Dockerfile install line (`git+https://github.com/google-research/timesfm.git`) is likewise untested here.
* The calibration run on seeds 7 / 11 / 13, G1 on real history, G2 / G3 results and the probe on real data all need the VM and market-data host. No such numbers exist yet.
* Test status when packaged (group 436): service logic, probe, nightly, calibration status, G3, latency check, `vm_calibrate.sh`, repo-wide script checks and the gate's pure logic all pass under a plain-assert runner. NOT run here: the gate's four DB-backed `entry.py` wiring tests (no SQLAlchemy in the build sandbox) and `services/timesfm-forecast-service/tests/test_main.py` through FastAPI's TestClient (its eight scenarios were run directly against the handlers instead). Run the real pytest suites on the VM / CI before trusting either.
