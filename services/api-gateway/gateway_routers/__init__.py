"""gateway_routers - route groups moved OUT of the 14.7 k-line main.py, one small group per release (E48 plan: docs/E48_GATEWAY_ROUTER_PILOT.md).

Rules for a group to move (so thousands of existing tests keep working):
  1. its handlers use NO helper defined in main.py (tests patch helpers as `main.<name>`; a moved handler would not see that patch);
  2. anything they use from another module is imported INSIDE the handler (tests patch `data_feed.<name>`, which works from any module);
  3. main.py keeps `from gateway_routers.<group> import <handler names>` so `gw.<handler>()` calls and `main.<handler>` lookups still resolve;
  4. the router is included at the SAME place in main.py the routes used to be, so registration order is unchanged.
`scripts/` + `tests/test_gateway_routers_split.py` check 1-4.
"""
