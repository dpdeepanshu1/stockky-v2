"""
scripts/check_compose_ports_loopback.py — every published port in docker-compose.yml must be bound
to 127.0.0.1.

Why: "8000:8000" publishes on 0.0.0.0 and Docker adds its own iptables rules ahead of ufw, so the
api-gateway / real-trade / position-stocks ports were reachable from the internet (VM log
2026-10-05: scanners, "Invalid HTTP request received" on api-gateway). The host nginx
(deploy/nginx-stockky.conf) proxies to 127.0.0.1:*, so loopback binding costs nothing.

Run from the repo root:  python3 scripts/check_compose_ports_loopback.py   (or: python3 -m pytest scripts/check_compose_ports_loopback.py)
"""
from __future__ import annotations

import os
import sys

import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _published_ports() -> list[tuple[str, str]]:
    with open(os.path.join(_ROOT, "docker-compose.yml"), encoding="utf-8") as fh:
        compose = yaml.safe_load(fh)
    out = []
    for name, svc in (compose.get("services") or {}).items():
        for port in svc.get("ports") or []:
            out.append((name, str(port)))
    return out


def test_every_published_port_is_loopback_only():
    bad = [(n, p) for n, p in _published_ports() if not p.startswith("127.0.0.1:")]
    assert not bad, f"ports published on all interfaces: {bad}"


def test_ports_exist_to_check():
    assert len(_published_ports()) >= 8


if __name__ == "__main__":
    ports = _published_ports()
    bad = [(n, p) for n, p in ports if not p.startswith("127.0.0.1:")]
    for n, p in ports:
        print(("OK   " if (n, p) not in bad else "OPEN "), n, p)
    sys.exit(1 if bad else 0)
