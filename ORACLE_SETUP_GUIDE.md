# Oracle Autonomous DB → `.env` on Ubuntu — Complete Step-by-Step Guide

This guide takes you from **nothing** to a running Stockky on your Oracle Cloud
Ubuntu VM using **Oracle Autonomous Database**, broken into small steps.

It answers, precisely:

- What each **`.env` key name** is and **what value** to put.
- **Where every value comes from** in the Oracle dashboard.
- **How the "URL" works** for Oracle (it is different from Neon — read Part 6).

> Big picture: **the exact same code runs on Render (Neon) and on your Oracle
> VM (Oracle DB).** Nothing in the code changes. The *only* difference is the
> `.env` file. When `ORACLE_DSN` is set, the app uses Oracle. When it is empty
> (Render), the app uses Neon. That is the whole trick.

---

## Part 0 — What you need before starting

- An Oracle Cloud account (the same one your Ubuntu VM is in is fine).
- Your Ubuntu VM reachable over SSH (you already have this — it serves
  `stockky.duckdns.org`).
- The Stockky code already copied to the VM (the `backup-production` branch),
  with `docker-compose.yml`, `.env.oracle.example`, and the `oracle_wallet/`
  folder present (all included in this delivery).

You will create **two passwords** in this guide. Write them down now; you will
paste them into `.env` later:

| Password | Where you set it | Goes into `.env` key |
|---|---|---|
| **ADMIN password** | When you create the database (Part 1) | `ORACLE_PASSWORD` |
| **Wallet password** | When you download the wallet (Part 3) | `ORACLE_WALLET_PASSWORD` |

---

## Part 1 — Create the Autonomous Database

1. Sign in to the Oracle Cloud Console: <https://cloud.oracle.com>.
2. Click the **hamburger menu** (☰, top-left) → **Oracle Database** →
   **Autonomous Database**.
3. Make sure the **Compartment** (left side) is the one you want (the root
   compartment is fine to start).
4. Click the blue **Create Autonomous Database** button.
5. Fill the form:
   - **Display name:** `stockkydb` (anything you like — just a label).
   - **Database name:** `stockkydb`
     👉 **Important:** this exact name becomes the prefix of your connection
     alias. If you name it `stockkydb`, your alias will be `stockkydb_high`
     (that is your future `ORACLE_DSN`). Use letters/numbers only, no spaces.
   - **Workload type:** **Transaction Processing** (best for an app).
   - **Deployment type:** **Serverless**.
   - **Always Free:** turn this **ON** if you see the toggle (free forever,
     20 GB, enough for Stockky). If you do not see it, a paid autonomous DB
     works identically.
   - **Database version:** leave the default (19c or 23ai — both work).
6. **Create administrator credentials:**
   - **Username:** `ADMIN` (fixed — this is your `ORACLE_USER`).
   - **Password:** choose a strong password.
     - Rules: 12–30 characters, at least one UPPERCASE, one lowercase, one
       number; **no** double-quote character `"`; cannot contain the word
       `admin`.
     - 👉 **Write this down.** This is your **`ORACLE_PASSWORD`**.
7. **Network access:** choose **Secure access from everywhere**.
   - Leave **Require mutual TLS (mTLS) authentication** **ON**.
     👉 This is what makes Oracle hand you a *wallet* (Part 3). Keep it ON.
8. **License type:** **License included** (default for Always Free).
9. Click **Create Autonomous Database**.
10. Wait ~1–3 minutes. When the big square icon turns **green / "Available"**,
    the database is ready.

---

## Part 2 — (Nothing to do) Confirm the database is up

On the database's details page you should see **State: AVAILABLE**. Keep this
browser tab open; you need the **Database Connection** button next.

---

## Part 3 — Download the Wallet (this is your "connection key")

The wallet is a small folder of files that lets your app connect securely. It
replaces the long password-in-URL style that Neon uses.

1. On the database details page, click the **Database Connection** button
   (sometimes labeled **DB Connection**).
2. A panel opens. Under **Wallet type**, select **Instance Wallet**
   (this is for this one database — the recommended choice).
3. Click **Download Wallet**.
4. It asks for a **wallet password**:
   - Type a strong password (any characters; 8+ chars).
   - 👉 **Write this down.** This is your **`ORACLE_WALLET_PASSWORD`**.
5. Click **Download**. You get a file named like **`Wallet_stockkydb.zip`**.

Inside that zip are these files (you do not edit them):

```
cwallet.sso   ewallet.pem   ewallet.p12   tnsnames.ora
sqlnet.ora    ojdbc.properties   keystore.jks   truststore.jks   README
```

The two that matter to us: **`tnsnames.ora`** (holds your connection alias) and
**`ewallet.pem`** (the private key our driver reads).

---

## Part 4 — Copy the wallet onto the Ubuntu VM

Do this from the computer where the zip downloaded. Replace `<VM_IP>` with your
VM's public IP and `ubuntu` with your VM's SSH user if different.

