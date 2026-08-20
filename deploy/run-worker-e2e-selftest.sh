#!/usr/bin/env bash
set -euo pipefail

docker run --rm -i \
  --user 0:0 \
  -e SKILLGO_ENVIRONMENT=test \
  -e SKILLGO_DATABASE_URL=sqlite:////tmp/worker-selftest.db \
  -e SKILLGO_STORAGE_ROOT=/tmp/skillgo-selftest-storage \
  -e SKILLGO_SANDBOX_WORKER_ENABLED=true \
  -e SKILLGO_SANDBOX_RUNTIME=runsc \
  -e SKILLGO_SANDBOX_IMAGE=skillgo/sandbox-runtime:local \
  -v /var/run/docker.sock:/var/run/docker.sock \
  skillgo-worker:latest \
  python - < /opt/skillgo/.deploy/worker-e2e-selftest.py
