"""E189: the single-stock fast path asks the decision engine as "not owned", so it must not take a decide-cache entry computed in owned mode
(SELL / HOLD) as its answer. Source-level guard (main.py needs the whole gateway to import)."""
from pathlib import Path


def test_ultra_path_skips_an_owned_mode_decide_cache_entry():
    src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    i = src.index("cached_decide = _redis_get(cache_key)")
    block = src[i:i + 2600]   # E191: a comment and the feed check now sit between the read and the guard
    assert 'cached_decide.get("decision")' in block
    assert 'not cached_decide.get("already_owned")' in block
    assert 'normalized["from_decide_cache"] = True' in block
