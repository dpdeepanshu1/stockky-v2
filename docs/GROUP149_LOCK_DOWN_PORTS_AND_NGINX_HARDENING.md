# group149 (2026-10-05) - published ports bound to localhost, nginx hardened against scanners

Cumulative on group148. No service code changed. Apply with `./deploy/deploy.sh` on the VM (it installs the nginx config, reloads nginx, then `docker compose build && up -d`, which re-creates the containers with the new port bindings).

## Cause (VM logs, 2026-10-05)
- Bots (45.154.138.x, 196.189.236.67) probed `/.env`, `/config.json`, `/.aws/credentials`, `phpinfo.php`, PHPUnit `eval-stdin.php` (known RCE) and `/containers/json` (Docker API) on the public site.
- The frontend answered those with `200` and 859 bytes: that is the SPA fallback serving `index.html`, not a leak, but it looks like one and fills the logs.
- api-gateway logged `Invalid HTTP request received` twice and 404s for `/config.js` and the PHPUnit path. Compose published every container port as `"8000:8000"` (all interfaces), and Docker's iptables rules bypass ufw, so the gateway, real-trade (live broker routes) and position-stocks ports could be reached directly from the internet.

## Changes
1. `docker-compose.yml`: all 8 published ports now `127.0.0.1:HOST:CONTAINER`. The host nginx already proxies to `127.0.0.1:5173/8000/8005/8006`, and curl/scripts run on the VM still work. Only direct outside access (`http://<vm-ip>:8000`) is closed.
2. `frontend/nginx.conf` (inside the container): dotfiles return 404; script/config/backup extensions (`.php .env .json .yml .sql ...`) return 404 unless a real file exists; `server_tokens off`. SPA deep links (including ones with dots, e.g. `/stock/RELIANCE.NS`) still fall back to `index.html`.
3. `deploy/nginx-stockky.conf` (host nginx): `server_tokens off`; `X-Content-Type-Options`, `X-Frame-Options: SAMEORIGIN`, `Referrer-Policy`; probe paths (dotfiles except `/.well-known/`, `*.php/asp/jsp/cgi`, `/vendor/`, `/containers/`, `/cgi-bin/`, `/wp-*`, `phpmyadmin`, ...) closed with `return 444`; per-IP rate limit (20 r/s, burst 60, HTTP 429) on the static-site location `/` only. `/api/`, `/realtrade/`, `/positionstocks/` are not rate limited because the dashboard fires many parallel polls from one IP. Routes, timeouts and WebSocket upgrade unchanged.
4. `scripts/check_compose_ports_loopback.py`: fails if any published port is not loopback-bound.

## Tested here
Both configs run under a real nginx 1.24 (syntax ok): `/` 200, hashed asset 200, SPA deep link 200, `/.env` `/phpinfo.php` `/vendor/.../eval-stdin.php` `/containers/json` closed (444), `/.well-known/` not blocked, `/config.json` `/config.js` 404, container-level `/.env` `/config.json` 404; 150 rapid requests to `/` gave 70 x 200 then 429. Compose YAML parses and all 8 ports pass the new check. Not tested here: TLS/certbot lines and the live VM.

## Verify on the VM after deploy
```
docker compose ps                       # ports show 127.0.0.1:...->...
ss -tlnp | grep -E ':(8000|8001|8002|8004|8005|8006|8008|5173)\b'   # all 127.0.0.1, none 0.0.0.0
curl -s -m 5 http://<vm-public-ip>:8000/health ; echo $?            # must fail/time out from another machine
curl -s https://stockky.duckdns.org/.env -o /dev/null -w '%{http_code}\n'   # 000 (connection closed)
curl -s https://stockky.duckdns.org/ -o /dev/null -w '%{http_code}\n'       # 200
sudo certbot renew --dry-run            # confirms /.well-known/ still works
```

## Not done (needs you)
- Oracle Cloud: in the VCN security list / NSG keep ingress only for 22, 80, 443 (remove any rule for 8000-8008 or 5173). Defence in depth; the compose change already closes them.
- Optional: `fail2ban` for repeat 404/444 offenders, and HSTS (`Strict-Transport-Security`) once you are sure the domain will stay HTTPS-only.
- If anything outside the VM calls `http://<vm-ip>:8000` directly, point it at `https://stockky.duckdns.org/api` instead. The workflows in `.github/workflows` were checked: none use a raw IP or port.

## Still open from the log review (not part of this group)
- Possible duplicate AngelOne feed thread (250-symbol then 491-symbol "background thread started").
- Afterhours scan picked `IT` and `ACE` (and maybe `OIL`) as symbols from news text.
