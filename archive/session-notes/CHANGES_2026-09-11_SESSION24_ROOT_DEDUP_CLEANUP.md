# Session 24 — repo root dedup cleanup (2026-09-11)

Previous session ran the archive `git mv` equivalents but the zip that
shipped afterward still had **both** copies: the archived one under
`archive/` and the untouched original still sitting at repo root. `git mv`
on a real checkout would not have this problem (it's one atomic
operation); it only showed up here because the zip was rebuilt by
copying into `archive/` without deleting the root originals.

Fixed by diffing every file that exists in both locations, confirming
byte-identical, then deleting the root copy:

- 13 `CHANGES_*.md` files removed from root (already in `archive/session-notes/`)
- `stockky_test_all.sh`, `stockky_recon.sh`, `test_angelone.sh`,
  `backtest_surprise_test.py`, `stockky_diagnostic_round14.sh` removed from
  root (already in `archive/root-scripts/`)
- `frontend_snippets/` directory removed (its one file,
  `repair_button_handler.ts`, was already archived; the empty dir was
  never rmdir'd)

No source/behavior changes in this note — see `SESSION23` and the
Session 23 in-conversation summary (Reset Failures timeout fix, Oracle
overnight orchestrator, api-gateway probe investigation) for the actual
feature work, which was already correctly in place.
