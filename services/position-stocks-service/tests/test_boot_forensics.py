import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import boot_forensics as bf


def test_classification(tmp_path, monkeypatch):
    p = tmp_path / "state.json"
    monkeypatch.setattr(bf, "_STATE_PATH", str(p))
    monkeypatch.setattr(bf, "_install_signal_logging", lambda s: None)
    monkeypatch.setattr(bf.threading, "Thread", lambda *a, **k: type("T", (), {"start": lambda s: None})())
    assert bf.record_boot("svc")["cause"] == "FRESH_CONTAINER"
    assert bf.record_boot("svc")["cause"] == "DIED_WITHOUT_CLEAN_SHUTDOWN"     # no clean flag -> SIGKILL/OOM signature
    bf.mark_clean_shutdown()
    assert bf.record_boot("svc")["cause"] == "RESTART_AFTER_CLEAN_SHUTDOWN"
    st = json.loads(p.read_text()); st["signal"] = "SIGTERM"; p.write_text(json.dumps(st))
    assert bf.record_boot("svc")["cause"] == "SIGTERM_BUT_NOT_CLEAN"
    p.write_text("{not json")
    assert bf.record_boot("svc")["cause"] == "UNKNOWN"


def test_valid_json_of_wrong_shape_does_not_raise_and_self_heals(tmp_path, monkeypatch):
    """Regression: a previous-state file that is valid JSON but the wrong shape
    (list, non-numeric timestamps, ...) used to make record_boot raise before the
    bad file was replaced, disabling forensics on every later boot."""
    p = tmp_path / "state.json"
    monkeypatch.setattr(bf, "_STATE_PATH", str(p))
    monkeypatch.setattr(bf, "_install_signal_logging", lambda s: None)
    monkeypatch.setattr(bf.threading, "Thread", lambda *a, **k: type("T", (), {"start": lambda s: None})())
    for bad in ([], "a string", {"last_heartbeat": "abc", "started_at": 1.0}):
        p.write_text(json.dumps(bad))
        assert bf.record_boot("svc")["cause"] == "UNKNOWN"
        assert bf.record_boot("svc")["cause"] == "DIED_WITHOUT_CLEAN_SHUTDOWN"  # healed
