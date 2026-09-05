# Stockky — Session 2026-09-03 (part 2): real Dhan token expiry was never being read

## Root cause, confirmed from your live log

Your regenerate-token click actually worked (200 OK, both in the backend
log and the browser Network tab you sent). The log line that mattered:

    Dhan TOTP refresh response keys: ['dhanClientId', 'dhanClientName',
    'dhanClientUcc', 'givenPowerOfAttorney', 'accessToken', 'expiryTime']

Dhan **does** send back a real `expiryTime` in that response. The code
just never looked at it — `save_credentials()` always set
`token_expires_at = issued_at + 24h`, a fixed assumption, no matter what
Dhan actually said. That's the entire cause of the mismatch you saw
against Dhan's own portal (4h37m real vs 23h54m shown in the dashboard):
two completely disconnected numbers, one real, one guessed.

## Fix — `services/real-trade-service/auth/dhan_credentials.py`

- `save_credentials()` now takes an optional `real_expires_at`. When
  given, it's stored as-is (still clamped through the existing
  `_effective_expiry()` 24h ceiling as a safety net — so a bad value from
  Dhan can only ever make the countdown shorter/more honest, never longer).
- New `_parse_dhan_expiry()` handles Dhan's `expiryTime` in whatever shape
  it comes back as — epoch seconds/ms, ISO-8601, or `"YYYY-MM-DD HH:MM:SS"`
  (assumed IST, converted to UTC). Logs the raw value once so we can
  confirm the exact format from your next real click, and falls back to
  the old 24h guess (never crashes, never silently stores garbage) if it
  can't parse it.
- `refresh_if_totp_enabled()` now parses `expiryTime` from the response
  and passes it through. Manual token paste (the "Rotate token" form) has
  no such field from Dhan, so it's untouched — still the 24h guess, which
  is the best information available for that path.

## Frontend — `RealAutoTrade.tsx`

Nothing computational changed here — the countdown already reads
`token_expires_at` from the API, so fixing the backend field fixes the
displayed number automatically. Only updated the caption text under it
(was flatly claiming "Dhan hard-caps every token at 24h," which your own
screenshot showed isn't true) to describe what it actually does now.

## What to check after deploying this

1. Click "Regenerate token" again.
2. Check the log for the new line:
   `Dhan TOTP token refreshed successfully. Real expiry from Dhan: <value>`
   — if it says "unparsed — used 24h lifetime guess" instead of a real
   timestamp, send me that log line plus the raw `expiryTime raw value`
   line just above it, and I'll adjust the parser to match Dhan's actual
   format on the first try.
3. Compare the dashboard's countdown against Dhan's own portal (Money →
   API access page) right after — they should now be close (within a
   minute or two of clock skew), not hours apart.

## Verified

- `python3 -m py_compile auth/dhan_credentials.py main.py` — clean.
- `npm run build` — exit code 0, zero TypeScript errors (ran for real,
  not just reviewed).
