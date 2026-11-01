"""E285 round 93: the shared Telegram look (tg_style.py). Pure tests, no network, no database. Plain runner: python3 tests/test_e285_tg_style.py (pytest also collects it)."""
from __future__ import annotations

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tg_style as t

NOW = datetime(2026, 11, 1, 18, 45, tzinfo=t.IST)


def _r(text, title="Stockky Trade"):
    return t.render(title, text, NOW)


def test_classify_kinds():
    assert t.classify("Stockky Trade", "📰 *Market-hours news*\n...") == "NEWS"
    assert t.classify("Stockky Trade", "📡 *After-hours scan — close*") == "NEWS"
    assert t.classify("Stockky Trade", "🚨 <b>BUY status UNKNOWN</b> — TCS") == "ALERT"
    assert t.classify("Stockky Trade", "🎯 *Target hit* — SBIN") == "PROFIT"
    assert t.classify("Stockky Trade", "🛑 Stop-loss hit — SBIN") == "STOP"
    assert t.classify("Stockky Trade", "📤 <b>BUY placed</b> — INFY") == "BUY"
    assert t.classify("Stockky Trade", "SELL placed — INFY") == "SELL"
    assert t.classify("Stockky Trade", "Daily summary") == "REPORT"
    assert t.classify("Stockky Trade", "hello") == "INFO"
    assert t.classify(None, None) == "INFO"


def test_header_time_and_divider():
    h = _r("📤 <b>BUY placed</b> — INFY x10\nEntry ₹1,500 | Target ₹1,560 | Stop ₹1,470")["html"]
    lines = h.split("\n")
    assert lines[0] == "<b>📤 BUY placed — INFY x10</b>"
    assert lines[1] == "<code>BUY · Sun 01 Nov · 18:45 IST</code>"
    assert lines[2] == t.DIVIDER


def test_pipe_fields_become_bullets_and_keyval_lines():
    h = _r("📤 <b>BUY placed</b> — INFY\nEntry ₹1,500.00 | Target ₹1,560.00 (4.00%) | Stop ₹1,470.00")["html"]
    assert "▫️ <b>Entry</b>  ₹1,500.00" in h and "▫️ <b>Target</b>  ₹1,560.00 (4.00%)" in h and "▫️ <b>Stop</b>  ₹1,470.00" in h
    k = _r("🎯 *Target hit* — SBIN\nNet P&L: +₹412.50\nNet P&L: -₹80\nHeld: 2h 10m\nsee https://x.y/z")["html"]
    assert "▸ 🟢 <b>Net P&amp;L</b> · +₹412.50" in k and "▸ 🔴 <b>Net P&amp;L</b> · -₹80" in k
    assert "▸ <b>Held</b> · 2h 10m" in k and "see https://x.y/z" in k             # a URL line is not split at its colon


def test_news_items_are_quoted_and_separated():
    h = _r("📰 *Market-hours news*\n🟢 *RELIANCE* · +2.4 · ET\n   _Q2 profit beats <estimates> & margin_\n⛔ *TCS* · -1.0 · MC\n   _TCS deal delayed_")["html"]
    assert "<blockquote>Q2 profit beats &lt;estimates&gt; &amp; margin</blockquote>" in h
    assert "<blockquote>TCS deal delayed</blockquote>" in h
    assert "</blockquote>\n\n⛔ <b>TCS</b>" in h                                           # blank line between two items
    assert h.startswith("<b>📰 Market-hours news</b>")


def test_html_is_escaped_but_caller_tags_survive():
    h = _r("Heads up\nOrder rejected: qty < min & <b>AAA</b> a_b_c _note_")["html"]
    assert "qty &lt; min &amp; <b>AAA</b>" in h and "a_b_c" in h and "<i>note</i>" in h


def test_over_long_render_returns_none_html_so_the_sender_uses_plain():
    assert _r("x\n" * 3000)["html"] is None
    assert t.render(None, None)["html"] is not None                                    # empty text still renders a header


def test_effects_only_in_private_chats_and_when_on():
    old = os.environ.get("TELEGRAM_EFFECTS_ENABLED")
    try:
        os.environ.pop("TELEGRAM_EFFECTS_ENABLED", None)
        assert t.effect_for("PROFIT", "42") == t.EFFECTS["PROFIT"] and t.effect_for("BUY", "42") == t.EFFECTS["BUY"]
        assert t.effect_for("PROFIT", "-100123") is None and t.effect_for("STOP", "42") is None and t.effect_for("NEWS", "42") is None
        os.environ["TELEGRAM_EFFECTS_ENABLED"] = "false"
        assert t.effect_for("PROFIT", "42") is None
    finally:
        os.environ.pop("TELEGRAM_EFFECTS_ENABLED", None)
        if old is not None:
            os.environ["TELEGRAM_EFFECTS_ENABLED"] = old


def test_style_switch():
    old = os.environ.get("TELEGRAM_STYLE_ENABLED")
    try:
        os.environ["TELEGRAM_STYLE_ENABLED"] = "false"
        assert not t.style_enabled()
        os.environ.pop("TELEGRAM_STYLE_ENABLED")
        assert t.style_enabled()
    finally:
        os.environ.pop("TELEGRAM_STYLE_ENABLED", None)
        if old is not None:
            os.environ["TELEGRAM_STYLE_ENABLED"] = old


def test_three_copies_are_identical():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    root = os.path.dirname(here)
    paths = [os.path.join(root, "real-trade-service", "tg_style.py"), os.path.join(root, "position-stocks-service", "tg_style.py"),
             os.path.join(root, "notification-scheduler-service", "notification", "tg_style.py")]
    if all(os.path.exists(p) for p in paths):                                          # inside a single-service image only its own copy exists
        assert len({open(p, "rb").read() for p in paths}) == 1


if __name__ == "__main__":
    n = 0
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f(); n += 1
    print(f"{n} passed")
