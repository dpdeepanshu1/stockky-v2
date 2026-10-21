# Group 472 (2026-10-20): minimum shares per REAL entry

Why: the 20 Oct review: 1-2 share trades (SUZLON, BLUESTONE) cannot take a partial profit or trail, and pay the same fixed costs for less edge.

* `ENTRY_MIN_QTY` (default 3, 0 = off). REAL mode only. After risk sizing (`result.approved_qty or proposed_qty`), below the minimum = WAIT, gate_tag `min_qty`.
* Placed before Gate 5.6 (cost model). Automatic path only.
* Effect: stocks priced above roughly (position cap / 3) are skipped. With the Rs3,000 cap that is about Rs1,000 per share.
* TIGHTENS entries; fewer trades. Rollback: `ENTRY_MIN_QTY=0`.
* Tests: `tests/test_group472_min_qty.py` (2, static wiring checks). The evaluate_mode behaviour was not run here (no sqlalchemy); CI is the first full run.
