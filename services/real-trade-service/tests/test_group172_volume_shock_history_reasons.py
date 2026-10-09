"""
group172 (item 5 of the 2026-10-06 list): volume_shock "no daily history" - say why, and stop re-asking for
symbols that definitely have none.

  * _fetch_history leaves the failure reason per symbol (timeout class name, HTTP code, empty answer)
  * HTTP 404/400, an empty answer or fewer than 6 candles pause the symbol for CANDIDATE_VOLUME_SHOCK_NOHIST_TTL_S;
    timeouts, 429 and 5xx never do; a later good answer clears the pause; TTL 0 turns it off
  * the cycle's one WARNING lists the reasons; paused symbols are counted on an INFO line and do not feed the
    "market-data failing" alarm
No network: fake clients; clock patched where time matters.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import intraday_eligibility
import models
from candidate_engine import candidates as cd


def run(c):
    return asyncio.run(c)


class _Resp:
    def __init__(self, code, payload=None):
        self.status_code, self._p, self.text = code, payload, ""

    def json(self):
        return self._p


class _Client:
    """/quote always answers a 5% mover; /history answers per `hist` (a status code, an exception, or candles)."""

    def __init__(self, hist):
        self.hist, self.history_calls = hist, 0

    async def get(self, url, timeout=None, params=None):
        if "/quote/" in url:
            return _Resp(200, {"price": 105.0, "previous_close": 100.0})
        self.history_calls += 1
        h = self.hist
        if isinstance(h, Exception):
            raise h
        if isinstance(h, int):
            return _Resp(h, {})
        return _Resp(200, {"candles": h})


def _candles(n):
    return [{"date": f"2026-09-{i + 1:02d} 00:00", "open": 100, "high": 101, "low": 99, "close": 100,
             "volume": 1000} for i in range(n)]


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setattr(cd, "VOLUME_SHOCK_NOHIST_TTL_S", 6 * 3600.0)
    monkeypatch.setattr(cd, "VOLUME_SHOCK_QUOTE_PREFILTER", True)
    cd.clear_history_state()
    yield
    cd.clear_history_state()


# ── reasons recorded by _fetch_history ───────────────────────────────────────

class _Boom(Exception):
    pass


def test_reason_is_the_exception_class_name_not_its_empty_message():
    run(cd._fetch_history(_Client(_Boom()), "AAA", "1mo", "1d"))
    assert cd._HIST_REASON["AAA"] == "_Boom"


def test_reason_is_the_http_code():
    run(cd._fetch_history(_Client(429), "AAA", "1mo", "1d"))
    assert cd._HIST_REASON["AAA"] == "HTTP 429"


def test_reason_is_empty_answer_and_cleared_by_a_good_answer():
    run(cd._fetch_history(_Client([]), "AAA", "1mo", "1d"))
    assert cd._HIST_REASON["AAA"] == "empty answer"
    assert run(cd._fetch_history(_Client(_candles(8)), "AAA", "1mo", "1d"))
    assert "AAA" not in cd._HIST_REASON


# ── known-no-history pause ───────────────────────────────────────────────────

@pytest.mark.parametrize("hist,why", [(404, "HTTP 404"), (400, "HTTP 400"), ([], "empty answer"),
                                      (_candles(3), "short history")])
def test_definite_no_history_is_not_requested_again(hist, why):
    c = _Client(hist)
    first = run(cd._volume_shock_analysis(c, "NEWCO"))
    assert "Insufficient daily history" in first["reject_reason"]
    assert cd._HIST_REASON["NEWCO"] == why
    second = run(cd._volume_shock_analysis(c, "NEWCO"))
    assert "No daily history on record" in second["reject_reason"]
    assert c.history_calls == 1


@pytest.mark.parametrize("hist", [429, 500, 503, _Boom()])
def test_transient_failures_are_retried_every_time(hist):
    c = _Client(hist)
    for _ in range(3):
        out = run(cd._volume_shock_analysis(c, "SLOWCO"))
        assert "Insufficient daily history" in out["reject_reason"]
    assert c.history_calls == 3 and "SLOWCO" not in cd._HIST_NONE_UNTIL


def test_pause_ends_after_the_ttl(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(cd.time, "monotonic", lambda: t[0])
    c = _Client(404)
    run(cd._volume_shock_analysis(c, "NEWCO"))
    t[0] += 6 * 3600 - 1
    assert "No daily history on record" in run(cd._volume_shock_analysis(c, "NEWCO"))["reject_reason"]
    t[0] += 2
    assert "Insufficient daily history" in run(cd._volume_shock_analysis(c, "NEWCO"))["reject_reason"]
    assert c.history_calls == 2


def test_a_good_answer_clears_the_pause(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(cd.time, "monotonic", lambda: t[0])
    run(cd._volume_shock_analysis(_Client(404), "NEWCO"))
    t[0] += 6 * 3600 + 1
    run(cd._volume_shock_analysis(_Client(_candles(21)), "NEWCO"))
    assert "NEWCO" not in cd._HIST_NONE_UNTIL


def test_ttl_zero_turns_the_pause_off(monkeypatch):
    monkeypatch.setattr(cd, "VOLUME_SHOCK_NOHIST_TTL_S", 0.0)
    c = _Client(404)
    run(cd._volume_shock_analysis(c, "NEWCO"))
    run(cd._volume_shock_analysis(c, "NEWCO"))
    assert c.history_calls == 2 and cd._HIST_NONE_UNTIL == {}


def test_pause_is_per_symbol():
    run(cd._volume_shock_analysis(_Client(404), "NEWCO"))
    c = _Client(_candles(21))
    run(cd._volume_shock_analysis(c, "OTHER"))
    assert c.history_calls == 1


def test_note_history_reason_never_raises_and_is_bounded(monkeypatch):
    for i in range(5002):
        cd._note_history_reason(f"S{i}", "HTTP 500")
    assert len(cd._HIST_REASON) <= 5001
    # group 272: undo the None inside the test. The autouse `_on` fixture's teardown calls clear_history_state() BEFORE a
    # function-level monkeypatch is undone (monkeypatch is set up first, so torn down last) and crashed on None.
    with monkeypatch.context() as m:
        m.setattr(cd, "_HIST_REASON", None)
        cd._note_history_reason("X", "boom")      # swallowed


# ── cycle summary ────────────────────────────────────────────────────────────

_engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                        poolclass=__import__("sqlalchemy.pool", fromlist=["StaticPool"]).StaticPool)


@pytest.fixture()
def db():
    models.Base.metadata.drop_all(_engine)
    models.Base.metadata.create_all(_engine)
    s = sessionmaker(bind=_engine)()
    yield s
    s.close()


def _drive(db, monkeypatch, universe, reasons, why):
    monkeypatch.setattr(intraday_eligibility, "get_restricted_symbols", lambda d: set())
    monkeypatch.setattr(config, "VOLUME_SHOCK_QUALITY_GATE_ENABLED", False)

    async def fake_universe(client):
        return list(universe)

    async def noop(*a, **k):
        return None

    async def fake_vs(client, symbol):
        if symbol in why:
            cd._HIST_REASON[symbol] = why[symbol]
        return {"reject_reason": reasons[symbol], "atr_pct": None}
    monkeypatch.setattr(cd, "_fetch_volume_shock_universe", fake_universe)
    monkeypatch.setattr(cd, "_prefetch_quotes_bulk", noop)
    monkeypatch.setattr(cd, "_volume_shock_analysis", fake_vs)
    return run(cd._refresh_volume_shock_candidates(db, "REAL", set()))


def test_warning_lists_the_reasons(db, monkeypatch, caplog):
    ins = "Insufficient daily history for volume-shock check."
    reasons = {"A": ins, "B": ins, "C": ins, "D": "Today's volume 1.0x 20-day average < 1.5x volume-shock threshold."}
    why = {"A": "ReadTimeout", "B": "ReadTimeout", "C": "HTTP 404"}
    with caplog.at_level("INFO"):
        _drive(db, monkeypatch, list(reasons), reasons, why)
    msg = next(r.getMessage() for r in caplog.records if "daily history unavailable" in r.getMessage())
    assert "3 of 4" in msg and "ReadTimeout x2" in msg and "HTTP 404 x1" in msg


def test_reason_unknown_when_none_was_recorded(db, monkeypatch, caplog):
    reasons = {"A": "Insufficient daily history for volume-shock check."}
    with caplog.at_level("INFO"):
        _drive(db, monkeypatch, ["A"], reasons, {})
    assert any("unknown x1" in r.getMessage() for r in caplog.records)


def test_paused_symbols_get_an_info_line_and_no_alarm(db, monkeypatch, caplog):
    known = "No daily history on record for volume-shock check (known, not retried yet)."
    reasons = {f"S{i}": known for i in range(4)}
    with caplog.at_level("INFO"):
        _drive(db, monkeypatch, list(reasons), reasons, {})
    msgs = [r.getMessage() for r in caplog.records]
    assert any("4 of 4 symbol(s) skipped without a request" in m for m in msgs)
    assert not any("daily history unavailable" in m for m in msgs)
    assert not any("rejected for missing" in m for m in msgs)
