#!/bin/sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
. "$ROOT/scripts/compose-lib.sh"

if [ ! -f "$ROOT/.env" ]; then
  echo "缺少 $ROOT/.env。请先从 .env.example 复制并填写旺店通凭证。" >&2
  exit 2
fi

mkdir -p "$ROOT/data" "$ROOT/backups" "$ROOT/.deploy"
# Synology Container Manager 常见项目文件名；保持为非 Git 文件，方便 UI 查看。
cp -f "$ROOT/docker-compose.yml" "$ROOT/compose.yaml"

# 先启动数据库，并在首次 V0.5 迁移前备份已有数据库。
compose up -d db
sleep 3
if compose exec -T db pg_isready -U recon -d reconciliation >/dev/null 2>&1; then
  echo "[install] 迁移前备份现有数据库"
  "$ROOT/scripts/backup.sh" || { echo "备份失败，停止升级。" >&2; exit 5; }
fi

echo "[install] 构建固定依赖镜像（以后重启不再 pip install）"
compose build app
compose up -d db app
"$ROOT/scripts/wait-health.sh"

echo "[install] 完成"
echo "局域网访问: http://<群晖IP>:8010"

echo ""
echo "[install] 网页更新中心还需要在 DSM → 控制面板 → 任务计划中创建一个每分钟任务："
echo "  /bin/sh /volume1/docker/ecommerce-reconciliation/scripts/synology-task-web-update.sh"
echo "并在 .env 设置 UPDATE_ADMIN_PASSWORD。完成一次后，以后可直接在网页右上角“系统更新”操作。"
