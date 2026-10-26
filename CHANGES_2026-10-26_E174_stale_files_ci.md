# E174 - the two CI failures (files deleted by a release stay in git)

Includes E173.

## What failed
1. `npm test`: `AssertionError: TabButton.tsx deleted` (appHelpers.test.mjs).
2. `python3 scripts/gen_env_reference.py --check`: `docs/ENV_REFERENCE.md is out of date`.

## Cause (same for both)
Both pass on the zip itself (also under Python 3.11, the CI version). In your repo they fail because a release is unpacked over
the git checkout: that adds and overwrites files but never deletes. Files removed by an earlier release (E70 removed
`TabButton.tsx` and `BackendStatusDot.tsx`) are still tracked, so the test sees them. The same leftover code is read by the env
scanner, so it finds variables the committed table does not list. I reproduced both failures by adding such files to a copy of
the E170 tree and overlaying this release; removing them with `git rm` makes both checks pass.

## Fix (run once in your repo, after unpacking this zip, before committing)
    python3 scripts/check_stale_files.py                 # lists every tracked file this release no longer has, as git rm lines
    python3 scripts/check_stale_files.py | grep '^git rm' | sh
    python3 scripts/gen_env_reference.py --check         # should now say "up to date"
    cd frontend && npm test

The list may also show files you added yourself since the last release; read it before running the `git rm` lines.

## Added
- `scripts/check_stale_files.py` + `docs/FILE_MANIFEST.txt` (every file in this release; rewritten by `--write` at each release).
  It only prints commands; it changes nothing. Not part of CI (a later commit of yours would make it fail).
- `gen_env_reference.py --check` now prints which rows differ (first 30), so a CI failure names the variable and the file.
- 3 tests in `scripts/test_repo_scripts.py` (16 pass).
- The frontend test stays strict on purpose: it protects against the dead components being used again.
