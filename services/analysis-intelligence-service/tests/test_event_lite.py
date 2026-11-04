"""E285 round 172: event_lite (offline headline classifier) - accuracy gates, copy sync, and the three places that use it."""
import importlib
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))
import event_lite as el  # noqa: E402
import event_lite_data as D  # noqa: E402


def _rows(rows):
    for r in rows:
        if len(r) == 5:
            yield r[0], r[1], r[2], r[3]
        else:
            yield r[0], (), r[1], r[2]


@pytest.mark.parametrize("name,min_pol,min_type", [
    ("DEV", 0.99, 0.99), ("HOLDOUT", 0.97, 0.95), ("HOLDOUT2", 0.97, 0.97), ("HOLD", 0.95, 0.97),
    ("HOLDOUT_R171", 0.97, 0.97), ("BLIND_R172", 0.97, 0.97), ("ALERT_R177", 0.99, 0.95), ("REAL_R178", 0.95, 0.80),
])
def test_accuracy_gates(name, min_pol, min_type):
    n = ok = tc = tt = 0
    for h, al, t, p in _rows(getattr(D, name)):
        r = el.classify(h, al)
        n += 1
        ok += r["pol"] == p
        if t:
            tc += 1
            tt += r["type"] == t
    assert ok / n >= min_pol, (name, ok, n)
    assert tt / max(tc, 1) >= min_type, (name, tt, tc)


def test_copies_are_identical():
    master = (ROOT / "event_lite.py").read_bytes()
    assert (ROOT / "event" / "event_lite.py").read_bytes() == master
    assert (ROOT / "news" / "event_lite.py").read_bytes() == master


def test_never_raises_and_shapes():
    for bad in (None, "", " ", 123, "x" * 5000, "\u20b9\u2019\x00"):
        r = el.classify(bad)
        assert set(r) == {"type", "pol", "strength", "about_company", "is_new_info", "confidence", "reason"}
        assert r["type"] in el.TYPES and r["pol"] in (-1, 0, 1)
        assert -0.8 <= el.sentiment(bad) <= 0.8


def test_sentiment_scale_and_min_conf(monkeypatch):
    assert el.sentiment("Infosys cuts FY27 revenue guidance, shares slide") < -0.4
    assert el.sentiment("Wipro Q2 profit jumps 30%") > 0.4
    assert el.sentiment("Sensex ends 300 points higher") == 0.0
    monkeypatch.setenv("EVENT_LITE_MIN_CONF", "0.99")
    assert el.sentiment("Wipro Q2 profit jumps 30%") == 0.0
    monkeypatch.setenv("EVENT_LITE_MIN_CONF", "junk")
    assert el.sentiment("Wipro Q2 profit jumps 30%") > 0.4


def test_event_category_none_for_other():
    assert el.event_category("Acme shares open flat") is None
    assert el.event_category("Segment update") is None          # the keyword classifier called this a board meeting ('egm' in 'segment')
    assert el.event_category("Amount raised") is None           # ... and this a guidance ('mou' in 'amount')
    assert el.event_category("Acme declares interim dividend") == "dividend"


def test_stats_and_alias_gate():
    el.reset_stats()
    el.classify("Wipro Q2 profit jumps 30%")
    el.classify("")
    assert el.stats()["calls"] >= 1
    r = el.classify("TCS Q2 profit jumps 30%", ["Infosys"])       # about another company: confidence halves
    assert r["about_company"] is False and r["confidence"] < 0.5


# ── event/main.py ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def _load(subdir, modname):
    """Import a sub-app module under a private name so the module cache the other test files rely on is left untouched."""
    import importlib.util
    path = ROOT / subdir / (modname + ".py")
    saved = {n: sys.modules.get(n) for n in ("main", "event_lite", "news_quality", "feed_fetch")}
    sys.path.insert(0, str(ROOT / subdir))
    for n in saved:
        sys.modules.pop(n, None)
    try:
        spec = importlib.util.spec_from_file_location("r172_" + subdir + "_" + modname, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(ROOT / subdir))
        for n, m in saved.items():
            sys.modules.pop(n, None)
            if m is not None:
                sys.modules[n] = m


def test_event_service_modes(monkeypatch):
    em = _load("event", "main")
    monkeypatch.delenv("EVENT_CLASSIFIER_MODE", raising=False)
    assert em._classifier_mode() == "lite"
    assert em._classify_event_title("Segment update") is None
    assert em._classify_event_title("Acme Q3 profit jumps 20%") == "results"
    monkeypatch.setenv("EVENT_CLASSIFIER_MODE", "keyword")
    assert em._classify_event_title("Segment update") == "board_meeting"       # the old substring quirk, kept for this mode
    assert em._event_reading("Acme Q3 profit jumps 20%") == {}
    monkeypatch.setenv("EVENT_CLASSIFIER_MODE", "both")
    assert em._classify_event_title("Acme shares open flat") is None
    assert em._classify_event_title("Acme to launch ebitda call") is not None   # lite says 'other'? the keyword groups decide
    monkeypatch.setenv("EVENT_CLASSIFIER_MODE", "nonsense")
    assert em._classifier_mode() == "lite"
    rd = em._event_reading("Acme Q3 profit jumps 20%")
    assert rd["event_polarity"] == 1 and rd["event_confidence"] >= 0.5
    assert em._event_reading("Sensex ends higher") == {}


