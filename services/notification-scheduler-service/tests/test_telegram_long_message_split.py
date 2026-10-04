"""
tests/test_telegram_long_message_split.py

Regression for the "incomplete / lost Telegram alert" bug (2026-10-04).
Telegram rejects any sendMessage text over 4096 chars; _send_telegram used to send
one request regardless of size (and the plain-text retry has the same limit), so a
long scan/summary alert was lost. It now splits on line boundaries into numbered
parts. Short messages are unchanged (one request, no "(1/1)" label).

Run from services/notification-scheduler-service:
    python -m pytest tests/test_telegram_long_message_split.py -v
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import pytest

from notification import main as nmain

CFG = {"telegram_bot_token": "123456789:AAH-testtokentesttokentesttoken", "telegram_chat_id": "42",
       "enabled": {"telegram": True}}


def test_short_message_is_a_single_unlabelled_part():
    assert nmain._split_for_telegram("hello\nworld") == ["hello\nworld"]
    assert nmain._split_for_telegram("") == [""]


def test_long_message_splits_on_line_boundaries_and_loses_nothing():
    lines = [f"line {i} " + "x" * 90 for i in range(200)]          # ~20k chars
    msg = "\n".join(lines)
    parts = nmain._split_for_telegram(msg, 3800)
    assert len(parts) > 1
    assert all(len(p) <= 3800 for p in parts)
    assert "\n".join(parts) == msg                                   # nothing dropped or reordered


def test_single_overlong_line_is_hard_split():
    parts = nmain._split_for_telegram("y" * 9000, 3800)
    assert [len(p) for p in parts] == [3800, 3800, 1400]


@pytest.fixture
def wire(monkeypatch):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Client(transport=transport).post(url, **kw))
    return seen


def test_send_telegram_short_message_one_request_no_label(wire):
    assert nmain._send_telegram(CFG, "Stockky Trade", "BUY *RELIANCE*") == "sent"
    assert len(wire) == 1
    assert wire[0]["text"] == "<b>Stockky Trade</b>\n\nBUY <b>RELIANCE</b>"


def test_send_telegram_long_message_goes_out_in_numbered_parts(wire):
    msg = "\n".join(f"📰 *SYM{i}* · score {i} · news" + " h" * 60 for i in range(150))
    assert len(msg) > 4096
    assert nmain._send_telegram(CFG, "Stockky Trade", msg) == "sent"
    n = len(wire)
    # ~22k chars at <= 3800 per part is 6 parts (the old "2 <= n <= 4" bound was a miscount of the message size);
    # derive the expectation from the splitter's own limit instead of a hard-coded range.
    assert n == len(nmain._split_for_telegram(msg, nmain._TELEGRAM_PART_CHARS))
    assert n == -(-len(msg) // nmain._TELEGRAM_PART_CHARS)            # no wasted extra parts
    assert n >= 2
    for i, body in enumerate(wire, 1):
        assert len(body["text"]) <= 4096
        assert body["text"].startswith(f"<b>Stockky Trade ({i}/{n})</b>")
    assert "SYM0" in wire[0]["text"] and "SYM149" in wire[-1]["text"]


def test_failed_part_is_reported_with_its_number(monkeypatch):
    calls = []

    def post(url, **kw):
        calls.append(kw["json"])
        return httpx.Response(400 if len(calls) >= 3 else 200, text="bad", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    msg = "\n".join("z" * 100 for _ in range(150))
    out = nmain._send_telegram(CFG, "t", msg)
    assert out.startswith("failed: HTTP 400")
    assert "(part 3/" in out                                          # parts 1-2 went out, part 3 failed
