"""group368 (item 7): the classic /analyze field set is frozen; the structure block is purely additive."""
from __future__ import annotations

import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _analyze_harness as H  # noqa: E402

GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden", "technical_baseline.json")
logging.disable(logging.CRITICAL)
_PLAIN = [c for c in H.cases() if c["kind"] is not None]
_THIN = [c for c in H.cases() if c["kind"] is None]


def _golden():
    return json.load(open(GOLDEN, encoding="utf-8"))


def test_legacy_fields_match_golden_for_every_case():
    g = _golden()
    assert set(g) == {c["name"] for c in H.cases()}
    for c in H.cases():
        assert H.legacy_fields(H.run(c)) == g[c["name"]], c["name"]


def test_structure_on_vs_off_gives_identical_legacy_fields():
    for c in H.cases():
        on, off = H.run(c, structure=True), H.run(c, structure=False)
        assert H.legacy_fields(on) == H.legacy_fields(off), c["name"]


def test_only_structure_keys_are_added():
    allowed = set(H.NEW_KEYS)
    for c in _PLAIN:
        on, off = H.run(c, structure=True), H.run(c, structure=False)
        assert set(on) - set(off) <= allowed, c["name"]
        assert set(off) - set(on) == set(), c["name"]


def test_lite_never_sends_heavy_block_and_keeps_legacy_fields():
    g = _golden()
    for c in _PLAIN:
        if c["params"]:
            continue
        r = H.run(c, lite=True)
        assert "market_structure" not in r, c["name"]
        assert H.legacy_fields(r) == g[c["name"]], c["name"]


def test_override_calls_keep_their_params_and_stay_off_the_shared_cache():
    m = H.load_main()
    stored = []
    prev = m._cache_set
    m._cache_set = lambda k, v, ttl=None: stored.append(k)
    try:
        for c in [c for c in _PLAIN if c["params"]]:
            r = H.run(c, main=m)
            used = r.get("adaptive_params_used") or {}
            for k, v in c["params"].items():
                assert used.get(k) == v, (c["name"], k)
        assert not [k for k in stored if k.startswith("tech_analysis:")]
    finally:
        m._cache_set = prev


def test_structure_computed_when_on_absent_when_off():
    c = _PLAIN[0]
    assert "market_structure" in H.run(c, structure=True)
    assert not (set(H.run(c, structure=False)) & set(H.NEW_KEYS))


def test_thin_and_missing_history_paths_have_no_structure_keys():
    g = _golden()
    for c in _THIN:
        r = H.run(c)
        assert not (set(r) & set(H.NEW_KEYS)), c["name"]
        assert r == g[c["name"]], c["name"]


def test_harness_is_hermetic_about_delivery():
    """group378: analyze() must not reach market-data for delivery % in the offline harness, whatever MARKET_DATA_URL is."""
    import _analyze_harness as h
    m = h.load_main()
    assert type(m.httpx).__name__ == "_OfflineHttpx"
    res = h.run(h.cases()[0], structure=False)
    assert res.get("delivery_pct") is None and res.get("delivery_source") is None