def test_event_service_without_lite(monkeypatch):
    em = _load("event", "main")
    monkeypatch.setattr(em, "_event_lite", None)
    assert em._classify_event_title("Acme board meeting on Friday") == "board_meeting"
    assert em._event_reading("x") == {}


def test_event_summary_new_types(monkeypatch):
    em = _load("event", "main")
    ev = {"symbol": "ACME.NS", "recent_news": [{"title": "SEBI imposes Rs 25 lakh penalty on Acme"}, {"title": "Morgan Stanley upgrades Acme to overweight"}]}
    out = em._summarize_events(ev)
    assert "Legal:" in out and "Rating:" in out


def test_event_service_error_in_lite_is_swallowed(monkeypatch):
    em = _load("event", "main")

    class Boom:
        @staticmethod
        def classify(*a, **k):
            raise RuntimeError("x")

        @staticmethod
        def event_category(*a, **k):
            return None
    monkeypatch.setattr(em, "_event_lite", Boom)
    assert em._event_reading("anything") == {}


# ── news/main.py ────────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_news_modes(monkeypatch):
    nm = _load("news", "main")
    monkeypatch.delenv("NEWS_SENTIMENT_MODE", raising=False)
    assert nm._sentiment_mode() == "auto"
    calls = []
    monkeypatch.setattr(nm, "_score_headline_hf", lambda t: calls.append(t) or None)     # HF has no answer
    assert nm._score_headline("Wipro Q2 profit jumps 30%") > 0.4                       # auto: lite takes over
    assert calls
    assert nm._sentiment_source() in ("lite", "none")
    monkeypatch.setattr(nm, "_score_headline_hf", lambda t: 0.0)                       # HF answered neutral: that stands
    assert nm._score_headline("Wipro Q2 profit jumps 30%") == 0.0
    monkeypatch.setenv("NEWS_SENTIMENT_MODE", "hf")
    monkeypatch.setattr(nm, "_score_headline_hf", lambda t: None)
    assert nm._score_headline("Wipro Q2 profit jumps 30%") == 0.0                      # old behaviour
    assert nm._sentiment_source() == "none"
    monkeypatch.setenv("NEWS_SENTIMENT_MODE", "lite")
    monkeypatch.setattr(nm, "_score_headline_hf", lambda t: pytest.fail("HF must not be called in lite mode"))
    assert nm._score_headline("Infosys cuts FY27 revenue guidance") < -0.4
    assert nm._sentiment_source() == "lite"


def test_news_no_lite_available(monkeypatch):
    nm = _load("news", "main")
    monkeypatch.setattr(nm, "_event_lite", None)
    monkeypatch.setattr(nm, "_score_headline_hf", lambda t: None)
    monkeypatch.setenv("NEWS_SENTIMENT_MODE", "auto")
    assert nm._score_headline("Wipro Q2 profit jumps 30%") == 0.0
    assert nm._sentiment_source() == "none"
    monkeypatch.setenv("NEWS_SENTIMENT_MODE", "lite")
    assert nm._sentiment_source() == "none"


def test_news_hf_failure_returns_none_not_neutral(monkeypatch):
    nm = _load("news", "main")
    monkeypatch.setattr(nm, "HF_API_KEY", None)
    assert nm._score_headline_hf("anything") is None
    monkeypatch.setattr(nm, "HF_API_KEY", "k")
    monkeypatch.setattr(nm, "_HF_BACKOFF_UNTIL", 10 ** 12)
    assert nm._score_headline_hf("anything") is None


# ── news_quality.py ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_news_quality_sentiment(monkeypatch):
    nq = _load("news", "news_quality")
    monkeypatch.delenv("NEWS_SENTIMENT_MODE", raising=False)
    assert nq._lite_verdict("Infosys cuts FY27 revenue guidance") == -1
    assert nq._lite_verdict("Sensex ends higher") is None              # market wrap: no opinion
    assert nq._keyword_verdict("Acme wins order") == 1 and nq._keyword_verdict("Acme probe") == -1 and nq._keyword_verdict("Acme news") == 0
    monkeypatch.setenv("NEWS_SENTIMENT_MODE", "hf")
    assert nq._lite_verdict("Infosys cuts FY27 revenue guidance") is None
    monkeypatch.setenv("NEWS_SENTIMENT_MODE", "auto")
    monkeypatch.setenv("EVENT_LITE_MIN_CONF", "x")
    assert nq._lite_verdict("Infosys cuts FY27 revenue guidance") == -1


