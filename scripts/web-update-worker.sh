#!/bin/sh
set -u
PATH="/usr/local/bin:/usr/bin:/bin:/usr/syno/bin:/var/packages/ContainerManager/target/usr/bin:${PATH:-}"
export PATH

# 群晖任务计划每分钟执行一次。Web 只写 data/system-update/request.env；
# Docker / 数据库 / 代码替换始终由宿主机任务执行，Web 容器不挂 docker.sock。
# 先把 worker 自身复制到 /tmp，确保更新时可以安全替换 scripts/ 目录。
if [ "${WEB_UPDATE_WORKER_ACTIVE:-0}" != "1" ]; then
  ROOT0="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
  TMP_SELF="/tmp/ecommerce-reconciliation-web-update-worker.$$"
  cp "$0" "$TMP_SELF" || exit 1
  chmod 700 "$TMP_SELF" 2>/dev/null || true
  WEB_UPDATE_WORKER_ACTIVE=1 ROOT_OVERRIDE="$ROOT0" exec sh "$TMP_SELF"
fi

set -eu
ROOT="${ROOT_OVERRIDE:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}"
STATE_DIR="$ROOT/data/system-update"
REQUEST="$STATE_DIR/request.env"
STATUS="$STATE_DIR/status.env"
LOG="$STATE_DIR/update.log"
WORKER_LOCK="$STATE_DIR/worker.lock"
mkdir -p "$STATE_DIR" "$ROOT/backups" "$ROOT/.deploy"

[ -f "$REQUEST" ] || exit 0
if ! mkdir "$WORKER_LOCK" 2>/dev/null; then exit 0; fi
trap 'rmdir "$WORKER_LOCK" 2>/dev/null || true' EXIT INT TERM

REQUEST_ID="$(sed -n 's/^REQUEST_ID=//p' "$REQUEST" | head -n1)"
ACTION="$(sed -n 's/^ACTION=//p' "$REQUEST" | head -n1)"
PACKAGE_DIR="$(sed -n 's/^PACKAGE_DIR=//p' "$REQUEST" | head -n1)"
PACKAGE_VERSION="$(sed -n 's/^PACKAGE_VERSION=//p' "$REQUEST" | head -n1)"
[ -n "$REQUEST_ID" ] || exit 0

safe_text(){ printf '%s' "$1" | tr '\r\n' '  ' | cut -c1-1000; }
now(){ date -Iseconds 2>/dev/null || date '+%Y-%m-%dT%H:%M:%S%z'; }
current_version(){ tr -d '\r\n' < "$ROOT/VERSION" 2>/dev/null || printf 'unknown'; }
write_status(){
  STATE="$1"; MESSAGE="$2"; HAS_UPDATE="${3:-}"; CURRENT_COMMIT="${4:-}"; REMOTE_COMMIT="${5:-}"; MODE="${6:-}"
  TMP="$STATUS.tmp.$$"
  {
    echo "REQUEST_ID=$(safe_text "$REQUEST_ID")"
    echo "ACTION=$(safe_text "$ACTION")"
    echo "STATE=$(safe_text "$STATE")"
    echo "MESSAGE=$(safe_text "$MESSAGE")"
    echo "UPDATED_AT=$(now)"
    echo "PACKAGE_VERSION=$(safe_text "$PACKAGE_VERSION")"
    echo "CURRENT_VERSION=$(safe_text "$(current_version)")"
    echo "CURRENT_COMMIT=$(safe_text "$CURRENT_COMMIT")"
    echo "REMOTE_COMMIT=$(safe_text "$REMOTE_COMMIT")"
    echo "HAS_UPDATE=$(safe_text "$HAS_UPDATE")"
    echo "MODE=$(safe_text "$MODE")"
  } > "$TMP"
  mv -f "$TMP" "$STATUS"
}
finish_request(){ rm -f "$REQUEST"; }
run_logged(){ "$@" >> "$LOG" 2>&1; }

