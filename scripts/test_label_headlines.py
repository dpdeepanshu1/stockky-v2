import csv

import label_headlines as L


def test_from_text_dedups_and_predicts(tmp_path):
    f = tmp_path / "h.txt"
    f.write_text("Wipro Q2 profit jumps 30%\nwipro q2 profit jumps 30%\n\nSensex ends higher\n", encoding="utf-8")
    out = tmp_path / "o.csv"
    assert L.from_text(str(f), str(out)) == 2
    rows = list(csv.DictReader(open(out, encoding="utf-8")))
    assert rows[0]["pred_pol"] == "1" and rows[0]["gold_pol"] == ""


def test_collect_uses_fetch_and_skips_failures(tmp_path):
    def fetch(url):
        if url.endswith("/BAD"):
            raise OSError("down")
        return {"headlines": [{"title": "Acme cuts FY27 revenue guidance"}, {"title": ""}]}
    out = tmp_path / "o.csv"
    assert L.collect("http://x/", ["ACME", "BAD"], str(out), fetch=fetch) == 1
    assert list(csv.DictReader(open(out, encoding="utf-8")))[0]["symbol"] == "ACME"


def test_score_only_labelled_rows(tmp_path):
    p = tmp_path / "s.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=L.FIELDS); w.writeheader()
        w.writerow({"headline": "Wipro Q2 profit jumps 30%", "gold_pol": "1", "gold_type": "results"})
        w.writerow({"headline": "Infosys cuts FY27 revenue guidance", "gold_pol": "1", "gold_type": ""})   # wrong on purpose
        w.writerow({"headline": "unlabelled row", "gold_pol": ""})
    r = L.score(str(p))
    assert r["labelled"] == 2 and r["pol_acc"] == 0.5 and r["type_labelled"] == 1 and r["type_acc"] == 1.0
    assert len(r["wrong_pol"]) == 1


def test_main_commands(tmp_path, capsys):
    f = tmp_path / "h.txt"; f.write_text("Wipro Q2 profit jumps 30%\n", encoding="utf-8")
    o = tmp_path / "o.csv"
    assert L.main(["from-text", str(f), "-o", str(o)]) == 0
    assert L.main(["score", str(o)]) == 0
    assert "labelled" in capsys.readouterr().out
