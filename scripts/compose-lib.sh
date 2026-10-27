#!/bin/sh
set -eu

PROJECT_NAME="${COMPOSE_PROJECT_NAME:-ecommerce-reconciliation}"
ROOT="${ROOT:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}"
COMPOSE_FILE="${COMPOSE_FILE:-$ROOT/docker-compose.yml}"

compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" "$@"
  elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" "$@"
  else
    echo "找不到 docker compose / docker-compose" >&2
    return 127
  fi
}
