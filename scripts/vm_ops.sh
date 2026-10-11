#!/usr/bin/env bash
# ============================================================
# Stockky VM ops helper - the "before Monday" checklist as commands. Run on the Oracle VM, from ~/stockky-v2.
#
#   bash scripts/vm_ops.sh checklist                 # one screen: what is done, what is still TODO
#   bash scripts/vm_ops.sh heartbeat [PING_URL]      # (re)install the dead-man's-switch cron; fixes the YOUR-UUID line
#   bash scripts/vm_ops.sh heartbeat-remove
#   bash scripts/vm_ops.sh sampler start|stop|status|report
#   bash scripts/vm_ops.sh backup-env                # encrypted copy of .env (then copy it OFF the VM) + chmod 600
#   bash scripts/vm_ops.sh security                  # read-only: sshd config, open ports, firewall, updates
#   bash scripts/vm_ops.sh schedule                  # cron: sampler 08:40 IST, checks 09:30 + 10:15 IST (Mon-Fri)
#   bash scripts/vm_ops.sh monday [0930|1015]        # run the live check now (label picked from the IST clock)
#
# Nothing here changes a service, the database or a trade. It only edits YOUR crontab (lines it can recognise by a
# `# stockky-` tag, or the broken YOUR-UUID line) and files under $HOME. PING_URL may also come from HC_PING_URL in the
# environment or in ./.env. Test hooks: CRONTAB_BIN, CURL_BIN, DRY_RUN=1 (prints the crontab it would install).
# ============================================================
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$REPO_DIR/.env}"
CRONTAB_BIN="${CRONTAB_BIN:-crontab}"
CURL_BIN="${CURL_BIN:-curl}"
HB_TAG="# stockky-heartbeat"
OPS_TAG="# stockky-ops"
HB_OK_FILE="${HB_OK_FILE:-$HOME/.stockky_hb_ok}"
SAMPLER_LOG="${SAMPLER_LOG:-$HOME/stats_mon.log}"

if [[ -t 1 ]]; then G=$'\033[0;32m'; R=$'\033[0;31m'; Y=$'\033[1;33m'; N=$'\033[0m'; else G=""; R=""; Y=""; N=""; fi
ok()   { echo "${G}[OK]${N}   $*"; }
bad()  { echo "${R}[FAIL]${N} $*"; }
todo() { echo "${Y}[TODO]${N} $*"; }
info() { echo "       $*"; }

current_cron() { "$CRONTAB_BIN" -l 2>/dev/null || true; }

