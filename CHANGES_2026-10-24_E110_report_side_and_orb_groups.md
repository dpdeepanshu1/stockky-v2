# E110 - /trades/report: LONG/SHORT split and the E104 ORB bar fields (24 Oct 2026)

Position Stocks `orders/trade_report.py` (report only, nothing is decided from it):
- `by_side` (LONG / SHORT; rows with no side are LONG). The short-selling plan asked for this split in the reports; with live shorts coming (T7) their results must not blend into the long numbers.
- `by_orb_close_trigger`: `close_trigger` / `no_close_trigger` / `no_orb_record` (a trade with no ORB record or no bars is never counted as "no trigger").
- `by_orb_volume_ratio`: trigger-bar volume against the opening-range average, buckets `<1.0`, `1.0-1.5`, `1.5-2.5`, `>=2.5`, `none`.
This closes the E104 note "records_report does not group by the new fields yet" for Position Stocks, where those fields are recorded. The API only gains keys; the frontend does not read this report's groups.
Proof: `tests/test_e110_report_side_and_orb_groups.py` (4); position-stocks suite green.
