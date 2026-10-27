#!/bin/sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
. "$ROOT/scripts/compose-lib.sh"
DEPLOY_DIR="$ROOT/.deploy"
BRANCH="${UPDATE_BRANCH:-main}"
LOCK="$DEPLOY_DIR/update.lock"
mkdir -p "$DEPLOY_DIR" "$ROOT/backups" "$ROOT/data"

if ! command -v git >/dev/null 2>&1; then
  echo "找不到 git。请先在群晖安装 Git / Git Server。" >&2
  exit 2
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "找不到 docker。请确认 Container Manager 已安装。" >&2
  exit 2
fi

if [ ! -d "$ROOT/.git" ]; then
  echo "当前目录还没有连接 GitHub。先执行 scripts/bootstrap-git.sh <GitHub仓库URL>。" >&2
  exit 2
fi

if ! mkdir "$LOCK" 2>/dev/null; then
  echo "已有更新任务正在运行。" >&2
  exit 3
fi
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT INT TERM

cd "$ROOT"
if ! git diff --quiet -- . ':(exclude).env' ':(exclude)data' ':(exclude)backups' ':(exclude)compose.yaml'; then
  echo "检测到本地代码修改，停止自动更新，避免覆盖。" >&2
  git status --short >&2
  exit 4
fi

PREV="$(git rev-parse HEAD)"
echo "[update] 当前 commit: $PREV"
git fetch --prune origin "$BRANCH"
TARGET="$(git rev-parse "origin/$BRANCH")"
if [ "$PREV" = "$TARGET" ] && [ "${1:-}" != "--force" ]; then
  echo "[update] 已是最新版本，无需更新。"
  exit 0
fi

echo "[update] 目标 commit: $TARGET"
DB_BACKUP="$($ROOT/scripts/backup.sh --print-path | tail -n 1)"
DATE="$(date -Iseconds 2>/dev/null || date)"
cat > "$DEPLOY_DIR/pending.env" <<META
PREVIOUS_COMMIT=$PREV
TARGET_COMMIT=$TARGET
PRE_UPDATE_DB_BACKUP=$DB_BACKUP
UPDATE_STARTED_AT=$DATE
META

rollback_failed_update() {
  echo "[update] 新版本失败，自动恢复升级前数据库和代码" >&2
  compose stop app || true
  "$ROOT/scripts/restore-db.sh" "$DB_BACKUP" || true
  git reset --hard "$PREV" || true
  cp -f "$ROOT/docker-compose.yml" "$ROOT/compose.yaml" || true
  compose build app || true
  compose up -d db app || true
  "$ROOT/scripts/wait-health.sh" || true
}
trap 'code=$?; if [ "$code" -ne 0 ]; then rollback_failed_update; fi; rmdir "$LOCK" 2>/dev/null || true; exit "$code"' EXIT INT TERM

git merge --ff-only "origin/$BRANCH"
cp -f "$ROOT/docker-compose.yml" "$ROOT/compose.yaml"

# 先构建镜像。依赖下载只发生在 build，不发生在每次容器启动。
echo "[update] docker build"
compose build app
compose up -d db

# 在切流前执行数据库迁移；失败会自动恢复升级前备份。
echo "[update] database migration"
compose run --rm --no-deps app python /app/scripts/migrate.py

echo "[update] restart app"
compose up -d --no-deps app
"$ROOT/scripts/wait-health.sh"

NEW="$(git rev-parse HEAD)"
FINISHED="$(date -Iseconds 2>/dev/null || date)"
cat > "$DEPLOY_DIR/last_update.env" <<META
UPDATE_MODE=git
CURRENT_COMMIT=$NEW
PREVIOUS_COMMIT=$PREV
PRE_UPDATE_DB_BACKUP=$DB_BACKUP
UPDATE_FINISHED_AT=$FINISHED
META
printf '%s\t%s\t%s\t%s\n' "$FINISHED" "$PREV" "$NEW" "$DB_BACKUP" >> "$DEPLOY_DIR/history.tsv"
rm -f "$DEPLOY_DIR/pending.env"
trap - EXIT INT TERM
rmdir "$LOCK" 2>/dev/null || true

echo "[update] 更新成功: $PREV -> $NEW"
