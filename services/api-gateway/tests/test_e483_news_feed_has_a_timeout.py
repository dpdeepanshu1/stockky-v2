"""E483: the Google News query fetch has a timeout (feedparser.parse(url) had none, so a silent server blocked the thread for good)."""
import socket
import threading
import time

import main as gw


def _silent_server():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    held = []
    threading.Thread(target=lambda: [held.append(srv.accept()) for _ in range(3)], daemon=True).start()   # accepts, never answers
    return srv, held


def test_a_silent_server_ends_with_an_error_inside_the_timeout(monkeypatch):
    srv, _held = _silent_server()
    monkeypatch.setenv("NEWS_FEED_TIMEOUT_S", "1")
    t0 = time.time()
    try:
        gw._news_feed_parse(f"http://127.0.0.1:{srv.getsockname()[1]}/rss")
        raised = False
    except Exception:
        raised = True
    finally:
        srv.close()
    assert raised and time.time() - t0 < 5


def test_a_query_that_times_out_is_skipped_and_the_others_still_count(monkeypatch):
    calls = []

    def fake(url):
        calls.append(url)
        if "results+earnings" in url:
            raise TimeoutError("read timed out")
        import types
        return types.SimpleNamespace(entries=[types.SimpleNamespace(title="TCS wins order", summary="")])

    monkeypatch.setattr(gw, "_news_feed_parse", fake)
    monkeypatch.setattr(gw, "_get_all_nse_securities", lambda: ["TCS"])
    monkeypatch.setattr(gw, "_get_nifty_indices", lambda: [])
    assert gw._get_news_mentioned_symbols() == ["TCS"] and len(calls) == 5


def test_non_200_gives_an_empty_feed_and_200_is_parsed(monkeypatch):
    class R:
        def __init__(self, code, body):
            self.status_code, self.content = code, body

    rss = b"<?xml version='1.0'?><rss version='2.0'><channel><title>t</title><item><title>TCS up</title></item></channel></rss>"
    monkeypatch.setattr(gw.httpx, "get", lambda url, **kw: R(503, b"x"))
    assert list(gw._news_feed_parse("http://x").entries) == []
    monkeypatch.setattr(gw.httpx, "get", lambda url, **kw: R(200, rss))
    assert [e.title for e in gw._news_feed_parse("http://x").entries] == ["TCS up"]


def test_timeout_setting_is_clamped_and_junk_falls_back(monkeypatch):
    for raw, want in (("", 10.0), ("abc", 10.0), ("0", 1.0), ("500", 60.0), ("5", 5.0), ("nan", 10.0)):
        monkeypatch.setenv("NEWS_FEED_TIMEOUT_S", raw)
        assert gw._news_feed_timeout_s() == want
