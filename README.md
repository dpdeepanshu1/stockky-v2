# Stockky v2

Multi-service stock-intelligence and trading system for the Indian market (NSE). It scans and scores stocks, decides, and can place
real orders through Dhan (real-trade-service and position-stocks-service). **Real money is involved**: keep every new trading switch
OFF/shadow until it has evidence, and read `CHANGELOG.md` before changing money paths.

This repo keeps exactly two hand-written Markdown files: **this README** (how to run, test and release) and **CHANGELOG.md** (what changed, newest
first). Do not add per-release `CHANGES_*.md`, `docs/*.md` or session-note files: add a bullet to `CHANGELOG.md` instead.
The one generated reference is `docs/ENV_REFERENCE.txt` (every environment variable the code reads).

## Services (`docker-compose.yml`)

| Service | What it does |
|---|---|
| `frontend` | React + Vite UI (nginx in the container; the same code is built on Vercel from `backup-production`) |
| `api-gateway` | Public entry: watchlist, scans, Hot Picks, IPO tracker, Data Feed, price alerts, auth for admin calls |
| `market-data-service` | Quotes, candles, fundamentals. Provider order: Dhan Data API -> AngelOne -> yfinance (bhavcopy when the market is closed) |
| `analysis-intelligence-service` | Technical, fundamental, news, event and sentiment analysis |
| `decision-prediction-service` | Decision engine, prediction model, training and outcome evaluation |
| `real-trade-service` | Automatic trading (DEMO and REAL) on Dhan: candidates, gates, entry/exit, swing lane, risk, reconciliation |
| `position-stocks-service` | Intraday scalp book on Dhan Super Orders: scan, capital pool, entry/exit, short selling, SELL probe, board, Time Machine |
| `calibration-runner-service` | Runs calibration / evidence / backtest scripts for the Real Money Calibration page (never changes a trading switch) |
| `notification-scheduler-service` | Telegram / Discord / Slack alerts and scheduled jobs |
| `kronos-forecast-service` | Optional price-path forecast for entry Gate 4f (ships OFF) |
| `timesfm-forecast-service` | Optional forecast-parameter service. Start it with `docker compose --profile timesfm up -d --build timesfm-forecast-service` and check it with `docker compose --profile timesfm exec timesfm-forecast-service python selfcheck.py` |

Shared code that every service copies lives in `shared/` and is guarded by `scripts/check_shared_copies.py` (a fix must land in every copy).
Switches that change trading behaviour live on the **Real Money Calibration / Switches** page (`control/catalog.py` in each trading service).

## Run on the Oracle VM (production)

Production is `docker compose` on one Oracle Cloud Ubuntu VM (`stockky.duckdns.org`) with Oracle Autonomous Database. The same code also runs
on Neon/Postgres: **`ORACLE_DSN` set = Oracle, empty = Postgres**. Branches: `main` and `backup-production` are kept identical.

```bash
cd ~/stockky-v2
cp .env.oracle.example .env      # first time only, then fill the values below
docker compose build && docker compose up -d
docker compose ps
docker compose logs -f <service>              # e.g. position-stocks-service
docker compose up -d --build <service>        # rebuild one service after a code change
bash scripts/healthcheck.sh                    # backend health
bash scripts/backup_state.sh run               # state volumes backup (see the header of the script for restore/schedule)
```

### Oracle database setup (once)

1. Oracle Cloud Console -> Autonomous Database -> **Create** (Transaction Processing, Serverless, Always Free is enough, mTLS ON). Database name `stockkydb`
   gives the alias `stockkydb_high`. Choose an ADMIN password (12-30 chars, upper + lower + digit, no `"`).
2. **Database Connection -> Instance Wallet -> Download Wallet** (choose a wallet password). Copy and unzip it on the VM:
   ```bash
   scp Wallet_stockkydb.zip ubuntu@<VM_IP>:/tmp/
   sudo mkdir -p /opt/stockky/oracle_wallet && sudo unzip -o /tmp/Wallet_stockkydb.zip -d /opt/stockky/oracle_wallet
   sudo chmod -R a+rX /opt/stockky/oracle_wallet
   grep -E "^[a-zA-Z0-9_]+ *=" /opt/stockky/oracle_wallet/tnsnames.ora | cut -d'=' -f1     # lists the aliases
   ```
3. Put these in `.env` (template: `.env.oracle.example`). There is no single DB URL on Oracle: alias + wallet + user/password.

   | Key | Value |
   |---|---|
   | `ORACLE_DSN` | an alias from step 2, e.g. `stockkydb_high` |
   | `ORACLE_USER` / `ORACLE_PASSWORD` | `ADMIN` / the ADMIN password |
   | `ORACLE_WALLET_PASSWORD` | the wallet password |
   | `ORACLE_WALLET_HOST_DIR` | `/opt/stockky/oracle_wallet` (compose mounts it at `/oracle_wallet`) |
   | `ORACLE_WALLET_DIR`, `TNS_ADMIN` | `/oracle_wallet` (leave as is) |
   | `VITE_API_URL` | `https://stockky.duckdns.org/api` |
   | `DATABASE_URL`, `CACHE_DATABASE_URL`, `TRAINING_DATABASE_URL` | empty on the VM |

   Never commit `.env` or the wallet (`.gitignore` blocks them).
