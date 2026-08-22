#!/usr/bin/env bash
set -euo pipefail

install_root="${SKILLGO_INSTALL_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
deploy_env="${SKILLGO_DEPLOY_ENV:-deploy/ecs.env}"
cd "$install_root"

fail() {
  echo "PREFLIGHT_FAILED: $*" >&2
  exit 1
}

for command_name in docker stat grep sed; do
  command -v "$command_name" >/dev/null 2>&1 || fail "missing command: $command_name"
done

[ -f .env ] || fail "missing .env; copy .env.example and configure it"
[ -f "$deploy_env" ] || fail "missing $deploy_env; copy deploy/ecs.env.example and configure it"

if grep -Eq '^(POSTGRES_PASSWORD|SKILLGO_JWT_SECRET|SKILLGO_BOOTSTRAP_PASSWORD)=[[:space:]]*(replace-|<)' .env; then
  fail ".env still contains placeholder secrets"
fi

docker compose version >/dev/null
compose=(docker compose --env-file .env --env-file "$deploy_env")
"${compose[@]}" --profile sandbox config --quiet

configured_gid="$(sed -n 's/^SKILLGO_DOCKER_GID=//p' "$deploy_env" | tail -n 1 | tr -d '\r[:space:]')"
actual_gid="$(stat -c '%g' /var/run/docker.sock 2>/dev/null || true)"
[ -n "$actual_gid" ] || fail "cannot inspect /var/run/docker.sock"
[ "$configured_gid" = "$actual_gid" ] || fail "SKILLGO_DOCKER_GID=$configured_gid, Docker socket GID=$actual_gid"

docker info --format '{{json .Runtimes}}' | grep -q 'runsc' || fail "Docker runtime runsc is not registered"

sandbox_image="$(sed -n 's/^SKILLGO_SANDBOX_IMAGE=//p' "$deploy_env" | tail -n 1 | tr -d '\r[:space:]')"
sandbox_image="${sandbox_image:-skillgo/sandbox-runtime:local}"
docker image inspect "$sandbox_image" >/dev/null 2>&1 || fail "sandbox image not found: $sandbox_image"

echo "PREFLIGHT_OK"
echo "install_root=$install_root"
echo "deploy_env=$deploy_env"
echo "docker_gid=$actual_gid"
echo "sandbox_image=$sandbox_image"
