"""group387: no `async def` in api-gateway may call a function that runs sync SQL (eng.connect()/eng.begin()) directly.

The 14 Oct 10:38 log showed the event loop blocked 3.2 s inside an oracledb rollback (item 3 of the log-issue list). group359
guarded kv_cache's durable reads and group356 moved the Hot Picks MERGE off the loop; this sweep audited every other store /
schema module (hotpicks_*, surprise_*, ipo_*) and main.py and found no remaining direct call, so it pins that state: a future
`async def` that calls one of these has to go through asyncio.to_thread (or a sync route handler, which FastAPI runs on a
worker thread). Pure AST, no imports of the service modules."""
import ast
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_MODULES = ["hotpicks_store", "hotpicks_schema", "surprise_premarket", "surprise_scanner", "surprise_schema", "ipo_scanner", "ipo_schema"]
# E82: handlers moved out of main.py (E55, E76-E81) live in gateway_support/bound_routes*.py and call main-owned helpers as `ref.<name>()`; they are scanned like main.py.
FACTORIES = ["gateway_support/" + f[:-3] for f in sorted(os.listdir(os.path.join(HERE, "gateway_support"))) if f.startswith("bound_routes") and f.endswith(".py")]
ASYNC_SCAN = DB_MODULES + ["main", "data_feed", "batch_worker", "structure_refresh", "instant_scanner", "buy_sniper", "pick_ranking"] + FACTORIES
THREAD_HOPS = ("to_thread", "run_in_executor", "run_sync")


def _tree(name):
    with open(os.path.join(HERE, name + ".py"), encoding="utf-8") as f:
        return ast.parse(f.read())


def _has_sql(fn):
    return any(
        isinstance(n, ast.Attribute) and n.attr in ("connect", "begin")
        and isinstance(n.value, ast.Name) and n.value.id in ("eng", "engine")
        for n in ast.walk(fn)
    )


def _call_name(c):
    f = c.func
    return f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else None)


def _calls_outside_thread_hops(fn):
    """Calls made by `fn` itself: not inside nested defs/lambdas, and not the function REFERENCE handed to a thread hop."""
    out = []

    def rec(node):
        for c in ast.iter_child_nodes(node):
            if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(c, ast.Call):
                nm = _call_name(c)
                if nm in THREAD_HOPS:
                    for a in c.args[1:]:
                        rec(a)
                    continue
                out.append((c.lineno, nm))
            rec(c)

    rec(fn)
    return out


def _db_function_names():
    names = {}
    for m in DB_MODULES:
        t = _tree(m)
        fns = [n for n in t.body if isinstance(n, ast.FunctionDef)]
        db = {n.name for n in fns if _has_sql(n)}
        changed = True
        while changed:
            changed = False
            for n in fns:
                if n.name not in db and any(nm in db for _, nm in _calls_outside_thread_hops(n)):
                    db.add(n.name)
                    changed = True
        names[m] = db
    return names


def _main_tainted(all_db):
    t = _tree("main")
    funcs = [n for n in ast.walk(t) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    taint = {
        n.name for n in funcs if isinstance(n, ast.FunctionDef) and any(
            isinstance(x, ast.ImportFrom) and x.module in DB_MODULES and any(a.name in all_db for a in x.names)
            for x in ast.walk(n))
    }
    changed = True
    while changed:
        changed = False
        for n in funcs:
            if isinstance(n, ast.FunctionDef) and n.name not in taint and any(
                    nm in taint or nm in all_db for _, nm in _calls_outside_thread_hops(n)):
                taint.add(n.name)
                changed = True
    return taint


def test_the_sweep_actually_sees_db_functions():
    names = _db_function_names()
    assert "hotpicks_db_upsert" in names["hotpicks_store"]
    assert "run_ipo_scan" in names["ipo_scanner"]
    assert "ensure_surprise_schema" in names["surprise_schema"]
    assert "hotpicks_db_upsert" in _main_tainted({f for s in names.values() for f in s})


def test_no_async_def_calls_sync_sql_directly():
    names = _db_function_names()
    all_db = {f for s in names.values() for f in s}
    tainted = _main_tainted(all_db)
    offenders = []
    for mod in ASYNC_SCAN:
        local = set(names.get(mod, set()))
        for n in ast.walk(_tree(mod)):
            if not isinstance(n, ast.AsyncFunctionDef):
                continue
            for line, nm in _calls_outside_thread_hops(n):
                if nm in all_db or nm in local or ((mod == "main" or mod in FACTORIES) and nm in tainted):
                    offenders.append(f"{mod}.py:{line} async def {n.name} calls {nm}() on the event loop")
    assert not offenders, "wrap in asyncio.to_thread:\n" + "\n".join(offenders)


def test_detector_flags_a_direct_call_and_ignores_a_thread_hop():
    bad = ast.parse("async def h():\n    return hotpicks_db_upsert(1)\n").body[0]
    ok = ast.parse("async def h():\n    return await asyncio.to_thread(hotpicks_db_upsert, 1)\n").body[0]
    assert [nm for _, nm in _calls_outside_thread_hops(bad)] == ["hotpicks_db_upsert"]
    assert "hotpicks_db_upsert" not in [nm for _, nm in _calls_outside_thread_hops(ok)]


def test_factories_are_scanned_and_a_ref_call_is_flagged():
    assert FACTORIES and "gateway_support/bound_routes_g" in FACTORIES
    bad = ast.parse("async def h():\n    return ref.hotpicks_db_upsert(1)\n").body[0]
    ok = ast.parse("async def h():\n    return await ref.asyncio.to_thread(ref.hotpicks_db_upsert, 1)\n").body[0]
    assert [nm for _, nm in _calls_outside_thread_hops(bad)] == ["hotpicks_db_upsert"]
    assert "hotpicks_db_upsert" not in [nm for _, nm in _calls_outside_thread_hops(ok)]
