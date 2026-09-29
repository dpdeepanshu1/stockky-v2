# Session 130 — analysis-intelligence-service: news/main.py to 100% + utcnow removal (2026-09-29)

- news/main.py 99% -> 100% (349 stmts, 0 missed). Total 4150 stmts, 17 missed.
- `datetime.utcnow()` -> `_utcnow()` (naive UTC, same value) at both call sites (cutoffs in the
  Yahoo/RSS fetch paths). No `utcnow()` remains in news/.
- Lines 11-12 (`from news_quality import ...` fallback -> build_news_response = None): tested by
  running the module via runpy with sys.modules["news_quality"] = None.
- Lines 657-659 (`__main__` uvicorn block, default port 8005): tested with a fake uvicorn, same
  pattern as tests/test_event_main.py::TestMainBlock.
- tests/test_news_main.py: 120 pass under `-W error::DeprecationWarning`.
- Remaining misses (17): fundamental/main.py 8-9, 659-661; peer_multi_quarter.py 173-175;
  peers.py 74-75; sentiment/main.py 37-38; technical/main.py 692-693, 765-767.
