"""Shared tree-walk helpers for the repo-scanning scripts (gen_env_reference, check_shared_copies,
check_price_source_imports).

Why this exists (group 316): the scanners walked `services/` and only skipped a few folder names, so on a machine
that has a local virtualenv (`services/<svc>/.venv`, as on the Ubuntu VM) they also read every installed package
(pip, coverage, platformdirs, ...) as if it were production code. `gen_env_reference.py` then listed 124 variables such
as `APPDATA` / `COVERAGE_CORE` / `ALLUSERSPROFILE` that no service of ours reads, and the output differed from machine
to machine, so `--check` could never be trusted. One shared rule keeps the three scripts in agreement.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

# folder names that never hold our production code
VENDORED_NAMES = {
    "__pycache__", "node_modules", "site-packages", "dist-packages", "build", "dist",
    ".git", ".tox", ".nox", ".eggs", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".idea", ".vscode", "htmlcov",
}
# virtualenv folders are recognised by name AND by content, so `python -m venv myenv` is caught too
VENV_NAME_PREFIXES = (".venv", "venv", ".env_", "env_")


def is_vendored_dir(path: Path) -> bool:
    """True for a directory that must not be scanned: caches, build output, any virtualenv, installed packages."""
    name = path.name
    if name in VENDORED_NAMES or name.endswith(".egg-info"):
        return True
    if name == "env" or name.startswith(VENV_NAME_PREFIXES):
        return True
    return (path / "pyvenv.cfg").is_file()  # every venv / virtualenv writes this file at its root


def walk_py(root: Path, extra_skip: frozenset[str] | set[str] = frozenset()) -> Iterator[Path]:
    """Yield every *.py under `root` in a stable (sorted) order, pruning vendored directories and `extra_skip` names."""
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if d not in extra_skip and not is_vendored_dir(here / d))
        for f in sorted(filenames):
            if f.endswith(".py"):
                yield here / f
