# Group 442: Real Money Calibration control tab

One page, two sub-tabs (**Real Trade** and **Position Stocks Service**), that replaces typing commands on the VM:

| Section (inside each sub-tab) | What it does |
|---|---|
| **Switches** | Every real-money rule that has an ON / OFF (or mode / number) setting, grouped. Shows LIVE (effective at once) or RESTART (saved now, applied at the next restart), the recommended value, who changed what and when. Loosening a rule asks for a confirm. "Move to recommended" previews first. |
| **Daily trades** | Closed trades per IST day: entry time, exit time, hold, P&L, exit reason; totals by entry hour, exit hour, hold length, exit reason; CSV. |
| **Run plan** | The written Monday / daily / Friday / Saturday / monthly plan with a button on every line, what already runs by itself, the optional timetable, and what "G1, G2 ..." mean in this repo. |
| **Tests & training** | Every test, calibration, evidence and training job with its options and a Run button. |
| **Runs & downloads** | History. Open a run to watch it live; download its **txt** or a **zip** (txt + json / csv files). |

## Pieces

| Piece | Where | Role |
|---|---|---|
| Control kit | `services/{real-trade,position-stocks}-service/control/` (`kit.py`, `catalog.py`, `boot.py`) | `GET /control/flags`, `POST/DELETE /control/flags/{key}`, `POST /control/preset/recommended`, `GET /control/audit`. 32 switches per service. Overrides saved in a volume (`control-state-rt` / `control-state-ps`, `/data/control`) and re-applied at boot. Admin login required. |
| Static proof | `scripts/check_control_catalog.py` (CI) | A switch is marked LIVE only if every piece of code reading it reads it at call time. Otherwise the page says "restart needed". |
| Runner | `services/calibration-runner-service/` (port 8012, nginx `/calibration/`) | Runs the repo's scripts one at a time (nice'd, CPU 0.5), keeps `output.txt` + artifacts per run under the `runner-data` volume, optional IST timetable. Never changes a switch. |
| Page | `frontend/src/components/CalibrationControlTab.tsx` and `components/calibration/*`, `calibrationApi.ts`, `calibrationPlan.ts` | Tab "Real Money Calibration" (Trading group). Uses the same admin login as Auto Trade. |
| Reports | `scripts/daily_trade_report.py`, `scripts/control_snapshot.py` | The txt versions of "Daily trades" and "Switch snapshot". |

## Deploy (once)

```bash
cd ~/stockky-v2-main
docker compose up -d --build real-trade-service position-stocks-service calibration-runner-service frontend
bash deploy/deploy.sh          # or copy deploy/nginx-stockky.conf, nginx -t, reload: it adds location /calibration/
curl -s http://127.0.0.1:8012/health
```
Then open **Real Money Calibration**, log in (same admin password). If a service has no `/control` routes the page says so and names the rebuild command. `SESSION_SECRET` must be set (it already is for the other services).

Restarting a service is still done by you (`docker compose restart real-trade-service`), outside market hours. The page tells you which switches are waiting for one. It cannot restart anything by design.

## The run plan

IST. "Pack" = one button that runs several jobs in order.

### Monday (and after any restart or deploy)
| When | Run | Why |
|---|---|---|
| 08:45 - 09:15 | **Monday open pack** (health, switch snapshot, 09:30 check) | Everything up; every switch is what you think; no PENDING RESTART left. |
| 09:30 | Live check 09:30 | Depth-20 / order feed layout, quote age. |
| 10:15 | Live check 10:15 | Session verdict must read `open`. |

### Every trading day
| When | Run | Why |
|---|---|---|
| After 15:45 | **Daily close pack**: daily trades (entry / exit time, P&L), save records, save position-stocks ledger, score outcomes, structure refresh, TimesFM cache | Records and the ledger are kept only a few days on the server; saved copies feed the weekly reports and the opening replay. |
| After any switch change | Switch snapshot, then next day's daily trades | Compare with the days before. One day is not a result. |
| (automatic) 00:30 | Model retrain by GitHub Actions | The "Retrain" job here starts an extra one on the VM's training service; 409 = one is already running. |

