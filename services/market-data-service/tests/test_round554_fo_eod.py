"""Round 554 (E579, Tredye plan P5): the end-of-day F&O pack - OI Flow and futures basis from the F&O bhavcopy. Display only. No network: the download is faked.
The sample file uses the exchange's UDiFF column names as last known (UNVERIFIED against a live file: run `python3 fo_eod.py <real file>` on the VM).

Run from services/market-data-service:  python3 -m pytest tests/test_round554_fo_eod.py -q
"""
from __future__ import annotations

import io
import os
import re
import sys
import zipfile
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import fo_eod

HEAD = "TradDt,BizDt,Sgmt,FinInstrmTp,TckrSymb,XpryDt,StrkPric,OptnTp,ClsPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol"
D = date(2026, 11, 13)


def _line(sym, exp, close, prev, und, oi, chg, kind="STF", td="2026-11-13"):
    return f"{td},{td},FO,{kind},{sym},{exp},,,{close},{prev},{und},{close},{oi},{chg},1000"


def _csv(lines, head=HEAD):
    return head + "\n" + "\n".join(lines) + "\n"


SAMPLE = _csv([
    # AAA: price up 2 %, OI up 10 % -> long build-up; futures 1 % over cash, 10 days to expiry; next month holds 30 % of the first three
    _line("AAA", "2026-11-24", 101.0, 99.0, 100.0, 1000, 100),
    _line("AAA", "2026-12-29", 101.5, 99.5, 100.0, 300, 0),
    _line("AAA", "2027-01-26", 102.0, 100.0, 100.0, 0, 0),
    # BBB: price up, OI down -> short covering
    _line("BBB", "2026-11-24", 51.0, 50.0, 50.8, 900, -100),
    # CCC: price down, OI up -> short build-up; trades at a discount
    _line("CCC", "2026-11-24", 198.0, 200.0, 200.0, 1100, 100),
    # DDD: price down, OI down -> long unwinding
    _line("DDD", "2026-11-24", 19.0, 20.0, 19.1, 800, -200),
    # EEE: price flat -> no clear read
    _line("EEE", "2026-11-24", 10.0, 10.0, 10.0, 500, 50),
    # FFF: an option row and an index future must be ignored
    _line("FFF", "2026-11-24", 5.0, 4.0, 5.0, 100, 10, kind="STO"),
    _line("NIFTY", "2026-11-24", 24000.0, 23900.0, 24000.0, 100, 10, kind="IDF"),
    # GGG: expired-today contract with a later one; no underlying price -> basis None
    _line("GGG", "2026-11-13", 70.0, 69.0, "", 400, -400),
    _line("GGG", "2026-11-24", 70.5, 69.5, "", 600, 600),
])


def _rows():
    pack = fo_eod.build_pack(SAMPLE, D)
    assert pack["ok"], pack
    return {r["symbol"]: r for r in pack["rows"]}, pack


def test_only_stock_futures_rows_are_read():
    rows, pack = _rows()
    assert set(rows) == {"AAA", "BBB", "CCC", "DDD", "EEE", "GGG"} and pack["trade_date"] == "2026-11-13"
    assert pack["counts"]["stocks"] == 6


def test_four_way_rule_and_no_clear_read():
    rows, pack = _rows()
    assert rows["AAA"]["buildup"] == "long_buildup"
    assert rows["BBB"]["buildup"] == "short_covering"
    assert rows["CCC"]["buildup"] == "short_buildup"
    assert rows["DDD"]["buildup"] == "long_unwinding"
    assert rows["EEE"]["buildup"] is None                      # flat price: not forced into a box
    assert rows["GGG"]["buildup"] == "long_buildup"                 # price up, total OI up (+200 over 800)
    assert pack["counts"]["long_buildup"] == 2 and pack["counts"]["short_covering"] == 1 and pack["counts"]["short_buildup"] == 1


