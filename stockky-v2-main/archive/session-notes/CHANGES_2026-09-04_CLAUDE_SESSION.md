# Stockky — Session 2026-09-04 (Claude, this conversation): remaining
Groww-style items closed out

## Remaining raw modals converted to BottomSheet
Found by an exhaustive `grep "fixed inset-0"` across every component (not
a curated list) — 4 modals still used the old raw centered-dialog pattern:
- `Trades.tsx` — 3 modals: backup detail viewer, "buy more" (add to
  position), and "add dummy funds" (deposit). All now slide up from the
  bottom on mobile with the drag handle / swipe-to-dismiss, matching
  `BuySniperModal`/`DecisionCard` from earlier rounds.
- `ServiceManager.tsx` — the system-health/microservices-topology panel.
  This one has its own bespoke `.topo-*` CSS design (not built from
  Tailwind tokens) — kept that visual language intact rather than forcing
  it into the generic card style, just replaced the outer overlay/
  positioning with `BottomSheet` so it gets proper mobile behavior too.
  Removed the now-redundant `.topo-shell` border/background/padding
  wrapper since `BottomSheet`'s own outer shell already supplies that —
  double-checked this didn't leave the content unstyled (it initially
  would have; caught and fixed before finalizing).

`BottomSheet.tsx` is now the only file in the entire `components/` tree
using the raw `fixed inset-0` modal pattern — correct, since it's the
primitive that owns that pattern for everything else.

## One more real color-token gap found and fixed
A genuinely exhaustive final sweep (every color family, bare AND numbered
shades, across every file — not the same list as last time) found
`slate-300`/`slate-400`/etc. — a raw *numbered* Tailwind shade, distinct
from this app's own `slate` token (no number). Because the token is also
named "slate," earlier sweeps that excluded the bare word "slate" (to
avoid flagging the app's own legitimate token) accidentally also excluded
these numbered raw shades sitting right next to it. Found in `DataFeed.tsx`,
`DecisionCard.tsx`, `HotStocks.tsx`, `IpoTracker.tsx`, `Trades.tsx` — all
fixed, same mechanical remap pattern as every prior round.

Re-verified after this round with the full exhaustive check (all color
families, numbered and bare, every `.tsx` file, no exclusions): zero raw
color references remain anywhere in `frontend/src/components/`.

## Verified
- Both touched files (`Trades.tsx`, `ServiceManager.tsx`): brace/paren
  balanced before and after.
- `grep "fixed inset-0"` across the whole tree: only `BottomSheet.tsx`.
- Full color-family sweep across the whole tree: zero matches.

## "Fully work on" the calibration gap — historical backtest tool added

The honest gap was: `board`/`results`/`bulk_block` catalyst types have
almost no live "entered" positions yet, so `calibrate_decay_profiles.py`
can't say anything useful about their bands/hold-times without waiting
weeks for enough live fills. That waiting can't be skipped for real —
but it CAN be worked around: every `WatchlistEntry` row already has a
symbol + catalyst timestamp regardless of whether it ever became a live
trade, so real historical price data (not fabricated) can answer the same
questions much faster.

**`services/real-trade-service/scripts/historical_backtest_calibration.py`**
(new) — for every watchlist row with a captured `catalyst_price` (any
status: active/missed/expired, not just entered), pulls the actual
historical daily candles that followed via market-data-service's existing
`/history` endpoint (same symbol-normalization/rename handling the live
system already uses — no separate, possibly-inconsistent data path), and
simulates: would the current `entry_band_pct` have allowed an entry, and
if so, what the current exit profile (trail schedule / `max_hold_days`)
would have actually produced — held days, P&L at exit, peak P&L, and
whether the position was cut off by the time-stop before its peak.

Explicitly NOT presented as equivalent to live data — printed reminders
throughout that this skips slippage/liquidity/fill mechanics, and to
trust live `calibrate_decay_profiles.py` numbers more once enough
`entered` rows accumulate there. This tool exists specifically to cover
the gap before that, using real market history instead of guessing.

Usage: `python scripts/historical_backtest_calibration.py --mode REAL --lookback-days 60`

Verified: syntax-checked clean.