### Friday after the close
**Friday review pack**: closed-trade report, hold buckets, opening-window verdict (SET / KEEP / NOT ENOUGH DATA), board parity, changed switches.

### Saturday 08:00 (market closed, 1-3 h)
**Weekly calibration pack**: history for 3 seeds, A/B replay, G1, walk-forward, G2 / G3 / G4, env-line pack, parity, completeness check. Then **G2 / G3 / G4 verdict**.

### First Saturday of the month
**Monthly evidence pack**: legacy gates, entry rules, P8 retest, ML V2, intraday VWAP / opening gates, TimesFM probe + combine, Kronos backtest. Needs the weekly history on disk; Kronos / TimesFM need their containers. G1 live (`latency_save` with `STRUCTURE_ENABLED=0`, then `latency_compare`) only when you re-test G1.

Heavy jobs are refused 09:00 - 15:45 IST on weekdays unless you confirm in the dialog. Exchange holidays are not known to the guard (it is stricter than needed on a holiday).

## What the gates are in THIS repo

| Gate | Meaning | Job |
|---|---|---|
| G1 | Structure engine fast enough (offline budget; live `/analyze` p95 within 15%) | `g1_check`, `latency_save` + `latency_compare` |
| G2 | Structure policy beats technical-only out of sample after costs | `ab_report` then `gate_check` |
| G3 | That answer is stable across seeds, halves, regimes, rules | `gate_check` |
| G4 | Live observations agree (>= 20 sessions, >= 30 scored rows per group) | `gate_check` |
| Gate 4f | Kronos forecast gate | `kronos_backtest` |
| Gate 4g / A | TimesFM range / volume vs ATR / HAR | `tsfm_probe`, `tsfm_combine` |
| A/B board | Engine board = legacy board maths | `board_parity` |
| **G5 - G8, "F3"** | **Do not exist in this repo.** Nothing is run or claimed for them. Tell me what they should test and they can be added as jobs. |

## What to do with a verdict
- **OK** (success): nothing, unless the headline lines say otherwise.
- **LOOK** (attention): the script ran and its own verdict is FAIL / NO_GO / NOT_ENOUGH_DATA. For G2-G4 that means: keep the kill switches ready (`STRUCTURE_POLICY_MODE`, `STRUCTURE_GATE_MODE`, `STRUCTURE_STOP_MODE` off), do not tune thresholds to make it pass. For board parity: leave the legacy board alone. For the opening-window verdict: SET means a candidate for `OPENING_ENTRY_NOT_BEFORE_IST`, a rule of thumb on a small sample.
- **FAILED**: it did not run properly. Open the txt: usually a missing input (run history first), an unreachable service, or a time-out.
- A switch the evidence says should change is changed on the Switches section, one at a time, and judged on several days of Daily trades.

## Safety design
- The runner has no shell: arguments are typed and bounded, scripts are the repo's own, one job at a time, `nice 10`, output capped at 5 MB, tokens redacted from txt and meta.
- Admin token never goes into a command line: jobs get a short-lived service token through the environment.
- Switch changes: only catalog keys, only validated values, never a secret-looking name; LOOSENING needs `confirm`; every change is in the change log.
- Not built, on purpose: restart from the page; changing broker credentials; placing or cancelling trades.

## Not verified in the authoring sandbox
FastAPI routes (`test_main_routes.py`, `test_group442_control_routes.py` run in CI), the docker build, nginx reload, the TypeScript build (`npm run build`; types were checked only against stubs, run `npm test` and `npm run build` on a machine with the registry), and any job against live services. Offline: 17 control-kit tests, static catalog check, 20 runner-engine tests (including a check that every flag the catalog passes exists in the script's `--help`), report and snapshot tests, frontend helper test.
