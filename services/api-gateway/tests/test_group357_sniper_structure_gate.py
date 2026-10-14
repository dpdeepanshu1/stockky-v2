"""group 357 (WS-4): buy_sniper's daily-structure gate. A card must not be surfaced for a chase / stretched / no-room / bearish setup -
including on the early-breakout path (HOLD/WAIT row with strong momentum). No structure = no opinion = unchanged behaviour. Plain asserts;
runs with pytest or `python3 tests/test_group357_sniper_structure_gate.py`."""
from __future__ import annotations

import importlib.util
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_ENV = ("SNIPER_MIN_CONVICTION", "SNIPER_MIN_PRICE", "MAX_PRICE", "SNIPER_MIN_REWARD_RISK", "SNIPER_STRUCTURE_MODE", "STRUCTURE_MAX_CHASE_ATR",
        "STRUCTURE_MAX_STRETCH_ATR", "STRUCTURE_MIN_ROOM_ATR", "STRUCTURE_MIN_RR", "STRUCTURE_BEARISH_BLOCK", "STRUCTURE_BEARISH_BOS_BARS",
        "STRUCTURE_MAX_PRICE_DEV_PCT", "STRUCTURE_MAX_AGE_DAYS", "STRUCTURE_REJECT_STOP_WIDE")


try:  # under pytest: leave os.environ exactly as found (the plain `python3` runner needs nothing)
    import pytest

    @pytest.fixture(autouse=True)
    def _restore_env():
        saved = {k: os.environ.get(k) for k in _ENV}
        yield
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
except ImportError:  # pragma: no cover
    pass


