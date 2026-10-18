"""Run every test_* function in tests/test_*.py without pytest:  python3 tests/run_plain.py [name-substring]   (pytest runs the same files unchanged)"""
import glob, importlib.util, os, sys, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
want = sys.argv[1] if len(sys.argv) > 1 else ""
passed = failed = 0
for path in sorted(glob.glob(os.path.join(HERE, "test_*.py"))):
    spec = importlib.util.spec_from_file_location(os.path.basename(path)[:-3], path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:  # noqa: BLE001
        failed += 1
        print(f"IMPORT FAIL {path}\n{traceback.format_exc()}")
        continue
    for name in sorted(dir(mod)):
        if name.startswith("test_") and callable(getattr(mod, name)) and want in f"{os.path.basename(path)}::{name}":
            try:
                getattr(mod, name)()
                passed += 1
            except Exception:  # noqa: BLE001
                failed += 1
                print(f"FAIL {os.path.basename(path)}::{name}\n{traceback.format_exc()}")
print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
