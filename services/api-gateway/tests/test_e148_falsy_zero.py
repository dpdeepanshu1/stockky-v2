"""E148: a real value of 0 must not be replaced by a default (the same bug class the 2026-09 audit found for market_score = 0).
Source-level guards: they need no web framework, so they run anywhere. Each pattern below was the buggy form."""
from pathlib import Path

GATEWAY = Path(__file__).resolve().parents[1]


def _text(name: str) -> str:
    return (GATEWAY / name).read_text(encoding="utf-8")


def test_surprise_scanner_52w_distance_zero_is_kept():
    src = _text("surprise_scanner.py")
    assert 'float(static.get("dist_52w_pct") or 100.0)' not in src
    assert '_dist_raw = static.get("dist_52w_pct")' in src
    assert "dist = float(_dist_raw) if _dist_raw is not None else 100.0" in src


def test_instant_scanner_sentiment_zero_is_kept():
    src = _text("instant_scanner.py")
    assert 'float(feed_item.get("sentiment_score") or 50.0)' not in src
    assert "sentiment = float(_sent_raw) if _sent_raw is not None else 50.0" in src


def test_default_logic_behaves_as_intended():
    # the exact expressions used in the fix, run on the values that used to break
    def dist(v):
        return float(v) if v is not None else 100.0
    assert dist(0.0) == 0.0 and dist(None) == 100.0 and dist(7.5) == 7.5