def _load(**env):
    for k in _ENV:
        os.environ.pop(k, None)
    os.environ.update({k: str(v) for k, v in env.items()})
    spec = importlib.util.spec_from_file_location("buy_sniper_t357", os.path.join(os.path.dirname(_HERE), "buy_sniper.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


SUM = {"ms_quality": "ok", "ms_price": 100.0, "ms_atr": 2.0, "ms_trend": "bullish", "ms_chase_atr": 0.4, "ms_stretch_atr": 1.0,
       "ms_room_atr": 3.0, "ms_stop": 96.0, "ms_target": 112.0, "ms_rr_flags": [], "ms_bos_dir": None, "ms_bos_bars": None,
       "ms_bos_failed": None}


def _row(summary=None, **kw):
    r = {"symbol": "ABC", "decision": "BUY", "conviction": 80, "price": 100.0, "technical_score": 75, "fundamental_score": 70}
    r.update(kw)
    if summary is not None:
        r["structure_summary"] = dict(summary)
    return r


def _sum(**kw):
    d = dict(SUM)
    d.update(kw)
    return d


def test_supportive_structure_keeps_card_and_attaches_levels():
    m = _load()
    c = m.build_suggestion(_row(SUM))
    assert c is not None
    st = c["structure"]
    assert st["opinion"] == "ok" and not st["would_reject"] and not st["enforced"]
    assert st["stop"] == 96.0 and st["target"] == 112.0 and st["rr"] == 3.0


def test_no_summary_is_no_opinion_and_never_blocks():
    m = _load()
    c = m.build_suggestion(_row())
    assert c is not None and c["structure"]["opinion"] == "no_opinion" and c["structure"]["no_data"] == "no structure summary"


def test_stale_insufficient_and_unavailable_are_no_opinion():
    m = _load()
    for q in ("insufficient", "stale", "unavailable", None):
        c = m.build_suggestion(_row(_sum(ms_quality=q, ms_trend="bearish")))
        assert c is not None, q  # even a bearish trend is ignored when the quality is not usable
    c = m.build_suggestion(_row(_sum(ms_trend="bearish", ms_asof="2020-01-01T00:00:00+00:00")))
    assert c is not None and "old" in c["structure"]["no_data"]


def test_chase_blocks_in_enforce_and_is_listed_as_rejected():
    m = _load()
    s = _sum(ms_chase_atr=2.5)
    assert m.build_suggestion(_row(s)) is None
    out = m.suggestions_from_scan_payload({"stocks": [_row(s)]})
    assert out["count"] == 0 and out["structure_mode"] == "enforce"
    assert out["structure_rejected"][0]["symbol"] == "ABC" and "chase" in out["structure_rejected"][0]["reasons"][0]
    assert "daily-structure" in out["message"]


def test_early_breakout_path_is_gated_too():
    m = _load()
    row = _row(_sum(ms_stretch_atr=4.0), decision="HOLD", conviction=75, change_pct=2.0)
    assert m._is_buy_candidate(row)  # the HOLD early-breakout path qualifies...
    assert m.build_suggestion(row) is None  # ...but a stretched price no longer reaches the tab


def test_each_rule_fires_alone():
    m = _load()
    cases = {
        "chase": _sum(ms_chase_atr=2.0),
        "stretched": _sum(ms_stretch_atr=3.5),
        "room": _sum(ms_room_atr=0.1),
        "R:R": _sum(ms_target=103.0),
        "too wide": _sum(ms_rr_flags=["stop_too_wide"]),
        "bearish": _sum(ms_trend="bearish"),
        "bearish break": _sum(ms_bos_dir="bearish", ms_bos_bars=2, ms_bos_failed=False),
    }
    for name, summ in cases.items():
        v = m.structure_verdict(_row(summ), 100.0)
        assert v["would_reject"] and v["enforced"] and len(v["reasons"]) == 1, (name, v["reasons"])


def test_failed_or_old_bearish_break_does_not_block():
    m = _load()
    assert not m.structure_verdict(_row(_sum(ms_bos_dir="bearish", ms_bos_bars=2, ms_bos_failed=True)), 100.0)["would_reject"]
    assert not m.structure_verdict(_row(_sum(ms_bos_dir="bearish", ms_bos_bars=9, ms_bos_failed=False)), 100.0)["would_reject"]


def test_stale_shadow_value_is_live_enforce():
    # group370: there is no record-only mode; a stale SNIPER_STRUCTURE_MODE=shadow drops the card like enforce
    m = _load(SNIPER_STRUCTURE_MODE="shadow")
    assert m._sg_mode() == "enforce" and m.build_suggestion(_row(_sum(ms_chase_atr=2.5))) is None
    out = m.suggestions_from_scan_payload({"stocks": [_row(_sum(ms_chase_atr=2.5))]})
    assert out["count"] == 0 and out["structure_mode"] == "enforce" and len(out["structure_rejected"]) == 1


def test_off_leaves_the_card_without_a_structure_key():
    m = _load(SNIPER_STRUCTURE_MODE="off")
    c = m.build_suggestion(_row(_sum(ms_chase_atr=9.0, ms_trend="bearish")))
    assert c is not None and "structure" not in c
    assert m.symbols_needing_structure({"stocks": [_row()]}) == []


def test_bad_mode_value_falls_back_to_enforce():
    m = _load(SNIPER_STRUCTURE_MODE="banana")
    assert m._sg_mode() == "enforce"


def test_limit_of_zero_switches_a_rule_off():
    m = _load(STRUCTURE_MAX_CHASE_ATR="0")
    assert m.build_suggestion(_row(_sum(ms_chase_atr=9.0))) is not None
    m = _load(STRUCTURE_BEARISH_BLOCK="false")
    assert m.build_suggestion(_row(_sum(ms_trend="bearish"))) is not None


def test_price_far_from_structure_close_is_no_opinion():
    m = _load()
    v = m.structure_verdict(_row(_sum(ms_chase_atr=9.0)), 150.0)  # +50% vs the structure close = split / bad data
    assert v["opinion"] == "no_opinion" and not v["would_reject"] and "split" in v["no_data"]


def test_live_price_move_is_shifted_into_the_metrics():
    m = _load()
    v = m.structure_verdict(_row(SUM), 103.0)  # +1.5 ATR since the structure close
    assert abs(v["live"]["chase_atr"] - 1.9) < 1e-6 and abs(v["live"]["room_atr"] - 1.5) < 1e-6
    assert v["would_reject"] and "chase" in v["reasons"][0]  # 0.4 + 1.5 = 1.9 > 1.5


def test_flat_ms_keys_on_the_row_are_read():
    m = _load()
    row = _row()
    row.update({k: v for k, v in _sum(ms_chase_atr=2.5).items()})
    assert m.build_suggestion(row) is None


def test_garbage_never_raises():
    m = _load()
    for bad in ({"ms_quality": "ok", "ms_price": "x", "ms_atr": None}, {"ms_quality": "ok"}, "nope", 5, []):
        row = _row()
        row["structure_summary"] = bad
        m.structure_verdict(row, 100.0)
        m.build_suggestion(row)
    assert m.structure_verdict(None, 100.0)["opinion"] == "no_opinion"


def test_symbols_needing_structure_picks_candidates_without_a_summary():
    m = _load()
    rows = [_row(symbol="LOW", conviction=63), _row(symbol="TOP", conviction=90), _row(SUM, symbol="HAS"),
            _row(symbol="SELL", decision="SELL"), {"_meta": True}, _row(symbol="LOW", conviction=63)]
    assert m.symbols_needing_structure({"stocks": rows}) == ["TOP", "LOW"]
    assert m.symbols_needing_structure({"stocks": rows}, limit=1) == ["TOP"]
    assert m.symbols_needing_structure({"stocks": rows, "min_conviction": 80}) == ["TOP"]
    assert m.symbols_needing_structure("junk") == [] and m.symbols_needing_structure(None) == []


def test_blocked_candidate_does_not_hide_the_good_one():
    m = _load()
    out = m.suggestions_from_scan_payload({"stocks": [_row(_sum(ms_chase_atr=3.0), symbol="BAD", conviction=95), _row(SUM, symbol="OK")]})
    assert [c["symbol"] for c in out["suggestions"]] == ["OK"] and out["structure_rejected"][0]["symbol"] == "BAD"


def test_rr_rejected_card_is_not_counted_as_a_structure_rejection():
    m = _load()
    row = _row(_sum(ms_chase_atr=3.0), target=101.0, stop_loss=95.0)  # fails the sniper's own R:R first
    out = m.suggestions_from_scan_payload({"stocks": [row]})
    assert out["count"] == 0 and out["structure_rejected"] == []


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
            n += 1
    print(f"{n} passed 0 failed")
