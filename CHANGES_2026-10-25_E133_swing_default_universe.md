# E133 - swing lane: a fresh deploy now records shadow evidence (2026-10-25)

**Problem (found reading E132):** the evening scan needs a stock list. `SWING_UNIVERSE_SYMBOLS` and `SWING_RANK_DEFAULT_SYMBOLS` both ship empty, so on a fresh deploy the scan stopped at `no_universe` and `/swing/status` showed no scan. The E132 "VM steps" never mention setting the list, so following them literally recorded nothing.

**Fix:** `swing/engine.py` falls back to the stocks the service already knows by name (`market_context.sector_signal.NSE_SECTOR_MAP`, 118 liquid sector-index names) when neither variable is set. Order: `SWING_UNIVERSE_SYMBOLS` > `SWING_RANK_DEFAULT_SYMBOLS` > built-in list. `SWING_UNIVERSE_MAX` still caps it; symbols held by a swing position are still always added.
- `engine.universe_source()` returns `env | rank_default | builtin | none`.
- `GET /swing/status` now shows `universe_source`; the scan thread's last result carries `universe: {source, size}`.
- Shadow only: nothing is ordered. Live entries still need `SWING_MODE=live`.

**Read this before trusting the verdict:** 118 names is smaller than the 300+ the plan wants (S1 gate) and is not the folder the S2 replay tested. Shadow evidence from the built-in list is a start, not proof. For like-for-like evidence run the runner job `swing_universe` (or `python3 scripts/swing_universe.py HIST_DIR`), paste the `SWING_UNIVERSE_SYMBOLS=` line into `.env`, and restart real-trade-service. Rows already recorded stay in the ledger; the report does not separate them by universe, so restart the evidence clock (or read `/swing/ledger` by date) when you switch.

**Checked here:** `tests/test_swing_lane.py` 26 tests pass (2 new: fallback order, cap, held symbols; source reported). Mutation: removing the fallback is caught. Momentum tests pass. Guards pass (control catalog, shared copies, table-name collisions, secrets, merge markers, requirements drift). `py_compile` on `engine.py` and `main.py`.
**Not run here (unchanged from E132):** pytest, SQLAlchemy / FastAPI paths (`SqlRepo`, `swing/live.py`, `swing/probe.py`, the `/swing/*` routes, the `auto_pilot` and exit-engine edits), anything against Dhan. pip cannot reach PyPI in this sandbox. Static check done instead: every `repo.*` call in `live.py`, `engine.py`, `probe.py`, `main.py`, `auto_pilot.py` uses only keywords the repo interface and ledger columns accept.

**Still open for the swing lane (none of it code the sandbox can finish):** run the full real-trade suite on the VM; S0 answers (account equity, swing size, DDPI/eDIS, whether CNC stop legs persist overnight); S4 `dhan_swing_probe` before any REAL; weeks of shadow data before `live`.

**VM steps:** deploy; leave `SWING_MODE=shadow`; after 16:00 IST `GET /swing/status` must show a scan and `universe_source: builtin` (or `env` once you set the list).
