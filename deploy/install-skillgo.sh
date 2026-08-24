#!/usr/bin/env bash
set -euo pipefail

install_root="${SKILLGO_INSTALL_ROOT:-/opt/skillgo}"
cd "$install_root"

if [ -f .deploy/skillgo-src.tar.gz ]; then
  tar -xzf .deploy/skillgo-src.tar.gz -C "$install_root"
fi

source deploy/deploy-lib.sh

deploy_env="${SKILLGO_DEPLOY_ENV:-deploy/ecs.env}"
if [ ! -f "$deploy_env" ]; then
  echo "Missing $deploy_env; copy deploy/ecs.env.example and adjust it first" >&2
  exit 1
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

deploy_revision="$(resolve_deploy_revision)"

compose --profile build-only build sandbox-runtime
compose build api web
compose --profile sandbox build worker
compose up -d db
wait_for_service_health db
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

compose --profile sandbox up -d api worker
wait_for_service_health api
wait_for_service_health worker
# Nginx resolves the api service name when it starts. Recreate Web after API is
# healthy so a backend-only upgrade cannot leave Nginx using the old container IP.
compose up -d --force-recreate web
verify_web_routes
record_deploy_revision "$deploy_revision"
compose --profile sandbox ps
echo "SKILLGO_DEPLOY_OK"