def test_news_quality_build_response(monkeypatch):
    nq = _load("news", "news_quality")
    items = [{"title": "Acme Q2 profit jumps 30%"}, {"title": "Acme cuts FY27 revenue guidance"}, {"title": "Acme profit falls, shares slide"}]
    monkeypatch.setattr(nq, "fetch_multi_source", lambda sym, name, stats=None: items)
    monkeypatch.setattr(nq, "summarize_headlines", lambda *a, **k: "s")
    monkeypatch.delenv("NEWS_SENTIMENT_MODE", raising=False)
    r = nq.build_news_response("ACME")
    assert r["sentiment_source"] == "lite+keywords"
    assert r["news_score"] < 50
    monkeypatch.setenv("NEWS_SENTIMENT_MODE", "hf")
    assert nq.build_news_response("ACME")["sentiment_source"] == "keywords"


def test_every_copy_behaves_like_the_master():
    """The event/ and news/ copies are what ships in those containers: run every labelled row through each copy and compare."""
    import importlib.util
    mods = []
    for sub in ("event", "news"):
        spec = importlib.util.spec_from_file_location("r172_copy_" + sub, ROOT / sub / "event_lite.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        mods.append(m)
    for name in ("DEV", "HOLDOUT", "HOLDOUT2", "HOLD", "HOLDOUT_R171", "BLIND_R172"):
        for h, al, _t, _p in _rows(getattr(D, name)):
            want = el.classify(h, al)
            for m in mods:
                assert m.classify(h, al) == want


_BRANCH_HEADLINES = [
    "Fund buys and sells shares of Acme in block deal", "Block deal in Acme shares",
    "Acme CFO to speak at investor conference", "Acme chairman interview on strategy",
    "ICRA retains negative outlook on Acme", "Kotak revises rating on Acme",
    "Acme dismisses report of SEBI probe", "Acme dividend", "Acme share buyback", "Acme buyback of shares approved",
    "Biocon receives final approval from USFDA for drug", "Acme order book update",
    "Acme Q2 revenue beats, profit misses", "Acme dividend record date", "Acme declares dividend",
    "Acme record date for split", "Acme to hold results and dividend meeting",
    "Jefferies turns more bearish on Acme", "Jefferies turns more bullish on Acme", "Brokerages retain buy on Acme", "Goldman Sachs turns less bearish on Acme, Sell stays",
    "Acme shares fail to lift on expansion", "Acme shares decline despite loan growth", "Acme faces DOJ investigation", "Senior executive of Acme departs", "No striking impact of RBI curbs on Acme",
    "Acme in talks to raise funds", "Kacholia Q2 portfolio: what's in, what's out", "Rupee settles higher against dollar", "Acme shows technical recovery signs",
]


def _copies():
    import importlib.util
    out = [el]
    for sub in ("event", "news"):
        spec = importlib.util.spec_from_file_location("r172_cov_" + sub, ROOT / sub / "event_lite.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        out.append(m)
    return out


def test_branches_error_paths_and_stats_in_every_copy(monkeypatch):
    for m in _copies():
        for h in _BRANCH_HEADLINES:
            r = m.classify(h)
            assert r["type"] in m.TYPES
            m.sentiment(h)
            m.event_category(h)
        assert m.classify("")["type"] == "none"
        assert m.classify("Acme profit up", ["", "  "])["type"] in m.TYPES
        monkeypatch.setenv("EVENT_LITE_MIN_CONF", "junk")
        assert m._min_conf() == 0.5
        monkeypatch.setenv("EVENT_LITE_MIN_CONF", "7")
        assert m._min_conf() == 0.5
        monkeypatch.delenv("EVENT_LITE_MIN_CONF")
        m.reset_stats()
        m.classify("Wipro Q2 profit jumps 30%")
        assert m.stats()["calls"] == 1
        monkeypatch.setattr(m, "_classify", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        assert m.classify("x")["reason"] == "error" and m.stats()["errors"] >= 1
        assert m.sentiment("x") == 0.0 and m.event_category("x") is None
        monkeypatch.undo()
        monkeypatch.setattr(m, "classify", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        assert m.sentiment("x") == 0.0 and m.event_category("x") is None
        monkeypatch.undo()


def test_mixed_beat_and_miss_is_neutral():
    for h in ("Revenue beats but profit misses estimates", "Acme Q2 revenue beats, profit misses"):
        assert el.classify(h)["pol"] == 0


def test_lite_stats_endpoints(monkeypatch):
    em = _load("event", "main")
    out = em.lite_stats()
    assert out["available"] is True and out["mode"] in ("lite", "keyword", "both") and "calls" in out["stats"]
    monkeypatch.setattr(em, "_event_lite", None)
    out2 = em.lite_stats()
    assert out2["available"] is False and out2["stats"] == {} and out2["mode"] == em._classifier_mode()
    nm = _load("news", "main")
    out = nm.lite_stats()
    assert out["available"] is True and out["headlines_scored_by_lite"] >= 0 and "sentiment_source" in out
    monkeypatch.setattr(nm, "_event_lite", None)
    assert nm.lite_stats()["available"] is False
