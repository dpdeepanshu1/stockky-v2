"""
tests/test_service_main.py — coverage for the service entrypoint, main.py (repo folder root)

main.py mounts the five sub-apps (technical / fundamental / news / event / sentiment) by
executing each folder's main.py under a private module name, records success/failure per
mount in MOUNT_STATUS and reports it on GET / and GET /health.

Most tests copy main.py into a temp directory next to five tiny FAKE sub-app folders, so
every branch (missing folder, no `app`, import-time crash, sys.path / cwd isolation) can be
driven deterministically and fast. One smoke test runs the REAL main.py against the real
sub-apps and asserts that all five mount — it will fail if a dependency is missing or a
sub-app stops importing. That test snapshots and restores sys.modules / sys.path so the
sub-app modules it loads cannot leak into other test files.

The temp trees are created inside a directory named `__pycache__` on purpose: .coveragerc
omits `*/__pycache__/*`, so the throwaway copies of main.py and the fake sub-apps never show
up in the coverage report (only the real main.py, executed by the smoke test, is measured).

Run from services/analysis-intelligence-service:
    python3 -m pytest tests/test_service_main.py -v
"""
from __future__ import annotations

import importlib.util
import itertools
import os
import shutil
import sys

import pytest
from fastapi.testclient import TestClient

_HERE = os.path.dirname(os.path.abspath(__file__))
_SVC = os.path.dirname(_HERE)
_REAL_MAIN = os.path.join(_SVC, "main.py")

_FOLDERS = ("technical", "fundamental", "news", "event", "sentiment")
_ALIASES = {
    "technical": "ai_technical_main",
    "fundamental": "ai_fundamental_main",
    "news": "ai_news_main",
    "event": "ai_event_main",
    "sentiment": "ai_sentiment_main",
}
_counter = itertools.count()

_OK = '''
import os, sys
from fastapi import FastAPI
app = FastAPI()
SEEN = {"cwd": os.getcwd(), "path0": sys.path[0], "path": list(sys.path)}

@app.get("/ping")
def ping():
    return {"who": "__WHO__"}
'''
_NO_APP = "x = 1\n"
_CRASH = "raise ValueError('boom ' + 'x' * 400)\n"
_CRASH_SHORT = "raise ValueError('short failure')\n"
_SYNTAX = "def broken(:\n"


class Snapshot:
    """Restore sys.modules / sys.path / cwd after a test that executes real or fake code."""

    def __init__(self):
        self.modules = dict(sys.modules)
        self.path = list(sys.path)
        self.cwd = os.getcwd()

    def restore(self):
        for k in list(sys.modules):
            if k not in self.modules:
                del sys.modules[k]
        for k, v in self.modules.items():
            if sys.modules.get(k) is not v:
                sys.modules[k] = v
        sys.path[:] = self.path
        os.chdir(self.cwd)


@pytest.fixture(autouse=True)
def _isolate():
    snap = Snapshot()
    yield
    snap.restore()


def _write_subapp(root, folder, kind):
    d = os.path.join(root, folder)
    if kind == "missing_folder":
        return
    os.makedirs(d, exist_ok=True)
    if kind == "missing_file":
        return
    body = {
        "ok": _OK.replace("__WHO__", folder),
        "no_app": _NO_APP,
        "crash": _CRASH,
        "crash_short": _CRASH_SHORT,
        "syntax": _SYNTAX,
    }[kind]
    with open(os.path.join(d, "main.py"), "w") as f:
        f.write(body)


@pytest.fixture
def tree_root(tmp_path):
    # Named __pycache__ on purpose: .coveragerc omits `*/__pycache__/*`, which keeps the
    # throwaway fake sub-apps and main.py copies out of the coverage report.
    # pytest cleans tmp_path itself.
    return str(tmp_path / "__pycache__")


