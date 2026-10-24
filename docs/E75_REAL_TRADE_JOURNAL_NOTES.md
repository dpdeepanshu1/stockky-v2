# E75 - reason + feeling notes for Real Trade (2026-10-23)

Position Stocks had them since E30; Real Trade did not. Now both do. Descriptive only: no gate, score, size or exit reads a note.

## Backend (real-trade-service)
- `journal_notes.py`: pure helpers (clean reason <= 300 chars, one of 9 feelings, `classify`, `summarize`, `upsert`, `refresh_snapshots`). `classify` counts only CLOSED positions (win / loss / breakeven by stored `realized_pnl`). A position is "manual" when it has no `source_tab` and is not a broker import.
- `models.TradeJournalNote` -> table `trade_journal_notes` (NEW table, `create_all` makes it, no ALTER). Keeps its own snapshot of the trade, so notes outlive the retention cleanup.
- Routes (admin login in REAL, open in DEMO, same as the other mode routes): `GET /journal/notes/{mode}` (notes, win rate by feeling, manual positions without a note) and `PUT /journal/notes/{mode}/{position_id}` (both fields blank removes the note).
- Switch `JOURNAL_NOTES_ENABLED` (ships `true`), on the Real Money Calibration page under Journal. OFF refuses saving with 409; saved notes stay.

## Frontend
- `realTradeApi.ts`: `journalNotes`, `saveJournalNote`, `closedPositions`.
- `components/realAuto/JournalNotesCard.tsx`, shown at the bottom of Real Auto Trade -> Activity: win rate by feeling, the 15 newest trades with add / edit note, manual trades without a note first. Row logic is the shared `components/board/journalNotesView.ts`.

## Tests run here
- `tests/test_e75_journal_notes.py` (8): validation, classify, manual detection, summary, upsert / update / remove on a fake session, route and admin-guard wiring from the source, model / switch / catalog agreement. All pass in a stand-in runner.
- `src/realAutoTabs.test.mjs` (source checks for the card and API calls) and `journalNotesView.test.mjs` pass under node.
- `check_control_catalog.py` ok, `gen_env_reference.py` regenerated (`docs/ENV_REFERENCE.md`) and `--check` ok, `py_compile` of the edited files ok.

## Not run here
pytest, sqlalchemy and fastapi are not installable, so the two routes were not called and the table was not created; `tsc` / `vite build` were not run for the new card (no node_modules). On the VM: rebuild real-trade-service + frontend, then Real Auto Trade -> Activity -> add a note to a closed trade, reload, it must still be there; `bash scripts/docker_test.sh`; `npm ci && npx tsc && npm test`.
