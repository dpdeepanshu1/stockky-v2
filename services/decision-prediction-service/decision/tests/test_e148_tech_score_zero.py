"""E148: technical_score = 0 is a real score, only a missing one becomes the neutral 50 (source-level guard, no framework needed)."""
from pathlib import Path


def test_tech_score_zero_is_kept():
    src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    assert 'int(tech_dict.get("technical_score") or 50)' not in src
    assert "tech_score = int(_ts_raw) if _ts_raw is not None else 50" in src
