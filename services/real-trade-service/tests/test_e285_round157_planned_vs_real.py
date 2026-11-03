"""E285 round 157 (plan item 14): scripts/records_report.py planned vs real reward:risk per exit reason."""
from __future__ import annotations

import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _report():
    spec = importlib.util.spec_from_file_location("records_report_r157", os.path.join(ROOT, "..", "..", "scripts", "records_report.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _t(reason, stop, mfe, entry, exit_):
    return {"exit_reason": reason, "stop_pct": stop, "max_gain_pct": mfe, "entry_price": entry, "exit_price": exit_, "return_pct": 0.0, "gross_pnl": 0.0, "net_pnl": 0.0,
            "charges": 0.0, "opened_at": "2026-11-03T04:00:00+00:00", "closed_at": "2026-11-03T05:00:00+00:00"}


def test_groups_numbers_and_all_row():
    m = _report()
    rows = [_t("stop_hit", 1.0, 0.5, 100.0, 99.0), _t("stop_hit", 1.0, 0.7, 100.0, 98.5), _t("eod_squareoff", 1.0, 1.4, 100.0, 100.3)]
    pv = {g["exit_reason"]: g for g in m.planned_vs_real(rows, 2.0, 2)}
    assert set(pv) == {"ALL", "stop_hit", "eod_squareoff"} and pv["ALL"]["n"] == 3
    s = pv["stop_hit"]
    assert s["planned_target_pct"] == 2.0 and s["mean_mfe_pct"] == 0.6 and s["mean_move_taken_pct"] == -1.25 and s["mfe_vs_target_pct"] == 30.0 and not s["few"]
    e = pv["eod_squareoff"]
    assert e["few"] and e["taken_vs_mfe_pct"] == 21.0          # 0.3 / 1.4
    a = pv["ALL"]
    assert abs(a["real_rr"] - round(0.3 / ((1.0 + 1.5) / 2), 2)) < 1e-9 and a["mean_win_move_pct"] == 0.3 and a["mean_loss_move_pct"] == -1.25


def test_rows_without_stop_or_prices_are_skipped_and_report_and_markdown_carry_it():
    m = _report()
    rows = [_t("stop_hit", 1.0, 0.5, 100.0, 99.0), dict(_t("stop_hit", None, 0.5, 100.0, 99.0)), dict(_t("stop_hit", 1.0, None, 100.0, 99.0))]
    assert m.planned_vs_real(rows)[0]["n"] == 1
    rep = m.build_report(rows, planned_rr=3.0)
    assert rep["planned_vs_real"][0]["planned_target_pct"] == 3.0
    assert "Planned vs real reward:risk" in m.to_markdown(rep)


if __name__ == "__main__":
    f = 0
    for n, fn in sorted(globals().items()):
        if n.startswith("test_") and callable(fn):
            try:
                fn(); print("ok  ", n)
            except Exception as e:  # noqa: BLE001
                f += 1; print("FAIL", n, type(e).__name__, e)
    sys.exit(1 if f else 0)
