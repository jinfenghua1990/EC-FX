#!/bin/sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
. "$ROOT/scripts/compose-lib.sh"
BACKUP_DIR="${BACKUP_DIR:-$ROOT/backups}"
KEEP="${BACKUP_KEEP:-14}"
TS="$(date +%Y%m%d_%H%M%S)"
DB_FILE="$BACKUP_DIR/db_$TS.dump"
DATA_FILE="$BACKUP_DIR/data_$TS.tar.gz"
QUIET=0

[ "${1:-}" = "--print-path" ] && QUIET=1
mkdir -p "$BACKUP_DIR"

if [ "$QUIET" -eq 0 ]; then echo "[backup] PostgreSQL -> $DB_FILE"; fi
compose exec -T db pg_dump -U recon -d reconciliation -Fc > "$DB_FILE"

if [ -d "$ROOT/data" ]; then
  if [ "$QUIET" -eq 0 ]; then echo "[backup] data/ -> $DATA_FILE"; fi
  tar -czf "$DATA_FILE" -C "$ROOT" data
fi

# 只保留最近 N 份数据库和 data 备份。
ls -1t "$BACKUP_DIR"/db_*.dump 2>/dev/null | awk "NR>$KEEP" | xargs -r rm -f
ls -1t "$BACKUP_DIR"/data_*.tar.gz 2>/dev/null | awk "NR>$KEEP" | xargs -r rm -f

if [ "$QUIET" -eq 1 ]; then
  printf '%s\n' "$DB_FILE"
else
  echo "[backup] 完成"
  echo "$DB_FILE"
fi
