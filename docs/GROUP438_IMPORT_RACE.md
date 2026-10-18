# Group 438: the bot's own CNC entries were recorded as broker imports

Found 18 Oct from the VM: all 15 newest `broker_imported` positions had a FILLED AUTO BUY order with a Dhan order id created about one
second before the position's `opened_at`. They were the bot's own entries.

Cause: `reconcile_real_orders` calls `import_broker_holdings` before it books pending order fills. A CNC BUY that had just filled showed up in
Dhan's positions while its order was still PLACED, so there was no TradePosition and the importer created one (`broker_imported=True`, ATR stop
instead of the decision's stop, no decision label / tier / stop basis). `record_real_fill` then found that position and ADDED the fill on top of it.

Fix (`portfolio/portfolio.py`, `config.py`): the importer skips a symbol while this service's own BUY for it is PLACED or PARTIAL and younger than
`IMPORT_SKIP_PENDING_BUY_MIN` (default 45 minutes, 0 = off). The fill is then booked by `record_real_fill` as before.

Records (`trade_records.py`): a `broker_imported` row whose entry is a filled AUTO BUY with a Dhan id booked within 120 s of `opened_at` now has
`origin = "system"` and `import_race = true`, and is included by default. Orders are matched to positions before the imported rows are filtered.

Not changed: rows already in the database keep `broker_imported=True` (exit.py uses it to sell as CNC; flipping it could bring back the
"insufficient funds" INTRADAY SELL rejections). Old exports are re-labelled by the new records code only.

Check on the VM whether past races doubled a quantity: see the query in the chat note for this group.
