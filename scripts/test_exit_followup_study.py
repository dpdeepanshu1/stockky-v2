"""E252: scripts/exit_followup_study.py on made-up records and bars."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import exit_followup_study as s  # noqa: E402

T0 = 1_800_000_000.0


def _rec(**kw):
    base = {"mode": "DEMO", "position_id": 1, "symbol": "X", "entry_price": 100.0, "exit_price": 100.4, "target_price": 102.5, "stop_price": 98.7,
            "opened_at": T0, "closed_at": T0 + 1500, "exit_reason": "STAGNATION_EXIT"}
    base.update(kw)
    return base


def _bars(highs):
    return [(T0 + 1500 + i * 60, 100, h, 99.9, 100, 1000) for i, h in enumerate(highs)]


def test_classify():
    assert s.classify_exit(_rec()) == "stagnation"
    assert s.classify_exit(_rec(exit_reason="STOP_HIT", exit_price=100.4)) == "trail"
    assert s.classify_exit(_rec(exit_reason="STOP_HIT", exit_price=98.7)) is None
    assert s.classify_exit(_rec(exit_reason="TARGET_HIT")) is None


def test_follow_up_reports_best_move_and_first_level():
    r = s.follow_up(_rec(), _bars([100.5, 101.0, 102.6, 103.0]))
    assert r["first_level"] == "target" and abs(r["best_day_pct"] - 3.0) < 1e-9 and r["best_30m_pct"] >= 2.6
    assert s.follow_up(_rec(), _bars([100.5, 100.6]))["first_level"] == "none"
    assert s.follow_up(_rec(), []) is None


def test_early_stopouts_count_only_losing_stops_by_mode():
    recs = [_rec(exit_reason="STOP_HIT", exit_price=98.7, mode="REAL", position_id=1, closed_at=T0 + 300),
            _rec(exit_reason="STOP_HIT", exit_price=98.7, mode="REAL", position_id=2, closed_at=T0 + 3000),
            _rec(exit_reason="STOP_HIT", exit_price=100.4, mode="REAL", position_id=3, closed_at=T0 + 100)]   # trailed above entry: not a stop-out
    assert s.study_early_stopouts(recs, 10) == {"REAL": {"stop_outs": 2, "early": 1}}


def test_main_runs_on_files(tmp_path, capsys):
    f = tmp_path / "r.json"
    f.write_text(json.dumps({"records": [_rec(exit_reason="STOP_HIT", exit_price=98.7, closed_at=T0 + 60)]}))
    assert s.main(["--records", str(f)]) == 0
    assert "1 of 1 stop-outs" in capsys.readouterr().out
