#!/usr/bin/env sh
set -eu

failures=0

pass() {
  printf '[PASS] %s\n' "$1"
}

fail() {
  printf '[FAIL] %s\n' "$1"
  failures=$((failures + 1))
}

if [ "$(uname -s)" = "Linux" ]; then
  pass "native Linux host"
else
  fail "native Linux host required"
fi

if command -v docker >/dev/null 2>&1; then
  pass "docker command found"
else
  fail "docker command not found"
fi

if docker info >/dev/null 2>&1; then
  pass "Docker daemon reachable"
else
  fail "Docker daemon is not reachable"
fi

if command -v runsc >/dev/null 2>&1; then
  pass "runsc command found"
else
  fail "runsc is not installed"
fi

runtimes="$(docker info --format '{{json .Runtimes}}' 2>/dev/null || true)"
case "$runtimes" in
  *runsc*) pass "Docker runtime runsc is registered" ;;
  *) fail "Docker runtime runsc is not registered" ;;
esac

if [ -f /sys/fs/cgroup/cgroup.controllers ]; then
  pass "cgroup v2 detected"
else
  fail "cgroup v2 was not detected"
fi

if [ -r /proc/sys/user/max_user_namespaces ]; then
  value="$(cat /proc/sys/user/max_user_namespaces)"
  if [ "$value" -gt 0 ]; then
    pass "user namespaces enabled"
  else
    fail "user namespaces disabled"
  fi
fi

kernel="$(uname -r)"
printf '[INFO] kernel=%s\n' "$kernel"
printf '[INFO] docker=%s\n' "$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo unknown)"
printf '[INFO] runtimes=%s\n' "$runtimes"
printf '[INFO] filesystem=%s\n' "$(df -T /var/lib/docker 2>/dev/null | tail -n 1 || true)"

if [ "$failures" -ne 0 ]; then
  printf '[RESULT] %s preflight check(s) failed\n' "$failures"
  exit 1
fi

printf '[RESULT] Linux gVisor preflight passed\n'
