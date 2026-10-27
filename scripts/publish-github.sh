#!/bin/sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
OWNER="${GITHUB_OWNER:-jinfenghua1990}"
REPO="${1:-ecommerce-reconciliation}"
VISIBILITY="${2:---private}"

if ! command -v gh >/dev/null 2>&1; then
  echo "需要 GitHub CLI (gh)。也可以先在 GitHub 网页创建空仓库，再运行 scripts/bootstrap-git.sh。" >&2
  exit 2
fi

gh auth status >/dev/null
cd "$ROOT"
[ -d .git ] || git init
git checkout -B main
git add .
if ! git diff --cached --quiet; then
  git commit -m "release: ecommerce reconciliation v$(cat VERSION)"
fi

if gh repo view "$OWNER/$REPO" >/dev/null 2>&1; then
  git remote remove origin 2>/dev/null || true
  git remote add origin "https://github.com/$OWNER/$REPO.git"
  git push -u origin main
else
  gh repo create "$OWNER/$REPO" "$VISIBILITY" --source=. --remote=origin --push
fi

echo "https://github.com/$OWNER/$REPO"
