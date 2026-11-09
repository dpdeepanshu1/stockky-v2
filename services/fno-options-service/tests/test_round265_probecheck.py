"""Round 265: the probe result reader, the charges-vs-contract-note check (fno/probecheck.py), its two routes and scripts/fno_charges_compare.py. No network, no money."""
from __future__ import annotations

import importlib.util
import json
import os

import pytest

import config
from fno import costs, probecheck
from fno.params import Params
from fno.store import Store

P = Params()


def make_probe(und="NIFTY", qty=65, buy=100.0, sell=110.0, extra_sell=False, live=True, code=0):
    c = costs.round_trip(und, buy, sell, qty, P)
    sells = [{"side": "SELL", "price": sell, "qty": qty, "ok": True, "order_id": "S1", "status": "TRADED", "filled_qty": qty if not extra_sell else qty - 10, "avg_price": sell}]
    if extra_sell:
        sells.append({"side": "SELL", "price": sell - 0.5, "qty": 10, "ok": True, "order_id": "S2", "status": "TRADED", "filled_qty": 10, "avg_price": sell - 0.5})
    pnl = {"bought_avg": buy, "sold_avg": sell, "qty": qty, "gross": (sell - buy) * qty, "charges": c.total, "charges_detail": c.to_json(), "net": 0.0, "market_move": 1.0, "spread_buy": 1.0, "spread_sell": 1.0}
    return {"live": live, "underlying": und, "side": "CE", "exit_code": code, "contract": "NIFTY 24500 CE", "bought": qty, "sold": qty, "dhan_net_position_after": 0, "pnl": pnl,
            "orders": [{"side": "BUY", "price": buy, "qty": qty, "ok": True, "order_id": "B1", "status": "TRADED", "filled_qty": qty, "avg_price": buy}] + sells}


def note_of(probe, **over):
    d = {k: round(v, 2) for k, v in probe["pnl"]["charges_detail"].items() if k in probecheck.LINES}
    d.update(over)
    return d


def by(c):
    return {r["line"]: r for r in c["lines"]}


# ── compare ──
def test_a_note_equal_to_the_model_is_matched():
    pr = make_probe()
    c = probecheck.compare(pr, note_of(pr), P)
    assert c["verdict"] == "MATCHED" and all(r["ok"] for r in c["lines"]) and "not proof for every trade" in c["text"]


def test_a_note_within_five_paise_is_rounding_and_still_matches():
    pr = make_probe()
    n = note_of(pr)
    n["gst"] += 0.04
    assert probecheck.compare(pr, n, P)["verdict"] == "MATCHED"


def test_one_wrong_line_differs_and_names_the_rate_to_look_at():
    pr = make_probe()
    n = note_of(pr)
    n["stt"] = round(pr["pnl"]["charges_detail"]["stt"] * 0.1 / 0.15 * 1.0 + 1.0, 2)      # a different STT rate
    c = probecheck.compare(pr, n, P)
    r = by(c)["stt"]
    assert c["verdict"] == "DIFFERS" and r["ok"] is False and r["param"] == "FNO_STT_SELL_PCT" and r["current"] == P.stt_sell_pct
    assert r["implied"] == pytest.approx(n["stt"] / (110.0 * 65) * 100, rel=1e-6) and "STT" in c["text"]
    assert all(by(c)[k]["ok"] for k in probecheck.LINES if k != "stt")


def test_implied_rates_for_every_line_come_from_the_right_turnover():
    pr = make_probe()
    n = {k: round(v * 2, 2) for k, v in note_of(pr).items()}                          # every line doubled
    c = probecheck.compare(pr, n, P)
    b = by(c)
    assert c["verdict"] == "DIFFERS"
    assert b["brokerage"]["implied"] == pytest.approx(40.0)                                # per order: 80 over 2 orders
    assert b["stt"]["implied"] == pytest.approx(P.stt_sell_pct * 2, rel=0.01)
    assert b["exchange"]["param"] == "FNO_EXCH_TXN_PCT_NSE" and b["exchange"]["implied"] == pytest.approx(P.exch_txn_pct_nse * 2, rel=0.01)
    assert b["stamp"]["implied"] == pytest.approx(P.stamp_buy_pct * 2, rel=0.1)           # a 0.20 paid on 0.195 is rounding noise: the implied rate is approximate


def test_sensex_names_the_bse_rate():
    pr = make_probe(und="SENSEX", qty=20)
    n = note_of(pr)
    n["exchange"] += 3.0
    assert by(probecheck.compare(pr, n, P))["exchange"]["param"] == "FNO_EXCH_TXN_PCT_BSE"