# 这些路径属于发布包。更新时先清理再复制，避免旧 Python/静态文件残留。
# .env / data / backups / .deploy / .git 永远不在这里。
clean_managed_code(){
  for p in backend scripts migrations docs .github Dockerfile alembic.ini Makefile README.md VERSION update-manifest.json docker-compose.yml compose.synology.yml .dockerignore .gitignore .env.example; do
    rm -rf "$ROOT/$p"
  done
  # 清理早期版本遗留的说明文本，不碰用户数据。
  rm -f "$ROOT"/V0.6-*.txt "$ROOT"/GITHUB-*.txt 2>/dev/null || true
}

write_status running "正在处理系统更新任务" "" "" "" "$ACTION"
printf '\n[%s] action=%s request=%s\n' "$(now)" "$ACTION" "$REQUEST_ID" >> "$LOG"

case "$ACTION" in
  # V0.6 旧请求兼容入口。V0.8+ 在线检查/下载由应用内 GitHub API 完成。
  check|update)
    write_status error "当前版本已使用 GitHub API 在线更新；请刷新网页后重新点击检查/下载安装。" "" "" "" "legacy"
    finish_request
    ;;

  rollback)
    if run_logged "$ROOT/scripts/rollback.sh" --yes; then
      write_status success "已回滚到上一版本。" "" "" "" "rollback"
    else
      write_status error "回滚失败，请查看更新日志。" "" "" "" "rollback"
    fi
    finish_request
    ;;

  install_package)
    case "$PACKAGE_DIR" in staging/*) ;; *) write_status error "更新包路径校验失败。" "" "" "" "package"; finish_request; exit 0;; esac
    STAGE="$STATE_DIR/$PACKAGE_DIR"
    if [ ! -d "$STAGE" ] || [ ! -f "$STAGE/update-manifest.json" ]; then
      write_status error "找不到已上传的更新包。" "" "" "" "package"; finish_request; exit 0
    fi
    for forbidden in .env data backups .git compose.yaml; do
      if [ -e "$STAGE/$forbidden" ]; then
        write_status error "更新包包含禁止覆盖的运行数据：$forbidden" "" "" "" "package"; finish_request; exit 0
      fi
    done
    if [ ! -f "$STAGE/VERSION" ] || [ "$(tr -d '\r\n' < "$STAGE/VERSION")" != "$PACKAGE_VERSION" ]; then
      write_status error "更新包 VERSION 与待安装版本不一致。" "" "" "" "package"; finish_request; exit 0
    fi

    PREVIOUS_VERSION="$(current_version)"
    TS="$(date '+%Y%m%d_%H%M%S')"
    CODE_BACKUP="$ROOT/backups/code_before_web_update_$TS.tar.gz"
    echo "[package] backup code -> $CODE_BACKUP" >> "$LOG"
    if ! tar -czf "$CODE_BACKUP" -C "$ROOT" \
      --exclude='./data' --exclude='./backups' --exclude='./.env' --exclude='./.git' --exclude='./.deploy' --exclude='./compose.yaml' \
      . >> "$LOG" 2>&1; then
      write_status error "代码备份失败，已停止更新。" "" "" "" "package"; finish_request; exit 0
    fi

    DB_BACKUP=""
    if [ -x "$ROOT/scripts/backup.sh" ]; then
      DB_BACKUP="$("$ROOT/scripts/backup.sh" --print-path 2>>"$LOG" | tail -n1 || true)"
    fi
    if [ -z "$DB_BACKUP" ] || [ ! -f "$DB_BACKUP" ]; then
      write_status error "数据库备份失败，已停止更新。" "" "" "" "package"; finish_request; exit 0
    fi

    restore_package_failure(){
      echo "[package] failure -> clean new code, restore old code and db" >> "$LOG"
      clean_managed_code || true
      tar -xzf "$CODE_BACKUP" -C "$ROOT" >> "$LOG" 2>&1 || true
      if [ -n "$DB_BACKUP" ] && [ -x "$ROOT/scripts/restore-db.sh" ]; then
        "$ROOT/scripts/restore-db.sh" "$DB_BACKUP" >> "$LOG" 2>&1 || true
      fi
      cp -f "$ROOT/docker-compose.yml" "$ROOT/compose.yaml" 2>/dev/null || true
      if command -v docker >/dev/null 2>&1 && [ -f "$ROOT/docker-compose.yml" ]; then
        cd "$ROOT"
        docker compose -p ecommerce-reconciliation -f "$ROOT/docker-compose.yml" build app >> "$LOG" 2>&1 || true
        docker compose -p ecommerce-reconciliation -f "$ROOT/docker-compose.yml" up -d db app >> "$LOG" 2>&1 || true
        [ -x "$ROOT/scripts/wait-health.sh" ] && "$ROOT/scripts/wait-health.sh" >> "$LOG" 2>&1 || true
      fi
    }

    echo "[package] clean managed code" >> "$LOG"
    clean_managed_code
    echo "[package] apply staged files: $STAGE" >> "$LOG"
    if ! cp -a "$STAGE/." "$ROOT/" >> "$LOG" 2>&1; then
      restore_package_failure
      write_status error "复制更新文件失败，已恢复旧版本。" "" "" "" "package"; finish_request; exit 0
    fi
    cp -f "$ROOT/docker-compose.yml" "$ROOT/compose.yaml" || true
    chmod +x "$ROOT"/scripts/*.sh "$ROOT"/scripts/migrate.py 2>/dev/null || true

    cd "$ROOT"
    if ! docker compose -p ecommerce-reconciliation -f "$ROOT/docker-compose.yml" build app >> "$LOG" 2>&1; then
      restore_package_failure; write_status error "新版镜像构建失败，已恢复旧版本。" "" "" "" "package"; finish_request; exit 0
    fi
    docker compose -p ecommerce-reconciliation -f "$ROOT/docker-compose.yml" up -d db >> "$LOG" 2>&1 || true
    if ! docker compose -p ecommerce-reconciliation -f "$ROOT/docker-compose.yml" run --rm --no-deps app python /app/scripts/migrate.py >> "$LOG" 2>&1; then
      restore_package_failure; write_status error "数据库迁移/版本校验失败，已恢复旧版本。" "" "" "" "package"; finish_request; exit 0
    fi
    if ! docker compose -p ecommerce-reconciliation -f "$ROOT/docker-compose.yml" up -d app >> "$LOG" 2>&1; then
      restore_package_failure; write_status error "新版启动失败，已恢复旧版本。" "" "" "" "package"; finish_request; exit 0
    fi
    if ! "$ROOT/scripts/wait-health.sh" >> "$LOG" 2>&1; then
      restore_package_failure; write_status error "新版健康检查失败，已恢复旧版本。" "" "" "" "package"; finish_request; exit 0
    fi

    # 成功后记录“代码备份 + DB 备份”，使网页手工回滚对上传包/GitHub API 更新同样有效。
    cat > "$ROOT/.deploy/last_update.env" <<META
UPDATE_MODE=package
PREVIOUS_VERSION=$PREVIOUS_VERSION
CURRENT_VERSION=$PACKAGE_VERSION
PRE_UPDATE_CODE_BACKUP=$CODE_BACKUP
PRE_UPDATE_DB_BACKUP=$DB_BACKUP
UPDATE_FINISHED_AT=$(now)
META
    printf '%s\tpackage\t%s\t%s\t%s\t%s\n' "$(now)" "$PREVIOUS_VERSION" "$PACKAGE_VERSION" "$CODE_BACKUP" "$DB_BACKUP" >> "$ROOT/.deploy/history.tsv"

    write_status success "更新包 v$PACKAGE_VERSION 已安装成功；数据库版本已校验。" "0" "" "" "package"
    rm -rf "$STAGE" 2>/dev/null || true
    finish_request
    ;;

  *)
    write_status error "未知更新动作。" "" "" "" "unknown"; finish_request
    ;;
esac
