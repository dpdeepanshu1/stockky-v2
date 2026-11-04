"""round 206: while real-trade-service self-adjusts the regime constants, the scheduler stops nagging about review dates."""
import scheduler.governance_check as gov


def test_stale_nag_is_muted_while_adaptive_is_live(monkeypatch):
    monkeypatch.setattr(gov, "_days_since", lambda d: 99)
    monkeypatch.delenv("REGIME_ADAPTIVE_MODE", raising=False)
    assert gov.check_stale_constants() == []
    monkeypatch.setenv("REGIME_ADAPTIVE_MODE", "live")
    assert gov.check_stale_constants() == []


def test_stale_nag_returns_when_adaptive_is_off(monkeypatch):
    monkeypatch.setattr(gov, "_days_since", lambda d: 99)
    monkeypatch.setenv("REGIME_ADAPTIVE_MODE", "off")
    assert {s["constant"] for s in gov.check_stale_constants()} == set(gov.REGIME_CONSTANTS)