def test_oi_is_summed_over_all_expiries_so_a_roll_is_not_a_build_up():
    rows, _ = _rows()
    a = rows["AAA"]
    assert a["oi"] == 1300 and a["oi_chg"] == 100
    assert a["oi_chg_pct"] == round(100 / 1200 * 100, 2)       # previous OI = 1300 - 100
    # a pure roll: near month loses 300, next month gains 300 -> total change 0 -> no read
    roll = _csv([_line("RRR", "2026-11-24", 11.0, 10.0, 10.5, 700, -300), _line("RRR", "2026-12-29", 11.2, 10.2, 10.5, 300, 300)])
    r = {x["symbol"]: x for x in fo_eod.build_pack(roll, D)["rows"]}["RRR"]
    assert r["oi_chg"] == 0 and r["oi_chg_pct"] == 0.0 and r["buildup"] is None


def test_price_change_is_the_near_months_own_change():
    rows, _ = _rows()
    assert rows["AAA"]["price_chg_pct"] == round((101.0 / 99.0 - 1) * 100, 2)
    assert rows["CCC"]["price_chg_pct"] < 0


def test_basis_premium_discount_and_annualised_carry():
    rows, _ = _rows()
    a = rows["AAA"]
    assert a["days_to_expiry"] == 11 and a["spot"] == 100.0
    assert a["basis_pct"] == 1.0
    assert a["annualised_pct"] == round(1.0 * 365 / 11, 1)
    assert rows["CCC"]["basis_pct"] == -1.0 and rows["CCC"]["annualised_pct"] < 0     # a discount keeps its sign


def test_rollover_share_is_next_month_over_the_first_three():
    rows, _ = _rows()
    assert rows["AAA"]["roll_pct"] == round(300 / 1300 * 100, 1)
    assert rows["BBB"]["roll_pct"] is None                     # one expiry only: unknown, not 0
    assert rows["AAA"]["rolling"] is False                     # 11 days out
    near = _csv([_line("NNN", "2026-11-17", 10.2, 10.0, 10.0, 100, 5), _line("NNN", "2026-12-29", 10.3, 10.0, 10.0, 100, 5)])
    assert {x["symbol"]: x for x in fo_eod.build_pack(near, D)["rows"]}["NNN"]["rolling"] is True


def test_missing_values_are_none_never_zero():
    rows, _ = _rows()
    g = rows["GGG"]
    assert g["spot"] is None and g["basis_pct"] is None and g["annualised_pct"] is None
    assert g["days_to_expiry"] == 0 and g["near_expiry"] == "2026-11-13"
    assert g["annualised_pct"] is None                         # expiry day: no carry per day-count
    # a blank or zero close / previous close gives no price change and no read
    bad = _csv([_line("ZZZ", "2026-11-24", "", 10.0, 10.0, 100, 10), _line("YYY", "2026-11-24", 10.0, 0, 10.0, 100, 10)])
    out = {x["symbol"]: x for x in fo_eod.build_pack(bad, D)["rows"]}
    assert out["ZZZ"]["price_chg_pct"] is None and out["ZZZ"]["buildup"] is None and out["ZZZ"]["basis_pct"] is None
    assert out["YYY"]["price_chg_pct"] is None and out["YYY"]["buildup"] is None


def test_a_missing_oi_change_on_any_expiry_makes_the_total_unknown_not_partial():
    txt = _csv([_line("PPP", "2026-11-24", 11.0, 10.0, 10.0, 100, 10), _line("PPP", "2026-12-29", 11.0, 10.0, 10.0, 100, "")])
    r = {x["symbol"]: x for x in fo_eod.build_pack(txt, D)["rows"]}["PPP"]
    assert r["oi_chg"] is None and r["oi_chg_pct"] is None and r["buildup"] is None and r["oi"] == 200


def test_zero_open_interest_stock_is_left_out():
    txt = _csv([_line("QQQ", "2026-11-24", 11.0, 10.0, 10.0, 0, 0)])
    assert fo_eod.build_pack(txt, D)["rows"] == []


def test_wrong_date_and_wrong_layout_are_refused_with_a_reason():
    p = fo_eod.build_pack(SAMPLE, date(2026, 11, 12))
    assert p["ok"] is False and "2026-11-13" in p["error"] and p["rows"] == []
    old = "INSTRUMENT,SYMBOL,EXPIRY_DT,CLOSE,OPEN_INT,CHG_IN_OI\nFUTSTK,AAA,24-NOV-2026,100,1000,10\n"
    p = fo_eod.build_pack(old, D)
    assert p["ok"] is False and "missing column" in p["error"] and "tckrsymb" in p["error"]
    for junk in ("", None, "<html>blocked</html>", HEAD + "\n"):
        assert fo_eod.build_pack(junk, D)["ok"] is False


