# Group 182 — Reconnect / Connect box on the "can't reach the backend" screens (frontend)

Carried-over item: "the Reconnect input on the unreachable-site screen". I did not have the original description, so this is
what reading `SystemCheck.tsx` and `api.ts` turned up. Tell me if you saw something different.

## Three defects found and fixed
1. **A URL typed without `https://` was stored as a relative path.** `my-vm.duckdns.org` made `apiUrl()` build
   `my-vm.duckdns.org/health`, so requests went to the page's own origin and the check failed again with no hint why.
   New `normalizeGatewayUrl()` (`src/api.ts`, used by `setApiUrl`, so Settings in `App.tsx` benefits too): trims,
   removes trailing slashes, adds `http://` for localhost / loopback / private-range / `.local` hosts and `https://` for
   everything else; an empty input still clears the override. The box shows the URL that was actually applied.
2. **Clicking Reconnect looked like nothing happened.** The screen was already in the `checking-gateway` phase, so setting
   the same phase restarted nothing: the elapsed timer kept running and the "stuck" panel stayed up. A `checkKey`
   now restarts the timer, so the screen returns to "Connecting to backend..." until it is stuck again.
3. **An old hung check could overwrite the new attempt.** The ping to the previous URL was still pending (that is why you
   were stuck) and, when it finally failed, set the screen back to "Can't reach the backend" over the new attempt. Each
   Connect/Reconnect now starts a numbered check chain; results of older chains, including their 5 s retry timers, are ignored.

## Checked
- `tsc --noEmit` clean on the whole frontend (npm ci in a scratch copy).
- `frontend/scripts/check_gateway_url.ts`: 18 cases for the URL normaliser, all pass
  (`cd frontend && npx tsx scripts/check_gateway_url.ts`).
- **Not browser-tested.** The screen flow (items 2 and 3) is covered by type-check and by reading, not by a running UI;
  the frontend has no test runner. Please try: stop the gateway, open the app, wait for the stuck panel, type a bare
  hostname and press Reconnect.

## Not changed
`wake`/`recheck` handlers (they call `runCheck` without a chain id, so they use the current chain, as before).

Rebuild the frontend image.
