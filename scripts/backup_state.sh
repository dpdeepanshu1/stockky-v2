#!/usr/bin/env bash
# ============================================================
# Stockky nightly backup + restore test (F33). Run on the Oracle VM, from ~/stockky-v2.
#
#   bash scripts/backup_state.sh run                 # back up the state volumes (+ the Postgres dump when DATABASE_URL is set), then verify what it wrote
#   bash scripts/backup_state.sh verify [DIR]        # check the newest (or DIR) backup against its manifest; does not touch any volume
#   bash scripts/backup_state.sh restore-test        # restore the newest backup of every volume into a SCRATCH volume (<name>-restoretest), compare, delete the scratch
#   bash scripts/backup_state.sh restore VOLUME FILE # restore one archive into a NEW volume named VOLUME-restored (never overwrites a live volume)
#   bash scripts/backup_state.sh list
#   bash scripts/backup_state.sh schedule            # cron: backup 02:30 IST daily, restore-test Sunday 03:00 IST (own `# stockky-backup` lines only)
#   bash scripts/backup_state.sh schedule-remove
#
# What is saved (docker volumes, project prefix = $COMPOSE_PROJECT_NAME, default the folder name): control-state-rt, control-state-ps (the Calibration page switches),
# runner-data (calibration runs), bar-records (1-minute bars), plus `pg_dump` of $DATABASE_URL (from ./.env). Caches that rebuild themselves (scrip-cache-*, HF model
# caches) are NOT saved. Output: $BACKUP_DIR/<UTC date>/ with one .tar.gz per volume, the dump, and MANIFEST.sha256. Older than $KEEP_DAYS (14) days are deleted.
# A backup that sits on the same VM dies with the VM: copy $BACKUP_DIR off the VM too (set OFFSITE_CMD, e.g. "rclone copy {dir} remote:stockky", to do it after each run).
#
# Nothing here changes a service, a trade or a live volume. Test hooks: DOCKER_BIN, CRONTAB_BIN, PGDUMP_BIN, BACKUP_DIR, ENV_FILE, DRY_RUN=1.
# ============================================================
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$REPO_DIR/.env}"
DOCKER_BIN="${DOCKER_BIN:-docker}"
CRONTAB_BIN="${CRONTAB_BIN:-crontab}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/backups/stockky}"
KEEP_DAYS="${KEEP_DAYS:-14}"
PROJECT="${COMPOSE_PROJECT_NAME:-$(basename "$REPO_DIR" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')}"
VOLUMES=(control-state-rt control-state-ps runner-data bar-records)
IMG="${BACKUP_IMAGE:-alpine:3.20}"
TAG="# stockky-backup"
FAILS=0

if [[ -t 1 ]]; then G=$'\033[0;32m'; R=$'\033[0;31m'; Y=$'\033[1;33m'; N=$'\033[0m'; else G=""; R=""; Y=""; N=""; fi
ok()   { echo "${G}[OK]${N}   $*"; }
bad()  { echo "${R}[FAIL]${N} $*"; FAILS=$((FAILS+1)); }
todo() { echo "${Y}[TODO]${N} $*"; }
info() { echo "       $*"; }

env_get() { [[ -f "$ENV_FILE" ]] && grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' || true; }
vol_name() { echo "${PROJECT}_$1"; }
vol_exists() { "$DOCKER_BIN" volume inspect "$1" >/dev/null 2>&1; }
latest_dir() { ls -1d "$BACKUP_DIR"/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]* 2>/dev/null | sort | tail -1; }

backup_volume() {   # $1 short name, $2 out dir
  local v; v="$(vol_name "$1")"
  if ! vol_exists "$v"; then info "$1: volume $v does not exist here, skipped"; return 0; fi
  if "$DOCKER_BIN" run --rm -v "$v:/src:ro" -v "$2:/out" "$IMG" sh -c "tar czf /out/$1.tar.gz -C /src ." >/dev/null 2>&1 && [[ -s "$2/$1.tar.gz" ]]; then
    ok "$1 saved ($(du -h "$2/$1.tar.gz" | cut -f1))"
  else
    bad "$1: the archive could not be written"; rm -f "$2/$1.tar.gz"
  fi
}

