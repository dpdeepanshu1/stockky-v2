"""Loads technical/main.py under its own module name for tests; fastapi / httpx are stubbed only when they are not installed."""
from __future__ import annotations

import importlib.util
import os
import sys
import types

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(HERE, "technical", "main.py")


def load(name="technical_main_loaded"):
    added = []

    def stub(name, **attrs):
        try:
            __import__(name)
            return
        except Exception:  # noqa: BLE001
            pass
        m = types.ModuleType(name)
        m.__dict__.update(attrs)
        sys.modules[name] = m
        added.append(name)

    class _App:
        def __init__(self, *a, **k):
            pass

        def add_middleware(self, *a, **k):
            pass

        def get(self, *a, **k):
            return lambda f: f

    class _HTTPException(Exception):
        def __init__(self, status_code=500, detail=""):
            super().__init__(detail)
            self.status_code, self.detail = status_code, detail

    class _Err(Exception):
        pass

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            raise _Err("no network in tests")

    def _get(*a, **k):
        raise _Err("no network in tests")

    stub("httpx", HTTPError=_Err, get=_get, Client=_Client)
    stub("fastapi", FastAPI=_App, HTTPException=_HTTPException)
    stub("fastapi.middleware")
    stub("fastapi.middleware.cors", CORSMiddleware=object)
    sys.path.insert(0, os.path.join(HERE, "technical"))
    sys.path.insert(0, HERE)
    spec = importlib.util.spec_from_file_location(name, PATH)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    finally:
        for n in added:
            sys.modules.pop(n, None)
    return mod
