"""round 587: timesfm, kronos and fno-options start and stop from a lifespan, not the deprecated @app.on_event (source check, stdlib only: kronos' CI job has
no fastapi, so this reads the files instead of importing them). Runtime checks live in each service's tests."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAINS = {"timesfm-forecast-service": 1, "kronos-forecast-service": 1, "fno-options-service": 2}   # hooks each service declared before


def _src(svc):
    return (ROOT / "services" / svc / "main.py").read_text(encoding="utf-8")


def test_no_service_main_uses_the_deprecated_decorator():
    for svc in MAINS:
        assert not re.search(r"^\s*@app\.on_event\(", _src(svc), re.M), svc


def test_every_hook_is_registered_and_the_app_gets_the_lifespan():
    for svc, n in MAINS.items():
        s = _src(svc)
        assert len(re.findall(r"^@_on_(startup|shutdown)\s*$", s, re.M)) == n, svc
        assert re.search(r"^app = FastAPI\(.*lifespan=_lifespan\)", s, re.M), svc


def test_fno_keeps_its_shutdown_hook():
    assert re.search(r"^@_on_shutdown\s*\n(async )?def _shutdown\(", _src("fno-options-service"), re.M)


def test_training_app_uses_model_config_not_the_deprecated_inner_config():
    s = (ROOT / "services" / "decision-prediction-service" / "training" / "app.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s+class Config:", s, re.M)
    assert len(re.findall(r'^\s+model_config = ConfigDict\(extra="ignore"\)', s, re.M)) == 2
    assert re.search(r"^from pydantic import .*ConfigDict", s, re.M)


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
    print("ok 4")
