# Group 416 - REMAINING_TASKS.md rewritten

Docs only. No service code, config or tests changed.

`REMAINING_TASKS.md` still described the market-structure engine "after group 349" (policy lost, gate and frontend not built).
Those exist since groups 370-374, so the list pointed work at things already done. It now lists only what is open after group 415:
evidence gates G1-G4, unbuilt items, VM / live checks, known data issues and what has never been verified.

Not touched on purpose: the in-memory SL-M-rejected flag (persisting it would change broker-order behaviour for a once-per-restart
retry) and the `HF_MODEL` default (no way to know from here which model your token can use).
