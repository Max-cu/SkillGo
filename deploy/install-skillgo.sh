#!/usr/bin/env bash
set -euo pipefail

install_root="${SKILLGO_INSTALL_ROOT:-/opt/skillgo}"
cd "$install_root"

deploy_env="${SKILLGO_DEPLOY_ENV:-deploy/ecs.env}"
if [ ! -f "$deploy_env" ]; then
  echo "Missing $deploy_env; copy deploy/ecs.env.example and adjust it first" >&2
  exit 1
fi

if [ -f .deploy/skillgo-src.tar.gz ]; then
  tar -xzf .deploy/skillgo-src.tar.gz -C "$install_root"
fi
if [ -f .deploy/.env ]; then
  install -m 600 .deploy/.env "$install_root/.env"
fi
if [ ! -f .env ]; then
  echo "Missing .env; copy .env.example and configure production secrets first" >&2
  exit 1
fi

compose() {
  docker compose --env-file .env --env-file "$deploy_env" "$@"
}

compose --profile build-only build sandbox-runtime
compose build api web
compose --profile sandbox build worker
compose up -d db

for _ in $(seq 1 60); do
  if [ "$(docker inspect --format '{{.State.Health.Status}}' skillgo-db-1 2>/dev/null || true)" = "healthy" ]; then
    break
  fi
  sleep 2
done
compose exec -T db sh -lc 'pg_isready -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'

if [ -f .deploy/skillgo.dump ] && [ -f .deploy/storage.tar.gz ] && [ ! -f .deploy/data-restored.marker ]; then
  docker cp .deploy/skillgo.dump skillgo-db-1:/tmp/skillgo.dump
  compose exec -T db sh -lc 'pg_restore -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner /tmp/skillgo.dump'
  docker run --rm \
    -v skillgo_skillgo-storage:/target \
    -v "$install_root/.deploy:/backup:ro" \
    postgres:16-alpine \
    tar -xzf /backup/storage.tar.gz -C /target
  touch .deploy/data-restored.marker
fi

compose --profile sandbox up -d api web worker

for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1/ >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

curl -fsS http://127.0.0.1/ >/dev/null
compose --profile sandbox ps
echo "SKILLGO_DEPLOY_OK"
