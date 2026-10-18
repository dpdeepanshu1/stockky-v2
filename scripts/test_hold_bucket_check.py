"""group421: scripts/hold_bucket_check.py - bucketing, returns, de-duplication, the min-trades rule and the bootstrap call."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hold_bucket_check as H


def rec(pid, bucket=None, ret=None, reason="eod_squareoff", mode="REAL", **kw):
    d = {"position_id": pid, "mode": mode, "exit_reason": reason}
    if bucket:
        d["hold_bucket"] = bucket
    if ret is not None:
        d["return_pct"] = ret
    d.update(kw)
    return d


def test_bucket_prefers_the_record_field_then_days_then_timestamps():
    assert H.bucket_of({"hold_bucket": "2_5_days", "held_days": 0}) == "2_5_days"
    assert H.bucket_of({"held_days": 0}) == "same_day"
    assert H.bucket_of({"held_days": 3}) == "2_5_days"
    assert H.bucket_of({"held_days": 9}) == "6_plus_days"
    # Fri 9 Oct 2026 09:30 IST -> Mon 12 Oct 09:30 IST is one session (weekend skipped)
    assert H.bucket_of({"opened_at": "2026-10-09T04:00:00Z", "closed_at": "2026-10-12T04:00:00Z"}) == "1_day"
    assert H.bucket_of({"opened_at": "2026-10-09T04:00:00Z", "closed_at": "2026-10-09T09:30:00Z"}) == "same_day"
    assert H.bucket_of({}) is None


def test_return_pct_fallbacks():
    assert H.return_pct({"return_pct": -1.5}) == -1.5
    assert H.return_pct({"entry_price": 100, "quantity": 10, "net_pnl": -20}) == -2.0
    assert H.return_pct({"entry_price": 100, "quantity": 10, "gross_pnl": 10}) == 1.0
    assert H.return_pct({"entry_price": 100, "exit_price": 103}) == 3.0
    assert H.return_pct({"entry_price": 0, "exit_price": 103}) is None
    assert H.return_pct({}) is None


def test_the_16_oct_shape_gives_no_verdict_and_counts_missing_reasons():
    rs = [rec(i, "same_day", -0.07, "eod_squareoff") for i in range(5)] + [rec(5, "same_day", 0.3, "stop_hit")]
    rs += [rec(10, "1_day", -9.0, "stop_hit"), rec(11, "1_day", -3.0, "emergency_gap_down"), rec(12, "1_day", 1.0, "eod_squareoff"),
           rec(13, "1_day", 0.5, None), rec(14, "1_day", 0.2, None)]
    s = H.summarize(rs, "REAL", 20)
    assert s["trades"] == 11 and s["buckets"]["same_day"]["n"] == 6 and s["buckets"]["1_day"]["n"] == 5
    assert s["buckets"]["2_5_days"]["n"] == 0 and s["held_2plus_sessions"] == 0
    assert s["verdict"].startswith("NO VERDICT") and s["no_exit_reason"] == 2
    assert s["buckets"]["1_day"]["exit_reasons"]["no_exit_reason"] == 2
    txt = H.render(s)
    assert "Held 2+ sessions: 0 of 11" in txt and "(too few)" in txt and "VERDICT: NO VERDICT" in txt


def test_mode_filter_and_all():
    rs = [rec(1, "same_day", 1.0), rec(2, "same_day", 1.0, mode="DEMO")]
    assert H.summarize(rs, "REAL", 1)["trades"] == 1
    assert H.summarize(rs, None, 1)["trades"] == 2


def test_a_clear_difference_is_called_and_a_noisy_one_is_not():
    good = [rec(i, "2_5_days", 3.0 + (i % 3) * 0.1) for i in range(25)]
    base = [rec(100 + i, "same_day", -0.5 + (i % 3) * 0.1) for i in range(25)]
    s = H.summarize(good + base, "REAL", 20)
    assert s["comparisons"][0]["call"] == "better" and s["comparisons"][0]["bucket"] == "2_5_days"
    noisy_a = [rec(i, "2_5_days", ((-1) ** i) * 5.0) for i in range(24)]
    noisy_b = [rec(100 + i, "same_day", ((-1) ** i) * 5.0 + 0.1) for i in range(24)]
    assert H.summarize(noisy_a + noisy_b, "REAL", 20)["comparisons"][0]["call"] == "no clear difference"


def test_one_bucket_only_has_nothing_to_compare():
    s = H.summarize([rec(i, "same_day", 0.1) for i in range(25)], "REAL", 20)
    assert s["comparisons"] == [] and "NOTHING TO COMPARE YET" in s["verdict"]


def test_deterministic():
    rs = [rec(i, "2_5_days", float(i % 7)) for i in range(25)] + [rec(100 + i, "same_day", float(i % 5) - 1) for i in range(25)]
    assert H.summarize(rs, "REAL", 20) == H.summarize(rs, "REAL", 20)


def test_load_records_dedupes_by_position_and_newest_file_wins(tmp_path):
    a, b = tmp_path / "rt_a.json", tmp_path / "rt_b.json"
    a.write_text(json.dumps({"records": [rec(1, "same_day", 1.0), rec(2, "1_day", 2.0)]}))
    b.write_text(json.dumps([rec(2, "1_day", 5.0), rec(3, "same_day", 3.0)]))
    got = {r["position_id"]: r["return_pct"] for r in H.load_records([str(a), str(b)])}
    assert got == {1: 1.0, 2: 5.0, 3: 3.0}


def test_load_records_survives_bad_files(tmp_path, capsys):
    bad, notjson, ok = tmp_path / "x.json", tmp_path / "y.json", tmp_path / "z.json"
    bad.write_text("{not json")
    notjson.write_text(json.dumps({"foo": 1}))
    ok.write_text(json.dumps({"records": [rec(1, "same_day", 1.0), "junk", 5]}))
    assert len(H.load_records([str(bad), str(notjson), str(ok), str(tmp_path / "missing.json")])) == 1
    assert "skipped" in capsys.readouterr().err


def test_main_prints_text_and_json(tmp_path, capsys):
    f = tmp_path / "rt.json"
    f.write_text(json.dumps({"records": [rec(1, "same_day", 0.5)]}))
    assert H.main([str(f)]) == 0
    assert "VERDICT" in capsys.readouterr().out
    assert H.main([str(f), "--json", "--min-trades", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["trades"] == 1


def test_origin_flag_limits_the_bucket_counts(tmp_path, capsys):
    recs = [rec(1, "same_day", 1.0, origin="system"), rec(2, "same_day", -1.0, origin="imported"), rec(3, "1_day", -2.0, origin="imported")]
    f = tmp_path / "rt_x.json"
    f.write_text(json.dumps({"records": recs}))
    assert H.main([str(f), "--origin", "imported", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["trades"] == 2 and out["buckets"]["same_day"]["n"] == 1
    assert H.main([str(f), "--origin", "system", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["trades"] == 1