backup_postgres() {   # $1 out dir
  local url; url="${DATABASE_URL:-$(env_get DATABASE_URL)}"
  if [[ -z "$url" ]]; then todo "DATABASE_URL is empty: no database dump (the local sqlite files live in the volumes above, or the provider's own backups apply)"; return 0; fi
  local out="$1/postgres.dump"
  if [[ -n "${PGDUMP_BIN:-}" ]]; then "$PGDUMP_BIN" --format=custom --no-owner "$url" >"$out" 2>"$1/postgres.err"
  elif command -v pg_dump >/dev/null 2>&1; then pg_dump --format=custom --no-owner "$url" >"$out" 2>"$1/postgres.err"
  else "$DOCKER_BIN" run --rm -i postgres:16-alpine pg_dump --format=custom --no-owner "$url" >"$out" 2>"$1/postgres.err"; fi
  if [[ -s "$out" ]]; then ok "postgres dumped ($(du -h "$out" | cut -f1))"; rm -f "$1/postgres.err"
  else bad "postgres dump failed: $(head -c 200 "$1/postgres.err" | tr '\n' ' ' | sed -E 's#(postgres(ql)?://[^:]+:)[^@]*@#\1***@#g')"; rm -f "$out"; fi
}

write_manifest() { ( cd "$1" && sha256sum ./*.tar.gz ./*.dump 2>/dev/null > MANIFEST.sha256 ); }

verify_dir() {   # $1 dir -> checks checksums and that every archive lists
  local d="$1"
  [[ -f "$d/MANIFEST.sha256" ]] || { bad "$d: no MANIFEST.sha256"; return; }
  [[ -s "$d/MANIFEST.sha256" ]] || { bad "$d: the manifest is empty (nothing was backed up)"; return; }
  if ( cd "$d" && sha256sum -c MANIFEST.sha256 >/dev/null 2>&1 ); then ok "$(basename "$d"): checksums match"; else bad "$(basename "$d"): a checksum does not match (file damaged or changed)"; return; fi
  local f
  for f in "$d"/*.tar.gz; do [[ -e "$f" ]] || continue; tar tzf "$f" >/dev/null 2>&1 && ok "$(basename "$f") reads back" || bad "$(basename "$f") is not a readable archive"; done
  if [[ -s "$d/postgres.dump" ]] && command -v pg_restore >/dev/null 2>&1; then pg_restore --list "$d/postgres.dump" >/dev/null 2>&1 && ok "postgres.dump reads back" || bad "postgres.dump is not a readable dump"; fi
}

cmd_run() {
  local day dir; day="$(date -u +%F)"; dir="$BACKUP_DIR/$day"
  mkdir -p "$dir" || { bad "cannot create $dir"; return 1; }
  chmod 700 "$BACKUP_DIR" "$dir" 2>/dev/null || true
  local v; for v in "${VOLUMES[@]}"; do backup_volume "$v" "$dir"; done
  backup_postgres "$dir"
  write_manifest "$dir"
  verify_dir "$dir"
  find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -name '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*' -mtime +"$KEEP_DAYS" -exec rm -rf {} + 2>/dev/null || true
  if [[ -n "${OFFSITE_CMD:-}" ]]; then
    if eval "${OFFSITE_CMD//\{dir\}/$dir}" >/dev/null 2>&1; then ok "copied off the VM (OFFSITE_CMD)"; else bad "OFFSITE_CMD failed: the backup is only on this VM"; fi
  else todo "OFFSITE_CMD is not set: this backup lives on the same VM. Copy $BACKUP_DIR off it (scp / rclone)."; fi
  [[ $FAILS -eq 0 ]]
}

cmd_verify() { local d="${1:-$(latest_dir)}"; [[ -n "$d" && -d "$d" ]] || { bad "no backup found in $BACKUP_DIR"; return 1; }; verify_dir "$d"; [[ $FAILS -eq 0 ]]; }

cmd_list() { ls -1d "$BACKUP_DIR"/[0-9]*/ 2>/dev/null | while read -r d; do echo "$(basename "$d")  $(du -sh "$d" | cut -f1)  $(ls "$d" | tr '\n' ' ')"; done; }

restore_into() {   # $1 archive $2 new volume name
  if vol_exists "$2"; then bad "$2 already exists: choose another name (restore never overwrites a volume)"; return 1; fi
  "$DOCKER_BIN" volume create "$2" >/dev/null 2>&1 || { bad "cannot create volume $2"; return 1; }
  "$DOCKER_BIN" run --rm -v "$2:/dst" -v "$(dirname "$1"):/in:ro" "$IMG" sh -c "tar xzf /in/$(basename "$1") -C /dst" >/dev/null 2>&1
}

cmd_restore() {
  local v="${1:-}" f="${2:-}"
  [[ -n "$v" && -f "$f" ]] || { echo "usage: restore VOLUME-SHORT-NAME ARCHIVE.tar.gz   (creates ${PROJECT}_VOLUME-restored)"; return 2; }
  local target; target="$(vol_name "$v")-restored"
  restore_into "$f" "$target" && ok "restored into volume $target. To use it: stop the service, then copy it over the live volume yourself (nothing was overwritten)."
  [[ $FAILS -eq 0 ]]
}

cmd_restore_test() {
  local d; d="$(latest_dir)"; [[ -n "$d" ]] || { bad "no backup found in $BACKUP_DIR"; return 1; }
  info "restore test from $(basename "$d")"
  local v f scratch want got
  for v in "${VOLUMES[@]}"; do
    f="$d/$v.tar.gz"; [[ -f "$f" ]] || { info "$v: not in this backup, skipped"; continue; }
    scratch="$(vol_name "$v")-restoretest"
    "$DOCKER_BIN" volume rm -f "$scratch" >/dev/null 2>&1 || true
    if ! restore_into "$f" "$scratch"; then bad "$v: restore failed"; continue; fi
    want="$(tar tzf "$f" | grep -v '/$' | sed 's#^\./##' | sort | grep -c . || true)"
    got="$("$DOCKER_BIN" run --rm -v "$scratch:/dst:ro" "$IMG" sh -c "cd /dst && find . -type f | wc -l" 2>/dev/null | tr -d ' ')"
    [[ "$want" == "$got" ]] && ok "$v: restored $got file(s), same as the archive" || bad "$v: archive has $want file(s), the restored copy has ${got:-?}"
    "$DOCKER_BIN" volume rm -f "$scratch" >/dev/null 2>&1 || true
  done
  [[ $FAILS -eq 0 ]]
}

cmd_schedule() {
  local cur new; cur="$("$CRONTAB_BIN" -l 2>/dev/null || true)"
  new="$(grep -v -F "$TAG" <<<"$cur" || true)"
  new+=$'\n'"CRON_TZ=Asia/Kolkata $TAG"
  new+=$'\n'"30 2 * * * cd $REPO_DIR && bash scripts/backup_state.sh run >> \$HOME/backup_state.log 2>&1 $TAG"
  new+=$'\n'"0 3 * * 0 cd $REPO_DIR && bash scripts/backup_state.sh restore-test >> \$HOME/backup_state.log 2>&1 $TAG"
  if [[ "${DRY_RUN:-0}" == "1" ]]; then echo "$new"; return 0; fi
  echo "$new" | "$CRONTAB_BIN" - && ok "scheduled: backup 02:30 IST daily, restore test Sunday 03:00 IST (log: ~/backup_state.log)"
}

cmd_schedule_remove() {
  local cur; cur="$("$CRONTAB_BIN" -l 2>/dev/null || true)"
  grep -v -F "$TAG" <<<"$cur" | "$CRONTAB_BIN" - && ok "backup cron lines removed"
}

main() {
  local c="${1:-}"; shift || true
  case "$c" in
    run) cmd_run ;;
    verify) cmd_verify "$@" ;;
    restore-test) cmd_restore_test ;;
    restore) cmd_restore "$@" ;;
    list) cmd_list ;;
    schedule) cmd_schedule ;;
    schedule-remove) cmd_schedule_remove ;;
    -h|--help|help|"") sed -n '2,22p' "$0" ;;
    *) echo "unknown command: $c (try: help)"; return 2 ;;
  esac
}
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
