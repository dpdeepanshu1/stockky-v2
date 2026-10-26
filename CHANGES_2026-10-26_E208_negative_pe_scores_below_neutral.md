# E208 - decision: a loss-making company (negative PE) no longer scores neutral

**Where:** decision-prediction-service/decision/main.py, `_derive_fundamental_from_payload` (payload has a PE but no stored fundamental_score). Open item from E193.
**Bug:** negative PE fell through to 50, better than a profitable company at PE 28-50 (48).
**Fix:** PE < 0 -> 40 (below 48, above the PE 50+ bucket 35). A stored score still wins; other buckets unchanged. A missing PE still defaults to 22 (62) and a PE of exactly 0 is still treated as missing (falsy), as before - not changed here.
**Tests:** tests/test_e208_negative_pe_scores_below_neutral.py (7 cases); tests/test_e193_pe_fallback_monotone.py's (-3) case now expects 40. NOT RUN (pytest/fastapi absent); py_compile only.
