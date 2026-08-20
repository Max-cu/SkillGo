#!/usr/bin/env bash
set -euo pipefail
docker exec -i skillgo-worker-1 python - < /opt/skillgo/.deploy/sandbox-selftest.py