def test_missing_lines_are_incomplete_never_matched():
    pr = make_probe()
    n = note_of(pr)
    del n["gst"], n["sebi"]
    c = probecheck.compare(pr, n, P)
    assert c["verdict"] == "INCOMPLETE" and "GST" in c["text"] and "SEBI fee" in c["text"] and "still missing" in c["text"]


def test_no_note_and_blank_values():
    pr = make_probe()
    assert probecheck.compare(pr, {}, P)["verdict"] == "NO_NOTE"
    assert probecheck.compare(pr, {"brokerage": "", "stt": None}, P)["verdict"] == "NO_NOTE"
    assert probecheck.compare(pr, {"brokerage": "Rs 40.00"}, P)["lines"][0]["note"] == 40.0       # pasted text with Rs and commas is read


def test_total_is_checked_when_given():
    pr = make_probe()
    n = note_of(pr)
    n["total"] = round(sum(n.values()) + 5, 2)
    c = probecheck.compare(pr, n, P)
    assert c["verdict"] == "DIFFERS" and c["total"]["ok"] is False and c["total"]["diff"] == pytest.approx(5.0, abs=0.1)


def test_an_extra_sell_order_adds_one_flat_brokerage_and_its_gst():
    pr = make_probe(extra_sell=True)
    e = probecheck.expected_lines(pr, P)
    assert e["orders"] == 3 and e["extra_sells"] == 1
    assert e["lines"]["brokerage"] == pytest.approx(pr["pnl"]["charges_detail"]["brokerage"] + 20.0)
    assert e["lines"]["gst"] == pytest.approx(pr["pnl"]["charges_detail"]["gst"] + 3.6)
    n = note_of(pr, brokerage=round(e["lines"]["brokerage"], 2), gst=round(e["lines"]["gst"], 2))
    assert probecheck.compare(pr, n, P)["verdict"] == "MATCHED"


@pytest.mark.parametrize("probe", [{"live": False, "orders": []}, {"live": True, "exit_code": 2, "orders": []}, {}, {"pnl": {"charges_detail": "x"}}])
def test_no_round_trip_means_not_comparable(probe):
    assert probecheck.compare(probe, {"stt": 1}, P)["verdict"] == "NOT_COMPARABLE"


# ── reading the files ──
def test_list_load_and_the_name_check(tmp_path):
    d = str(tmp_path)
    for stamp in ("20261111_101500", "20261112_093500"):
        json.dump(make_probe(), open(os.path.join(d, f"probe_{stamp}.json"), "w"))
    open(os.path.join(d, "probe_20261112_093500.txt"), "w").write("x")
    open(os.path.join(d, "notes.json"), "w").write("{}")
    assert probecheck.list_files(d) == ["probe_20261112_093500.json", "probe_20261111_101500.json"]
    assert probecheck.load(d)[0] == "probe_20261112_093500.json"
    assert probecheck.load(d, "probe_20261111_101500.json")[1]["live"] is True
    for bad in ("../x.json", "notes.json", "probe_1.json", "/etc/passwd"):
        assert probecheck.load(d, bad) == (None, None)
    assert probecheck.list_files(str(tmp_path / "nope")) == [] and probecheck.load(str(tmp_path / "nope")) == (None, None)
    open(os.path.join(d, "probe_20261113_093500.json"), "w").write("{not json")
    assert probecheck.load(d) == (None, None)


def test_summarize_explains_a_rejected_buy_in_plain_words():
    pr = {"live": True, "underlying": "NIFTY", "side": "CE", "exit_code": 2, "orders": [{"side": "BUY", "price": 100, "qty": 65, "ok": False, "status": "REJECTED", "message": "DH-906 Trader Control: Derivatives is off"}]}
    s = probecheck.summarize(pr, "probe_20261111_101500.json")
    assert "refused" in s["outcome"] and "Trader Control" in s["dhan_hint"] and s["orders"][0]["hint"] and s["pnl"] is None and s["still_open"] is False


def test_summarize_flags_an_open_position_and_a_dry_run():
    assert probecheck.summarize({"live": True, "exit_code": 3, "orders": []})["still_open"] is True
    assert probecheck.summarize({"live": False, "orders": []})["outcome"].startswith("dry run")
    assert probecheck.summarize({"live": True, "exit_code": 99})["outcome"] == "unknown outcome"
    assert probecheck.summarize({"orders": [None, 5]})["orders"] == []


