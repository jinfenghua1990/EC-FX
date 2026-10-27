#!/bin/sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
REMOTE="${1:-}"
BRANCH="${2:-main}"

if [ -z "$REMOTE" ]; then
  echo "用法: $0 https://github.com/OWNER/ecommerce-reconciliation.git [branch]" >&2
  exit 2
fi

cd "$ROOT"
if [ ! -d .git ]; then
  git init
fi

git checkout -B "$BRANCH"
git add .
if ! git diff --cached --quiet; then
  git commit -m "chore: bootstrap reconciliation ops v$(cat VERSION)"
fi

git remote remove origin 2>/dev/null || true
git remote add origin "$REMOTE"
git push -u origin "$BRANCH"
echo "已连接 GitHub: $REMOTE ($BRANCH)"
