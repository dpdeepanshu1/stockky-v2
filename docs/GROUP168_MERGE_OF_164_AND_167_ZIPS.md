# Group 168 - merge of the group 164 and group 167 zips

Both zips branched from group 161, so group numbers 162-164 are used twice with different content.

| Line | Groups | Service | Doc file |
|---|---|---|---|
| IndianAPI | 162 | analysis-intelligence | GROUP162_INDIANAPI_429_COOLDOWN.md |
| Entry pre-check | 163 | position-stocks | GROUP163_ENTRY_PRECHECK_BEFORE_QUALITY_GATE.md |
| Watchlist | 164 | real-trade-service | GROUP164_WATCHLIST_ONE_ROW_PER_SYMBOL_INDEX_FILTER.md |
| Scalp review | 162-167 | position-stocks | GROUP162_SCALP_REVIEW..., GROUP163_DAY_STATS..., GROUP164_REPAIR..., GROUP165_..., GROUP166_..., GROUP167_... |

Hand-merged files (position-stocks): `main.py`, `config.py`, `tests/test_main.py`.
Everything else comes whole from one zip; no file was edited on both sides.

Run on the VM:

    python3 -m pytest services/position-stocks-service/tests services/real-trade-service/tests services/analysis-intelligence-service/tests -q
