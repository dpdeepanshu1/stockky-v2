#!/usr/bin/env bash
###############################################################################
# stockky_recon.sh
# Run this FIRST. It doesn't test anything — it just gathers basic info
# about your Ubuntu box / Oracle VM so I know how to point the real test
# script at your setup. Nothing secret is collected (no .env contents,
# no passwords, no tokens — only presence/absence and paths).
#
# Usage:
#   chmod +x stockky_recon.sh
#   ./stockky_recon.sh > recon_output.txt
#   (then paste recon_output.txt content back to me)
###############################################################################
set -uo pipefail
echo "===== BASIC INFO ====="
echo "date: $(date)"
echo "whoami: $(whoami)"
echo "pwd: $(pwd)"
echo "home: $HOME"
echo "hostname: $(hostname)"
echo "OS: $(lsb_release -ds 2>/dev/null || cat /etc/os-release 2>/dev/null | head -2)"

echo -e "\n===== NETWORK / IP ====="
echo "private IP(s): $(hostname -I 2>/dev/null)"
echo "public IP (via ifconfig.me, needs internet): $(curl -s -m 5 ifconfig.me || echo 'unreachable')"
echo "listening ports (docker/app related):"
ss -tlnp 2>/dev/null | grep -E ":(8000|8001|8002|8004|8005|8008|5173|80|443)\b" || echo "  (ss needs sudo, or none of these ports are open yet)"

echo -e "\n===== PROJECT LOCATION ====="
echo "Looking for stockky-v2-main under \$HOME (max depth 4)..."
find "$HOME" -maxdepth 4 -iname "stockky-v2-main" -type d 2>/dev/null
echo "Looking for docker-compose.yml with 'stockky' in it under \$HOME..."
find "$HOME" -maxdepth 5 -iname "docker-compose.yml" 2>/dev/null -exec grep -l "stockky\|api-gateway" {} \; 2>/dev/null

echo -e "\n===== DOCKER ====="
if command -v docker >/dev/null 2>&1; then
  echo "docker version: $(docker --version)"
  echo "docker compose version: $(docker compose version 2>/dev/null || echo 'docker compose plugin not found')"
  echo -e "\n--- docker compose ps (run from inside the project dir) ---"
  echo "(cd into your stockky-v2-main folder and re-run this script, or run: cd <path> && docker compose ps)"
else
  echo "docker: NOT FOUND"
fi

echo -e "\n===== IF INSIDE THE PROJECT DIR (has docker-compose.yml here) ====="
if [[ -f "./docker-compose.yml" ]]; then
  echo "Found docker-compose.yml in current dir: $(pwd)"
  echo ".env present: $([[ -f .env ]] && echo yes || echo no)"
  echo "oracle_wallet dir present: $([[ -d oracle_wallet ]] && echo yes || echo no)"
  echo -e "\n--- docker compose ps ---"
  docker compose ps 2>&1
  echo -e "\n--- docker compose config --services (services this compose file defines) ---"
  docker compose config --services 2>&1
else
  echo "(not run from inside the project dir — cd there and re-run for compose status)"
fi

echo -e "\n===== NGINX (if present, e.g. Oracle VM reverse proxy) ====="
if command -v nginx >/dev/null 2>&1; then
  echo "nginx installed: $(nginx -v 2>&1)"
  echo "site configs:"
  ls -la /etc/nginx/sites-enabled/ 2>/dev/null
else
  echo "nginx: not found (or not installed at system level)"
fi

echo -e "\n===== QUICK LOCAL HEALTH PING (only if services already running) ====="
for portname in "8000:api-gateway" "8001:market-data" "8002:analysis" "8004:decision-prediction" "8005:real-trade" "8008:notification-scheduler" "5173:frontend"; do
  port="${portname%%:*}"; name="${portname##*:}"
  code=$(curl -s -o /dev/null -m 3 -w "%{http_code}" "http://localhost:$port/health" 2>/dev/null)
  [[ -z "$code" ]] && code="000"
  echo "  $name (port $port): http_code=$code"
done

echo -e "\n===== PYTHON / RESOURCES ====="
echo "python3: $(python3 --version 2>&1)"
echo "disk free:"; df -h / 2>/dev/null | tail -1
echo "memory:"; free -h 2>/dev/null | head -2

echo -e "\n===== DONE — paste everything above back to Claude ====="
