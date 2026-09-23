#!/usr/bin/env bash
# deploy/deploy.sh — the ONE command to run on the Oracle VM after a git pull.
#
# WHY THIS FILE EXISTS (session 8): every prior session fixed
# deploy/nginx-stockky.conf in the repo and then ran `docker compose up`,
# assumed the fix was live, and moved on. It never was — `docker compose up`
# only touches containers; it has no idea the host's actual nginx config at
# /etc/nginx/sites-available/stockky exists, let alone that this repo file
# is supposed to replace it. Every session since (5, 6, 7, this one) kept
# fixing code that never had a chance to run, because the ONE step that
# actually makes nginx changes live — copy the file to
# /etc/nginx/sites-available/, symlink it into sites-enabled/, `nginx -t`,
# `systemctl reload nginx` — was never automated and never (reliably) done
# by hand either. This script does that step EVERY time, so it can't be
# silently skipped again.
#
# Usage (on the VM, from the repo root):
#   chmod +x deploy/deploy.sh   # once
#   ./deploy/deploy.sh
#
# Safe to re-run any time — every step here is idempotent.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "==> [1/5] Installing host nginx config (deploy/nginx-stockky.conf)"
if [ "$(id -u)" -ne 0 ] && ! sudo -n true 2>/dev/null; then
  echo "    (will prompt for sudo password to write /etc/nginx/...)"
fi
sudo cp deploy/nginx-stockky.conf /etc/nginx/sites-available/stockky
sudo ln -sf /etc/nginx/sites-available/stockky /etc/nginx/sites-enabled/stockky

echo "==> [2/5] Testing nginx config (nginx -t)"
if ! sudo nginx -t; then
  echo "!! nginx -t FAILED — the config just copied in has a syntax error."
  echo "!! NOT reloading nginx (would take the site down). Fix the error above and re-run."
  exit 1
fi

echo "==> [3/5] Reloading nginx"
sudo systemctl reload nginx

echo "==> [4/5] Rebuilding + restarting containers"
docker compose build
docker compose up -d

echo "==> [5/5] Quick smoke test against the live domain"
sleep 3
DOMAIN="stockky.duckdns.org"
echo "    GET  https://${DOMAIN}/positionstocks/status"
curl -s -o /dev/null -w "    -> HTTP %{http_code}\n" "https://${DOMAIN}/positionstocks/status" || true
echo "    GET  https://${DOMAIN}/realtrade/health"
curl -s -o /dev/null -w "    -> HTTP %{http_code}\n" "https://${DOMAIN}/realtrade/health" || true
echo
echo "Expect 200 (or 401 if that route now requires admin login) on BOTH —"
echo "NOT an HTML page, and NOT '405 Not Allowed'. If you see either of"
echo "those, nginx did not actually pick up the new config — check the"
echo "nginx -t / reload output above for errors, and confirm this VM's"
echo "nginx.conf actually includes /etc/nginx/sites-enabled/*."
echo
echo "==> Done."
