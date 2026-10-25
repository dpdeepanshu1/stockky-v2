"""E149: audit_route_auth.py finds the guarded gateway routes, every unguarded mutating route is triaged, and the three routes closed in E149 stay closed. Plain script or pytest."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audit_route_auth as ar  # noqa: E402

CLOSED_IN_E149 = {"/ops/qstash/publish", "/api/feed/purge-over-cap", "/data-feed/purge-over-cap"}
CLOSED_IN_E150 = {"/ipo/notify-top-picks", "/surprise/ipo/notify-top-picks", "/surprise/notify-top-picks", "/stockky-hot/notify-top-picks", "/ipo/purge-non-equity", "/surprise/ipo/purge-non-equity", "/scan/universe/cache"}
GUARDED_BEFORE = {"/notifications/config", "/ops/power-off", "/ops/circuit-reset", "/data-feed/hard-reset", "/training/train", "/training/{path:path}"}


def _by_path():
    out = {}
    for r in ar.mutating_routes():
        out[r["path"]] = out.get(r["path"], True) and r["guarded"]
    return out


def test_earlier_guards_are_seen():
    routes = _by_path()
    for p in GUARDED_BEFORE:
        assert routes.get(p) is True, p


def test_routes_closed_in_e149_are_guarded():
    routes = _by_path()
    for p in CLOSED_IN_E149:
        assert routes.get(p) is True, f"{p} must need the admin login"


def test_routes_closed_in_e150_are_guarded():
    routes = _by_path()
    for p in CLOSED_IN_E150:
        assert routes.get(p) is True, f"{p} must need the admin login"


def test_router_level_dependency_counts_as_guarded():
    routes = _by_path()
    assert routes.get("/scheduler-control/{path:path}") is True  # APIRouter(dependencies=[Depends(require_admin)])


def test_every_unguarded_route_is_triaged_and_none_is_stale():
    assert ar.main(["--check"]) == 0


def test_triage_has_a_class_and_a_risk_for_each_entry():
    tri = json.loads(ar.TRIAGE_FILE.read_text())
    assert tri
    for p, v in tri.items():
        assert v["class"] in {"ui-open", "machine", "machine-signed", "uncalled"}, p
        assert v["risk"], p
    assert not ((CLOSED_IN_E149 | CLOSED_IN_E150) & set(tri))


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
