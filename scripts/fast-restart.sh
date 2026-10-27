#!/bin/sh
set -eu
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT"

if ! command -v docker >/dev/null 2>&1; then
  echo "[fast] 找不到 docker 命令，请在 DSM Container Manager 中对项目执行一次‘重建/重新创建’。"
  exit 2
fi

if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  echo "[fast] 找不到 docker compose，请在 DSM Container Manager 中对项目执行一次‘重建/重新创建’。"
  exit 2
fi

echo "[fast] 重新创建 app（不 build、不重建数据库）"
$COMPOSE -f compose.yaml up -d --no-deps --force-recreate app

# 简短健康检查
for i in $(seq 1 30); do
  if wget -q -O /dev/null http://127.0.0.1:8010/healthz 2>/dev/null || curl -fsS http://127.0.0.1:8010/healthz >/dev/null 2>&1; then
    echo "[fast] app healthy"
    exit 0
  fi
  sleep 2
done

echo "[fast] app 未在 60 秒内通过健康检查，请查看 Container Manager 日志。"
exit 1
