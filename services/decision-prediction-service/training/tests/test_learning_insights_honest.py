"""group 146: /api/insights returns no invented insights.

Run: cd services/decision-prediction-service/training && python3 -m pytest tests/test_learning_insights_honest.py -q
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

app = pytest.importorskip("app")


def test_returns_empty_list_not_placeholders():
    out = app.get_learning_insights()
    assert out["insights"] == []
    assert out["last_updated"]
    assert "No learned insights" in out["note"]


def test_does_not_depend_on_a_report_file_or_the_insights_module(monkeypatch):
    monkeypatch.setattr(app, "HAS_INSIGHTS", False, raising=False)
    monkeypatch.chdir(os.path.dirname(HERE))   # a directory with no training_report.joblib
    assert app.get_learning_insights()["insights"] == []


def test_no_hardcoded_example_text_left_in_source():
    src = open(os.path.join(HERE, "app.py"), encoding="utf-8").read()
    for text in ("higher T+5 success rates", "RSI between 50-65 performs best", "improves win rate by 12%"):
        assert text not in src


def test_route_returns_200_json():
    import asyncio
    resp = asyncio.run(app.api_insights())
    assert resp.status_code == 200
    assert b'"insights":[]' in resp.body.replace(b" ", b"")
