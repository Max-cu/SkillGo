#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$repo_root/deploy/deploy-lib.sh"

test_root="$(mktemp -d)"
trap 'rm -rf "$test_root"' EXIT
cd "$test_root"
mkdir -p .deploy

expected_revision="0123456789abcdef0123456789abcdef01234567"
printf '%s\n' "$expected_revision" > .deploy/revision.pending
actual_revision="$(resolve_deploy_revision)"
test "$actual_revision" = "$expected_revision"

record_deploy_revision "$actual_revision" >/dev/null
test "$(cat .deploy/revision)" = "$expected_revision"
test ! -e .deploy/revision.pending

SKILLGO_DEPLOY_REVISION=invalid-revision
export SKILLGO_DEPLOY_REVISION
if resolve_deploy_revision >/dev/null 2>&1; then
  echo "Invalid deploy revision was accepted" >&2
  exit 1
fi
unset SKILLGO_DEPLOY_REVISION

compose() {
  if [ "$1" = "port" ] && [ "$2" = "web" ] && [ "$3" = "8080" ]; then
    printf '0.0.0.0:18080\n'
    return 0
  fi
  return 1
}
test "$(web_base_url)" = "http://127.0.0.1:18080"

echo "DEPLOY_LIB_TEST_OK"
