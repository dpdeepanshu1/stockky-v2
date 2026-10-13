# Group 341 - log review 2026-10-13 (09:00-09:21 IST, one trade: TECHM)

Built on group 340. Switches are env vars; every default below is the new behaviour, the "off" value restores the old one.

## Money
| # | Change | File | Switch (default) |
|---|--------|------|------------------|
| 1 | Pool baseline adds back only the MARGIN the pool's open positions block (TECHM: free cash -Rs596 for a Rs2,979 position; the full add-back lifted the baseline Rs9,446 -> Rs12,126) | position-stocks `capital/ledger.py` | `SCALP_POOL_COMMITTED_MARGIN_FRAC` (0.20; 1.0 = old) |
| 2 | One slot's notional capped at pool / MAX_CONCURRENT x mult (a 0.8% stop was sizing one slot at 50% of the pool) | `capital/ledger.py` | `SCALP_SLOT_NOTIONAL_CAP_MULT` (1.5; 0 = off) |
| 3 | VIX and calendar size cuts round to the nearest share, not down | `orders/entry.py` | - |
| 4 | Fill-price re-arm: each leg saved the moment Dhan accepts it, stop read back, failed leg retried on later reconcile passes (max 6, then ERROR + Telegram) | `orders/reconcile.py` `_rearm_legs` | `STOP_VERIFY_ENABLED` (group 340) |
| 5 | Quality gate reuses a complete result for 90 s; a timed-out event call keeps the last earnings date / negative news seen in 24 h | `screening/quality_gate.py` | `QUALITY_GATE_RESULT_TTL_S` (90), `QUALITY_GATE_EVENT_MEMORY_H` (24) |
| 6 | Tick buffer per symbol capped at 4,000 (was 50,000 x ~2,600 symbols); container limit 400m -> 700m | `feed/ws_client.py`, `docker-compose.yml` | `WS_MAX_TICKS_PER_SYMBOL` (4000) |
| 7 | real-trade logs a SAFETY warning at boot while `BROKER_STOP_ENABLED` is off | real-trade `main.py` | - |

## Feeds and noise
- 10: yfinance "possibly delisted" ERROR bursts reduced to one WARNING per symbol per 6 h (market-data `main.py`).
- 11: dead news-feed warnings (cnbctv18 404, Moneycontrol / NDTV Profit frozen) once per hour per feed (analysis `news/feed_fetch.py`).
- 12: Hugging Face "model not supported" 400 backs off 6 h instead of 30 min (still needs your `HF_MODEL` change).
- 13: surprise scan logs a "finished" line (rows, symbols, seconds) so a scan that never completes is visible (api-gateway `surprise_scanner.py`).
- 14: AngelOne movers wait up to `ANGELONE_MOVERS_SCRIP_WAIT_S` (8 s) for the first scrip-master load; `get_all_symbols(wait_s=)`.
- 15: "entry fill ... taken from order book" logs only when it changes the stored price.
- 17: the held-symbol skip in `screening/engine.py` ignores a `.NS` / `.BO` suffix.

## Not changed (no code fix possible or wanted)
8 (AngelOne 403 / per-process budget, 25 s poll), 9 (NSE blocking), 16, 18, 19, 20 (retention keeps 3 days of closed scalp rows - export them if you need tax/audit history), 21 (group 340 features still untested live).
