"""Round 251: the history / strategy reports measure return % and drawdown % against the capital the mode really uses (REAL = the capital board, not FNO_DEMO_CAPITAL)."""
from __future__ import annotations

import tempfile
from pathlib import Path
from time import time

import capital_board
from fno import capital as fc, paper
from fno.models import ContractRef
from fno.params import Params
from backtest import metrics
from test_engine_paper import NOW, make_cfg


def _closed(net_loss_exit=60.0):
    c = ContractRef("NIFTY", "2026-11-17", 24500.0, "CE", 65, 111, "NSE_FNO", 0.05)
    p = paper.open_position(c, 1, 100.0, NOW.timestamp(), Params(), mode="REAL")
    paper.close_position(p, net_loss_exit, NOW.timestamp() + 600, paper.SL, Params())
    return p


def test_demo_report_capital_is_the_demo_capital():
    cfg = make_cfg(FNO_DEMO_CAPITAL=400000.0)
    assert fc.report_capital(cfg, "DEMO") == 400000.0 and fc.report_capital(cfg, "demo") == 400000.0
    assert fc.report_capital(cfg, "SIM") == 400000.0 and fc.report_capital(cfg, None) == 400000.0


def test_real_report_capital_is_the_board_amount_not_the_demo_capital():
    cfg = make_cfg(FNO_DEMO_CAPITAL=500000.0)                              # make_cfg writes a fresh board giving F&O Rs 5,00,000: change it
    capital_board.write_json(cfg.CAPITAL_ALLOC_DIR, capital_board.ALLOC_FILE, {"ts": time(), "mode": "shadow", "pcts": {"fno": 10.0}, "amounts": {"fno": 120000.0}})
    assert fc.report_capital(cfg, "REAL") == 120000.0 and fc.report_capital(cfg, "real") == 120000.0
    p = _closed()
    m = metrics.summarize([p], fc.report_capital(cfg, "REAL"))["overall"]
    assert m["return_pct"] == round(p.net_pnl / 120000.0 * 100.0, 2)         # not net / 5,00,000


def test_real_report_has_no_percent_when_there_is_no_board():
    cfg = make_cfg()
    cfg.CAPITAL_ALLOC_DIR = tempfile.mkdtemp(prefix="fno_empty_board_")     # nothing written there
    assert fc.report_capital(cfg, "REAL") == 0.0
    m = metrics.summarize([_closed()], fc.report_capital(cfg, "REAL"))["overall"]
    assert "return_pct" not in m and "max_drawdown_pct" not in m            # no % of a made-up capital


def test_report_capital_never_raises():
    class Bad:
        def __getattr__(self, n): raise RuntimeError("boom")
    assert fc.report_capital(Bad(), "REAL") == 0.0 and fc.report_capital(Bad(), "DEMO") == 0.0


def test_the_routes_use_the_report_capital_and_upper_case_the_mode():
    src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    assert "config.FNO_DEMO_CAPITAL" not in src and src.count("fno_capital.report_capital(") == 2
    assert src.count("mode = mode.upper()") == 2 and "mode=mode.upper() if mode else None" in src


def test_net_positions_skips_a_row_that_is_not_an_object():
    import io, json
    from fno import execution

    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False
    body = [None, "x", {"securityId": 111, "netQty": 65, "exchangeSegment": "NSE_FNO"}, {"securityId": 5, "netQty": 0, "exchangeSegment": "NSE_FNO"}, {"securityId": 7, "netQty": "bad", "exchangeSegment": "NSE_FNO"}]
    b = execution.DhanBroker("https://x", "c", "t", opener=lambda req, timeout=None: R(json.dumps(body).encode()))
    assert b.net_positions() == {"111": 65}
