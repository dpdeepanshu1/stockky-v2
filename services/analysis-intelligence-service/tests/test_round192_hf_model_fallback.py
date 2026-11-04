"""Round 192: a Hugging Face 'model not supported' answer moves on to the next candidate model instead of switching sentiment off for 6 h."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "news"))
import main as nm  # noqa: E402


class _R:
    def __init__(self, status, text="", content="positive"):
        self.status_code, self.text, self._c = status, text, content

    def json(self):
        return {"choices": [{"message": {"content": self._c}}]}


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(nm, "_HF_BACKOFF_UNTIL", 0.0)
    monkeypatch.setattr(nm, "HF_API_KEY", "K")
    monkeypatch.setattr(nm, "_HF_BAD_MODELS", set())
    monkeypatch.delenv("HF_MODEL_FALLBACKS", raising=False)


def test_default_model_is_not_the_retired_mistral_v02():
    assert "Mistral-7B-Instruct-v0.2" not in nm.HF_MODEL


def test_unsupported_model_falls_through_to_next_and_scores(monkeypatch):
    asked = []

    def post(url, json=None, **k):
        asked.append(json["model"])
        return _R(400, "model is not supported by any provider") if len(asked) == 1 else _R(200)

    monkeypatch.setattr(nm.httpx, "post", post)
    assert nm._score_headline_hf("h") == 0.8
    assert len(asked) == 2 and asked[0] == nm.HF_MODEL and asked[1] != asked[0]
    assert nm._HF_BACKOFF_UNTIL == 0.0          # no pause: a later model answered


def test_refused_model_is_not_asked_again(monkeypatch):
    asked = []

    def post(url, json=None, **k):
        asked.append(json["model"])
        return _R(400, "not supported") if json["model"] == nm.HF_MODEL else _R(200)

    monkeypatch.setattr(nm.httpx, "post", post)
    nm._score_headline_hf("a")
    nm._score_headline_hf("b")
    assert asked.count(nm.HF_MODEL) == 1


def test_all_models_refused_pauses_six_hours(monkeypatch):
    monkeypatch.setattr(nm.httpx, "post", lambda *a, **k: _R(400, "not supported"))
    assert nm._score_headline_hf("h") is None
    assert nm._HF_BACKOFF_UNTIL > nm.time.monotonic() + 21000


def test_fallbacks_none_means_single_model(monkeypatch):
    monkeypatch.setenv("HF_MODEL_FALLBACKS", "none")
    assert nm._hf_models() == [nm.HF_MODEL]


def test_other_400_does_not_try_next_model(monkeypatch):
    asked = []
    monkeypatch.setattr(nm.httpx, "post", lambda url, json=None, **k: (asked.append(1), _R(400, "bad request"))[1])
    nm._score_headline_hf("h")
    assert len(asked) == 1
