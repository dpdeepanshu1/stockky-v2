# Group 401 - sector-relative returns in the structure observations

The all-decision observation log (group 374) could say whether a pick beat the Nifty (`nifty_ret_N`), but not whether it beat its own sector, so a
whole-sector rally looked like stock-picking skill. Now each observation also carries its sector and the sector index's return over the same sessions.

- **Columns** on `decision_structure_observations`: `sector` (label the decision service saw), `sector_ret_1/5/10/20`. A table created before this group
  gets the columns added once, on first use (`models._add_missing_structure_columns`, Postgres/SQLite `ADD COLUMN`, Oracle `ADD (...)`). If an ALTER fails it is
  logged and the table stays usable only once the column exists, so check the training log for "could not add" after the first deploy.
- **Input:** `decision/structure_obs_hook.py` sends `response["sector"]` (text only). A later decision of the same day without a sector never erases a stored one.
- **Index map:** `training/sector_index.py`, whole-word match on the lowercased label (IT / Technology -> `^CNXIT`, Banks -> `^NSEBANK`, Financial Services -> `^CNXFIN`,
  Pharma / Healthcare -> `^CNXPHARMA`, FMCG, Auto, Metal, Energy / Utilities, Realty, Infra, Media). Broad labels with no clean NSE index ("Consumer Cyclical",
  "Basic Materials", "Industrials") are left unmapped on purpose. Add or override with `STRUCTURE_OBS_SECTOR_INDEX_JSON`.
- **Evaluation:** `/api/evaluate/structure` fetches each sector index once per call (a failed fetch is skipped for that sector only) and uses the same
  open-to-close basis as `nifty_ret_N`. No usable sector bars = the fields stay empty (None), never 0.
- **Report:** every group in `/api/structure/report` adds `avg_rel_to_sector_20` and `n_with_sector_20` (only rows that have both returns are counted).

Not verified offline: that Yahoo serves each index ticker above (a ticker it does not serve just yields no bars), and the real-database migration
(no sqlalchemy in the sandbox). After the first evaluator run check `n_with_sector_20` in the report: a low count means the labels or tickers need the override.
