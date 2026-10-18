# Group 427 - order-flow record on every entry, backtest progress lines, Docker recovery

Nothing here changes a trading decision. Defaults: the record is ON, everything else is tooling.

## 1. Order-flow record (real-trade-service + position-stocks-service)
Before this, spread and best-5 book value were checked at entry but the heavier SIDE of the book was read nowhere, and no depth rule had ever been backtested (daily candles cannot test it).
Now every entry decision stores what the book looked like, so after about two weeks your own trades can say whether bid-heavy / ask-heavy / wide-book entries did better or worse.

| Where | Stored in | When |
| --- | --- | --- |
| real-trade `entry_engine/order_flow.py` | `trade_decisions.flow_json` (new nullable column, additive migration) | every ENTRY decision that has a price (WAITs included, so vetoed vs passed can be compared); approved entries also get the 20-level book |
| position-stocks `orders/order_flow.py` | `scalp_positions.flow_json` (new nullable column) | at entry, after the depth gate (read from the same cached /quote row, plus one short /depth call) |

Recorded: `spread_pct`, `book_value_5`, `bid_qty_5`, `ask_qty_5`, `imbalance_5` = (bid - ask) / (bid + ask) over the best five levels (+1 all bids, -1 all asks), `vs_vwap_pct`, `vs_open_pct`, `day_pos` (0 = day low, 1 = day high), `day_change_pct`, `volume`, and `d20` = `imbalance_20`, `book_value_20`, `buy_impact_pct` (cost of buying the planned size), `buy_complete`.
Plumbing: real-trade's `Tick` gained `bid_qty_5`, `ask_qty_5`, `open_price` (read from market-data's `/quote` and `/quotes/bulk` rows, which already carried them).

Switches (both services): `ORDER_FLOW_RECORD=0` turns the record off; `ORDER_FLOW_DEPTH20=0` skips the one extra `/depth/{symbol}` call (needs `DHAN_DEPTH20_ENABLED=1`, which you set); `ORDER_FLOW_DEPTH20_TIMEOUT_S` (default 1.5). A slow or missing depth answer is simply left out; the entry goes ahead unchanged.

Reading it (real-trade): `GET /records` expectancy now has `by_imbalance_5`, `by_imbalance_20` (`ask_heavy` < -0.2, `balanced`, `bid_heavy` > 0.2, `unknown`) and `by_spread` (`tight` < 0.15 %, `normal` < 0.30 %, `wide`), and each trade record has `imbalance_5`, `imbalance_20`, `flow_spread_pct`, `flow_book_value_5`, `flow_vs_vwap_pct`, `flow_day_pos`. position-stocks: the positions list carries `order_flow`.
Do not change a rule on fewer than about 100 trades per bucket; a bucket with a handful of trades is noise.

What this is NOT: there is no trade-by-trade feed, so cumulative volume delta, aggressor volume and absorption are not recorded. Imbalance is a snapshot of resting orders and can be spoofed - it is a hypothesis until the buckets show a difference.

## 2. Backtest scripts looked hung
The three backtests print nothing while they run, so a 400-stock run looks stuck (the 18 Oct run was Ctrl-C'd three times). Now `entry_rules_backtest.py` and `legacy_gate_backtest.py` print a progress line with elapsed time and time left (new `scripts/_progress.py`; results and reports are identical, same order), and `structure_backtest.py` prints one every 5 symbols. `structure_backtest.py` is the slow one (full structure analysis on every 3rd bar of every stock, one process): run it last and in the background:
```bash
cd ~/stockky-v2
nohup python3 scripts/structure_backtest.py hist400 > hist400/structure_out.json 2> hist400/structure.log &
tail -f hist400/structure.log
```
## 3. `^NSEI.json` was missing in hist400
The entry-rules report needs it for the exchange calendar (pre-holiday, expiry roll-back); without it those are guessed. Add just the benchmark, the 400 stock files stay untouched:
```bash
MARKET_DATA_URL=http://127.0.0.1:8001 python3 scripts/fetch_history_for_backtest.py hist400 --benchmark-only
```

## 4. Docker build failure (`parent snapshot ... does not exist: not found`)
Not a code problem: Docker's image store lost a layer one image still points at (interrupted build, prune during a build, or a full disk). `docker compose down` had already stopped the whole stack. `bash scripts/vm_docker_recover.sh` rebuilds the broken service, clears that image + the build cache if needed, restarts docker if needed, then runs `docker compose up -d --build`. It never removes volumes.

## Verified
real-trade 4934 passed (the same 5 fail on the uploaded group426 zip: 4 `test_oracle_compat` need the `oracledb` driver the sandbox lacks, and `test_group425_kronos_entry_wiring::test_gate_5_6_enforce_waits_on_cost_model`, the file your VM log shows as locally modified); position-stocks 3843 passed; scripts tests passed. New tests: real-trade 14, position-stocks 9, scripts 3.