def test_no_underlying_column_still_gives_oi_flow_but_no_basis():
    head = HEAD.replace(",UndrlygPric", "")
    txt = head + "\n2026-11-13,2026-11-13,FO,STF,AAA,2026-11-24,,,101,99,101,1000,100,5\n"
    r = fo_eod.build_pack(txt, D)["rows"][0]
    assert r["buildup"] == "long_buildup" and r["basis_pct"] is None and r["spot"] is None


def test_classify_edges():
    assert fo_eod.classify(1, 1) == "long_buildup" and fo_eod.classify(-1, -1) == "long_unwinding"
    assert fo_eod.classify(0, 5) is None and fo_eod.classify(5, 0) is None and fo_eod.classify(None, 5) is None and fo_eod.classify(5, None) is None


# ---- sessions: weekends and the system's holiday list
def test_candidate_sessions_skip_weekends_and_listed_holidays():
    ist = lambda y, m, d, h: datetime(y, m, d, h, 0, tzinfo=fo_eod.IST)
    # Sunday 15 Nov 2026: the last completed session is Friday 13 Nov
    assert fo_eod.candidate_sessions(ist(2026, 11, 15, 12))[:2] == [date(2026, 11, 13), date(2026, 11, 12)]
    # Monday 16 Nov 10:00: the file for Monday is not out yet, expected is Friday 13th
    assert fo_eod.candidate_sessions(ist(2026, 11, 16, 10))[0] == date(2026, 11, 13)
    # Monday 16 Nov 20:00: Monday's file should be out
    assert fo_eod.candidate_sessions(ist(2026, 11, 16, 20))[0] == date(2026, 11, 16)
    # Wednesday 11 Nov: Tuesday 10 Nov is a listed holiday (Diwali Balipratipada), so the one before is Monday 9 Nov
    assert fo_eod.candidate_sessions(ist(2026, 11, 11, 12))[0] == date(2026, 11, 9)
    # a holiday evening does not expect today's file
    assert fo_eod.candidate_sessions(ist(2026, 11, 10, 21))[0] == date(2026, 11, 9)


# ---- fetch + cache (download faked)
@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(fo_eod, "_SNAPSHOT_PATH", str(tmp_path / "fo_eod.json"))
    for k in ("FO_EOD_ENABLED", "FO_EOD_TTL_S", "FO_EOD_RETRY_S"):
        monkeypatch.delenv(k, raising=False)
    fo_eod.reset()
    yield
    fo_eod.reset()


SUN = datetime(2026, 11, 15, 8, 0, tzinfo=timezone.utc)          # 13:30 IST on Sunday: expected session is Fri 13 Nov


def _fake(monkeypatch, files, calls):
    def dl(d):
        calls.append(d)
        return files.get(d)
    monkeypatch.setattr(fo_eod, "_download", dl)


def test_get_pack_fetches_newest_first_and_serves_from_cache(monkeypatch):
    calls = []
    _fake(monkeypatch, {date(2026, 11, 13): SAMPLE}, calls)
    p = fo_eod.get_pack(SUN)
    assert p["trade_date"] == "2026-11-13" and p["expected_date"] == "2026-11-13" and p["stale"] is False and len(p["rows"]) == 6 and "error" not in p
    assert calls == [date(2026, 11, 13)]
    fo_eod.get_pack(SUN)
    assert calls == [date(2026, 11, 13)]                           # held and current: not fetched again


def test_a_day_with_no_file_walks_back_and_says_it_is_stale(monkeypatch):
    calls = []
    older = SAMPLE.replace("2026-11-13", "2026-11-12")
    _fake(monkeypatch, {date(2026, 11, 12): older}, calls)
    p = fo_eod.get_pack(SUN)
    assert calls == [date(2026, 11, 13), date(2026, 11, 12)]
    assert p["trade_date"] == "2026-11-12" and p["expected_date"] == "2026-11-13" and p["stale"] is True and p["rows"]


