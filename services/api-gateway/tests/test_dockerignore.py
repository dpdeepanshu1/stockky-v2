"""tests/test_dockerignore.py — group 72: every Docker build context has a .dockerignore, and it only drops junk.

Every Dockerfile in the repo does `COPY . .`, and until group 72 there was no .dockerignore anywhere, so each
image carried the tests, __pycache__/*.pyc, .coverage files, any local .env and (for the repo-root Dockerfile)
the Oracle wallet, and the `COPY . .` layer re-ran on every edit to a test file.

Two properties are enforced:
  1. every directory containing a Dockerfile also has a .dockerignore that blocks the secrets / caches /
     test+coverage output (and, for the frontend, node_modules + dist);
  2. nothing a build or the running service needs is dropped: every file a context's ignore rules exclude must
     be a test, cache, coverage, VCS, editor, secret or build-output file. A future rule that swallowed
     requirements.txt, a module, nginx.conf or a data file fails here.

No Docker daemon is needed: a small matcher implements .dockerignore rules (a pattern matches the path or any
parent directory; `**/` = any depth; a bare name only matches at the context root).

Run from services/api-gateway:
    python3 -m pytest tests/test_dockerignore.py -v
"""
from __future__ import annotations

import os
import re

import pytest

SERVICES = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
REPO = os.path.dirname(SERVICES)
_PRUNE = {"node_modules", ".git", "__pycache__", ".pytest_cache", ".venv", "venv"}


def _contexts():
    out = []
    for cur, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in _PRUNE]
        if "Dockerfile" in files:
            out.append(os.path.relpath(cur, REPO).replace(os.sep, "/"))
    return sorted(out)


CONTEXTS = _contexts()


def _load(ctx):
    path = os.path.join(REPO, ctx, ".dockerignore") if ctx != "." else os.path.join(REPO, ".dockerignore")
    with open(path, encoding="utf-8") as fh:
        return [ln.strip() for ln in fh if ln.strip() and not ln.strip().startswith("#")]


def _regex(pattern):
    pattern = pattern.rstrip("/")
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile("^" + out + "$")


def _ignored(rel, rules):
    parts = rel.split("/")
    for rule in rules:
        rx = _regex(rule)
        # a rule that matches the path itself or any parent directory excludes it
        if any(rx.match("/".join(parts[:n])) for n in range(1, len(parts) + 1)):
            return True
    return False


def _files(ctx):
    base = os.path.join(REPO, ctx) if ctx != "." else REPO
    for cur, dirs, files in os.walk(base):
        for f in files:
            yield os.path.relpath(os.path.join(cur, f), base).replace(os.sep, "/")


def _expendable(rel):
    """Files that may legitimately be dropped from an image."""
    parts = rel.split("/")
    name = parts[-1]
    if parts[0] in (".git", ".vscode", ".idea", "node_modules", "dist", ".vite", "oracle_wallet", ".venv", "venv"):
        return True
    if any(p in ("tests", "__pycache__", ".pytest_cache", "node_modules", "dist", "oracle_wallet", "htmlcov")
           or p.endswith(".egg-info") for p in parts[:-1]):
        return True
    return (
        name in (".gitignore", ".gitattributes", ".DS_Store", ".coveragerc", "run_tests.sh", ".coverage")
        or name.startswith((".env", ".coverage."))
        or name.endswith((".pyc", ".pyo", ",cover", ".log"))
    )


def test_every_dockerfile_directory_was_found():
    # guards the guard: services reorganised -> nothing found -> everything below passes vacuously
    assert "." in CONTEXTS and "frontend" in CONTEXTS
    assert len(CONTEXTS) >= 19, CONTEXTS
    compose_services = [
        "market-data-service", "analysis-intelligence-service", "decision-prediction-service",
        "notification-scheduler-service", "api-gateway", "real-trade-service", "position-stocks-service",
    ]
    for s in compose_services:
        assert f"services/{s}" in CONTEXTS, s


@pytest.mark.parametrize("ctx", CONTEXTS)
def test_context_has_a_dockerignore(ctx):
    path = os.path.join(REPO, ctx, ".dockerignore") if ctx != "." else os.path.join(REPO, ".dockerignore")
    assert os.path.isfile(path), ctx + " has a Dockerfile but no .dockerignore"
    assert _load(ctx), ctx + ": .dockerignore has no rules"


@pytest.mark.parametrize("ctx", [c for c in CONTEXTS if c != "frontend"])
def test_python_contexts_block_secrets_caches_and_tests(ctx):
    rules = _load(ctx)
    for sample in (".env", ".env.production", ".git/config", "__pycache__/x.pyc", "pkg/__pycache__/x.cpython-311.pyc",
                   "mod.pyc", "tests/test_x.py", "pkg/tests/test_x.py", ".coverage", ".pytest_cache/v/cache",
                   "htmlcov/index.html", "oracle_wallet/ewallet.p12"):
        if sample.startswith("oracle_wallet") and ctx != ".":
            sample = "sub/" + sample   # service contexts: a nested wallet dir
        assert _ignored(sample, rules), (ctx, sample)


def test_frontend_context_blocks_host_build_output_but_keeps_env_files():
    rules = _load("frontend")
    for sample in ("node_modules/react/index.js", "dist/index.html", ".git/config"):
        assert _ignored(sample, rules), sample
    # vite reads .env.production at build time, so it must still reach the build
    assert not _ignored(".env.production", rules)
    assert not _ignored("src/main.tsx", rules)


def test_root_context_blocks_the_oracle_wallet_and_node_modules():
    rules = _load(".")
    assert _ignored("oracle_wallet/ewallet.p12", rules)
    assert _ignored("frontend/node_modules/react/index.js", rules)
    assert _ignored(".env", rules)


@pytest.mark.parametrize("ctx", CONTEXTS)
def test_ignore_rules_only_drop_expendable_files(ctx):
    rules = _load(ctx)
    dropped_needed = [rel for rel in _files(ctx) if _ignored(rel, rules) and not _expendable(rel)]
    assert dropped_needed == [], ctx


@pytest.mark.parametrize("ctx", CONTEXTS)
def test_build_inputs_survive(ctx):
    # the files the Dockerfiles COPY by name, plus every runtime module / data file
    rules = _load(ctx)
    base = os.path.join(REPO, ctx) if ctx != "." else REPO
    for must in ("requirements.txt", "package.json", "nginx.conf", "index.html", "vite.config.ts", "main.py"):
        if os.path.isfile(os.path.join(base, must)):
            assert not _ignored(must, rules), (ctx, must)
    kept_py = [r for r in _files(ctx) if r.endswith(".py") and not _expendable(r)]
    assert all(not _ignored(r, rules) for r in kept_py), ctx
