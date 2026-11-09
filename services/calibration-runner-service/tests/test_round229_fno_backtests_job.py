"""round 229: the F&O backtest job exists, its steps are soft (a missing service never blocks a pack), and it is part of the Friday review pack."""
import jobs


def test_job_defined_and_steps_soft():
    j = jobs.JOBS["fno_backtests"]
    steps = j["builder"]({"fno": "http://fno:8007"}, {"underlyings": "NIFTY"})
    assert steps and all(s["kind"] == "http" and s.get("soft") and s["auth"] for s in steps)
    assert steps[0]["url"] == "http://fno:8007/fno/backtests/run-scheduled?kind=replay&underlyings=NIFTY"
    assert any(s.get("poll", {}) and s["poll"]["key"] == "state" for s in steps if s.get("poll"))
    assert 409 in steps[0]["expect"]


def test_in_friday_pack():
    pack = next(p for p in jobs.PACKS if p["id"] == "friday_review")
    assert ["fno_backtests", {}] in pack["jobs"]


def test_round233_sim_runs_once_per_index_with_the_chosen_strategy():
    steps = jobs.JOBS["fno_backtests"]["builder"]({"fno": "http://fno:8007"}, {"underlyings": "NIFTY,SENSEX", "strategy": "BEAR_SWEEP"})
    sims = [s["url"] for s in steps if "kind=sim" in s["url"]]
    assert sims == ["http://fno:8007/fno/backtests/run-scheduled?kind=sim&underlyings=NIFTY&strategy=BEAR_SWEEP",
                    "http://fno:8007/fno/backtests/run-scheduled?kind=sim&underlyings=SENSEX&strategy=BEAR_SWEEP"]
    default = jobs.JOBS["fno_backtests"]["builder"]({"fno": "http://fno:8007"}, {})
    assert any(s["url"].endswith("underlyings=NIFTY&strategy=ALL") for s in default) and any(s["url"].endswith("underlyings=SENSEX&strategy=ALL") for s in default)