def test_summary_text_and_file(tmp_path):
    pr = make_probe()
    n = note_of(pr)
    n["stt"] += 2
    c = probecheck.compare(pr, n, P)
    s = probecheck.summarize(pr, "probe_20261111_101500.json")
    t = probecheck.summary_text(s, c)
    assert "charges vs contract note: DIFFERS" in t and "FNO_STT_SELL_PCT" in t and "NIFTY 24500 CE" in t
    path = probecheck.write_summary(str(tmp_path), "probe_20261111_101500.json", s, c)
    assert path.endswith("probe_20261111_101500_summary.txt") and open(path).read() == t


# ── routes ──
@pytest.fixture()
def client(tmp_path, monkeypatch):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FNO_RUNNER", "0")
    import main
    d = tmp_path / "fno_real_probe"
    d.mkdir()
    monkeypatch.setattr(probecheck, "PROBE_DIR", str(tmp_path / "missing"))             # the container path does not exist here: the route falls back to DATA_DIR/fno_real_probe
    main.app.dependency_overrides[main.require_admin] = lambda: {"sub": "tester", "admin": True}
    monkeypatch.setattr(main, "store", Store(":memory:"))
    yield TestClient(main.app), main, d
    main.app.dependency_overrides.clear()


def test_probe_route_with_no_probe_says_so_and_never_errors(client):
    c, main, d = client
    r = c.get("/fno/probe").json()
    assert r["probe"] is None and r["files"] == [] and r["comparison"] is None


def test_probe_note_is_saved_compared_logged_and_per_file(client):
    c, main, d = client
    pr = make_probe()
    json.dump(pr, open(d / "probe_20261111_101500.json", "w"))
    r = c.get("/fno/probe").json()
    assert r["probe"]["file"] == "probe_20261111_101500.json" and r["comparison"]["verdict"] == "NO_NOTE"
    r = c.post("/fno/probe/note", json=note_of(pr)).json()
    assert r["comparison"]["verdict"] == "MATCHED" and r["note"]["lines"]["stt"] == note_of(pr)["stt"]
    assert any(e.get("action") == "probe_note" and e.get("file") == "probe_20261111_101500.json" for e in main.store.events("manual"))
    assert c.get("/fno/probe").json()["comparison"]["verdict"] == "MATCHED"                  # kept
    bad = c.post("/fno/probe/note", json={**note_of(pr), "stt": note_of(pr)["stt"] + 3}).json()
    assert bad["comparison"]["verdict"] == "DIFFERS"


def test_probe_note_rejects_bad_file_names_negative_numbers_and_a_missing_probe(client):
    c, main, d = client
    assert c.post("/fno/probe/note", json={"stt": 1}).status_code == 404
    json.dump(make_probe(), open(d / "probe_20261111_101500.json", "w"))
    assert c.post("/fno/probe/note", json={"file": "../x.json", "stt": 1}).status_code == 400
    assert c.post("/fno/probe/note", json={"stt": -1}).status_code == 400
    assert c.get("/fno/probe?file=../../etc/passwd").status_code == 400


def test_probe_note_does_not_confirm_the_readiness_item(client):
    c, main, d = client
    pr = make_probe()
    json.dump(pr, open(d / "probe_20261111_101500.json", "w"))
    c.post("/fno/probe/note", json=note_of(pr))
    items = {i["key"]: i for i in c.get("/fno/readiness").json()["items"]}
    assert items["probe_charges"]["ok"] is None                                           # still your click


# ── the script ──
def _script():
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location("fno_charges_compare", os.path.join(here, "..", "..", "..", "scripts", "fno_charges_compare.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_script_exit_codes_and_summary_file(tmp_path, capsys):
    m = _script()
    assert m.main(["--dir", str(tmp_path)]) == 2                                          # no probe yet
    pr = make_probe()
    json.dump(pr, open(tmp_path / "probe_20261111_101500.json", "w"))
    assert m.main(["--dir", str(tmp_path)]) == 3                                          # summary only: no note
    assert (tmp_path / "probe_20261111_101500_summary.txt").exists()
    args = ["--dir", str(tmp_path)] + [x for k, v in note_of(pr).items() for x in (f"--{k}", str(v))]
    assert m.main(args) == 0
    args[args.index("--stt") + 1] = str(note_of(pr)["stt"] + 5)
    assert m.main(args) == 1
    assert "FNO_STT_SELL_PCT" in (tmp_path / "probe_20261111_101500_summary.txt").read_text()