@pytest.fixture
def build(tree_root):
    """build({folder: kind, ...}) -> freshly executed copy of main.py in a temp tree."""

    def _build(kinds=None):
        kinds = dict(kinds or {})
        root = os.path.join(tree_root, f"svc{next(_counter)}")
        os.makedirs(root)
        for folder in _FOLDERS:
            _write_subapp(root, folder, kinds.get(folder, "ok"))
        shutil.copy(_REAL_MAIN, os.path.join(root, "main.py"))
        spec = importlib.util.spec_from_file_location(f"_svc_main_{next(_counter)}", os.path.join(root, "main.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.ROOT = root
        return mod

    return _build


# ── happy path ────────────────────────────────────────────────────────────────

class TestAllMounted:
    def test_every_subapp_is_recorded_as_mounted_in_order(self, build):
        m = build()
        assert list(m.MOUNT_STATUS) == ["/technical", "/fundamental", "/news", "/event", "/sentiment"]
        assert m.MOUNT_STATUS["/technical"] == {"ok": True, "label": "technical analysis", "folder": "technical"}
        assert m.MOUNT_STATUS["/fundamental"]["label"] == "fundamental analysis"
        assert m.MOUNT_STATUS["/news"]["label"] == "news intelligence"
        assert m.MOUNT_STATUS["/event"]["label"] == "event tracker"
        assert m.MOUNT_STATUS["/sentiment"]["label"] == "market sentiment"
        assert all("error" not in s for s in m.MOUNT_STATUS.values())

    def test_subapps_are_reachable_under_their_prefix(self, build):
        client = TestClient(build().app)
        for folder in _FOLDERS:
            r = client.get(f"/{folder}/ping")
            assert r.status_code == 200
            assert r.json() == {"who": folder}

    def test_module_aliases_are_registered(self, build):
        build()
        for folder, alias in _ALIASES.items():
            assert alias in sys.modules
            assert sys.modules[alias].app is not None

    def test_health_ok(self, build):
        body = TestClient(build().app).get("/health").json()
        assert body["status"] == "ok"
        assert body["service"] == "analysis-intelligence-service"
        assert body["all_ok"] is True
        assert body["failed"] == []
        assert body["mounted"] == ["/technical", "/fundamental", "/news", "/event", "/sentiment"]
        assert set(body["mounts"]) == set(body["mounted"])

    def test_root_running(self, build):
        body = TestClient(build().app).get("/").json()
        assert body["service"] == "Stockky Analysis Intelligence Service"
        assert body["version"] == "1.0.2"
        assert body["status"] == "running"
        assert body["modules"] == list(_FOLDERS)
        assert body["all_ok"] is True

    def test_route_functions_can_be_called_directly(self, build):
        m = build()
        assert m.health()["status"] == "ok"
        assert m.root()["status"] == "running"

    def test_app_metadata(self, build):
        app = build().app
        assert app.title == "Stockky Analysis Intelligence Service"
        assert app.version == "1.0.2"
        assert app.description == "Merged technical, fundamental, news, event, sentiment analysis"

    def test_cors_allows_any_origin(self, build):
        client = TestClient(build().app)
        r = client.get("/health", headers={"Origin": "https://example.test"})
        assert r.headers["access-control-allow-origin"] == "*"
        pre = client.options("/health", headers={
            "Origin": "https://example.test",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-anything",
        })
        assert pre.status_code == 200
        assert pre.headers["access-control-allow-origin"] == "*"


# ── failures are recorded, not raised ─────────────────────────────────────────

class TestMountFailures:
    def test_missing_folder_is_recorded_and_others_still_mount(self, build):
        m = build({"news": "missing_folder"})
        st = m.MOUNT_STATUS["/news"]
        assert st["ok"] is False
        assert st["folder"] == "news" and st["label"] == "news intelligence"
        assert st["error"].endswith(os.path.join("news", "main.py"))
        assert m.MOUNT_STATUS["/technical"]["ok"] is True
        assert m.MOUNT_STATUS["/sentiment"]["ok"] is True

    def test_folder_without_main_py_is_recorded(self, build):
        m = build({"event": "missing_file"})
        assert m.MOUNT_STATUS["/event"]["ok"] is False
        assert "main.py" in m.MOUNT_STATUS["/event"]["error"]

    def test_module_without_app_is_recorded(self, build):
        m = build({"sentiment": "no_app"})
        assert m.MOUNT_STATUS["/sentiment"] == {
            "ok": False,
            "label": "market sentiment",
            "folder": "sentiment",
            "error": "sentiment/main.py has no 'app' attribute",
        }

    def test_import_time_exception_is_recorded(self, build):
        m = build({"technical": "crash_short"})
        assert m.MOUNT_STATUS["/technical"]["ok"] is False
        assert m.MOUNT_STATUS["/technical"]["error"] == "short failure"

    def test_syntax_error_is_recorded(self, build):
        m = build({"fundamental": "syntax"})
        assert m.MOUNT_STATUS["/fundamental"]["ok"] is False
        assert m.MOUNT_STATUS["/fundamental"]["error"]

    def test_error_text_is_truncated_to_300_chars(self, build):
        m = build({"technical": "crash"})
        err = m.MOUNT_STATUS["/technical"]["error"]
        assert len(err) == 300
        assert err.startswith("boom xxx")

    def test_failed_mount_returns_404_others_still_serve(self, build):
        client = TestClient(build({"news": "crash_short"}).app)
        assert client.get("/news/ping").status_code == 404
        assert client.get("/event/ping").status_code == 200

    def test_failure_is_logged_as_error(self, build, caplog):
        with caplog.at_level("ERROR", logger="analysis-intelligence-service"):
            build({"news": "crash_short"})
        msgs = [r.getMessage() for r in caplog.records]
        assert any("Failed to mount /news" in m and "short failure" in m for m in msgs)

    def test_success_is_logged_at_info(self, build, caplog):
        with caplog.at_level("INFO", logger="analysis-intelligence-service"):
            build()
        assert sum("Mounted /" in r.getMessage() for r in caplog.records) == 5

    def test_partial_failure_reports_degraded(self, build):
        client = TestClient(build({"news": "crash_short", "event": "no_app"}).app)
        h = client.get("/health").json()
        assert h["status"] == "degraded"
        assert h["all_ok"] is False
        assert h["failed"] == ["/news", "/event"]
        assert h["mounted"] == ["/technical", "/fundamental", "/sentiment"]
        r = client.get("/").json()
        assert r["status"] == "degraded"
        assert r["failed"] == ["/news", "/event"]

    def test_total_failure_reports_error_on_health_but_degraded_on_root(self, build):
        m = build({f: "crash_short" for f in _FOLDERS})
        client = TestClient(m.app)
        h = client.get("/health").json()
        assert h["status"] == "error"
        assert h["mounted"] == []
        assert h["all_ok"] is False
        # Pinned: the root endpoint has no "error" state — it only knows running/degraded.
        assert client.get("/").json()["status"] == "degraded"


class TestMountSummary:
    def test_empty_status_is_not_all_ok(self, build):
        m = build()
        m.MOUNT_STATUS.clear()
        s = m._mount_summary()
        assert s == {"mounts": {}, "mounted": [], "failed": [], "all_ok": False}
        assert m.health()["status"] == "error"

    def test_summary_shape(self, build):
        m = build({"news": "no_app"})
        s = m._mount_summary()
        assert set(s) == {"mounts", "mounted", "failed", "all_ok"}
        assert s["mounts"] is m.MOUNT_STATUS
        assert s["failed"] == ["/news"]

    def test_status_missing_ok_key_counts_as_failed(self, build):
        m = build()
        m.MOUNT_STATUS.clear()
        m.MOUNT_STATUS["/x"] = {"label": "x"}
        s = m._mount_summary()
        assert s["failed"] == ["/x"] and s["mounted"] == []


# ── sys.path / cwd isolation while loading ────────────────────────────────────

class TestLoaderIsolation:
    def test_base_dir_is_put_on_sys_path_once(self, build):
        m = build()
        assert sys.path.count(m.BASE) == 1
        assert sys.path[0] == m.BASE or m.BASE in sys.path

    def test_base_dir_not_duplicated_when_already_present(self, tree_root):
        root = os.path.join(tree_root, "pre")
        os.makedirs(root)
        for f in _FOLDERS:
            _write_subapp(root, f, "ok")
        shutil.copy(_REAL_MAIN, os.path.join(root, "main.py"))
        sys.path.insert(0, root)
        spec = importlib.util.spec_from_file_location("_svc_main_pre", os.path.join(root, "main.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert sys.path.count(root) == 1

    def test_subapp_sees_its_own_folder_first_and_cwd_set_to_it(self, build):
        m = build()
        for folder, alias in _ALIASES.items():
            seen = sys.modules[alias].SEEN
            want = os.path.realpath(os.path.join(m.ROOT, folder))
            assert os.path.realpath(seen["cwd"]) == want
            assert os.path.realpath(seen["path0"]) == want

    def test_sibling_subapp_folders_are_hidden_while_loading(self, build):
        # Pre-seed sys.path with a sibling sub-app folder; the loader must drop it while
        # another sub-app executes so same-named helpers (utils.py, ...) cannot collide.
        root = build().ROOT               # first build only to get a populated temp tree
        sibling = os.path.join(root, "fundamental")
        for alias in _ALIASES.values():
            sys.modules.pop(alias, None)
        sys.path.insert(0, sibling)
        spec = importlib.util.spec_from_file_location("_svc_main_again", os.path.join(root, "main.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        seen_tech = sys.modules["ai_technical_main"].SEEN["path"]
        assert sibling not in seen_tech
        assert os.path.realpath(seen_tech[0]) == os.path.realpath(os.path.join(root, "technical"))

    def test_empty_sys_path_entry_is_left_alone_while_loading(self, build):
        # Pinned: only the five sibling folders are filtered; '' (cwd) is NOT stripped.
        root = build().ROOT
        for alias in _ALIASES.values():
            sys.modules.pop(alias, None)
        sys.path.insert(0, "")
        spec = importlib.util.spec_from_file_location("_svc_main_empty", os.path.join(root, "main.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert "" in sys.modules["ai_technical_main"].SEEN["path"]

    def test_sys_path_and_cwd_restored_after_success(self, build):
        cwd = os.getcwd()
        m = build()
        assert os.getcwd() == cwd
        assert sys.path[0] == m.BASE
        assert not any(p.endswith(os.sep + f) for p in sys.path for f in _FOLDERS if p.startswith(m.ROOT))

    def test_sys_path_and_cwd_restored_after_failure(self, build):
        cwd = os.getcwd()
        m = build({f: "crash_short" for f in _FOLDERS})
        assert os.getcwd() == cwd
        assert not any(p.startswith(m.ROOT + os.sep) for p in sys.path)

    def test_failed_module_stays_registered_under_its_alias(self, build):
        # Pinned: the alias is inserted into sys.modules BEFORE exec, so a crashing
        # sub-app leaves a half-initialised module behind.
        build({"technical": "crash_short"})
        assert "ai_technical_main" in sys.modules

    def test_load_subapp_raises_directly_for_missing_folder(self, build):
        m = build()
        with pytest.raises(FileNotFoundError):
            m._load_subapp("does_not_exist", "ai_nothing")


# ── smoke test against the REAL sub-apps ──────────────────────────────────────

class TestRealServiceSmoke:
    def test_all_five_real_subapps_mount(self):
        spec = importlib.util.spec_from_file_location("_real_service_main", _REAL_MAIN)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        failed = {p: s.get("error") for p, s in mod.MOUNT_STATUS.items() if not s["ok"]}
        assert failed == {}, f"sub-apps failed to mount: {failed}"
        client = TestClient(mod.app)
        h = client.get("/health").json()
        assert h["status"] == "ok"
        assert h["mounted"] == ["/technical", "/fundamental", "/news", "/event", "/sentiment"]
        assert client.get("/").json()["status"] == "running"
