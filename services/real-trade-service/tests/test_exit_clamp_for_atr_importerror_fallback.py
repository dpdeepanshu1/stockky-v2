"""Phase 1 (100%-coverage plan) -- exit_engine/exit.py's last remaining
untested lines: the module-level

    try:
        from return_sanity import clamp_for_atr as _clamp_for_atr
    except ImportError:
        def _clamp_for_atr(x):
            return None if x is None or abs(x) > 30.0 else x

fallback (lines ~67-69, flagged in session82c's note as "cheap to do next
session"). The normal (import-succeeds) branch is already exercised
indirectly every time the rest of the exit.py suite imports this module --
only the except-branch's fallback function itself had zero direct coverage.

`return_sanity` is faked missing by patching `builtins.__import__` for that
one module name and reloading exit_engine.exit under the patch, then
reloading it again afterwards (in a `finally`) to restore the real,
production import for every other test module in the same pytest session.

Run from services/real-trade-service:
    python -m pytest tests/test_exit_clamp_for_atr_importerror_fallback.py -q
"""
import builtins
import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import exit_engine.exit as ex


def test_clamp_for_atr_uses_return_sanity_when_importable():
    # Sanity check on the normal branch first: in this repo, return_sanity.py
    # is present (a local copy inside real-trade-service/), so the plain
    # `import exit_engine.exit` above already took the `try` branch, not the
    # fallback. Confirm _clamp_for_atr is the real module's function.
    import return_sanity
    assert ex._clamp_for_atr is return_sanity.clamp_for_atr


def test_clamp_for_atr_importerror_fallback_behaves_correctly():
    real_import = builtins.__import__

    def _fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "return_sanity":
            raise ImportError("simulated: return_sanity not installed")
        return real_import(name, globals, locals, fromlist, level)

    builtins.__import__ = _fake_import
    try:
        reloaded = importlib.reload(ex)
    finally:
        builtins.__import__ = real_import

    try:
        # The reload happened under the patched __import__, so this module's
        # `except ImportError:` branch must have defined the fallback
        # `_clamp_for_atr` locally -- confirm it's no longer return_sanity's.
        import return_sanity
        assert reloaded._clamp_for_atr is not return_sanity.clamp_for_atr

        # Fallback contract: `None if x is None or abs(x) > 30.0 else x`.
        assert reloaded._clamp_for_atr(None) is None
        assert reloaded._clamp_for_atr(5.0) == 5.0
        assert reloaded._clamp_for_atr(-5.0) == -5.0
        assert reloaded._clamp_for_atr(30.0) == 30.0   # boundary: not > 30.0, kept
        assert reloaded._clamp_for_atr(30.1) is None    # just past boundary, excluded
        assert reloaded._clamp_for_atr(-31.0) is None   # negative side, excluded
        assert reloaded._clamp_for_atr(1000.0) is None  # obvious corporate-action jump
    finally:
        # Restore the real, unpatched import for every other test module
        # sharing this pytest process/session.
        importlib.reload(ex)
        import return_sanity as _rs
        assert ex._clamp_for_atr is _rs.clamp_for_atr
