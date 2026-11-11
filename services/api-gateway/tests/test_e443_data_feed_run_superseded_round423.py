"""E443 (round 423): every /data-feed/run launch stamps its own run id on the job row, and a worker whose run id was replaced (a Resume that
stopped the running job, or a force run) exits quietly instead of carrying on as a second worker."""
from test_main_data_feed_run import FakeResp, execute, fr, ok_routes, seq, start  # noqa: F401  (fr is a fixture)


def _rid(fr):
    return fr.store.job_val.get("run_id")


def test_each_launch_stamps_a_fresh_run_id(fr):
    seq(fr)
    start(fr)
    first = _rid(fr)
    start(fr, force=True)
    second = _rid(fr)
    start(fr, resume=True)
    third = _rid(fr)
    assert first and second and third and len({first, second, third}) == 3


def test_worker_replaced_before_its_first_symbol_touches_nothing(fr):
    seq(fr)
    ok_routes(fr, "AAA", "BBB", "CCC")
    out, bt = start(fr)
    fr.store.job_val["run_id"] = "newer-run"        # a newer launch took over the job row
    execute(bt)
    assert fr.store.puts == [] and fr.client.calls == []
    assert fr.store.job_val["status"] == "running" and fr.store.job_val["run_id"] == "newer-run"
    assert not any(m.get("source") == "stop" for m in fr.store.metas)
    assert fr.store.metas == []


def test_worker_replaced_mid_symbol_keeps_the_symbol_but_writes_no_progress(fr):
    seq(fr)
    ok_routes(fr, "AAA", "BBB", "CCC")
    real_get = fr.client.get
    state = {"n": 0}

    async def get(url, timeout=None):
        state["n"] += 1
        if state["n"] == 1:
            fr.store.job_val["run_id"] = "newer-run"
        return await real_get(url, timeout=timeout)

    fr.client.get = get
    out, bt = start(fr)
    execute(bt)
    assert [p[0] for p in fr.store.puts] == ["AAA"]                     # the in-flight symbol is saved
    assert all("BBB" not in u and "CCC" not in u for u in fr.client.urls())
    job = fr.store.job_val
    assert job["run_id"] == "newer-run" and job["status"] == "running"
    assert job["processed"] == 0 and job["checkpoint"]["cursor"] == 0   # not overwritten with this worker's cursor 1
    assert fr.store.metas == []                                          # no stopped / done commit either


def test_row_without_a_run_id_is_not_superseded(fr):
    seq(fr)
    ok_routes(fr, "AAA", "BBB", "CCC")
    out, bt = start(fr)
    fr.store.job_val.pop("run_id", None)
    execute(bt)
    assert fr.store.job_val["status"] == "done" and [p[0] for p in fr.store.puts] == ["AAA", "BBB", "CCC"]


def test_normal_run_keeps_its_own_run_id_to_the_end(fr):
    seq(fr)
    ok_routes(fr, "AAA", "BBB", "CCC")
    out, bt = start(fr)
    rid = _rid(fr)
    execute(bt)
    assert fr.store.job_val["status"] == "done" and fr.store.job_val["run_id"] == rid