1. **Send the zip to the VM:**
   ```bash
   scp Wallet_stockkydb.zip ubuntu@<VM_IP>:/tmp/
   ```
2. **SSH into the VM:**
   ```bash
   ssh ubuntu@<VM_IP>
   ```
3. **Unzip it into the wallet folder** (this exact path matches `.env`):
   ```bash
   sudo mkdir -p /opt/stockky/oracle_wallet
   sudo unzip -o /tmp/Wallet_stockkydb.zip -d /opt/stockky/oracle_wallet
   ```
4. **Let Docker read the files:**
   ```bash
   sudo chmod -R a+rX /opt/stockky/oracle_wallet
   ```
5. **Verify + find your alias name:**
   ```bash
   grep -E "^[a-zA-Z0-9_]+ *=" /opt/stockky/oracle_wallet/tnsnames.ora | cut -d'=' -f1
   ```
   You will see lines like:
   ```
   stockkydb_high
   stockkydb_low
   stockkydb_medium
   stockkydb_tp
   stockkydb_tpurgent
   ```
   👉 **Pick one** — `stockkydb_high` is the simplest choice. That exact string
   is your **`ORACLE_DSN`**. (`_tp` or `_low` also work and use fewer resources;
   any of them is fine for Stockky.)

> **Where should the wallet live?** The example uses
> `/opt/stockky/oracle_wallet`. You can use any absolute path — just make sure
> `ORACLE_WALLET_HOST_DIR` in `.env` matches it exactly.

---

## Part 5 — The `.env` file: exact Key names and Values

On the VM, go to the folder that has `docker-compose.yml` and create `.env`
from the template:

```bash
cd /path/to/stockky            # the folder with docker-compose.yml
cp .env.oracle.example .env
nano .env                      # edit the values
```

Fill in **exactly these keys**. This is the table you asked for — Key Field
Name on the left, what to put on the right:

| # | Key Field Name (left of `=`) | Value to put (right of `=`) | Where it came from |
|---|---|---|---|
| 1 | `ORACLE_DSN` | your alias, e.g. `stockkydb_high` | Part 4, step 5 (from `tnsnames.ora`) |
| 2 | `ORACLE_USER` | `ADMIN` | Fixed — set in Part 1 |
| 3 | `ORACLE_PASSWORD` | the **ADMIN** password | You chose it in Part 1, step 6 |
| 4 | `ORACLE_WALLET_PASSWORD` | the **wallet** password | You chose it in Part 3, step 4 |
| 5 | `ORACLE_WALLET_HOST_DIR` | `/opt/stockky/oracle_wallet` | Part 4 (where you unzipped) |
| 6 | `ORACLE_WALLET_DIR` | `/oracle_wallet` | **Leave as-is** (path inside container) |
| 7 | `TNS_ADMIN` | `/oracle_wallet` | **Leave as-is** (path inside container) |
| 8 | `VITE_API_URL` | `https://stockky.duckdns.org/api` | Your public site |
| 9 | `DATABASE_URL` | *(leave empty)* | Empty = do not use Neon here |
| 10 | `CACHE_DATABASE_URL` | *(leave empty)* | Empty = do not use Neon here |

A correctly filled Oracle `.env` looks like this (only the important lines
shown — keep the rest of the template as it is):

```dotenv
# ---- Oracle Autonomous DB ----
ORACLE_DSN=stockkydb_high
ORACLE_USER=ADMIN
ORACLE_PASSWORD=MyAdminPass123
ORACLE_WALLET_PASSWORD=MyWalletPass456
ORACLE_WALLET_HOST_DIR=/opt/stockky/oracle_wallet
ORACLE_WALLET_DIR=/oracle_wallet
TNS_ADMIN=/oracle_wallet

# ---- Frontend ----
VITE_API_URL=https://stockky.duckdns.org/api

# ---- Neon: leave empty on the Oracle VM ----
DATABASE_URL=
CACHE_DATABASE_URL=
TRAINING_DATABASE_URL=
```

Save and close (`nano`: `Ctrl+O`, `Enter`, `Ctrl+X`).

> 🔒 **Never commit `.env` or the wallet files to git.** `.gitignore` already
> blocks `.env`, `.env.oracle`, and everything under `oracle_wallet/` except the
> placeholder. The ADMIN password and wallet password live **only** in `.env`.

---

## Part 6 — "How do I put the correct URL, and where do I get it?"

This is the part that confuses everyone coming from Neon, so read it carefully.

**Neon** gives you one long URL that has *everything* in it:

```
postgresql://user:password@ep-xxx.aws.neon.tech/neondb?sslmode=require
             └─ user ─┘ └password┘ └────── host ──────┘ └─db─┘
```

**Oracle Autonomous does NOT work that way.** There is no single URL to paste.
Instead the connection is split into three separate things:

1. **A short alias** (the DSN) — `stockkydb_high` → goes in `ORACLE_DSN`.
2. **The wallet folder** — proves who you are (replaces `sslmode`) → goes in
   `ORACLE_WALLET_DIR` / `TNS_ADMIN` (+ `ORACLE_WALLET_PASSWORD`).
