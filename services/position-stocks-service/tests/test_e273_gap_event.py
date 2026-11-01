"""E273 round 11: event-aware gap rule for long entries (orders/gap_event.py). Pure helpers + wiring checked on the source text (attempt_entry needs SQLAlchemy)."""
import pathlib
import subprocess
import sys
from types import SimpleNamespace as NS

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import config  # noqa: E402
from orders import gap_event  # noqa: E402


def _cfg_value(expr, **env_over):
    import os
    env = {k: v for k, v in os.environ.items() if not k.startswith("GAP_EVENT")}
    env.update(env_over)
    code = "import sys; sys.path.insert(0,'.'); import config; print('V'+str(" + expr + "))"
    r = subprocess.run([sys.executable, "-I", "-c", code], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=60)
    return next(x for x in r.stdout.splitlines() if x.startswith("V"))[1:]


def test_config_defaults_and_clamp():
    assert _cfg_value("config.GAP_EVENT_RULE_ENABLED") == "True" and _cfg_value("config.GAP_EVENT_MIN_STRENGTH") == "0.3"
    assert _cfg_value("config.GAP_EVENT_RULE_ENABLED", GAP_EVENT_RULE_ENABLED="0") == "False"
    assert _cfg_value("config.GAP_EVENT_MIN_STRENGTH", GAP_EVENT_MIN_STRENGTH="5") == "1.0"


def test_positive_event_needs_polarity_plus_one_and_strength(monkeypatch):
    monkeypatch.setattr(config, "GAP_EVENT_RULE_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "GAP_EVENT_MIN_STRENGTH", 0.3, raising=False)
    assert gap_event.positive_event(NS(event_polarity=1, event_strength=0.5))
    assert gap_event.positive_event(NS(event_polarity=1, event_strength=0.3))
    assert not gap_event.positive_event(NS(event_polarity=1, event_strength=0.29))      # too weak / decayed
    assert not gap_event.positive_event(NS(event_polarity=-1, event_strength=0.9))      # negative never explains a gap
    assert not gap_event.positive_event(NS(event_polarity=None, event_strength=0.9))    # unknown polarity never counts
    assert not gap_event.positive_event(NS(event_polarity=1, event_strength=None))
    assert not gap_event.positive_event(None) and not gap_event.positive_event(NS())
    monkeypatch.setattr(config, "GAP_EVENT_RULE_ENABLED", False, raising=False)
    assert not gap_event.positive_event(NS(event_polarity=1, event_strength=0.9))       # switch off = the old handling


def test_gap_kind_reads_both_gates():
    assert gap_event.gap_kind("OPENING_GATE:GAP_UP:+4.00% > +3.00%") == "up"
    assert gap_event.gap_kind("PREOPEN_GAP_UP:indicative open +7.0% ...") == "up"
    assert gap_event.gap_kind("OPENING_GATE:GAP_DOWN:-3.00% < -2.00%") == "down"
    assert gap_event.gap_kind("PREOPEN_GAP_DOWN:indicative open -4.0% ...") == "down"
    assert gap_event.gap_kind("OPENING_GATE:BELOW_OPEN:ltp") is None and gap_event.gap_kind(None) is None


def test_against_reason_names_the_event():
    r = gap_event.against_reason("PREOPEN_GAP_DOWN:x", NS(event_type="results", event_strength=0.62))
    assert r.startswith("GAP_AGAINST_EVENT:positive results (strength 0.62)") and "PREOPEN_GAP_DOWN:x" in r


def test_very_large_gap_keeps_a_half_size_penalty(monkeypatch):
    monkeypatch.setattr(config, "PREOPEN_GAP_UP_BLOCK_PCT", 6.0, raising=False)
    assert gap_event.large_gap_penalty(6.0) == 1.0 and gap_event.large_gap_penalty(9.0) == 1.0
    assert gap_event.large_gap_penalty(5.9) is None and gap_event.large_gap_penalty(None) is None and gap_event.large_gap_penalty(-8) is None
    monkeypatch.setattr(config, "PREOPEN_GAP_UP_BLOCK_PCT", 0.0, raising=False)
    assert gap_event.large_gap_penalty(9.0) is None


def test_waived_gap_up_needs_s1():
    why = "OPENING_GATE:GAP_UP:+4.00% > +3.00%"
    assert gap_event.unconfirmed_reason([], "S2_RS_BREAKOUT") is None                  # nothing waived: nothing to confirm
    assert gap_event.unconfirmed_reason([why], "S1_CATALYST_UP") is None
    for sid in (None, "S2_RS_BREAKOUT", "S3_EXHAUSTION"):
        r = gap_event.unconfirmed_reason([why], sid)
        assert r.startswith("GAP_UP_NOT_CONFIRMED:") and why in r


def test_entry_wiring():
    src = (ROOT / "orders" / "entry.py").read_text(encoding="utf-8")
    assert "_gap_waived: list = []" in src and "_pos_event = gap_event.positive_event(quality)" in src
    # waivers (opening gate, pre-open penalty) and the large-gap penalty come before the setup engine; the S1 confirmation right after it, before NO_SETUP
    i = src.index
    assert i("_gap_waived: list = []") < i("gap_event.gap_kind(_pg[") < i("gap_event.large_gap_penalty(preopen.gap_of(candidate.symbol))") < i("setup_live.classify_long(")
    assert i("setup_live.classify_long(") < i("gap_event.unconfirmed_reason(_gap_waived") < i("setup_live.no_setup_refusal(")
    assert '"GAP_AGAINST_EVENT", "GAP_UP_NOT_CONFIRMED"' in src                           # per-symbol skips
    assert src.count("gap_event.against_reason(") == 3                                    # opening gate, pre-open penalty, veto-mode pre-open


def test_catalog_has_both_flags():
    cat = (ROOT / "control" / "catalog.py").read_text(encoding="utf-8")
    assert 'shipped="true"' in cat.split('Flag("GAP_EVENT_RULE_ENABLED"', 1)[1].split("Flag(", 1)[0]
    assert 'shipped="0.3"' in cat.split('Flag("GAP_EVENT_MIN_STRENGTH"', 1)[1].split("Flag(", 1)[0]
