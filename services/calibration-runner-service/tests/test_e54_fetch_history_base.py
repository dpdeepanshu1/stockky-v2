"""E54: the fetch_history job must point --base at the MARKET-DATA service. The gateway has no /history/<SYM> path (only /market/history/<SYM>), so with the
gateway as base every symbol not already on disk got an instant 404 (23 Oct run: saved=0, failed 289 of 400). Offline, stdlib only."""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent / "scripts"))

import jobs as J  # noqa: E402

CTX = {"gw": "http://gw:8000", "md": "http://md:8001", "cal": "/tmp/cal", "run": "/tmp/run", "scripts": "/app/scripts", "rt": "http://rt", "ps": "http://ps",
       "analysis": "http://an", "decision": "http://dec", "notify": "http://nt", "kronos": "http://kr", "tsfm": "http://ts", "resume": False, "python": "python3"}


def _args():
    st = J.JOBS["fetch_history"]["builder"](CTX, J.coerce_params(J.JOBS["fetch_history"], {}))[0]
    return st["argv"]


def test_fetch_base_is_market_data_and_universe_is_gateway():
    a = _args()
    assert a[a.index("--base") + 1] == "http://md:8001"
    assert a[a.index("--universe-url") + 1] == "http://gw:8000/api/universe"


def test_failure_reasons_and_hint(tmp_path, capsys=None):
    import io
    import urllib.error
    import contextlib
    import fetch_history_for_backtest as F

    def fake(url, timeout):
        if "%5ENSEI" in url or "^NSEI" in url:
            return {"candles": [[i] for i in range(300)]}
        raise urllib.error.HTTPError(url, 404, "nf", None, None)

    F._get_json = fake
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = F.main(["--base", "http://gw:8000", "--symbols", "AAA,BBB", "--sleep", "0", str(tmp_path)])
    assert rc == 1
    assert '"failed_reasons": {"http_404": 2}' in out.getvalue()
    assert "MARKET-DATA" in err.getvalue()


if __name__ == "__main__":
    import tempfile
    test_fetch_base_is_market_data_and_universe_is_gateway()
    test_failure_reasons_and_hint(Path(tempfile.mkdtemp()))
    print("ok")
