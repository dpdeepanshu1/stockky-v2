# Group 431 - `scripts/export_records.py`, plus three fixes to `records_report.py` (tooling only, no service code changed)

## Why
On 18 Oct `records_report.py --since 2026-10-01` found ONE trade (opened 6 Oct): the `rt_*.json` files on the VM were an old export, so the October question (do the September problems - 09:xx-10:xx entry losses, gaps through stops, late `eod_squareoff` closes - still happen?) could not be answered. Exporting needed a hand-made curl with an admin token. Three small report problems showed up at the same time.

## 1. `scripts/export_records.py` - a fresh export in one command
```bash
cd ~/stockky-v2
python3 scripts/export_records.py                                  # last 60 days, REAL, to rt_<today>.json (asks for the admin username + password)
TOKEN=<bearer token> python3 scripts/export_records.py             # reuse a session token (e.g. the dashboard's) instead of logging in
python3 scripts/records_report.py rt_*.json --since 2026-10-01
```
Talks to the real-trade container's published port (`--base`, default `http://127.0.0.1:8005`; `REAL_TRADE_URL` also works; `docker compose ps` shows the port). The password is typed hidden and goes only to that address. Logging in calls `POST /auth/login`, the same call the dashboard makes, so it also marks the REAL admin session authenticated until it expires - pass `TOKEN` if you would rather not. It prints how many trades were saved and the NEWEST and OLDEST closed dates (a stale export is obvious), and warns if the server holds more trades than came back. Exit 1 when nothing was saved or login / connection failed (with the reason).

## 2. `records_report.py` fixes
- **Stale export note**: when the newest trade in the files closed more than 5 days ago it says so and points at `export_records.py`.
- **Stop-hit wording**: the old line said a trailed stop "lost -2.69 points MORE than planned" (COHANCE closed at -1.98 against a 4.67 % initial stop - the stop had been ratcheted up, so it did NOT overshoot). It now counts stops that lost MORE than the initial stop (a gap / slow fill) separately from stops that closed inside it (trailed), and averages the overshoot over the first kind only.
- **Charges share of a gross loss** is shown only with at least `--min-n` trades (it printed 1700 % on a single trade).

## Reading the 18 Oct result
One trade (COHANCE, watchlist, BUY NOW, 15:xx IST entry, 1-day hold, stop hit, 0.51 % entry slippage, charges 17 on a gross of -1) tells nothing. Re-export first.

## Verified
`scripts/test_export_records.py` (3 tests against a throwaway local HTTP server: login + export + 60-day clamp, token reuse / failed login / 401, truncation warning, DEMO without login, unreachable host) and 5 new / changed `test_records_report.py` tests. Not yet run against your real container.
