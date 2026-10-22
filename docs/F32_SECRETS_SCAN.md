# F32 - secrets scan in CI

- `scripts/check_secrets.py`: stdlib-only, offline. Finds JWTs, private-key blocks, AWS keys, Telegram bot tokens, GitHub / Slack tokens, passwords inside
  connection URLs, high-entropy values assigned to NAMEs containing TOKEN / SECRET / PASSWORD / API_KEY / ACCESS_KEY / PRIVATE_KEY (not in code files, where
  `NAME = x` is almost always an identifier), and file names that must never be committed (`.env`, wallets, keys, `run_output*`, `*.token`, `*.jwt`).
  Output is redacted: file:line, rule, first 4 characters, length, fingerprint.
- `scripts/secrets_allowlist.txt`: fingerprints of known false positives (one today: a fake JWT in a redaction test).
- `.github/workflows/secrets-scan.yml`: every push and pull request (no path filter) plus weekly.
- Run it before zipping or pulling to the VM: `python3 scripts/check_secrets.py`.
- The full pytest of every service already runs in `.github/workflows/service-tests.yml` (the `test` matrix); nothing was missing there.

Does not rotate anything: `SESSION_SECRET` and the Upstash token still need rotating by you, and any old zip / runner volume that held the JWT must be deleted.
It also cannot see git history; if a secret was ever committed, rotating it is the only fix.