4. Verify: `docker compose logs api-gateway | grep -i "Oracle Autonomous"` prints the engine line (`dsn=..., wallet=/oracle_wallet`); `curl -s http://localhost:8000/ops/db-status` reports `db_backend`.

| Log error | Cause / fix |
|---|---|
| `ORA-01017` | wrong `ORACLE_PASSWORD` |
| `ORA-12154` | alias not in `tnsnames.ora`, or the wallet is not mounted |
| `DPY-4011` / PEM error | wrong `ORACLE_WALLET_PASSWORD`, or `ewallet.pem` missing |
| TLS / `ssl_server_dn_match` | VM clock off: `sudo timedatectl set-ntp true`, restart containers |
| permission denied on wallet | `sudo chmod -R a+rX /opt/stockky/oracle_wallet` |

## Run locally

```bash
cp .env.example .env
docker compose up --build
```
Frontend http://localhost:5173, API docs http://localhost:8000/docs. If `VITE_API_URL` was missing at build time, paste the gateway address once in
the app's **Settings** (stored as `stockky:api_url`).

## Tests

**Do not run pytest inside a production container or on the host Python.** The production images do not contain pytest
(`No module named pytest`), and a bare host Python does not have the service requirements (`No module named sqlalchemy` / `httpx`).
Use the helper: it builds a throw-away container on the same Python version as the service's Dockerfile, installs `requirements.txt` + pytest,
mounts the whole repo and runs pytest.

```bash
bash scripts/docker_test.sh position-stocks-service                         # whole suite of one service
bash scripts/docker_test.sh position-stocks-service tests/test_sell_probe.py tests/test_sell_orders.py tests/test_short_entry_order.py tests/test_short_live.py -q
bash scripts/docker_test.sh real-trade-service tests/test_x.py -x           # extra arguments go to pytest
bash scripts/docker_frontend_check.sh                                       # frontend: npm ci + tsc + build + unit tests
python3 -m pytest scripts/test_repo_scripts.py -q                            # repo tooling tests (CI runs this and a few sibling script tests)
```

Repo checks that need no packages (run them before every commit or zip):

```bash
python3 scripts/check_merge_conflicts.py     # no leftover <<<<<<< markers
python3 scripts/gen_env_reference.py --check # docs/ENV_REFERENCE.txt is current (drop --check to regenerate)
python3 scripts/check_shared_copies.py       # duplicated modules agree
python3 scripts/check_stale_files.py         # tracked files that the release no longer contains (prints the `git rm` lines)
```
CI is `.github/workflows/service-tests.yml` (service suites + the repo checks above), `frontend-tests.yml` and `secrets-scan.yml`.
`scripts/freeze_constraints.sh` writes `constraints.txt` (pinned versions); `docker_test.sh` uses it when present.

## Release workflow (zip -> git)

A release arrives as a zip that is copied over the checkout. `robocopy ... /E` adds and overwrites files but **never deletes**, so a file removed in a
release stays in git until you remove it. After every copy:

```powershell
cd D:\stockky-v2
robocopy "D:\stockky-update" "D:\stockky-v2" /E /COPY:DAT /DCOPY:DAT
python scripts/check_stale_files.py          # lists tracked files the release no longer has, with git rm lines
python scripts/check_merge_conflicts.py
git add -A ; git status
```
Run `python scripts/check_stale_files.py --write` only when you are producing a release (it rewrites `docs/FILE_MANIFEST.txt` from disk).

Old release notes, audit reports, service sheets and session notes (about 790 Markdown files) were folded into `CHANGELOG.md` on 2026-10-28 and removed.
They are still in git history: `git log --diff-filter=D --name-only -- "docs/*.md"`, then `git show <commit>^:docs/<file>.md`.

## Useful scripts (`scripts/`)

| Script | Use |
|---|---|
| `healthcheck.sh`, `vm_ops.sh`, `vm_docker_recover.sh`, `wake_all.sh` | VM health and recovery |
| `backup_state.sh` | backup / verify / restore-test of the state volumes |
| `vm_calibrate.sh`, `vm_review_evidence.sh`, `vm_structure_report.sh` | read-only evidence reports on the VM |
| `dhan_sell_probe.py`, `dhan_swing_probe.py` | broker probes (market hours only; see CHANGELOG E242-E244 before running) |
| `daily_trade_report.py`, `charges_split.py`, `calibration_status.py` | trade / charges / calibration reports |
| `check_*.py`, `gen_env_reference.py`, `audit_*.py` | repo guards and audits (what CI runs) |
| `*_backtest.py`, `ml_v2_walkforward.py`, `swing_*.py`, `robustness.py` | offline backtests (stdlib, deterministic) |

## Open items (as of E245, 2026-10-28)

- Dhan SELL probe: re-run in market hours (09:30-14:30 IST) after E243/E244 and expect PASS (TRADED fill, one cover). Confirm the stray 3-share ITC short from the
  09:41 run is closed in Dhan.
- Scan-hits / day-mover check (E232) still needs a market-hours look.
- `HF_MODEL` (news sentiment model) was last noted as not served to the HF token: set a model your token can use in `.env`.
- 2027 NSE holiday dates are published in December: add them to all seven copies (`python3 scripts/check_holiday_lists_sync.py` checks they agree).
- api-gateway `main.py` is still very large; the router split continues one group per release (`gateway_routers/`).