# A ping URL is acceptable when it is https, has a host and a path, and is not a placeholder.
valid_ping_url() {
  local u="$1"
  [[ -n "$u" ]] || return 1
  [[ "$u" =~ ^https://[A-Za-z0-9.-]+/[^[:space:]]+$ ]] || return 1
  [[ "${u^^}" == *YOUR-UUID* || "${u^^}" == *"<UUID>"* || "${u^^}" == *UUID-HERE* || "${u^^}" == *XXXX* ]] && return 1
  # the hosted service uses a 36-character UUID after /; other hosts (self-hosted) only need a path
  if [[ "$u" =~ ^https://hc-ping\.com/ ]]; then
    [[ "$u" =~ ^https://hc-ping\.com/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(/[A-Za-z0-9_-]+)?$ ]] || return 1
  fi
  return 0
}

env_value() {   # env_value KEY -> value from the shell env, else from .env (no export, no eval, quotes stripped)
  local k="$1" v="${!1:-}"
  if [[ -z "$v" && -f "$ENV_FILE" ]]; then
    v="$(grep -E "^[[:space:]]*${k}=" "$ENV_FILE" | tail -1 | cut -d= -f2- | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e "s/^['\"]//" -e "s/['\"]\$//")"
  fi
  printf '%s' "$v"
}

install_cron() {   # reads the new crontab on stdin
  local new; new="$(cat)"
  if [[ "${DRY_RUN:-0}" == "1" ]]; then printf '%s\n' "$new"; return 0; fi
  printf '%s\n' "$new" | "$CRONTAB_BIN" -
}

# ---------------------------------------------------------------- heartbeat
cmd_heartbeat() {
  local url="${1:-}"
  [[ -z "$url" ]] && url="$(env_value HC_PING_URL)"
  if ! valid_ping_url "$url"; then
    bad "no usable ping URL. Create a check at healthchecks.io (period 5 min, grace 5-10 min), then run:"
    info "bash scripts/vm_ops.sh heartbeat https://hc-ping.com/<your-uuid>"
    info "(or put HC_PING_URL=... in .env). A placeholder such as YOUR-UUID is refused because it pings nothing."
    local cur; cur="$(current_cron)"
    if grep -q 'YOUR-UUID' <<<"$cur"; then
      bad "your crontab still has the placeholder line; it silently fails every 5 minutes and no alert would ever fire."
    fi
    return 1
  fi
  local keep line
  keep="$(current_cron | grep -v -F "$HB_TAG" | grep -v 'YOUR-UUID' | grep -v 'hc-ping.com/.*curl' || true)"
  line="*/5 * * * * $CURL_BIN -fsS -m 10 --retry 2 '$url' >/dev/null 2>&1 && touch \"\$HOME/.stockky_hb_ok\" $HB_TAG"
  { [[ -n "$keep" ]] && printf '%s\n' "$keep"; printf '%s\n' "$line"; } | install_cron || { bad "crontab install failed"; return 1; }
  [[ "${DRY_RUN:-0}" == "1" ]] && return 0
  ok "heartbeat cron installed (every 5 minutes)"
  if "$CURL_BIN" -fsS -m 10 --retry 2 "$url" >/dev/null 2>&1; then
    touch "$HB_OK_FILE"
    ok "test ping accepted - the check on healthchecks.io should turn green within a minute"
  else
    bad "test ping was REJECTED (wrong URL, no internet from the VM, or the check is paused). Fix the URL and run this again."
    return 1
  fi
  info "reminder: in healthchecks.io add your email / Telegram under Integrations, or the alert has nowhere to go."
}

cmd_heartbeat_remove() {
  current_cron | grep -v -F "$HB_TAG" | grep -v 'YOUR-UUID' | install_cron && ok "heartbeat cron lines removed"
}

hb_status() {
  local cur; cur="$(current_cron)"
  if grep -q 'YOUR-UUID' <<<"$cur"; then
    bad "heartbeat: crontab has the YOUR-UUID placeholder - NOT working. Run: bash scripts/vm_ops.sh heartbeat <ping-url>"; return 1
  fi
  if ! grep -q -F "$HB_TAG" <<<"$cur"; then
    if grep -q 'hc-ping.com' <<<"$cur"; then
      todo "heartbeat: an untagged hc-ping line exists; run 'heartbeat <ping-url>' once so it is managed and tracked"; return 1
    fi
    todo "heartbeat: not installed. Run: bash scripts/vm_ops.sh heartbeat <ping-url>"; return 1
  fi
  if [[ -f "$HB_OK_FILE" ]]; then
    local age=$(( $(date +%s) - $(stat -c %Y "$HB_OK_FILE" 2>/dev/null || echo 0) ))
    if (( age <= 900 )); then ok "heartbeat: installed, last accepted ping ${age}s ago"; return 0; fi
    bad "heartbeat: installed but the last accepted ping was ${age}s ago (cron stopped, or pings are rejected)"; return 1
  fi
  todo "heartbeat: installed, no accepted ping recorded yet (wait 5 minutes, then run again)"; return 1
}

# ---------------------------------------------------------------- sampler
sampler_pids() { pgrep -f 'stats_loop\.sh' 2>/dev/null || true; }
sampler_script() {
  if [[ -x "$HOME/stats_loop.sh" ]]; then echo "$HOME/stats_loop.sh"
  elif [[ -f "$HOME/stats_loop.sh" ]]; then echo "$HOME/stats_loop.sh"
  else echo "$REPO_DIR/scripts/stats_loop.sh"; fi
}
cmd_sampler() {
  local act="${1:-status}" pids n
  pids="$(sampler_pids)"; n=$(grep -c . <<<"$pids" || true)
  case "$act" in
    start)
      if (( n >= 1 )); then
        ok "sampler already running (pid $(tr '\n' ' ' <<<"$pids"))"
        (( n > 1 )) && bad "more than one sampler process; run 'sampler stop' then 'sampler start' (two writers double the numbers)"
        return 0
      fi
      local s; s="$(sampler_script)"
      [[ -f "$s" ]] || { bad "no sampler script at $s"; return 1; }
      nohup setsid bash "$s" >> "$SAMPLER_LOG" 2>&1 < /dev/null &
      sleep 1
      if [[ -n "$(sampler_pids)" ]]; then ok "sampler started ($s) -> $SAMPLER_LOG"; else bad "sampler exited at once; read $SAMPLER_LOG"; return 1; fi ;;
    stop)
      if (( n == 0 )); then ok "sampler not running"; return 0; fi
      # shellcheck disable=SC2086
      kill $pids 2>/dev/null; sleep 1
      [[ -z "$(sampler_pids)" ]] && ok "sampler stopped" || { bad "still running; kill -9 $(tr '\n' ' ' <<<"$(sampler_pids)")"; return 1; } ;;
    status)
      if (( n == 1 )); then ok "sampler running (pid $pids)"
      elif (( n == 0 )); then todo "sampler not running. Start before 08:45 IST: bash scripts/vm_ops.sh sampler start"; return 1
      else bad "sampler has $n processes ($(tr '\n' ' ' <<<"$pids")); expected exactly one"; return 1; fi
      if [[ -f "$SAMPLER_LOG" ]]; then
        local age=$(( $(date +%s) - $(stat -c %Y "$SAMPLER_LOG") ))
        (( age > 300 )) && bad "sampler log last written ${age}s ago - process may be hung" || info "log written ${age}s ago ($SAMPLER_LOG)"
      fi ;;
    report) bash "$REPO_DIR/scripts/stats_loop.sh" --report "${2:-$SAMPLER_LOG}" ;;
    *) echo "usage: sampler start|stop|status|report [logfile]"; return 2 ;;
  esac
}

# ---------------------------------------------------------------- .env backup
cmd_backup_env() {
  [[ -f "$ENV_FILE" ]] || { bad "no $ENV_FILE"; return 1; }
  local mode; mode="$(stat -c %a "$ENV_FILE")"
  if [[ "$mode" != "600" ]]; then chmod 600 "$ENV_FILE" && ok ".env permissions were $mode, now 600 (owner only)"; else ok ".env permissions 600"; fi
  local dir="$HOME/backups"; mkdir -p "$dir"; chmod 700 "$dir"
  local out="$dir/env-$(date +%Y%m%d-%H%M).enc"
  if command -v gpg >/dev/null 2>&1; then
    out="$out.gpg"; gpg --symmetric --cipher-algo AES256 --output "$out" "$ENV_FILE" || { bad "gpg failed"; return 1; }
  elif command -v openssl >/dev/null 2>&1; then
    openssl enc -aes-256-cbc -pbkdf2 -salt -in "$ENV_FILE" -out "$out" || { bad "openssl failed"; return 1; }
  else
    bad "neither gpg nor openssl found; copy .env by hand into a password manager"; return 1
  fi
  chmod 600 "$out"; ok "encrypted copy: $out"
  info "A backup that lives only on the VM is lost with the VM. From YOUR PC (PowerShell):"
  info "  scp -i \"D:\\Main Key Oracle Account\\ssh-key-2026-08-21.key\" ubuntu@<vm-ip>:$out ."
  info "decrypt later: gpg -d FILE > .env   (or: openssl enc -d -aes-256-cbc -pbkdf2 -in FILE -out .env)"
  info "Database backups are NOT covered here: turn on automatic backups in your database provider's console."
}

# ---------------------------------------------------------------- security audit (read only)
cmd_security() {
  local rc=0
  echo "== SSH =="
  command -v ssh >/dev/null && info "$(ssh -V 2>&1)"
  if command -v sshd >/dev/null 2>&1 || [[ -x /usr/sbin/sshd ]]; then
    if sudo -n /usr/sbin/sshd -t 2>/tmp/.sshd_t.$$; then ok "sshd config test passes (sshd -t)"; else
      if grep -qi 'password\|sudo' /tmp/.sshd_t.$$ 2>/dev/null; then todo "could not run sshd -t without a sudo password; run: sudo sshd -t"; else bad "sshd -t: $(head -c 200 /tmp/.sshd_t.$$)"; rc=1; fi
    fi; rm -f /tmp/.sshd_t.$$
    local eff; eff="$(sudo -n /usr/sbin/sshd -T 2>/dev/null | grep -Ei '^(passwordauthentication|permitrootlogin|pubkeyauthentication|port) ' || true)"
    if [[ -n "$eff" ]]; then
      while read -r k v; do info "$k $v"; done <<<"$eff"
      grep -qi '^passwordauthentication yes' <<<"$eff" && { bad "password login is ON - turn it off (key login only) once your key works"; rc=1; }
      grep -qi '^permitrootlogin yes' <<<"$eff" && { bad "root login over SSH is ON"; rc=1; }
    fi
  fi
  systemctl is-active --quiet ssh 2>/dev/null && ok "ssh service active" || { systemctl is-active --quiet sshd 2>/dev/null && ok "sshd service active" || { todo "could not confirm ssh service state"; }; }
  [[ -f /var/run/reboot-required ]] && todo "reboot required ($(tr '\n' ' ' </var/run/reboot-required.pkgs 2>/dev/null | head -c 120)) - pick a weekend, not market hours"
  echo "== Listening sockets (anything public besides 22/80/443 is wrong) =="
  if command -v ss >/dev/null 2>&1; then
    local bad_ports
    bad_ports="$(ss -H -tln 2>/dev/null | awk '{print $4}' | grep -Ev '^(127\.|\[::1\]|::1)' | sed 's/.*://' | sort -un | grep -Ev '^(22|80|443|53)$' || true)"
    if [[ -z "$bad_ports" ]]; then ok "only 22/80/443 (plus local DNS) listen on public addresses"; else bad "public listeners on port(s): $(tr '\n' ' ' <<<"$bad_ports") - bind to 127.0.0.1 or close them"; rc=1; fi
  else todo "ss not found"; fi
  if [[ -f "$REPO_DIR/scripts/check_compose_ports_loopback.py" ]]; then
    python3 "$REPO_DIR/scripts/check_compose_ports_loopback.py" >/dev/null 2>&1 && ok "docker-compose publishes every service on 127.0.0.1 only" || { bad "docker-compose has a port not bound to 127.0.0.1 (run scripts/check_compose_ports_loopback.py)"; rc=1; }
  fi
  echo "== Firewall / hardening =="
  if command -v ufw >/dev/null 2>&1; then info "ufw: $(sudo -n ufw status 2>/dev/null | head -1 || echo 'needs sudo')"; fi
  command -v fail2ban-client >/dev/null 2>&1 && ok "fail2ban installed" || todo "fail2ban not installed (optional): sudo apt install fail2ban"
  dpkg -s unattended-upgrades >/dev/null 2>&1 && ok "unattended-upgrades installed" || todo "unattended-upgrades missing: sudo apt install unattended-upgrades"
  [[ -f "$ENV_FILE" ]] && { [[ "$(stat -c %a "$ENV_FILE")" == "600" ]] && ok ".env is 600" || { bad ".env permissions are $(stat -c %a "$ENV_FILE"); run: chmod 600 .env"; rc=1; }; }
  echo "== Cannot be checked from inside the VM (do in the Oracle console) =="
  info "Networking > VCN > Security List: inbound ONLY TCP 22, 80, 443; limit 22 to your own IP (a /32); no 'all protocols' rule."
  info "Database: automatic backups ON. Test SSH from a SECOND window before closing the first one."
  return $rc
}

# ---------------------------------------------------------------- schedule (cron, IST -> server clock)
ist_to_cron() {   # ist_to_cron HH:MM -> "M H" on the server's clock; fails when it would cross midnight
  local hhmm="$1" ist_day srv_day mins
  ist_day="$(TZ=Asia/Kolkata date +%F)"
  srv_day="$(date -d "TZ=\"Asia/Kolkata\" $ist_day $hhmm" +%F 2>/dev/null)" || return 1
  [[ "$srv_day" == "$ist_day" ]] || return 1
  date -d "TZ=\"Asia/Kolkata\" $ist_day $hhmm" +"%-M %-H"
}
cmd_schedule() {
  local t1 t2 t3
  t1="$(ist_to_cron 08:40)" && t2="$(ist_to_cron 09:30)" && t3="$(ist_to_cron 10:15)" || {
    bad "this server's clock puts an IST time on another date; set the VM to UTC (sudo timedatectl set-timezone UTC) and retry"; return 1; }
  local keep; keep="$(current_cron | grep -v -F "$OPS_TAG" || true)"
  local dow="1-5"
  local m1 h1 m2 h2 m3 h3
  read -r m1 h1 <<<"$t1"; read -r m2 h2 <<<"$t2"; read -r m3 h3 <<<"$t3"
  {
    [[ -n "$keep" ]] && printf '%s\n' "$keep"
    printf '%s %s * * %s cd %s && bash scripts/vm_ops.sh sampler start >/dev/null 2>&1 %s\n' "$m1" "$h1" "$dow" "$REPO_DIR" "$OPS_TAG"
    printf '%s %s * * %s cd %s && python3 scripts/monday_check.py --label 0930 >/dev/null 2>&1 %s\n' "$m2" "$h2" "$dow" "$REPO_DIR" "$OPS_TAG"
    printf '%s %s * * %s cd %s && python3 scripts/monday_check.py --label 1015 >/dev/null 2>&1 %s\n' "$m3" "$h3" "$dow" "$REPO_DIR" "$OPS_TAG"
  } | install_cron || { bad "crontab install failed"; return 1; }
  [[ "${DRY_RUN:-0}" == "1" ]] && return 0
  ok "scheduled (server clock $(date +%Z)): sampler 08:40 IST, checks 09:30 and 10:15 IST, Mon-Fri"
  info "reports land in ~/check_logs/<date>_0930ist_live.log and _1015ist_live.log (your own 09:30 log, if you already have one, is not touched)"
}

