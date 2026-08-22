#!/usr/bin/env bash
set -euo pipefail

install_root="${SKILLGO_INSTALL_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
deploy_env="${SKILLGO_DEPLOY_ENV:-deploy/ecs.env}"
backup_root="${SKILLGO_BACKUP_ROOT:-$install_root/backups}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_dir="$backup_root/$timestamp"
cd "$install_root"

[ -f .env ] || { echo "Missing .env" >&2; exit 1; }
[ -f "$deploy_env" ] || { echo "Missing $deploy_env" >&2; exit 1; }
[ ! -e "$backup_dir" ] || { echo "Backup already exists: $backup_dir" >&2; exit 1; }

mkdir -p "$backup_dir"
cleanup_incomplete() {
  if [ ! -f "$backup_dir/manifest.txt" ]; then
    rm -rf -- "$backup_dir"
  fi
}
trap cleanup_incomplete EXIT

compose=(docker compose --env-file .env --env-file "$deploy_env")
"${compose[@]}" up -d db >/dev/null
# Variables in the following single-quoted command expand inside the DB container.
# shellcheck disable=SC2016
"${compose[@]}" exec -T db sh -lc \
  'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > "$backup_dir/database.dump"

docker run --rm \
  -e TARGET_UID="$(id -u)" \
  -e TARGET_GID="$(id -g)" \
  -v skillgo_skillgo-storage:/source:ro \
  -v "$backup_dir:/backup" \
  postgres:16-alpine \
  sh -lc 'tar -czf /backup/storage.tar.gz -C /source . && chown "$TARGET_UID:$TARGET_GID" /backup/storage.tar.gz'

install -m 600 .env "$backup_dir/app.env"
install -m 600 "$deploy_env" "$backup_dir/deploy.env"

{
  echo "created_at=$timestamp"
  echo "git_commit=$(git rev-parse HEAD 2>/dev/null || echo unknown)"
  echo "database_sha256=$(sha256sum "$backup_dir/database.dump" | cut -d' ' -f1)"
  echo "storage_sha256=$(sha256sum "$backup_dir/storage.tar.gz" | cut -d' ' -f1)"
} > "$backup_dir/manifest.txt"
chmod 600 "$backup_dir/manifest.txt"

trap - EXIT
echo "BACKUP_OK"
echo "backup_dir=$backup_dir"
