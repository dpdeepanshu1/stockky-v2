"""orders/statuses.py - one place for the trade status names that other modules must agree on (group 340).

A new exit status used to need edits in reconcile's pending list, its retention list, the frontend and the model comment, and one missed
list left that status stuck on the entry-price placeholder (STAGNATION_EXIT, session69). Add the name to FLAT_SELL_STATUSES here and the
pending-fill resolution and the retention list pick it up. The frontend type/colour in positionStocksApi.ts / PositionStocksTab.tsx still
mirror this tuple by hand; tests/test_group340.py checks they match.
"""
FLAT_SELL_STATUSES = ("EOD_SQUAREOFF", "MANUAL_EXIT", "STAGNATION_EXIT", "STOP_BREACH_EXIT", "CLOSE_ALL", "TARGET_EXIT")   # exits sent as a plain MARKET order (SELL for a long, BUY for a short), fill read later. TARGET_EXIT (E114): a plain-order short reached its target, covered by hand
BRACKET_STATUSES = ("TARGET_HIT", "STOP_HIT")                                                    # exits by a Super Order leg
TERMINAL_STATUSES = BRACKET_STATUSES + FLAT_SELL_STATUSES + ("ERROR",)
