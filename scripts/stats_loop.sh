#!/usr/bin/env bash
# stats_loop.sh - fallback resource sampler (used by `vm_ops.sh sampler start` only when ~/stats_loop.sh does not exist).
#
#   bash scripts/stats_loop.sh                       # sample forever, one block per INTERVAL seconds (default 30) to stdout
#   bash scripts/stats_loop.sh --report [LOGFILE]    # peaks per container from a log this script wrote (default ~/stats_mon.log)
#
# Line format (easy to grep / paste):  <ISO time>|HOST|load1=<x>|mem_used_mib=<n>|mem_avail_mib=<n>|swap_used_mib=<n>
#                                      <ISO time>|<container>|cpu=<pct>|mem_mib=<n>
# Read-only: it only calls `free`, /proc/loadavg and `docker stats --no-stream`.
INTERVAL="${STATS_INTERVAL_S:-30}"

to_mib() {   # "123.4MiB" / "1.2GiB" / "800kB" -> MiB (integer)
  awk -v v="$1" 'BEGIN{ n=v+0; if (v ~ /GiB/) n*=1024; else if (v ~ /KiB|kB/) n/=1024; else if (v ~ /B$/ && v !~ /MiB/) n/=1048576; printf "%d", n }'
}

report() {
  local f="${1:-$HOME/stats_mon.log}"
  [[ -f "$f" ]] || { echo "no log at $f"; return 1; }
  echo "== peaks in $f =="
  awk -F'|' '
    $2=="HOST" { split($5,a,"="); split($4,u,"="); split($6,s,"="); if (u[2]+0>hu) {hu=u[2]+0; hut=$1}
                 if (a[2]!="" && (la==""||a[2]+0<la)) {la=a[2]+0; lat=$1}
                 if (s[2]+0>hs) hs=s[2]+0; n++ ; next }
    $2!="" && $3 ~ /^cpu=/ { split($4,m,"="); split($3,c,"=");
                 if (m[2]+0>mm[$2]) {mm[$2]=m[2]+0; mt[$2]=$1}
                 if (c[2]+0>cc[$2]) cc[$2]=c[2]+0 }
    END { printf "host: peak used %d MiB at %s, lowest available %d MiB at %s, peak swap %d MiB (%d samples)\n", hu, hut, la, lat, hs, n;
          for (k in mm) printf "%-28s peak mem %6d MiB at %s, peak cpu %.1f%%\n", k, mm[k], mt[k], cc[k] }' "$f" | sort
}

sample() {
  local ts load used avail swap
  ts="$(date -Is)"
  load="$(cut -d' ' -f1 /proc/loadavg)"
  read -r used avail <<<"$(free -m | awk '/^Mem:/{print $3, $7}')"
  swap="$(free -m | awk '/^Swap:/{print $3}')"
  echo "${ts}|HOST|load1=${load}|mem_used_mib=${used}|mem_avail_mib=${avail}|swap_used_mib=${swap}"
  if command -v docker >/dev/null 2>&1; then
    docker stats --no-stream --format '{{.Name}} {{.CPUPerc}} {{.MemUsage}}' 2>/dev/null | while read -r name cpu mem _; do
      echo "${ts}|${name}|cpu=${cpu%\%}|mem_mib=$(to_mib "${mem}")"
    done
  fi
}

if [[ "${1:-}" == "--report" ]]; then report "${2:-}"; exit $?; fi
if [[ "${1:-}" == "--once" ]]; then sample; exit 0; fi
while true; do sample; sleep "$INTERVAL"; done
