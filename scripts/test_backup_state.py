"""F33: scripts/backup_state.sh with a fake docker (volumes are plain folders). Plain functions: pytest runs them, `python3 scripts/test_backup_state.py` too."""
import os
import subprocess
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "backup_state.sh")

SHIM = r'''#!/usr/bin/env bash
FV="$FAKE_VOLS"
case "$1" in
  volume)
    case "$2" in
      inspect) [[ -d "$FV/$3" ]] ;;
      create) mkdir -p "$FV/$3" ;;
      rm) shift 2; for a in "$@"; do [[ "$a" == -f ]] || rm -rf "$FV/$a"; done ;;
    esac ;;
  run)
    shift; declare -A M; cmd=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --rm|-i) shift ;;
        -v) spec="$2"; shift 2; src="${spec%%:*}"; rest="${spec#*:}"; mp="${rest%%:*}"
            [[ "$src" == /* ]] && M[$mp]="$src" || M[$mp]="$FV/$src" ;;
        *) shift; [[ "$1" == sh ]] && { cmd="$3"; }; break ;;
      esac
    done
    for mp in "${!M[@]}"; do cmd="${cmd//$mp/${M[$mp]}}"; done
    sh -c "$cmd" ;;
esac
'''


def _setup(tmp, vols=None):
    fv = os.path.join(tmp, "vols")
    os.makedirs(fv)
    for name, files in (vols if vols is not None else {"proj_control-state-rt": {"overrides.json": "{}", "audit.jsonl": "x\n"}, "proj_runner-data": {"a/b.txt": "hello"}}).items():
        for rel, txt in files.items():
            p = os.path.join(fv, name, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").write(txt)
    shim = os.path.join(tmp, "docker")
    open(shim, "w").write(SHIM)
    os.chmod(shim, 0o755)
    cron = os.path.join(tmp, "cron.txt")
    open(cron, "w").write("")
    ct = os.path.join(tmp, "crontab")
    open(ct, "w").write(f'#!/usr/bin/env bash\nif [[ "$1" == "-l" ]]; then cat {cron}; else cat > {cron}; fi\n')
    os.chmod(ct, 0o755)
    env = dict(os.environ, DOCKER_BIN=shim, CRONTAB_BIN=ct, FAKE_VOLS=fv, BACKUP_DIR=os.path.join(tmp, "bk"), COMPOSE_PROJECT_NAME="proj",
               ENV_FILE=os.path.join(tmp, "no.env"), HOME=tmp)
    env.pop("DATABASE_URL", None)
    env.pop("OFFSITE_CMD", None)
    return env, fv, cron


def sh(env, *args):
    return subprocess.run(["bash", SCRIPT, *args], capture_output=True, text=True, env=env, timeout=60)


def _day_dir(env):
    d = os.path.join(env["BACKUP_DIR"], sorted(os.listdir(env["BACKUP_DIR"]))[-1])
    return d


def test_run_writes_archives_manifest_and_verifies():
    with tempfile.TemporaryDirectory() as tmp:
        env, fv, _ = _setup(tmp)
        r = sh(env, "run")
        assert r.returncode == 0, r.stdout + r.stderr
        d = _day_dir(env)
        assert {"control-state-rt.tar.gz", "runner-data.tar.gz", "MANIFEST.sha256"} <= set(os.listdir(d))
        assert "checksums match" in r.stdout and "OFFSITE_CMD is not set" in r.stdout and "DATABASE_URL is empty" in r.stdout
        assert sh(env, "verify").returncode == 0


def test_a_damaged_archive_fails_verify():
    with tempfile.TemporaryDirectory() as tmp:
        env, fv, _ = _setup(tmp)
        sh(env, "run")
        with open(os.path.join(_day_dir(env), "runner-data.tar.gz"), "ab") as f:
            f.write(b"junk")
        r = sh(env, "verify")
        assert r.returncode != 0 and "checksum does not match" in r.stdout


def test_nothing_to_back_up_is_a_failure_not_a_silent_ok():
    with tempfile.TemporaryDirectory() as tmp:
        env, fv, _ = _setup(tmp, vols={})
        r = sh(env, "run")
        assert r.returncode != 0 and "manifest is empty" in r.stdout


def test_restore_test_compares_and_cleans_up():
    with tempfile.TemporaryDirectory() as tmp:
        env, fv, _ = _setup(tmp)
        sh(env, "run")
        r = sh(env, "restore-test")
        assert r.returncode == 0, r.stdout
        assert "restored 2 file(s), same as the archive" in r.stdout and "restored 1 file(s)" in r.stdout
        assert not [n for n in os.listdir(fv) if n.endswith("-restoretest")]


def test_restore_makes_a_new_volume_and_never_overwrites():
    with tempfile.TemporaryDirectory() as tmp:
        env, fv, _ = _setup(tmp)
        sh(env, "run")
        arc = os.path.join(_day_dir(env), "runner-data.tar.gz")
        r = sh(env, "restore", "runner-data", arc)
        assert r.returncode == 0 and open(os.path.join(fv, "proj_runner-data-restored", "a", "b.txt")).read() == "hello"
        assert open(os.path.join(fv, "proj_runner-data", "a", "b.txt")).read() == "hello"
        r2 = sh(env, "restore", "runner-data", arc)
        assert r2.returncode != 0 and "already exists" in r2.stdout


def test_old_backups_are_pruned_and_new_kept():
    with tempfile.TemporaryDirectory() as tmp:
        env, fv, _ = _setup(tmp)
        old = os.path.join(env["BACKUP_DIR"], "2020-01-01")
        os.makedirs(old)
        t = time.time() - 40 * 86400
        os.utime(old, (t, t))
        sh(env, "run")
        assert not os.path.exists(old) and len(os.listdir(env["BACKUP_DIR"])) == 1


def test_postgres_dump_and_failure_redaction():
    with tempfile.TemporaryDirectory() as tmp:
        env, fv, _ = _setup(tmp)
        good = os.path.join(tmp, "pgd_ok")
        open(good, "w").write("#!/usr/bin/env bash\necho PGDMP-fake\n")
        os.chmod(good, 0o755)
        stub = os.path.join(tmp, "stubbin")                       # a real pg_restore would (rightly) reject the fake dump, so use a stub
        os.makedirs(stub)
        open(os.path.join(stub, "pg_restore"), "w").write("#!/usr/bin/env bash\nexit 0\n")
        os.chmod(os.path.join(stub, "pg_restore"), 0o755)
        env.update(PGDUMP_BIN=good, DATABASE_URL="postgresql://u:secretpw@h/db", PATH=stub + os.pathsep + env["PATH"])
        r = sh(env, "run")
        assert r.returncode == 0 and "postgres dumped" in r.stdout and "postgres.dump" in open(os.path.join(_day_dir(env), "MANIFEST.sha256")).read()
        bad = os.path.join(tmp, "pgd_bad")
        open(bad, "w").write("#!/usr/bin/env bash\necho 'connection to postgresql://u:secretpw@h/db failed' >&2\nexit 1\n")
        os.chmod(bad, 0o755)
        env.update(PGDUMP_BIN=bad)
        r = sh(env, "run")
        assert r.returncode != 0 and "secretpw" not in r.stdout + r.stderr and "postgres dump failed" in r.stdout


def test_offsite_command_runs_and_its_failure_is_reported():
    with tempfile.TemporaryDirectory() as tmp:
        env, fv, _ = _setup(tmp)
        marker = os.path.join(tmp, "copied")
        env["OFFSITE_CMD"] = f"cp -r {{dir}} {marker}"
        r = sh(env, "run")
        assert r.returncode == 0 and os.path.isdir(marker) and "copied off the VM" in r.stdout
        env["OFFSITE_CMD"] = "false"
        assert sh(env, "run").returncode != 0


def test_schedule_adds_only_its_own_lines_and_removes_them():
    with tempfile.TemporaryDirectory() as tmp:
        env, fv, cron = _setup(tmp)
        open(cron, "w").write("0 1 * * * echo mine\n")
        assert sh(env, "schedule").returncode == 0
        txt = open(cron).read()
        assert "echo mine" in txt and "backup_state.sh run" in txt and "restore-test" in txt and "CRON_TZ=Asia/Kolkata" in txt
        sh(env, "schedule")
        assert open(cron).read().count("backup_state.sh run") == 1
        sh(env, "schedule-remove")
        txt = open(cron).read()
        assert "echo mine" in txt and "backup_state" not in txt


if __name__ == "__main__":
    import traceback
    ok = bad = 0
    for k, f in sorted(globals().items()):
        if k.startswith("test_") and callable(f):
            try:
                f(); ok += 1; print("ok", k)
            except Exception:
                bad += 1; print("FAIL", k); traceback.print_exc()
    print(f"{ok} passed, {bad} failed")
    raise SystemExit(1 if bad else 0)
