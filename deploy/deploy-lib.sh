#!/usr/bin/env bash

# Shared deployment helpers. Callers must provide a `compose` function that
# invokes Docker Compose with the correct env files and profiles.

wait_for_service_health() {
  local service="$1"
  local attempts="${2:-60}"
  local delay_seconds="${3:-2}"
  local container_id=""
  local status=""

  for _ in $(seq 1 "$attempts"); do
    container_id="$(compose ps -q "$service" 2>/dev/null || true)"
    if [ -n "$container_id" ]; then
      status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id" 2>/dev/null || true)"
      if [ "$status" = "healthy" ] || [ "$status" = "running" ]; then
        return 0
      fi
      if [ "$status" = "unhealthy" ] || [ "$status" = "exited" ] || [ "$status" = "dead" ]; then
        echo "Service $service entered terminal state: $status" >&2
        compose logs --tail=100 "$service" >&2 || true
        return 1
      fi
    fi
    sleep "$delay_seconds"
  done

  echo "Timed out waiting for service $service to become healthy (last status: ${status:-missing})" >&2
  compose logs --tail=100 "$service" >&2 || true
  return 1
}

web_base_url() {
  local published=""
  local port=""

  published="$(compose port web 8080 2>/dev/null | head -n 1)"
  port="${published##*:}"
  if ! [[ "$port" =~ ^[0-9]+$ ]]; then
    echo "Could not determine the published Web port from: ${published:-<empty>}" >&2
    return 1
  fi
  printf 'http://127.0.0.1:%s\n' "$port"
}

verify_web_routes() {
  local attempts="${1:-60}"
  local delay_seconds="${2:-2}"
  local base_url=""
  local health_body=""

  base_url="$(web_base_url)"
  for _ in $(seq 1 "$attempts"); do
    if curl -fsS "$base_url/" >/dev/null 2>&1; then
      health_body="$(curl -fsS "$base_url/health" 2>/dev/null || true)"
      if [[ "$health_body" =~ \"status\"[[:space:]]*:[[:space:]]*\"healthy\" ]]; then
        printf 'web_url=%s\n' "$base_url"
        return 0
      fi
    fi
    sleep "$delay_seconds"
  done

  echo "Web route verification failed for $base_url/ and $base_url/health" >&2
  compose logs --tail=100 web api >&2 || true
  return 1
}

resolve_deploy_revision() {
  local revision="${SKILLGO_DEPLOY_REVISION:-}"

  if [ -z "$revision" ] && [ -f .deploy/revision.pending ]; then
    revision="$(head -n 1 .deploy/revision.pending)"
  fi
  if [ -z "$revision" ] && command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    revision="$(git rev-parse HEAD)"
  fi

  revision="${revision//$'\r'/}"
  revision="${revision//$'\n'/}"
  if [ -n "$revision" ] && ! [[ "$revision" =~ ^[0-9a-fA-F]{40}$ ]]; then
    echo "Deploy revision must be a full 40-character Git commit SHA" >&2
    return 1
  fi
  printf '%s\n' "$revision"
}

record_deploy_revision() {
  local revision="$1"
  local revision_tmp=""

  if [ -z "$revision" ]; then
    echo "deployed_revision=unchanged"
    return 0
  fi

  mkdir -p .deploy
  revision_tmp="$(mktemp .deploy/revision.XXXXXX)"
  printf '%s\n' "$revision" > "$revision_tmp"
  chmod 600 "$revision_tmp"
  mv -f "$revision_tmp" .deploy/revision
  rm -f .deploy/revision.pending
  echo "deployed_revision=$revision"
}
