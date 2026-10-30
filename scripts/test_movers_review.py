"""E263: the static rules of scripts/movers_review.py."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import movers_review as m  # noqa: E402


def test_static_blockers():
    assert m.static_blockers(3042, 6.3)[0].startswith("PRICE")
    assert m.static_blockers(203.2, 8.78)[0].startswith("DAYGAIN long")
    assert m.static_blockers(126.79, -9.3)[0].startswith("DAYGAIN short")
    assert m.static_blockers(259.55, 5.66) == []
    assert m.static_blockers(None, None) == []
