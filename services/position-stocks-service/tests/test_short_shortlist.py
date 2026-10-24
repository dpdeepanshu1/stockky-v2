"""E107 (P2 item 17): the short-side shortlist ranks bearish candidates (screening/shortlist.build_short), is live in the short cycle, and is a switch.
Stdlib only (no SQLAlchemy). It only orders and trims; it never adds a candidate."""
import pathlib
import re
import sys
from types import SimpleNamespace as NS

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import config  # noqa: E402
from screening import shortlist  # noqa: E402

CAT = (ROOT / "control" / "catalog.py").read_text(encoding="utf-8")
AUTO = (ROOT.parent / "calibration-runner-service" / "autocalib.py").read_text(encoding="utf-8")
ENTRY = (ROOT / "orders" / "short_entry.py").read_text(encoding="utf-8")


def _c(sym, score):
    return NS(symbol=sym, composite_score=score, pct_change=-1.0)


def _row(conv, zone=None, ext=False):
    return {"side": "bear", "conviction": conv, "zone_score": zone, "extended": ext, "rank": 1}


def _on(monkeypatch, **kw):
    monkeypatch.setattr(config, "SHORT_SHORTLIST_ENABLED", kw.get("enabled", True), raising=False)
    monkeypatch.setattr(config, "SHORT_SHORTLIST_MAX", kw.get("cap", 5), raising=False)


def test_stronger_board_conviction_beats_scan_order(monkeypatch):
    _on(monkeypatch)
    passers = [(_c("AAA", 10.0), None), (_c("BBB", 10.0), None)]
    out = shortlist.build_short(passers, {"AAA": _row(55), "BBB": _row(90)})
    assert [r["symbol"] for r in out] == ["BBB", "AAA"] and [r["pick"] for r in out] == [1, 2]


def test_weak_chart_is_a_better_short(monkeypatch):
    _on(monkeypatch)
    q_weak, q_strong = NS(technical_score=20.0), NS(technical_score=85.0)
    out = shortlist.build_short([(_c("STRONG", 10.0), q_strong), (_c("WEAK", 10.0), q_weak)], {"STRONG": _row(70), "WEAK": _row(70)})
    assert out[0]["symbol"] == "WEAK"


def test_extended_short_is_demoted(monkeypatch):
    _on(monkeypatch)
    out = shortlist.build_short([(_c("LATE", 10.0), None), (_c("FRESH", 10.0), None)], {"LATE": _row(80, ext=True), "FRESH": _row(70)})
    assert out[0]["symbol"] == "FRESH" and out[1]["extended"] is True


def test_equal_scores_keep_scan_order_and_cap_trims(monkeypatch):
    _on(monkeypatch, cap=2)
    passers = [(_c(s, 10.0), None) for s in ("A", "B", "C")]
    out = shortlist.build_short(passers, {s: _row(60) for s in "ABC"})
    assert [r["symbol"] for r in out] == ["A", "B"]


def test_never_adds_a_candidate_and_missing_data_is_scored_on_what_exists(monkeypatch):
    _on(monkeypatch)
    out = shortlist.build_short([(_c("ONLY", 5.0), None)], {})
    assert len(out) == 1 and out[0]["symbol"] == "ONLY" and 0.0 <= out[0]["score"] <= 100.0
    assert shortlist.build_short([], {}) == []


def test_off_switch_restores_scan_order(monkeypatch):
    _on(monkeypatch, enabled=False)
    out = shortlist.build_short([(_c("AAA", 10.0), None), (_c("BBB", 10.0), None)], {"AAA": _row(10), "BBB": _row(99)})
    assert [r["symbol"] for r in out] == ["AAA", "BBB"] and out[0]["score"] is None


def test_error_falls_back_to_scan_order(monkeypatch):
    _on(monkeypatch)
    monkeypatch.setattr(shortlist, "score_short", lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    out = shortlist.build_short([(_c("AAA", 1.0), None), (_c("BBB", 9.0), None)], {})
    assert [r["symbol"] for r in out] == ["AAA", "BBB"]


def test_public_rows_are_json_safe(monkeypatch):
    import json
    _on(monkeypatch)
    json.dumps(shortlist.public(shortlist.build_short([(_c("AAA", 3.0), NS(technical_score=40.0))], {"AAA": _row(60, zone=50)})))


def test_cycle_uses_the_short_shortlist_live():
    assert "shortlist.build_short" in ENTRY or "_shortlist.build_short" in ENTRY
    assert "for c, q in ((r[\"cand\"], r[\"quality\"]) for r in ranked)" in ENTRY      # attempts walk the ranked order, not scan order
    assert "SHORT_SHORTLIST_MAX" in ENTRY                                              # the pool is widened so ranking has something to rank


def test_switches_are_live_protected_and_shipped_equal_default():
    for k, shipped in (("SHORT_SHORTLIST_ENABLED", "true"), ("SHORT_SHORTLIST_MAX", "5")):
        i = CAT.index(f'Flag("{k}"')
        f = CAT[i:CAT.index("\n    Flag(", i + 1)]
        assert "live=True" in f and f'shipped="{shipped}"' in f
        assert re.search(r'"%s":\s*"' % k, AUTO), k
    assert config.SHORT_SHORTLIST_ENABLED is True and config.SHORT_SHORTLIST_MAX == 5
