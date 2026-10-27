#!/bin/sh
set -u
PATH="/usr/local/bin:/usr/bin:/bin:/usr/syno/bin:/var/packages/ContainerManager/target/usr/bin:${PATH:-}"
export PATH

# 自复制，避免包模式回滚清理 scripts/ 时影响正在执行的脚本。
if [ "${ROLLBACK_SELF_ACTIVE:-0}" != "1" ]; then
  ROOT0="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
  TMP_SELF="/tmp/ecommerce-reconciliation-rollback.$$"
  cp "$0" "$TMP_SELF" || exit 1
  chmod 700 "$TMP_SELF" 2>/dev/null || true
  ROLLBACK_SELF_ACTIVE=1 ROOT_OVERRIDE="$ROOT0" exec sh "$TMP_SELF" "$@"
fi

set -eu
ROOT="${ROOT_OVERRIDE:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}"
META="$ROOT/.deploy/last_update.env"
YES=0
[ "${1:-}" = "--yes" ] && YES=1

if [ ! -f "$META" ]; then
  echo "没有找到可回滚的上一版本记录。" >&2
  exit 2
fi

# 该文件仅由更新脚本生成。
. "$META"
MODE="${UPDATE_MODE:-git}"

if [ "$YES" -ne 1 ]; then
  echo "回滚模式: $MODE"
  echo "数据库恢复到: ${PRE_UPDATE_DB_BACKUP:-未知}"
  printf '确认回滚? [y/N] '
  read ans
  [ "$ans" = "y" ] || [ "$ans" = "Y" ] || exit 0
fi

clean_managed_code(){
  for p in backend scripts migrations docs .github Dockerfile alembic.ini Makefile README.md VERSION update-manifest.json docker-compose.yml compose.synology.yml .dockerignore .gitignore .env.example; do
    rm -rf "$ROOT/$p"
  done
  rm -f "$ROOT"/V0.6-*.txt "$ROOT"/GITHUB-*.txt 2>/dev/null || true
}

cd "$ROOT"
if [ -x "$ROOT/scripts/backup.sh" ]; then
  echo "[rollback] 先备份当前数据库"
  "$ROOT/scripts/backup.sh" >/dev/null
fi

case "$MODE" in
  package)
    : "${PRE_UPDATE_CODE_BACKUP:?缺少 PRE_UPDATE_CODE_BACKUP}"
    : "${PRE_UPDATE_DB_BACKUP:?缺少 PRE_UPDATE_DB_BACKUP}"
    [ -f "$PRE_UPDATE_CODE_BACKUP" ] || { echo "找不到代码备份: $PRE_UPDATE_CODE_BACKUP" >&2; exit 3; }
    [ -f "$PRE_UPDATE_DB_BACKUP" ] || { echo "找不到数据库备份: $PRE_UPDATE_DB_BACKUP" >&2; exit 3; }

    # 先用当前脚本停止 app，再恢复旧代码和旧数据库。
    if command -v docker >/dev/null 2>&1 && [ -f "$ROOT/docker-compose.yml" ]; then
      docker compose -p ecommerce-reconciliation -f "$ROOT/docker-compose.yml" stop app || true
    fi
    clean_managed_code
    tar -xzf "$PRE_UPDATE_CODE_BACKUP" -C "$ROOT"
    cp -f "$ROOT/docker-compose.yml" "$ROOT/compose.yaml" || true

    "$ROOT/scripts/restore-db.sh" "$PRE_UPDATE_DB_BACKUP"
    docker compose -p ecommerce-reconciliation -f "$ROOT/docker-compose.yml" build app
    docker compose -p ecommerce-reconciliation -f "$ROOT/docker-compose.yml" up -d db app
    "$ROOT/scripts/wait-health.sh"
    mv -f "$META" "$ROOT/.deploy/last_rollback.env"
    echo "[rollback] 包模式回滚完成 -> ${PREVIOUS_VERSION:-上一版本}"
    ;;

  git|*)
    : "${PREVIOUS_COMMIT:?缺少 PREVIOUS_COMMIT}"
    : "${PRE_UPDATE_DB_BACKUP:?缺少 PRE_UPDATE_DB_BACKUP}"
    command -v git >/dev/null 2>&1 || { echo "Git 回滚需要 git。" >&2; exit 2; }
    [ -d "$ROOT/.git" ] || { echo "当前目录没有 .git，无法执行 Git 回滚。" >&2; exit 2; }
    "$ROOT/scripts/restore-db.sh" "$PRE_UPDATE_DB_BACKUP"
    git reset --hard "$PREVIOUS_COMMIT"
    cp -f "$ROOT/docker-compose.yml" "$ROOT/compose.yaml"
    docker compose -p ecommerce-reconciliation -f "$ROOT/docker-compose.yml" build app
    docker compose -p ecommerce-reconciliation -f "$ROOT/docker-compose.yml" up -d db app
    "$ROOT/scripts/wait-health.sh"
    mv -f "$META" "$ROOT/.deploy/last_rollback.env"
    echo "[rollback] Git 模式已回滚到 $PREVIOUS_COMMIT"
    ;;
esac
