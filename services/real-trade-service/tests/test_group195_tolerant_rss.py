"""
group195: the after-hours scan reads a feed that is not strictly well-formed XML instead of dropping it.

2026-10-06: `afterhours-scan: BusinessStandard returned non-XML content (HTTP 200): not well-formed (invalid token):
line 269, column 51` - a real <?xml?><rss> body rejected by strict ElementTree (typically an unescaped '&' or a control
character in one headline/URL), so a whole source contributed nothing to the catalyst scan.

    cd services/real-trade-service
    python -m pytest tests/test_group195_tolerant_rss.py -v
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import xml.etree.ElementTree as ET
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

from watchlist_engine import afterhours_scan as ahs

_FEED = {"source": "BusinessStandard", "url": "https://example.com/rss", "source_bonus": 10}


def _rss(*items: str) -> str:
    return '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>' + "".join(items) + "</channel></rss>"


def _item(title: str, link: str = "https://x.test/1", pub: str = "Tue, 06 Oct 2026 10:00:00 +0530") -> str:
    return f"<item><title>{title}</title><link>{link}</link><pubDate>{pub}</pubDate></item>"


class TestParseFeedText:
    def test_well_formed_is_strict(self):
        items, how = ahs._parse_feed_text(_rss(_item("Plain headline")))
        assert how == "strict" and [i["title"] for i in items] == ["Plain headline"]

    def test_bom_and_leading_whitespace_are_ignored(self):
        items, how = ahs._parse_feed_text("\ufeff \n" + _rss(_item("Headline")))
        assert how == "strict" and len(items) == 1

    def test_bare_ampersand_in_title_and_url_is_repaired(self):
        text = _rss(_item("Tata & Sons results beat estimates", "https://x.test/a?b=1&c=2"), _item("Second"))
        with pytest.raises(ET.ParseError):
            ET.fromstring(text)                                   # strict really does fail on this body
        items, how = ahs._parse_feed_text(text)
        assert how == "repaired" and len(items) == 2
        assert items[0]["title"] == "Tata & Sons results beat estimates"
        assert items[0]["link"] == "https://x.test/a?b=1&c=2"

    def test_valid_entities_are_not_double_escaped(self):
        items, how = ahs._parse_feed_text(_rss(_item("A &amp; B &lt;C&gt; &#8377;5 &#x20B9;6", "https://x.test/?a=1&amp;b=2"),
                                               _item("Needs repair & more")))
        assert how == "repaired"
        assert items[0]["title"] == "A & B <C> ₹5 ₹6" and items[0]["link"] == "https://x.test/?a=1&b=2"

    def test_control_characters_are_removed(self):
        items, how = ahs._parse_feed_text(_rss(_item("Bad\x0bchar\x00 headline")))
        assert how == "repaired" and items[0]["title"] == "Badchar headline"

    def test_regex_fallback_reads_what_xml_cannot(self):
        text = ('<?xml version="1.0"?><rss><channel><item><title><![CDATA[Stocks <b>rally</b> today]]></title>'
                "<link>https://x.test/9</link><pubDate>Tue, 06 Oct 2026 09:00:00 +0530</pubDate></item>"
                "<item><title>Broken <unclosed></title><link>https://x.test/10</link></item></channel>")   # no </rss>
        items, how = ahs._parse_feed_text(text)
        assert how == "regex" and items[0]["title"] == "Stocks rally today"
        assert items[1]["title"] == "Broken" and items[1]["link"] == "https://x.test/10"

    def test_atom_entries_via_regex_use_href_and_updated(self):
        text = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Atom &amp; headline</title>'
                '<link rel="alternate" href="https://x.test/atom?a=1&amp;b=2"/><updated>2026-10-06T04:00:00Z</updated></entry>'
                "<broken")
        items, how = ahs._parse_feed_text(text)
        assert how == "regex"
        assert items == [{"title": "Atom & headline", "link": "https://x.test/atom?a=1&b=2", "pubDate": "2026-10-06T04:00:00Z"}]

    def test_html_page_still_raises(self):
        with pytest.raises(ET.ParseError):
            ahs._parse_feed_text("<html><body>Access denied<br></body></html>")   # unclosed <br>: not XML, no items

    def test_well_formed_non_feed_is_just_empty(self):
        assert ahs._parse_feed_text("<html><body>Access denied</body></html>") == ([], "strict")   # as before

    def test_empty_body_raises(self):
        with pytest.raises(ET.ParseError):
            ahs._parse_feed_text("")


def _client(text: str):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.text = text
    resp.status_code = 200
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


class TestFetchRssItems:
    def test_repaired_feed_yields_items_and_logs_how(self, caplog):
        with patch("httpx.AsyncClient", return_value=_client(_rss(_item("Tata & Sons")))):
            with caplog.at_level(logging.INFO):
                items = asyncio.run(ahs._fetch_rss_items(_FEED))
        assert [i["title"] for i in items] == ["Tata & Sons"]
        assert "not strictly well-formed" in caplog.text and "repaired" in caplog.text
        assert "non-XML" not in caplog.text

    def test_html_interstitial_still_warns_and_returns_nothing(self, caplog):
        with patch("httpx.AsyncClient", return_value=_client("<html><body>blocked<br></body></html>")):
            with caplog.at_level(logging.WARNING):
                assert asyncio.run(ahs._fetch_rss_items(_FEED)) == []
        assert "non-XML content" in caplog.text
