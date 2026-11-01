#!/usr/bin/env bash
# E277: one read-only pass on the VM HOST (run from the repo folder, next to docker-compose.yml). Changes nothing, reads no token.
#   bash scripts/e277_vm_check.sh
cd "$(dirname "$0")/.." || exit 2
ok(){ echo "PASS $*"; }; bad(){ echo "FAIL $*"; }; warn(){ echo "WARN $*"; }
# E285 round 124: ask one service for one route from INSIDE its container and keep asking until it answers (E277_WAIT_S seconds, default 90, every E277_POLL_S, default 5).
# A service started a few seconds earlier is not listening yet; the first answer was `ERR URLError` and looked like a fault. Every line names its service.
#   probe <service> <port> <path>      any HTTP answer below 500 counts as "answering" (401 = the route exists and wants the admin login)
probe(){
  local svc="$1" port="$2" path="$3" wait="${E277_WAIT_S:-90}" poll="${E277_POLL_S:-5}" t0=$SECONDS r last="no answer yet"
  while :; do
    r=$(docker compose exec -T "$svc" python -c "import urllib.request,urllib.error
try: print('OK',urllib.request.urlopen('http://localhost:$port$path',timeout=15).status)
except urllib.error.HTTPError as e: print('HTTP',e.code)
except Exception as e: print('ERR',type(e).__name__)" 2>/dev/null | tr -d '\r' | tail -1)
    case "$r" in
      "OK "*)   ok "$svc :$port$path answered ${r#OK }"; return 0 ;;
      "HTTP "*) if [ "${r#HTTP }" -lt 500 ] 2>/dev/null; then ok "$svc :$port$path answered HTTP ${r#HTTP } (the route exists; 401 = needs the admin login)"; return 0; fi
                last="$r" ;;
      "ERR "*)  last="$r (not listening yet?)" ;;
      *)        last="the container did not run the check (is it up? docker compose ps $svc)" ;;
    esac
    if [ $((SECONDS - t0)) -ge "$wait" ]; then break; fi
    sleep "$poll"
  done
  bad "$svc :$port$path gave no answer in ${wait}s - last: $last. Look: docker compose ps $svc ; docker compose logs --tail 40 $svc"
  return 1
}
[ "${E277_FUNCS_ONLY:-0}" = 1 ] && { return 0 2>/dev/null || exit 0; }
echo "== 1. is the E277 code in the running containers?"
n=$(docker compose exec -T position-stocks-service grep -c '/premarket/plan' main.py 2>/dev/null | tr -d '\r')
[ "${n:-0}" -ge 1 ] && ok "position-stocks has GET /premarket/plan" || bad "position-stocks has no /premarket/plan (rebuild: docker compose up -d --build position-stocks-service)"
if docker compose exec -T frontend sh -c "grep -l 'Setup ladder' /usr/share/nginx/html/assets/*.js" >/dev/null 2>&1; then ok "served frontend contains the Setup ladder"; else bad "served frontend is the OLD build (docker compose build --no-cache frontend && docker compose up -d frontend)"; fi
echo "== 2. effective switches"
docker compose exec -T position-stocks-service python - <<'PY'
import config
want={"MOMENTUM_EXIT_MODE":"live","ORB_MODE":"live","NO_SETUP_MODE":"off","PREMARKET_PLAN_ENABLED":True,"SLOT_LADDER_ENABLED":True,"EVENT_CANDIDATES_ENABLED":True,"SHORT_MODE":"live","MARKET_STATE_MODE":"live"}
for k,v in want.items():
    g=getattr(config,k,"n/a"); print(("PASS" if g==v else "FAIL"),"PS",k,g,"(want",v,")")
PY
docker compose exec -T real-trade-service python - <<'PY'
import config
want={"SWING_MODE":"live","SWING_MAX_POSITIONS":3,"SWING_PICKS_PER_DAY":3,"REGIME_RULES_MODE":"live","NO_SETUP_MODE":"off","SETUP_S2_ENABLED":True,"SETUP_S3_ENABLED":True}
for k,v in want.items():
    g=getattr(config,k,"n/a"); print(("PASS" if g==v else "FAIL"),"RT",k,g,"(want",v,")")
PY
echo "== 3. leftover env keys (should print nothing)"
grep -nE "^[[:space:]]*(CANDIDATE_52W|SWING_SHADOW_PICKS_PER_DAY|SWING_MODE=shadow)" .env || ok "no stale keys in .env"
echo "== 4. files the release no longer contains (stale tests break the suites; read before deleting)"
python3 scripts/check_stale_files.py 2>&1 | tail -30
echo "== 5. E276 database checks"
python3 scripts/e276_check.py 2>&1 | tail -40
echo "== 6. services answering (Real Trade is on 8005, Position Stocks on 8006; 401 on a token route is fine; each one is retried for up to ${E277_WAIT_S:-90}s after a restart)"
probe real-trade-service 8005 /health
probe position-stocks-service 8006 /premarket/plan
