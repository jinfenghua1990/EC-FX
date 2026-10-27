#!/bin/sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
. "$ROOT/scripts/compose-lib.sh"
DUMP="${1:-}"

if [ -z "$DUMP" ] || [ ! -f "$DUMP" ]; then
  echo "用法: $0 /path/to/db_xxx.dump" >&2
  exit 2
fi

echo "[restore] 停止 app"
compose stop app || true
compose up -d db

echo "[restore] 恢复 $DUMP"
compose exec -T db pg_restore \
  -U recon -d reconciliation \
  --clean --if-exists --no-owner --no-privileges < "$DUMP"

echo "[restore] 数据库恢复完成"
