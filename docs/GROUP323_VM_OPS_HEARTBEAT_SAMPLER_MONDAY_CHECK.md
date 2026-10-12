# Group 323 (2026-10-11) - VM ops: heartbeat that actually pings, one sampler, Monday live check, backup + security audit

Cumulative on group 322. **No service code changed** - only `scripts/`, `.env.example`, this note and the changelog.

## Why
The pre-Monday checklist was run by hand on the VM and two things in the pasted output were wrong or unverifiable:
- The heartbeat cron was installed with the literal placeholder `https://hc-ping.com/YOUR-UUID`. `curl -fsS` fails on it, `>/dev/null 2>&1`
  hides the failure, so the cron looked installed but no ping could ever reach healthchecks.io (and an alert you never tested is no alert).
- The sampler was started with a one-off `nohup` line; nothing stops a second copy, and nothing reads the result back.
- The Monday review was "cat a log and look for FAIL / suspect / CRITICAL" with no tool that produces the Depth20 / order-WS verdicts group 322 added.

## `scripts/vm_ops.sh` (run on the VM from `~/stockky-v2`)
| command | does |
|---|---|
| `checklist` | one screen: heartbeat state, sampler state, `.env` backup, scheduled checks, then the security audit; lists what only you can do |
| `heartbeat [URL]` | installs the 5-minute cron. URL from the argument, `HC_PING_URL` in the shell or in `.env`. **Refuses** placeholders (`YOUR-UUID`, `<uuid>`, `xxxx`), non-https, and an `hc-ping.com` path that is not a 36-character UUID. Removes the broken placeholder line and any older heartbeat line, keeps every other cron job, sends one test ping and tells you if it was rejected. The cron line touches `~/.stockky_hb_ok` after each accepted ping so `status` can tell "installed" from "working" |
| `heartbeat-remove` | removes only the heartbeat lines |
| `sampler start\|stop\|status\|report` | `start` does nothing when one is already running and complains about two; uses `~/stats_loop.sh` if it exists (yours is untouched), else `scripts/stats_loop.sh`. `status` also flags a log that stopped growing. `report` prints peak memory / CPU per container and the host's lowest available memory (works on logs written by the bundled `stats_loop.sh`) |
| `backup-env` | `chmod 600 .env`, then an AES-256 encrypted copy in `~/backups` (gpg, else openssl; you type the passphrase, it is never stored) and the `scp` line to bring it to your PC. Backups on the VM alone die with the VM |
| `security` | read-only: `sshd -t` (config valid after the OpenSSH upgrade), effective password / root login settings, service active, reboot-required, any **public** listener other than 22 / 80 / 443, compose ports all on 127.0.0.1, ufw / fail2ban / unattended-upgrades, `.env` mode |
| `schedule` | cron (Mon-Fri): sampler start 08:40 IST, `monday_check.py` at 09:30 and 10:15 IST, converted to the server's clock; refuses if the server clock would put an IST time on another date (set the VM to UTC); idempotent, tagged `# stockky-ops` |
| `monday [0930\|1015]` | runs the live check now |

Not installed by anything automatically: run `bash scripts/vm_ops.sh heartbeat https://hc-ping.com/<your-uuid>` yourself with the real URL.

## `scripts/monday_check.py` (stdlib only, GET requests to the loopback ports, no token read or printed)
`--label 0930|1015`, `--no-docker`, `--out-dir`. Prints and saves `~/check_logs/<date>_<label>ist_live.log` (the `_live` suffix keeps it apart from any
09:30 log your own job already writes). Each problem is one `FAIL` / `WARN` line; exit code 1 when any `FAIL`.
- every service `/health`;
- market-data `/market/session`: at **10:15** anything but `open` is a FAIL (weekend is INFO); at 09:30 `pending` is only INFO;
- `/quote/RELIANCE` `age_s` over 20 s while the clock says the market is open = FAIL;
- `/internal/dhan-status` depth20 `layout_verdict` (`ok` / `unverified` / `suspect`, with `suspect_packets`, `crossed_books`) and `/depth/RELIANCE`;
- position-stocks `/orders/ws-status` `layout_verdict` (off = INFO);
- host disk free (<15 % WARN) and available memory (<400 MiB WARN); `docker compose logs --since 20m` lines with FAIL / suspect / CRITICAL (last 40 shown).

## `scripts/stats_loop.sh` (fallback sampler)
Read-only (`free`, `/proc/loadavg`, `docker stats --no-stream`); line format `<time>|HOST|load1=..|mem_used_mib=..|mem_avail_mib=..|swap_used_mib=..` and `<time>|<container>|cpu=..|mem_mib=..`. `--report [log]` prints the peaks; `--once` takes one sample; interval `STATS_INTERVAL_S` (30).

## What the pasted terminal output showed
- `pgrep -af stats_loop.sh` printed exactly one `/bin/sh /home/ubuntu/stats_loop.sh`: correct. The `[1]+ Done` line is the `nohup setsid` launcher exiting after it forked - normal.
- The cron line is the placeholder: replace it with `heartbeat <real-url>` (above).

## Tests
`scripts/test_vm_ops.py`, 25 tests (stub crontab and curl, no network): placeholder / URL validation, placeholder line replaced and other jobs kept, idempotent, refused placeholder changes nothing, URL from `.env`, rejected test ping, status flags the placeholder, remove touches only heartbeat lines, IST to server-clock conversion (UTC; refuses across midnight), schedule adds three tagged weekday jobs once, sampler report peaks / missing file / unit conversion, and every `monday_check.py` verdict (session at 09:30 vs 10:15, quote age, depth20, order WS, health, log scan, docker runner failures, summary text, exit code, log file name).
Run in the sandbox under a plain runner: 25 passed. Not run: real pytest, and none of it was run on your VM (`ss`, `sshd`, `docker compose`, `gpg` paths are only exercised there).

## NOT done (needs you)
- Create the healthchecks.io check and run `heartbeat <url>`; add the email / Telegram integration on that check.
- Second-window SSH test, Oracle security list (22 / 80 / 443 only, 22 to your IP), database automatic backups, copying the encrypted `.env` off the VM.
- Password-hash rotation stays for before real money; broker-side stop stays off until one small trial (group 322).
- Live check of Depth20 / order-WS frames: the 09:30 report prints the verdicts.