def test_last_good_stays_when_a_refresh_fails_and_error_is_reported(monkeypatch):
    calls = []
    _fake(monkeypatch, {date(2026, 11, 12): SAMPLE.replace("2026-11-13", "2026-11-12")}, calls)
    fo_eod.get_pack(SUN)
    monkeypatch.setenv("FO_EOD_RETRY_S", "60")
    fo_eod._STATE["tried_at"] -= 10_000                              # the retry window has passed
    calls.clear()
    p = fo_eod.get_pack(SUN)
    assert calls == [date(2026, 11, 13)]                             # only days newer than the held one are tried
    assert p["trade_date"] == "2026-11-12" and p["stale"] is True and p["rows"] and p["error"]


def test_nothing_ever_found_gives_an_empty_pack_with_an_error(monkeypatch):
    calls = []
    _fake(monkeypatch, {}, calls)
    p = fo_eod.get_pack(SUN)
    assert p["rows"] == [] and p["trade_date"] is None and p["error"] and len(calls) == 6


def test_retry_is_throttled_while_the_file_is_missing(monkeypatch):
    calls = []
    _fake(monkeypatch, {}, calls)
    fo_eod.get_pack(SUN)
    n = len(calls)
    fo_eod.get_pack(SUN)
    assert len(calls) == n                                           # inside the retry window: no second round of downloads


def test_a_file_with_the_wrong_date_is_never_shown_as_the_day_asked_for(monkeypatch):
    calls = []
    _fake(monkeypatch, {date(2026, 11, 13): SAMPLE.replace("2026-11-13", "2026-11-12")}, calls)   # the 13th's URL served the 12th's data
    p = fo_eod.get_pack(SUN)
    assert p["rows"] == [] and p["trade_date"] is None and "2026-11-12" in p["error"]


def test_switch_off_fetches_nothing(monkeypatch):
    calls = []
    _fake(monkeypatch, {date(2026, 11, 13): SAMPLE}, calls)
    monkeypatch.setenv("FO_EOD_ENABLED", "0")
    p = fo_eod.get_pack(SUN)
    assert calls == [] and p["enabled"] is False and p["rows"] == []


def test_snapshot_survives_a_restart(monkeypatch):
    calls = []
    _fake(monkeypatch, {date(2026, 11, 13): SAMPLE}, calls)
    fo_eod.get_pack(SUN)
    fo_eod.reset()                                                   # a restart: memory gone, the snapshot file stays
    calls.clear()
    _fake(monkeypatch, {}, calls)
    p = fo_eod.get_pack(SUN)
    assert p["trade_date"] == "2026-11-13" and len(p["rows"]) == 6 and calls == []   # held date is already the expected one: nothing newer to look for


def test_download_text_handles_zip_csv_and_block_pages():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("BhavCopy_NSE_FO_0_0_0_20261113_F_0000.csv", SAMPLE)
    assert fo_eod._body_from_download(buf.getvalue()) == SAMPLE
    assert fo_eod._body_from_download(SAMPLE.encode()) == SAMPLE
    assert fo_eod._body_from_download(b"<html>Access Denied</html>") is None
    assert fo_eod._body_from_download(b"") is None and fo_eod._body_from_download(b"PK-not-a-zip") is None


# ---- wiring and isolation (static: this sandbox has no fastapi)
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_route_is_wired_and_thin():
    src = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
    assert '@app.get("/fo-eod")' in src and "fo_eod.get_pack()" in src


def test_nothing_that_trades_imports_the_pack():
    svc = os.path.dirname(HERE)
    hits = []
    for root, _dirs, files in os.walk(svc):
        if "node_modules" in root or os.sep + "tests" in root:
            continue
        for f in files:
            if f.endswith(".py") and f != "fo_eod.py":
                try:
                    t = open(os.path.join(root, f), encoding="utf-8").read()
                except OSError:
                    continue
                if re.search(r"^\s*(?:import fo_eod\b|from fo_eod\b)", t, re.M):    # the gateway's own gateway_routers.fo_eod is a different module
                    hits.append(os.path.relpath(os.path.join(root, f), svc))
    assert hits == ["market-data-service/main.py"], hits
