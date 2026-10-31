"""E271: scripts/flow_tape_report.py on made-up trades."""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import flow_tape_report as r  # noqa: E402


def _ps(i, day, pct, against=None, reliable=True, tape_exit=None, status="TARGET_HIT", side="LONG", entry=100.0, exit_=101.0):
    row = {"id": i, "symbol": "S%d" % i, "status": status, "side": side, "entry_price": entry, "exit_price": exit_, "realized_pnl": pct * 10,
           "realized_pnl_pct": pct, "closed_at": "2026-10-%02dT10:00:00+00:00" % day, "opened_at": "2026-10-%02dT09:30:00+00:00" % day}
    if against is not None:
        row["tape"] = {"against": against, "reliable": reliable}
    if tape_exit:
        row["tape_exit"] = tape_exit
    return row


def test_normalise_skips_open_and_unpriced_rows():
    assert r.normalise(_ps(1, 1, 0.5, status="OPEN"), "ps") is None
    assert r.normalise({"id": 1, "status": "TARGET_HIT"}, "ps") is None
    n = r.normalise(_ps(1, 1, 0.5, against=2), "ps")
    assert n["against"] == 2 and n["day"] == "2026-10-01" and n["reliable"] is True


def test_normalise_real_trade_uses_net_pnl_and_tape_tightens():
    n = r.normalise({"id": 5, "symbol": "X", "net_realized_pnl": -12.5, "realized_pnl": -10, "closed_at": "2026-10-02T10:00:00+00:00",
                     "tape": {"against": 1, "reliable": True}, "tape_tightens": 2}, "rt")
    assert n["pnl"] == -12.5 and n["tightens"] == 2 and r.bucket_of(n["against"]) == "1"


def test_buckets():
    assert [r.bucket_of(x) for x in (None, -1, 0, 1, 2, 3, 6)] == [None, None, "0", "1", "2", "3+", "3+"]


def test_summarize_clusters_by_day():
    recs = [{"day": "d1", "pct": 1.0, "pnl": 10}, {"day": "d1", "pct": 1.0, "pnl": 10}, {"day": "d2", "pct": -1.0, "pnl": -10}, {"day": "d2", "pct": -1.0, "pnl": -10}]
    s = r.summarize(recs)
    assert s["n"] == 4 and s["days"] == 2 and s["mean"] == 0.0 and s["win_rate_pct"] == 50.0
    assert abs(s["se"] - 0.5 * 2 ** 0.5) < 1e-3 and s["se"] > 0.577     # days move together: wider than the naive std / sqrt(n)
    one_day = r.summarize([{"day": "d1", "pct": 1.0, "pnl": 1}, {"day": "d1", "pct": 2.0, "pnl": 2}])
    assert one_day["se"] is None and one_day["lo90"] is None
    assert r.summarize([])["n"] == 0


def _many(day_count, per_day, pct_fn, against):
    rows, i = [], 0
    for d in range(1, day_count + 1):
        for k in range(per_day):
            i += 1
            rows.append(_ps(i, d, pct_fn(d, k), against=against))
    return rows


def test_cutoff_verdicts():
    # blocked clearly worse than allowed -> KEEP
    rows = _many(8, 6, lambda d, k: 0.5 + 0.05 * (k % 3), 0) + _many(8, 6, lambda d, k: -0.6 - 0.05 * (k % 3), 2)
    for i, x in enumerate(rows):
        x["id"] = i
    rep = r.build([rows], [], [], 30, 5)
    assert rep["entry"]["cutoff"]["verdict"] == "KEEP", rep["entry"]["cutoff"]
    # blocked clearly better -> RELAX
    rows = _many(8, 6, lambda d, k: -0.5 - 0.05 * (k % 3), 0) + _many(8, 6, lambda d, k: 0.6 + 0.05 * (k % 3), 2)
    for i, x in enumerate(rows):
        x["id"] = i
    assert r.build([rows], [], [], 30, 5)["entry"]["cutoff"]["verdict"] == "RELAX"


def test_not_enough_data_when_nothing_was_blocked():
    rows = _many(8, 6, lambda d, k: 0.3, 0)
    for i, x in enumerate(rows):
        x["id"] = i
    rep = r.build([rows], [], [], 30, 5)
    assert rep["entry"]["cutoff"]["verdict"] == "NOT_ENOUGH_DATA"
    assert "no trade with that many adverse checks" in r.render(rep)


def test_unreliable_tape_is_left_out():
    rows = [_ps(1, 1, 1.0, against=3, reliable=False)]
    assert r.build([rows], [], [], 1, 1)["entry"]["trades_with_reliable_tape"] == 0


def test_saved_pct_is_an_upper_bound_only_for_a_stop_exit_on_the_tape_stop():
    tx = [{"old_stop": 98.7, "new_stop": 100.2, "old_target": 102.5, "new_target": 101.5}]
    rec = r.normalise(_ps(1, 1, 0.2, status="STOP_HIT", tape_exit=tx, entry=100.0, exit_=100.2), "ps")
    assert abs(r.saved_pct(rec) - 1.5) < 1e-9
    short = r.normalise(_ps(2, 1, 0.2, status="STOP_HIT", side="SHORT", entry=100.0, exit_=99.8,
                            tape_exit=[{"old_stop": 101.3, "new_stop": 99.8}]), "ps")
    assert abs(r.saved_pct(short) - 1.5) < 1e-9
    assert r.saved_pct(r.normalise(_ps(3, 1, 1.0, status="TARGET_HIT", tape_exit=tx), "ps")) is None      # not a stop exit
    assert r.saved_pct(r.normalise(_ps(4, 1, -1.3, status="STOP_HIT", tape_exit=tx, exit_=98.7), "ps")) is None   # closed far from the tape's stop
    assert r.saved_pct(r.normalise(_ps(5, 1, 0.2, status="STOP_HIT"), "ps")) is None


def test_exit_report_counts_tightened_trades():
    tx = [{"old_stop": 98.7, "new_stop": 100.2}]
    rows = [_ps(1, 1, 0.2, status="STOP_HIT", tape_exit=tx, exit_=100.2), _ps(2, 1, 0.9)]
    ex = r.build([rows], [], [], 30, 5)["exit"]
    assert ex["tightened"]["n"] == 1 and ex["not_tightened"]["n"] == 1 and ex["stop_exits_after_tighten"] == 1
    assert ex["verdict"] == "NOT_ENOUGH_DATA"


def test_store_merge_replaces_same_trade_and_keeps_others():
    a = r.normalise(_ps(1, 1, 0.5, against=1), "ps")
    b = r.normalise(_ps(2, 1, -0.5, against=2), "ps")
    a2 = dict(a, pct=0.9)
    out = r.merge([a, b], [a2])
    assert len(out) == 2 and out[0]["pct"] == 0.9 and out[1]["id"] == 2


def test_main_appends_to_store_and_prints(capsys=None):
    with tempfile.TemporaryDirectory() as t:
        ps, store = Path(t) / "ps.json", Path(t) / "store.jsonl"
        ps.write_text(json.dumps({"trades": [_ps(1, 1, 0.5, against=1)]}))
        assert r.main(["--ps", str(ps), "--store", str(store), "--json"]) == 0
        assert r.main(["--ps", str(ps), "--store", str(store), "--json"]) == 0      # same file twice: no duplicate row
        assert len(store.read_text().strip().splitlines()) == 1
        assert r.main(["--ps", str(Path(t) / "missing.json")]) == 0                  # unreadable input: reported, not a crash


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
            n += 1
    print(n, "passed")
