"""E209: instant_scanner's decision call must target a route the decision service really has (POST /decide/evaluate, mounted under /decision)."""
import importlib.util
import os
import re

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_GATEWAY = os.path.dirname(_HERE)
_DECISION_MAIN = os.path.join(os.path.dirname(os.path.dirname(_GATEWAY)), "services", "decision-prediction-service", "decision", "main.py")


@pytest.fixture(scope="module")
def ins():
    spec = importlib.util.spec_from_file_location("instant_scanner_e209", os.path.join(_GATEWAY, "instant_scanner.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("base", ["http://d/decision", "http://d/decision/", "http://d", "http://d/decision/decide"])
def test_url_ends_in_the_real_route(ins, base):
    assert ins._decision_evaluate_url(base).endswith("/decide/evaluate")


def test_the_route_exists_in_the_decision_service():
    if not os.path.exists(_DECISION_MAIN):
        pytest.skip("decision service source not in this checkout")
    src = open(_DECISION_MAIN, encoding="utf-8").read()
    assert re.search(r'@app\.post\("/decide/evaluate"\)', src)
