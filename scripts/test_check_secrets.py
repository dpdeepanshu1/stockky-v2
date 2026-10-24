"""F32: scripts/check_secrets.py finds secrets, ignores placeholders / code identifiers, never prints a value, honours the allowlist."""
import os, sys, tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_secrets as S

JWT = "eyJhbGciOiJIUzI1NiJ9." + "eyJzdWIiOiJhZG1pbiIsInJvbGUiOiJhZG1pbiJ9" + "." + "k3Jx9aQ2LmN7pR4tVwYz8BcD1eFgHiJk"
UPSTASH = "AXlkAAIjcDE0YzA1ZjJhOWRiNzQ0MGM4YjZlM2JkZjFhNzIyMDhmZHAxMA"
TG = "123456789:" + "AAH" + "x" * 3 + "Q7rT2mN8vB4cK1dL6fG9hJ3sW5yZ0pXaE"[:32]


def _rules(text, rel="a.env"):
    return {f.rule for f in S.scan_text(text, rel)}


def test_jwt_and_private_key_and_aws_and_url_password_are_found():
    assert "jwt" in _rules(f"Authorization: Bearer {JWT}", "output.txt")
    # built at run time so this file itself holds no literal that looks like a secret (test_the_repo_itself_is_clean)
    assert "private-key" in _rules("-----BEGIN RSA " + "PRIVATE KEY-----", "k.txt")
    assert "aws-key" in _rules("AKIA" + "Q7RT2MN8VB4XK9LP", "x.md")
    assert "url-password" in _rules("DATABASE_URL=postgresql:" + "//app:Zq8vK2mTn4xR@db.internal:5432/stockky")
    assert "url-password" in _rules("REDIS=rediss:" + "//default:Zq8vK2mTn4xRb7@us1-x.upstash.io:6379")


def test_upstash_style_token_assigned_to_a_secret_name_is_found():
    assert "assigned-secret" in _rules(f"UPSTASH_REDIS_REST_TOKEN={UPSTASH}")
    assert "assigned-secret" in _rules(f'SESSION_SECRET: "{UPSTASH}"', "c.yml")


def test_placeholders_and_short_or_identifier_values_are_ignored():
    for line in ["SESSION_SECRET=changeme-please-generate-one", "UPSTASH_TOKEN=<your-token-here>", "API_KEY=${API_KEY}", "ADMIN_PASSWORD=",
                 "TOKEN=xxxxxxxxxxxxxxxxxxxx", "SECRET_KEY=your-secret-key-goes-here", "DB=postgresql://user:password@localhost/db",
                 "DB=postgresql://user:${DB_PASSWORD}@localhost/db", "scrip_master_token=self.scrip_master_token_value"]:
        assert _rules(line) == set(), line
    assert _rules("SHORT_TOKEN=abc123") == set()


def test_code_files_skip_the_assignment_rule_but_not_the_jwt_rule():
    assert _rules(f'SESSION_SECRET = "{UPSTASH}"', "svc/x.py") == set()
    assert "jwt" in _rules(f'T = "{JWT}"', "svc/x.py")


def test_findings_never_contain_the_value_and_fingerprint_is_stable():
    f = S.scan_text(f"UPSTASH_REDIS_REST_TOKEN={UPSTASH}", "a.env")[0]
    assert UPSTASH not in f.render() and UPSTASH[4:] not in f.render() and f.first4 == UPSTASH[:4] and f.length == len(UPSTASH)
    assert f.fp == S.fingerprint(UPSTASH) == S.scan_text(f"UPSTASH_REDIS_REST_TOKEN={UPSTASH}", "b.env")[0].fp


def test_allowlist_suppresses_only_that_fingerprint():
    fp = S.fingerprint(UPSTASH)
    assert S.scan_text(f"X_TOKEN={UPSTASH}", "a.env", {fp}) == []
    assert S.scan_text(f"X_TOKEN={UPSTASH}", "a.env", {"000000000000"}) != []


def test_forbidden_file_names():
    for n in ["ewallet.p12", "cwallet.sso", "server.pem", "x.key", ".env", ".env.prod", "run_output_1.txt", "a.token", "me.jwt"]:
        assert S.forbidden_file(Path(n)), n
    for n in [".env.example", ".env.oracle.example", ".env.oracle.recommended", "main.py", ".gitkeep"]:
        assert not S.forbidden_file(Path(n)), n


def test_scan_paths_walks_a_folder_skips_vendored_dirs_and_flags_forbidden_files():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "ok.md").write_text("nothing here\n")
        (root / "node_modules").mkdir()
        (root / "node_modules" / "leak.txt").write_text(f"Bearer {JWT}\n")
        (root / "run1").mkdir()
        (root / "run1" / "output.txt").write_text(f"token {JWT}\n")
        (root / ".env").write_text("A=b\n")
        got = S.scan_paths([root], allow=set())
        rules = sorted((Path(f.path).name, f.rule) for f in got)
        assert rules == [(".env", "forbidden-file"), ("output.txt", "jwt")], rules


def _tracked_files():
    """E128: in a git checkout, the files git tracks (what a push would publish); None when there is no git or no .git (a zip): then the whole tree is scanned."""
    import shutil
    import subprocess
    if not (S.ROOT / ".git").exists() or not shutil.which("git"):
        return None
    r = subprocess.run(["git", "-C", str(S.ROOT), "ls-files", "-z"], capture_output=True, timeout=60)
    if r.returncode != 0:
        return None
    return [S.ROOT / n for n in r.stdout.decode("utf-8", "ignore").split("\0") if n and (S.ROOT / n).is_file()]


def test_the_repo_itself_is_clean():
    # The VM working tree also holds git-ignored local files by design (.env, oracle_wallet, data/, downloaded run outputs), which this test used to fail on.
    # So it checks what is TRACKED; `python3 scripts/check_secrets.py` still scans the whole folder and is the one to run before zipping or sharing a folder.
    tracked = _tracked_files()
    assert S.scan_paths(tracked if tracked is not None else [S.ROOT]) == []


def test_tracked_file_list_is_used_only_inside_a_git_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "ROOT", tmp_path)
    assert _tracked_files() is None
