#!/bin/sh
set -eu
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
BRANCH="${UPDATE_BRANCH:-main}"
cd "$ROOT"

if ! command -v git >/dev/null 2>&1; then
  echo "找不到 git；群晖需要先安装 Git / Git Server，或在其他机器维护代码。" >&2
  exit 2
fi
if [ ! -d .git ]; then
  echo "当前目录尚未绑定 GitHub。" >&2
  exit 3
fi

git fetch --prune origin "$BRANCH"
LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse "origin/$BRANCH")"
echo "本机: $LOCAL"
echo "远端: $REMOTE"
if [ "$LOCAL" = "$REMOTE" ]; then
  echo "状态: 已是最新版本"
else
  echo "状态: 有新版本可更新"
  git log --oneline --no-decorate "$LOCAL..$REMOTE" | head -20
fi
