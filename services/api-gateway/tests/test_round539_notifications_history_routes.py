"""E556 (round 539, B1): the Alerts-feed routes are admin-only and proxy to the notification service's /history (source-level check, no app import)."""
from pathlib import Path

import admin_guard

SRC = (Path(__file__).resolve().parent.parent / "gateway_support" / "bound_routes_g.py").read_text(encoding="utf-8")
MAIN = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")


def test_both_routes_are_in_the_protected_list():
    assert "/notifications/history" in admin_guard.PROTECTED
    assert "/notifications/read-all" in admin_guard.PROTECTED


def test_both_routes_carry_the_admin_dependency_and_the_right_method():
    assert '@notifications_history_router.api_route("/notifications/history", dependencies=[ref.Depends(ref._require_admin)], methods=["GET"])' in SRC
    assert '@notifications_read_all_router.post("/notifications/read-all", dependencies=[ref.Depends(ref._require_admin)])' in SRC


def test_they_proxy_to_the_history_endpoints_and_clamp_the_limit():
    assert '{ref.NOTIFICATION_URL}/history"' in SRC and '{ref.NOTIFICATION_URL}/history/read-all"' in SRC
    assert "max(1, min(int(limit), 200))" in SRC


def test_routers_are_included_on_the_app():
    assert "app.include_router(_bound_routes_g.notifications_history_router)" in MAIN
    assert "app.include_router(_bound_routes_g.notifications_read_all_router)" in MAIN
