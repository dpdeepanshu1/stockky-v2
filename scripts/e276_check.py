#!/usr/bin/env python3
"""e276_check.py - one read-only pass over the E276 checks that could not run offline (the database parts of rounds 50 to 61).

  python3 scripts/e276_check.py                 # run from the repo folder on the VM, next to docker-compose.yml
  python3 scripts/e276_check.py --since 24h     # how far back to read the container logs (default 24h)

It runs `docker compose exec` / `docker compose logs` (nothing is changed, no token is read or printed) and prints one line per check that
starts with PASS, WARN or FAIL. Exit code 0 = no FAIL. WARN usually means 'nothing has happened yet' (no position was open, no entry refused).
"""
from __future__ import annotations

import argparse
import subprocess
import sys

PS, RT = "position-stocks-service", "real-trade-service"

CONFIG_SNIPPET = """
import config
for k in ("SECTOR_HEAT_ENABLED","SECTOR_HEAT_MULT","HEAT_LIMIT_PCT","DEFAULT_MAX_PORTFOLIO_RISK_PCT","RISK_LADDER_ENABLED","RISK_LADDER_HEAT_STEP_PCT","PORTFOLIO_HEAT_SHARED_ENABLED","DEFAULT_MAX_CONCURRENT_POSITIONS"):
    if hasattr(config, k):
        print("CFG", k, getattr(config, k))
"""

TABLE_SNIPPET = """
from sqlalchemy import text
import db
S = db.get_session_factory()() if hasattr(db, "get_session_factory") else None
for t, cols in (("stockky_shared_heat", "service_name, trade_date, open_risk, gap_allowance, capital_base, updated_at"),
                ("stockky_shared_heat_sectors", "service_name, trade_date, sector_json, updated_at")):
    try:
        rows = S.execute(text("SELECT %s FROM %s" % (cols, t))).fetchall()
        print("TABLE", t, len(rows))
        for r in rows:
            print("ROW", t, tuple(str(x) for x in r))
    except Exception as e:
        print("TABLE_ERR", t, str(e).splitlines()[0][:200])
        try:
            S.rollback()
        except Exception:
            pass
"""


def run(cmd, stdin=None, timeout=120):
    try:
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:  # noqa: BLE001
        return 1, f"{type(e).__name__}: {e}"


def py_in(service, snippet):
    return run(["docker", "compose", "exec", "-T", service, "python", "-"], stdin=snippet)


def _need_docker() -> None:
    """E277: this script drives `docker compose` from the VM host. Inside a container there is no docker, and every check died with FileNotFoundError."""
    import shutil
    if shutil.which("docker") is None:
        sys.exit("e276_check.py must run on the VM host (next to docker-compose.yml), not inside a container: `docker` was not found.\n"
                 "  cd ~/stockky-v2 && python3 scripts/e276_check.py")


def main() -> int:
    _need_docker()
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="24h")
    a = ap.parse_args()
    fails = 0

    def out(level, msg):
        nonlocal fails
        fails += level == "FAIL"
        print(f"{level} {msg}")

    # 1. switches and limits, both services
    for svc in (PS, RT):
        rc, txt = py_in(svc, CONFIG_SNIPPET)
        cfg = {l.split()[1]: l.split(None, 2)[2] for l in txt.splitlines() if l.startswith("CFG ")}
        if rc != 0 or not cfg:
            out("FAIL", f"{svc}: could not read config ({txt.strip()[:160]})")
            continue
        out("PASS" if cfg.get("SECTOR_HEAT_ENABLED") == "True" else "FAIL", f"{svc}: SECTOR_HEAT_ENABLED={cfg.get('SECTOR_HEAT_ENABLED')} mult={cfg.get('SECTOR_HEAT_MULT')}")
        base = cfg.get("HEAT_LIMIT_PCT") or cfg.get("DEFAULT_MAX_PORTFOLIO_RISK_PCT")
        out("PASS" if base in ("5.0", "5") else "WARN", f"{svc}: stage 0 heat base={base} (E276 round 61 decision: 5)")
        out("PASS" if cfg.get("RISK_LADDER_ENABLED") == "True" else "WARN", f"{svc}: RISK_LADDER_ENABLED={cfg.get('RISK_LADDER_ENABLED')}")

    # 2. the shared tables (the new one is created by create_all at boot)
    rc, txt = py_in(PS, TABLE_SNIPPET)
    if "TABLE_ERR stockky_shared_heat_sectors" in txt:
        out("FAIL", "stockky_shared_heat_sectors is missing or unreadable (did position-stocks-service restart on the new zip?): "
                    + next((l for l in txt.splitlines() if l.startswith("TABLE_ERR stockky_shared_heat_sectors")), ""))
    elif "TABLE stockky_shared_heat_sectors" in txt:
        rows = [l for l in txt.splitlines() if l.startswith("ROW stockky_shared_heat_sectors")]
        if rows:
            out("PASS", f"stockky_shared_heat_sectors has {len(rows)} row(s): " + " | ".join(r[len('ROW stockky_shared_heat_sectors '):][:120] for r in rows))
            if len(rows) < 2:
                out("WARN", "only one service has published a sector split yet (the other publishes after its first open position)")
        else:
            out("WARN", "stockky_shared_heat_sectors exists but is empty (nothing has been open since the deploy)")
    else:
        out("FAIL", f"could not query the shared tables: {txt.strip()[:200]}")
    hrows = [l for l in txt.splitlines() if l.startswith("ROW stockky_shared_heat ")]
    out("PASS" if hrows else "WARN", f"stockky_shared_heat rows: {len(hrows)}")

    # 3. logs: refusals and failures
    for svc in (PS, RT):
        rc, logs = run(["docker", "compose", "logs", "--no-color", "--since", a.since, svc], timeout=180)
        lines = logs.splitlines()
        bad = [l for l in lines if "sector heat failed" in l or "shared-heat: failed" in l or "heat gate failed" in l or "heat_state:" in l and "failed" in l]
        out("FAIL" if bad else "PASS", f"{svc}: {len(bad)} heat/sector failure line(s) in the last {a.since}" + (f", e.g. {bad[-1][-200:]}" if bad else ""))
        refusals = [l for l in lines if "HEAT_LIMIT" in l or "max_portfolio_risk" in l]
        staged = [l for l in refusals if "stage=" in l]
        sector = [l for l in refusals if "sector=" in l or "same-sector" in l]
        out("PASS" if refusals else "WARN", f"{svc}: {len(refusals)} heat refusal(s), {len(staged)} with stage=, {len(sector)} same-sector")
        if refusals:
            print("     last: " + refusals[-1][-240:])
    print("FAIL" if fails else "OK", f"- {fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
