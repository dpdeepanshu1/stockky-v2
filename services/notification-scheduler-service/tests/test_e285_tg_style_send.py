"""E285 round 93: _send_telegram sends the styled layout, asks for a message effect only in a private chat, retries once without the effect when Telegram refuses it,
adds the dashboard button when TELEGRAM_DASHBOARD_URL is https, and keeps the old layout with the switch off.
Run from services/notification-scheduler-service:  python -m pytest tests/test_e285_tg_style_send.py -v"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import pytest

from notification import main as nmain

CFG = {"telegram_bot_token": "123456789:AAH-testtokentesttokentesttoken", "telegram_chat_id": "42", "enabled": {"telegram": True}}


@pytest.fixture
def wire(monkeypatch):
    seen, statuses = [], []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        code = statuses.pop(0) if statuses else 200
        return httpx.Response(code, text="ok")

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Client(transport=transport).post(url, **kw))
    for k in ("TELEGRAM_STYLE_ENABLED", "TELEGRAM_EFFECTS_ENABLED", "TELEGRAM_DASHBOARD_URL"):
        monkeypatch.delenv(k, raising=False)
    return seen, statuses


def test_styled_layout_and_effect_in_a_private_chat(wire):
    seen, _ = wire
    assert nmain._send_telegram(CFG, "Stockky Trade", "🎯 *Target hit* — SBIN\nNet P&L: +₹412.50") == "sent"
    body = seen[0]
    assert body["parse_mode"] == "HTML" and body["text"].startswith("<b>🎯 Target hit — SBIN</b>\n<code>TARGET HIT")
    assert "▸ 🟢 <b>Net P&amp;L</b> · +₹412.50" in body["text"]
    assert body["message_effect_id"]


def test_no_effect_in_a_group_chat_or_when_off(wire, monkeypatch):
    seen, _ = wire
    nmain._send_telegram(dict(CFG, telegram_chat_id="-100123"), "Stockky Trade", "🎯 *Target hit* — SBIN")
    assert "message_effect_id" not in seen[0]
    monkeypatch.setenv("TELEGRAM_EFFECTS_ENABLED", "false")
    nmain._send_telegram(CFG, "Stockky Trade", "🎯 *Target hit* — SBIN")
    assert "message_effect_id" not in seen[1]


def test_a_refused_effect_is_retried_once_without_it(wire):
    seen, statuses = wire
    statuses.append(400)
    assert nmain._send_telegram(CFG, "Stockky Trade", "🎯 *Target hit* — SBIN") == "sent"
    assert len(seen) == 2 and "message_effect_id" in seen[0] and "message_effect_id" not in seen[1]
    assert seen[0]["text"] == seen[1]["text"]


def test_dashboard_button_only_for_https(wire, monkeypatch):
    seen, _ = wire
    monkeypatch.setenv("TELEGRAM_DASHBOARD_URL", "http://insecure")
    nmain._send_telegram(CFG, "Stockky Trade", "hello")
    assert "reply_markup" not in seen[0]
    monkeypatch.setenv("TELEGRAM_DASHBOARD_URL", "https://stockky.example")
    nmain._send_telegram(CFG, "Stockky Trade", "hello")
    assert seen[1]["reply_markup"]["inline_keyboard"][0][0]["url"] == "https://stockky.example"


def test_switch_off_keeps_the_old_layout(wire, monkeypatch):
    seen, _ = wire
    monkeypatch.setenv("TELEGRAM_STYLE_ENABLED", "false")
    nmain._send_telegram(CFG, "Stockky Trade", "BUY *RELIANCE*")
    assert seen[0]["text"] == "<b>Stockky Trade</b>\n\nBUY <b>RELIANCE</b>" and "message_effect_id" not in seen[0]


def test_long_message_parts_stay_under_the_telegram_limit(wire):
    seen, _ = wire
    msg = "\n".join(f"📰 *SYM{i}* · score {i} · news & more" + " h" * 60 for i in range(150))
    assert nmain._send_telegram(CFG, "Stockky Trade", msg) == "sent"
    assert len(seen) > 1 and all(len(b["text"]) <= 4096 for b in seen)
