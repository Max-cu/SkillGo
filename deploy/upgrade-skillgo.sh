#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "Usage: $0 <version-tag>" >&2
  exit 2
fi

target_version="$1"
install_root="${SKILLGO_INSTALL_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
deploy_env="${SKILLGO_DEPLOY_ENV:-deploy/ecs.env}"
cd "$install_root"

if [ -n "$(git status --porcelain)" ]; then
  echo "Refusing to upgrade a dirty working tree" >&2
  exit 1
fi

old_commit="$(git rev-parse HEAD)"
SKILLGO_INSTALL_ROOT="$install_root" SKILLGO_DEPLOY_ENV="$deploy_env" bash deploy/preflight.sh
backup_output="$(SKILLGO_INSTALL_ROOT="$install_root" SKILLGO_DEPLOY_ENV="$deploy_env" bash deploy/backup-skillgo.sh)"
echo "$backup_output"
backup_dir="$(printf '%s\n' "$backup_output" | sed -n 's/^backup_dir=//p')"

git fetch --tags origin
git rev-parse --verify "refs/tags/$target_version^{commit}" >/dev/null
git checkout "$target_version"

compose=(docker compose --env-file .env --env-file "$deploy_env")
if ! "${compose[@]}" --profile build-only build sandbox-runtime \
  || ! "${compose[@]}" --profile sandbox up -d --build; then
  echo "UPGRADE_FAILED" >&2
  echo "previous_commit=$old_commit" >&2
  echo "backup_dir=$backup_dir" >&2
  echo "The database may already be migrated; restore the backup before checking out old code." >&2
  exit 1
fi

for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1/health >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
curl -fsS http://127.0.0.1/health >/dev/null
SKILLGO_INSTALL_ROOT="$install_root" SKILLGO_DEPLOY_ENV="$deploy_env" bash deploy/verify-ecs.sh

echo "UPGRADE_OK"
echo "from=$old_commit"
echo "to=$target_version"
echo "backup_dir=$backup_dir"
