# E167 - Alerts: Save checks what was typed and reports the real result (2026-10-26)

Includes E166. Eighth card-body screen (plan order: ..., Watchlist, Alerts, then Settings). Status: `docs/AUDIT_STATUS_E167.md`.
The Alerts screen is a set of channel forms (Telegram, Discord, Slack, CallMeBot) under the E157 summary strip, not a list of cards, so the work is on the forms.
Roll back: in `frontend/src/components/NotificationsPanel.tsx` remove the `checkChannelInput` block at the top of `saveChannel` (the `const chk ... if (!chk.ok) {...}` lines), put `setTestResult(`${channel} saved successfully.`);` back in place of `saveOutcome(...)`, and restore the import to `{ alertsKpis }`; delete `checkChannelInput`, `saveOutcome` and their types from `components/alertsView.ts`; remove the "E167" block from `alertsView.test.mjs`. No CSS change.

## Bugs fixed
- **Save sent whatever was typed, or nothing.** Telegram, Discord and Slack Save with empty or wrong fields still switched the channel on and said "saved successfully". Now:
  - Telegram: the bot token must look like one (digits, colon, letters and digits) and the chat id must be a number (may start with -) or @channelname. A first-time save with either empty is refused with what to enter.
  - Discord / Slack: the webhook must be a full https:// address on discord.com / discordapp.com (/api/webhooks/...) or hooks.slack.com (/services/...); look-alike hosts such as discord.com.evil.example, http:// and addresses with a user:password part are refused.
  - An empty field on a channel that is already connected just re-enables it, as before.
  - The refusal messages never repeat a token or URL back.
- **"saved successfully" was said even when the backend still reported the channel as not connected.** The message now follows the backend's own answer: "Telegram saved and connected." or "... saved, but the backend still reports it as not connected. Check the values and that the notification service is up."

## Unchanged
Summary strip (E157), the CallMeBot form and its own messages, Test / Disconnect / on-off switches, how secrets are stored (nothing new is stored or shown), layout and styling.

## Files
`frontend/src/components/alertsView.ts` (+ `checkChannelInput`, `saveOutcome`, pure), `frontend/src/components/NotificationsPanel.tsx` (`saveChannel` only), `frontend/src/alertsView.test.mjs` (extended, still 44 suites).

## Tests
Run here: `npm test` passes (44 suites, exit 0). Five deliberate breakages in the helpers and one in the component caught (Slack host test removed, user:password test removed, Discord host pattern loosened, Telegram first-time token check removed, outcome always "connected", Save bypassing the check; the component guard was too weak at first and is now tightened). esbuild bundles the screen; strict `tsc` on `alertsView.ts` alone passes. NOT run: `tsc --noEmit` on the whole app, `vite build`, a browser, backend pytest. Not checked: whether the backend also validates these fields on save. The Telegram token pattern is the commonly documented shape; if a real token of yours is refused, tell me and I will loosen it.
