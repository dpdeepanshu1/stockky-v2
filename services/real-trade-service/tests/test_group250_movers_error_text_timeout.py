"""group250: momentum-movers failure logs name the exception type; its client timeout is configurable (default 45 s)."""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import watchlist_engine.dynamic_universe as du


def _client(get_side_effect=None, get_return=None):
    c = AsyncMock()
    c.__aenter__ = AsyncMock(return_value=c)
    c.__aexit__ = AsyncMock(return_value=None)
    c.get = AsyncMock(side_effect=get_side_effect) if get_side_effect is not None else AsyncMock(return_value=get_return)
    return c


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestErrText:
    def test_empty_message_gives_type_only(self):
        assert du._err_text(httpx.ReadTimeout("")) == "ReadTimeout"

    def test_message_kept_with_type(self):
        assert du._err_text(RuntimeError("boom")) == "RuntimeError: boom"

    def test_whitespace_message_treated_as_empty(self):
        assert du._err_text(ValueError("   ")) == "ValueError"


class TestMoversTimeout:
    def test_default_45(self, monkeypatch):
        monkeypatch.delenv("DYNAMIC_UNIVERSE_MOVERS_TIMEOUT_S", raising=False)
        assert du._movers_timeout_s() == 45.0

    @pytest.mark.parametrize("raw", ["", "   ", "abc", "0", "-3", "nan"])
    def test_blank_or_bad_values_fall_back(self, monkeypatch, raw):
        monkeypatch.setenv("DYNAMIC_UNIVERSE_MOVERS_TIMEOUT_S", raw)
        assert du._movers_timeout_s() == 45.0

    def test_override(self, monkeypatch):
        monkeypatch.setenv("DYNAMIC_UNIVERSE_MOVERS_TIMEOUT_S", "20")
        assert du._movers_timeout_s() == 20.0

    def test_movers_client_gets_the_configured_timeout(self, monkeypatch):
        monkeypatch.setenv("DYNAMIC_UNIVERSE_MOVERS_TIMEOUT_S", "33")
        seen = []
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"symbols": ["SBIN"]}

        def factory(*a, **kw):
            seen.append(kw.get("timeout"))
            return _client(get_return=resp)

        with patch("candidate_engine.candidates._fetch_volume_shock_universe", new=AsyncMock(return_value=[])), \
             patch("httpx.AsyncClient", side_effect=factory):
            out = _run(du._compute_desired_universe())
        assert out == ["SBIN"]
        assert seen == [15.0, 33.0]  # volume-shock client unchanged, movers client uses the new value


class TestMoversFailureLog:
    def test_timeout_logs_type_and_elapsed(self, caplog):
        with patch("candidate_engine.candidates._fetch_volume_shock_universe", new=AsyncMock(return_value=["INFY"])), \
             patch("httpx.AsyncClient", return_value=_client(get_side_effect=httpx.ReadTimeout(""))):
            with caplog.at_level("WARNING"):
                out = _run(du._compute_desired_universe())
        assert out == ["INFY"]
        msgs = [r.getMessage() for r in caplog.records]
        assert any("momentum-movers source failed after " in m and "(ReadTimeout)" in m for m in msgs)
        assert not any("failed ()" in m for m in msgs)

    def test_volume_shock_failure_names_type(self, caplog):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"symbols": ["AXIS"]}
        with patch("candidate_engine.candidates._fetch_volume_shock_universe",
                   new=AsyncMock(side_effect=httpx.ConnectTimeout(""))), \
             patch("httpx.AsyncClient", return_value=_client(get_return=resp)):
            with caplog.at_level("WARNING"):
                _run(du._compute_desired_universe())
        assert any("volume-shock source failed (ConnectTimeout)" in r.getMessage() for r in caplog.records)

    def test_message_kept_when_present(self, caplog):
        with patch("candidate_engine.candidates._fetch_volume_shock_universe", new=AsyncMock(return_value=[])), \
             patch("httpx.AsyncClient", return_value=_client(get_side_effect=RuntimeError("gateway 502"))):
            with caplog.at_level("WARNING"):
                _run(du._compute_desired_universe())
        assert any("(RuntimeError: gateway 502)" in r.getMessage() for r in caplog.records)
