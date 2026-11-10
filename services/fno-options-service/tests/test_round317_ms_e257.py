"""Round 317 (F&O copy of shared E257): walk-forward oos_edge is the weighted mean of per-fold edges, not a pool across periods. F&O was not in scripts/sync_market_structure.py TARGETS, so its stockky_ms kept the pooled edge,
which observation.py turns into a rule suggestion when it is above 0.25."""
from __future__ import annotations

import statistics
from stockky_ms import backtest as B


def test_oos_edge_does_not_flip_sign_by_pooling_periods(monkeypatch):
    # Block 0 trains; blocks 1-4 are the folds. Inside every fold the rejected group does BETTER than the passed one (edge < 0), but the
    # rejections sit mostly in the low-level fold and the passes in the high-level folds, so a pooled difference comes out strongly positive.
    block = [(-5.0, 20, 190), (-5.0, 200, 10), (-2.0, 10, 200), (2.0, 10, 200), (5.0, 10, 200)]
    samples, ts, pooled_rej, pooled_ok = [], 0, [], []
    for b, (lvl, nrej, nok) in enumerate(block):
        for i in range(nrej + nok):
            ts += 1
            rej = i < nrej
            ret = lvl + (0.0 if rej else -0.3)
            samples.append({"ts": ts, "summary": {"flag": rej}, "outcome": {"ret_10d": ret}})
            if b >= 1:
                (pooled_rej if rej else pooled_ok).append(ret)
    assert statistics.mean(pooled_ok) - statistics.mean(pooled_rej) > 0       # the old pooled number points the wrong way
    monkeypatch.setattr(B, "rule_flags", lambda summary, th=None: {**{r: False for r in B.RULES}, "chase": bool(summary["flag"])})
    out = B.walk_forward(samples, "chase", [1.0], folds=4, horizon=10, min_train=10, min_n=10)
    assert len(out["folds"]) == 4 and all(f["test_edge"] < 0 for f in out["folds"]), out
    assert out["oos_edge_pooled"] > 0 and out["oos_edge"] < 0, out


def test_the_vendored_copy_matches_its_manifest():
    """A hand-edit of stockky_ms (or a copy that was never regenerated) shows here: every file's hash must equal MANIFEST.sha256 (scripts/sync_market_structure.py writes it)."""
    import hashlib
    from pathlib import Path

    pkg = Path(B.__file__).resolve().parent
    rows = [ln.split("  ", 1) for ln in (pkg / "MANIFEST.sha256").read_text().splitlines() if ln.strip()]
    assert rows
    for digest, name in rows:
        assert hashlib.sha256((pkg / name).read_bytes()).hexdigest() == digest, name
    assert {n for _, n in rows} == {p.name for p in pkg.glob("*.py")}
