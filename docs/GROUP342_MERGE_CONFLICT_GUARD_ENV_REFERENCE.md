# Group 342 - merge-conflict guard (from the 2026-10-13 VM startup log)

## What the log showed
- `position-stocks-service` restarted in a loop all session: `/app/orders/reconcile.py", line 1032: <<<<<<< HEAD -> IndentationError`.
  A half-resolved merge was on the VM. The group 341b zip already has `orders/reconcile.py` clean (compiles; no marker anywhere in
  the repo), so redeploying that tree fixes it: copy the zip's `services/position-stocks-service/orders/reconcile.py` over the VM
  copy (or unzip the whole tree) and `docker compose up -d --build position-stocks-service`.
- Everything else in the log is expected or outside the code (see "Not changed").

## Changes (tooling only, no service logic)
| # | Change | File |
|---|--------|------|
| 1 | `scripts/check_merge_conflicts.py`: fails on `<<<<<<<` / `>>>>>>>` lines (indented ones too) and on `=======` after an open start marker; a lone `=======` (Markdown / docstring underline) is ignored; skips venvs and `archive/` | scripts |
| 2 | Every service Dockerfile runs `python -m compileall` (tests / venvs excluded) after `COPY . .`, so a syntax error fails `docker compose build` instead of crash-looping the container | 7 Dockerfiles |
| 3 | CI "Repo checks" runs the conflict check first | `.github/workflows/service-tests.yml` |
| 4 | 5 tests in `scripts/test_repo_scripts.py` (indented markers, underline false positives, CLI + venv skip, whole repo clean, Dockerfile guard) | scripts |
| 5 | `docs/ENV_REFERENCE.md` regenerated (it was stale since group 341: 657 -> 659 variables; `test_checked_in_generated_files_are_current` and the CI step were failing) | docs |

Run before zipping / pulling to the VM: `python3 scripts/check_merge_conflicts.py`.

## Not changed (no code fix possible or wanted)
- AngelOne `loginByPassword` 403 in market-data: broker-side / per-process budget (group 211's global cooldown already handles it).
- NSE `AKA_A2 status 403` and "0 securities rows": bhavcopy universe fallback (2962 symbols) is working as designed.
- Moneycontrol frozen feed / cnbctv18 404: fallbacks served (group 341 throttles the warning).
- `$HEG.NS possibly delisted`: yfinance noise, one WARNING per symbol per 6 h since group 341.
- CCAVENUE / HDFIRE / SPELS unresolved at AngelOne: no AngelOne row; Dhan prices them.
- `BROKER_STOP_ENABLED is off` warning: intentional until you confirm broker-side stops on a small position.
- api-gateway "event loop blocked 4.0s" in an Oracle rollback on a dead SSL socket: one occurrence at boot; repeat it and the next round
  should move the hotpicks schema check off the loop.
