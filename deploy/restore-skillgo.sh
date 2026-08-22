#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 2 ] || [ "$1" != "--confirm" ]; then
  echo "Usage: $0 --confirm <backup-directory>" >&2
  echo "This replaces the current database and managed file storage." >&2
  exit 2
fi

install_root="${SKILLGO_INSTALL_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
deploy_env="${SKILLGO_DEPLOY_ENV:-deploy/ecs.env}"
backup_dir="$(cd "$2" && pwd)"
cd "$install_root"

for required in database.dump storage.tar.gz manifest.txt; do
  [ -f "$backup_dir/$required" ] || { echo "Missing $backup_dir/$required" >&2; exit 1; }
done

expected_database="$(sed -n 's/^database_sha256=//p' "$backup_dir/manifest.txt")"
expected_storage="$(sed -n 's/^storage_sha256=//p' "$backup_dir/manifest.txt")"
[ "$(sha256sum "$backup_dir/database.dump" | cut -d' ' -f1)" = "$expected_database" ] || { echo "Database checksum mismatch" >&2; exit 1; }
[ "$(sha256sum "$backup_dir/storage.tar.gz" | cut -d' ' -f1)" = "$expected_storage" ] || { echo "Storage checksum mismatch" >&2; exit 1; }

compose=(docker compose --env-file .env --env-file "$deploy_env")
"${compose[@]}" stop api worker web || true
"${compose[@]}" up -d db
# Variables in the following single-quoted commands expand inside the DB container.
# shellcheck disable=SC2016
"${compose[@]}" exec -T db sh -lc 'until pg_isready -h 127.0.0.1 -U "$POSTGRES_USER" -d postgres; do sleep 1; done'

docker cp "$backup_dir/database.dump" skillgo-db-1:/tmp/skillgo-restore.dump
# shellcheck disable=SC2016
"${compose[@]}" exec -T db sh -lc '
  export PGPASSWORD="$POSTGRES_PASSWORD"
  dropdb -h 127.0.0.1 -U "$POSTGRES_USER" --if-exists --force "$POSTGRES_DB"
  createdb -h 127.0.0.1 -U "$POSTGRES_USER" "$POSTGRES_DB"
  pg_restore -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner /tmp/skillgo-restore.dump
  rm -f /tmp/skillgo-restore.dump
'

docker run --rm \
  -v skillgo_skillgo-storage:/target \
  -v "$backup_dir:/backup:ro" \
  postgres:16-alpine \
  sh -lc 'find /target -mindepth 1 -depth -delete && tar -xzf /backup/storage.tar.gz -C /target'

"${compose[@]}" --profile sandbox up -d api web worker
echo "RESTORE_OK"
echo "backup_dir=$backup_dir"
echo "Run deploy/verify-ecs.sh before reopening the service to users."
