"""E229: /surprise/audit must not run audit_surprise_feed() (KV network read) on the event loop."""
from pathlib import Path
SRC = (Path(__file__).resolve().parent.parent / "gateway_support" / "bound_routes_c.py").read_text(encoding="utf-8")


def test_surprise_audit_runs_in_a_worker_thread():
    assert "await ref.asyncio.to_thread(audit_surprise_feed)" in SRC
    assert '_audit_cache_put("surprise_audit", audit_surprise_feed())' not in SRC
