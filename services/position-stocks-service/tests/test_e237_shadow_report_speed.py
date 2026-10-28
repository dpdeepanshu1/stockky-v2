"""E237: /board/shadow-report speed. Bootstrap uses random.choices with a draw budget (same seeded, deterministic method), and the
endpoint caches the heavy result per `days` (stale-while-revalidate). No database needed."""
import random
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from screening import board_report  # noqa: E402

SRC = (ROOT / "main.py").read_text()


def _route():
    i = SRC.index('@app.get("/board/shadow-report")')
    return SRC[i:SRC.index('@app.get("/board")', i)]


def test_boot_iters_scales_with_budget_and_keeps_floor():
    assert board_report.boot_iters(20, 20) == 2000
    assert board_report.boot_iters(14000, 14000) == board_report.BOOT_MIN_ITERS
    mid = board_report.boot_iters(500, 500)
    assert board_report.BOOT_MIN_ITERS <= mid <= 2000 and mid == 2_000_000 // 1000
    assert board_report.boot_iters(5000, 5000, board_report.BOOT_DRAW_BUDGET_PRIMARY) == 600


def test_bootstrap_deterministic_and_same_answer_as_before_for_small_groups():
    rng = random.Random(3)
    a = [rng.gauss(0.3, 1) for _ in range(300)]
    b = [rng.gauss(0.0, 1) for _ in range(300)]
    ci = board_report.bootstrap_diff(a, b)
    assert ci == board_report.bootstrap_diff(a, b) and ci[0] < ci[1]
    mean_diff = sum(a) / len(a) - sum(b) / len(b)
    assert ci[0] < mean_diff < ci[1]
    assert board_report.bootstrap_diff([], b) is None and board_report.bootstrap_diff(a, []) is None
    assert board_report.bootstrap_diff(a, b, iters=50) is not None


def test_large_groups_finish_fast():
    rng = random.Random(5)
    a = [rng.gauss(0.05, 1) for _ in range(12000)]
    b = [rng.gauss(0.0, 1) for _ in range(12000)]
    t = time.time()
    ci = board_report.bootstrap_diff(a, b)
    assert time.time() - t < 6.0 and ci[0] < 0.05 < ci[1] + 0.2


def test_primary_horizon_gets_the_bigger_budget():
    src = (ROOT / "screening" / "board_report.py").read_text()
    assert "BOOT_DRAW_BUDGET_PRIMARY if hname == PRIMARY" in src


def test_endpoint_caches_and_refreshes_in_background():
    body = _route()
    assert "BOARD_REPORT_CACHE_S" in body and "fresh: bool = False" in body
    assert "threading.Thread(target=_board_report_refresh_bg" in body and "_BOARD_REPORT_BUSY" in SRC
    assert "cache={" in body
    assert re.search(r"_db\.get_session_factory\(\)", SRC[SRC.index("def _board_report_refresh_bg"):SRC.index('@app.get("/board/shadow-report")')])
    assert "BOARD_REPORT_CACHE_S" in (ROOT / "config.py").read_text()


def test_report_still_ends_to_end():
    rows = []
    for d in range(12):
        for i in range(4):
            for note, f in (("would_block:extended", -1.0), ("pass", 0.4)):
                rows.append({"kind": "entry", "day": f"d{d}", "symbol": f"{note[:2]}{i}", "side": "bull", "rank": None, "conviction": None,
                             "note": note, "zone_status": None, "ts": str(i), "params_version": "b1", "fwd15_pct": None,
                             "fwd30_pct": f + 0.1 * (i % 2), "fwd60_pct": None, "fwd_close_pct": None})
    r = board_report.report(rows, 0.12, 30, 10, "b1")["rules"]["chase_guard"]
    assert r["verdict"] == "promote" and r["horizons"]["30m"]["signal"]["n"] == 48


def test_startup_warms_the_cache_in_a_background_thread():
    life = SRC[SRC.index("async def lifespan("):SRC.index("app = FastAPI(")]
    assert "board-report-warm" in life and "_board_report_claim(30)" in life and "BOARD_REPORT_CACHE_S > 0" in life
    claim = SRC[SRC.index("def _board_report_claim("):SRC.index("def _board_report_compute(")]
    assert "_BOARD_REPORT_BUSY.add(days)" in claim and "return False" in claim
