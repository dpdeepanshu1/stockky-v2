"""A4b: per-service session secrets (session_keys.py). Pure module tests - no FastAPI, no DB."""
import time

import jwt
import pytest

import session_keys as sk

RT, PS, CR, LEGACY = "r" * 40, "p" * 40, "c" * 40, "l" * 40
ALL = ("real-trade", "position-stocks", "calibration-runner")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in list(sk.SERVICE_ENV.values()) + ["SESSION_SECRET"]:
        monkeypatch.delenv(name, raising=False)


def _claims(**kw):
    now = int(time.time())
    return {"sub": "admin", "iat": now, "exp": now + 600, **kw}


def test_signs_with_own_secret_and_kid(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_REAL_TRADE", RT)
    tok = sk.sign("real-trade", _claims(), legacy=LEGACY)
    assert jwt.get_unverified_header(tok)["kid"] == "real-trade"
    assert jwt.decode(tok, RT, algorithms=["HS256"])["sub"] == "admin"


def test_falls_back_to_legacy_without_kid():
    tok = sk.sign("real-trade", _claims(), legacy=LEGACY)
    assert "kid" not in jwt.get_unverified_header(tok)
    assert sk.decode(tok, ALL, legacy=LEGACY)["sub"] == "admin"


def test_no_secret_at_all_cannot_sign():
    assert not sk.can_sign("real-trade", "")
    with pytest.raises(sk.MissingSecret):
        sk.sign("real-trade", _claims(), legacy="")


def test_cross_service_token_accepted_when_listed(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_POSITION_STOCKS", PS)
    tok = sk.sign("position-stocks", _claims())
    assert sk.decode(tok, ALL)["sub"] == "admin"


def test_kid_not_in_accept_list_is_rejected_even_if_secret_present(monkeypatch):
    # the gateway case: a leaked env_file value for the runner must not make runner tokens valid there
    monkeypatch.setenv("SESSION_SECRET_CALIBRATION_RUNNER", CR)
    tok = sk.sign("calibration-runner", _claims())
    with pytest.raises(jwt.InvalidTokenError):
        sk.decode(tok, ("real-trade", "position-stocks"))


def test_kid_without_its_secret_here_is_rejected_and_never_falls_back(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_REAL_TRADE", RT)
    tok = sk.sign("real-trade", _claims())
    monkeypatch.delenv("SESSION_SECRET_REAL_TRADE")
    with pytest.raises(jwt.InvalidTokenError):
        sk.decode(tok, ALL, legacy=LEGACY)


def test_token_signed_with_wrong_key_for_its_kid_is_rejected(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_REAL_TRADE", RT)
    forged = jwt.encode(_claims(), "x" * 40, algorithm="HS256", headers={"kid": "real-trade"})
    with pytest.raises(jwt.InvalidTokenError):
        sk.decode(forged, ALL)


def test_one_service_cannot_pass_as_another(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_REAL_TRADE", RT)
    monkeypatch.setenv("SESSION_SECRET_POSITION_STOCKS", PS)
    # signed with the position-stocks secret but claiming to be real-trade
    forged = jwt.encode(_claims(), PS, algorithm="HS256", headers={"kid": "real-trade"})
    with pytest.raises(jwt.InvalidTokenError):
        sk.decode(forged, ALL)


def test_legacy_token_stops_working_when_legacy_secret_removed():
    tok = sk.sign("real-trade", _claims(), legacy=LEGACY)
    with pytest.raises(jwt.InvalidTokenError):
        sk.decode(tok, ALL, legacy="")


def test_expired_and_garbage_and_odd_kid(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_REAL_TRADE", RT)
    old = sk.sign("real-trade", {"sub": "a", "exp": int(time.time()) - 5})
    with pytest.raises(jwt.ExpiredSignatureError):
        sk.decode(old, ALL)
    with pytest.raises(jwt.InvalidTokenError):
        sk.decode("not-a-jwt", ALL, legacy=LEGACY)
    t = jwt.encode(_claims(), RT, algorithm="HS256", headers={"kid": "nope"})
    with pytest.raises(jwt.InvalidTokenError):
        sk.decode(t, ALL, legacy=LEGACY)
    import base64, json  # a non-string kid (PyJWT will not encode one, a hand-built token can carry it)
    b = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b"=").decode()
    hand = b({"alg": "HS256", "typ": "JWT", "kid": ["real-trade"]}) + "." + b(_claims()) + ".sig"
    with pytest.raises(jwt.InvalidTokenError):
        sk.decode(hand, ALL, legacy=LEGACY)


def test_alg_none_rejected(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_REAL_TRADE", RT)
    t = jwt.encode(_claims(), None, algorithm="none", headers={"kid": "real-trade"})
    with pytest.raises(jwt.InvalidTokenError):
        sk.decode(t, ALL)


def test_diagnostics_modes_and_warnings(monkeypatch):
    assert sk.diagnostics("real-trade", ALL, "")["signing_mode"] == "none"
    assert sk.diagnostics("real-trade", ALL, LEGACY)["signing_mode"] == "legacy-shared"
    monkeypatch.setenv("SESSION_SECRET_REAL_TRADE", RT)
    d = sk.diagnostics("real-trade", ALL, "")
    assert d["signing_mode"] == "per-service" and d["accepts"]["real-trade"]["fingerprint"] and not d["duplicate_secrets"]
    assert RT not in repr(d)  # fingerprints only
    monkeypatch.setenv("SESSION_SECRET_POSITION_STOCKS", RT)  # same value twice
    assert sk.diagnostics("real-trade", ALL, "")["duplicate_secrets"]
    monkeypatch.setenv("SESSION_SECRET_POSITION_STOCKS", "short")
    assert sk.diagnostics("real-trade", ALL, "")["weak_secret"]
