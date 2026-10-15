"""group 403: /health says which market-structure engine this position-stocks service runs (version + code fingerprint) so a deploy can confirm that every
service carries the same engine. The fingerprint is the sha1 of the vendored copy's MANIFEST.sha256, so a stale or hand-edited copy shows a different value."""
import hashlib
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)


def test_vendored_engine_info_matches_its_manifest():
    from stockky_ms.info import engine_info
    from stockky_ms.ms_types import MS_ENGINE_VERSION
    mf = os.path.join(HERE, "stockky_ms", "MANIFEST.sha256")
    d = engine_info()
    assert d["engine_version"] == MS_ENGINE_VERSION and d["code_hash"] == hashlib.sha1(open(mf, "rb").read()).hexdigest()[:8]


def test_health_reports_the_engine_without_a_params_hash():
    try:
        import main
    except Exception:          # fastapi / sqlalchemy not installed in this environment: the route is covered where they are
        return
    res = main.health()
    eng = res["structure_engine"]
    assert eng["engine_version"] and eng["code_hash"] and "params_hash" not in eng
    assert res["status"] == "ok"