3. **User + password** → `ORACLE_USER` + `ORACLE_PASSWORD`.

The app then builds the SQLAlchemy URL for you internally as simply
`oracle+oracledb://` and passes the wallet, DSN, and credentials through
connect args. **You never type a long Oracle URL.** Filling in the 7 keys in
Part 5 IS "putting in the correct URL."

**But where is the "connection string" in the dashboard, then?**
If you click **Database Connection**, you will also see a **Connection Strings**
section with long text like:

```
(description=(retry_count=20)(retry_delay=3)
 (address=(protocol=tcps)(port=1522)(host=adb.<region>.oraclecloud.com))
 (connect_data=(service_name=abcd1234_stockkydb_high.adb.oraclecloud.com))
 (security=(ssl_server_dn_match=yes)))
```

That long block is exactly what the short alias `stockkydb_high` *points to*
inside `tnsnames.ora`. Because the wallet already contains it, **you use the
short alias, not this long string.** (Advanced users can paste the long string
directly into `ORACLE_DSN` instead of the alias — it also works — but the alias
is cleaner and is what this guide uses.)

---

## Part 7 — Build and run

Still on the VM, in the folder with `docker-compose.yml`:

```bash
docker compose build
docker compose up -d
```

Docker Compose reads `.env`, mounts your wallet from
`ORACLE_WALLET_HOST_DIR` into each container at `/oracle_wallet`, and starts all
services. The first `build` takes a few minutes (it installs the Oracle driver
`oracledb`, which is a small pure-Python package — no Oracle client to install).

---

## Part 8 — Verify Oracle is actually being used

1. **Check the logs for the Oracle engine line:**
   ```bash
   docker compose logs api-gateway | grep -i "Oracle Autonomous"
   ```
   You should see something like:
   ```
   Oracle Autonomous DB engine (dsn=stockkydb_high, wallet=/oracle_wallet)
   ```
2. **Ask the app which backend it is on** (the KV status reports it):
   ```bash
   curl -s http://localhost:8000/ops/kv-status
   ```
   Look for `"durable_backend": "oracle"` in the response (endpoint name may
   vary slightly in your build — any status/health route that lists the durable
   backend will show `oracle`).
3. **Open the site:** <https://stockky.duckdns.org> — it should load and behave
   exactly like the Render/Neon copy. Run a scan; add a watchlist item; the data
   now persists in Oracle.

If all three look right, you are done. 🎉

---

## Part 9 — Troubleshooting (common errors → fix)

| Symptom in logs | Cause | Fix |
|---|---|---|
| `ORA-01017: invalid username/password` | Wrong `ORACLE_PASSWORD` | Re-check the ADMIN password from Part 1; retype in `.env`, `docker compose up -d` again |
| `ORA-12154` / `could not resolve the connect identifier` | `ORACLE_DSN` alias not found in `tnsnames.ora`, or wallet not mounted | Confirm the alias with the `grep` in Part 4 step 5; confirm `ORACLE_WALLET_HOST_DIR` points at the unzipped folder |
| `DPY-4011` / wallet / decrypt / PEM error | Wrong `ORACLE_WALLET_PASSWORD`, or `ewallet.pem` missing from the wallet | Retype the wallet password; if `ewallet.pem` is absent, re-download the wallet (newer downloads always include it) |
| `config_dir` / file-not-found on startup | Wallet path wrong or not unzipped | `ls /opt/stockky/oracle_wallet` should list `tnsnames.ora` and `ewallet.pem` |
| TLS / certificate / `ssl_server_dn_match` error | VM clock is off | `sudo timedatectl set-ntp true` then restart containers |
| Permission denied reading wallet | Container user cannot read files | `sudo chmod -R a+rX /opt/stockky/oracle_wallet` |

To read a specific service's logs: `docker compose logs -f decision-prediction-service`
(replace with any service name). To restart after editing `.env`:
`docker compose up -d` (Compose recreates only what changed).

---

## Part 10 — What runs where (so nothing surprises you)

- **Durable core runs fully on Oracle:** the data-feed cache, paper trades,
  training samples, model predictions, watchlist, notification settings, and the
  generic key-value store. All of it reads/writes Oracle when `ORACLE_DSN` is set.
- **The "surprise premarket scanner" and the Neon keep-alive are Postgres-only
  and safely switch off on Oracle.** They are secondary features that use
  Postgres-specific SQL; on the Oracle VM they cleanly do nothing (no error, no
  crash). Your main scans, decisions, trades, and training are unaffected.
- **Scheduled jobs (GitHub Actions) stay pointed at Render**, as you asked.
  Your 24/7 VM does not spin down, so it does not need the "keep-warm" workflows
  at all — they only wake the free-tier Render dynos.

That is the entire process. Keep this file next to `DEPLOY_GUIDE.md` for future
reference.