# ---------------------------------------------------------------- monday / checklist
cmd_monday() {
  local label="${1:-}"
  if [[ -z "$label" ]]; then
    local hm; hm="$(TZ=Asia/Kolkata date +%H%M)"
    if (( 10#$hm < 1000 )); then label=0930; else label=1015; fi
  fi
  cd "$REPO_DIR" && python3 scripts/monday_check.py --label "$label"
}

cmd_checklist() {
  echo "== Before Monday's open =="
  hb_status || true
  cmd_sampler status || true
  if ls "$HOME"/backups/env-* >/dev/null 2>&1; then ok "an encrypted .env copy exists in ~/backups (make sure it is also OFF the VM)"; else todo ".env backup: bash scripts/vm_ops.sh backup-env, then scp it to your PC / password manager"; fi
  cur="$(current_cron)"
  grep -q -F "$OPS_TAG" <<<"$cur" && ok "09:30 / 10:15 checks scheduled" || todo "schedule the live checks: bash scripts/vm_ops.sh schedule"
  echo "== Needs you (cannot be done from here) =="
  info "1. second-window SSH test after the OpenSSH upgrade; 2. Oracle security list (22/80/443 only, 22 to your IP);"
  info "3. database automatic backups ON; 4. email/Telegram integration on the healthchecks.io check."
  echo "== Security audit =="
  cmd_security || true
}

main() {
  local c="${1:-checklist}"; shift || true
  case "$c" in
    checklist) cmd_checklist ;;
    heartbeat) cmd_heartbeat "$@" ;;
    heartbeat-remove) cmd_heartbeat_remove ;;
    sampler) cmd_sampler "$@" ;;
    backup-env) cmd_backup_env ;;
    security) cmd_security ;;
    schedule) cmd_schedule ;;
    monday) cmd_monday "$@" ;;
    -h|--help|help) sed -n '2,20p' "$0" ;;
    *) echo "unknown command: $c (try: help)"; return 2 ;;
  esac
}
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
