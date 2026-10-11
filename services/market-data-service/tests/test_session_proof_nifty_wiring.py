"""group320: the Nifty price feed for the closed-market proof is registered and reads this service's own /quote path."""
import ast
import os
from types import SimpleNamespace

import main

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_main_registers_the_nifty_fetcher_at_import():
    src = open(os.path.join(_HERE, "main.py"), encoding="utf-8").read()
    assert "register_nifty_fetcher(_session_nifty_price)" in src
    ast.parse(src)


def test_fetcher_reads_price_from_a_dict_quote(monkeypatch):
    monkeypatch.setattr(main, "get_quote", lambda sym: {"symbol": sym, "price": 25123.4})
    assert main._session_nifty_price() == 25123.4


def test_fetcher_reads_price_from_a_model_quote(monkeypatch):
    seen = []
    monkeypatch.setattr(main, "get_quote", lambda sym: (seen.append(sym), SimpleNamespace(price=25001.5))[1])
    assert main._session_nifty_price() == 25001.5
    assert seen == ["^NSEI"]


def test_angelone_feed_loop_probes_nifty_after_each_cycle():
    src = open(os.path.join(_HERE, "angelone_ws_feed.py"), encoding="utf-8").read()
    assert "asyncio.to_thread(session_proof.probe_nifty)" in src
    assert src.index("await _poll_cycle()") < src.index("session_proof.probe_nifty")
