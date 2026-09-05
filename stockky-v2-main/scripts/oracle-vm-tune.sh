#!/usr/bin/env bash
# scripts/oracle-vm-tune.sh
#
# One-time host-level tuning for the Oracle Cloud "2 vCPU / 12 GB" Ubuntu
# free-tier VM running Stockky. Safe to re-run (idempotent checks before
# each change). Does NOT touch application code, containers, or data.
#
# What this does and why:
#   1. Adds a 4 GB swap file. Oracle's Ubuntu image ships with NO swap by
#      default. 12 GB is normally plenty for six small services, but a
#      sklearn training burst or a big pandas fundamentals pass can spike
#      briefly — without swap, that spike is an instant OOM-kill of
#      whichever container the kernel picks, not a graceful slowdown.
#      With swap, the same spike just borrows disk for a few seconds.
#   2. Sets vm.swappiness=10 — tells the kernel to strongly prefer RAM and
#      only touch swap under real pressure, so normal operation never
#      slows down because of it existing.
#   3. Sets the Docker daemon's default log rotation (json-file,
#      max-size=10m, max-file=3) at the daemon level too, as a backstop for
#      any container not started via the docker-compose.yml in this repo
#      (which already sets per-service logging limits).
#
# Usage:  sudo bash scripts/oracle-vm-tune.sh

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run this with sudo: sudo bash scripts/oracle-vm-tune.sh" >&2
  exit 1
fi

# ── 1) Swap file ────────────────────────────────────────────────────────────
SWAPFILE=/swapfile
if swapon --show | grep -q "$SWAPFILE"; then
  echo "[swap] $SWAPFILE already active — skipping."
else
  echo "[swap] creating 4G swap file at $SWAPFILE..."
  fallocate -l 4G "$SWAPFILE" || dd if=/dev/zero of="$SWAPFILE" bs=1M count=4096
  chmod 600 "$SWAPFILE"
  mkswap "$SWAPFILE"
  swapon "$SWAPFILE"
  if ! grep -q "^$SWAPFILE " /etc/fstab; then
    echo "$SWAPFILE none swap sw 0 0" >> /etc/fstab
  fi
  echo "[swap] done."
fi

# ── 2) swappiness ────────────────────────────────────────────────────────────
SYSCTL_FILE=/etc/sysctl.d/99-stockky-swappiness.conf
echo "vm.swappiness=10" > "$SYSCTL_FILE"
sysctl -p "$SYSCTL_FILE"
echo "[sysctl] vm.swappiness set to 10."

# ── 3) Docker daemon log rotation default ───────────────────────────────────
DAEMON_JSON=/etc/docker/daemon.json
mkdir -p /etc/docker
if [[ -f "$DAEMON_JSON" ]] && grep -q "log-driver" "$DAEMON_JSON"; then
  echo "[docker] $DAEMON_JSON already configures logging — leaving as-is."
else
  cat > "$DAEMON_JSON" <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "10m",
    "max-file": "3"
  }
}
EOF
  echo "[docker] wrote default log rotation to $DAEMON_JSON — restarting Docker..."
  systemctl restart docker
fi

echo "Done. Current memory/swap:"
free -h
