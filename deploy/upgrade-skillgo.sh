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

source deploy/deploy-lib.sh

compose() {
  docker compose --env-file .env --env-file "$deploy_env" "$@"
}

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
target_commit="$(git rev-parse HEAD)"

if ! compose --profile build-only build sandbox-runtime \
  || ! compose --profile sandbox build api web worker \
  || ! compose up -d db \
  || ! wait_for_service_health db \
  || ! compose --profile sandbox up -d api worker \
  || ! wait_for_service_health api \
  || ! wait_for_service_health worker \
  || ! compose up -d --force-recreate web \
  || ! verify_web_routes; then
  echo "UPGRADE_FAILED" >&2
  echo "previous_commit=$old_commit" >&2
  echo "backup_dir=$backup_dir" >&2
  echo "The database may already be migrated; restore the backup before checking out old code." >&2
  exit 1
fi

SKILLGO_INSTALL_ROOT="$install_root" SKILLGO_DEPLOY_ENV="$deploy_env" bash deploy/verify-ecs.sh
record_deploy_revision "$target_commit"

echo "UPGRADE_OK"
echo "from=$old_commit"
echo "to=$target_version"
echo "backup_dir=$backup_dir"
