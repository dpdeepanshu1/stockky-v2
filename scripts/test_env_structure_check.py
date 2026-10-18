import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import env_structure_check as e


def test_parse_env_last_wins_quotes_comments_export():
    env = e.parse_env("# c\nA=1\nexport STRUCTURE_EXIT_MODE='exit'  \nSTRUCTURE_MAX_AGE_DAYS=7 # old\nSTRUCTURE_MAX_AGE_DAYS=5\n")
    assert env["STRUCTURE_EXIT_MODE"] == "exit" and env["STRUCTURE_MAX_AGE_DAYS"] == "5" and env["A"] == "1"


def test_defaults_produce_no_findings():
    _, f = e.check({"STRUCTURE_POLICY_MODE": "record", "STRUCTURE_EXIT_MODE": "LOG", "STRUCTURE_MAX_AGE_DAYS": "5", "OTHER": "x"})
    assert f == []


def test_old_overrides_are_flagged_with_direction():
    env = e.parse_env("STRUCTURE_EXIT_MODE=exit\nSTRUCTURE_STOP_MODE=on\nSTRUCTURE_PREPARE_UPGRADE=1\nSTRUCTURE_MAX_AGE_DAYS=7\nSTRUCTURE_POLICY_MODE=full\nCANDIDATE_52W_LOG_ONLY=1\n")
    _, f = e.check(env)
    text = "\n".join(f)
    assert "TIGHTENS  STRUCTURE_EXIT_MODE=exit" in text and "TIGHTENS  STRUCTURE_STOP_MODE=on" in text
    assert "LOOSENS  STRUCTURE_PREPARE_UPGRADE=1" in text and "OVERRIDE  STRUCTURE_MAX_AGE_DAYS=7" in text
    assert "TIGHTENS  STRUCTURE_POLICY_MODE=full" in text and "LOOSENS  CANDIDATE_52W_LOG_ONLY=1" in text


def test_secret_like_names_are_hidden_and_unknown_keys_are_info():
    listing, f = e.check({"STRUCTURE_API_KEY": "abc123", "STRUCTURE_NEW_THING": "z"})
    assert "abc123" not in "\n".join(listing + f) and any(x.startswith("INFO  STRUCTURE_NEW_THING") for x in f)


def test_cli_exit_codes(tmp_path):
    p = tmp_path / ".env"
    p.write_text("STRUCTURE_STOP_MODE=on\n")
    assert e.main([str(p)]) == 0 and e.main([str(p), "--strict"]) == 1
    assert e.main([str(tmp_path / "missing")]) == 2
