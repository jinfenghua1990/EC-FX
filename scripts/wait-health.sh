#!/bin/sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
. "$ROOT/scripts/compose-lib.sh"
TIMEOUT="${HEALTH_TIMEOUT:-180}"
START="$(date +%s)"

while :; do
  CID="$(compose ps -q app 2>/dev/null || true)"
  if [ -n "$CID" ]; then
    STATUS="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$CID" 2>/dev/null || true)"
    case "$STATUS" in
      healthy)
        echo "[health] app healthy"
        exit 0
        ;;
      exited|dead)
        echo "[health] app $STATUS" >&2
        compose logs --tail=80 app >&2 || true
        exit 1
        ;;
    esac
  fi
  NOW="$(date +%s)"
  if [ $((NOW-START)) -ge "$TIMEOUT" ]; then
    echo "[health] ${TIMEOUT}s 内未健康" >&2
    compose logs --tail=80 app >&2 || true
    exit 1
  fi
  sleep 3
done
